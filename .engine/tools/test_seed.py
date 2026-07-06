#!/usr/bin/env python3
"""Self-tests for the seed's checker-of-checkers (validator + the two guards).

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock in the load-bearing teeth so a later edit to the trust root cannot
silently regress them. The deliverable-gate cold review attests that each test's
assertion matches its name; CI runs them as a step in `engine-ci`.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import re
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


class TestCiAuthorExempt(unittest.TestCase):
    """The engine honors a rule's `ci_author_exempt` in the merge gate as a DISCLOSED
    not-applicable pass — never a silent green; the closed kinds stay author-agnostic; the
    by-id guard path is never exempt; exact-match only. (D-207/D-208, issue #116.)"""
    DISCLOSURE = "NOT APPLICABLE"

    def setUp(self):
        self._rules, self._reg = validate.load_rules, dict(validate.REGISTRY)

    def tearDown(self):
        validate.load_rules = self._rules
        validate.REGISTRY.clear()
        validate.REGISTRY.update(self._reg)

    def _install(self, *, suites=("CI",), exempt=("dependabot[bot]",)):
        # A synthetic rule whose kind ALWAYS hard-fails — so a PASS proves the engine skipped
        # it (exempt) and a FAIL proves the kind ran (enforced). The marker text "the kind ran"
        # appears iff the kind was dispatched.
        validate.load_rules = lambda: [{"id": "engine/check/synthetic-exempt",
                                        "kind": "always-fail", "tier": "hard",
                                        "suites": list(suites), "params": {},
                                        "ci_author_exempt": list(exempt)}]
        validate.REGISTRY["always-fail"] = lambda rule, ctx: (
            False, [validate.finding("hard", "the kind ran")])

    def _run(self, suite, ctx):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = validate.run(suite, ctx)
        return rc, out.getvalue()

    def test_exempt_author_passes_in_ci_with_disclosure(self):
        self._install()
        rc, text = self._run("CI", {"pr_body": "", "pr_author": "dependabot[bot]"})
        self.assertEqual(rc, 0)                  # exempt → no hard finding → completed pass
        self.assertIn(self.DISCLOSURE, text)     # disclosed, never a silent green (build-owe #3)
        self.assertNotIn("the kind ran", text)   # the kind was skipped before dispatch

    def test_github_actions_bot_author_is_exempt(self):
        # The scheduled self-review DIGEST pull request is opened by github-actions[bot] (audit-prep.yml opens
        # it via the workflow GITHUB_TOKEN) and carries a plain-language body, not the eight-section template;
        # like dependabot it is an exempted, disclosed not-applicable pass (this is what clears the digest PR's
        # otherwise-red engine-ci). Proves the engine honors the EXACT bot login, brackets and all. (The
        # memory-erasure proposal is NOT bot-authored — a local SessionStart hook opens it under the operator's
        # own gh token — so it is cleared by the engine-erasure LABEL exemption instead; see TestCiLabelExempt.)
        self._install(exempt=("dependabot[bot]", "github-actions[bot]"))
        rc, text = self._run("CI", {"pr_body": "", "pr_author": "github-actions[bot]"})
        self.assertEqual(rc, 0)
        self.assertIn(self.DISCLOSURE, text)
        self.assertNotIn("the kind ran", text)

    def test_nonexempt_author_still_enforced(self):
        self._install()
        rc, text = self._run("CI", {"pr_body": "", "pr_author": "a-human"})
        self.assertEqual(rc, 1)
        self.assertIn("the kind ran", text)
        self.assertNotIn(self.DISCLOSURE, text)

    def test_no_author_enforced(self):
        # Local run / --pr-body-file / malformed event → None author → never matches → enforces.
        self._install()
        rc, _ = self._run("CI", {"pr_body": "", "pr_author": None})
        self.assertEqual(rc, 1)

    def test_wrong_case_author_enforced(self):
        self._install()
        rc, text = self._run("CI", {"pr_body": "", "pr_author": "Dependabot[Bot]"})
        self.assertEqual(rc, 1)                  # exact match only; no silent case-fold widening
        self.assertIn("the kind ran", text)

    def test_exempt_only_in_blocking_gate_suite(self):
        # The same rule + author in a non-blocking-gate suite is NOT exempted: the kind runs
        # (advisory there, so the exit code can't distinguish — assert on the text). This locks
        # the gate to the suite's blocking-gate CONTEXT, not the literal name "CI" (build-owe #2).
        self._install(suites=("pre-commit",))
        rc, text = self._run("pre-commit", {"pr_body": "", "pr_author": "dependabot[bot]"})
        self.assertNotIn(self.DISCLOSURE, text)
        self.assertIn("the kind ran", text)

    def test_by_id_guard_path_never_exempt(self):
        # run_check() (the by-id path engine-guard uses) carries no suite, so it never honors
        # ci_author_exempt — the §15 guard judges Dependabot too (build-owe #7).
        self._install()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = validate.run_check("engine/check/synthetic-exempt",
                                    {"pr_body": "", "pr_author": "dependabot[bot]"})
        self.assertEqual(rc, 1)
        self.assertNotIn(self.DISCLOSURE, out.getvalue())

    def test_kind_presence_is_author_agnostic(self):
        # The closed kind never reads the author: an exempt author still fails an empty body.
        passed, found = validate.kind_presence(
            COMPLETENESS_RULE, {"pr_body": "", "pr_author": "dependabot[bot]"})
        self.assertFalse(passed)
        self.assertEqual(len(found), 8)

    def test_exempt_in_any_blocking_gate_suite_not_just_ci(self):
        # The positive of test_exempt_only_in_blocking_gate_suite: the gate keys on the
        # blocking-gate CONTEXT, not the literal name "CI", so a differently-named blocking-gate
        # suite ALSO exempts. Locks the plan-gate decision to gate on `gates`, future-proofing a
        # second blocking-gate suite. (Today CI is the only one — so this needs a synthetic suite.)
        self._install(suites=("release-gate",))
        saved = validate.load_suites
        validate.load_suites = lambda: {"release-gate": {"trigger": "x", "context": "blocking-gate"}}
        try:
            rc, text = self._run("release-gate", {"pr_body": "", "pr_author": "dependabot[bot]"})
        finally:
            validate.load_suites = saved
        self.assertEqual(rc, 0)
        self.assertIn(self.DISCLOSURE, text)
        self.assertNotIn("the kind ran", text)


class TestCiLabelExempt(unittest.TestCase):
    """The engine honors a rule's `ci_label_exempt` in the merge gate as a DISCLOSED not-applicable
    pass — keyed on a LABEL the pull request carries (e.g. engine-erasure), not its author — so a
    single-purpose pull-request class whose own plain body is its account is waived without a silent
    green; the closed kinds stay label-agnostic; the by-id guard path is never exempt; exact-match
    only. The label-keyed sibling of TestCiAuthorExempt (the memory-erasure proposal is operator-
    authored, so the author exemption cannot reach it — this is what clears its engine-ci)."""
    DISCLOSURE = "NOT APPLICABLE"

    def setUp(self):
        self._rules, self._reg = validate.load_rules, dict(validate.REGISTRY)

    def tearDown(self):
        validate.load_rules = self._rules
        validate.REGISTRY.clear()
        validate.REGISTRY.update(self._reg)

    def _install(self, *, suites=("CI",), exempt=("engine-erasure",)):
        # The same always-fail synthetic kind TestCiAuthorExempt uses: a PASS proves the engine
        # skipped it (exempt), a FAIL proves the kind ran ("the kind ran" appears iff dispatched).
        validate.load_rules = lambda: [{"id": "engine/check/synthetic-label-exempt",
                                        "kind": "always-fail", "tier": "hard",
                                        "suites": list(suites), "params": {},
                                        "ci_label_exempt": list(exempt)}]
        validate.REGISTRY["always-fail"] = lambda rule, ctx: (
            False, [validate.finding("hard", "the kind ran")])

    def _run(self, suite, ctx):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = validate.run(suite, ctx)
        return rc, out.getvalue()

    def test_exempt_label_passes_in_ci_with_disclosure(self):
        self._install()
        rc, text = self._run("CI", {"pr_body": "", "pr_labels": ["engine-erasure"]})
        self.assertEqual(rc, 0)                  # waived → no hard finding → completed pass
        self.assertIn(self.DISCLOSURE, text)     # disclosed, never a silent green
        self.assertNotIn("the kind ran", text)   # the kind was skipped before dispatch

    def test_one_matching_label_among_many_exempts(self):
        self._install()
        rc, text = self._run("CI", {"pr_body": "", "pr_labels": ["chore", "engine-erasure", "z"]})
        self.assertEqual(rc, 0)
        self.assertIn(self.DISCLOSURE, text)
        self.assertNotIn("the kind ran", text)

    def test_nonexempt_label_still_enforced(self):
        self._install()
        rc, text = self._run("CI", {"pr_body": "", "pr_labels": ["some-other-label"]})
        self.assertEqual(rc, 1)
        self.assertIn("the kind ran", text)
        self.assertNotIn(self.DISCLOSURE, text)

    def test_no_labels_enforced(self):
        # Local run / --pr-body-file / malformed event → [] labels → never matches → enforces.
        self._install()
        rc, _ = self._run("CI", {"pr_body": "", "pr_labels": []})
        self.assertEqual(rc, 1)

    def test_wrong_case_label_enforced(self):
        self._install()
        rc, text = self._run("CI", {"pr_body": "", "pr_labels": ["Engine-Erasure"]})
        self.assertEqual(rc, 1)                  # exact match only; no silent case-fold widening
        self.assertIn("the kind ran", text)

    def test_exempt_only_in_blocking_gate_suite(self):
        # The same rule + label in a non-blocking-gate suite is NOT exempted: the kind runs
        # (advisory there, so assert on the text). Locks the gate to the suite's blocking-gate
        # CONTEXT, not the literal name "CI".
        self._install(suites=("pre-commit",))
        rc, text = self._run("pre-commit", {"pr_body": "", "pr_labels": ["engine-erasure"]})
        self.assertNotIn(self.DISCLOSURE, text)
        self.assertIn("the kind ran", text)

    def test_by_id_guard_path_never_exempt(self):
        # run_check() (the by-id path engine-guard uses) carries no suite, so it never honors
        # ci_label_exempt — the §15 guard judges an engine-erasure-labelled PR too.
        self._install()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = validate.run_check("engine/check/synthetic-label-exempt",
                                    {"pr_body": "", "pr_labels": ["engine-erasure"]})
        self.assertEqual(rc, 1)
        self.assertNotIn(self.DISCLOSURE, out.getvalue())

    def test_kind_presence_is_label_agnostic(self):
        # The closed kind never reads labels: an exempt label still fails an empty body.
        passed, found = validate.kind_presence(
            COMPLETENESS_RULE, {"pr_body": "", "pr_labels": ["engine-erasure"]})
        self.assertFalse(passed)
        self.assertEqual(len(found), 8)


class TestCheckSchemaCiAuthorExempt(unittest.TestCase):
    """The optional `ci_author_exempt` field is additive: the committed rule carries it,
    the schema still requires exactly the seven, and no committed rule is invalidated."""
    def _schema(self):
        return validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))

    def test_schema_still_requires_exactly_the_seven(self):
        self.assertEqual(self._schema()["required"],
                         ["id", "target", "kind", "params", "tier", "suites", "message"])

    def test_committed_pr_body_rule_declares_and_validates(self):
        # Two exempt bot AUTHORS: dependabot[bot] (its dependency PRs) and github-actions[bot] (the scheduled
        # self-review digest pull request, opened via the workflow token) — both carry their own plain-language
        # body, never the eight-section template. Plus one exempt LABEL, engine-erasure: the single-purpose
        # memory-erasure proposal is opened by a local hook under the operator's own identity (NOT a bot), so the
        # author exemption cannot reach it — its deliberate plain consent body is cleared by the label instead.
        # A drop of any of these silently re-breaks those PRs' engine-ci, so pin the exact lists.
        rule = validate.load_json(os.path.join(validate.CHECK_DIR, "pr-body-completeness.json"))
        self.assertEqual(rule.get("ci_author_exempt"), ["dependabot[bot]", "github-actions[bot]"])
        self.assertEqual(rule.get("ci_label_exempt"), ["engine-erasure"])
        errs = list(validate.Draft202012Validator(self._schema()).iter_errors(rule))
        self.assertEqual(errs, [])

    def test_every_committed_check_rule_validates(self):
        v = validate.Draft202012Validator(self._schema())
        for name in sorted(os.listdir(validate.CHECK_DIR)):
            if not name.endswith(".json"):
                continue
            rule = validate.load_json(os.path.join(validate.CHECK_DIR, name))
            errs = list(v.iter_errors(rule))
            self.assertEqual(errs, [], f"{name} does not conform to check.v1.json: {errs}")


