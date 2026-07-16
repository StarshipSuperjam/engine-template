#!/usr/bin/env python3
"""Unit tests for release_cut — the version-decision + manifest-write core.

Covered: sentinel-aware version ordering; first-cut vs diff classification (add / remove / new
migration => the mechanical floor); raise-only refusal; the atomic, shape-preserving write (only
version values change, home_repository byte-preserved); the packages<->manifest split-brain guard;
and rollback-on-validation-failure (nothing written)."""
import json
import os
import shutil
import tempfile
import unittest

import validate
import module_coherence
import release_cut as rc


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")


def _module(mid, ver="0.0.0-dev", migrations=None):
    m = {"id": mid, "version": ver, "status": "required", "provides": {}}
    if migrations:
        m["migrations"] = migrations
    return m


class _Tree:
    """A temp engine tree (engine.json + module manifests) with validate.ROOT pointed at it."""
    def __init__(self, modules, home="acme/engine-home", engine_release="0.0.0-dev"):
        self.root = tempfile.mkdtemp()
        engine = {"engine_release": engine_release,
                  "packages": {mid: m["version"] for mid, m in modules.items()},
                  "identity": "solo", "home_repository": home}
        _write(os.path.join(self.root, ".engine", "engine.json"), engine)
        for mid, m in modules.items():
            _write(os.path.join(self.root, ".engine", "modules", mid, "manifest.json"), m)

    def __enter__(self):
        self._saved = (validate.ROOT, validate.ENGINE_DIR)
        validate.ROOT = self.root
        validate.ENGINE_DIR = os.path.join(self.root, ".engine")
        return self

    def __exit__(self, *exc):
        validate.ROOT, validate.ENGINE_DIR = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def engine_text(self):
        with open(os.path.join(self.root, ".engine", "engine.json"), encoding="utf-8") as f:
            return f.read()

    def engine(self):
        return json.loads(self.engine_text())

    def module_version(self, mid):
        p = os.path.join(self.root, ".engine", "modules", mid, "manifest.json")
        return validate.load_json(p)["version"]


def _baseline_tree(modules):
    """A throwaway release-tree root carrying the given baseline module manifests."""
    root = tempfile.mkdtemp()
    for mid, m in modules.items():
        _write(os.path.join(root, ".engine", "modules", mid, "manifest.json"), m)
    return root


class VersionOrdering(unittest.TestCase):
    def test_sentinel_sorts_below_any_release(self):
        self.assertTrue(rc._strictly_greater("0.1.0", "0.0.0-dev"))
        self.assertTrue(rc._strictly_greater("1.0.0", "0.0.0-dev"))
        self.assertTrue(rc._strictly_greater("0.0.0", "0.0.0-dev"))  # a real release outranks the dev sentinel

    def test_equal_and_lower_refused(self):
        self.assertFalse(rc._strictly_greater("0.1.0", "0.1.0"))
        self.assertFalse(rc._strictly_greater("0.0.9", "0.1.0"))
        self.assertFalse(rc._strictly_greater("0.0.0-dev", "0.0.0-dev"))  # sentinel is not > itself

    def test_normal_increments(self):
        self.assertTrue(rc._strictly_greater("0.1.1", "0.1.0"))
        self.assertTrue(rc._strictly_greater("1.0.0", "0.9.9"))

    def test_prerelease_sorts_below_its_release(self):
        # a pre-release must NOT be taken as greater than its own release (else raise-only accepts a downgrade)
        self.assertFalse(rc._strictly_greater("1.0.0-rc1", "1.0.0"))
        self.assertTrue(rc._strictly_greater("1.0.0", "1.0.0-rc1"))
        self.assertTrue(rc._strictly_greater("1.0.0-rc1", "0.9.0"))       # higher numbers still win
        self.assertFalse(rc._strictly_greater("1.0.0-rc2", "1.0.0-rc1"))  # conservative: rc progression refused

    def test_valid_version_grammar(self):
        self.assertTrue(rc._valid_version("1.2.0"))
        self.assertTrue(rc._valid_version("1.0.0-rc1"))
        self.assertTrue(rc._valid_version("0.0.0-dev"))
        self.assertFalse(rc._valid_version("99999.total-garbage;rm -rf ~"))
        self.assertFalse(rc._valid_version("v1.2.0"))
        self.assertFalse(rc._valid_version(""))
        # #402 U07a: the writer grammar is now strict MAJOR.MINOR.PATCH, matching the module.v1 schema gate
        # (so the two cannot bless different shapes). A 1-, 2-, or 4-component number is rejected here too.
        self.assertFalse(rc._valid_version("1.2"))
        self.assertFalse(rc._valid_version("1"))
        self.assertFalse(rc._valid_version("1.2.3.4"))


class Classify(unittest.TestCase):
    def test_first_cut_derives_no_floor(self):
        with _Tree({"core": _module("core"), "qa-review": _module("qa-review")}):
            p = rc.classify(rc.Baseline(None, True, "first cut"), None)
        self.assertEqual(p["mode"], "first-cut")
        self.assertEqual(p["engine_floor_level"], "none")
        self.assertIn("First release", p["change_inventory"][0])

    def test_diff_add_remove_migration_floor(self):
        # live tree: core (with a new migration) + product-design (new); baseline: core (no migration) + legacy
        live = {"core": _module("core", migrations={"0.2.0": {"description": "d", "run": "r", "kind": "config"}}),
                "product-design": _module("product-design")}
        base = _baseline_tree({"core": _module("core"), "legacy": _module("legacy")})
        try:
            with _Tree(live):
                p = rc.classify(rc.Baseline("v0.0.9", False, "diff"), base)
            inv = " ".join(p["change_inventory"])
            self.assertEqual(p["mode"], "diff")
            self.assertEqual(p["engine_floor_level"], "major")           # a removal => major
            self.assertIn("Added the 'product-design'", inv)
            self.assertIn("Removed the 'legacy'", inv)
            self.assertIn("core", p["package_floor"])                    # new migration => package floor
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_diff_no_signal_notes_patch_and_contract_silent_caveat(self):
        mods = {"core": _module("core")}
        base = _baseline_tree({"core": _module("core")})
        try:
            with _Tree(mods):
                p = rc.classify(rc.Baseline("v0.0.9", False, "diff"), base)
            self.assertEqual(p["engine_floor_level"], "none")
            self.assertIn("no structural signal", " ".join(p["change_inventory"]))
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_diff_sets_concrete_engine_floor_version(self):
        base = _baseline_tree({"core": _module("core"), "legacy": _module("legacy")})
        try:
            with _Tree({"core": _module("core")}, engine_release="1.0.0"):
                p = rc.classify(rc.Baseline("v0.9.0", False, "diff"), base)
            self.assertEqual(p["engine_floor_level"], "major")       # a removal forces a major
            self.assertEqual(p["engine_floor_version"], "2.0.0")     # concrete major floor from current 1.0.0
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_first_cut_has_no_engine_floor_version(self):
        with _Tree({"core": _module("core")}):
            p = rc.classify(rc.Baseline(None, True, "first cut"), None)
        self.assertIsNone(p["engine_floor_version"])


