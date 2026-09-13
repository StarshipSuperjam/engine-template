#!/usr/bin/env python3
"""The renewable-root contract of `execution_context` (C4 node 1, plan pln_2dade630388f).

Exactly two operations own a renewable root context — the long-lived accepted memory server and the
short-lived accepted write-dispatch child — and both get the same three renewals: a per-operation narrowing
under the held store lock (`refresh_for_operation`), a post-commit re-seal of the root
(`refresh_current_context`), and the read-side root refresh (`refresh_root_for_read`). Any other root — a
command-line writer such as pins.py — is refused all three with a `ContextError`, so a context that merely
carries a script name can never renew itself into write authority. The behaviours these renewals feed are
proven end to end in test_mutation_authority.py and test_mcp_server.py; this module pins the GATE itself,
which the plan named as this node's own verification instrument (round 5, SC-1)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import execution_context  # noqa: E402
from memory.test_mutation_authority import _QualifiedFixture  # noqa: E402


class RenewableRootTests(unittest.TestCase):
    def setUp(self):
        execution_context._CURRENT_CONTEXT = None

    def tearDown(self):
        execution_context._CURRENT_CONTEXT = None
        os.environ.pop(execution_context.CONTEXT_ENV, None)

    def _fixture(self, **kind):
        # The fixture's own cleanup also resets a lock hook that is test-only for the authority suite; this
        # module never installs one, so it releases only what it used: the temporary tree and the env.
        fixture = _QualifiedFixture(**kind)
        self.addCleanup(fixture.temp.cleanup)
        return fixture

    def test_exactly_the_server_and_the_dispatch_child_own_a_renewable_root(self):
        self.assertEqual(execution_context.RENEWABLE_ROOT,
                         frozenset({"attended-memory-mcp", "attended-write-dispatch"}))

    def test_the_dispatch_child_narrows_to_the_write_operation_it_is_about_to_run(self):
        fixture = self._fixture(dispatch=True)
        narrowed = execution_context.refresh_for_operation(fixture.context, "ledger-append")
        self.assertEqual(narrowed["operation"]["registry_id"], "ledger-append")
        self.assertEqual(narrowed["operation"]["invocation_mode"], "attended")
        self.assertNotEqual(narrowed.digest, fixture.context.digest)          # a fresh seal over disk
        self.assertTrue(execution_context._is_authorized_context(narrowed))   # and it is remembered

    def test_the_memory_server_still_narrows_but_not_into_the_write_operations(self):
        fixture = self._fixture(mcp=True)
        narrowed = execution_context.refresh_for_operation(fixture.context, "read-pins")
        self.assertEqual(narrowed["operation"]["registry_id"], "read-pins")
        with self.assertRaises(execution_context.ContextError) as caught:
            execution_context.refresh_for_operation(fixture.context, "ledger-append")
        self.assertIn("outside this invocation's closed transitive boundary", str(caught.exception))

    def test_a_command_line_writer_is_refused_every_renewal(self):
        fixture = self._fixture()   # ledger-append on pins.py: a real writer, but not a renewable root
        for renew in (lambda c: execution_context.refresh_for_operation(c, "ledger-append"),
                      execution_context.refresh_current_context,
                      execution_context.refresh_root_for_read):
            with self.subTest(renewal=getattr(renew, "__name__", "refresh_for_operation")):
                with self.assertRaises(execution_context.ContextError):
                    renew(fixture.context)
        self.assertIsNone(execution_context._CURRENT_CONTEXT)               # nothing was installed

    def test_both_roots_reseal_after_a_commit_and_install_the_new_root(self):
        for kind in ({"dispatch": True}, {"mcp": True}):
            with self.subTest(root=next(iter(kind))):
                fixture = self._fixture(**kind)
                execution_context._CURRENT_CONTEXT = None
                resealed = execution_context.refresh_current_context(fixture.context)
                self.assertEqual(resealed["operation"]["registry_id"],
                                 fixture.context["operation"]["registry_id"])   # the root stays the root
                self.assertIs(execution_context._CURRENT_CONTEXT, resealed)

    def test_both_roots_get_the_read_side_refresh_without_changing_their_binding(self):
        for kind in ({"dispatch": True}, {"mcp": True}):
            with self.subTest(root=next(iter(kind))):
                fixture = self._fixture(**kind)
                execution_context._CURRENT_CONTEXT = None
                refreshed = execution_context.refresh_root_for_read(fixture.context)
                self.assertEqual(execution_context.binding_identity(refreshed),
                                 execution_context.binding_identity(fixture.context))
                self.assertIs(execution_context._CURRENT_CONTEXT, refreshed)
                self.assertEqual(os.environ[execution_context.CONTEXT_ENV], refreshed.to_json())


if __name__ == "__main__":
    unittest.main()
