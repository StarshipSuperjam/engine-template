#!/usr/bin/env python3
"""Self-tests for the seed's checker-of-checkers (validator + the two guards).

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

These lock in the load-bearing teeth so a later edit to the trust root cannot
silently regress them. The deliverable-gate cold review attests that each test's
assertion matches its name; CI runs them as a step in `engine-ci`.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import weakening_guard   # noqa: E402
import protection_guard  # noqa: E402


def _run_quiet(suite, ctx):
    """validate.run() prints its operator report to stdout; these tests assert on its exit CODE, not the
    text, so capture the report to keep the unittest run quiet — the leaked 'FAIL ... boom' / 'kaboom'
    fixture lines otherwise read like a real failure."""
    with contextlib.redirect_stdout(io.StringIO()):
        return validate.run(suite, ctx)


def _check_quiet(rule_id, ctx):
    """validate.run_check() (the --check single-rule path) prints its report too; capture it so the
    unittest run stays quiet (these tests assert on the exit code, not the text)."""
    with contextlib.redirect_stdout(io.StringIO()):
        return validate.run_check(rule_id, ctx)


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
        self.assertEqual(_run_quiet("CI", {"pr_body": None}), 1)

    def test_soft_finding_passes_even_when_verdict_false(self):
        self._install(lambda rule, ctx: (False, [validate.finding("soft", "note")]))
        self.assertEqual(_run_quiet("CI", {"pr_body": None}), 0)

    def test_unregistered_hard_kind_fails_closed(self):
        validate.load_rules = lambda: [{"id": "d", "kind": "nope", "tier": "hard",
                                        "suites": ["CI"], "params": {}}]
        self.assertEqual(_run_quiet("CI", {"pr_body": None}), 1)

    def test_erroring_kind_fails_closed(self):
        def boom(rule, ctx):
            raise RuntimeError("kaboom")
        self._install(boom)
        self.assertEqual(_run_quiet("CI", {"pr_body": None}), 1)


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


class TestPRContractNoDrift(unittest.TestCase):
    """The control-plane 8-section PR-body contract must not silently drift.

    The locked contract names eight required sections, in order — Purpose, Scope,
    Out of scope, Risk, Validation, Review, Files of interest, Claude involvement —
    transcribed once above as SECTIONS (a human transcription of the control-plane
    spec; there is no in-repo machine source to derive it from, so the transcription
    itself has no mechanical correlate and is read by a human against the spec). The
    two legs below pin BOTH committed artifacts to that canonical anchor: the PR
    template a contributor fills, and the pr-body-completeness check (owned by
    validators-core) that gates the merge. A future edit that drops, renames,
    reorders, or adds a section to either one then fails CI instead of slipping
    through.

    These close two real gaps the existing completeness tests leave: those assert
    behaviour against the in-file COMPLETENESS_RULE *fixture* and only count the
    committed template's unfilled sections (test_committed_template_body_fails_
    completeness) — neither pins the heading IDENTITY of the committed template,
    nor reads the committed check at all. A `## Review` -> `## Reviewed` rename in
    the template passes the count-only test but fails leg (b) here.

    Honest ceiling: a *consistent* trim across the template, the check, AND this
    SECTIONS literal would pass both legs (the trimmer edits the anchor too). That
    residue is walled elsewhere, not here — the check and this test file both sit
    under guarded `.engine/` prefixes, so editing either is a guardrail-weakening
    change requiring `guardrail-ack`, and the operator's merge is the binding gate.
    """

    def _repo_root(self):
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def test_committed_template_headings_match_canonical(self):
        # Leg (b): the committed PR template's level-2 (##) headings equal the
        # canonical eight, in order, with no extras or omissions. Catches a
        # dropped/renamed/reordered section the count-only completeness test misses.
        path = os.path.join(self._repo_root(), ".github", "pull_request_template.md")
        with open(path, encoding="utf-8") as fh:
            headings = validate.section_order(fh.read())
        self.assertEqual(headings, SECTIONS)

    def test_committed_completeness_check_sections_match_canonical(self):
        # Leg (a): the committed pr-body-completeness check (validators-core) enforces
        # exactly the canonical eight, in order. Pins the real gating enforcement to
        # the contract; the other completeness tests only exercise the in-file fixture.
        path = os.path.join(self._repo_root(), ".engine", "check",
                            "pr-body-completeness.json")
        with open(path, encoding="utf-8") as fh:
            rule = json.load(fh)
        self.assertEqual(rule["params"]["sections"], SECTIONS)


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
        self.assertEqual(_run_quiet("CI", {}), 1)

    def test_local_nudge_suite_does_not_gate_the_same_hard_finding(self):
        self._install_hard()
        self.assertEqual(_run_quiet("pre-close", {}), 0)

    def test_undeclared_suite_is_a_config_error(self):
        self.assertEqual(_run_quiet("nope", {}), 2)

    def test_malformed_suites_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            validate.SUITES_PATH = _write(d, "suites.json", "{ not json")
            self.assertEqual(_run_quiet("CI", {}), 2)

    def test_malformed_rule_file_fails_closed_in_plain_language(self):
        # A broken check rule file is a CONFIG ERROR (exit 2), not an uncaught traceback,
        # so the non-engineer operator sees plain language, never engineer shorthand.
        def boom():
            raise ValueError("check rule is not valid JSON")
        validate.load_rules = boom
        self.assertEqual(_run_quiet("CI", {}), 2)


# ---- slice 5a: coverage / coherence / custom-script + protection re-home ----

class TestCoverageKind(unittest.TestCase):
    def test_unrecognized_mode_fails_closed(self):
        passed, found = validate.kind_coverage(_rule(kind="coverage", params={"mode": "bogus"}), {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_links_hard_in_repo_soft_outside(self):
        # mode=links keeps the link-integrity teeth: an in-repo missing target is hard,
        # an outside-repo target is a soft note. ROOT + markdown_files are stubbed so the
        # in/out-of-repo split is exercised against a controlled tree.
        with tempfile.TemporaryDirectory() as d:
            md = os.path.join(d, "doc.md")
            with open(md, "w", encoding="utf-8") as fh:
                fh.write("[broken](./missing.md)\n[outside](../nope.md)\n[ok](https://x)\n")
            orig_root, orig_mf = validate.ROOT, validate.markdown_files
            validate.ROOT = d
            validate.markdown_files = lambda exclude: [md]
            try:
                passed, found = validate.kind_coverage(
                    _rule(kind="coverage", params={"mode": "links"}), {})
            finally:
                validate.ROOT, validate.markdown_files = orig_root, orig_mf
        sev = {f["severity"] for f in found}
        self.assertIn("hard", sev)   # the in-repo missing target
        self.assertIn("soft", sev)   # the outside-repo target
        self.assertFalse(passed)

    def test_catalog_coverage_pure(self):
        surfaces = {"alpha": {"location": ".engine/alpha/"},
                    "beta": {"location": ".engine/beta/"}}
        present = {".engine/alpha/", ".engine/orphan/"}
        msgs = " ".join(f["message"] for f in
                        validate.catalog_coverage_findings(surfaces, present, "hard", "msg"))
        self.assertIn("beta", msgs)      # catalogued but absent
        self.assertIn("orphan", msgs)    # present but unclaimed
        self.assertNotIn("alpha", msgs)  # present + catalogued -> fine
        self.assertEqual(validate.catalog_coverage_findings(
            {"alpha": {"location": ".engine/alpha/"}}, {".engine/alpha/"}, "hard", "m"), [])
        self.assertEqual(validate.catalog_coverage_findings(  # infra allowlist suppresses an orphan
            {}, {".engine/boot/"}, "hard", "m", infra=[".engine/boot/"]), [])


class TestCoherenceKind(unittest.TestCase):
    def test_missing_dependency(self):
        m = [{"id": "a", "version": "1.0.0", "depends": {"b": ""}}]
        self.assertTrue(any("not installed" in x["message"]
                            for x in validate.coherence_findings(m, "hard", "msg")))

    def test_version_out_of_range(self):
        m = [{"id": "a", "version": "1.0.0", "depends": {"b": ">=2.0.0"}},
             {"id": "b", "version": "1.5.0", "depends": {}}]
        self.assertTrue(any("needs 'b'" in x["message"]
                            for x in validate.coherence_findings(m, "hard", "msg")))

    def test_in_range_is_clean(self):
        m = [{"id": "a", "version": "1.0.0", "depends": {"b": ">=1.0.0"}},
             {"id": "b", "version": "1.5.0", "depends": {}}]
        self.assertEqual(validate.coherence_findings(m, "hard", "msg"), [])

    def test_dependency_cycle(self):
        m = [{"id": "a", "version": "1.0.0", "depends": {"b": ""}},
             {"id": "b", "version": "1.0.0", "depends": {"a": ""}}]
        self.assertTrue(any("cycle" in x["message"]
                            for x in validate.coherence_findings(m, "hard", "msg")))

    def test_kind_with_no_manifests_is_clean(self):
        passed, found = validate.kind_coherence(_rule(kind="coherence"), {})
        self.assertTrue(passed)
        self.assertEqual(found, [])


class TestCustomScriptKind(unittest.TestCase):
    def _run(self, body, tier="hard", params_extra=None):
        # ROOT is repointed at the temp dir so the in-repo containment check accepts the
        # fixture script (a custom/script must be a committed, in-repo file).
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "s.py"), "w", encoding="utf-8") as fh:
                fh.write(body)
            params = {"script": "s.py"}
            if params_extra:
                params.update(params_extra)
            rule = _rule(kind="custom/script", tier=tier, params=params)
            orig = validate.ROOT
            validate.ROOT = d
            try:
                return validate.kind_custom_script(rule, {})
            finally:
                validate.ROOT = orig

    def test_findings_pass_through(self):
        passed, found = self._run(
            "import json; print(json.dumps([{'severity':'hard','message':'boom','location':None}]))")
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "boom" in f["message"] for f in found))

    def test_empty_array_is_pass(self):
        passed, found = self._run("print('[]')")
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_nonzero_exit_is_hard_regardless_of_tier(self):
        # A soft-tier rule whose script crashes still fails CLOSED (hard) — a broken guard
        # can never silently pass.
        passed, found = self._run("import sys; print('[]'); sys.exit(3)", tier="soft")
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_unparseable_output_is_hard_fail_closed(self):
        passed, found = self._run("print('not json at all')")
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_non_dict_finding_is_hard(self):
        passed, found = self._run("import json; print(json.dumps(['a', 'b']))")
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_missing_script_param_is_hard(self):
        passed, found = validate.kind_custom_script(_rule(kind="custom/script", params={}), {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_out_of_repo_script_is_refused(self):
        rule = _rule(kind="custom/script", params={"script": "../../../../etc/passwd"})
        passed, found = validate.kind_custom_script(rule, {})
        self.assertFalse(passed)
        self.assertTrue(any("outside the repository" in f["message"] for f in found))

    def test_nonexistent_in_repo_script_is_hard(self):
        rule = _rule(kind="custom/script",
                     params={"script": ".engine/tools/_nope_does_not_exist.py"})
        passed, found = validate.kind_custom_script(rule, {})
        self.assertFalse(passed)
        self.assertTrue(any("does not exist" in f["message"] for f in found))

    def test_token_reaches_only_opted_in_scripts(self):
        body = ("import os, json; print(json.dumps([{'severity': 'soft', 'location': None, "
                "'message': 'TOKEN_SEEN' if os.environ.get('GITHUB_TOKEN') else 'no-token'}]))")
        saved = dict(os.environ)
        os.environ["GITHUB_TOKEN"] = "secret"
        try:
            _, without = self._run(body)
            _, withtok = self._run(body, params_extra={"pass_token": True})
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(without[0]["message"], "no-token")    # not forwarded by default
        self.assertEqual(withtok[0]["message"], "TOKEN_SEEN")  # forwarded on opt-in


class TestProtectionReHome(unittest.TestCase):
    """The re-homed protection guard emits finding.v1 JSON: a soft fail-open note with no
    token (local), and a hard finding when the floor is missing (token present, CI)."""
    def _main_json(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = protection_guard.main()
        return rc, json.loads(buf.getvalue())

    def test_no_token_is_soft_and_exit_zero(self):
        saved = dict(os.environ)
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("GITHUB_REPOSITORY", None)
        try:
            rc, out = self._main_json()
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "soft")

    def test_missing_floor_emits_hard(self):
        saved, orig_api = dict(os.environ), protection_guard.api_get
        os.environ.update({"GITHUB_TOKEN": "x", "GITHUB_REPOSITORY": "o/r",
                           "ENGINE_RULE_TIER": "hard"})
        protection_guard.api_get = lambda path, token: []  # no rules in force -> floor missing
        try:
            rc, out = self._main_json()
        finally:
            os.environ.clear()
            os.environ.update(saved)
            protection_guard.api_get = orig_api
        self.assertEqual(rc, 0)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("not fully in force", out[0]["message"])


# ---- slice 5b: re-home the weakening guard as a custom/script rule (D-051) ----

class TestWeakeningReHome(unittest.TestCase):
    """The re-homed weakening guard emits finding.v1 JSON via the custom/script contract:
    [] when nothing weakens or the ack label is present, one hard finding (carrying the
    plain-language ack guidance) on an unacknowledged guardrail change, and a hard
    fail-closed finding when the pull-request context cannot be read OR the guard could not
    read every changed file (a partial view — a too-large PR past GitHub's file-listing
    cap). The latter is the principles §15 non-falsifiability property: a weakening edit
    must not hide past file 100 of a big PR, so the guard paginates the diff to completion
    and cross-checks what it read against the pull request's authoritative changed_files."""

    _AUTO = object()  # sentinel: derive expected from len(files) unless overridden

    def _main_json(self, event, files, expected=_AUTO):
        """Drive main() with the two network seams stubbed: the complete changed-file list
        and the authoritative changed_files count. `expected` defaults to len(files) (a
        fully-seen PR); pass a larger int to simulate a truncated / over-cap view, or None
        to simulate the count being unavailable."""
        import contextlib
        import io
        if expected is self._AUTO:
            expected = len(files)
        saved = dict(os.environ)
        orig_fetch = weakening_guard.fetch_all_changed_files
        orig_count = weakening_guard.changed_files_total
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "event.json")
            with open(ep, "w", encoding="utf-8") as fh:
                json.dump(event, fh)
            os.environ.update({"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "x",
                               "ENGINE_RULE_TIER": "hard", "GITHUB_EVENT_PATH": ep})
            weakening_guard.fetch_all_changed_files = lambda repo, number, token: files
            weakening_guard.changed_files_total = lambda repo, number, token: expected
            try:
                with contextlib.redirect_stdout(buf):
                    rc = weakening_guard.main()
            finally:
                os.environ.clear()
                os.environ.update(saved)
                weakening_guard.fetch_all_changed_files = orig_fetch
                weakening_guard.changed_files_total = orig_count
        return rc, json.loads(buf.getvalue())

    def test_no_weakening_is_empty_and_exit_zero(self):
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": "README.md", "status": "modified"}])
        self.assertEqual(rc, 0)
        self.assertEqual(out, [])

    def test_unacked_weakening_is_one_hard_with_ack_guidance(self):
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/tools/validate.py", "status": "modified"}])
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("guardrail-ack", out[0]["message"])  # the informed-consent surface (D-134)

    def test_ack_label_clears_to_empty(self):
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": [{"name": "guardrail-ack"}]}},
            [{"filename": ".engine/tools/validate.py", "status": "modified"}])
        self.assertEqual(rc, 0)
        self.assertEqual(out, [])

    def test_missing_pr_context_is_hard_fail_closed(self):
        import contextlib
        import io
        saved = dict(os.environ)
        for k in ("GITHUB_REPOSITORY", "GITHUB_TOKEN", "GITHUB_EVENT_PATH"):
            os.environ.pop(k, None)
        os.environ["ENGINE_RULE_TIER"] = "hard"
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = weakening_guard.main()
        finally:
            os.environ.clear()
            os.environ.update(saved)
        out = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")

    # ---- issue #25: paginate the changed-files fetch; fail closed on a partial view ----

    def test_next_link_parses_next_url_and_none_when_absent(self):
        """_next_link returns the rel="next" URL, and None when there is no next page."""
        header = ('<https://api.github.com/repositories/1/pulls/1/files?per_page=100&page=2>; '
                  'rel="next", <https://api.github.com/repositories/1/pulls/1/files?'
                  'per_page=100&page=9>; rel="last"')
        self.assertEqual(
            weakening_guard._next_link(header),
            "https://api.github.com/repositories/1/pulls/1/files?per_page=100&page=2")
        # only rel="last" (the last page) -> no next; and no header at all -> no next
        self.assertIsNone(weakening_guard._next_link(
            '<https://api.github.com/repositories/1/pulls/1/files?page=9>; rel="last"'))
        self.assertIsNone(weakening_guard._next_link(None))

    def test_fetch_all_changed_files_follows_pagination(self):
        """The loop follows Link: rel="next" across pages and returns EVERY changed file —
        the direct regression guard for the fail-open bug (a late-page file was invisible)."""
        page1 = [{"filename": f"docs/f{i}.md", "status": "modified"} for i in range(100)]
        page2 = [{"filename": ".engine/check/pr-body-completeness.json", "status": "modified"}]
        page2_url = ("https://api.github.com/repos/o/r/pulls/1/files?per_page=100&page=2")
        pages = {
            "/repos/o/r/pulls/1/files?per_page=100": (page1, f'<{page2_url}>; rel="next"'),
            page2_url: (page2, None),
        }
        orig = weakening_guard._get_page
        weakening_guard._get_page = lambda url, token: pages[url]
        try:
            got = weakening_guard.fetch_all_changed_files("o/r", 1, "x")
        finally:
            weakening_guard._get_page = orig
        self.assertEqual(len(got), 101)  # both pages, not just the first 100
        self.assertEqual(got[-1]["filename"], ".engine/check/pr-body-completeness.json")

    def test_weakening_on_a_late_page_is_caught(self):
        """The classifier sees the WHOLE paginated list, so a guardrail edit that lands
        after file 100 is flagged — the behavior the single-page fetch missed."""
        files = [{"filename": f"docs/f{i}.md", "status": "modified"} for i in range(100)]
        files.append({"filename": ".engine/tools/validate.py", "status": "modified"})
        rc, out = self._main_json({"pull_request": {"number": 1, "labels": []}}, files)
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("guardrail-ack", out[0]["message"])
        self.assertIn("validate.py", out[0]["message"])
        self.assertIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])

    def test_oversized_pr_fails_closed_not_clean(self):
        """When the guard reads fewer files than the PR's authoritative changed_files (a
        too-large PR past the listing cap), it fails CLOSED — a hard finding demanding the
        ack, naming PR SIZE as the cause, never the 'weakening detected' message, never []."""
        files = [{"filename": f"docs/f{i}.md", "status": "modified"} for i in range(100)]
        rc, out = self._main_json({"pull_request": {"number": 1, "labels": []}},
                                  files, expected=5000)
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("guardrail-ack", out[0]["message"])
        self.assertIn("5000", out[0]["message"])
        self.assertNotIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])

    def test_duplicate_listing_entry_cannot_mask_a_missing_file(self):
        """The completeness gate counts DISTINCT filenames, so a duplicate listing entry
        cannot inflate the tally to match changed_files while a real file goes unseen
        (§15: the guard must not be falsifiable by the change it judges). This would pass
        clean under a raw len() comparator — it must fail closed."""
        files = [{"filename": f"docs/f{i}.md", "status": "modified"} for i in range(99)]
        files.append({"filename": "docs/f0.md", "status": "modified"})  # dup -> len 100, distinct 99
        rc, out = self._main_json({"pull_request": {"number": 1, "labels": []}},
                                  files, expected=100)  # the PR truly changed 100 files
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("guardrail-ack", out[0]["message"])
        self.assertNotIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])

    def test_unavailable_count_fails_closed(self):
        """If the authoritative count is unavailable (not an int), the guard cannot confirm
        it read every file, so it fails closed rather than judging a partial view."""
        files = [{"filename": "README.md", "status": "modified"}]
        rc, out = self._main_json({"pull_request": {"number": 1, "labels": []}},
                                  files, expected=None)  # count unavailable
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("guardrail-ack", out[0]["message"])
        self.assertNotIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])


