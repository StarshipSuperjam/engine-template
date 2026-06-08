"""Tests for the shared optional-module catalog reader (core slice 27a).

Verifies the single parse path both readers (the /engine-help index and the first-run walkthrough) share:
a normalized record per entry sorted by command; degrade-to-empty on absent / unreadable / malformed /
wrong-shaped input (never raises); an entry with no command dropped; non-dict items skipped; missing
optional fields coerced; and the committed catalog (the default path) read as the empty array it ships.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import module_catalog as mc  # noqa: E402


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestEntries(unittest.TestCase):
    def test_absent_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(mc.entries(os.path.join(d, "nope.json")), [])

    def test_malformed_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            _write(p, "{ not valid json")
            self.assertEqual(mc.entries(p), [], "a damaged catalog narrows to nothing, never raises")

    def test_non_array_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            _write(p, json.dumps({"modules": []}))
            self.assertEqual(mc.entries(p), [], "the catalog must be a top-level array; an object narrows")

    def test_scalar_top_level_returns_empty(self):
        # A top-level scalar (not even iterable as records) must narrow, never raise.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            _write(p, "42")
            self.assertEqual(mc.entries(p), [], "a scalar catalog body narrows to nothing")

    def test_valid_entries_normalized_and_sorted_by_verb(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            _write(p, json.dumps([
                {"id": "z-mod", "verb": "engine-zed", "description": "Z.",
                 "category": "Product Management", "status": "optional"},
                {"id": "a-mod", "verb": "engine-ay", "description": "A.",
                 "category": "Verification & Validation"},
            ]))
            got = mc.entries(p)
            self.assertEqual([e["verb"] for e in got], ["engine-ay", "engine-zed"], "sorted by command")
            self.assertEqual(got[0], {"id": "a-mod", "verb": "engine-ay", "description": "A.",
                                      "category": "Verification & Validation", "status": ""},
                             "missing optional fields coerce to empty string; all fields present")

    def test_entry_without_verb_is_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            _write(p, json.dumps([{"id": "no-verb", "description": "has no command"},
                                  {"verb": "engine-keep", "description": "kept"}]))
            self.assertEqual([e["verb"] for e in mc.entries(p)], ["engine-keep"],
                             "an entry with no command is unusable to either reader and is dropped")

    def test_non_dict_items_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            _write(p, json.dumps(["a string", 5, {"verb": "engine-ok", "description": "d"}]))
            self.assertEqual([e["verb"] for e in mc.entries(p)], ["engine-ok"])

    def test_committed_catalog_ships_empty(self):
        # The default path reads the real committed catalog, which ships as an empty array.
        self.assertEqual(mc.entries(), [])


if __name__ == "__main__":
    unittest.main()
