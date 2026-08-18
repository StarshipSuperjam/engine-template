"""Tests for issue_kind_label — the on:issues applicator that keeps the GitHub-native kind labels consistent.

These lock the load-bearing behaviours: that the workflow is an engine-owned traveler (FOUNDATION_INFRA →
CODEOWNERS + upgrade overlay, the same treatment as the other engine workflows); that the title→native-label
derivation is exactly the intended mapping and refuses to guess on an unmappable title; that the applicator is
apply-only (it SKIPS a native label the repo owner deleted, never minting one); that it is idempotent (no
redundant add when the label is already present) and orthogonal to the `engine` label (it acts on ANY issue);
and that out-of-scope / unactionable inputs no-op while a genuine API failure surfaces (the safety-net fail
contract). The label value is a fixed enum, never raw title text.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate                 # noqa: E402
import module_coherence         # noqa: E402
import module_manager           # noqa: E402
import issue_gate               # noqa: E402  (the single source for the `engine` label string)
import issue_kind               # noqa: E402  (the canonical vocabulary + marker the reconciler acts on)
import issue_label_client       # noqa: E402
import issue_kind_label as k    # noqa: E402
import quiet_call               # noqa: E402  (capture a CLI walkthrough's stdout so it can't bury the suite summary)

WORKFLOW_REL = ".github/workflows/engine-issue-kind-label.yml"
_ENGINE = [{"name": issue_gate.ENGINE_LABEL}]


class TestWorkflowIsEngineOwnedTraveler(unittest.TestCase):
    """The workflow is a FOUNDATION_INFRA member, so it travels on upgrade (FOUNDATION_CODE) and is owned in
    CODEOWNERS (foundation_infra_paths) — the same treatment as the other engine workflows. No generic check
    catches an omission here, so these assertions ARE the guard."""

    def test_workflow_is_present_in_the_tree(self):
        self.assertTrue(os.path.isfile(os.path.join(validate.ROOT, WORKFLOW_REL)),
                        f"{WORKFLOW_REL} must exist")

    def test_is_a_foundation_infra_member(self):
        self.assertIn(WORKFLOW_REL, module_coherence.FOUNDATION_INFRA)

    def test_travels_on_upgrade_via_foundation_code(self):
        self.assertIn(WORKFLOW_REL, module_manager.FOUNDATION_CODE)

    def test_renders_into_codeowners_via_foundation_infra_paths(self):
        owned = module_coherence.foundation_infra_paths()
        self.assertIn(WORKFLOW_REL, owned)
        self.assertFalse(any("*" in p for p in owned), "paths are concrete, never bare globs")


class TestKindDerivation(unittest.TestCase):
    """native_label_for_title is the single source both the applicator and any one-time backfill call."""

    def test_each_kind_maps_to_its_native_label(self):
        cases = {
            "Bug: broke": "bug", "Fix: it": "bug", "Engine fault: x": "bug", "Defect: y": "bug",
            "Security: z": "bug", "Feature: new": "enhancement", "Improvement: better": "enhancement",
            "Docs: note": "documentation", "Documentation: full": "documentation", "Question: ?": "question",
        }
        for title, expected in cases.items():
            self.assertEqual(k.native_label_for_title(title), expected, title)

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(k.native_label_for_title("fix: lower"), "bug")
        self.assertEqual(k.native_label_for_title("  Feature: leading space"), "enhancement")
        self.assertEqual(k.native_label_for_title("Engine fault : spaced colon"), "bug")

    def test_unmappable_titles_get_no_label_never_a_guess(self):
        for title in ("Maintenance: upkeep", "Migration M3: rename", "Delivery wave 2", "no prefix", "", None,
                      "Provisioning: detect", "Log the decision", "documentationless prose"):
            self.assertIsNone(k.native_label_for_title(title), repr(title))

    def test_docs_prefix_does_not_shadow_documentation(self):
        self.assertEqual(k.native_label_for_title("Documentation: x"), "documentation")

    def test_the_value_range_is_only_the_four_github_natives(self):
        self.assertEqual(set(k.NATIVE_KIND_LABELS), {"bug", "enhancement", "documentation", "question"})


class TestApplyIsApplyOnlyAndIdempotent(unittest.TestCase):
    def _client(self, gh):
        return issue_label_client.IssueLabelClient("o/r", "t", user_agent=k.USER_AGENT, transport=gh)

    def test_mappable_title_present_on_repo_absent_on_issue_gets_one_add(self):
        gh = k._FakeGitHub(label_exists=True)
        action = k.apply_kind_label({"number": 1, "title": "Fix: x", "labels": []}, self._client(gh))
        self.assertEqual(action, "labelled")
        self.assertEqual(len(gh.issue_label_adds()), 1)

    def test_repo_absent_native_label_is_skipped_never_minted(self):
        gh = k._FakeGitHub(label_exists=False)
        action = k.apply_kind_label({"number": 2, "title": "Feature: x", "labels": []}, self._client(gh))
        self.assertEqual(action, "absent")
        self.assertEqual(gh.issue_label_adds(), [])
        # no POST to /repos/o/r/labels (label creation) ever happened
        self.assertFalse(any(m == "POST" and p.endswith("/repos/o/r/labels") for m, p, _ in gh.calls))

    def test_label_already_present_is_a_no_op(self):
        gh = k._FakeGitHub(label_exists=True)
        action = k.apply_kind_label(
            {"number": 3, "title": "Improvement: x", "labels": [{"name": "enhancement"}]}, self._client(gh))
        self.assertEqual(action, "already")
        self.assertEqual(gh.issue_label_adds(), [])

    def test_unmappable_title_makes_no_github_calls(self):
        gh = k._FakeGitHub()
        action = k.apply_kind_label({"number": 4, "title": "Maintenance: x", "labels": []}, self._client(gh))
        self.assertEqual(action, "no-kind")
        self.assertEqual(gh.calls, [])

    def test_acts_on_any_issue_not_only_engine_labelled(self):
        # The kind axis is orthogonal to the engine label — a non-engine issue is still labelled.
        gh = k._FakeGitHub(label_exists=True)
        action = k.apply_kind_label(
            {"number": 5, "title": "Bug: x", "labels": [{"name": "some-product-label"}]}, self._client(gh))
        self.assertEqual(action, "labelled")

    def test_api_failure_surfaces_as_degraded_write(self):
        def boom(method, path, body=None):
            if "/labels/" in path:      # the label-exists GET fails hard
                return 500, None
            return 200, None
        with self.assertRaises(issue_label_client.DegradedWriteError):
            k.apply_kind_label({"number": 6, "title": "Fix: x", "labels": []}, self._client(boom))

    def test_http_wraps_urlerror_as_degraded_write(self):
        # The REAL _http (not an injected transport) must still map an unreachable host to DegradedWriteError
        # after the shared transport moved into github_client.json_request (#907) — write-fail preserved.
        from unittest import mock
        import urllib.error
        import github_client
        client = issue_label_client.IssueLabelClient("o/r", "tok", user_agent="ua")
        with mock.patch.object(github_client, "_urlopen", side_effect=urllib.error.URLError("down")):
            with self.assertRaises(issue_label_client.DegradedWriteError):
                client._http("POST", "/repos/o/r/issues/1/labels", {"labels": ["x"]})


class TestRunFailContract(unittest.TestCase):
    """_run reads the event from $GITHUB_EVENT_PATH and applies the safety-net-not-a-gate fail contract
    (mirroring the conformance net's TestRunFailContract): no/partial/malformed event or an unmappable
    title → quiet exit 0; a mappable title with no token → exit 1 (the net's own breakage is visible).
    These paths reach no network."""

    def _env(self, **overrides):
        keys = ("GITHUB_EVENT_PATH", "GITHUB_TOKEN", "GITHUB_REPOSITORY")
        saved = {kk: os.environ.get(kk) for kk in keys}

        def restore():
            for kk, v in saved.items():
                if v is None:
                    os.environ.pop(kk, None)
                else:
                    os.environ[kk] = v
        self.addCleanup(restore)
        for kk in keys:
            os.environ.pop(kk, None)
        for kk, v in overrides.items():
            if v is not None:
                os.environ[kk] = v

    def _event_file(self, event) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        self.addCleanup(os.remove, path)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if isinstance(event, str):
                fh.write(event)            # raw text — the malformed-JSON case
            else:
                json.dump(event, fh)
        return path

    def test_no_event_exits_zero(self):
        self._env()  # GITHUB_EVENT_PATH unset
        self.assertEqual(quiet_call.run(k.main, []), 0)

    def test_malformed_event_json_exits_zero(self):
        path = self._event_file("{not json at all")
        self._env(GITHUB_EVENT_PATH=path, GITHUB_TOKEN="tok", GITHUB_REPOSITORY="o/r")
        self.assertEqual(quiet_call.run(k.main, []), 0)

    def test_partial_event_exits_zero(self):
        path = self._event_file({"issue": None})
        self._env(GITHUB_EVENT_PATH=path, GITHUB_TOKEN="tok", GITHUB_REPOSITORY="o/r")
        self.assertEqual(quiet_call.run(k.main, []), 0)

    def test_unmappable_title_exits_zero_without_network(self):
        # unmappable → no-op BEFORE the env check, so exit 0 even with no token (and no client built).
        path = self._event_file({"number": 1, "issue": {"number": 1, "title": "Delivery wave 2", "labels": []}})
        self._env(GITHUB_EVENT_PATH=path)
        self.assertEqual(quiet_call.run(k.main, []), 0)

    def test_mappable_title_without_token_exits_one(self):
        path = self._event_file({"issue": {"number": 1, "title": "Fix: broken thing", "labels": []}})
        self._env(GITHUB_EVENT_PATH=path)  # mappable but no token/repo → visible failure
        self.assertEqual(quiet_call.run(k.main, []), 1)

    def test_engine_marker_already_canonical_no_native_label_exits_zero_without_token(self):
        # An engine issue whose title already matches its marker AND whose kind (Maintenance) projects no native
        # label is a TRUE no-op — quiet exit 0 even with no token, since reconcile_title would make zero calls.
        path = self._event_file({"issue": {"number": 1, "title": "Maintenance: upkeep",
                                            "labels": [{"name": issue_gate.ENGINE_LABEL}],
                                            "body": issue_kind.kind_trailer("Maintenance")}})
        self._env(GITHUB_EVENT_PATH=path)  # no token, but nothing to do → exit 0
        self.assertEqual(quiet_call.run(k.main, []), 0)

    def test_engine_marker_drifted_title_without_token_exits_one(self):
        # ...but a drifted title (a real repair pending) with no token is a visible red — the net's own breakage.
        path = self._event_file({"issue": {"number": 1, "title": "Architecture: drift",
                                            "labels": [{"name": issue_gate.ENGINE_LABEL}],
                                            "body": issue_kind.kind_trailer("Improvement")}})
        self._env(GITHUB_EVENT_PATH=path)
        self.assertEqual(quiet_call.run(k.main, []), 1)


class TestImportLayering(unittest.TestCase):
    def test_hot_path_import_stays_lean(self):
        # The reconciler runs per issue event; importing it must never drag the module-manager/release_cut/
        # telemetry stack in — not directly, and NOT transitively through one of its imports. Check sys.modules
        # in a FRESH interpreter: the in-process suite has already loaded these modules for other tests, so an
        # in-process check would be a false green, and the old __dict__ check missed a transitive import.
        tools = os.path.dirname(os.path.abspath(__file__))
        heavy = ("release_cut", "module_manager", "module_coherence", "telemetry")
        code = (f"import sys; sys.path.insert(0, {tools!r}); import issue_kind_label; "
                f"print(','.join(m for m in {heavy!r} if m in sys.modules))")
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "",
                         f"issue_kind_label transitively imported a heavy module: {proc.stdout.strip()}")


class TestEngineKindGate(unittest.TestCase):
    """engine_kind_or_none is the double gate on the title-write path: engine label AND a valid marker."""

    def test_engine_label_and_valid_marker_yields_the_kind(self):
        self.assertEqual(k.engine_kind_or_none({"labels": _ENGINE, "body": issue_kind.kind_trailer("Fix")}), "Fix")

    def test_missing_engine_label_is_none_even_with_a_marker(self):
        forged = {"labels": [{"name": "bug"}], "body": issue_kind.kind_trailer("Security")}
        self.assertIsNone(k.engine_kind_or_none(forged))   # an external user cannot self-apply `engine`

    def test_missing_or_garbled_marker_is_none(self):
        self.assertIsNone(k.engine_kind_or_none({"labels": _ENGINE, "body": "no marker"}))
        self.assertIsNone(k.engine_kind_or_none({"labels": _ENGINE, "body": "<!-- engine-kind: bogus -->"}))
        self.assertIsNone(k.engine_kind_or_none("not a dict"))


class TestNativeLabelForIssue(unittest.TestCase):
    """The two-projection native label: canonical projection for an engine+marker issue, legacy title-parse
    fallback for a human/pre-marker issue (so a pre-marker engine issue's label does not regress)."""

    def test_engine_marker_projects_from_the_kind_ignoring_a_drifted_title(self):
        issue = {"title": "Architecture: drift", "labels": _ENGINE, "body": issue_kind.kind_trailer("Fix")}
        self.assertEqual(k.native_label_for_issue(issue), "bug")

    def test_pre_marker_engine_issue_falls_back_to_title_parse(self):
        issue = {"title": "Feature: add a thing", "labels": _ENGINE, "body": "no marker yet"}
        self.assertEqual(k.native_label_for_issue(issue), "enhancement")   # no regression from the old behaviour

    def test_non_engine_issue_uses_legacy_title_parse(self):
        self.assertEqual(k.native_label_for_issue({"title": "Bug: x", "labels": []}), "bug")

    def test_maintenance_marker_projects_to_no_native_label(self):
        issue = {"title": "x", "labels": _ENGINE, "body": issue_kind.kind_trailer("Maintenance")}
        self.assertIsNone(k.native_label_for_issue(issue))


class TestReconcileTitle(unittest.TestCase):
    """The title reconcile (StarshipSuperjam/engine-template#937): repair a missing/invented/stale prefix from
    the authoritative marker, idempotently and lost-update-safe, only for engine+marker issues."""

    def _client(self, gh):
        return issue_label_client.IssueLabelClient("o/r", "t", user_agent=k.USER_AGENT, transport=gh)

    def _issue(self, title, kind_marker, labels=None, body=None):
        return {"number": 9, "title": title,
                "labels": _ENGINE if labels is None else labels,
                "body": issue_kind.kind_trailer(kind_marker) if body is None else body}

    def test_invented_prefix_is_repaired(self):
        issue = self._issue("Architecture: example", "Improvement")
        gh = k._FakeGitHub(live_issue=issue)
        self.assertEqual(k.reconcile_title(dict(issue), self._client(gh)), "retitled")
        self.assertEqual(gh.title_edits()[0][2], {"title": "Improvement: example"})

    def test_missing_prefix_is_restored(self):
        issue = self._issue("example", "Fix")
        gh = k._FakeGitHub(live_issue=issue)
        k.reconcile_title(dict(issue), self._client(gh))
        self.assertEqual(gh.title_edits()[0][2], {"title": "Fix: example"})

    def test_stale_prefix_is_repaired(self):
        issue = self._issue("Feature: x", "Security")   # marker says Security; title says Feature
        gh = k._FakeGitHub(live_issue=issue)
        k.reconcile_title(dict(issue), self._client(gh))
        self.assertEqual(gh.title_edits()[0][2], {"title": "Security: x"})

    def test_all_six_kinds_repair_from_a_drifted_prefix(self):
        for kind in issue_kind.KINDS:
            issue = self._issue("Bug: drift", kind)     # `Bug:` is a recognised slot → stripped and replaced
            gh = k._FakeGitHub(live_issue=issue)
            k.reconcile_title(dict(issue), self._client(gh))
            self.assertEqual(gh.title_edits()[0][2], {"title": f"{kind}: drift"}, kind)

    def test_ordinary_descriptive_edit_preserves_the_prefix_with_no_write(self):
        # editing the descriptive part keeps the canonical prefix — the snapshot is already the fixed point.
        issue = self._issue("Improvement: a much better example", "Improvement")
        gh = k._FakeGitHub()
        self.assertEqual(k.reconcile_title(dict(issue), self._client(gh)), "title-canonical")
        self.assertEqual(gh.calls, [])                  # no live read, no write

    def test_no_marker_is_a_noop(self):
        gh = k._FakeGitHub()
        issue = self._issue("Bug: x", "Fix", body="no marker here")
        self.assertEqual(k.reconcile_title(issue, self._client(gh)), "no-marker")
        self.assertEqual(gh.title_edits(), [])

    def test_non_engine_forged_marker_never_retitles(self):
        gh = k._FakeGitHub()
        issue = self._issue("arbitrary human title", "Security", labels=[{"name": "bug"}])
        self.assertEqual(k.reconcile_title(issue, self._client(gh)), "no-marker")
        self.assertEqual(gh.title_edits(), [])

    def test_second_pass_over_a_repaired_title_is_zero_writes(self):
        repaired = self._issue("Improvement: example", "Improvement")
        gh = k._FakeGitHub(live_issue=repaired)
        self.assertEqual(k.reconcile_title(dict(repaired), self._client(gh)), "title-canonical")
        self.assertEqual(gh.calls, [])                  # the idempotent fixed point issues NO writes

    def test_lost_update_a_concurrent_edit_is_not_reverted(self):
        # the snapshot says repair, but a human already made it canonical LIVE → skip the write
        snapshot = self._issue("Architecture: example", "Improvement")
        live = self._issue("Improvement: a concurrently edited example", "Improvement")
        gh = k._FakeGitHub(live_issue=live)
        self.assertEqual(k.reconcile_title(snapshot, self._client(gh)), "already-repaired")
        self.assertEqual(gh.title_edits(), [])

    def test_marker_removed_live_before_the_write_is_a_noop(self):
        snapshot = self._issue("Architecture: example", "Improvement")
        live = self._issue("Architecture: example", "Improvement", body="the marker was removed")
        gh = k._FakeGitHub(live_issue=live)
        self.assertEqual(k.reconcile_title(snapshot, self._client(gh)), "marker-gone")
        self.assertEqual(gh.title_edits(), [])

    def test_api_failure_on_the_title_write_surfaces_as_degraded_write(self):
        issue = self._issue("Architecture: example", "Improvement")

        def boom(method, path, body=None):
            if method == "PATCH":
                return 500, None
            if re.search(r"/issues/\d+$", path):
                return 200, issue
            return 200, None
        with self.assertRaises(issue_label_client.DegradedWriteError):
            k.reconcile_title(dict(issue), self._client(boom))


class TestClientMinimalMutations(unittest.TestCase):
    """get_issue / edit_title — the minimal whole-Issue operations the reconciler adds to the shared client."""

    def _client(self, transport):
        return issue_label_client.IssueLabelClient("o/r", "t", user_agent="ua", transport=transport)

    def test_get_issue_returns_live_state(self):
        gh = k._FakeGitHub(live_issue={"number": 1, "title": "Live T", "body": "Live B"})
        self.assertEqual(self._client(gh).get_issue(1)["title"], "Live T")

    def test_get_issue_raises_on_failure(self):
        with self.assertRaises(issue_label_client.DegradedWriteError):
            self._client(lambda m, p, b=None: (503, None)).get_issue(1)

    def test_edit_title_patches_the_title(self):
        gh = k._FakeGitHub()
        self._client(gh).edit_title(5, "Fix: x")
        self.assertEqual(gh.calls[-1], ("PATCH", "/repos/o/r/issues/5", {"title": "Fix: x"}))

    def test_edit_title_raises_on_failure(self):
        with self.assertRaises(issue_label_client.DegradedWriteError):
            self._client(lambda m, p, b=None: (500, None)).edit_title(5, "Fix: x")


class TestIssueTemplatesUseCanonicalKinds(unittest.TestCase):
    """The acceptance guarantee that templates cannot emit a non-canonical prefix (Bug:/Engine fault:/Defect:)
    is ONGOING, not one-time — pin each issue template's seeded title prefix to the canonical vocabulary so a
    future edit that reintroduces a legacy prefix fails rather than passing every existing test."""

    def test_each_issue_template_title_prefix_is_canonical(self):
        tdir = os.path.join(validate.ROOT, ".github", "ISSUE_TEMPLATE")
        seen = 0
        for fname in sorted(os.listdir(tdir)):
            if not fname.endswith(".md"):
                continue
            with open(os.path.join(tdir, fname), encoding="utf-8") as fh:
                m = re.search(r"(?m)^title:\s*['\"]?([^'\"\n:]*):", fh.read())
            if not m:
                continue   # a template with no kind-prefixed title (a bare `title:`) is allowed
            seen += 1
            self.assertIn(m.group(1).strip(), issue_kind.KINDS,
                          f"{fname} seeds a non-canonical kind prefix {m.group(1).strip()!r}")
        self.assertGreater(seen, 0, "expected at least one kind-prefixed issue template")


class TestDemoSelfChecks(unittest.TestCase):
    def test_demo_passes(self):
        self.assertEqual(quiet_call.run(k._demo), 0)


if __name__ == "__main__":
    unittest.main()
