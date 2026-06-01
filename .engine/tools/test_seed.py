#!/usr/bin/env python3
"""Self-tests for the seed's checker-of-checkers (validator + the two guards).

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

These lock in the load-bearing teeth so a later edit to the trust root cannot
silently regress them. The deliverable-gate cold review attests that each test's
assertion matches its name; CI runs them as a step in `engine-ci`.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import weakening_guard   # noqa: E402
import protection_guard  # noqa: E402

SECTIONS = ["Purpose", "Scope", "Out of scope", "Risk", "Validation", "Review",
            "Files of interest", "Claude involvement"]
COMPLETENESS_RULE = {"id": "engine/check/pr-body-completeness",
                     "target": {"context": "pull-request-body"},
                     "kind": "presence", "tier": "hard",
                     "suites": ["CI"], "params": {"sections": SECTIONS},
                     "message": "Fill the section."}


class TestCompletenessTeeth(unittest.TestCase):
    def test_placeholder_and_blank_count_as_empty(self):
        self.assertTrue(validate.is_empty_section("<why this exists>"))
        self.assertTrue(validate.is_empty_section("   \n  \n"))
        self.assertTrue(validate.is_empty_section("<a>\n\n<b>"))

    def test_real_content_is_not_empty(self):
        self.assertFalse(validate.is_empty_section("Real text."))
        self.assertFalse(validate.is_empty_section("<placeholder>\nbut also real text"))

    def test_section_blocks_parses_h2_only(self):
        blocks = validate.section_blocks("## Purpose\nx\n### Sub\ny\n## Scope\nz")
        self.assertIn("Purpose", blocks)
        self.assertIn("Scope", blocks)
        self.assertNotIn("Sub", blocks)  # a ### subsection is not a contract section

    def test_missing_body_fails_open(self):
        passed, found = validate.kind_presence(COMPLETENESS_RULE, {"pr_body": None})
        self.assertTrue(passed)
        self.assertTrue(all(f["severity"] != "hard" for f in found))

    def test_empty_body_flags_all_eight_hard(self):
        passed, found = validate.kind_presence(COMPLETENESS_RULE, {"pr_body": ""})
        self.assertFalse(passed)
        self.assertEqual(len(found), 8)
        self.assertTrue(all(f["severity"] == "hard" for f in found))

    def test_placeholder_only_body_fails(self):
        body = "\n".join(f"## {s}\n<prompt>" for s in SECTIONS)
        passed, found = validate.kind_presence(COMPLETENESS_RULE, {"pr_body": body})
        self.assertFalse(passed)
        self.assertEqual(len(found), 8)

    def test_filled_body_passes(self):
        body = "\n".join(f"## {s}\nreal content for {s}" for s in SECTIONS)
        passed, found = validate.kind_presence(COMPLETENESS_RULE, {"pr_body": body})
        self.assertTrue(passed)
        self.assertEqual(found, [])


class TestDispatcherGate(unittest.TestCase):
    """Lock in the fix: the CI exit code gates on a hard-severity finding, never on
    a callable's verdict flag, so report() and the exit code can never disagree."""
    def setUp(self):
        self._rules, self._reg = validate.load_rules, dict(validate.REGISTRY)

    def tearDown(self):
        validate.load_rules = self._rules
        validate.REGISTRY.clear()
        validate.REGISTRY.update(self._reg)

    def _install(self, kind_fn, tier="hard"):
        validate.load_rules = lambda: [{"id": "synthetic", "kind": "synthetic",
                                        "tier": tier, "suites": ["CI"], "params": {}}]
        validate.REGISTRY["synthetic"] = kind_fn

    def test_hard_finding_fails_even_when_verdict_true(self):
        self._install(lambda rule, ctx: (True, [validate.finding("hard", "boom")]))
        self.assertEqual(validate.run("CI", {"pr_body": None}), 1)

    def test_soft_finding_passes_even_when_verdict_false(self):
        self._install(lambda rule, ctx: (False, [validate.finding("soft", "note")]))
        self.assertEqual(validate.run("CI", {"pr_body": None}), 0)

    def test_unregistered_hard_kind_fails_closed(self):
        validate.load_rules = lambda: [{"id": "d", "kind": "nope", "tier": "hard",
                                        "suites": ["CI"], "params": {}}]
        self.assertEqual(validate.run("CI", {"pr_body": None}), 1)

    def test_erroring_kind_fails_closed(self):
        def boom(rule, ctx):
            raise RuntimeError("kaboom")
        self._install(boom)
        self.assertEqual(validate.run("CI", {"pr_body": None}), 1)