class Apply(unittest.TestCase):
    def test_raise_only_refuses_engine_non_increase(self):
        # the ENGINE version must strictly increase — an equal engine version is refused (a cut always moves it)
        with _Tree({"core": _module("core")}):
            r = rc.apply("0.0.0-dev", "0.0.0-dev", {}, None, dry_run=True)
        self.assertFalse(r["applied"])
        self.assertEqual(r["reason"], "raise-only")

    def test_engine_only_cut_holds_unchanged_capabilities(self):
        # the reported-failure shape: the engine version moves but no capability changed. Unchanged
        # capabilities keep their version (not refused as "not strictly greater"); only engine.json is written.
        with _Tree({"core": _module("core", ver="0.1.0"), "qa-review": _module("qa-review", ver="0.1.0")},
                   engine_release="0.1.0") as t:
            proposal = {"engine_floor_version": "0.2.0", "package_floor": {}}
            r = rc.apply("0.2.0", None, {}, proposal, dry_run=False)
            self.assertTrue(r["applied"])
            self.assertEqual(r["targets"], {})                          # nothing written but the engine
            self.assertEqual(t.engine()["engine_release"], "0.2.0")
            self.assertEqual(t.engine()["packages"]["core"], "0.1.0")   # held
            self.assertEqual(t.module_version("core"), "0.1.0")
            self.assertEqual(t.module_version("qa-review"), "0.1.0")

    def test_diff_cut_auto_raises_floored_capability_holds_others(self):
        # a migration-bearing cut on the derive path (no --package): the floored capability is auto-raised to
        # its floor, the rest hold. This is the case that would otherwise refuse on below-confirmed-floor.
        with _Tree({"core": _module("core", ver="0.1.0"), "qa-review": _module("qa-review", ver="0.1.0")},
                   engine_release="0.1.0") as t:
            proposal = {"engine_floor_version": "0.2.0", "package_floor": {"core": "0.2.0"}}
            r = rc.apply("0.2.0", None, {}, proposal, dry_run=False)
            self.assertTrue(r["applied"])
            self.assertEqual(r["targets"], {"core": "0.2.0"})           # only core written
            self.assertEqual(t.module_version("core"), "0.2.0")         # raised to its floor
            self.assertEqual(t.module_version("qa-review"), "0.1.0")    # held
            self.assertEqual(t.engine()["packages"]["core"], "0.2.0")

    def test_explicit_package_below_current_still_refused(self):
        # loosening the write set to allow a no-op keep must NOT allow a genuine lowering: an explicit
        # --package below the current version is still refused by raise-only.
        with _Tree({"core": _module("core", ver="0.1.0")}, engine_release="0.1.0"):
            r = rc.apply("0.2.0", None, {"core": "0.0.9"}, None, dry_run=True)
        self.assertFalse(r["applied"])
        self.assertEqual(r["reason"], "raise-only")

    def test_refusal_prints_reason_to_stderr(self):
        # Fix: a refusal must say WHY on stderr (the workflow redirects the --json stdout into a file, so a
        # bare non-zero exit would otherwise be reasonless).
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc._print_refusal({"reason": "raise-only", "violations": ["engine version 0.0.9 is not higher"],
                               "recovery": "choose a higher version."})
        err = buf.getvalue()
        self.assertIn("raise-only", err)
        self.assertIn("not higher", err)
        self.assertIn("choose a higher version", err)

    def test_cmd_apply_json_refusal_exits_one_and_writes_reason_to_stderr(self):
        # the actual "bare exit code 1" fix: `apply --json` on a refusal must exit 1, print the JSON to stdout
        # (captured by the workflow into applied.json) AND the plain reason to stderr (shown in the run log).
        import io
        from contextlib import redirect_stderr, redirect_stdout
        with _Tree({"core": _module("core", ver="0.1.0")}, engine_release="0.1.0"):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = rc.main(["apply", "--engine", "0.0.9", "--all", "0.0.9", "--json"])  # a lowering
        self.assertEqual(code, 1)
        self.assertIn('"reason": "raise-only"', out.getvalue())   # machine-readable JSON on stdout
        self.assertIn("Refused (raise-only)", err.getvalue())     # plain reason on stderr
        self.assertIn("To fix:", err.getvalue())

    def test_apply_writes_versions_and_preserves_home_repository(self):
        with _Tree({"core": _module("core"), "qa-review": _module("qa-review")}) as t:
            before_home_line = [ln for ln in t.engine_text().splitlines() if "home_repository" in ln][0]
            r = rc.apply("0.1.0", "0.1.0", {}, None, dry_run=False)
            self.assertTrue(r["applied"])
            self.assertEqual(t.engine()["engine_release"], "0.1.0")
            self.assertEqual(t.engine()["packages"]["core"], "0.1.0")
            self.assertEqual(t.module_version("core"), "0.1.0")
            self.assertEqual(t.module_version("qa-review"), "0.1.0")
            # home_repository line is byte-identical (would otherwise trip weakening_guard, D-281/D-282)
            after_home_line = [ln for ln in t.engine_text().splitlines() if "home_repository" in ln][0]
            self.assertEqual(before_home_line, after_home_line)
            self.assertEqual(t.engine()["identity"], "solo")           # unrelated keys preserved

    def test_apply_dry_run_writes_nothing(self):
        with _Tree({"core": _module("core")}) as t:
            rc.apply("0.1.0", "0.1.0", {}, None, dry_run=True)
            self.assertEqual(t.engine()["engine_release"], "0.0.0-dev")
            self.assertEqual(t.module_version("core"), "0.0.0-dev")

    def test_below_confirmed_floor_refused(self):
        # target 0.1.5 is ABOVE the current 0.1.0 (so raise-only passes) but BELOW the confirmed floor
        # 0.2.0 — this must be caught by the below-floor guard specifically, not raise-only.
        with _Tree({"core": _module("core", ver="0.1.0")}):
            proposal = {"package_floor": {"core": "0.2.0"}}
            r = rc.apply("0.2.0", None, {"core": "0.1.5"}, proposal, dry_run=True)
        self.assertFalse(r["applied"])
        self.assertEqual(r["reason"], "below-confirmed-floor")

    def test_at_or_above_confirmed_floor_passes(self):
        with _Tree({"core": _module("core", ver="0.1.0")}):
            proposal = {"package_floor": {"core": "0.2.0"}}
            r = rc.apply("0.2.0", None, {"core": "0.2.0"}, proposal, dry_run=True)   # meets the floor
        self.assertEqual(r["reason"], "dry-run")   # would apply

    def test_engine_below_mechanical_floor_refused(self):
        # a removed capability forces a major floor (2.0.0); dispatching 1.0.1 is ABOVE current (1.0.0), so
        # raise-only passes — the ENGINE-floor gate must still refuse it as below the mechanical floor.
        with _Tree({"core": _module("core", ver="1.0.0")}, engine_release="1.0.0"):
            proposal = {"engine_floor_version": "2.0.0", "package_floor": {}}
            r = rc.apply("1.0.1", "1.0.1", {}, proposal, dry_run=True)
        self.assertFalse(r["applied"])
        self.assertEqual(r["reason"], "below-confirmed-floor")
        self.assertTrue(any("mechanical floor 2.0.0" in v for v in r["violations"]))

    def test_engine_at_mechanical_floor_passes(self):
        with _Tree({"core": _module("core", ver="1.0.0")}, engine_release="1.0.0"):
            proposal = {"engine_floor_version": "2.0.0", "package_floor": {}}
            r = rc.apply("2.0.0", "2.0.0", {}, proposal, dry_run=True)   # meets the mechanical floor
        self.assertEqual(r["reason"], "dry-run")   # would apply

    def test_no_engine_floor_when_proposal_omits_it(self):
        # a None/absent engine_floor_version imposes no engine floor (raise-only still applies)
        with _Tree({"core": _module("core", ver="1.0.0")}, engine_release="1.0.0"):
            proposal = {"engine_floor_version": None, "package_floor": {}}
            r = rc.apply("1.0.1", "1.0.1", {}, proposal, dry_run=True)
        self.assertEqual(r["reason"], "dry-run")   # would apply — no floor to breach

    def test_invalid_version_refused(self):
        with _Tree({"core": _module("core")}):
            r = rc.apply("99999.total-garbage;rm -rf ~", "99999.total-garbage;rm -rf ~", {}, None, dry_run=True)
        self.assertFalse(r["applied"])
        self.assertEqual(r["reason"], "invalid-version")

    def test_pre_write_validation_failure_writes_nothing(self):
        # a validation error fires BEFORE any file is staged — the pre-write refusal path
        with _Tree({"core": _module("core")}) as t:
            orig = rc._schema_ok
            rc._schema_ok = lambda inst, path: ["forced error"]
            try:
                r = rc.apply("0.1.0", "0.1.0", {}, None, dry_run=False)
            finally:
                rc._schema_ok = orig
            self.assertFalse(r["applied"])
            self.assertEqual(r["reason"], "validation")
            self.assertEqual(t.engine()["engine_release"], "0.0.0-dev")   # untouched
            self.assertEqual(t.module_version("core"), "0.0.0-dev")

    def test_swap_failure_rolls_back_all_files(self):
        # a write error mid-swap must roll back the files already swapped — no split-brain left on disk
        with _Tree({"core": _module("core"), "qa-review": _module("qa-review")}) as t:
            real_replace = rc.os.replace
            calls = {"n": 0}

            def flaky(src, dst):
                calls["n"] += 1
                if calls["n"] == 2:            # engine.json swaps (1), the first manifest swap (2) fails
                    raise OSError("disk full")
                return real_replace(src, dst)

            rc.os.replace = flaky
            try:
                with self.assertRaises(RuntimeError):
                    rc.apply("0.1.0", "0.1.0", {}, None, dry_run=False)
            finally:
                rc.os.replace = real_replace
            # everything is back at the sentinel — engine.json was restored, no manifest half-written
            self.assertEqual(t.engine()["engine_release"], "0.0.0-dev")
            self.assertEqual(t.engine()["packages"]["core"], "0.0.0-dev")
            self.assertEqual(t.module_version("core"), "0.0.0-dev")
            self.assertEqual(t.module_version("qa-review"), "0.0.0-dev")