class TestGetPrAuthor(unittest.TestCase):
    """get_pr_author() reads the trusted event context (.pull_request.user.login) and degrades
    to None — therefore to ENFORCING — on any doubt; it NEVER consults github.actor (the spoof
    vector a re-run would attribute to the re-runner). The one security-load-bearing parser, so
    it is tested directly against a real GITHUB_EVENT_PATH file, not via an injected ctx."""
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("GITHUB_EVENT_PATH", "GITHUB_ACTOR")}
        self._paths = []

    def tearDown(self):
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        for p in self._paths:
            if os.path.exists(p):
                os.unlink(p)

    def _event(self, raw):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write(raw if isinstance(raw, str) else json.dumps(raw))
        self._paths.append(path)
        os.environ["GITHUB_EVENT_PATH"] = path

    def test_valid_event_returns_login(self):
        self._event({"pull_request": {"user": {"login": "octocat"}}})
        self.assertEqual(validate.get_pr_author(), "octocat")

    def test_degrades_to_none_on_every_malformed_shape(self):
        for raw in ({"pull_request": {"user": None}},        # null user
                    {"pull_request": {"user": {}}},          # user present, no login
                    {"pull_request": {"user": "a-string"}},  # type-confused user (was a crash)
                    {"pull_request": []},                    # type-confused pull_request
                    {"pull_request": None},                  # null pull_request
                    {},                                      # no pull_request
                    "this is not json"):                     # unparseable event
            with self.subTest(raw=raw):
                self._event(raw)
                self.assertIsNone(validate.get_pr_author())

    def test_missing_file_or_unset_env_returns_none(self):
        os.environ["GITHUB_EVENT_PATH"] = "/no/such/event/file.json"
        self.assertIsNone(validate.get_pr_author())
        os.environ.pop("GITHUB_EVENT_PATH", None)
        self.assertIsNone(validate.get_pr_author())

    def test_never_consults_github_actor(self):
        # Even with a planted actor, an event with no author resolves to None — never the actor.
        os.environ["GITHUB_ACTOR"] = "dependabot[bot]"
        self._event({"pull_request": {"user": {}}})
        self.assertIsNone(validate.get_pr_author())


