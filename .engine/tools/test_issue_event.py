"""Tests for issue_event — the shared on:issues event-parsing boundary the two backstops route through.

These lock the defensive event mechanics that both `on:issues` nets (issue_conformance_ci, issue_kind_label)
now depend on: the $GITHUB_EVENT_PATH reader tolerates an absent/unreadable/malformed event as a quiet None;
`issue_or_none` extracts an issue with a numeric id and is deliberately SCOPE-FREE (no label or title policy —
that is why the same primitive serves the engine-label net AND the any-issue kind-label net); `labels_of` reads
`.issue.labels[].name` defensively; and `resolve_repo_token` resolves the credential pair while deciding
NOTHING about what a missing value means (each caller keeps that fail policy local). The security property the
whole layer exists to hold: the event is read from the JSON file, never a shell-interpolated argument, and this
module only parses — it applies no label and runs no command.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_event  # noqa: E402


def _event_file(testcase, event) -> str:
    """Write `event` to a temp file and return its path (cleaned up after the test). A dict is JSON-encoded; a
    str is written raw — the malformed-JSON case."""
    import json
    fd, path = tempfile.mkstemp(suffix=".json")
    testcase.addCleanup(os.remove, path)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        if isinstance(event, str):
            fh.write(event)
        else:
            json.dump(event, fh)
    return path


class _EnvIsolated(unittest.TestCase):
    """Base that snapshots and clears the three GitHub env keys, restoring them after each test so a test's
    env manipulation can never leak into the rest of the suite."""

    _KEYS = ("GITHUB_EVENT_PATH", "GITHUB_TOKEN", "GITHUB_REPOSITORY")

    def _env(self, **overrides):
        saved = {k: os.environ.get(k) for k in self._KEYS}

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        for k in self._KEYS:
            os.environ.pop(k, None)
        for k, v in overrides.items():
            if v is not None:
                os.environ[k] = v


class TestLoadEvent(_EnvIsolated):
    """load_event reads $GITHUB_EVENT_PATH from the file and degrades every unreadable case to None."""

    def test_unset_path_is_none(self):
        self._env()  # GITHUB_EVENT_PATH unset
        self.assertIsNone(issue_event.load_event())

    def test_missing_file_is_none(self):
        self._env(GITHUB_EVENT_PATH="/no/such/event/file.json")
        self.assertIsNone(issue_event.load_event())

    def test_malformed_json_is_none(self):
        path = _event_file(self, "{not valid json at all")
        self._env(GITHUB_EVENT_PATH=path)
        self.assertIsNone(issue_event.load_event())

    def test_unreadable_path_is_none(self):
        # a directory path → open() raises IsADirectoryError (an OSError) → tolerated as None, never a crash.
        d = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, d)
        self._env(GITHUB_EVENT_PATH=d)
        self.assertIsNone(issue_event.load_event())

    def test_valid_event_is_parsed(self):
        path = _event_file(self, {"issue": {"number": 7, "title": "Fix: x"}})
        self._env(GITHUB_EVENT_PATH=path)
        event = issue_event.load_event()
        self.assertEqual(event, {"issue": {"number": 7, "title": "Fix: x"}})


class TestLabelsOf(unittest.TestCase):
    """labels_of reads `.issue.labels[].name`, defensively against a missing/None/partly-malformed list."""

    def test_reads_label_names(self):
        self.assertEqual(issue_event.labels_of({"labels": [{"name": "engine"}, {"name": "bug"}]}),
                         ["engine", "bug"])

    def test_missing_labels_is_empty(self):
        self.assertEqual(issue_event.labels_of({}), [])

    def test_none_labels_is_empty(self):
        self.assertEqual(issue_event.labels_of({"labels": None}), [])

    def test_non_dict_label_entries_are_skipped(self):
        # a stray string in the list is ignored; a dict with no `name` yields None (the caller's `in` checks
        # never match None, so this is safe) — the point is it must not raise on a partial payload.
        self.assertEqual(issue_event.labels_of({"labels": [{"name": "engine"}, "stray", {"colour": "x"}]}),
                         ["engine", None])


class TestIssueOrNone(unittest.TestCase):
    """issue_or_none is SCOPE-FREE: it asserts only a dict issue with an integer number, imposing no label or
    title policy — which is exactly why the same primitive serves both backstops."""

    def test_valid_numeric_issue_is_returned(self):
        issue = {"number": 3, "title": "Feature: y", "labels": []}
        self.assertEqual(issue_event.issue_or_none({"issue": issue}), issue)

    def test_scope_free_returns_any_issue_regardless_of_labels(self):
        # the load-bearing property: no engine-label gate here. An unlabelled or product-labelled issue with a
        # numeric id still comes back — the kind-label net relies on this (it acts on ANY issue).
        for labels in ([], [{"name": "bug"}], [{"name": "some-product-label"}]):
            issue = {"number": 9, "title": "Bug: z", "labels": labels}
            self.assertEqual(issue_event.issue_or_none({"issue": issue}), issue, repr(labels))

    def test_non_dict_event_is_none(self):
        for event in (None, "not-a-dict", 42, []):
            self.assertIsNone(issue_event.issue_or_none(event), repr(event))

    def test_missing_or_none_issue_is_none(self):
        self.assertIsNone(issue_event.issue_or_none({}))
        self.assertIsNone(issue_event.issue_or_none({"issue": None}))

    def test_non_dict_issue_is_none(self):
        self.assertIsNone(issue_event.issue_or_none({"issue": "nope"}))

    def test_non_numeric_or_missing_number_is_none(self):
        self.assertIsNone(issue_event.issue_or_none({"issue": {"number": "3"}}))
        self.assertIsNone(issue_event.issue_or_none({"issue": {"number": None}}))
        self.assertIsNone(issue_event.issue_or_none({"issue": {"title": "no number at all"}}))


class TestResolveRepoToken(_EnvIsolated):
    """resolve_repo_token returns the (repo, token) pair and decides NOTHING — never raises, never exits."""

    def test_both_present(self):
        self._env(GITHUB_REPOSITORY="o/r", GITHUB_TOKEN="tok")
        self.assertEqual(issue_event.resolve_repo_token(), ("o/r", "tok"))

    def test_both_unset_is_none_pair(self):
        self._env()
        self.assertEqual(issue_event.resolve_repo_token(), (None, None))

    def test_partial_resolution_is_reported_not_decided(self):
        self._env(GITHUB_REPOSITORY="o/r")  # token unset
        self.assertEqual(issue_event.resolve_repo_token(), ("o/r", None))
        # it resolves and returns — it is the CALLER that turns a missing half into a no-op or a red run.


class TestDependencyLight(unittest.TestCase):
    """The parser is imported on every per-issue CI hot path, so it must stay stdlib-only — importing it must
    never drag a heavier engine module into its namespace."""

    def test_no_heavy_engine_modules_leak_in(self):
        for heavy in ("release_cut", "module_manager", "module_coherence", "issue_label_client", "github_client"):
            self.assertNotIn(heavy, getattr(issue_event, "__dict__", {}),
                             f"issue_event must not import {heavy} (dependency-light on:issues hot path)")


if __name__ == "__main__":
    unittest.main()