class RenderPRBody(unittest.TestCase):
    def test_first_cut_body_has_inventory_versions_subbar_and_guidance(self):
        with _Tree({"core": _module("core"), "qa-review": _module("qa-review")}):
            proposal = rc.classify(rc.Baseline(None, True, "no prior release"), None)
            applied = rc.apply("0.1.0", "0.1.0", {}, None, dry_run=False)
        body = rc.render_pr_body(proposal, applied)
        self.assertIn("no earlier version → 0.1.0", body)           # the version move (sentinel hidden)
        self.assertNotIn("0.0.0-dev", body)                         # the internal sentinel never leaks
        self.assertIn("First release", body)                        # the change inventory carried through
        self.assertIn("Every capability (2)", body)                 # uniform targets collapse to one line
        self.assertIn("no automated check", body.lower())           # the gate-path line (no benchmark built)
        self.assertIn("## Review", body)                            # the §3 confirm/raise/reject guidance
        self.assertIn("close this and run the release again", body)  # the raise + missing-signal backstop
        # maintainer-facing register (§8): no internal machinery vocabulary leaks
        for banned in ("release-cut", "bump rule", "version production", "first-cut", "engine_floor"):
            self.assertNotIn(banned, body)

    def test_body_carries_all_eight_required_sections_filled(self):
        # The release pull request must clear the same `pr-body-completeness` gate every engine pull request
        # meets (a RELEASE_PAT-opened PR is not author-exempt) — otherwise the release PR is un-mergeable.
        # Assert against the REAL check logic (validate.section_presence_findings), never a reimplementation,
        # so the test tracks the gate it protects.
        with _Tree({"core": _module("core"), "qa-review": _module("qa-review")}):
            proposal = rc.classify(rc.Baseline(None, True, "no prior release"), None)
            applied = rc.apply("0.1.0", "0.1.0", {}, None, dry_run=False)
        body = rc.render_pr_body(proposal, applied)
        required = ["Purpose", "Scope", "Out of scope", "Risk", "Validation",
                    "Review", "Files of interest", "Claude involvement"]
        findings = validate.section_presence_findings(body, required, "hard", "", "pull-request body")
        self.assertEqual(findings, [], f"release body missing/empty required sections: {findings}")

    def test_body_follows_the_pr_template_form_not_just_headers(self):
        # The completeness gate only checks the eight HEADERS are present; a header-only body passes it but is
        # not a template-conforming body. Every section must carry the repo template's shape — a bold one-line
        # summary AND an *Impact:* line — so the release body reads like every other engine pull request's.
        proposal = {"change_inventory": ["First release."], "impacts": []}
        applied = {"applied": True, "engine": "0.1.0", "from_engine": "0.0.0-dev", "targets": {"core": "0.1.0"}}
        body = rc.render_pr_body(proposal, applied)
        sections = ["Purpose", "Scope", "Out of scope", "Risk", "Validation",
                    "Review", "Files of interest", "Claude involvement"]
        for i, name in enumerate(sections):
            seg = body.split(f"## {name}", 1)[1]
            if i + 1 < len(sections):
                seg = seg.split(f"## {sections[i + 1]}", 1)[0]
            lines = [ln for ln in seg.splitlines() if ln.strip()]
            self.assertTrue(lines and lines[0].startswith("**"), f"{name}: no bold summary line")
            self.assertTrue(any(ln.startswith("*Impact:") for ln in lines), f"{name}: no *Impact:* line")

    def test_major_removal_surfaces_breaking_change_under_risk(self):
        # a removed capability is a breaking change (engine_floor_level 'major') — the Risk section, not only
        # the neutral Scope inventory, must say so, so a reviewer scanning "Risk" is not under-warned.
        proposal = {"change_inventory": ["Removed the 'legacy' capability."], "impacts": [],
                    "engine_floor_level": "major", "engine_floor_version": "1.0.0"}
        applied = {"applied": True, "engine": "1.0.0", "from_engine": "0.3.0", "targets": {"core": "1.0.0"}}
        body = rc.render_pr_body(proposal, applied)
        risk = body.split("## Risk", 1)[1].split("## Validation", 1)[0]
        self.assertIn("breaking change", risk.lower())   # the warning is WITHIN the Risk section
        # a non-breaking release must NOT cry wolf
        minor = rc.render_pr_body({"change_inventory": ["Added the 'x' capability."], "impacts": [],
                                   "engine_floor_level": "minor"},
                                  {"applied": True, "engine": "0.4.0", "from_engine": "0.3.0",
                                   "targets": {"core": "0.4.0"}})
        self.assertNotIn("breaking change", minor.lower())

    def test_major_release_with_impacts_separates_interface_list_from_breaking_bullet(self):
        # The highest-stakes case: a breaking release that ALSO touches interface contracts. The "Interface
        # changes to read before you merge:" intro must sit on its own line with a blank line before it —
        # otherwise markdown treats it as a lazy continuation of the breaking-change bullet above and fuses
        # them, hiding the interface-changes signpost on exactly the release that most needs reading.
        proposal = {"change_inventory": ["Removed the 'legacy' capability."],
                    "impacts": [{"what": "the contract surface 'c' changed", "why": "read it against consumers"}],
                    "engine_floor_level": "major", "engine_floor_version": "1.0.0"}
        applied = {"applied": True, "engine": "1.0.0", "from_engine": "0.3.0", "targets": {"core": "1.0.0"}}
        lines = rc.render_pr_body(proposal, applied).splitlines()
        idx = lines.index("Interface changes to read before you merge:")
        self.assertEqual(lines[idx - 1], "")                                  # a blank line precedes the intro
        self.assertTrue(any("breaking change" in ln.lower() for ln in lines[:idx]))  # the breaking bullet is above

    def test_scope_what_changed_includes_contract_impacts(self):
        # a contract-only release has an EMPTY structural change_inventory (no module add/remove/migration),
        # so the "what changed" list would read blank without the impacts — yet a changed contract is exactly
        # what forced the bump. The Scope collation must surface the contract change so the version is justified.
        proposal = {"change_inventory": [],
                    "impacts": [{"what": "the contract surface 'eADR-0014-one-history.md' changed",
                                 "why": "read it against consumers", "floor_level": "minor"}],
                    "engine_floor_level": "minor", "engine_floor_version": "0.2.0"}
        applied = {"applied": True, "engine": "0.2.0", "from_engine": "0.1.0", "targets": {}}
        body = rc.render_pr_body(proposal, applied)
        scope = body.split("## Scope", 1)[1].split("## Out of scope", 1)[0]
        self.assertIn("What changed since the last release:", scope)
        self.assertIn("eADR-0014-one-history.md", scope)             # the contract change is in "what changed"
        # the Risk section renders the interface change with the SAME polish as the published Release notes —
        # a bold heading + its description as a sentence — so the consent surface is not rougher (usability).
        risk = body.split("## Risk", 1)[1].split("## Validation", 1)[0]
        self.assertIn("**The contract surface 'eADR-0014-one-history.md' changed.** Read it against consumers",
                      risk)

    def test_scope_pr_list_leads_and_migration_surfaces_beside_it(self):
        proposal = {"change_inventory": ["'memory-store' gained a data/config migration (0.2.0)."],
                    "impacts": [], "engine_floor_level": "minor", "engine_floor_version": "0.2.0",
                    "merged_prs": ["Refactor storage (#41)", "Tidy CLI (#42)"]}
        applied = {"applied": True, "engine": "0.2.0", "from_engine": "0.1.0", "targets": {"memory-store": "0.2.0"}}
        scope = rc.render_pr_body(proposal, applied).split("## Scope", 1)[1].split("## Out of scope", 1)[0]
        self.assertIn("What changed since the last release (2 pull requests):", scope)
        self.assertIn("- Refactor storage (#41)", scope)
        self.assertIn("Capability and data changes:", scope)
        self.assertIn("gained a data/config migration", scope)          # the migration is not lost

    def test_scope_long_pr_list_is_foldable_but_open_by_default(self):
        proposal = {"change_inventory": [], "impacts": [], "engine_floor_level": "minor",
                    "merged_prs": [f"PR number {i} (#{i})" for i in range(20)]}
        applied = {"applied": True, "engine": "0.2.0", "from_engine": "0.1.0", "targets": {}}
        scope = rc.render_pr_body(proposal, applied).split("## Scope", 1)[1].split("## Out of scope", 1)[0]
        self.assertIn("<details open>", scope)          # foldable, but OPEN by default so the work shows on load
        self.assertIn("(20 pull requests)", scope)

    def test_first_cut_pr_body_heading_does_not_say_since_the_last_release(self):
        # the first-cut PR body must not contradict itself (no "since the last release" for a first release)
        with _Tree({"core": _module("core"), "qa-review": _module("qa-review")}):
            proposal = rc.classify(rc.Baseline(None, True, "no prior release"), None)
            applied = rc.apply("0.1.0", "0.1.0", {}, None, dry_run=False)
        scope = rc.render_pr_body(proposal, applied).split("## Scope", 1)[1].split("## Out of scope", 1)[0]
        self.assertIn("What this release establishes:", scope)
        self.assertNotIn("since the last release", scope)

    def test_gate_path_three_states_are_visibly_distinct(self):
        passed, subbar, errored = (rc._gate_path_line("passed"), rc._gate_path_line("sub-bar"),
                                   rc._gate_path_line("errored"))
        self.assertEqual(len({passed, subbar, errored}), 3)         # §6: never look alike
        self.assertIn("passed", passed.lower())
        self.assertIn("errored", errored.lower())
        self.assertIn("no automated check", subbar.lower())
        for s in (passed, subbar, errored):
            self.assertTrue(s.strip())

    def test_diff_body_lists_impacts_and_itemises_varied_versions(self):
        proposal = {"change_inventory": ["Added the 'x' capability."],
                    "impacts": [{"what": "the contract surface 'c' changed", "why": "read it against consumers"}]}
        applied = {"applied": True, "engine": "0.2.0", "from_engine": "0.1.0",
                   "targets": {"core": "0.2.0", "qa-review": "0.1.5"}}
        body = rc.render_pr_body(proposal, applied)
        self.assertIn("Interface changes", body)                    # impacts surfaced
        self.assertIn("qa-review: → 0.1.5", body)                   # itemised (not collapsed — versions differ)
        self.assertNotIn("Every capability", body)

    def test_body_shows_mechanical_floor_when_present(self):
        proposal = {"change_inventory": ["Removed the 'legacy' capability."], "impacts": [],
                    "engine_floor_version": "2.0.0"}
        applied = {"applied": True, "engine": "2.0.0", "from_engine": "1.0.0", "targets": {"core": "2.0.0"}}
        body = rc.render_pr_body(proposal, applied)
        self.assertIn("least this release could be is **2.0.0**", body)

    def test_body_refuses_a_none_release(self):
        # a refused apply result carries no engine version — it must NOT render a "None → None" release
        with self.assertRaises(RuntimeError):
            rc.render_pr_body({"change_inventory": []}, {"applied": False, "reason": "raise-only"})

    def test_first_cut_body_hides_the_dev_sentinel(self):
        proposal = {"change_inventory": ["First release."], "impacts": []}
        applied = {"applied": True, "engine": "0.1.0", "from_engine": "0.0.0-dev", "targets": {"core": "0.1.0"}}
        body = rc.render_pr_body(proposal, applied)
        self.assertIn("no earlier version → 0.1.0", body)
        self.assertNotIn("0.0.0-dev", body)

    def test_pr_body_subcommand_reads_files_and_prints(self):
        # the CLI seam the workflow drives: proposal + applied files in, body on stdout
        d = tempfile.mkdtemp()
        try:
            _write(os.path.join(d, "proposal.json"),
                   {"change_inventory": ["First release."], "impacts": []})
            _write(os.path.join(d, "applied.json"),
                   {"applied": True, "engine": "0.1.0", "from_engine": "0.0.0-dev", "targets": {"core": "0.1.0"}})
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = rc.main(["pr-body", "--proposal", os.path.join(d, "proposal.json"),
                                "--applied", os.path.join(d, "applied.json")])
            self.assertEqual(code, 0)
            self.assertIn("no earlier version → 0.1.0", buf.getvalue())
        finally:
            shutil.rmtree(d, ignore_errors=True)