class TestGetPrLabels(unittest.TestCase):
    """get_pr_labels() reads the trusted event context (.pull_request.labels[].name) and degrades to
    [] — therefore to ENFORCING the rule — on any doubt: a non-list labels field, a label without a
    string name, an unreadable/partial event. The second security-load-bearing event parser (its
    falsely-exempt failure mode would waive a hard check), so it is tested directly against a real
    GITHUB_EVENT_PATH file, not via an injected ctx — the same posture as get_pr_author."""
    def setUp(self):
        self._env = {"GITHUB_EVENT_PATH": os.environ.get("GITHUB_EVENT_PATH")}
        self._paths = []

    def tearDown(self):
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        for p in self._paths:
            if os.path.exists(p):
                os.unlink(p)

    def _event(self, raw):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write(raw if isinstance(raw, str) else json.dumps(raw))
        self._paths.append(path)
        os.environ["GITHUB_EVENT_PATH"] = path

    def test_valid_event_returns_label_names_in_order(self):
        self._event({"pull_request": {"labels": [{"name": "engine-erasure"}, {"name": "chore"}]}})
        self.assertEqual(validate.get_pr_labels(), ["engine-erasure", "chore"])

    def test_no_labels_is_empty_not_an_error(self):
        self._event({"pull_request": {"labels": []}})
        self.assertEqual(validate.get_pr_labels(), [])

    def test_degrades_to_empty_on_every_malformed_shape(self):
        for raw in ({"pull_request": {"labels": None}},               # null labels
                    {"pull_request": {"labels": "engine-erasure"}},   # type-confused labels (str, not list)
                    {"pull_request": {"labels": [{"name": None}]}},   # name present but not a string
                    {"pull_request": []},                             # type-confused pull_request
                    {"pull_request": None},                           # null pull_request
                    {},                                               # no pull_request
                    "this is not json"):                              # unparseable event
            with self.subTest(raw=raw):
                self._event(raw)
                self.assertEqual(validate.get_pr_labels(), [])

    def test_mixed_items_keep_only_well_formed_string_names(self):
        # A real labels array of valid label objects mixed with junk → only the string names survive,
        # never a crash and never a falsely-included non-string.
        self._event({"pull_request": {"labels": [
            {"name": "engine-erasure"}, {"no-name": "x"}, "a-string", 7, None, {"name": 9}]}})
        self.assertEqual(validate.get_pr_labels(), ["engine-erasure"])

    def test_missing_file_or_unset_env_returns_empty(self):
        os.environ["GITHUB_EVENT_PATH"] = "/no/such/event/file.json"
        self.assertEqual(validate.get_pr_labels(), [])
        os.environ.pop("GITHUB_EVENT_PATH", None)
        self.assertEqual(validate.get_pr_labels(), [])


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

    def test_validator_and_guard_are_permanent_floor_members(self):
        # D-268 build-owe 5 — the self-protection property that must NOT silently lapse: the validator and this
        # guard are guarded regardless of the derived set (validate.py is the sole home of the 5 built-in HARD
        # check kinds, which carry no params.script and so are unreachable by the derived clause).
        for p in (".engine/tools/validate.py", ".engine/tools/weakening_guard.py"):
            self.assertTrue(weakening_guard.is_guardrail(p, derived_scripts=frozenset()), p)

    def test_settings_json_and_ruleset_proxy_are_floored(self):
        # The live hole D-268 closed (settings.json wires the enforcement hooks) + the ruleset-applying proxy.
        for p in (".claude/settings.json", ".engine/tools/bootstrap.py"):
            self.assertTrue(weakening_guard.is_guardrail(p, derived_scripts=frozenset()), p)

    def test_enforcement_hook_gates_are_floored(self):
        # The hand-listed enforcement-hook gates (build-owe 3 audit) — not check-scripts, guarded by the floor.
        # Includes BOTH block-budget members: modes.py (PreToolUse write-gate) and close.py (Stop disposition gate).
        for p in (".engine/tools/modes.py", ".engine/tools/close.py", ".engine/tools/hook-runner.sh",
                  ".engine/tools/hooks.py", ".engine/tools/issue_gate.py", ".engine/tools/github_client.py",
                  ".engine/tools/wiring.py", ".engine/tools/security_floor.py"):
            self.assertTrue(weakening_guard.is_guardrail(p, derived_scripts=frozenset()), p)

    def test_non_gate_tooling_is_not_guarded(self):
        # The over-firing D-268 fixes: benign tools (boot, memory, telemetry, status, the self-review renderer,
        # attention) are NOT guarded when the derived set does not name them — the whole point of the narrowing.
        derived = frozenset({".engine/tools/protection_guard.py"})
        for p in (".engine/tools/boot.py", ".engine/tools/engine_status.py",
                  ".engine/tools/memory/consolidate.py", ".engine/tools/telemetry.py",
                  ".engine/tools/self_map.py", ".engine/tools/scent.py",
                  ".engine/tools/audit_digest.py", "README.md", "src/app.py"):
            self.assertFalse(weakening_guard.is_guardrail(p, derived_scripts=derived), p)

    def test_check_script_is_guarded_by_presence_only(self):
        # A check rule's enforcement script is guarded BY PRESENCE in the derived set — and NOT when absent.
        p = ".engine/tools/product_design/lock_integrity.py"
        self.assertTrue(weakening_guard.is_guardrail(p, derived_scripts=frozenset({p})))
        self.assertFalse(weakening_guard.is_guardrail(p, derived_scripts=frozenset()))

    def test_blanket_tools_prefix_dropped_but_fail_safe_restores_it(self):
        # The blanket .engine/tools/ prefix is GONE from the static roster (a non-gate tool is guarded only if the
        # derived set names it), but the None fail-safe sentinel restores whole-dir coverage.
        self.assertFalse(weakening_guard.is_guardrail(".engine/tools/boot.py", derived_scripts=frozenset()))
        self.assertTrue(weakening_guard.is_guardrail(".engine/tools/boot.py", derived_scripts=None))


