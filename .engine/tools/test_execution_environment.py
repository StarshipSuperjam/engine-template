#!/usr/bin/env python3
"""Self-tests for execution-environment awareness: the execution-state.v1 schema, the committed genesis
baseline, the schema-kind rule that refuses a malformed baseline, and the deriver's posture logic.

Run: uv run --directory .engine --frozen -- python tools/selftest.py

These lock the load-bearing teeth: the schema bites on each malformed shape (missing/out-of-grammar fields,
a bad status, a non-sha floor value, a non-UTC as_of); the committed genesis baseline conforms; the rule
names its schema directly (foundation, not a catalogued surface) and refuses a malformed baseline as a plain
finding; and compare() enforces the two plan-gate rules — a qualified-but-unverifiable entry NEVER matches
(an un-checkable floor is not a pass), and a qualification counts only in the repo it was made for.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import shutil
import io
import contextlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import execution_environment as ee  # noqa: E402

_ROOT = validate.ROOT

EXEC_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "execution-state.v1.json"))
REAL_BASELINE = os.path.join(validate.ROOT, ".engine", "state", "execution.json")

_SHA = "sha256:" + "a" * 64
_SHB = "sha256:" + "b" * 64


def _valid_env(**over):
    base = {"status": "qualified", "as_of": "2026-07-27T14:30:00Z", "repo": "owner/repo",
            "engine_release": "0.3.2", "floors": {"CLAUDE.md": _SHA}, "model_alias": "opus",
            "evidence": "https://github.com/owner/repo/pull/1"}
    base.update(over)
    return base


def _valid_baseline(**claude_over):
    return {"schema_version": 1,
            "environments": {"claude": _valid_env(**claude_over), "codex": dict(ee._GENESIS_ENVIRONMENT)}}


def _errors(instance):
    return list(validate.Draft202012Validator(EXEC_SCHEMA).iter_errors(instance))


def _observed(*, runtime="claude", repo="owner/repo", engine_release="0.3.2", floors=None):
    return {"runtime": runtime, "repo": repo, "engine_release": engine_release,
            "floors": {"CLAUDE.md": _SHA} if floors is None else floors}


def _run_kind(kind_fn, rule, files):
    orig = validate.target_files
    validate.target_files = lambda r: list(files)
    try:
        return kind_fn(rule, {})
    finally:
        validate.target_files = orig


class TestExecutionSchema(unittest.TestCase):
    def test_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(EXEC_SCHEMA)

    def test_committed_genesis_baseline_conforms(self):
        self.assertEqual(_errors(validate.load_json(REAL_BASELINE)), [])

    def test_genesis_helper_conforms(self):
        self.assertEqual(_errors(ee.genesis_baseline()), [])

    def test_valid_qualified_baseline_passes(self):
        self.assertEqual(_errors(_valid_baseline()), [])

    def test_missing_top_level_required_is_flagged(self):
        for drop in ("schema_version", "environments"):
            bad = {k: v for k, v in _valid_baseline().items() if k != drop}
            self.assertTrue(_errors(bad), f"missing {drop} should fail")

    def test_missing_environment_is_flagged(self):
        for drop in ("claude", "codex"):
            bad = _valid_baseline()
            del bad["environments"][drop]
            self.assertTrue(_errors(bad), f"missing environment {drop} should fail")

    def test_missing_env_field_is_flagged(self):
        for drop in ("status", "as_of", "repo", "engine_release", "floors", "model_alias", "evidence"):
            bad = _valid_baseline()
            del bad["environments"]["claude"][drop]
            self.assertTrue(_errors(bad), f"missing env.{drop} should fail")

    def test_field_outside_the_grammar_is_flagged(self):
        # An extra field at the root and inside an environment — the "no store, no growth" guard.
        self.assertTrue(_errors({**_valid_baseline(), "extra": 1}))
        bad = _valid_baseline()
        bad["environments"]["claude"]["note"] = "history"
        self.assertTrue(_errors(bad))
        bad2 = _valid_baseline()
        bad2["environments"]["extra_env"] = dict(ee._GENESIS_ENVIRONMENT)
        self.assertTrue(_errors(bad2))

    def test_wrong_schema_version_is_flagged(self):
        self.assertTrue(_errors({**_valid_baseline(), "schema_version": 2}))
        self.assertTrue(_errors({**_valid_baseline(), "schema_version": "1"}))

    def test_bad_status_is_flagged(self):
        self.assertTrue(_errors(_valid_baseline(status="blessed")))
        self.assertTrue(_errors(_valid_baseline(status=None)))

    def test_floor_value_must_be_sha_or_null(self):
        for ok in ({}, {"CLAUDE.md": _SHA}, {"CLAUDE.md": None}, {"CLAUDE.md": _SHA, "AGENTS.md": _SHB}):
            self.assertEqual(_errors(_valid_baseline(floors=ok)), [], f"floors={ok!r} should pass")
        for bad in ({"CLAUDE.md": "abc"}, {"CLAUDE.md": "sha256:XYZ"}, {"CLAUDE.md": 1}):
            self.assertTrue(_errors(_valid_baseline(floors=bad)), f"floors={bad!r} should fail")

    def test_as_of_accepts_null_and_utc_z_only(self):
        for ok in (None, "2026-07-27T14:30:00Z"):
            self.assertEqual(_errors(_valid_baseline(as_of=ok)), [], f"{ok!r} should pass")
        # fractional seconds now rejected too (#631: fixed-width so raw-string comparisons sort right)
        for bad in ("2026-07-27T14:30:00.5Z", "2026-07-27T14:30:00+02:00", "2026-07-27T14:30:00", "nope"):
            self.assertTrue(_errors(_valid_baseline(as_of=bad)), f"{bad!r} should fail")

    def test_empty_string_pointers_flagged(self):
        for field in ("repo", "engine_release", "model_alias", "evidence"):
            self.assertTrue(_errors(_valid_baseline(**{field: ""})), f"empty {field} should fail")


class TestExecutionRuleIntegration(unittest.TestCase):
    def _rule(self):
        return validate.load_json(os.path.join(validate.CHECK_DIR, "execution-state.json"))

    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        rule = self._rule()
        self.assertEqual(list(validate.Draft202012Validator(check_schema).iter_errors(rule)), [])
        self.assertIn("CI", rule.get("suites", []))

    def test_real_baseline_passes_via_the_schema_routed_rule(self):
        rule = self._rule()
        self.assertEqual(rule.get("params"), {"schema": ".engine/schemas/execution-state.v1.json"})
        passed, found = _run_kind(validate.kind_schema, rule, [REAL_BASELINE])
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_malformed_json_is_refused_as_a_plain_finding(self):
        rule = self._rule()
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "execution.json")
            with open(bad, "w") as fh:
                fh.write("{ not json")
            passed, found = _run_kind(validate.kind_schema, rule, [bad])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_schema_invalid_baseline_is_refused_at_tier(self):
        rule = self._rule()
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "execution.json")
            with open(bad, "w") as fh:
                json.dump({"schema_version": 1}, fh)   # missing environments
            passed, found = _run_kind(validate.kind_schema, rule, [bad])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))


class TestCompare(unittest.TestCase):
    def test_matched_when_repo_release_and_floors_agree(self):
        r = ee.compare(_observed(), _valid_baseline())
        self.assertEqual(r["posture"], "matched")
        self.assertEqual(r["drift"], [])

    def test_changed_on_floor_content(self):
        r = ee.compare(_observed(floors={"CLAUDE.md": _SHB}), _valid_baseline())
        self.assertEqual(r["posture"], "changed")
        self.assertEqual(r["drift"], ["CLAUDE.md"])

    def test_changed_on_engine_release(self):
        r = ee.compare(_observed(engine_release="0.4.0"), _valid_baseline())
        self.assertEqual(r["posture"], "changed")
        self.assertIn("engine release", r["drift"])

    def test_changed_when_floor_added(self):
        r = ee.compare(_observed(floors={"CLAUDE.md": _SHA, ".engine/conduct/extra.md": _SHB}),
                       _valid_baseline())
        self.assertEqual(r["posture"], "changed")
        self.assertIn(".engine/conduct/extra.md", r["drift"])

    def test_changed_when_floor_removed(self):
        r = ee.compare(_observed(floors={}), _valid_baseline())
        self.assertEqual(r["posture"], "changed")
        self.assertIn("CLAUDE.md", r["drift"])

    def test_unqualified_when_status_unqualified(self):
        base = _valid_baseline()
        base["environments"]["claude"]["status"] = "unqualified"
        self.assertEqual(ee.compare(_observed(), base)["posture"], "unqualified")

    def test_unqualified_when_repo_differs(self):
        # A baseline qualified for another repo (a shipped/foreign baseline) reads as not-ours, never as drift.
        r = ee.compare(_observed(repo="other/place"), _valid_baseline())
        self.assertEqual(r["posture"], "unqualified")
        self.assertEqual(r["drift"], [])

    def test_qualified_but_unresolvable_repo_never_matches(self):
        # Rule 1 for the repo component: a qualified baseline whose LIVE repo can't be resolved (observed None)
        # must degrade to conservative — never match on floor hashes alone (engine-shipped floors are not
        # repo-distinguishing, so a floors-only match would load the qualified posture in a repo never qualified).
        r = ee.compare(_observed(repo=None), _valid_baseline())
        self.assertNotEqual(r["posture"], "matched")
        self.assertEqual(r["posture"], "unqualified")

    def test_qualified_with_null_recorded_floor_never_matches(self):
        # BLOCKING-fix: an un-checkable recorded floor is not a pass — degrade to conservative, never matched.
        r = ee.compare(_observed(floors={"CLAUDE.md": None}),
                       _valid_baseline(floors={"CLAUDE.md": None}))
        self.assertNotEqual(r["posture"], "matched")
        self.assertEqual(r["posture"], "unqualified")

    def test_qualified_with_live_unreadable_floor_never_matches(self):
        # The recorded hash is real but the live file can't be read now -> unverifiable -> not matched.
        r = ee.compare(_observed(floors={"CLAUDE.md": None}), _valid_baseline(floors={"CLAUDE.md": _SHA}))
        self.assertNotEqual(r["posture"], "matched")

    def test_qualified_with_null_engine_release_never_matches(self):
        r = ee.compare(_observed(engine_release=None), _valid_baseline())
        self.assertNotEqual(r["posture"], "matched")

    def test_codex_environment_independently_evaluated(self):
        base = _valid_baseline()
        base["environments"]["codex"] = _valid_env(repo="owner/repo")
        self.assertEqual(ee.compare(_observed(runtime="codex"), base)["posture"], "matched")


class TestReadBaseline(unittest.TestCase):
    def test_missing_file_yields_genesis(self):
        with tempfile.TemporaryDirectory() as d:
            got = ee.read_baseline(d)
        self.assertEqual(got["environments"]["claude"]["status"], "unqualified")

    def test_malformed_file_raises_baseline_unreadable(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".engine", "state"))
            with open(os.path.join(d, ".engine", "state", "execution.json"), "w") as fh:
                fh.write("{ not json")
            with self.assertRaises(ee.BaselineUnreadable):
                ee.read_baseline(d)


def _fixture_root(d, *, claude="claude floor\n", agents="agents floor\n",
                  conduct=None, engine_release="0.3.2", baseline=None):
    """Lay out a minimal repo tree for observe()/record()/derive()."""
    if claude is not None:
        with open(os.path.join(d, "CLAUDE.md"), "w") as fh:
            fh.write(claude)
    if agents is not None:
        with open(os.path.join(d, "AGENTS.md"), "w") as fh:
            fh.write(agents)
    os.makedirs(os.path.join(d, ".engine", "conduct"), exist_ok=True)
    for name, body in (conduct or {"defaults.md": "conduct floor\n"}).items():
        with open(os.path.join(d, ".engine", "conduct", name), "w") as fh:
            fh.write(body)
    os.makedirs(os.path.join(d, ".engine", "state"), exist_ok=True)
    with open(os.path.join(d, ".engine", "engine.json"), "w") as fh:
        json.dump({"engine_release": engine_release}, fh)
    if baseline is not None:
        with open(os.path.join(d, ".engine", "state", "execution.json"), "w") as fh:
            json.dump(baseline, fh)
    return d


class TestObserveAndRecord(unittest.TestCase):
    def test_observe_hashes_the_floor_set(self):
        with tempfile.TemporaryDirectory() as d:
            _fixture_root(d, conduct={"defaults.md": "a\n", "operator.md": "b\n"})
            obs = ee.observe(provider="claude", repo="o/r", root=d)
        self.assertEqual(obs["runtime"], "claude")
        self.assertEqual(obs["engine_release"], "0.3.2")
        self.assertEqual(set(obs["floors"]),
                         {"CLAUDE.md", "AGENTS.md", ".engine/conduct/defaults.md",
                          ".engine/conduct/operator.md"})
        self.assertTrue(all(v.startswith("sha256:") for v in obs["floors"].values()))

    def test_record_stamps_qualified_and_round_trips_to_matched(self):
        with tempfile.TemporaryDirectory() as d:
            _fixture_root(d)
            entry = ee.record_qualification("claude", root=d, repo="o/r", model_alias="opus",
                                            now="2026-07-27T00:00:00Z")
            self.assertEqual(entry["status"], "qualified")
            self.assertEqual(entry["repo"], "o/r")
            # The written baseline is schema-valid and now derives as matched for the same repo/floors.
            self.assertEqual(_errors(ee.read_baseline(d)), [])
            posture = ee.derive(provider="claude", repo="o/r", root=d)
        self.assertEqual(posture["posture"], "matched")

    def test_record_refuses_when_a_floor_is_unobservable(self):
        # A listed floor file that exists but cannot be read hashes to None; record must refuse to stamp a
        # qualified snapshot around an un-checkable floor. Simulated by an observation carrying a null floor.
        with tempfile.TemporaryDirectory() as d:
            _fixture_root(d)
            orig = ee.observe
            ee.observe = lambda **kw: {"runtime": "claude", "repo": "o/r", "engine_release": "0.3.2",
                                       "floors": {"CLAUDE.md": None}}
            try:
                with self.assertRaises(ee.QualificationRefused):
                    ee.record_qualification("claude", root=d, repo="o/r")
            finally:
                ee.observe = orig

    def test_record_refuses_when_no_floors_present(self):
        # An empty floor set is itself unqualifiable — there is nothing to freeze a drift signal against.
        with tempfile.TemporaryDirectory() as d:
            _fixture_root(d)
            orig = ee.observe
            ee.observe = lambda **kw: {"runtime": "claude", "repo": "o/r", "engine_release": "0.3.2",
                                       "floors": {}}
            try:
                with self.assertRaises(ee.QualificationRefused):
                    ee.record_qualification("claude", root=d, repo="o/r")
            finally:
                ee.observe = orig

    def test_record_refuses_when_engine_release_unreadable(self):
        with tempfile.TemporaryDirectory() as d:
            _fixture_root(d)
            os.remove(os.path.join(d, ".engine", "engine.json"))
            with self.assertRaises(ee.QualificationRefused):
                ee.record_qualification("claude", root=d, repo="o/r")

    def test_record_refuses_when_repo_unresolvable(self):
        with tempfile.TemporaryDirectory() as d:
            _fixture_root(d)
            # repo not injected and the temp dir is not a git repo -> current_repo returns None -> refuse.
            with self.assertRaises(ee.QualificationRefused):
                ee.record_qualification("claude", root=d)

    def test_record_writes_but_never_commits(self):
        # The merge is the qualification act — record only writes the working-tree file, never a commit.
        with tempfile.TemporaryDirectory() as d:
            _fixture_root(d)
            for args in (["init", "-q"], ["add", "-A"],
                         ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base"]):
                subprocess.run(["git", *args], cwd=d, check=True, capture_output=True)
            head = lambda: subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                                          capture_output=True, text=True).stdout
            before = head()
            ee.record_qualification("claude", root=d, repo="o/r", now="2026-07-27T00:00:00Z")
            self.assertEqual(before, head(), "record_qualification must not create a commit")
            self.assertTrue(os.path.exists(os.path.join(d, ".engine", "state", "execution.json")))
            status = subprocess.run(["git", "status", "--porcelain"], cwd=d,
                                    capture_output=True, text=True).stdout
            self.assertTrue(status.strip(), "record leaves an uncommitted change in the working tree")


class TestDerive(unittest.TestCase):
    def test_genesis_baseline_is_unqualified(self):
        with tempfile.TemporaryDirectory() as d:
            _fixture_root(d, baseline=ee.genesis_baseline())
            self.assertEqual(ee.derive(provider="claude", repo="o/r", root=d)["posture"], "unqualified")

    def test_missing_baseline_is_unqualified(self):
        with tempfile.TemporaryDirectory() as d:
            _fixture_root(d)   # no execution.json written
            self.assertEqual(ee.derive(provider="claude", repo="o/r", root=d)["posture"], "unqualified")

    def test_unreadable_baseline_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            _fixture_root(d)
            with open(os.path.join(d, ".engine", "state", "execution.json"), "w") as fh:
                fh.write("{ not json")
            self.assertEqual(ee.derive(provider="claude", repo="o/r", root=d)["posture"], "unknown")


class TestSlugParsing(unittest.TestCase):
    def test_slug_from_common_origin_urls(self):
        with tempfile.TemporaryDirectory() as d:
            for url, want in (("git@github.com:owner/repo.git", "owner/repo"),
                              ("https://github.com/owner/repo.git", "owner/repo"),
                              ("https://github.com/owner/repo", "owner/repo")):
                subprocess.run(["git", "init", "-q"], cwd=d)
                subprocess.run(["git", "remote", "remove", "origin"], cwd=d,
                               capture_output=True)
                subprocess.run(["git", "remote", "add", "origin", url], cwd=d)
                self.assertEqual(ee.current_repo(d), want, f"{url} -> {want}")

    def test_slug_from_a_mixed_case_host(self):
        # Host names are case-insensitive by specification: `GitHub.com` parses like `github.com`, so a
        # qualification is not spuriously refused for a hand-configured mixed-case origin (#625). A mixed-case
        # look-alike still returns None — IGNORECASE folds only the literal host, not the anchor.
        with tempfile.TemporaryDirectory() as d:
            for url, want in (("git@GitHub.com:owner/repo.git", "owner/repo"),
                              ("https://GitHub.com/owner/repo.git", "owner/repo"),
                              ("https://notGitHub.com/owner/repo.git", None)):
                subprocess.run(["git", "init", "-q"], cwd=d)
                subprocess.run(["git", "remote", "remove", "origin"], cwd=d,
                               capture_output=True)
                subprocess.run(["git", "remote", "add", "origin", url], cwd=d)
                self.assertEqual(ee.current_repo(d), want, f"{url} -> {want}")


class TestBootPostureRelay(unittest.TestCase):
    """Boot wires the deriver's posture into the AI-facing Tier-0 briefing and pushes a drift alarm only on
    a 'changed' posture. Uses a real signals dict, mutating only the execution signal."""

    @classmethod
    def setUpClass(cls):
        import boot  # noqa: E402  (tools/ is on the path via the module-level insert)
        cls.boot = boot
        cls.signals = boot.gather_signals()

    def test_execution_signal_is_present(self):
        self.assertIn("execution", self.signals)
        self.assertIn(self.signals["execution"]["posture"],
                      ("matched", "changed", "unqualified", "unknown"))

    def test_changed_posture_pushes_an_alarm_keyed_execution(self):
        s = dict(self.signals)
        s["execution"] = {"runtime": "claude", "posture": "changed", "drift": ["CLAUDE.md"], "lines": []}
        keys = [a["key"] for a in self.boot._pushed_alarms(s)]
        self.assertIn("execution", keys)

    def test_non_changed_postures_push_no_alarm(self):
        for posture in ("matched", "unqualified", "unknown"):
            s = dict(self.signals)
            s["execution"] = {"runtime": "claude", "posture": posture, "drift": [], "lines": []}
            keys = [a["key"] for a in self.boot._pushed_alarms(s)]
            self.assertNotIn("execution", keys, f"{posture} must not push an alarm")

    def test_posture_block_reaches_the_assembled_pack(self):
        # The genesis baseline in this repo yields the conservative posture; its block must reach Tier 0.
        self.assertIn("EXECUTION POSTURE", self.boot.assemble_pack())


class TestRoutingData(unittest.TestCase):
    """The posture lines are schema-checked data (model-routing-postures.json): the boot-facing loader returns None
    on ANY miss and resolve_posture falls back to the built-in conservative default; the strict loader names
    the miss for the merge check; the fence parser is gone; the page's generated region is current, drifts
    are caught, and render restores them; the registered check is green live and bites its fixture."""

    FIXTURE = ".engine/_fixtures/model-routing/malformed-routing.json"

    def _scratch(self) -> str:
        tmp = tempfile.mkdtemp(prefix="model-routing-")
        for rel in (ee._ROUTING_REL, ee._ROUTING_SCHEMA_REL, ee._POLICY_REL):
            dst = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(_ROOT, rel), dst)
        return tmp

    def test_live_data_loads_and_a_matched_environment_gets_the_qualified_lines(self):
        routing = ee.load_routing_strict(_ROOT)
        self.assertEqual(set(routing["postures"]), {"qualified", "conservative-default"})
        self.assertEqual(ee.resolve_posture("matched", _ROOT), routing["postures"]["qualified"])
        for other in ("changed", "unqualified", "unknown"):
            self.assertEqual(ee.resolve_posture(other, _ROOT), routing["postures"]["conservative-default"])

    def test_absent_data_falls_back_to_the_constant_and_never_raises(self):
        tmp = self._scratch()
        try:
            os.remove(os.path.join(tmp, ee._ROUTING_REL))
            self.assertIsNone(ee.load_routing(tmp))
            self.assertEqual(ee.resolve_posture("matched", tmp), list(ee._CONSERVATIVE_DEFAULT))
            with self.assertRaises(ee.RoutingUnreadable):
                ee.load_routing_strict(tmp)
        finally:
            shutil.rmtree(tmp)

    def test_malformed_data_falls_back_and_the_strict_loader_names_the_miss(self):
        tmp = self._scratch()
        try:
            shutil.copyfile(os.path.join(_ROOT, self.FIXTURE), os.path.join(tmp, ee._ROUTING_REL))
            self.assertIsNone(ee.load_routing(tmp))
            self.assertEqual(ee.resolve_posture("matched", tmp), list(ee._CONSERVATIVE_DEFAULT))
            with self.assertRaises(ee.RoutingUnreadable) as ctx:
                ee.load_routing_strict(tmp)
        finally:
            shutil.rmtree(tmp)
        self.assertIn("qualified", str(ctx.exception))
        # An explicit path is the CHECK's seam, and only a caller that passes one reaches it.
        with self.assertRaises(ee.RoutingUnreadable):
            ee.load_routing_strict(_ROOT, self.FIXTURE)

    def test_the_boot_loader_reads_no_environment_seam(self):
        # The lines this loader feeds are relayed to a session at boot, so an environment variable must not be
        # able to swap the file (deliverable review, SG-2): with the check's override pointing at the malformed
        # fixture, boot still reads the committed data.
        with mock.patch.dict(os.environ, {"ENGINE_MODEL_ROUTING_PATH": self.FIXTURE}):
            self.assertEqual(set(ee.load_routing_strict(_ROOT)["postures"]), {"qualified", "conservative-default"})
        self.assertFalse(hasattr(ee, "ROUTING_ENV_OVERRIDE"))
        with open(ee.__file__, encoding="utf-8") as fh:
            self.assertNotIn("ENGINE_MODEL_ROUTING_PATH", fh.read())

    def test_unreadable_json_falls_back(self):
        tmp = self._scratch()
        try:
            with open(os.path.join(tmp, ee._ROUTING_REL), "w", encoding="utf-8") as fh:
                fh.write("{not json")
            self.assertIsNone(ee.load_routing(tmp))
            self.assertEqual(ee.resolve_posture("matched", tmp), list(ee._CONSERVATIVE_DEFAULT))
        finally:
            shutil.rmtree(tmp)

    def test_derive_stays_total_when_the_data_is_gone(self):
        tmp = self._scratch()
        try:
            os.remove(os.path.join(tmp, ee._ROUTING_REL))
            result = ee.derive(provider="claude", repo="o/r", root=tmp)
            self.assertEqual(result["lines"], list(ee._CONSERVATIVE_DEFAULT))
        finally:
            shutil.rmtree(tmp)

    def test_the_fence_parser_is_gone(self):
        self.assertFalse(hasattr(ee, "_policy_posture_block"))
        with open(ee.__file__, encoding="utf-8") as fh:
            self.assertNotIn("<!-- posture:", fh.read())

    def test_committed_page_region_is_current(self):
        expected, actual = ee.posture_projection_status(_ROOT)
        self.assertEqual(actual, expected, "run `execution_environment.py render-postures` and commit the page")

    def test_drift_is_caught_and_render_restores(self):
        tmp = self._scratch()
        try:
            page = os.path.join(tmp, ee._POLICY_REL)
            with open(page, encoding="utf-8") as fh:
                text = fh.read()
            with open(page, "w", encoding="utf-8") as fh:
                fh.write(text.replace("run your full, careful ceremony", "relax"))
            expected, actual = ee.posture_projection_status(tmp)
            self.assertNotEqual(actual, expected)
            self.assertTrue(ee.apply_posture_projection(tmp))
            expected, actual = ee.posture_projection_status(tmp)
            self.assertEqual(actual, expected)
            self.assertFalse(ee.apply_posture_projection(tmp))
        finally:
            shutil.rmtree(tmp)


class TestModelRoutingCheck(unittest.TestCase):
    def _run(self, env: dict | None = None) -> list:
        import model_routing_check as mrc
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(buf):
            rc = mrc.main([])
        self.assertEqual(rc, 0)
        return json.loads(buf.getvalue())

    def test_green_on_the_live_tree(self):
        self.assertEqual(self._run(), [])

    def test_bites_its_negative_fixture(self):
        import model_routing_check as mrc
        found = self._run({mrc.ENV_OVERRIDE: TestRoutingData.FIXTURE})
        self.assertTrue(any(f["severity"] == "hard" and "does not load as model-routing.v1" in f["message"]
                            for f in found), found)

    def test_a_revived_marker_is_a_finding(self):
        import model_routing_check as mrc
        tmp = TestRoutingData._scratch(TestRoutingData())
        try:
            stray = os.path.join(tmp, ".engine/policies/stray.md")
            with open(stray, "w", encoding="utf-8") as fh:
                fh.write("# stray\n\n<!-- posture:qualified -->\n```text\nx\n```\n")
            os.makedirs(os.path.join(tmp, ".engine/tools"), exist_ok=True)
            found = mrc.findings("hard", tmp)
            self.assertTrue(any("stray.md" in f["message"] and "retired" in f["message"] for f in found), found)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