class ChangeSummary(unittest.TestCase):
    def test_merges_inventory_and_contract_impacts(self):
        proposal = {"change_inventory": ["Added the 'x' capability."],
                    "impacts": [{"what": "the contract surface 'c.md' changed", "why": "..."}]}
        lines = rc.change_summary(proposal)
        self.assertIn("Added the 'x' capability.", lines)
        self.assertIn("The contract surface 'c.md' changed.", lines)   # capitalized, period added

    def test_empty_when_nothing_changed(self):
        self.assertEqual(rc.change_summary({"change_inventory": [], "impacts": []}), [])

    def test_contract_only_release_is_not_empty(self):
        # the case that made the PR body's "what changed" read blank: no structural inventory, only impacts
        lines = rc.change_summary({"change_inventory": [],
                                   "impacts": [{"what": "the contract surface 'c.md' changed", "why": "..."}]})
        self.assertEqual(len(lines), 1)


class ReleaseNotes(unittest.TestCase):
    def test_human_readable_with_sections_bullets_and_descriptions(self):
        proposal = {"engine_floor_level": "major",
                    "change_inventory": ["Added the 'x' capability.", "Removed the 'legacy' capability."],
                    "impacts": [{"what": "the contract surface 'c.md' changed",
                                 "why": "read it against consumers before confirming."}]}
        notes = rc.render_release_notes("v1.0.0", proposal)
        self.assertIn("Engine version v1.0.0.", notes)
        self.assertIn("no automated check", notes.lower())               # readiness line
        self.assertIn("breaking change", notes.lower())                  # major callout
        self.assertIn("## What changed since the last release", notes)   # section
        self.assertIn("- Added the 'x' capability.", notes)              # bullets
        self.assertIn("## Interface changes to read", notes)             # distinct section
        self.assertIn("**The contract surface 'c.md' changed.** Read it against consumers", notes)  # bold + desc

    def test_minor_release_has_no_breaking_callout(self):
        notes = rc.render_release_notes("v0.2.0", {"engine_floor_level": "minor",
                                                   "change_inventory": ["Added the 'x' capability."], "impacts": []})
        self.assertNotIn("breaking change", notes.lower())
        self.assertNotIn("## Interface changes to read", notes)          # no impacts -> no section

    def test_no_proposal_degrades_to_version_and_readiness_only(self):
        notes = rc.render_release_notes("v0.2.0", None)
        self.assertIn("Engine version v0.2.0.", notes)
        self.assertIn("no automated check", notes.lower())
        self.assertNotIn("## What changed", notes)
        self.assertNotIn("## Interface changes", notes)

    def test_no_internal_vocabulary_leaks(self):
        notes = rc.render_release_notes("v1.0.0", {"engine_floor_level": "major",
                                                   "change_inventory": ["Removed the 'legacy' capability."],
                                                   "impacts": []})
        for banned in ("release-cut", "release_cut", "terminal cut", "engine_floor", "first-cut"):
            self.assertNotIn(banned, notes)

    def test_merged_prs_lead_what_changed_and_are_counted(self):
        # when the merged-PR list is present it IS the "what changed" section (the actual work), with a count
        proposal = {"engine_floor_level": "minor", "change_inventory": [], "impacts": [],
                    "merged_prs": ["Fix the thing (#12)", "Add the other thing (#13)"]}
        notes = rc.render_release_notes("v0.2.0", proposal)
        self.assertIn("## What changed since the last release (2 pull requests)", notes)
        self.assertIn("- Fix the thing (#12)", notes)
        self.assertIn("- Add the other thing (#13)", notes)

    def test_single_merged_pr_uses_singular(self):
        notes = rc.render_release_notes("v0.2.0", {"change_inventory": [], "impacts": [],
                                                   "merged_prs": ["Only change (#9)"]})
        self.assertIn("(1 pull request)", notes)

    def test_falls_back_to_structural_signals_when_no_pr_list(self):
        # best-effort failure / first cut => no merged_prs => the structural inventory is still shown
        notes = rc.render_release_notes("v0.2.0", {"change_inventory": ["Added the 'x' capability."],
                                                   "impacts": [], "merged_prs": []})
        self.assertIn("## What changed since the last release", notes)
        self.assertIn("- Added the 'x' capability.", notes)
        self.assertNotIn("pull request", notes)

    def test_migration_signal_surfaces_beside_the_pr_list(self):
        # the consent-critical case: with a PR list present, a data migration must STILL be named (it has no
        # other callout), not be replaced by flat PR titles.
        proposal = {"engine_floor_level": "minor", "impacts": [],
                    "change_inventory": ["'memory-store' gained a data/config migration (0.2.0)."],
                    "merged_prs": ["Refactor storage layout (#41)", "Tidy CLI (#42)"]}
        notes = rc.render_release_notes("v0.2.0", proposal)
        self.assertIn("## What changed since the last release (2 pull requests)", notes)   # the work
        self.assertIn("## Capability and data changes", notes)                             # + the signals
        self.assertIn("gained a data/config migration", notes)                             # the migration is NAMED

    def test_no_signal_caveat_is_not_shown_beside_the_pr_list(self):
        # when nothing structural fired, the "No module added…" caveat is not a per-item signal — don't show it
        # next to the PR list.
        proposal = {"engine_floor_level": "none", "impacts": [],
                    "change_inventory": [rc._NO_STRUCTURAL_SIGNAL_NOTE],
                    "merged_prs": ["Tidy CLI (#42)"]}
        notes = rc.render_release_notes("v0.2.0", proposal)
        self.assertNotIn("## Capability and data changes", notes)
        self.assertNotIn("No module added or removed", notes)


