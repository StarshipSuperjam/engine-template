#!/usr/bin/env python3
"""Tests for the coordination advisory-only guarantees (StarshipSuperjam/engine-template#939): the fail-closed confinement check
(per-category bite + the real tree is confined + the scanner excludes itself), the authority-scoping pin
(a forged doorbell cannot redirect prepare/advance onto a foreign branch), and the no-local-identifiers law
(no session/worktree/machine identifier reaches a durable surface)."""
import inspect
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coordination_confinement_check as conf  # noqa: E402
import coordination_notice as cn  # noqa: E402


def _root_with(filename: str, body: str) -> str:
    root = tempfile.mkdtemp()
    tools = os.path.join(root, ".engine", "tools")
    os.makedirs(tools)
    with open(os.path.join(tools, filename), "w", encoding="utf-8") as fh:
        fh.write(body)
    return root


class TestConfinementPerCategory(unittest.TestCase):
    def _bites(self, body: str, filename: str = "coordination_x.py") -> bool:
        findings = conf.check(_root_with(filename, body))
        return any(f["severity"] == "hard" for f in findings)

    def test_merge_bites(self):
        self.assertTrue(self._bites('def f(t, r, n): t("POST", f"/repos/{r}/pulls/{n}/merge", {})\n'))

    def test_label_bites(self):
        self.assertTrue(self._bites('def f(t, r, n): t("POST", f"/repos/{r}/issues/{n}/labels", {})\n'))

    def test_commit_status_bites(self):
        self.assertTrue(self._bites('import ack_status\n'))

    def test_issue_body_edit_bites(self):
        # a PATCH to issues/{n} (NOT a comment) is a body edit -> the whitelist method check fires
        self.assertTrue(self._bites('def f(t, r, n): t("PATCH", f"/repos/{r}/issues/{n}", {"body": "x"})\n'))

    def test_pr_state_change_bites(self):
        self.assertTrue(self._bites('import build_coordinator_github as g\ndef f(): g.set_ready(1, 2, 3)\n'))

    def test_comment_write_is_allowed(self):
        clean = ('def f(t, r, n):\n'
                 '    t("POST", f"/repos/{r}/issues/{n}/comments", {"body": "x"})\n'
                 '    t("PATCH", f"/repos/{r}/issues/comments/{n}", {"body": "x"})\n'
                 '    t("GET", f"/repos/{r}/pulls?state=open", None)\n')
        self.assertFalse(self._bites(clean))

    def test_scanner_excludes_check_files(self):
        # a *_check.py with violations is the scanner surface, not coordination runtime -> not scanned
        self.assertFalse(self._bites('import ack_status\n', filename="coordination_evil_check.py"))

    def test_delete_to_a_comment_bites(self):
        # The law sanctions only the two comment-WRITE shapes (POST create, PATCH edit); DELETE is never
        # sanctioned, even to a comments path -> the scanner must flag it (not exempt it for "comments").
        self.assertTrue(self._bites('def f(t, r, n): t("DELETE", f"/repos/{r}/issues/comments/{n}", None)\n'))

    def test_put_to_a_comment_bites(self):
        self.assertTrue(self._bites('def f(t, r, n): t("PUT", f"/repos/{r}/issues/comments/{n}", {"body": "x"})\n'))

    def test_lowercase_mutating_method_bites(self):
        # case-insensitive: a lowercase literal to a non-comment endpoint must not slip the catch-all
        self.assertTrue(self._bites('def f(t, r, n): t("post", f"/repos/{r}/issues/{n}/subscription", {})\n'))

    def test_single_quoted_delete_to_a_comment_bites(self):
        # the naive-in-file case must be caught in EITHER quote style — a single-quoted DELETE to a comments
        # path is not a sanctioned comment write (only POST/PATCH are) and must still bite.
        self.assertTrue(self._bites("def f(t, r, n): t('DELETE', f'/repos/{r}/issues/comments/{n}', None)\n"))

    def test_single_quoted_comment_post_is_allowed(self):
        # a legitimate single-quoted POST to a comments endpoint is still a sanctioned comment write
        self.assertFalse(self._bites("def f(t, r, n): t('POST', f'/repos/{r}/issues/{n}/comments', {'body': 'x'})\n"))


class TestRealTreeConfined(unittest.TestCase):
    def test_real_coordination_library_is_confined(self):
        self.assertEqual(conf.check(), [])  # root=None -> the real tree; must be clean


class TestAuthorityScoping(unittest.TestCase):
    """A forged 'you're next, prepare' doorbell must not be able to redirect work onto a foreign branch. The
    structural guarantee: pr_reconcile.prepare takes NO pull-request/branch selector — it acts only on the
    checked-out branch. Pin it so a future signature change that added one would fail here."""

    def test_prepare_takes_no_foreign_ref_selector(self):
        import pr_reconcile
        params = set(inspect.signature(pr_reconcile.prepare).parameters)
        for forbidden in ("pr", "number", "branch", "ref", "head", "target"):
            self.assertNotIn(forbidden, params,
                             f"pr_reconcile.prepare exposes '{forbidden}' — a doorbell could redirect it")

    def test_surface_next_pr_arg_is_ownership_only(self):
        # surface_next's only pr-shaped param is this_pr (an ownership gate); it never takes a target branch.
        import integration_queue
        params = set(inspect.signature(integration_queue.surface_next).parameters)
        self.assertIn("this_pr", params)
        for forbidden in ("branch", "ref", "head", "target_branch"):
            self.assertNotIn(forbidden, params)


class TestNoLocalIdentifiers(unittest.TestCase):
    def test_schema_has_no_session_or_machine_field(self):
        with open(cn._SCHEMA_REL, encoding="utf-8") as fh:
            schema = json.load(fh)

        names = set()

        def _collect(node):
            if isinstance(node, dict):
                for key in ("properties",):
                    if isinstance(node.get(key), dict):
                        names.update(node[key].keys())
                for v in node.values():
                    _collect(v)
            elif isinstance(node, list):
                for v in node:
                    _collect(v)

        _collect(schema)
        for forbidden in ("session", "worktree", "machine", "hostname", "session_id", "session_tag"):
            self.assertNotIn(forbidden, names, f"the notice schema has a '{forbidden}' FIELD")

    def test_absolute_and_home_paths_are_dropped(self):
        n = cn.render(kind="dependency-update", event="merged", emitter_work_ref={"pr": 1},
                      audience={"pr": 1},
                      subject={"pr": 1, "paths": ["/Users/alice/wt/secret.py", "src/ok.py",
                                                  "/home/bob/x.py"]},
                      verify_action="none", now="2026-08-18T00:00:00Z", id_source=lambda: "a" * 32)
        self.assertEqual(n["subject"]["paths"], ["src/ok.py"])  # only the repo-relative one survives
        block = cn.render_block(n)
        self.assertNotIn("/Users/", block)
        self.assertNotIn("/home/", block)


if __name__ == "__main__":
    unittest.main()