class TestWeakeningClassifier(unittest.TestCase):
    def test_is_guardrail_covers_guards_and_lockfiles(self):
        for p in (".github/workflows/engine-ci.yml", ".engine/check/x.json",
                  ".engine/tools/validate.py", ".github/CODEOWNERS",
                  ".engine/pyproject.toml", ".engine/uv.lock", ".engine/suites.json"):
            self.assertTrue(weakening_guard.is_guardrail(p), p)
        for p in ("README.md", "src/app.py", ".gitignore"):
            self.assertFalse(weakening_guard.is_guardrail(p), p)

    def test_suite_declarations_are_a_guarded_killswitch(self):
        # .engine/suites.json decides which suite blocks the merge; a schema-valid
        # edit (CI -> local-nudge) would silently un-gate CI, so modifying it must
        # be flagged for the guardrail-ack (core slice 4). A pure addition does not.
        self.assertTrue(weakening_guard.is_guardrail(".engine/suites.json"))
        flagged = weakening_guard.flagged_changes(
            [{"filename": ".engine/suites.json", "status": "modified"}])
        self.assertEqual(len(flagged), 1)
        self.assertEqual(weakening_guard.flagged_changes(
            [{"filename": ".engine/suites.json", "status": "added"}]), [])

    def test_copied_status_is_caught(self):
        self.assertIn("copied", weakening_guard.WEAKENING_STATUS)
        flagged = weakening_guard.flagged_changes(
            [{"filename": ".github/workflows/x.yml", "status": "copied"}])
        self.assertEqual(len(flagged), 1)

    def test_removed_renamed_and_modified_lock_are_flagged(self):
        files = [
            {"filename": ".engine/tools/validate.py", "status": "removed"},
            {"filename": ".github/workflows/new.yml", "status": "renamed",
             "previous_filename": ".github/workflows/engine-ci.yml"},
            {"filename": ".engine/uv.lock", "status": "modified"},
        ]
        self.assertEqual(len(weakening_guard.flagged_changes(files)), 3)

    def test_addition_and_nonguardrail_not_flagged(self):
        files = [
            {"filename": ".github/workflows/new.yml", "status": "added"},
            {"filename": "README.md", "status": "modified"},
        ]
        self.assertEqual(weakening_guard.flagged_changes(files), [])