def _write_check_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


class TestWeakeningDerivedSet(unittest.TestCase):
    """The derived-by-presence clause (D-268/§14) + its ALL-OR-NOTHING fail-safe. The derivation reads the base
    check dir on disk; these tests inject a temp dir the way TestWeakeningReHome monkeypatches _read_base_home."""

    def test_derives_params_script_from_check_jsons(self):
        with tempfile.TemporaryDirectory() as d:
            _write_check_json(os.path.join(d, "a.json"),
                              {"kind": "custom/script", "params": {"script": ".engine/tools/a.py"}})
            _write_check_json(os.path.join(d, "b.json"), {"kind": "presence"})  # no params.script -> skipped
            _write_check_json(os.path.join(d, "c.json"),
                              {"kind": "custom/script", "params": {"script": ".engine/tools/sub/c.py"}})
            self.assertEqual(weakening_guard._derive_check_scripts(d),
                             {".engine/tools/a.py", ".engine/tools/sub/c.py"})

    def test_one_malformed_json_collapses_whole_derivation_to_none(self):
        # ALL-OR-NOTHING: a single corrupt rule returns None (the fail-safe sentinel), NEVER a partial set that
        # would silently drop the broken rule's own script from the guarded set (the fail-open D-268 rejects).
        with tempfile.TemporaryDirectory() as d:
            _write_check_json(os.path.join(d, "good.json"), {"params": {"script": ".engine/tools/good.py"}})
            with open(os.path.join(d, "bad.json"), "w", encoding="utf-8") as fh:
                fh.write("{ not valid json")
            self.assertIsNone(weakening_guard._derive_check_scripts(d))

    def test_missing_check_dir_is_the_fail_safe_sentinel(self):
        self.assertIsNone(weakening_guard._derive_check_scripts(os.path.join(tempfile.gettempdir(), "no-such-check")))

    def test_fail_safe_none_guards_the_whole_tools_dir(self):
        # When derivation fails, is_guardrail falls back to the blanket .engine/tools/ — never drops a gate.
        for p in (".engine/tools/anything.py", ".engine/tools/sub/deep/x.py", ".engine/tools/hook-runner.sh"):
            self.assertTrue(weakening_guard.is_guardrail(p, derived_scripts=None), p)
        self.assertFalse(weakening_guard.is_guardrail("README.md", derived_scripts=None))

    def test_live_check_dir_covers_real_scattered_enforcement_scripts(self):
        # Against the REAL base .engine/check dir: representative scattered enforcement scripts (a top-level guard
        # and two subpackage check-scripts) are guarded by presence, proving the derivation reaches the subpackages.
        derived = weakening_guard._derive_check_scripts()
        self.assertIsNotNone(derived)
        for p in (".engine/tools/protection_guard.py",
                  ".engine/tools/product_design/lock_integrity.py",
                  ".engine/tools/dependency_discipline/pinning.py"):
            self.assertIn(p, derived, p)


