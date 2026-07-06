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
    def test_raise_only_refuses_non_increase(self):
        with _Tree({"core": _module("core")}):
            r = rc.apply("0.0.0-dev", "0.0.0-dev", {}, None, dry_run=True)
        self.assertFalse(r["applied"])
        self.assertEqual(r["reason"], "raise-only")

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


if __name__ == "__main__":
    unittest.main()