class TestProtectionFloor(unittest.TestCase):
    CHECKS = ["engine-ci", "engine-guard"]

    def _full(self):
        return [
            {"type": "pull_request", "parameters": {
                "required_review_thread_resolution": True, "required_approving_review_count": 0}},
            {"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "engine-ci"}, {"context": "engine-guard"}]}},
            {"type": "non_fast_forward", "parameters": {}},
            {"type": "deletion", "parameters": {}},
        ]

    def test_full_floor_has_nothing_missing(self):
        self.assertEqual(protection_guard.missing_floor(self._full(), self.CHECKS), [])

    def test_empty_rules_flags_every_floor_piece(self):
        missing = protection_guard.missing_floor([], self.CHECKS)
        self.assertTrue(any("pull request" in m for m in missing))
        self.assertTrue(any("status checks" in m for m in missing))
        self.assertTrue(any("force-push" in m for m in missing))
        self.assertTrue(any("deletion" in m for m in missing))

    def test_unbound_required_check_is_flagged(self):
        rules = self._full()
        rules[1]["parameters"]["required_status_checks"] = [{"context": "engine-ci"}]
        missing = protection_guard.missing_floor(rules, self.CHECKS)
        self.assertTrue(any("engine-guard" in m for m in missing))

    def test_conversation_resolution_required(self):
        rules = self._full()
        rules[0]["parameters"]["required_review_thread_resolution"] = False
        missing = protection_guard.missing_floor(rules, self.CHECKS)
        self.assertTrue(any("conversations" in m for m in missing))


class TestDecoratedScaffold(unittest.TestCase):
    """The visible-scaffold template: decorated placeholder slots still read as
    unfilled, real content reads as filled, and an inline <token> in real text is
    not mistaken for a placeholder (the over-strip guard)."""

    def test_decorated_placeholder_lines_are_empty(self):
        for line in ("**<summary>**", "- <detail>", "*<Impact: why>*",
                     "<bare>", "__<x>__", "  - <y>  "):
            self.assertTrue(validate.is_empty_section(line), line)

    def test_real_content_lines_are_not_empty(self):
        for line in ("**Real bold summary**", "- a real detail", "*Impact: real text*",
                     "Uses the <head> ref here.", "- text with <token> inside"):
            self.assertFalse(validate.is_empty_section(line), line)

    def test_decorated_section_with_one_real_line_is_not_empty(self):
        self.assertFalse(validate.is_empty_section("**<summary>**\n- a real bullet\n*<Impact: x>*"))

    def test_committed_template_body_fails_completeness(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, ".github", "pull_request_template.md"), encoding="utf-8") as fh:
            tmpl = fh.read()
        passed, found = validate.kind_presence(COMPLETENESS_RULE, {"pr_body": tmpl})
        self.assertFalse(passed)
        self.assertEqual(len(found), len(SECTIONS))  # every section unfilled

    def test_filled_scaffold_passes(self):
        body = "\n".join(
            f"## {s}\n**Real summary for {s}**\n- a real bullet\n*Impact: real impact*"
            for s in SECTIONS)
        passed, found = validate.kind_presence(COMPLETENESS_RULE, {"pr_body": body})
        self.assertTrue(passed)
        self.assertEqual(found, [])


# ---- slice 4: the generic closed kinds + suite-context gating --------------

META = validate.META_SCHEMA_URI


def _rule(**kw):
    base = {"id": "engine/check/x", "target": {}, "kind": "schema", "tier": "hard",
            "suites": ["CI"], "params": {}, "message": "Fix it."}
    base.update(kw)
    return base


def _run_kind(kind_fn, rule, files):
    """Run a kind callable with validate.target_files stubbed to `files`, so a
    fixture under a temp dir (or a real repo file) can be targeted directly."""
    orig = validate.target_files
    validate.target_files = lambda r: list(files)
    try:
        return kind_fn(rule, {})
    finally:
        validate.target_files = orig


def _write(d, name, obj):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh) if isinstance(obj, (dict, list)) else fh.write(obj)
    return p


class TestPresenceFileTarget(unittest.TestCase):
    """presence works on a prose file target too, reusing the placeholder teeth."""
    def test_missing_and_placeholder_sections_on_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "doc.md", "## Alpha\nreal content\n## Beta\n<placeholder>\n")
            rule = _rule(kind="presence", target={"path": "x"},
                         params={"sections": ["Alpha", "Beta", "Gamma"]})
            passed, found = _run_kind(validate.kind_presence, rule, [p])
        self.assertFalse(passed)
        msgs = " ".join(f["message"] for f in found)
        self.assertIn("Beta", msgs)      # present but placeholder-only -> empty
        self.assertIn("Gamma", msgs)     # missing
        self.assertNotIn("Alpha", msgs)  # filled -> fine
        self.assertTrue(all(f["severity"] == "hard" for f in found))