class TestEnforcementHookGuardCoverage(unittest.TestCase):
    """Drift detector (D-268 / issue #250): every hook wired on a block-eligible event (PreToolUse / Stop) in
    .claude/settings.json whose handler CAN emit a merge-relevant block MUST be guarded (in the weakening_guard
    floor). A NEW block-capable hook wired without being floored fails this test, converting the silent fail-open
    (an un-guarded new gate) into a loud CI failure at hook-add time.

    'Can block' is read from the hook's OWN CODE, not a hand-maintained non-gate allowlist (which is exactly what
    let close.py slip in the first draft — it was allowlisted as 'never blocks' when it HARD-BLOCKS the turn).
    hooks.py defines only two block channels: hooks.block (exit 2) and hooks.decide (the structured deny). A hook
    whose source references neither genuinely cannot deny (a pure regenerator / housekeeping) and need not be
    floored; one that references either must be. SessionStart / PostToolUse / PreCompact / UserPromptSubmit hooks
    are not block-eligible events and are out of scope."""

    ENFORCEMENT_EVENTS = ("PreToolUse", "Stop")
    BLOCK_PRIMITIVES = ("hooks.block", "hooks.decide")  # the ONLY two deny channels (hooks.py) — grep-derivable
    # Handler paths are extracted from the hook command strings; this regex encodes the "every engine tool is a
    # .py or .sh" assumption. A future non-py/sh handler (a .js, a binary) would not match — so wiring one is a
    # DELIBERATE decision that must also extend this regex, not a silent coverage gap.
    _SCRIPT_RE = re.compile(r"\.engine/tools/[\w./-]+\.(?:py|sh)")

    def _root(self):
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(weakening_guard.__file__))))

    def _settings(self):
        with open(os.path.join(self._root(), ".claude", "settings.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _can_block(self, script_rel: str) -> bool:
        """True iff the hook handler's committed source references a block channel (so it can deny). Fail-safe:
        an unreadable handler is treated as block-capable (it must then be floored)."""
        try:
            with open(os.path.join(self._root(), script_rel), encoding="utf-8") as fh:
                src = fh.read()
        except OSError:
            return True
        return any(prim in src for prim in self.BLOCK_PRIMITIVES)

    def test_every_block_capable_enforcement_hook_is_floored(self):
        settings = self._settings()
        derived = weakening_guard._derive_check_scripts()
        unguarded = []
        for event in self.ENFORCEMENT_EVENTS:
            for matcher in settings.get("hooks", {}).get(event, []):
                for hook in matcher.get("hooks", []):
                    for script in self._SCRIPT_RE.findall(hook.get("command", "")):
                        if self._can_block(script) and not weakening_guard.is_guardrail(script, derived):
                            unguarded.append((event, script))
        self.assertEqual(unguarded, [],
                         "block-capable enforcement hook(s) wired in settings.json but NOT guarded: "
                         f"{unguarded} — a hook that can emit hooks.block/hooks.decide can turn a gate off; add it "
                         "to _FLOOR_ENFORCEMENT_HOOKS in weakening_guard.py")

    def test_both_block_budget_gates_are_covered(self):
        # Ground the drift detector against the two real block-budget members, so it can't pass vacuously: modes.py
        # (PreToolUse write-gate, denies via hooks.decide) and close.py (Stop disposition gate, denies via
        # hooks.block) are BOTH wired on their events, BOTH block-capable, and BOTH floored.
        settings = self._settings()
        wired = {(ev, s) for ev in self.ENFORCEMENT_EVENTS
                 for m in settings.get("hooks", {}).get(ev, [])
                 for h in m.get("hooks", []) for s in self._SCRIPT_RE.findall(h.get("command", ""))}
        self.assertIn(("PreToolUse", ".engine/tools/modes.py"), wired)
        self.assertIn(("Stop", ".engine/tools/close.py"), wired)
        for gate in (".engine/tools/modes.py", ".engine/tools/close.py"):
            self.assertTrue(self._can_block(gate), gate)
            self.assertTrue(weakening_guard.is_guardrail(gate, frozenset()), gate)


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

    # A long body (over the default budget) plus a recorded per-file override.
    _OVER = "## Decision\n" + "\n".join(["line"] * 12) + "\n## Rationale\ny\n## Status\nz\n"

    _OK_OVERRIDE = {"budget": 99, "why": "a recorded reason"}

    def test_per_file_override_raises_budget(self):
        # The overridden file is over the default 6 but under its own 99 ceiling -> no finding at all.
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "x.md", self._OVER)
            key = os.path.relpath(p, validate.ROOT)
            params = dict(self.PARAMS, length_budget_overrides={key: self._OK_OVERRIDE})
            rule = _rule(kind="shape", tier="hard", target={"path": "x"}, params=params)
            passed, found = _run_kind(validate.kind_shape, rule, [p])
            self.assertTrue(passed)
            self.assertEqual(found, [])  # well-formed body, under its 99 override -> nothing

    def test_override_applies_only_to_its_file(self):
        # An override for a.md must not lift b.md, which still nudges at the default 6.
        with tempfile.TemporaryDirectory() as d:
            a, b = _write(d, "a.md", self._OVER), _write(d, "b.md", self._OVER)
            params = dict(self.PARAMS,
                          length_budget_overrides={os.path.relpath(a, validate.ROOT): self._OK_OVERRIDE})
            rule = _rule(kind="shape", tier="hard", target={"path": "x"}, params=params)
            passed, found = _run_kind(validate.kind_shape, rule, [a, b])
            over = [f for f in found if f["severity"] == "soft" and "over its" in f["message"]]
            self.assertTrue(any("b.md" in f["message"] for f in over))      # default still bites b
            self.assertFalse(any("a.md" in f["message"] for f in over))     # override lifts a

    def test_stale_override_key_is_hard(self):
        # A key naming no targeted operation is the rule's hard tier — a dead grant can't accumulate.
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "x.md", "## Decision\nx\n## Rationale\ny\n## Status\nz\n")  # well-formed, in-budget
            params = dict(self.PARAMS, length_budget_overrides={"no/such/operation.md": self._OK_OVERRIDE})
            rule = _rule(kind="shape", tier="hard", target={"path": "x"}, params=params)
            passed, found = _run_kind(validate.kind_shape, rule, [p])
            self.assertFalse(passed)
            self.assertTrue(any("stale or mistyped key" in f["message"]
                                and f["severity"] == "hard" for f in found))

    def test_override_without_recorded_reason_is_hard(self):
        # An override must carry both an integer budget AND a recorded why (#273, made mechanical).
        # A budget-only entry, a non-int budget, and a bare value each fail at the hard tier.
        for bad in ({"budget": 99}, {"budget": "lots", "why": "r"}, 99):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as d:
                p = _write(d, "x.md", "## Decision\nx\n## Rationale\ny\n## Status\nz\n")
                key = os.path.relpath(p, validate.ROOT)
                params = dict(self.PARAMS, length_budget_overrides={key: bad})
                rule = _rule(kind="shape", tier="hard", target={"path": "x"}, params=params)
                passed, found = _run_kind(validate.kind_shape, rule, [p])
                self.assertFalse(passed)
                self.assertTrue(any("is incomplete" in f["message"]
                                    and f["severity"] == "hard" for f in found))


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


