#!/usr/bin/env python3
"""Self-tests for the shared issue-authoring helper.

Run: uv run --directory .engine --frozen -- python tools/selftest.py

Each test locks one law of the control-plane engine-authored-issue body contract: the two required
parts cannot be omitted (TypeError at the call boundary — the by-construction enforcement) nor left
blank (ValueError); the assembled body carries the fixed plainness floor plus both parts under plain
headings; backstage references render as plain markdown links and a bare id (no label/url) is refused
(never a bare id dump), while the references part stays optional; and telemetry — the first in-repo
producer — authors its body THROUGH the helper (the single issue-authoring path, no second route).
The deliverable-gate cold review attests each test's assertion matches its name.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_author  # noqa: E402
import issue_kind     # noqa: E402
import telemetry      # noqa: E402


class TestRequiredParts(unittest.TestCase):
    def test_omitting_a_required_part_raises_typeerror(self):
        # The "a call omitting a part cannot run" enforcement: keyword-only, no default.
        with self.assertRaises(TypeError):
            issue_author.render_engine_issue_body(what_this_is="only one part")  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            issue_author.render_engine_issue_body(whats_next="only one part")  # type: ignore[call-arg]

    def test_blank_required_part_raises_valueerror(self):
        for blank in ("", "   ", "\n\t"):
            with self.assertRaises(ValueError):
                issue_author.render_engine_issue_body(what_this_is=blank, whats_next="ok")
            with self.assertRaises(ValueError):
                issue_author.render_engine_issue_body(what_this_is="ok", whats_next=blank)

    def test_non_string_part_raises_valueerror(self):
        with self.assertRaises(ValueError):
            issue_author.render_engine_issue_body(what_this_is=None, whats_next="ok")  # type: ignore[arg-type]


class TestBodyShape(unittest.TestCase):
    def test_body_carries_floor_and_both_parts(self):
        body = issue_author.render_engine_issue_body(
            what_this_is="WHAT_IT_IS", whats_next="WHAT_NEXT")
        self.assertIn(issue_author._FRAMING, body)         # the fixed plainness floor (part 1)
        self.assertIn("**What this is.** WHAT_IT_IS", body)
        self.assertIn("**What happens next.** WHAT_NEXT", body)

    def test_references_optional_absent_by_default(self):
        body = issue_author.render_engine_issue_body(what_this_is="a", whats_next="b")
        self.assertNotIn("More detail", body)

    def test_references_render_as_markdown_links(self):
        body = issue_author.render_engine_issue_body(
            what_this_is="a", whats_next="b",
            references=[("The failing run", "https://example.com/run/1"),
                        ("The policy", "https://example.com/policy")])
        self.assertIn("**More detail.**", body)
        self.assertIn("- [The failing run](https://example.com/run/1)", body)
        self.assertIn("- [The policy](https://example.com/policy)", body)

    def test_part_renders_structured_markdown_verbatim(self):
        # Readability guidance is realizable: a producer may shape a part as a one-line summary plus
        # markdown bullets, and the helper renders it verbatim (it never forces structure, but it
        # supports the readable summary->bullets shape rather than only flat prose).
        what = "Summary line.\n\n- first detail\n- second detail"
        body = issue_author.render_engine_issue_body(what_this_is=what, whats_next="b")
        self.assertIn("**What this is.** Summary line.\n\n- first detail\n- second detail", body)


class TestNoBareIdDump(unittest.TestCase):
    def test_reference_without_label_or_url_is_refused(self):
        for bad in (
            [("", "https://example.com")],   # blank label
            [("label only", "")],            # blank url
            [("rule:abc",)],                 # 1-tuple
            ["rule:abc"],                    # bare string (length != 2)
            ["ab"],                          # 2-char string would unpack to ('a','b') — must be refused
            [("a", "b", "c")],               # 3-tuple
            [{"k": "v"}],                    # a non-pair container
        ):
            with self.assertRaises(ValueError):
                issue_author.render_engine_issue_body(what_this_is="a", whats_next="b", references=bad)


class TestImportOrderStaysCycleFree(unittest.TestCase):
    def test_both_import_orders_work_in_a_fresh_interpreter(self):
        # telemetry imports issue_author at module load; issue_author must therefore import telemetry only
        # function-locally (inside render_engine_issue_body). A module-scope `import telemetry` sneaking back
        # into issue_author would crash whichever order loads telemetry first — this pins BOTH orders in
        # fresh interpreters, which the in-process suite (one fixed order) cannot exercise.
        tools = os.path.dirname(os.path.abspath(__file__))
        for order in ("import telemetry, issue_author", "import issue_author, telemetry"):
            proc = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {tools!r}); {order}"],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"{order!r} failed:\n{proc.stderr}")


class TestUrgencyAtFiling(unittest.TestCase):
    def test_default_unrated_leaves_body_unchanged(self):
        # Omitting urgency (the default) must render byte-for-byte what a pre-urgency caller got — no marker.
        without = issue_author.render_engine_issue_body(what_this_is="a", whats_next="b")
        explicit_none = issue_author.render_engine_issue_body(what_this_is="a", whats_next="b", urgency=None)
        self.assertEqual(without, explicit_none)
        self.assertIsNone(telemetry.parse_severity(without))

    def test_each_class_appends_the_marker_last_and_round_trips(self):
        for sev in (telemetry.TRUST_CRITICAL, telemetry.PERSISTENT_BENIGN):
            body = issue_author.render_engine_issue_body(what_this_is="a", whats_next="b", urgency=sev)
            # The marker telemetry writes, recovered by the same reader — appended LAST so a forged prose
            # marker cannot win (parse_severity takes the last match).
            self.assertEqual(telemetry.parse_severity(body), sev)
            self.assertTrue(body.rstrip().endswith(f"<!-- engine-severity: {sev} -->"))

    def test_urgency_outside_the_two_classes_is_refused(self):
        for bad in ("high", "trust_critical", "", "TRUST-CRITICAL"):
            with self.assertRaises(ValueError):
                issue_author.render_engine_issue_body(what_this_is="a", whats_next="b", urgency=bad)


class TestSingleAuthoringPath(unittest.TestCase):
    def test_telemetry_authors_through_the_helper(self):
        # The roadmap's "route producers through it / avoid two issue-authoring paths": telemetry's
        # body must carry the helper's framing floor, proving it is assembled via the one helper.
        rec = {"source_id": "rule:x", "message": "A check could not run.", "severity": "trust-critical"}
        body = telemetry.issue_body(rec, "2026-06-06T00:00:00Z", "2026-06-06T00:00:00Z")
        self.assertIn(issue_author._FRAMING, body)
        # ...and telemetry still appends its own trailers the helper does not own.
        self.assertIn("First noticed", body)
        self.assertEqual(telemetry.parse_source_id(body), "rule:x")

    def test_unpunctuated_message_does_not_run_on(self):
        # The finding sits in its own paragraph, so an operator concern lacking trailing punctuation
        # (e.g. via close.py) cannot collide with the following prose (deliverable-gate regression).
        rec = {"source_id": "rule:z", "message": "validator timing out", "severity": "trust-critical"}
        body = telemetry.issue_body(rec, "2026-06-06T00:00:00Z", "2026-06-06T00:00:00Z")
        self.assertIn("**What it noticed.** validator timing out\n", body)
        self.assertNotIn("validator timing out It", body)


_GOOD = {
    "repository": "StarshipSuperjam/engine-template",
    "kind": "Fix",
    "title": "A finding",
    "what_this_is": "The engine noticed something.",
    "whats_next": "Nothing right now.",
}
_TRUSTED_ENV = {"GITHUB_REPOSITORY": "StarshipSuperjam/engine-template", "GITHUB_TOKEN": "tok"}


class _CapturingIssues:
    """A stand-in for telemetry.GitHubIssues: records the (repo, token) it was built with and the open_issue
    call, and returns a created-Issue dict — so the whole create path runs offline with no network."""

    last = None

    def __init__(self, repo, token):
        self.repo, self.token, self.opened = repo, token, []
        _CapturingIssues.last = self

    def open_issue(self, title, body):
        self.opened.append((title, body))
        return {"html_url": f"https://github.com/{self.repo}/issues/7", "number": 7}


class TestInputLoadingAndValidation(unittest.TestCase):
    def test_load_input_from_stdin_parses_object(self):
        data = issue_author.load_input("-", _stdin=io.StringIO(json.dumps(_GOOD)))
        self.assertEqual(data["title"], "A finding")

    def test_load_input_rejects_non_object_and_bad_json(self):
        with self.assertRaises(issue_author.IssueInputError):
            issue_author.load_input("-", _stdin=io.StringIO("[1, 2, 3]"))    # a JSON array, not an object
        with self.assertRaises(issue_author.IssueInputError):
            issue_author.load_input("-", _stdin=io.StringIO("{not json"))

    def test_load_input_unreadable_path_refused(self):
        with self.assertRaises(issue_author.IssueInputError):
            issue_author.load_input("/no/such/input-xyz.json")

    def test_validate_input_accepts_a_good_input_and_returns_it(self):
        data = dict(_GOOD)
        self.assertIs(issue_author.validate_input(data), data)   # returns the same object unchanged

    def test_validate_input_names_the_first_violation(self):
        bad = {"repository": "o/r", "title": "x", "what_this_is": "y"}   # whats_next missing
        with self.assertRaises(issue_author.IssueInputError) as ctx:
            issue_author.validate_input(bad)
        self.assertIn("engine-issue-input.v1", str(ctx.exception))

    def test_validate_input_rejects_bad_urgency_and_bad_repo(self):
        for bad in ({**_GOOD, "urgency": "high"}, {**_GOOD, "repository": "not-a-slug"}):
            with self.assertRaises(issue_author.IssueInputError):
                issue_author.validate_input(bad)


class TestTrustedTarget(unittest.TestCase):
    def test_github_repository_env_wins(self):
        with mock.patch("checkout_health.recorded_product_build_target", return_value=None):
            self.assertEqual(
                issue_author.resolve_trusted_targets(env={"GITHUB_REPOSITORY": "o/r"}), ["o/r"])

    def test_unresolved_when_no_env_and_no_origin(self):
        # a temp dir with no git origin and no product target resolves to [] (create then refuses — fail closed)
        with mock.patch("checkout_health.recorded_product_build_target", return_value=None):
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(issue_author.resolve_trusted_targets(env={}, root=d), [])

    def test_mechanic_owned_product_is_also_a_trusted_target(self):
        # S2: an engine-mechanic's owned product (committed product_build_target — trusted config) joins the
        # trusted set, so an owned-product engine Issue can reach the product it builds. Not injectable: it
        # comes from the manifest, never from the input.
        with mock.patch("checkout_health.recorded_product_build_target", return_value="acme/product"):
            targets = issue_author.resolve_trusted_targets(env={"GITHUB_REPOSITORY": "acme/mechanic"})
        self.assertEqual(targets, ["acme/mechanic", "acme/product"])


class TestPreviewText(unittest.TestCase):
    def test_match_shows_agreement(self):
        text = issue_author.preview_text(dict(_GOOD), ["StarshipSuperjam/engine-template"])
        self.assertIn("✓", text)
        self.assertIn("nothing has been filed", text)
        self.assertIn("**What this is.** The engine noticed something.", text)

    def test_mismatch_and_unresolved_warn(self):
        self.assertIn("does NOT match", issue_author.preview_text(dict(_GOOD), ["other/repo"]))
        self.assertIn("no trusted target", issue_author.preview_text(dict(_GOOD), []))


class TestCreateIssue(unittest.TestCase):
    def test_files_through_the_trusted_target_and_returns_link(self):
        with mock.patch("checkout_health.recorded_product_build_target", return_value=None):
            link = issue_author.create_issue(dict(_GOOD), env=dict(_TRUSTED_ENV),
                                             issues_factory=_CapturingIssues)
        self.assertEqual(link, "https://github.com/StarshipSuperjam/engine-template/issues/7")
        self.assertEqual(_CapturingIssues.last.repo, "StarshipSuperjam/engine-template")
        self.assertEqual(_CapturingIssues.last.token, "tok")
        title, body = _CapturingIssues.last.opened[0]
        self.assertEqual(title, "Fix: A finding")     # title is rendered from the structured kind, not verbatim
        self.assertIn("<!-- engine-kind: Fix -->", body)   # and the authoritative kind marker is stamped
        self.assertIn(issue_author._FRAMING, body)   # filed body is assembled through the one contract

    def test_files_into_the_matched_owned_product_target(self):
        # S2: filing an engine Issue whose repository is the owned product files INTO the product, not the
        # mechanic's own repo — the input matched a trusted target, so the create path honors it.
        with mock.patch("checkout_health.recorded_product_build_target", return_value="acme/product"):
            issue_author.create_issue({**_GOOD, "repository": "acme/product"},
                                      env={"GITHUB_REPOSITORY": "acme/mechanic", "GITHUB_TOKEN": "tok"},
                                      issues_factory=_CapturingIssues)
        self.assertEqual(_CapturingIssues.last.repo, "acme/product")

    def test_refuses_when_input_repository_matches_no_trusted_target(self):
        with mock.patch("checkout_health.recorded_product_build_target", return_value=None):
            env = {"GITHUB_REPOSITORY": "someone/else", "GITHUB_TOKEN": "tok"}
            with self.assertRaises(issue_author.IssueInputError) as ctx:
                issue_author.create_issue(dict(_GOOD), env=env, issues_factory=_CapturingIssues)
        self.assertIn("trusted target", str(ctx.exception))

    def test_refuses_without_a_token(self):
        env = {"GITHUB_REPOSITORY": "StarshipSuperjam/engine-template"}   # no GITHUB_TOKEN
        with mock.patch("checkout_health.recorded_product_build_target", return_value=None):
            with self.assertRaises(issue_author.IssueInputError):
                issue_author.create_issue(dict(_GOOD), env=env, issues_factory=_CapturingIssues)

    def test_refuses_when_target_unresolvable(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(issue_author.IssueInputError):
                issue_author.create_issue(dict(_GOOD), env={"GITHUB_TOKEN": "tok"},
                                          root=d, issues_factory=_CapturingIssues)


class TestCliDispatch(unittest.TestCase):
    def test_parse_cli_requires_input(self):
        with self.assertRaises(issue_author.IssueInputError):
            issue_author._parse_cli(["--confirm"])
        self.assertEqual(issue_author._parse_cli(["--input", "x", "--confirm"]), ("x", True))
        self.assertEqual(issue_author._parse_cli(["--input=y"]), ("y", False))

    def test_create_without_confirm_refuses_before_any_read(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = issue_author._cli_create("/no/such/path.json", confirm=False)
        self.assertEqual(rc, 2)
        self.assertIn("--confirm", err.getvalue())


class TestVerifiedHeadAtFiling(unittest.TestCase):
    """The verified-head provenance trailer (StarshipSuperjam/engine-template#957): an optional owner/repo@sha
    recorded BEFORE the severity marker, machine-recoverable, fail-closed on a malformed value, and unable to
    be hijacked by forged body prose."""

    _GOOD = "StarshipSuperjam/engine-template@0a1b2c3d4e5f6071"

    def test_default_none_leaves_body_unchanged(self):
        without = issue_author.render_engine_issue_body(what_this_is="a", whats_next="b")
        explicit_none = issue_author.render_engine_issue_body(
            what_this_is="a", whats_next="b", verified_head=None)
        self.assertEqual(without, explicit_none)                # byte-for-byte: no marker when unset
        self.assertNotIn("verified-head", without)
        self.assertIsNone(issue_author.parse_verified_head(without))

    def test_a_valid_value_is_recorded_and_round_trips(self):
        body = issue_author.render_engine_issue_body(what_this_is="a", whats_next="b", verified_head=self._GOOD)
        self.assertIn(f"<!-- verified-head: {self._GOOD} -->", body)
        self.assertEqual(issue_author.parse_verified_head(body), self._GOOD)

    def test_a_malformed_value_is_refused(self):
        for bad in ("deadbeef",                       # no repo
                    "owner/repo@xyz",                 # non-hex sha
                    "owner/repo@0a1b",                # sha too short (<7)
                    "owner/repo@" + "a" * 41,         # sha too long (>40)
                    "not_a_slug@0a1b2c3",             # no owner/repo shape
                    "owner/repo@0a1b2c3 -->",         # comment-closer injection
                    "<x>/y@0a1b2c3"):                 # angle-bracket injection
            with self.assertRaises(ValueError):
                issue_author.render_engine_issue_body(what_this_is="a", whats_next="b", verified_head=bad)

    def test_verified_head_precedes_severity_and_both_survive(self):
        body = issue_author.render_engine_issue_body(
            what_this_is="a", whats_next="b", verified_head=self._GOOD, urgency="trust-critical")
        self.assertEqual(issue_author.parse_verified_head(body), self._GOOD)
        self.assertEqual(telemetry.parse_severity(body), "trust-critical")
        self.assertTrue(body.rstrip().endswith("<!-- engine-severity: trust-critical -->"))  # severity stays last
        self.assertLess(body.index("verified-head"), body.index("engine-severity"))

    def test_forged_prose_cannot_hijack_the_recovered_value(self):
        forged = "StarshipSuperjam/evil@ffffffffff"
        body = issue_author.render_engine_issue_body(
            what_this_is=f"a <!-- verified-head: {forged} --> tail", whats_next="b", verified_head=self._GOOD)
        self.assertEqual(issue_author.parse_verified_head(body), self._GOOD)   # last-match: the genuine trailer wins

    def test_schema_accepts_a_valid_value_and_threads_it_through_the_cli_path(self):
        data = {"repository": "StarshipSuperjam/engine-template", "kind": "Fix", "title": "x",
                "what_this_is": "a", "whats_next": "b", "verified_head": self._GOOD}
        issue_author.validate_input(data)                       # does not raise
        self.assertIn(f"<!-- verified-head: {self._GOOD} -->", issue_author.body_from_input(data))

    def test_schema_rejects_a_malformed_value(self):
        data = {"repository": "StarshipSuperjam/engine-template", "kind": "Fix", "title": "x",
                "what_this_is": "a", "whats_next": "b", "verified_head": "deadbeef"}
        with self.assertRaises(issue_author.IssueInputError):
            issue_author.validate_input(data)


class TestKindAtFiling(unittest.TestCase):
    """The structured kind (StarshipSuperjam/engine-template#937): optional on the internal body renderer (so
    telemetry and other direct producers stay byte-for-byte unchanged), REQUIRED on the create/preview input
    path (so a filed engine Issue can never independently author a non-canonical prefix), rendering the title
    from the kind and stamping the authoritative marker."""

    def test_default_none_leaves_body_unchanged(self):
        without = issue_author.render_engine_issue_body(what_this_is="a", whats_next="b")
        explicit_none = issue_author.render_engine_issue_body(what_this_is="a", whats_next="b", kind=None)
        self.assertEqual(without, explicit_none)          # byte-for-byte: no marker when unset
        self.assertNotIn("engine-kind", without)

    def test_kind_stamps_the_marker_and_round_trips(self):
        for k in issue_kind.KINDS:
            body = issue_author.render_engine_issue_body(what_this_is="a", whats_next="b", kind=k)
            self.assertIn(f"<!-- engine-kind: {k} -->", body)
            self.assertEqual(issue_kind.parse_kind(body), k)

    def test_non_canonical_kind_is_refused_at_render(self):
        for bad in ("Bug", "Engine fault", "Architecture", ""):
            with self.assertRaises(ValueError):
                issue_author.render_engine_issue_body(what_this_is="a", whats_next="b", kind=bad)

    def test_kind_precedes_severity_when_both_present(self):
        body = issue_author.render_engine_issue_body(
            what_this_is="a", whats_next="b", kind="Fix", urgency="trust-critical")
        self.assertLess(body.index("engine-kind"), body.index("engine-severity"))  # severity remains last
        self.assertTrue(body.rstrip().endswith("<!-- engine-severity: trust-critical -->"))

    def test_title_is_rendered_from_kind_not_verbatim(self):
        self.assertEqual(issue_author.title_from_input(dict(_GOOD)), "Fix: A finding")
        # a prefix mistyped into the descriptive title is normalised away (never `Fix: Fix: …`):
        self.assertEqual(issue_author.title_from_input({**_GOOD, "title": "Fix: A finding"}), "Fix: A finding")

    def test_body_from_input_stamps_the_marker(self):
        self.assertIn("<!-- engine-kind: Fix -->", issue_author.body_from_input(dict(_GOOD)))

    def test_schema_requires_kind_and_rejects_non_canonical(self):
        no_kind = {k: v for k, v in _GOOD.items() if k != "kind"}
        with self.assertRaises(issue_author.IssueInputError):
            issue_author.validate_input(no_kind)                 # kind is now required
        with self.assertRaises(issue_author.IssueInputError):
            issue_author.validate_input({**_GOOD, "kind": "Bug"})   # not one of the six

    def test_schema_enum_mirrors_the_single_source(self):
        # The JSON enum is a CLI-boundary gate that must stay equal to issue_kind.KINDS (the source of truth),
        # exactly as urgency mirrors telemetry's severity classes.
        with open(issue_author._INPUT_SCHEMA_REL, "r", encoding="utf-8") as fh:
            schema = json.load(fh)
        self.assertEqual(tuple(schema["properties"]["kind"]["enum"]), issue_kind.KINDS)
        self.assertIn("kind", schema["required"])

    def test_preview_shows_the_rendered_title(self):
        text = issue_author.preview_text(dict(_GOOD), ["StarshipSuperjam/engine-template"])
        self.assertIn("Fix: A finding", text)


if __name__ == "__main__":
    unittest.main()