class TestSchemaKind(unittest.TestCase):
    SCHEMA = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}

    def test_valid_instance_passes(self):
        with tempfile.TemporaryDirectory() as d:
            sp = _write(d, "s.json", self.SCHEMA)
            ip = _write(d, "i.json", {"a": "hi"})
            passed, found = _run_kind(validate.kind_schema, _rule(params={"schema": sp}), [ip])
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_invalid_instance_flags_at_tier(self):
        with tempfile.TemporaryDirectory() as d:
            sp = _write(d, "s.json", self.SCHEMA)
            ip = _write(d, "i.json", {"a": 1})  # wrong type
            passed, found = _run_kind(validate.kind_schema, _rule(params={"schema": sp}), [ip])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_malformed_governing_schema_is_loud(self):
        with tempfile.TemporaryDirectory() as d:
            sp = _write(d, "s.json", {"type": 123})  # not a well-formed schema
            ip = _write(d, "i.json", {"a": "hi"})
            passed, found = _run_kind(validate.kind_schema, _rule(params={"schema": sp}), [ip])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_offline_external_ref_is_caught_never_fetched(self):
        with tempfile.TemporaryDirectory() as d:
            sp = _write(d, "s.json", {"$ref": "https://example.com/nope.json"})
            ip = _write(d, "i.json", {"a": "hi"})
            passed, found = _run_kind(validate.kind_schema, _rule(params={"schema": sp}), [ip])
        self.assertFalse(passed)
        self.assertTrue(any("unresolvable" in f["message"] for f in found))

    def test_malformed_json_target_is_loud(self):
        with tempfile.TemporaryDirectory() as d:
            sp = _write(d, "s.json", self.SCHEMA)
            ip = _write(d, "i.json", "{ not json")
            passed, found = _run_kind(validate.kind_schema, _rule(params={"schema": sp}), [ip])
        self.assertFalse(passed)
        self.assertTrue(any("not valid JSON" in f["message"] for f in found))

    def test_wellformedness_failure_via_meta_schema(self):
        # params.schema == the dialect URI => validate the target file AS a schema.
        with tempfile.TemporaryDirectory() as d:
            bad = _write(d, "bad.schema.json", {"type": 123})
            passed, found = _run_kind(validate.kind_schema, _rule(params={"schema": META}), [bad])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_catalog_routing_wellformedness_on_a_real_schema_passes(self):
        # No params: routing resolves the schema surface -> the 2020-12 meta-schema ->
        # finding.v1.json is checked for well-formedness and passes. Proves catalog-first
        # resolution against a real merged file.
        real = os.path.join(validate.SCHEMAS_DIR, "finding.v1.json")
        rule = _rule(target={"path": ".engine/schemas/finding.v1.json"})
        passed, found = _run_kind(validate.kind_schema, rule, [real])
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_catalog_routing_validates_a_real_check_rule_instance(self):
        # The central "routing reuses the catalog" path for an ordinary INSTANCE (not a
        # schema): a real check file routes via the catalog (.engine/check/ -> the check
        # surface -> governing_schema check.v1.json) and is validated against it. Proves
        # catalog-first resolution loads the sibling schema and checks the instance.
        real = os.path.join(validate.ENGINE_DIR, "check", "link-integrity.json")
        rule = _rule(target={"path": ".engine/check/link-integrity.json"})
        passed, found = _run_kind(validate.kind_schema, rule, [real])
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_params_override_validates_catalog_against_its_meta_contract(self):
        # The catalog is governed by its own meta-contract, not the meta-schema URL its
        # schema-surface record carries; a params override names the right schema. (This
        # proves the override path validates the catalog against surface-catalog.schema.json;
        # the override exists for this self-governance edge that catalog routing can't express.)
        rule = _rule(params={"schema": ".engine/schemas/surface-catalog.schema.json"})
        passed, found = _run_kind(validate.kind_schema, rule, [validate.CATALOG_PATH])
        self.assertTrue(passed)
        self.assertEqual(found, [])