class TestRunCheckById(unittest.TestCase):
    """validate.py --check <id> runs ONE rule by id, outside any suite: it gates on a
    hard finding (exit 1 / 0 clean / 2 on unknown id), fails closed on a dangling or
    erroring kind, and does NOT load suites.json (the D-051 isolation from the suite grammar)."""
    def setUp(self):
        self._rules, self._reg, self._suites = (
            validate.load_rules, dict(validate.REGISTRY), validate.SUITES_PATH)

    def tearDown(self):
        validate.load_rules = self._rules
        validate.SUITES_PATH = self._suites
        validate.REGISTRY.clear()
        validate.REGISTRY.update(self._reg)

    def _install(self, kind_fn, kind="synthetic", tier="hard", rid="engine/check/synthetic"):
        validate.load_rules = lambda: [{"id": rid, "kind": kind, "tier": tier,
                                        "suites": [], "params": {}}]
        validate.REGISTRY[kind] = kind_fn

    def test_hard_finding_exits_one(self):
        self._install(lambda rule, ctx: (False, [validate.finding("hard", "boom")]))
        self.assertEqual(_check_quiet("engine/check/synthetic", {}), 1)

    def test_clean_exits_zero(self):
        self._install(lambda rule, ctx: (True, []))
        self.assertEqual(_check_quiet("engine/check/synthetic", {}), 0)

    def test_soft_only_exits_zero(self):
        self._install(lambda rule, ctx: (False, [validate.finding("soft", "note")]))
        self.assertEqual(_check_quiet("engine/check/synthetic", {}), 0)

    def test_unknown_id_exits_two(self):
        self._install(lambda rule, ctx: (True, []))
        self.assertEqual(_check_quiet("engine/check/nope", {}), 2)

    def test_dangling_kind_fails_closed(self):
        validate.load_rules = lambda: [{"id": "engine/check/x", "kind": "ghost",
                                        "tier": "hard", "suites": [], "params": {}}]
        self.assertEqual(_check_quiet("engine/check/x", {}), 1)

    def test_erroring_kind_fails_closed(self):
        def boom(rule, ctx):
            raise RuntimeError("kaboom")
        self._install(boom)
        self.assertEqual(_check_quiet("engine/check/synthetic", {}), 1)

    def test_does_not_load_suites_json(self):
        # Point SUITES_PATH at garbage; a by-id run must still work — it never reads the
        # suite declarations, so a broken/loosened suites.json cannot strand the guard.
        with tempfile.TemporaryDirectory() as d:
            validate.SUITES_PATH = _write(d, "suites.json", "{ not json")
            self._install(lambda rule, ctx: (True, []))
            self.assertEqual(_check_quiet("engine/check/synthetic", {}), 0)


class TestGuardRuleIsolation(unittest.TestCase):
    """The re-homed weakening guard joins NO suite (suites: []), so the head-checkout CI
    suite can never run it — it is invoked only by id from engine-guard.yml, which runs
    from the trusted base (D-051). The rule is also well-formed under check.v1.json."""
    def _guard_rule(self):
        return validate.load_json(os.path.join(validate.CHECK_DIR, "guardrail-weakening.json"))

    def test_guard_rule_joins_no_suite(self):
        self.assertEqual(self._guard_rule().get("suites"), [])

    def test_ci_roster_excludes_the_guard(self):
        # Over the real committed rules, the CI suite roster never includes the guard.
        ci = [r["id"] for r in validate.load_rules() if "CI" in r.get("suites", [])]
        self.assertNotIn("engine/check/guardrail-weakening", ci)

    def test_guard_rule_validates_against_check_schema(self):
        schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        errs = list(validate.Draft202012Validator(schema).iter_errors(self._guard_rule()))
        self.assertEqual(errs, [])


if __name__ == "__main__":
    unittest.main()
