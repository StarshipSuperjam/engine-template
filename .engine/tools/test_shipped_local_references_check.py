#!/usr/bin/env python3
"""Tests for the shipped local-reference floor (StarshipSuperjam/engine-template#943).

The floor reuses the sibling shipped-issue-references machinery for the shipped surface and prose extraction
(exhaustively tested there), so these tests pin the DELTA: it is declaration-driven (no-ops when nothing is
declared, scans against the declared vocabulary), prose-only including shipped test/demo comments and
docstrings, and fails closed on an unreadable retire census. A synthetic `ZZ-` vocabulary is used so no real
decision-record token is seeded.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shipped_local_references_check as check  # noqa: E402
import validate  # noqa: E402

_DECL = ".engine/operator-local-references.json"


def _seed(root, files, *, declare="ZZ-", with_manifest=True):
    """Write `files` (repo-relative -> content) under `root`; add a default empty retire census unless opted
    out; and, unless `declare` is None, write an operator-local-references.json declaring it (`declare` may be
    a prefix string or a full id_prefixes list, so `[]` seeds the deliberately-empty case)."""
    for rel, content in files.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
    manifest = os.path.join(root, ".engine", "provisioning", "first-run-assets.json")
    if with_manifest and not os.path.exists(manifest):
        os.makedirs(os.path.dirname(manifest), exist_ok=True)
        with open(manifest, "w", encoding="utf-8") as fh:
            fh.write('{"files": [], "dirs": []}')
    if declare is not None:
        prefixes = declare if isinstance(declare, list) else [declare]
        p = os.path.join(root, *_DECL.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"id_prefixes": prefixes}))


class _Rooted(unittest.TestCase):
    """Point validate.ROOT at a seeded tree so both the scan root and the declaration are read from it (the
    floor has no home-repo gate — it is scoped by the declaration itself)."""
    def _check(self, files, *, declare="ZZ-", with_manifest=True):
        with tempfile.TemporaryDirectory() as root:
            _seed(root, files, declare=declare, with_manifest=with_manifest)
            with mock.patch.object(validate, "ROOT", root):
                return check.check()


class DeclarationDriven(_Rooted):
    def test_no_declaration_no_ops(self):
        # the steady state for every deployment: nothing declared -> nothing scanned, even with a would-be token
        f = self._check({".engine/tools/probe.py": '"""cites ZZ-1 here."""\n'}, declare=None)
        self.assertEqual(f, [])

    def test_an_empty_declaration_no_ops(self):
        # a deliberate "this project has no shorthand" -> nothing compiled -> nothing scanned
        f = self._check({".engine/tools/probe.py": '"""cites ZZ-1 here."""\n'}, declare=[])
        self.assertEqual(f, [])

    def test_a_declared_reference_in_a_comment_is_flagged(self):
        f = self._check({".engine/tools/probe.py": 'def g():\n    # see ZZ-1 here\n    return 1\n'})
        self.assertEqual(len(f), 1)
        self.assertIn("ZZ-1", f[0]["message"])
        self.assertEqual(f[0]["severity"], "hard")

    def test_a_declared_reference_in_a_docstring_is_flagged(self):
        f = self._check({".engine/tools/probe.py": '"""mentions ZZ-1 in prose."""\nx = 1\n'})
        self.assertEqual(len(f), 1)

    def test_a_reference_inside_a_string_literal_is_not_flagged(self):
        # prose-only, the load-bearing exclusion: assertion data / behaviour-bearing strings are never swept
        f = self._check({".engine/tools/probe.py": 'def g():\n    msg = "ZZ-1 in a string"\n    return msg\n'})
        self.assertEqual(f, [])

    def test_a_shipped_test_comment_and_docstring_are_scanned(self):
        source = '"""cites ZZ-1 in a docstring."""' + chr(10) + '# and ZZ-2 in a comment' + chr(10)
        f = self._check({".engine/tools/test_probe.py": source})
        self.assertEqual(len(f), 2)
        self.assertIn("ZZ-1", f[0]["message"] + f[1]["message"])
        self.assertIn("ZZ-2", f[0]["message"] + f[1]["message"])

    def test_a_shipped_test_fixture_string_is_not_scanned(self):
        f = self._check({".engine/tools/test_probe.py": 'fixture = "ZZ-1 in a fixture string"' + chr(10)})
        self.assertEqual(f, [])

    def test_a_retired_test_file_remains_excluded(self):
        f = self._check({".engine/tools/test_probe.py": '"""cites ZZ-1."""',
                         ".engine/provisioning/first-run-assets.json":
                         '{"files": [".engine/tools/test_probe.py"], "dirs": []}'})
        self.assertEqual(f, [])

    def test_a_markdown_reference_is_flagged(self):
        f = self._check({".engine/contracts/note.md": "A note citing ZZ-1 by bare identifier.\n"})
        self.assertEqual(len(f), 1)

    def test_an_undeclared_prefix_is_left_alone(self):
        # only the DECLARED prefix matches; a different id shape is not this vocabulary's concern
        f = self._check({".engine/tools/probe.py": '"""cites QQ-9 here."""\n'})
        self.assertEqual(f, [])

    def test_the_declaration_file_itself_is_not_reported_back(self):
        # scan() skips the declaration path, and id_prefixes is not a prose key — the vocabulary never self-flags
        f = self._check({".engine/tools/probe.py": '"""clean."""\n'})
        self.assertEqual(f, [])

    def test_a_missing_retire_census_fails_closed(self):
        # without the first-run census the shipped surface cannot be enumerated -> a hard fault, never a silent pass
        f = self._check({".engine/tools/probe.py": '"""clean."""\n'}, with_manifest=False)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["severity"], "hard")
        self.assertIn("can't read", f[0]["message"].lower())


if __name__ == "__main__":
    unittest.main()