class TestRunUnitSeam(unittest.TestCase):
    """run_unit (#286, D-256…D-260): drive ONE real check-logic unit against a
    caller-substituted target so the negative-fixture meta-check can witness that each
    hard check actually bites. Assertions are by SET-MEMBERSHIP (a finding with the
    expected severity/text is present) — never order or count. The production
    run()/run_check()/--check paths never call run_unit and are covered unchanged elsewhere."""

    def test_drives_a_kind_callable_against_a_substituted_path(self):
        # schema kind: a transient rule + target.path point the REAL kind_schema at a
        # fixture file under (a substituted) ROOT. A bad file bites; a good one passes —
        # so the seam runs the real callable, not a reimplementation.
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "schema.json"), "w", encoding="utf-8") as fh:
                json.dump({"type": "object", "required": ["x"],
                           "additionalProperties": False, "properties": {"x": {"type": "integer"}}}, fh)
            with open(os.path.join(d, "bad.json"), "w", encoding="utf-8") as fh:
                json.dump({"y": 1}, fh)          # missing required "x"
            with open(os.path.join(d, "good.json"), "w", encoding="utf-8") as fh:
                json.dump({"x": 1}, fh)
            unit = _rule(kind="schema", params={"schema": "schema.json"})
            orig = validate.ROOT
            validate.ROOT = d
            try:
                bad_pass, bad_found = validate.run_unit(unit, {"path": "bad.json"}, {})
                ok_pass, ok_found = validate.run_unit(unit, {"path": "good.json"}, {})
            finally:
                validate.ROOT = orig
        self.assertFalse(bad_pass)
        self.assertTrue(any(f["severity"] == "hard" for f in bad_found))
        self.assertTrue(ok_pass)
        self.assertFalse(any(f["severity"] == "hard" for f in ok_found))

    def test_coverage_override_aims_the_real_callable_at_a_mini_tree(self):
        # The named coverage override: run_unit substitutes BOTH the catalog source and the
        # walk root via ctx, so the REAL kind_coverage (catalog mode) bites a seeded mini-tree
        # — the same entry point CI runs, not the pure helper.
        with tempfile.TemporaryDirectory() as tree, tempfile.TemporaryDirectory() as cd:
            os.makedirs(os.path.join(tree, ".engine", "orphan"))   # present, unclaimed -> orphan
            catalog = os.path.join(cd, "catalog.json")
            with open(catalog, "w", encoding="utf-8") as fh:
                json.dump({"surfaces": {"alpha": {"location": ".engine/alpha/"}}}, fh)  # claimed, absent
            unit = _rule(kind="coverage", params={"mode": "catalog"}, message="map drift")
            passed, found = validate.run_unit(
                unit, {"coverage_catalog": catalog, "coverage_root": tree}, {})
        msgs = " ".join(f["message"] for f in found)
        self.assertFalse(passed)
        self.assertIn("orphan", msgs)   # the present-but-unclaimed directory
        self.assertIn("alpha", msgs)    # the catalogued-but-absent surface

    def test_drives_the_coherence_callable_against_substituted_manifests(self):
        # The manifests class: a substituted manifest set drives the REAL kind_coherence —
        # a manifest depending on an uninstalled module bites; a satisfiable set passes.
        unit = _rule(kind="coherence", message="modules inconsistent")
        bad = [{"id": "a", "version": "1.0.0", "depends": {"b": ""}}]   # b not installed
        good = [{"id": "a", "version": "1.0.0", "depends": {"b": ">=1.0.0"}},
                {"id": "b", "version": "1.5.0", "depends": {}}]
        bad_pass, bad_found = validate.run_unit(unit, {"manifests": bad}, {})
        ok_pass, ok_found = validate.run_unit(unit, {"manifests": good}, {})
        self.assertFalse(bad_pass)
        self.assertTrue(any(f["severity"] == "hard" for f in bad_found))
        self.assertTrue(ok_pass)
        self.assertEqual(ok_found, [])

    def test_custom_script_substitutes_through_env_and_restores(self):
        # A custom/script reads its target from the environment; run_unit sets the env var
        # around the child and ALWAYS restores os.environ afterward (whether the key was
        # absent before, or held a prior value).
        body = ("import os, json; print(json.dumps([{'severity': 'hard', 'location': None, "
                "'message': 'SEEDED:' + os.environ.get('ENGINE_PR_BODY_FILE', '<unset>')}]))")
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "s.py"), "w", encoding="utf-8") as fh:
                fh.write(body)
            unit = _rule(kind="custom/script", params={"script": "s.py"})
            orig_root = validate.ROOT
            validate.ROOT = d
            saved = os.environ.get("ENGINE_PR_BODY_FILE")
            os.environ.pop("ENGINE_PR_BODY_FILE", None)   # absent before
            try:
                _, found = validate.run_unit(unit, {"env": {"ENGINE_PR_BODY_FILE": "/seeded/path"}}, {})
                seen_when_absent = "ENGINE_PR_BODY_FILE" in os.environ  # must be restored to absent
                os.environ["ENGINE_PR_BODY_FILE"] = "/prior"           # prior value before a second run
                validate.run_unit(unit, {"env": {"ENGINE_PR_BODY_FILE": "/seeded/path"}}, {})
                restored_prior = os.environ.get("ENGINE_PR_BODY_FILE")
            finally:
                validate.ROOT = orig_root
                os.environ.pop("ENGINE_PR_BODY_FILE", None) if saved is None \
                    else os.environ.__setitem__("ENGINE_PR_BODY_FILE", saved)
        self.assertTrue(any(f["message"] == "SEEDED:/seeded/path" for f in found))  # the child saw it
        self.assertFalse(seen_when_absent)        # restored to absent
        self.assertEqual(restored_prior, "/prior")  # restored to the prior value

    def test_unregistered_kind_fails_closed(self):
        passed, found = validate.run_unit(_rule(kind="no-such-kind"), {}, {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "unregistered" in f["message"]
                            for f in found))

    def test_does_not_mutate_the_caller_rule_or_ctx(self):
        # run_unit overlays a substitution onto private copies; the caller's unit and ctx
        # are left exactly as passed (so a roster loop can reuse them).
        unit = _rule(kind="coherence", target={"path": "orig"})
        ctx = {"manifests": []}
        validate.run_unit(unit, {"path": "swapped", "manifests": [{"id": "x"}]}, ctx)
        self.assertEqual(unit["target"], {"path": "orig"})
        self.assertEqual(ctx, {"manifests": []})


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
        saved, orig_api = dict(os.environ), protection_guard.get_json
        os.environ.update({"GITHUB_TOKEN": "x", "GITHUB_REPOSITORY": "o/r",
                           "ENGINE_RULE_TIER": "hard"})
        protection_guard.get_json = lambda path, token, **kw: []  # no rules in force -> floor missing
        try:
            rc, out = self._main_json()
        finally:
            os.environ.clear()
            os.environ.update(saved)
            protection_guard.get_json = orig_api
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

    def _main_json(self, event, files, expected=_AUTO, base_home=None):
        """Drive main() with the network seams stubbed: the complete changed-file list, the authoritative
        changed_files count, and the BASE manifest's recorded home (`base_home`, default None = no home
        recorded, so a home in the diff reads as a first recording). `expected` defaults to len(files) (a
        fully-seen PR); pass a larger int to simulate a truncated / over-cap view, or None for the count
        being unavailable."""
        import contextlib
        import io
        if expected is self._AUTO:
            expected = len(files)
        saved = dict(os.environ)
        orig_fetch = weakening_guard.fetch_all_changed_files
        orig_count = weakening_guard.changed_files_total
        orig_home = weakening_guard._read_base_home
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "event.json")
            with open(ep, "w", encoding="utf-8") as fh:
                json.dump(event, fh)
            os.environ.update({"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "x",
                               "ENGINE_RULE_TIER": "hard", "GITHUB_EVENT_PATH": ep})
            weakening_guard.fetch_all_changed_files = lambda repo, number, token: files
            weakening_guard.changed_files_total = lambda repo, number, token: expected
            weakening_guard._read_base_home = lambda: base_home
            try:
                with contextlib.redirect_stdout(buf):
                    rc = weakening_guard.main()
            finally:
                os.environ.clear()
                os.environ.update(saved)
                weakening_guard.fetch_all_changed_files = orig_fetch
                weakening_guard.changed_files_total = orig_count
                weakening_guard._read_base_home = orig_home
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

    # ---- the engine's update-home repoint is a §15 weakening (content-aware; #367, D-281/D-282) ----
    _REPOINT_PATCH = ('@@ -1,4 +1,4 @@\n'
                      '   "identity": "solo",\n'
                      '-  "home_repository": "acme/engine-home"\n'
                      '+  "home_repository": "evil/look-alike"\n'
                      ' }\n')

    def test_home_repoint_is_flagged_as_weakening(self):
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": self._REPOINT_PATCH}],
            base_home="acme/engine-home")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])
        self.assertIn("update home", out[0]["message"])
        self.assertIn("acme/engine-home", out[0]["message"])   # names old -> new so the operator sees the redirect
        self.assertIn("evil/look-alike", out[0]["message"])
        self.assertIn("guardrail-ack", out[0]["message"])

    def test_home_repoint_is_cleared_by_the_ack(self):
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": [{"name": "guardrail-ack"}]}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": self._REPOINT_PATCH}],
            base_home="acme/engine-home")
        self.assertEqual(out, [])

    def test_home_first_recording_is_not_a_repoint(self):
        # No home in the base (base_home=None) -> the seed/back-fill add is a first recording, not a redirect,
        # and must not demand an ack.
        patch = ('@@ -1,3 +1,4 @@\n'
                 '   "identity": "solo"\n'
                 '+  ,"home_repository": "acme/engine-home"\n'
                 ' }\n')
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": patch}])   # base_home None
        self.assertEqual(out, [])

    def test_home_version_bump_is_not_a_repoint(self):
        # .engine/engine.json legitimately churns on every upgrade (version bumps) with NO home line in the
        # patch — so even with a home recorded it must NOT be flagged (why it is not whole-file guarded).
        patch = ('@@ -1,3 +1,3 @@\n'
                 '-  "engine_release": "1.0.0",\n'
                 '+  "engine_release": "1.1.0",\n'
                 '   "identity": "solo"\n')
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": patch}],
            base_home="acme/engine-home")
        self.assertEqual(out, [])

    def test_home_duplicate_key_injection_is_flagged(self):
        # #367 security review: a repoint hidden by ADDING a second home_repository key (JSON's last value
        # wins) shows only a '+' line — the old line-pair match missed it. The fail-closed detector flags any
        # touch of the home key when a home is already recorded.
        dup = ('@@ -1,3 +1,4 @@\n'
               '   "home_repository": "acme/engine-home",\n'          # the original stays as context
               '+  "home_repository": "evil/look-alike",\n'          # a second key, last-wins -> effective home
               '   "identity": "solo"\n')
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": dup}],
            base_home="acme/engine-home")
        self.assertEqual(len(out), 1)
        self.assertIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])
        self.assertIn("update home", out[0]["message"])

    def test_home_change_with_no_patch_fails_closed(self):
        # #367 security review: GitHub elides the patch on a large PR. A manifest change we cannot inspect,
        # with a home recorded, fails CLOSED (demands the ack) rather than passing silently.
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified"}],   # no 'patch' field
            base_home="acme/engine-home")
        self.assertEqual(len(out), 1)
        self.assertIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])
        self.assertIn("guardrail-ack", out[0]["message"])

    def test_home_repoint_pure_classifier_reads_only_the_manifest(self):
        files = [{"filename": ".engine/engine.json", "status": "modified", "patch": self._REPOINT_PATCH}]
        self.assertEqual(weakening_guard.home_repoint(files, "acme/engine-home"),
                         ("acme/engine-home", "evil/look-alike"))
        # no base home recorded -> a first recording, never a repoint
        self.assertIsNone(weakening_guard.home_repoint(files, None))
        # the same patch on a NON-manifest file is ignored — only the manifest carries the home coordinate
        self.assertIsNone(weakening_guard.home_repoint(
            [{"filename": "docs/x.md", "status": "modified", "patch": self._REPOINT_PATCH}], "acme/engine-home"))

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
            weakening_guard.next_link(header),
            "https://api.github.com/repositories/1/pulls/1/files?per_page=100&page=2")
        # only rel="last" (the last page) -> no next; and no header at all -> no next
        self.assertIsNone(weakening_guard.next_link(
            '<https://api.github.com/repositories/1/pulls/1/files?page=9>; rel="last"'))
        self.assertIsNone(weakening_guard.next_link(None))

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
        orig = weakening_guard.get_page
        weakening_guard.get_page = lambda url, token, **kw: pages[url]
        try:
            got = weakening_guard.fetch_all_changed_files("o/r", 1, "x")
        finally:
            weakening_guard.get_page = orig
        self.assertEqual(len(got), 101)  # both pages, not just the first 100
        self.assertEqual(got[-1]["filename"], ".engine/check/pr-body-completeness.json")

    def test_an_off_host_pagination_link_fails_the_fetch_closed(self):
        """End-to-end §15: a crafted off-host Link header reaching the REAL get_page must raise (the
        off-host guard now homed in github_client.request), so fetch_all_changed_files fails closed
        rather than following the link off-host. Only the network boundary (github_client._urlopen) is
        faked, so the guard is driven through weakening_guard's highest-stakes caller, not in isolation."""
        import github_client

        class _Resp:
            def __init__(self, body, link):
                self._b = json.dumps(body).encode("utf-8")
                self.headers = {"Link": link}

            def read(self):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        orig = github_client._urlopen
        github_client._urlopen = lambda req, timeout=None: _Resp(
            [{"filename": "docs/a.md", "status": "modified"}],
            '<https://evil.example/repos/o/r/pulls/1/files?page=2>; rel="next"')
        try:
            with self.assertRaises(ValueError):
                weakening_guard.fetch_all_changed_files("o/r", 1, "x")
        finally:
            github_client._urlopen = orig

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