class MergedPrList(unittest.TestCase):
    _BODY = ("## What's Changed\n"
             "* Render the body in template form by @alice in https://github.com/o/r/pull/388\n"
             "* Bump setup-uv from 8.2.0 to 8.3.0 by @dependabot[bot] in https://github.com/o/r/pull/389\n"
             "\n**Full Changelog**: https://github.com/o/r/compare/v0.1.0...v0.2.0\n")

    def test_parses_titles_and_pr_numbers(self):
        self.assertEqual(rc._parse_pr_lines(self._BODY),
                         ["Render the body in template form (#388)", "Bump setup-uv from 8.2.0 to 8.3.0 (#389)"])

    def test_ignores_non_pr_lines(self):
        self.assertEqual(rc._parse_pr_lines("## What's Changed\n**Full Changelog**: x\nrandom\n"), [])

    def test_excludes_the_engine_own_release_pr(self):
        # a release must not list itself: the "Release X.Y.Z" PR (in range at publish) is dropped
        body = ("## What's Changed\n"
                "* Fix a thing by @a in https://github.com/o/r/pull/50\n"
                "* Release 0.2.0 by @engine in https://github.com/o/r/pull/60\n")
        self.assertEqual(rc._parse_pr_lines(body), ["Fix a thing (#50)"])

    def test_defuses_closing_keywords_in_titles(self):
        # a title's "Closes #N" must not auto-close the issue from the release PR body — keyword dropped, ref kept
        self.assertEqual(rc._defuse_closing_keywords("Raise the ceiling (Closes #460)"),
                         "Raise the ceiling (#460)")
        self.assertEqual(rc._defuse_closing_keywords("Wire the tier (U11, closes #408)"),
                         "Wire the tier (U11, #408)")
        self.assertEqual(rc._defuse_closing_keywords("Fix the parser fixes #12"), "Fix the parser #12")
        self.assertEqual(rc._defuse_closing_keywords("Resolves #5 and resolved #6"), "#5 and #6")

    def test_defuses_the_cross_repo_close_form(self):
        # GitHub also auto-closes a cross-repo "Closes owner/repo#N" — strip the keyword, keep the reference
        self.assertEqual(rc._defuse_closing_keywords("Port the fix (Fixes octo-org/octo-repo#100)"),
                         "Port the fix (octo-org/octo-repo#100)")

    def test_defuse_leaves_a_non_closing_reference_untouched(self):
        # "fail closed (#390)" is NOT a close (the '(' breaks the keyword->#N bond), mirroring GitHub — leave it
        self.assertEqual(rc._defuse_closing_keywords("doesn't fail closed (#390)"), "doesn't fail closed (#390)")
        self.assertEqual(rc._defuse_closing_keywords("Part of #405"), "Part of #405")
        self.assertEqual(rc._defuse_closing_keywords("Fixed several bugs, see #5"), "Fixed several bugs, see #5")

    def test_parse_pr_lines_defuses_closing_keywords(self):
        body = ("## What's Changed\n"
                "* Raise boot ceiling to 150 (Closes #460) by @a in https://github.com/o/r/pull/482\n")
        self.assertEqual(rc._parse_pr_lines(body), ["Raise boot ceiling to 150 (#460) (#482)"])

    def test_rendered_pr_body_carries_no_active_closing_keyword(self):
        # end-to-end on the REAL path: titles enter only via _parse_pr_lines (which defuses), then feed
        # render_pr_body (the auto-closing consent surface). The rendered body must carry no active closing
        # keyword bound to a reference — across close/fix/resolve — while keeping the references.
        gh_body = ("## What's Changed\n"
                   "* Do a thing (Closes #10) by @a in https://github.com/o/r/pull/20\n"
                   "* Fix it (fixes #11) by @b in https://github.com/o/r/pull/21\n"
                   "* Sort it (Resolves owner/repo#12) by @c in https://github.com/o/r/pull/22\n")
        proposal = {"engine_floor_level": "minor", "impacts": [], "change_inventory": [],
                    "merged_prs": rc._parse_pr_lines(gh_body)}
        applied = {"applied": True, "engine": "0.2.0", "from_engine": "0.1.0", "targets": {}}
        body = rc.render_pr_body(proposal, applied)
        self.assertNotRegex(body, r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]+(?:[\w.-]+/[\w.-]+)?#\d+")
        for ref in ("#10", "#11", "#12"):                # the references themselves are kept
            self.assertIn(ref, body)

    def test_merged_pr_titles_uses_injected_fetch_offline(self):
        prs = rc.merged_pr_titles("v0.1.0", "deadbeef", repo="o/r", _fetch=lambda *a, **k: self._BODY)
        self.assertEqual(prs, ["Render the body in template form (#388)", "Bump setup-uv from 8.2.0 to 8.3.0 (#389)"])

    def test_best_effort_returns_empty_on_failure(self):
        def boom(*a, **k):
            raise RuntimeError("network down")
        self.assertEqual(rc.merged_pr_titles("v0.1.0", "sha", repo="o/r", _fetch=boom), [])

    def test_empty_without_previous_tag_or_target(self):
        # a first release (no previous tag) or a missing target -> no PR list, no network attempt
        self.assertEqual(rc.merged_pr_titles(None, "sha", repo="o/r", _fetch=lambda *a, **k: self._BODY), [])
        self.assertEqual(rc.merged_pr_titles("v0.1.0", None, repo="o/r", _fetch=lambda *a, **k: self._BODY), [])


