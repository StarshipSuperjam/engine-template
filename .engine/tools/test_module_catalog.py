"""Tests for the shared optional-module catalog reader/generator.

Verifies the single parse path both readers (the /engine-help index and the first-run walkthrough) share:
a normalized record per entry {id, description, category, status} sorted by id; degrade-to-empty on absent /
unreadable / malformed / wrong-shaped input (never raises); missing optional fields coerced; non-dict items
skipped; and the committed catalog (the default path) read as the shipped array. There is no per-module
`verb` — offerable modules are reached through natural-language setup routes and the permanent engine-setup
dispatcher, not a typed command. Also verifies the DERIVED-committed generation: `derive`/`generate` produce
the catalog from the present offerable manifests' `presentation`, MERGE-PRESERVING a declined module's entry
(one with no present manifest) so a later upgrade neither resurrects nor forgets it.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import module_catalog as mc  # noqa: E402

_FIELDS = {"id", "description", "category", "status"}


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

    def test_valid_entries_normalized_and_sorted_by_id(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            _write(p, json.dumps([
                {"id": "z-mod", "description": "Z.", "category": "Product Management", "status": "optional"},
                {"id": "a-mod", "description": "A.", "category": "Verification & Validation"},
            ]))
            got = mc.entries(p)
            self.assertEqual([e["id"] for e in got], ["a-mod", "z-mod"], "sorted by id")
            self.assertEqual(got[0], {"id": "a-mod", "description": "A.",
                                      "category": "Verification & Validation", "status": ""},
                             "missing optional fields coerce to empty string; canonical fields, no verb")

    def test_entries_carry_no_verb(self):
        # The catalog no longer carries a per-module command; a normalized entry is exactly the four fields.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            _write(p, json.dumps([{"id": "m", "description": "d", "category": "Product Management"}]))
            self.assertEqual(set(mc.entries(p)[0]), _FIELDS, "no verb key; only the canonical fields")

    def test_non_dict_items_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            _write(p, json.dumps(["a string", 5, {"id": "ok", "description": "d",
                                                  "category": "Product Management"}]))
            self.assertEqual([e["id"] for e in mc.entries(p)], ["ok"])

    def test_committed_catalog_relays_the_shipped_optionals(self):
        # The committed catalog is the derived one; it relays github-projects-sync as a normalized entry
        # (the default path reads the real committed catalog), by description, with no verb.
        board = [e for e in mc.entries() if e["id"] == "github-projects-sync"]
        self.assertEqual(len(board), 1, "the committed catalog relays the github-projects-sync entry")
        self.assertEqual(board[0]["category"], "Product Management")
        self.assertNotIn("verb", board[0], "no per-module verb")


def _seed_module(root: str, mid: str = "seeded-addon") -> str:
    """Seed one offerable module in `root` so these cases derive from a module set they OWN. They once named a
    real optional module, which made them fail wherever that module is not installed — including the release
    gate's declined projection, the very shape the catalog's declined-memory exists for."""
    d = os.path.join(root, ".engine", "modules", mid)
    os.makedirs(d, exist_ok=True)
    _write(os.path.join(d, "manifest.json"), json.dumps(
        {"id": mid, "status": "optional",
         "presentation": {"description": "A seeded add-on.", "category": "Product Management",
                          "setup_trigger": "the operator asks to set up the seeded add-on"}}))
    return mid


class TestDeriveAndGenerate(unittest.TestCase):
    def test_derive_produces_offerable_modules_from_manifests(self):
        # derive() reads the present offerable manifests' `presentation`; every offerable module with a
        # presentation appears, keyed and sorted by id, with the canonical fields and no verb.
        with tempfile.TemporaryDirectory() as d:
            mid = _seed_module(d)
            got = mc.derive(os.path.join(d, "no-prior.json"), d)  # no prior catalog → pure from-manifests
            ids = [e["id"] for e in got]
            self.assertIn(mid, ids, "an offerable module with a presentation is derived")
            self.assertEqual(ids, sorted(ids), "sorted by id")
            for e in got:
                self.assertEqual(set(e), _FIELDS, "canonical fields only; no verb")
                self.assertTrue(e["description"] and e["category"], "presentation fields carried")

    def test_generate_merge_preserves_a_declined_entry(self):
        # A prior-catalog entry whose module has NO present manifest is a DECLINED module: its entry is
        # retained on regeneration so an upgrade neither resurrects nor forgets it. The present offerable
        # modules are (re)derived alongside it, and the result is written to disk.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "module-catalog.json")
            _write(p, json.dumps([{"id": "ghost-declined", "description": "A declined module.",
                                   "category": "Product Management", "status": "optional"}]))
            mid = _seed_module(d)
            result = mc.generate(p, d)
            ids = [e["id"] for e in result]
            self.assertIn("ghost-declined", ids, "a declined module with no present manifest is retained")
            self.assertIn(mid, ids, "present offerable modules are (re)derived")
            self.assertEqual(json.load(open(p)), result, "the derived catalog is written to disk")


class TestDriftCheck(unittest.TestCase):
    """The derived-committed drift gate: a hand-edited or absent catalog is flagged; a freshly generated one
    is in sync (a declined-module entry, having no manifest, is legitimately not flagged)."""

    def test_freshly_generated_catalog_is_in_sync(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "module-catalog.json")
            _seed_module(d)
            mc.generate(p, d)
            self.assertEqual(mc.check("hard", path=p, root=d), [])

    def test_hand_edited_present_entry_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "module-catalog.json")
            mid = _seed_module(d)
            mc.generate(p, d)
            data = json.load(open(p, encoding="utf-8"))
            for e in data:
                if e["id"] == mid:   # a PRESENT offerable module — derive rebuilds it
                    e["description"] = "HAND EDITED — the generator would never write this"
            _write(p, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            fs = [f for f in mc.check("hard", path=p, root=d) if f["severity"] == "hard"]
            self.assertTrue(fs, "a hand-edited present-module entry must be flagged as drift")
            self.assertIn("out of date", fs[0]["message"])

    def test_absent_catalog_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            fs = mc.check("hard", path=os.path.join(d, "nope.json"))
            self.assertTrue(any(f["severity"] == "hard" for f in fs))


if __name__ == "__main__":
    unittest.main()
