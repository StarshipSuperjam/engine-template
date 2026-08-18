#!/usr/bin/env python3
"""Tests for coordination_domains — declared/actual domain resolution and lock-free overlap, with the
DAG-primitive parity pins (StarshipSuperjam/engine-template#939)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator_dag as dag  # noqa: E402
import coordination_domains as cd  # noqa: E402


def _reader(files, *, status=200, full=False):
    """A fake read-only GET returning a pulls/files page. `full` pads to the page size to trip truncation."""
    payload = [{"filename": f} for f in files]
    if full:
        payload = payload + [{"filename": f"pad/{i}.py"} for i in range(cd._FILES_PAGE)]

    def _r(method, path):
        assert method == "GET" and "/pulls/" in path and "/files" in path
        return status, payload

    return _r


class TestDeclaredFromPlan(unittest.TestCase):
    def test_union_dedup_sorted(self):
        plan = {"work_items": [{"paths": ["b/*.py", "a.py"]}, {"paths": ["a.py", "c/"]}]}
        self.assertEqual(cd.declared_paths_from_plan(plan), ["a.py", "b/*.py", "c/"])

    def test_empty_plan(self):
        self.assertEqual(cd.declared_paths_from_plan({}), [])
        self.assertEqual(cd.declared_paths_from_plan(None), [])


class TestChangedFiles(unittest.TestCase):
    def test_reads_filenames(self):
        files, trunc = cd.changed_files(_reader(["a.py", "b/c.py"]), "o/r", 3)
        self.assertEqual(files, ["a.py", "b/c.py"])
        self.assertFalse(trunc)

    def test_truncation_disclosed(self):
        _files, trunc = cd.changed_files(_reader(["a.py"], full=True), "o/r", 3)
        self.assertTrue(trunc)

    def test_read_failure_is_unknown_not_empty_touch(self):
        files, trunc = cd.changed_files(_reader([], status=404), "o/r", 3)
        self.assertEqual((files, trunc), ([], False))


class TestOverlap(unittest.TestCase):
    def test_declared_vs_declared_uses_paths_conflict(self):
        a = {"declared": [".engine/tools/coordination_notice.py"], "actual": []}
        b = {"declared": [".engine/tools/coordination_notice.py"], "actual": []}
        self.assertTrue(cd.overlaps(a, b))
        self.assertEqual(cd.overlaps(a, b),
                         dag.paths_conflict(a["declared"], b["declared"]))

    def test_disjoint_declared(self):
        a = {"declared": ["docs/*.md"], "actual": []}
        b = {"declared": ["docs/*.json"], "actual": []}
        self.assertFalse(cd.overlaps(a, b))
        self.assertEqual(cd.overlaps(a, b), dag.paths_conflict(a["declared"], b["declared"]))

    def test_actual_file_within_other_declared(self):
        a = {"declared": [], "actual": [".engine/tools/boot.py"]}
        b = {"declared": [".engine/tools/"], "actual": []}
        self.assertTrue(cd.overlaps(a, b))
        # parity with the primitive it must call for this case
        self.assertTrue(any(dag.path_within_declared(f, b["declared"]) for f in a["actual"]))

    def test_concrete_file_intersection(self):
        a = {"declared": [], "actual": ["x/y.py"]}
        b = {"declared": [], "actual": ["x/y.py"]}
        self.assertTrue(cd.overlaps(a, b))

    def test_fully_disjoint(self):
        a = {"declared": ["src/*.py"], "actual": ["src/a.py"]}
        b = {"declared": ["web/*.ts"], "actual": ["web/b.ts"]}
        self.assertFalse(cd.overlaps(a, b))

    def test_domain_assembles(self):
        d = cd.domain(_reader(["a.py"]), "o/r", 3, declared=["b/*.py"])
        self.assertEqual(d, {"declared": ["b/*.py"], "actual": ["a.py"], "truncated": False})


if __name__ == "__main__":
    unittest.main()