class NestedContractDetection(unittest.TestCase):
    def test_dir_bytes_recurses_into_subdirectories(self):
        d = tempfile.mkdtemp()
        try:
            _write(os.path.join(d, "eADR-0001-top.md"), {"x": 1})              # a top-level file
            _write(os.path.join(d, "sub", "nested.md"), {"y": 2})              # + a nested file
            keys = set(rc._dir_bytes(d).keys())
            self.assertIn("eADR-0001-top.md", keys)
            self.assertIn(os.path.join("sub", "nested.md"), keys)              # the subtree is no longer skipped
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_impact_statement_fires_for_a_nested_contract_change(self):
        # a contract surface added in a subdirectory must produce an impact (the non-recursive read missed it)
        base = tempfile.mkdtemp()
        os.makedirs(os.path.join(base, ".engine", "contracts", "instance"), exist_ok=True)
        with _Tree({"core": _module("core", ver="0.1.0")}, engine_release="0.1.0"):
            # add a nested contract file only in the LIVE tree
            live_nested = os.path.join(validate.ROOT, ".engine", "contracts", "instance", "README.md")
            os.makedirs(os.path.dirname(live_nested), exist_ok=True)
            with open(live_nested, "w") as f:
                f.write("a new nested contract surface")
            impacts = rc._impact_statements(base)   # base has an empty contracts dir -> the nested file is "added"
        surfaces = [im["surface"] for im in impacts]
        self.assertTrue(any("instance/README.md" in s for s in surfaces),
                        f"nested contract change not detected: {surfaces}")
        shutil.rmtree(base, ignore_errors=True)


