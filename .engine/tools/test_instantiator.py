"""Tests for the first-run setup orchestrator's GATHER + CONFIRM half (core slice 27a).

Verifies: the not-set-up signal keys off the manifest's presence (no new state); identity is derived
best-effort and degrades when it can't be read; the optional features group by discipline in the fixed
order (empty when the catalog is empty; an unrecognized category is kept, not dropped); the walkthrough is
plain-language and states the destructive-on-confirm outcome; CONFIRM writes the manifest with the required
spine always plus the kept optionals (an unkept optional omitted, its files NOT touched here — deletion is a
later phase); the catalog the demo plants conforms to the catalog schema; and the demo runs green.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import instantiator as inst  # noqa: E402
import validate  # noqa: E402

_FORBIDDEN = ("orchestrat", "coherence", "wiring", "wires", "manifest", "idempotent", "venv", "sync",
              "lockfile", "pyproject", "ruleset", "override", "custom/script", "provides", "invocation",
              "model-auto", "operator-typed", "model-only", "foundation")


def _module(root, mid, status, version="1.0.0"):
    d = os.path.join(root, ".engine", "modules", mid)
    os.makedirs(d, exist_ok=True)
    inst._write_json(os.path.join(d, "manifest.json"),
                     {"id": mid, "version": version, "status": status, "provides": {}, "depends": {}})


class TestIsProvisioned(unittest.TestCase):
    def test_absent_manifest_is_not_provisioned(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".engine"))
            self.assertFalse(inst.is_provisioned(d), "no manifest yet → setup should run")

    def test_present_manifest_is_provisioned(self):
        with tempfile.TemporaryDirectory() as d:
            inst._write_json(inst._engine_manifest_path(d),
                             {"engine_release": "1.0.0", "packages": {"core": "1.0.0"}, "identity": "solo"})
            self.assertTrue(inst.is_provisioned(d), "a written manifest → already set up")


class TestDeriveIdentity(unittest.TestCase):
    def test_derives_owner_and_name_from_slug(self):
        with mock.patch.object(inst.boot, "repo_slug", return_value="acme/widgets"):
            ident = inst.derive_identity()
        self.assertEqual((ident["owner"], ident["name"]), ("acme", "widgets"))
        self.assertEqual(ident["branch"], inst.boot.PROTECTED_BRANCH)

    def test_degrades_when_slug_unreadable(self):
        with mock.patch.object(inst.boot, "repo_slug", return_value=None):
            ident = inst.derive_identity()
        self.assertIsNone(ident["owner"])
        self.assertIsNone(ident["name"])


class TestSelectable(unittest.TestCase):
    def test_empty_catalog_groups_to_nothing(self):
        self.assertEqual(inst.selectable([]), {})

    def test_grouped_in_fixed_category_order(self):
        entries = [
            {"verb": "engine-vv", "description": "x", "category": "Verification & Validation"},
            {"verb": "engine-pm", "description": "x", "category": "Product Management"},
        ]
        self.assertEqual(list(inst.selectable(entries).keys()),
                         ["Product Management", "Verification & Validation"], "PM before V&V (fixed order)")

    def test_unrecognized_category_kept_last_not_dropped(self):
        entries = [{"verb": "engine-x", "description": "x", "category": "Made Up"},
                   {"verb": "engine-pm", "description": "y", "category": "Product Management"}]
        keys = list(inst.selectable(entries).keys())
        self.assertIn("Made Up", keys, "an unexpected category is kept, never silently dropped")
        self.assertEqual(keys[-1], "Made Up", "an unexpected category sorts after the recognized ones")


class TestPresentGather(unittest.TestCase):
    def _gather(self, catalog_path=None):
        with mock.patch.object(inst.boot, "repo_slug", return_value="acme/widgets"):
            return inst.present_gather(catalog_path=catalog_path)

    def test_empty_catalog_shows_the_no_addons_line_and_choices(self):
        out = self._gather(None)
        self.assertIn(inst._EMPTY_CATALOG_LINE, out)
        self.assertIn("who reviews changes here", out, "the identity choice is presented")
        self.assertIn("will be removed from this project", out, "the destructive-on-confirm outcome is stated")

    def test_present_catalog_lists_the_command(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump([{"id": "x", "verb": "engine-x", "description": "Does x.",
                            "category": "Product Management"}], fh)
            out = self._gather(p)
            self.assertIn("engine-x — Does x.", out)
            self.assertIn("Product Management", out)

    def test_walkthrough_is_plain_language(self):
        out = self._gather(None)
        low = out.lower()
        for term in _FORBIDDEN:
            self.assertNotIn(term, low, f"plain-language law: '{term}' must not surface to the operator")


class TestConfirm(unittest.TestCase):
    def test_writes_required_plus_kept_omitting_unkept(self):
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            _module(d, "validators-core", "required")
            _module(d, "extras", "optional")
            with inst._redirect_root(d):
                res = inst.confirm(["extras"], "team", engine_release="2.0.0")
            man = res["manifest"]
            self.assertEqual(man["identity"], "team")
            self.assertEqual(man["engine_release"], "2.0.0")
            self.assertEqual(sorted(man["packages"]), ["core", "extras", "validators-core"],
                             "required spine always, plus the kept optional")
            # The written file is on disk and matches.
            with open(res["path"], encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), man)

    def test_unkept_optional_is_omitted_but_its_files_remain(self):
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            _module(d, "extras", "optional")
            with inst._redirect_root(d):
                res = inst.confirm([], "solo", engine_release="2.0.0")
            self.assertEqual(sorted(res["manifest"]["packages"]), ["core"],
                             "an unkept optional is left out of the manifest")
            self.assertTrue(os.path.isdir(os.path.join(d, ".engine", "modules", "extras")),
                            "CONFIRM deletes nothing — removal is a later phase")

    def test_confirm_makes_the_repo_provisioned(self):
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            self.assertFalse(inst.is_provisioned(d))
            with inst._redirect_root(d):
                inst.confirm([], "solo", engine_release="1.0.0")
            self.assertTrue(inst.is_provisioned(d), "after confirm the checkpoint exists")

    def test_rerun_keeps_the_existing_release_when_none_passed(self):
        # A re-run that passes no release reads the one the existing manifest recorded rather than resetting it.
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            inst._write_json(inst._engine_manifest_path(d),
                             {"engine_release": "9.9.9", "packages": {"core": "1.0.0"}, "identity": "solo"})
            with inst._redirect_root(d):
                res = inst.confirm([], "team")  # no engine_release → keep the recorded one
            self.assertEqual(res["manifest"]["engine_release"], "9.9.9", "a re-run keeps the recorded release")

    def test_manifest_with_no_id_does_not_crash_confirm(self):
        # Defense-in-depth on the committing step: a malformed (id-less) manifest is skipped, never a crash.
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            bad = os.path.join(d, ".engine", "modules", "bad")
            os.makedirs(bad)
            inst._write_json(os.path.join(bad, "manifest.json"), {"status": "required", "version": "1.0.0"})
            with inst._redirect_root(d):
                res = inst.confirm([], "solo", engine_release="1.0.0")
            self.assertEqual(sorted(res["manifest"]["packages"]), ["core"], "the id-less manifest is skipped")


class TestCatalogSchemaConformance(unittest.TestCase):
    def _schema(self):
        with open(os.path.join(validate.ENGINE_DIR, "schemas", "provisioning-catalog.v1.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)

    def test_committed_catalog_and_a_good_entry_validate(self):
        import jsonschema
        schema = self._schema()
        jsonschema.validate([], schema)  # the empty catalog this repo ships
        jsonschema.validate([{"id": "x-mod", "verb": "engine-x", "description": "Does x.",
                              "category": "Product Management", "status": "optional"}], schema)

    def test_missing_required_field_is_rejected(self):
        import jsonschema
        schema = self._schema()
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate([{"id": "x", "verb": "engine-x"}], schema)  # no description/category


class TestCLI(unittest.TestCase):
    def _run(self, argv):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = inst.main(argv)
        return rc, buf.getvalue()

    def test_demo_runs_green(self):
        rc, out = self._run(["demo"])
        self.assertEqual(rc, 0)
        self.assertIn("naming it, not hiding it", out, "the honest-ceiling banner leads the demo")

    def test_show_short_circuits_when_already_set_up(self):
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            inst._write_json(inst._engine_manifest_path(d),
                             {"engine_release": "1.0.0", "packages": {"core": "1.0.0"}, "identity": "solo"})
            with inst._redirect_root(d):
                rc, out = self._run(["show"])
            self.assertEqual(rc, 0)
            self.assertIn(inst._ALREADY_SET_UP, out, "a set-up project short-circuits, never re-offering setup")

    def test_show_presents_the_walkthrough_when_not_set_up(self):
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")  # no engine.json → not provisioned
            with inst._redirect_root(d), mock.patch.object(inst.boot, "repo_slug", return_value="acme/widgets"):
                rc, out = self._run([])
            self.assertEqual(rc, 0)
            self.assertIn("who reviews changes here", out, "an unset project shows the gather walkthrough")


if __name__ == "__main__":
    unittest.main()