class TestShapeKind(unittest.TestCase):
    PARAMS = {"required_sections": ["Decision", "Rationale", "Status"],
              "allowed_sections": ["Supersedes"], "length_budget": 6}

    def _shape(self, body, tier="hard"):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "x.md", body)
            rule = _rule(kind="shape", tier=tier, target={"path": "x"}, params=self.PARAMS)
            return _run_kind(validate.kind_shape, rule, [p])

    def test_well_formed_instance_passes(self):
        passed, found = self._shape("## Decision\nx\n## Rationale\ny\n## Status\nz\n")
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_missing_required_is_rule_tier(self):
        passed, found = self._shape("## Decision\nx\n## Status\nz\n")  # Rationale missing
        self.assertFalse(passed)
        self.assertTrue(any("Rationale" in f["message"] and f["severity"] == "hard" for f in found))

    def test_required_out_of_order_is_flagged(self):
        passed, found = self._shape("## Rationale\ny\n## Decision\nx\n## Status\nz\n")
        self.assertTrue(any("out of order" in f["message"] for f in found))

    def test_section_outside_allowed_is_flagged(self):
        passed, found = self._shape("## Decision\nx\n## Rationale\ny\n## Status\nz\n## Bogus\nq\n")
        self.assertTrue(any("Bogus" in f["message"] for f in found))

    def test_over_budget_is_soft_only_never_hard(self):
        body = "## Decision\n" + "\n".join(["line"] * 12) + "\n## Rationale\ny\n## Status\nz\n"
        passed, found = self._shape(body)
        over = [f for f in found if "budget" in f["message"]]
        self.assertTrue(over)
        self.assertTrue(all(f["severity"] == "soft" for f in over))
        self.assertFalse(any(f["severity"] == "hard" for f in found))  # length never the hard tier


class TestSuiteContextGating(unittest.TestCase):
    """The locked tier-vs-context law: a hard finding fails the run ONLY in a
    blocking-gate context (CI). A suites.json that mislabeled CI's context would
    silently un-gate the merge — this is the test that would catch that."""
    def setUp(self):
        self._rules, self._reg, self._suites = (
            validate.load_rules, dict(validate.REGISTRY), validate.SUITES_PATH)

    def tearDown(self):
        validate.load_rules = self._rules
        validate.SUITES_PATH = self._suites
        validate.REGISTRY.clear()
        validate.REGISTRY.update(self._reg)

    def _install_hard(self):
        validate.load_rules = lambda: [{"id": "synthetic", "kind": "synthetic", "tier": "hard",
                                        "suites": ["CI", "pre-close"], "params": {}}]
        validate.REGISTRY["synthetic"] = lambda rule, ctx: (False, [validate.finding("hard", "boom")])

    def test_ci_blocking_gate_fails_on_hard(self):
        self._install_hard()
        self.assertEqual(validate.run("CI", {}), 1)

    def test_local_nudge_suite_does_not_gate_the_same_hard_finding(self):
        self._install_hard()
        self.assertEqual(validate.run("pre-close", {}), 0)

    def test_undeclared_suite_is_a_config_error(self):
        self.assertEqual(validate.run("nope", {}), 2)

    def test_malformed_suites_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            validate.SUITES_PATH = _write(d, "suites.json", "{ not json")
            self.assertEqual(validate.run("CI", {}), 2)

    def test_malformed_rule_file_fails_closed_in_plain_language(self):
        # A broken check rule file is a CONFIG ERROR (exit 2), not an uncaught traceback,
        # so the non-engineer operator sees plain language, never engineer shorthand.
        def boom():
            raise ValueError("check rule is not valid JSON")
        validate.load_rules = boom
        self.assertEqual(validate.run("CI", {}), 2)


if __name__ == "__main__":
    unittest.main()