class BaselineTreeSeam(unittest.TestCase):
    def test_injected_tree_wins_and_never_fetches(self):
        # an injected tree short-circuits the fetch (the test/`--baseline-tree` path stays network-free)
        tree, cleanup = rc._baseline_tree_for(rc.Baseline("v0.0.9", False, "diff"), "/some/injected/tree")
        self.assertEqual(tree, "/some/injected/tree")
        self.assertIsNone(cleanup)

    def test_first_cut_needs_no_tree(self):
        tree, cleanup = rc._baseline_tree_for(rc.Baseline(None, True, "first cut"), None)
        self.assertIsNone(tree)
        self.assertIsNone(cleanup)


class KindGrouping(unittest.TestCase):
    """The merged-PR list groups by the change-kind a title declares as a leading `Kind:` prefix."""

    def test_groups_by_prefix_and_strips_it(self):
        groups = rc._group_prs_by_kind(
            ["Feature: cold-start recall (#1)", "Fix: quote the hook path (#2)"])
        self.assertEqual(groups,
                         [("Feature", ["cold-start recall (#1)"]), ("Fix", ["quote the hook path (#2)"])])

    def test_output_follows_declared_kind_order_not_input_order(self):
        # input arrives Maintenance-first; output follows _RELEASE_NOTE_KINDS (Feature precedes Maintenance)
        groups = rc._group_prs_by_kind(["Maintenance: bump x (#2)", "Feature: add y (#1)"])
        self.assertEqual([k for k, _ in groups], ["Feature", "Maintenance"])

    def test_prefix_match_is_case_insensitive_and_canonicalises(self):
        groups = rc._group_prs_by_kind(["fix: lower (#1)", "FEATURE: upper (#2)"])
        self.assertEqual(dict(groups), {"Feature": ["upper (#2)"], "Fix": ["lower (#1)"]})

    def test_all_six_kinds_are_recognised_in_order(self):
        lines = [f"{k}: item (#{i})" for i, k in enumerate(rc._RELEASE_NOTE_KINDS)]
        self.assertEqual([k for k, _ in rc._group_prs_by_kind(lines)], rc._RELEASE_NOTE_KINDS)

    def test_unprefixed_and_non_kind_colon_titles_fall_to_other_changes_last(self):
        groups = rc._group_prs_by_kind([
            "Feature: real (#1)",
            "Refactor storage (#2)",                              # no prefix at all
            "Fix the thing without a colon (#3)",                 # kind word, but no colon => not a prefix
            "Add dark mode: respect the system setting (#4)"])    # a colon, but the lead token is not a kind
        self.assertEqual(groups[0], ("Feature", ["real (#1)"]))
        self.assertEqual(groups[-1], ("Other changes", [
            "Refactor storage (#2)",
            "Fix the thing without a colon (#3)",
            "Add dark mode: respect the system setting (#4)"]))

    def test_empty_input_yields_no_groups(self):
        self.assertEqual(rc._group_prs_by_kind([]), [])

    def test_kind_vocabulary_edit_with_a_metacharacter_matches_literally(self):
        # the kind list is the one edit point for a deployer; a kind carrying a regex metacharacter must match
        # literally and never make the (non-best-effort) render throw.
        pat = rc._compile_kind_prefix(["C++", ".NET"])
        self.assertTrue(pat.match("C++: ship it (#1)"))
        self.assertTrue(pat.match(".NET: ship it (#2)"))
        self.assertIsNone(pat.match("CXX: not this (#3)"))          # `+` is literal, not "one-or-more C"

    def test_release_notes_render_groups_under_kind_subheadings(self):
        proposal = {"engine_floor_level": "minor", "change_inventory": [], "impacts": [],
                    "merged_prs": ["Feature: add recall (#1)", "Maintenance: bump dep (#2)",
                                   "Reword copy (#3)"]}                # unprefixed => Other changes
        notes = rc.render_release_notes("v0.2.0", proposal)
        self.assertIn("## What changed since the last release (3 pull requests)", notes)
        self.assertIn("### Feature", notes)
        self.assertIn("- add recall (#1)", notes)                      # prefix stripped in the bullet
        self.assertIn("### Other changes", notes)
        self.assertIn("- Reword copy (#3)", notes)
        self.assertLess(notes.index("### Feature"), notes.index("### Maintenance"))
        self.assertLess(notes.index("### Maintenance"), notes.index("### Other changes"))

    def test_pr_body_render_groups_under_bold_labels_not_headings(self):
        # inside the single ## Scope section the kinds must be BOLD LABELS, never ### headings (a heading would
        # out-rank the plain-text "Capability and data changes:" peer and invert the outline).
        proposal = {"engine_floor_level": "minor", "change_inventory": [], "impacts": [],
                    "merged_prs": ["Feature: add recall (#1)", "Fix: patch it (#2)"]}
        applied = {"applied": True, "engine": "0.2.0", "from_engine": "0.1.0", "targets": {}}
        scope = rc.render_pr_body(proposal, applied).split("## Scope", 1)[1].split("## Out of scope", 1)[0]
        self.assertIn("**Feature**", scope)
        self.assertIn("- add recall (#1)", scope)
        self.assertNotIn("### Feature", scope)                        # no heading inside Scope


if __name__ == "__main__":
    unittest.main()
