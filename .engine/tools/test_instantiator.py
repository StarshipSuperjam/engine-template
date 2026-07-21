"""Tests for the first-run setup orchestrator's GATHER + CONFIRM half.

Verifies: the not-set-up signal keys off the manifest's presence (no new state); identity is derived
best-effort and degrades when it can't be read; the optional features group by discipline in the fixed
order (empty when the catalog is empty; an unrecognized category is kept, not dropped); the walkthrough is
plain-language and states the destructive-on-confirm outcome; CONFIRM writes the manifest with the required
spine always plus the kept optionals (an unkept optional omitted, its files NOT touched here — deletion is a
later phase); the catalog the demo plants conforms to the catalog schema; and the demo runs green.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml  # the #416 uv-pin tie parses the CI workflows structurally (already a runtime dep)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import instantiator as inst  # noqa: E402
import license_seeds  # noqa: E402
import module_coherence  # noqa: E402
import self_map  # noqa: E402  (the retire-time map re-derive, #513)
import validate  # noqa: E402


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

    def test_command_less_entry_is_grouped_not_dropped(self):
        # A command-less optional module (no verb) is still a real choice — it groups under its discipline
        # for the setup menu rather than vanishing (#254).
        entries = [{"id": "lens", "description": "x", "category": "Verification & Validation"}]
        grouped = inst.selectable(entries)
        self.assertEqual(list(grouped.keys()), ["Verification & Validation"])
        self.assertEqual(grouped["Verification & Validation"][0]["id"], "lens",
                         "a command-less entry is grouped, not dropped")


class TestPresentGather(unittest.TestCase):
    def _gather(self, catalog_path=None):
        with mock.patch.object(inst.boot, "repo_slug", return_value="acme/widgets"):
            return inst.present_gather(catalog_path=catalog_path)

    def test_empty_catalog_shows_the_no_addons_line_and_choices(self):
        # Inject an explicitly empty catalog so this still tests the no-add-ons path now that the committed
        # catalog ships an optional module.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "empty.json")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("[]")
            out = self._gather(p)
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

    def test_command_less_module_listed_by_description_not_a_fake_command(self):
        # A command-less optional module (no verb) is offered by its plain description — never an empty "• —"
        # handle, and never its raw module id shown as a command a non-engineer can't actually type (#254).
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump([{"id": "design-review", "description": "Reviews your plans before you build.",
                            "category": "Verification & Validation"}], fh)
            out = self._gather(p)
            self.assertIn("• Reviews your plans before you build.", out, "offered by its description")
            self.assertNotIn("• —", out, "no empty command handle")
            self.assertNotIn("design-review", out, "the raw module id is never shown as a command")

    def test_menu_annotates_the_optional_dependency_closure(self):
        # #411: an optional feature that depends on ANOTHER optional feature surfaces that pull-in at the
        # choice moment. Synthetic manifests: optional 'a' depends on optional
        # 'b' (and on required 'core', which must NOT be surfaced).
        manifests = [
            ("a", {"id": "a", "status": "optional", "depends": {"b": "", "core": ""}}),
            ("b", {"id": "b", "status": "optional", "depends": {}}),
            ("core", {"id": "core", "status": "required", "depends": {}}),
        ]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump([{"id": "a", "verb": "engine-a", "description": "Does a.",
                            "category": "Product Management"}], fh)
            with mock.patch.object(inst.boot, "repo_slug", return_value="acme/widgets"):
                out = inst.present_gather(catalog_path=p, manifests=manifests)
        self.assertIn("Including this also turns on: b", out, "the optional pull-in is surfaced")
        self.assertNotIn("core", out, "an always-present required dependency is never surfaced")

    def test_welcome_orients_to_what_is_already_running(self):
        # The first-run welcome briefs the new operator (post-adoption, not a prospect) on the always-present
        # essentials — DESCRIBED, never offered as a choice — in the operator-onboarding voice and consistent
        # with the README's "What's inside" names. It must land AHEAD of the choices, and name capabilities,
        # never raw module ids. manifests=[] + a minimal catalog keep the assertion deterministic.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump([{"id": "x", "verb": "engine-x", "description": "Does x.",
                            "category": "Product Management"}], fh)
            with mock.patch.object(inst.boot, "repo_slug", return_value="acme/widgets"):
                out = inst.present_gather(catalog_path=p, manifests=[])
        self.assertIn("What's already running", out, "the welcome names what comes live automatically")
        for capability in ("Memory", "State", "Knowledge", "Attention", "review gate",
                           "Explore and Build", "boot briefing", "Unattended routines", "self-review"):
            self.assertIn(capability, out, f"the live section names {capability!r}")
        # Honest without over-claiming (plan-gate consent findings): memory backup is off by default, and
        # unattended routines never merge unwatched.
        self.assertIn("backup stays off until you ask", out, "backup is not implied to be on")
        self.assertIn("I never merge on my own", out, "unattended action is bounded by the review gate")
        # Described, never offered: the live essentials come before the choices, and no raw module id leaks.
        self.assertLess(out.index("What's already running"), out.index("Optional add-ons"),
                        "what's-live orients before the choices")
        self.assertNotIn("core", out, "capability copy names capabilities, never raw module ids")

    def test_optional_section_states_add_later_and_removable(self):
        # The honest lifecycle framing the operator asked for, kept simple and consistent with the README:
        # add later, removable — kept distinct from the delete-on-unkeep detail (never conflated into a
        # "dormant, toggle it back" illusion).
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump([{"id": "x", "verb": "engine-x", "description": "Does x.",
                            "category": "Product Management"}], fh)
            with mock.patch.object(inst.boot, "repo_slug", return_value="acme/widgets"):
                out = inst.present_gather(catalog_path=p, manifests=[])
        self.assertIn("you can add any later", out, "add-later is stated")
        self.assertIn("removable", out, "removability is stated")
        self.assertIn("fresh request, not a checkbox you flip back", out, "delete-on-unkeep detail is kept")


class TestOptionalDependencyClosure(unittest.TestCase):
    @staticmethod
    def _m(mid, status, deps):
        return (mid, {"id": mid, "status": status, "depends": {d: "" for d in deps}})

    def test_optional_on_optional_is_surfaced(self):
        closure = inst.optional_dependency_closure(
            [self._m("a", "optional", ["b"]), self._m("b", "optional", [])])
        self.assertEqual(closure["a"], ["b"])
        self.assertEqual(closure["b"], [])

    def test_required_dependencies_are_excluded(self):
        closure = inst.optional_dependency_closure(
            [self._m("a", "optional", ["core"]), self._m("core", "required", [])])
        self.assertEqual(closure["a"], [], "core is the always-present spine, never surfaced")
        self.assertNotIn("core", closure, "required modules are not keys in the optional closure")

    def test_transitive_optional_chain(self):
        closure = inst.optional_dependency_closure(
            [self._m("a", "optional", ["b"]), self._m("b", "optional", ["c"]),
             self._m("c", "optional", [])])
        self.assertEqual(closure["a"], ["b", "c"], "the closure is transitive")

    def test_live_manifests_are_vacuous(self):
        # Every optional module depends only on core today, so the live closure is all-empty — the mechanism
        # is armed but untriggered (shipped complete, not deferred).
        closure = inst.optional_dependency_closure(module_coherence.discover_manifests())
        self.assertTrue(closure, "there are optional modules to key on")
        self.assertTrue(all(pulls == [] for pulls in closure.values()),
                        "no optional module depends on another optional one yet")

    def test_confirm_folds_the_closure_so_a_kept_module_brings_its_optional_dep(self):
        # #411 (deliverable-gate fold): present_gather promises "keeping a also turns on b", so confirm
        # must WRITE b into the manifest when the operator keeps a — else the annotation lies and the apply
        # phase would halt on a missing-dependency coherence finding. Keep only 'a'; expect a, b, and core.
        manifests = [
            ("a", {"id": "a", "status": "optional", "version": "1.0.0", "depends": {"b": "", "core": ""}}),
            ("b", {"id": "b", "status": "optional", "version": "1.0.0", "depends": {}}),
            ("core", {"id": "core", "status": "required", "version": "1.0.0", "depends": {}}),
        ]
        with tempfile.TemporaryDirectory() as d:
            result = inst.confirm(["a"], "solo", root=d, engine_release="1.0.0", manifests=manifests)
        packages = result["manifest"]["packages"]
        self.assertIn("a", packages, "the operator's kept module")
        self.assertIn("b", packages, "its optional pull-in is folded in, honoring the gather annotation")
        self.assertIn("core", packages, "the required spine")


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

    def test_persists_the_derived_default_branch_when_known(self):
        # #342: the derived default-branch name is persisted as operator config in the manifest, so offline
        # classification reads a known name. It validates against the (closed) engine schema.
        import validate as _v
        schema = _v.load_json(os.path.join(_v.SCHEMAS_DIR, "engine.v1.json"))
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            with inst._redirect_root(d):
                res = inst.confirm([], "solo", engine_release="1.0.0", default_branch="trunk")
            self.assertEqual(res["manifest"]["default_branch"], "trunk")
            with open(res["path"], encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["default_branch"], "trunk")
            self.assertEqual(list(_v.Draft202012Validator(schema).iter_errors(res["manifest"])), [],
                             "the manifest with default_branch validates against the closed engine schema")

    def test_omits_default_branch_when_underivable(self):
        # Best-effort: when the default branch can't be derived (None), it is simply absent — manifest stays valid.
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            with inst._redirect_root(d):
                res = inst.confirm([], "solo", engine_release="1.0.0", default_branch=None)
            self.assertNotIn("default_branch", res["manifest"])

    def test_carries_the_recorded_home_forward_across_setup(self):
        # #367: the update home is seeded as data and carried across first-run setup (like the release),
        # so a generated repo keeps where its engine updates from. Validates against the closed engine schema.
        import validate as _v
        schema = _v.load_json(os.path.join(_v.SCHEMAS_DIR, "engine.v1.json"))
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            inst._write_json(inst._engine_manifest_path(d),
                             {"engine_release": "1.0.0", "packages": {"core": "1.0.0"}, "identity": "solo",
                              "home_repository": "acme/engine-template"})   # the traveled seed value
            with inst._redirect_root(d):
                res = inst.confirm([], "solo")   # no home param -> carried forward from the traveled manifest
            self.assertEqual(res["manifest"]["home_repository"], "acme/engine-template")
            with open(res["path"], encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["home_repository"], "acme/engine-template")
            self.assertEqual(list(_v.Draft202012Validator(schema).iter_errors(res["manifest"])), [],
                             "the manifest with home_repository validates against the closed engine schema")

    def test_omits_home_when_none_recorded(self):
        # A repo with no seeded home leaves the key out (manifest stays valid); the update path then
        # refuses-with-a-remedy rather than setup guessing a home.
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            with inst._redirect_root(d):
                res = inst.confirm([], "solo", engine_release="1.0.0")
            self.assertNotIn("home_repository", res["manifest"])

    def test_persists_an_external_product_repository(self):
        # eADR-0026: when the engine builds a repo DIFFERENT from the one it is deployed into, the operator's
        # named product is recorded. It validates against the (closed) engine schema.
        import validate as _v
        schema = _v.load_json(os.path.join(_v.SCHEMAS_DIR, "engine.v1.json"))
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            with inst._redirect_root(d):
                res = inst.confirm([], "solo", engine_release="1.0.0", product_repository="acme/upstream")
            self.assertEqual(res["manifest"]["product_repository"], "acme/upstream")
            with open(res["path"], encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["product_repository"], "acme/upstream")
            self.assertEqual(list(_v.Draft202012Validator(schema).iter_errors(res["manifest"])), [],
                             "the manifest with product_repository validates against the closed engine schema")

    def test_omits_product_when_self_building(self):
        # The common self-building case: no external product named -> the key is absent (the product is this
        # repo itself, derived live at read time), so the manifest never stores a derivable, rename-stale copy.
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            with inst._redirect_root(d):
                res = inst.confirm([], "solo", engine_release="1.0.0")
            self.assertNotIn("product_repository", res["manifest"])

    def test_carries_the_recorded_product_forward_across_setup(self):
        # Resume-safe: a re-run of confirm that does NOT re-supply the override must NOT clobber a recorded
        # external product with nothing — it is carried forward (the home_repository precedence).
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            inst._write_json(inst._engine_manifest_path(d),
                             {"engine_release": "1.0.0", "packages": {"core": "1.0.0"}, "identity": "solo",
                              "product_repository": "acme/upstream"})
            with inst._redirect_root(d):
                res = inst.confirm([], "solo")   # no product param -> carried forward, not overwritten
            self.assertEqual(res["manifest"]["product_repository"], "acme/upstream")

    def test_external_product_guard_reads_normalized_self_as_self(self):
        # The load-bearing "never store self" guard: an override that equals self — case-insensitively,
        # ignoring a .git suffix and surrounding whitespace — records NOTHING (self is derived live).
        for same in ("acme/widget", "Acme/Widget", "acme/widget.git", "  acme/widget  ", "ACME/WIDGET.git"):
            self.assertIsNone(inst._external_product_or_none(same, "acme/widget"),
                              f"{same!r} should read as self and store nothing")

    def test_external_product_guard_keeps_a_real_external_trimmed(self):
        self.assertEqual(inst._external_product_or_none("  other/upstream  ", "acme/widget"), "other/upstream")

    def test_external_product_guard_keeps_when_self_underivable(self):
        # origin unreadable -> self can't be proven; keep the trimmed value (conservative, display-only)
        self.assertEqual(inst._external_product_or_none(" other/x ", None), "other/x")

    def test_external_product_guard_none_for_empty_or_blank(self):
        self.assertIsNone(inst._external_product_or_none(None, "acme/widget"))
        self.assertIsNone(inst._external_product_or_none("   ", "acme/widget"))

    def test_derive_default_branch_prefers_gh_then_degrades_to_none(self):
        # gh is authoritative: a fake transport reports the repo's default branch (scoped to the given slug).
        self.assertEqual(
            inst.derive_default_branch(slug="o/r", gh_api=lambda _p: {"default_branch": "trunk"}), "trunk")
        # gh silent + a non-git tree with no origin/HEAD -> None (never a bare guess), persists nothing.
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(inst.derive_default_branch(root=d, slug="o/r", gh_api=lambda _p: None))


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

    def test_command_less_entry_validates_without_a_verb(self):
        # verb is optional now (#254): a command-less optional module omits it and still validates.
        import jsonschema
        schema = self._schema()
        jsonschema.validate([{"id": "design-review", "description": "Reviews plans.",
                              "category": "Verification & Validation"}], schema)

    def test_explicit_empty_verb_is_rejected(self):
        # A command-less module must OMIT verb, not set it to "" — a present-but-empty verb violates the
        # command pattern and is caught (the schema check fails loud rather than relaying a malformed entry).
        import jsonschema
        schema = self._schema()
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate([{"id": "x", "description": "d", "category": "Product Management",
                                  "verb": ""}], schema)


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

    def test_show_short_circuits_in_the_workshop_where_origin_equals_home(self):
        # The workshop (or any non-copy): manifest present AND origin == recorded home -> NOT a downstream copy
        # -> short-circuit "already set up", never re-offering setup. (#353: presence alone is not "provisioned".)
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            inst._write_json(inst._engine_manifest_path(d),
                             {"engine_release": "1.0.0", "packages": {"core": "1.0.0"}, "identity": "solo",
                              "home_repository": "acme/engine"})
            with inst._redirect_root(d), mock.patch.object(inst.boot, "repo_slug", return_value="acme/engine"):
                rc, out = self._run(["show"])
            self.assertEqual(rc, 0)
            self.assertIn(inst._ALREADY_SET_UP, out, "the workshop (origin == home) short-circuits")

    def test_show_offers_setup_on_a_downstream_copy_that_inherited_the_manifest(self):
        # The #353 fix: a fresh generated copy INHERITS the manifest, so is_provisioned() is true — but its origin
        # differs from the recorded upstream home, so it is a downstream copy still pending setup. show must fall
        # into the gather (offer setup) rather than short-circuit "already set up".
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")
            inst._write_json(inst._engine_manifest_path(d),
                             {"engine_release": "1.0.0", "packages": {"core": "1.0.0"}, "identity": "solo",
                              "home_repository": "StarshipSuperjam/engine-template"})
            with inst._redirect_root(d), mock.patch.object(inst.boot, "repo_slug",
                                                           return_value="adopter/their-product"):
                rc, out = self._run(["show"])
            self.assertEqual(rc, 0)
            self.assertNotIn(inst._ALREADY_SET_UP, out, "a downstream copy must NOT report 'already set up'")
            self.assertIn("who reviews changes here", out, "a downstream copy shows the setup gather instead")

    def test_show_presents_the_walkthrough_when_not_set_up(self):
        with tempfile.TemporaryDirectory() as d:
            _module(d, "core", "required")  # no engine.json → not provisioned
            with inst._redirect_root(d), mock.patch.object(inst.boot, "repo_slug", return_value="acme/widgets"):
                rc, out = self._run([])
            self.assertEqual(rc, 0)
            self.assertIn("who reviews changes here", out, "an unset project shows the gather walkthrough")


# The first-run setup tool is the ONE engine tool that must run BEFORE the tool-runtime it installs exists:
# it bootstraps uv, so it cannot presuppose the packages the runtime provides (yaml, jsonschema).
# This block runs in a subprocess with those two packages forced absent via a sys.meta_path finder — proving
# `import instantiator` and the show/demo CLI start on the Python standard library alone, deterministically
# (independent of whether THIS machine's Python happens to carry the packages — it does, so the block is
# mandatory, and the block-bites guard fails loudly if it ever stops working).
_STARTABILITY_SNIPPET = r"""
import sys
_BLOCK = {"yaml", "jsonschema"}
class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _BLOCK:
            raise ImportError("startability test: '%s' is blocked" % name)
        return None
for _m in [n for n in list(sys.modules) if n.split(".")[0] in _BLOCK]:
    del sys.modules[_m]
sys.meta_path.insert(0, _Blocker())
try:                                  # the block must actually bite, or the test is vacuous
    import yaml
    print("BLOCKER-INEFFECTIVE"); sys.exit(3)
except ImportError:
    pass
import io, contextlib
import instantiator                   # the import that used to transitively require the runtime
# `apply-demo` exercises the WHOLE apply chain (module_manager / wiring / bootstrap / knowledge_gen) with
# every boundary faked, `finish-demo` exercises the verify + retire close (check_coherence +
# knowledge_gen.generate, both JSON/walk-only), and `collision-demo` exercises the brownfield overlap check
# (engine_owned_paths + glob/fnmatch + the tolerant readers, all stdlib) — proving the heavy apply path, the
# lifecycle close, AND the overlap check all start on the standard library alone (the whole instantiator runs
# on the operator's system python).
for _argv in (["show"], ["demo"], ["apply-demo"], ["finish-demo"], ["collision-demo"]):
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        _rc = instantiator.main(_argv)
    assert _rc == 0, (_argv, _rc, _buf.getvalue()[-400:])
print("STARTABLE-OK")
"""


class TestStartabilityWithoutRuntime(unittest.TestCase):
    def _run_blocked(self):
        here = os.path.dirname(os.path.abspath(__file__))
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}  # don't lean on a venv site path
        return subprocess.run([sys.executable, "-c", _STARTABILITY_SNIPPET],
                              cwd=here, env=env, capture_output=True, text=True)

    def test_import_and_cli_start_without_yaml_or_jsonschema(self):
        proc = self._run_blocked()
        self.assertNotIn("BLOCKER-INEFFECTIVE", proc.stdout,
                         "the deps blocker stopped biting — the startability test would be vacuous")
        self.assertIn("STARTABLE-OK", proc.stdout,
                      "the setup tool must import and run `show`/`demo` with yaml+jsonschema absent "
                      "(it bootstraps the runtime that provides them).\n"
                      f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_runbook_launch_command_string_runs(self):
        # The verbatim command the runbook/skill tell the operator to type must actually run (from repo root,
        # the skill's cwd) — not just the underlying main() call.
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        proc = subprocess.run([sys.executable, ".engine/tools/instantiator.py", "show"],
                              cwd=root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # In this construction repo the manifest is present, so the verb short-circuits "already set up".
        self.assertIn(inst._ALREADY_SET_UP.split("\n")[0], proc.stdout, proc.stdout)


def _confirmed_fixture(tmp, handle="octocat", keep=None):
    """Build a generated-repo fixture and confirm it (write the checkpoint), returning under a redirected
    root. Caller is responsible for the surrounding `with inst._redirect_root(tmp)`."""
    inst.confirm(keep or [], "solo", engine_release="1.0.0", handle=handle)


def _fake_apply(tmp, **overrides):
    """Run apply with every external boundary faked (no network, no machine writes, no real uv): the happy
    defaults, overridable per-test."""
    base = dict(
        announce=lambda t: None,
        home_reader=lambda: {},                                  # no global preference → adopt plan
        uv_present=lambda: None,                                 # uv not yet present
        uv_installer=lambda: os.path.join(tmp, ".engine", ".uv", "uv"),
        uv_runner=lambda uv, groups: True,
        consent=lambda kind: True,
        control_transport=inst._approve_transport(),
        gh_refresh=lambda s: True,
        control_issues=inst._FakeIssues(),
        control_repo="you/your-project",                         # injected so the control-plane step is
        control_token="tok",                                     # deterministic — never the CI/ambient token
    )
    base.update(overrides)
    return inst.apply(**base)


class TestApplyOrchestrator(unittest.TestCase):
    def test_refuses_when_not_confirmed(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                res = inst.apply(announce=lambda t: None)   # no manifest written
            self.assertTrue(res["refused"])
            self.assertEqual(res["reason"], "not-confirmed")
            self.assertEqual(res["steps"], [])

    def test_full_happy_path_runs_every_step_in_order(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d)
            self.assertFalse(res["refused"]); self.assertFalse(res["halted"])
            names = [s["step"] for s in res["steps"]]
            self.assertEqual(names, ["remove-unselected", "foundation-ignores", "codeowners", "plan-mode",
                                     "tool-runtime", "substrates", "wires", "control-plane",
                                     "actions-enablement", "security-floor", "repo-behavior"])
            behavior = next(s for s in res["steps"] if s["step"] == "repo-behavior")
            self.assertEqual(behavior["status"], "applied", behavior)

    def test_handle_falls_back_to_the_manifest_value(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d, handle="manifest-owner")
                res = _fake_apply(d, handle=None)   # not passed → read from the manifest
                co = inst.validate.read(os.path.join(d, ".github", "CODEOWNERS"))
            self.assertIn("@manifest-owner", co)


class TestApplyActionsEnablement(unittest.TestCase):
    """#514: the one-time GitHub Actions enablement is the OWNER's click — first-run tells,
    unconditionally, and never silently automates. No detection buys silence: the review proved every
    candidate signal dishonest in exactly the deadlock state (GitHub-managed scan runs appear while real
    workflows stay gated; run history proves the past, not the present) — so the message itself carries
    the already-on branch, and this class asserts the telling is truly unconditional."""

    def test_tells_the_operator_whenever_the_project_is_connected(self):
        said = []
        step = inst._apply_actions_enablement(None, said.append, inst.load_copy(),
                                              repo="you/your-project", token="tok")
        self.assertEqual(step, {"step": "actions-enablement", "status": "operator-step-told"})
        blob = " ".join(said).replace("\n", " ")
        self.assertIn("Actions tab", blob)
        self.assertIn("already on and you're done", blob)      # the already-enabled reader has a branch
        self.assertIn("fresh nudge", blob)                     # and the clicked-but-still-waiting reader too

    def test_production_shape_transport_is_none_and_never_consulted(self):
        # Both real entry paths hand this step control_transport=None (a review caught the first draft
        # probing it and dying silently) — the step must behave identically with no transport at all.
        step = inst._apply_actions_enablement(None, lambda _s: None, inst.load_copy(),
                                              repo="you/your-project", token="tok")
        self.assertEqual(step["status"], "operator-step-told")

    def test_no_project_or_signin_skips_quietly(self):
        said = []
        # An empty arg falls back to the ambient repo/token resolution, so fake THAT boundary too —
        # otherwise this test would resolve the construction repo's own live coordinates.
        with mock.patch.object(inst.boot, "repo_slug", return_value=""), \
             mock.patch.object(inst.boot, "gh_token", return_value=""):
            step = inst._apply_actions_enablement(None, said.append,
                                                  inst.load_copy(), repo=None, token=None)
        self.assertEqual(step["status"], "skipped")
        self.assertEqual(said, [])


class TestApplyRepoBehavior(unittest.TestCase):
    """#541: the repo-behavior step mirrors the security floor's posture — quiet skip without a
    project/sign-in, honest degraded status when a setting doesn't confirm."""

    def test_no_project_or_signin_skips_quietly(self):
        said = []
        with mock.patch.object(inst.boot, "repo_slug", return_value=""), \
             mock.patch.object(inst.boot, "gh_token", return_value=""):
            step = inst._apply_repo_behavior(lambda m, p, b=None: (200, {}, {}), said.append,
                                             inst.load_copy(), repo=None, token=None)
        self.assertEqual(step["status"], "skipped")
        self.assertEqual(said, [])

    def test_unconfirmed_setting_degrades_the_step_honestly(self):
        def t(method, path, body=None):   # a GitHub that answers nothing usable
            return 500, None, {}
        step = inst._apply_repo_behavior(t, lambda _s: None, inst.load_copy(),
                                         repo="you/your-project", token="tok")
        self.assertEqual(step["status"], "degraded")
        self.assertIn("toggles", step)

    def test_org_reserved_switch_is_an_honest_outcome_not_a_degrade(self):
        # An organization-reserved Dependabot switch is disclosed, never forced — and the step's ledger
        # reads "applied" (the leg did its whole job), not "degraded".
        def t(method, path, body=None):
            if path.endswith("/vulnerability-alerts") or path.endswith("/automated-security-fixes"):
                return (403, {"message": "org policy"}, {}) if method == "PUT" else (404, None, {})
            if method == "GET" and path.startswith("/repos/"):
                return 200, {"delete_branch_on_merge": True, "allow_update_branch": True}, {}
            return 404, None, {}
        step = inst._apply_repo_behavior(t, lambda _s: None, inst.load_copy(),
                                         repo="you/your-project", token="tok")
        self.assertEqual(step["status"], "applied")
        self.assertEqual(step["toggles"]["dependabot-alerts"], "unsupported")

    def _fresh_repo_transport(self):
        # A fresh repo: the four comfort settings enable-and-confirm; wiki + project boards start ON.
        state = {"has_wiki": True, "has_projects": True,
                 "delete_branch_on_merge": False, "allow_update_branch": False}
        alerts, fixes = {"on": False}, {"on": False}

        def t(method, path, body=None):
            if path.endswith("/vulnerability-alerts"):
                if method == "PUT":
                    alerts["on"] = True; return 204, None, {}
                return (204, None, {}) if alerts["on"] else (404, None, {})
            if path.endswith("/automated-security-fixes"):
                if method == "PUT":
                    fixes["on"] = True; return 204, None, {}
                return 200, {"enabled": fixes["on"]}, {}
            if method == "PATCH" and isinstance(body, dict):
                state.update(body); return 200, {}, {}
            if method == "GET" and path.startswith("/repos/"):
                return 200, dict(state, full_name="you/your-project"), {}
            return 404, None, {}
        return t

    def test_greenfield_turns_off_wiki_and_projects_when_module_absent(self):
        # #541 item 4: fresh repo, github-projects-sync NOT installed -> both wiki and project boards off.
        with mock.patch.object(inst, "_github_projects_sync_present", return_value=False):
            step = inst._apply_repo_behavior(self._fresh_repo_transport(), lambda _s: None, inst.load_copy(),
                                             repo="you/your-project", token="tok", brownfield=False)
        self.assertEqual(step["status"], "applied")
        self.assertEqual(step["toggles"]["wiki"], "off")
        self.assertEqual(step["toggles"]["projects"], "off")

    def test_greenfield_retains_projects_when_module_present(self):
        # The board-sync module is installed -> project boards are RETAINED (not touched); wiki still off.
        with mock.patch.object(inst, "_github_projects_sync_present", return_value=True):
            step = inst._apply_repo_behavior(self._fresh_repo_transport(), lambda _s: None, inst.load_copy(),
                                             repo="you/your-project", token="tok", brownfield=False)
        self.assertEqual(step["status"], "applied")
        self.assertEqual(step["toggles"]["wiki"], "off")
        self.assertNotIn("projects", step["toggles"], "retained project boards are left untouched")

    def test_brownfield_skips_the_turnoffs(self):
        # A brownfield arrival never overrides the operator's own wiki/projects choices.
        with mock.patch.object(inst, "_github_projects_sync_present", return_value=False):
            step = inst._apply_repo_behavior(self._fresh_repo_transport(), lambda _s: None, inst.load_copy(),
                                             repo="you/your-project", token="tok", brownfield=True)
        self.assertEqual(step["status"], "applied")
        self.assertNotIn("wiki", step["toggles"])
        self.assertNotIn("projects", step["toggles"])


class TestApplyStep1DeleteUnselected(unittest.TestCase):
    def test_deletes_the_unkept_module_keeps_required(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)                       # core (required) + extras-demo (optional)
            with inst._redirect_root(d):
                _confirmed_fixture(d, keep=[])           # keep nothing optional → extras-demo deselected
                res = _fake_apply(d)
            step = res["steps"][0]
            self.assertEqual(step["deleted"], ["extras-demo"])
            self.assertFalse(os.path.isdir(os.path.join(d, ".engine", "modules", "extras-demo")))
            self.assertTrue(os.path.isdir(os.path.join(d, ".engine", "modules", "core")), "required spine kept")

    def test_reverse_dependency_refusal_is_recorded_not_crashed(self):
        # A kept module depends on an unkept one → remove() refuses; apply records it and continues (the kept
        # module and its dependency both remain — coherent — and the phase never crashes).
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            _module(d, "depend200", "optional")
            # 'core' (kept, required) depends on 'depend200' (not kept) → removing depend200 is refused.
            inst._write_json(os.path.join(d, ".engine", "modules", "core", "manifest.json"),
                             {"id": "core", "version": "1.0.0", "status": "required",
                              "provides": {}, "wires": [], "depends": {"depend200": "*"}})
            with inst._redirect_root(d):
                _confirmed_fixture(d, keep=[])           # neither extras-demo nor depend200 kept
                res = _fake_apply(d)
            step = res["steps"][0]
            refused_ids = {r["id"] for r in step["refused"]}
            self.assertIn("depend200", refused_ids, "a still-depended-on module is refused, not deleted")
            self.assertTrue(os.path.isdir(os.path.join(d, ".engine", "modules", "depend200")), "kept on refusal")
            self.assertFalse(res["halted"], "a refusal records-and-continues; it never halts the phase")


class TestApplyStepFoundationIgnores(unittest.TestCase):
    """#409: the apply step places the keyed foundation `.gitignore` fence. It runs BEFORE codeowners (so
    the file exists when the ownership set globs it) and pre-runtime (so a tool-runtime halt still leaves
    `.venv/` ignored), preserves the operator's own lines, and is idempotent."""

    def _gi(self, d):
        return inst._read_text_or(os.path.join(d, ".gitignore"), "")

    def _names(self, res):
        return [s["step"] for s in res["steps"]]

    def test_greenfield_places_the_foundation_fence_with_the_three_lines(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d)
            gi = self._gi(d)
        step = next(s for s in res["steps"] if s["step"] == "foundation-ignores")
        self.assertIn(step["status"], ("written", "already"))
        self.assertIn("BEGIN engine-managed block: foundation-ignores", gi)
        for line in inst.wiring.FOUNDATION_IGNORE_LINES:
            self.assertIn(line, gi)

    def test_runs_before_codeowners_and_the_tool_runtime(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d)
            names = self._names(res)
        self.assertLess(names.index("foundation-ignores"), names.index("codeowners"))
        self.assertLess(names.index("foundation-ignores"), names.index("tool-runtime"))

    def test_brownfield_operator_ignore_lines_are_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                with open(os.path.join(d, ".gitignore"), "w", encoding="utf-8") as fh:
                    fh.write("# my own\nnode_modules/\n")             # operator content, no engine fence
                _fake_apply(d)
                gi = self._gi(d)
        self.assertIn("node_modules/", gi, "the operator's own ignore lines survive")
        self.assertIn("BEGIN engine-managed block: foundation-ignores", gi)

    def test_idempotent_second_apply_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                _fake_apply(d)
                first = self._gi(d)
                res2 = _fake_apply(d)
                second = self._gi(d)
            status2 = next(s for s in res2["steps"] if s["step"] == "foundation-ignores")["status"]
        self.assertEqual(status2, "already")
        self.assertEqual(first, second, "a resumed apply never rewrites the foundation fence")


class TestApplyStep2Codeowners(unittest.TestCase):
    def test_writes_block_and_owns_itself(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d, handle="acme")
                res = _fake_apply(d)
                co = inst.validate.read(os.path.join(d, ".github", "CODEOWNERS"))
            self.assertEqual(res["steps"][2]["status"], "written")   # step 2: codeowners (foundation-ignores is 1)
            self.assertIn("/.github/CODEOWNERS @acme", co, "the block owns itself from the first render")
            self.assertIn("/.engine/engine.json @acme", co)

    def test_degrades_without_a_handle(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d, handle=None)       # confirm wrote no handle
                res = _fake_apply(d, handle=None)
            step = res["steps"][2]                       # step 2: codeowners (foundation-ignores is 1)
            self.assertEqual(step["status"], "degraded")
            self.assertFalse(os.path.isfile(os.path.join(d, ".github", "CODEOWNERS")), "no render without a handle")
            self.assertFalse(res["halted"], "a missing handle degrades, never halts")


class TestApplyStep3PlanMode(unittest.TestCase):
    def _mode(self, tmp):
        return (inst._read_json_or(os.path.join(tmp, ".claude", "settings.json"), {})
                .get("permissions", {}).get("defaultMode"))

    def test_adopts_when_no_global_preference(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, home_reader=lambda: {})
            self.assertEqual(self._plan_step(res)["status"], "adopted")
            self.assertEqual(self._mode(d), "plan")

    def test_conflict_keeps_operator_default_when_declined(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, home_reader=lambda: {"permissions": {"defaultMode": "acceptEdits"}},
                                  consent=lambda kind: False)        # operator declines adopt
            self.assertEqual(self._plan_step(res)["status"], "kept-operator-default")
            self.assertIsNone(self._mode(d), "keep writes nothing — the project key stays unset (the yield)")

    def test_conflict_adopts_when_operator_chooses(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, home_reader=lambda: {"permissions": {"defaultMode": "acceptEdits"}},
                                  consent=lambda kind: kind == "plan-mode-adopt")
            self.assertEqual(self._plan_step(res)["status"], "adopted")
            self.assertEqual(self._mode(d), "plan")

    def test_never_writes_home_settings(self):
        # The yield-to-the-operator law: ~/.claude is read-only. We assert by giving a home_reader that would
        # raise if WRITTEN (it is a pure value), and checking the project — but the strongest guard is that
        # apply has no path that writes the home file; the conflict path writes nothing at all.
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            calls = {"n": 0}
            def reader():
                calls["n"] += 1
                return {"permissions": {"defaultMode": "acceptEdits"}}
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                _fake_apply(d, home_reader=reader, consent=lambda kind: False)
            self.assertGreaterEqual(calls["n"], 1, "the global default is READ")

    # --- #409: a pre-existing PROJECT-level non-plan defaultMode is a conflict too (not silently
    #     overwritten), detected independently of the global value. The plan-mode step is found by NAME (its
    #     positional index shifts when the foundation-ignores step is inserted ahead of it).
    def _plan_step(self, res):
        return next(s for s in res["steps"] if s["step"] == "plan-mode")

    def _set_project_mode(self, d, mode):
        inst.wiring._write_json(os.path.join(d, ".claude", "settings.json"),
                                {"permissions": {"defaultMode": mode}})

    def test_project_scalar_conflict_keeps_the_committed_value_when_declined(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                self._set_project_mode(d, "acceptEdits")           # the operator's OWN committed project default
                res = _fake_apply(d, home_reader=lambda: {}, consent=lambda kind: False)
            self.assertEqual(self._plan_step(res)["status"], "kept-operator-default")
            self.assertEqual(self._mode(d), "acceptEdits",
                             "keep leaves the operator's committed project value exactly as it was")

    def test_project_scalar_conflict_is_independent_of_a_plan_global(self):
        # The bug this fixes: home=plan, project=acceptEdits fell straight through to a silent overwrite. A
        # global preference (even 'plan') must never license overwriting a value committed in THIS repo.
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                self._set_project_mode(d, "acceptEdits")
                res = _fake_apply(d, home_reader=lambda: {"permissions": {"defaultMode": "plan"}},
                                  consent=lambda kind: False)
            self.assertEqual(self._plan_step(res)["status"], "kept-operator-default")
            self.assertEqual(self._mode(d), "acceptEdits", "the committed project value is preserved")

    def test_project_scalar_conflict_adopts_replaces_the_committed_value(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                self._set_project_mode(d, "acceptEdits")
                res = _fake_apply(d, home_reader=lambda: {},
                                  consent=lambda kind: kind == "plan-mode-adopt")
            self.assertEqual(self._plan_step(res)["status"], "adopted")
            self.assertEqual(self._mode(d), "plan", "on adopt the committed value is replaced with plan")

    def test_project_scalar_already_plan_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                self._set_project_mode(d, "plan")
                res = _fake_apply(d, home_reader=lambda: {"permissions": {"defaultMode": "acceptEdits"}})
            self.assertEqual(self._plan_step(res)["status"], "already")


class TestApplyStep4ToolRuntime(unittest.TestCase):
    def test_materializes_when_present_and_synced(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, uv_present=lambda: "/usr/bin/uv", uv_runner=lambda uv, g: True)
            runtime = next(s for s in res["steps"] if s["step"] == "tool-runtime")
            self.assertEqual(runtime["status"], "materialized")
            self.assertFalse(res["halted"])

    def test_halts_without_consent_to_install(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, uv_present=lambda: None, consent=lambda kind: False)
            self.assertTrue(res["halted"])
            self.assertEqual(res["steps"][-1]["step"], "tool-runtime")
            self.assertEqual(len(res["steps"]), 5, "the post-runtime steps are not attempted on a halt "
                             "(remove-unselected, foundation-ignores, codeowners, plan-mode, tool-runtime)")

    def test_halts_when_install_fails(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, uv_present=lambda: None, uv_installer=lambda: None)
            self.assertTrue(res["halted"])
            self.assertEqual(res["steps"][-1]["status"], "degraded")

    def test_halts_when_sync_fails_never_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, uv_present=lambda: "/usr/bin/uv", uv_runner=lambda uv, g: False)
            self.assertTrue(res["halted"])
            names = [s["step"] for s in res["steps"]]
            self.assertNotIn("substrates", names, "no substrate init on a degraded runtime (never system python)")

    def test_install_uses_unmanaged_path_and_pinned_versioned_url(self):
        # The real installer command (faked everywhere else) must use the PATH-independent unmanaged install
        # and the version-pinned official URL — the deployed supply-chain contract.
        self.assertIn("UV_UNMANAGED_INSTALL", inst._install_uv.__doc__ or "")
        self.assertEqual(inst.UV_INSTALL_URL, f"https://astral.sh/uv/{inst.UV_PIN}/install.sh")

    def test_uv_pin_ties_to_every_ci_workflow_setup_uv_version(self):
        # #416: the instantiator's UV_PIN and every CI workflow's astral-sh/setup-uv `version:` must
        # agree, or a one-sided bump silently ships a bootstrap runtime that mismatches the engine's resolved
        # uv.lock. This test IS that tie — it reads the real workflow files and asserts each setup-uv step
        # pins UV_PIN. It lives HERE, in the instantiator's own tests, on purpose: UV_PIN is bootstrap-only,
        # and instantiator.py + this test file both RETIRE at first-run (_FIRST_RUN_ASSET_FILES), so the tie
        # is construction-coupled — the concern (a new adopter's bootstrap runtime matches uv.lock) exists
        # only while the instantiator does. #411 weighed a first-class traveling check for this and found
        # it INFEASIBLE: a surviving validators-core check reading UV_PIN would reference the retired
        # instantiator (first-run reference-closure), and the tie is moot once the instantiator is gone. So the
        # construction-coupled unittest is the correct form. Parsed via the YAML structure (jobs->steps->uses/
        # with), NOT a bare `version:` scan, so it never false-hits an unrelated input or a comment.
        wf_dir = os.path.join(validate.ROOT, ".github", "workflows")
        checked = []  # (filename, version) for every setup-uv step reached through the parsed structure
        for fn in sorted(f for f in os.listdir(wf_dir) if f.endswith((".yml", ".yaml"))):
            with open(os.path.join(wf_dir, fn), encoding="utf-8") as fh:
                raw = fh.read()
            doc = yaml.safe_load(raw) or {}
            found_here = []
            for job in (doc.get("jobs") or {}).values():
                for step in (job.get("steps") or []):
                    if ((step or {}).get("uses") or "").startswith("astral-sh/setup-uv"):
                        found_here.append((step.get("with") or {}).get("version"))
            # Fail LOUDLY on a parser miss: a file that textually uses setup-uv must yield a parsed version,
            # else the tie would silently pass over an unchecked pin.
            if "astral-sh/setup-uv" in raw:
                self.assertTrue(found_here, f"{fn}: uses astral-sh/setup-uv but no step parsed from its structure")
            for version in found_here:
                self.assertIsNotNone(version, f"{fn}: an astral-sh/setup-uv step carries no `version:` input")
                checked.append((fn, version))
        # Not vacuous — at least one workflow pins uv — and every pin agrees with the instantiator constant.
        self.assertTrue(checked, "no astral-sh/setup-uv version found in any workflow — the tie would be vacuous")
        for fn, version in checked:
            self.assertEqual(
                version, inst.UV_PIN,
                f"{fn} pins uv {version!r} but instantiator.UV_PIN is {inst.UV_PIN!r} — a one-sided bump; "
                f"bump both the instantiator constant and every workflow `version:` together")


class TestApplyStep6WiresInstallsHooks(unittest.TestCase):
    def test_apply_installs_hooks_not_only_the_query_server(self):
        # B1: the apply phase must wire ALL of a kept module's wires — the HOOKS that boot/gate/close the
        # engine, not only the MCP server. A hook-less generated repo is otherwise an inert engine.
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)                      # ships a HOOK-LESS settings.json
            settings_before = inst._read_json_or(os.path.join(d, ".claude", "settings.json"), {})
            self.assertNotIn("hooks", settings_before, "the fixture models a published, un-wired template")
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                _fake_apply(d)
                after = inst._read_json_or(os.path.join(d, ".claude", "settings.json"), {})
                mcp = inst._read_json_or(os.path.join(d, ".mcp.json"), {})
            self.assertIn("hooks", after, "apply must install the engine's hooks")
            for event in ("SessionStart", "PreToolUse", "Stop"):
                self.assertIn(event, after["hooks"], f"the {event} hook must be wired")
            self.assertIn("engine-knowledge-graph", mcp.get("mcpServers", {}), "and the query server too")


class TestApplyStep7ControlPlane(unittest.TestCase):
    def test_applied_turns_the_gate_on(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, control_transport=inst._approve_transport())
            cp = inst._step(res["steps"], "control-plane")   # the gate is no longer the LAST step
            self.assertEqual(cp["status"], "applied"); self.assertTrue(cp["protected"])

    def test_degraded_never_pretends_and_phase_ends(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, control_transport=inst._defer_transport(), gh_refresh=lambda s: False)
            cp = inst._step(res["steps"], "control-plane")
            self.assertEqual(cp["status"], "degraded"); self.assertFalse(cp["protected"])
            self.assertFalse(res["halted"], "a deferred gate ends the phase cleanly; it never halts")

    def test_degraded_when_no_repo_or_token(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d), \
                 mock.patch.object(inst.boot, "repo_slug", return_value=None), \
                 mock.patch.object(inst.boot, "gh_token", return_value=None):
                _confirmed_fixture(d)
                # opt out of the injected coordinates so the no-repo/no-sign-in path is exercised
                res = _fake_apply(d, control_repo=None, control_token=None)
            cp = inst._step(res["steps"], "control-plane")
            self.assertEqual(cp["status"], "degraded")
            self.assertIn("no project", cp["detail"])

    def test_brownfield_augments_a_product_ruleset_and_records_the_marker(self):
        # A brownfield repo whose OWN ruleset (id 9) already protects main. The control-plane step must
        # AUGMENT it in place (not create a second) and record the marker in engine.json so a later removal
        # can reverse exactly what it added. This drives the augment path end-to-end through apply().
        product = {"id": 9, "name": "team rules", "target": "branch", "enforcement": "active",
                   "bypass_actors": [], "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"],
                                                                     "exclude": []}},
                   "rules": [{"type": "pull_request",
                              "parameters": {"required_review_thread_resolution": True}},
                             {"type": "required_status_checks",
                              "parameters": {"required_status_checks": [{"context": "product-ci"}]}},
                             {"type": "non_fast_forward"}, {"type": "deletion"}]}
        store = {9: product}

        def transport(method, path, body=None):
            h = {"X-OAuth-Scopes": "repo"}
            if method == "GET" and path.endswith("/rules/branches/main"):
                rules = []
                for rid, rs in store.items():
                    for r in rs["rules"]:
                        rules.append({**r, "ruleset_id": rid, "ruleset_source_type": "Repository"})
                return 200, rules, h
            if method == "GET" and path.endswith("/rulesets"):
                return 200, [{"id": rid, "name": rs["name"]} for rid, rs in store.items()], h
            if method == "GET" and "/rulesets/" in path:
                return 200, dict(store[int(path.rsplit("/", 1)[1])]), h
            if method == "PUT" and "/rulesets/" in path:
                rid = int(path.rsplit("/", 1)[1])
                store[rid] = {**store[rid], **body, "id": rid}
                return 200, {"id": rid}, h
            if path.startswith("/repos/") and "/ruleset" not in path and "/rules" not in path:
                return 200, {"full_name": "you/your-project"}, h
            return 404, None, h

        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                res = _fake_apply(d, control_transport=transport)
                manifest = json.loads(validate.read(inst._engine_manifest_path()))
            cp = inst._step(res["steps"], "control-plane")
            self.assertEqual(cp["mode"], "augmented")
            self.assertEqual(len(store), 1, "augmented in place — no second ruleset created")
            self.assertEqual(manifest["control_plane"]["ruleset_mode"], "augmented")
            self.assertEqual(manifest["control_plane"]["augmented_ruleset_id"], 9)
            self.assertEqual(set(manifest["control_plane"]["added"]["checks"]),
                             set(inst.bootstrap.protection_guard.REQUIRED_CHECKS))

    def test_marker_persist_is_a_noop_for_a_read_only_outcome(self):
        # 'already'/degraded carry no marker; engine.json must be left without a control_plane key.
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                inst._persist_control_plane_marker(d, None)
                manifest = json.loads(validate.read(inst._engine_manifest_path()))
            self.assertNotIn("control_plane", manifest)


class TestApplyIdempotentResume(unittest.TestCase):
    def test_rerun_no_ops_the_writing_steps(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                _fake_apply(d, control_transport=inst._approve_transport())
                second = _fake_apply(d, control_transport=inst._already_transport())
            by = {s["step"]: s["status"] for s in second["steps"]}
            self.assertFalse(second["halted"])
            self.assertEqual(by["codeowners"], "already", "a resumed render is a true no-op")
            self.assertEqual(by["plan-mode"], "already")
            self.assertEqual(by["control-plane"], "already")


class TestApplyIsolation(unittest.TestCase):
    def test_apply_under_redirect_leaves_real_files_untouched(self):
        # The construction repo's own engine files must be byte-for-byte unchanged by a redirected apply —
        # the demo's mechanical isolation guarantee, as a test (catches a path constant escaping the fixture).
        snap = inst._snapshot_real_files()
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                _confirmed_fixture(d)
                _fake_apply(d)
        self.assertTrue(inst._assert_real_files_unchanged(snap), "a redirected apply must not touch real files")


class TestDeriveHandle(unittest.TestCase):
    def test_returns_login_on_success(self):
        fake = mock.Mock(returncode=0, stdout="octocat\n")
        with mock.patch("subprocess.run", return_value=fake):
            self.assertEqual(inst.derive_handle(), "octocat")

    def test_returns_none_when_gh_absent_or_empty(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(inst.derive_handle())
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stdout="")):
            self.assertIsNone(inst.derive_handle())


class TestApplyCopySurface(unittest.TestCase):
    def test_template_carries_every_apply_section(self):
        # Read the template's headings DIRECTLY — load_copy() silently substitutes the built-in fallback
        # for a missing section, so going through it made this test vacuous on the template side (a review
        # of #514 proved it by deleting a section and watching this stay green). The name is now true.
        with open(inst.TEMPLATE_PATH, encoding="utf-8") as fh:
            template = fh.read()
        for key, heading in inst.COPY_HEADINGS.items():
            self.assertIn(f"## {heading}\n", template,
                          f"apply copy section {key!r} ({heading!r}) missing from the template")
        copy = inst.load_copy(inst.TEMPLATE_PATH)
        for key in inst.COPY_HEADINGS:
            self.assertTrue(copy[key].strip(), f"apply copy section {key!r} resolves empty")

    def test_missing_template_falls_back_not_crashes(self):
        copy = inst.load_copy("/no/such/first-run.md")
        self.assertEqual(copy["tool-runtime-consent"], inst.FALLBACK_COPY["tool-runtime-consent"])


class TestApplyChainRunsOnSystemPython39(unittest.TestCase):
    # The apply phase runs on the operator's SYSTEM python (3.9 on macOS) BEFORE it installs the 3.11+
    # runtime. An evaluated `X | None` annotation raises there; `from __future__ import annotations` defers
    # it. Hold every tool the instantiator's apply chain imports to that, so a future edit can't silently
    # re-break a real adopter's first run (bootstrap.py was the gap this slice closed).
    _APPLY_CHAIN = ("instantiator", "module_manager", "wiring", "bootstrap", "security_floor",
                    "knowledge_gen", "module_coherence", "boot", "telemetry", "protection_guard",
                    "hooks", "module_catalog", "validate", "modes", "close")

    def test_every_apply_chain_tool_defers_annotations(self):
        here = os.path.dirname(os.path.abspath(__file__))
        missing = []
        for name in self._APPLY_CHAIN:
            with open(os.path.join(here, name + ".py"), encoding="utf-8") as fh:
                if "from __future__ import annotations" not in fh.read():
                    missing.append(name)
        self.assertEqual(missing, [], f"system-python-launched tools must defer annotations: {missing}")


# ==== the front-door root-README seed/replace (issue #133) ==============================

def _seed_readme_root(tmp, *, readme=None, seed=None):
    """Plant a fixture root for a _seed_readme test: optionally a root README (its exact text) and the
    .engine/provisioning/readme-seed.md starter (its exact text). Either may be omitted to model the
    absent-file cases. The caller holds the surrounding inst._redirect_root(tmp)."""
    if readme is not None:
        with open(os.path.join(tmp, "README.md"), "w", encoding="utf-8") as fh:
            fh.write(readme)
    if seed is not None:
        os.makedirs(os.path.join(tmp, ".engine", "provisioning"), exist_ok=True)
        with open(os.path.join(tmp, ".engine", "provisioning", "readme-seed.md"), "w", encoding="utf-8") as fh:
            fh.write(seed)


_MARKED_FRONT = inst._MARKETING_SEED_MARKER + "\n\n# engine-template\n"   # the traveled marketing landing front


class TestFirstRunWalksThroughMcpApproval(unittest.TestCase):
    """#400 F2: the first-run procedure walks the operator through turning on the engine's live helpers —
    approving the engine-memory + engine-knowledge-graph servers and restarting Claude. first-run.md is itself
    a first-run asset (retired after setup), so this is construction-scoped: it skips once the walkthrough has
    been tidied away (the deployed post-retire tree), exactly as the reference-closure gate no-ops then."""

    def test_first_run_names_the_two_servers_and_the_approve_plus_restart_fix(self):
        path = os.path.join(validate.ROOT, ".engine", "operations", "first-run.md")
        if not os.path.exists(path):
            self.skipTest("first-run.md already retired (deployed post-setup tree)")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("engine-memory", text)                 # both live helpers named
        self.assertIn("engine-knowledge-graph", text)
        self.assertIn("approve", text.lower())               # the approval half of the fix
        self.assertIn("reopen Claude", text)                 # the restart half (host-agnostic)
        self.assertIn("committed-file fallback", text)       # names the fallback state the operator is in


class TestReadmeRecognizer(unittest.TestCase):
    def test_marker_constant_is_the_exact_committed_token(self):
        # A typo here would silently break the replace on every generated repo — pin the exact string.
        self.assertEqual(inst._MARKETING_SEED_MARKER, "<!-- engine-template:landing-front -->")

    def test_recognizer_matches_only_the_marker_preserving_on_any_doubt(self):
        self.assertTrue(inst._is_marketing_seed(_MARKED_FRONT))
        self.assertTrue(inst._is_marketing_seed("\n\n" + _MARKED_FRONT))               # leading whitespace tolerated
        self.assertFalse(inst._is_marketing_seed("# My Project\n\nMy own words.\n"))   # operator content
        self.assertFalse(inst._is_marketing_seed(""))                                  # empty
        self.assertFalse(inst._is_marketing_seed(None))                                # absent/unreadable -> ""/None

    def test_a_readme_that_only_mentions_the_marker_mid_document_is_not_a_match(self):
        # The marker must LEAD the file (the slot IS the engine's seed), not merely appear inside it — so an
        # operator README that happens to quote the marker in its own content is preserved, never clobbered.
        mentions = ("# My Project\n\nWe started from a template; it had a "
                    + inst._MARKETING_SEED_MARKER + " comment we kept as a note.\n")
        self.assertFalse(inst._is_marketing_seed(mentions))

    def test_starter_and_default_carry_no_marker(self):
        # The safety-critical idempotency invariant: the starter the engine WRITES carries no marker, so a
        # re-run never re-replaces it (the engine never re-touches the root README after instantiation).
        self.assertNotIn(inst._MARKETING_SEED_MARKER, inst._DEFAULT_README_MD)
        with open(os.path.join(validate.ENGINE_DIR, "provisioning", "readme-seed.md"), encoding="utf-8") as fh:
            shipped = fh.read()
        self.assertNotIn(inst._MARKETING_SEED_MARKER, shipped)

    def test_starter_discloses_the_required_spine_in_plain_words(self):
        # The starter names the always-on spine in operator language and the no-style-floor gap,
        # never "the memory package is required" and never maintainer jargon.
        with open(os.path.join(validate.ENGINE_DIR, "provisioning", "readme-seed.md"), encoding="utf-8") as fh:
            shipped = fh.read()
        for text in (shipped, inst._DEFAULT_README_MD):
            low = text.lower()
            self.assertIn("remembers across sessions", low)   # memory, built in
            self.assertIn("keeps your work safe", low)
            self.assertIn("clean-code", low)                  # the gap named
            self.assertNotIn("required", low, "never 'the memory package is required'")
            # The disclosure must NOT surface the carved-out experimental engine-knowledge-graph
            # capability (the operator cannot yet enable it). Pin the negative next to the "required" one — the
            # disclosure renders capabilities by plain name, so guard both the slug and the plain phrase.
            self.assertNotIn("engine-knowledge-graph", low, "never surface the carved-out experimental capability")
            self.assertNotIn("knowledge graph", low, "nor by its plain name")


class TestRepoReadmeLeadsWithMarker(unittest.TestCase):
    """A durable guard on the TEMPLATE's OWN root README (issue #134): the committed README must keep
    LEADING with the marketing marker, or provisioning's front-door replace (_seed_readme) silently stops
    recognizing the front — and the engine's marketing README would then travel and land as a generated
    repo's product README. Unlike the recognizer tests above, this reads the REAL committed README,
    not a fixture: it pins the live template artifact #134 fills with marketing copy. It lives here, among
    the first-run assets the Retire phase deletes at instantiation, so it never runs in a generated repo
    (where the README is correctly replaced and the marker is gone)."""

    def test_committed_root_readme_leads_with_the_marketing_marker(self):
        readme = inst._read_text_or(os.path.join(validate.ROOT, "README.md"), "")
        self.assertTrue(
            inst._is_marketing_seed(readme),
            "the template's root README.md must LEAD with " + repr(inst._MARKETING_SEED_MARKER)
            + " (marketing copy goes BELOW it) so provisioning still recognizes and replaces the marketing "
            "front at first run; a copy edit that displaced the marker would silently kill the front-door "
            "replace and the engine's marketing page would land as a generated repo's product README.")


class TestSeedConduct(unittest.TestCase):
    """#409: the conduct operator-override seed is COPY-IF-ABSENT — once .engine/conduct/operator.md
    exists it is operator config, so a resumed/re-run apply never clobbers a /engine-conduct-tuned stance (the
    seed-then-own law). Mirrors _seed_security's existence guard."""

    def _plant_seed(self, root, body):
        os.makedirs(os.path.join(root, ".engine", "provisioning"), exist_ok=True)
        with open(os.path.join(root, ".engine", "provisioning", "conduct-seed.md"), "w", encoding="utf-8") as fh:
            fh.write(body)

    def test_seeds_operator_md_from_the_template_seed_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            inst.os.makedirs(os.path.join(d, ".engine"))
            self._plant_seed(d, "---\ncodes: []\n---\n\nRECOGNIZABLE CONDUCT SEED\n")
            with inst._redirect_root(d):
                outcome = inst._seed_conduct(lambda _t: None, None)
                now = inst._read_text_or(os.path.join(d, ".engine", "conduct", "operator.md"), "")
        self.assertEqual(outcome, "seeded")
        self.assertIn("RECOGNIZABLE CONDUCT SEED", now)

    def test_falls_back_to_the_empty_override_when_seed_absent(self):
        with tempfile.TemporaryDirectory() as d:                  # no seed source planted
            inst.os.makedirs(os.path.join(d, ".engine"))
            with inst._redirect_root(d):
                outcome = inst._seed_conduct(lambda _t: None, None)
                now = inst._read_text_or(os.path.join(d, ".engine", "conduct", "operator.md"), "")
        self.assertEqual(outcome, "seeded")
        self.assertEqual(now, inst._EMPTY_OPERATOR, "an absent seed yields the valid empty override")

    def test_never_overwrites_a_tuned_operator_md(self):
        tuned = "---\ncodes: [my-own-rule]\n---\n\nMY TUNED STANCE -- DO NOT CLOBBER\n"
        with tempfile.TemporaryDirectory() as d:
            inst.os.makedirs(os.path.join(d, ".engine"))
            self._plant_seed(d, "---\ncodes: []\n---\n\nthe seed that must NOT be used\n")
            target = os.path.join(d, ".engine", "conduct", "operator.md")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(tuned)
            with inst._redirect_root(d):
                # a skip discloses NOTHING — a `say` that fails the test if called catches a wrongful re-seed
                outcome = inst._seed_conduct(self.fail, inst.load_copy())
                now = inst._read_text_or(target, "")
        self.assertEqual(outcome, "present")
        self.assertEqual(now, tuned, "a /engine-conduct-tuned operator.md is left exactly as it was")

    def test_resume_is_idempotent_a_second_seed_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            inst.os.makedirs(os.path.join(d, ".engine"))
            self._plant_seed(d, "---\ncodes: []\n---\n\nseed body\n")
            target = os.path.join(d, ".engine", "conduct", "operator.md")
            with inst._redirect_root(d):
                first = inst._seed_conduct(lambda _t: None, None)
                body1 = inst._read_text_or(target, "")
                second = inst._seed_conduct(self.fail, inst.load_copy())  # a re-run must not re-seed or disclose
                body2 = inst._read_text_or(target, "")
        self.assertEqual((first, second), ("seeded", "present"))
        self.assertEqual(body1, body2, "a resumed seed never rewrites the file")


class TestSeedReadme(unittest.TestCase):
    def test_greenfield_replaces_the_marketing_front_and_discloses(self):
        said = []
        starter = "# Your project\n\nA starter for you.\n"
        with tempfile.TemporaryDirectory() as d:
            inst.os.makedirs(os.path.join(d, ".engine"))
            with inst._redirect_root(d):
                _seed_readme_root(d, readme=_MARKED_FRONT, seed=starter)
                outcome = inst._seed_readme(said.append, inst.load_copy())
                now = inst._read_text_or(os.path.join(d, "README.md"), "")
        self.assertEqual(outcome, "replaced")
        self.assertEqual(now, starter, "the marketing front is replaced with the product starter")
        self.assertNotIn(inst._MARKETING_SEED_MARKER, now, "the seeded starter carries no marker")
        blob = "\n".join(said).lower()
        self.assertTrue(said, "the replace is disclosed, never silent")
        self.assertIn("your project", blob)
        self.assertIn("replaced", blob, "names what changed (what changed and why it is theirs)")

    def test_brownfield_operator_readme_is_preserved_untouched(self):
        said = []
        mine = "# My Project\n\nMy own words — nothing to do with the Engine.\n"
        with tempfile.TemporaryDirectory() as d:
            inst.os.makedirs(os.path.join(d, ".engine"))
            with inst._redirect_root(d):
                _seed_readme_root(d, readme=mine, seed="# Your project\n")
                outcome = inst._seed_readme(said.append, inst.load_copy())
                now = inst._read_text_or(os.path.join(d, "README.md"), "")
        self.assertEqual(outcome, "present")
        self.assertEqual(now, mine, "an operator README (no marker) is left exactly as it is")
        self.assertEqual(said, [], "no disclosure on a no-op")

    def test_operator_readme_that_quotes_the_marker_is_preserved(self):
        # End-to-end guard for the recognizer-leads-the-file rule: an operator README that merely mentions the
        # marker (not at the start) must be left exactly as it is — _seed_readme never clobbers it.
        said = []
        mine = ("# My Project\n\nNote: this repo began from a template carrying a "
                + inst._MARKETING_SEED_MARKER + " marker, which I left in.\n")
        with tempfile.TemporaryDirectory() as d:
            inst.os.makedirs(os.path.join(d, ".engine"))
            with inst._redirect_root(d):
                _seed_readme_root(d, readme=mine, seed="# Your project\n")
                outcome = inst._seed_readme(said.append, inst.load_copy())
                now = inst._read_text_or(os.path.join(d, "README.md"), "")
        self.assertEqual(outcome, "present")
        self.assertEqual(now, mine, "a README that only quotes the marker mid-document is never replaced")
        self.assertEqual(said, [])

    def test_rerun_after_a_replace_is_a_noop(self):
        said = []
        with tempfile.TemporaryDirectory() as d:
            inst.os.makedirs(os.path.join(d, ".engine"))
            with inst._redirect_root(d):
                _seed_readme_root(d, readme=_MARKED_FRONT, seed="# Your project\n")
                inst._seed_readme(lambda t: None, inst.load_copy())     # first pass replaces
                first = inst._read_text_or(os.path.join(d, "README.md"), "")
                outcome = inst._seed_readme(said.append, inst.load_copy())   # second pass
                second = inst._read_text_or(os.path.join(d, "README.md"), "")
        self.assertEqual(outcome, "present", "the seeded starter has no marker → second pass is a no-op")
        self.assertEqual(first, second, "a re-run never re-touches the root README")
        self.assertEqual(said, [])

    def test_absent_seed_falls_back_to_the_built_in_default(self):
        with tempfile.TemporaryDirectory() as d:
            inst.os.makedirs(os.path.join(d, ".engine"))
            with inst._redirect_root(d):
                _seed_readme_root(d, readme=_MARKED_FRONT)              # marker present, NO seed file
                outcome = inst._seed_readme(lambda t: None, inst.load_copy())
                now = inst._read_text_or(os.path.join(d, "README.md"), "")
        self.assertEqual(outcome, "replaced")
        self.assertEqual(now, inst._DEFAULT_README_MD, "an absent seed yields the minimal default, never an error")

    def test_absent_readme_is_a_noop(self):
        with tempfile.TemporaryDirectory() as d:
            inst.os.makedirs(os.path.join(d, ".engine"))
            with inst._redirect_root(d):
                _seed_readme_root(d, seed="# Your project\n")          # no root README at all
                outcome = inst._seed_readme(lambda t: None, inst.load_copy())
                exists = os.path.exists(os.path.join(d, "README.md"))
        self.assertEqual(outcome, "present", "no README to recognize → preserve-on-doubt, write nothing")
        self.assertFalse(exists, "the engine never creates a root README out of nothing")


# ==== the root LICENSE clear (issue #147) ===============================================

def _template_license_text(holder="StarshipSuperjam"):
    """Reconstruct a full LICENSE from the recognizer's OWN seed, for fixtures — so the fixtures can never silently
    drift from what the recognizer accepts. With the template author (StarshipSuperjam) named as Licensor this is the
    engine's traveled license (cleared); with any other holder it is an adopter's own license (preserved)."""
    return inst._TEMPLATE_LICENSE_SEED.replace("StarshipSuperjam", holder)


class TestLicenseRecognizer(unittest.TestCase):
    def test_the_exact_template_license_is_recognized(self):
        self.assertTrue(inst._is_template_license(_template_license_text()))

    def test_crlf_trailing_newline_blank_line_and_bom_variance_still_match(self):
        base = _template_license_text()
        self.assertTrue(inst._is_template_license(base.replace("\n", "\r\n")), "CRLF (a Windows-saved copy)")
        self.assertTrue(inst._is_template_license(base.rstrip("\n")), "a missing trailing newline")
        self.assertTrue(inst._is_template_license("\ufeff" + base), "a leading byte-order mark")
        self.assertTrue(inst._is_template_license(base.replace("\n\n", "\n\n\n")), "extra blank lines")

    def test_a_renamed_licensor_is_preserved_never_deleted(self):
        # The catastrophic false-positive guard: our EXACT text, but THEIR name on the Licensor/copyright →
        # normalizes differently → preserved, never deleted. Includes near-miss names that merely embed the author.
        for holder in ("Acme Corp", "StarshipSuperjamson", "The StarshipSuperjam Foundation", "starshipsuperjam"):
            self.assertFalse(inst._is_template_license(_template_license_text(holder=holder)),
                             f"licensor {holder!r} is not the template author — must be preserved, never cleared")

    def test_the_apache_text_without_the_commons_clause_is_not_matched(self):
        # Plain Apache-2.0 (no Commons Clause preamble) is a different license — an adopter's own choice → preserve.
        # Proves the recognizer keys on the WHOLE license (the no-Sell condition included), not merely "Apache-ness".
        seed = inst._TEMPLATE_LICENSE_SEED
        apache_only = seed[seed.index("Version 2.0, January 2004"):]
        self.assertFalse(inst._is_template_license(apache_only))

    def test_a_stock_mit_license_is_not_matched(self):
        self.assertFalse(inst._is_template_license(
            "MIT License\n\nCopyright (c) 2026 StarshipSuperjam\n\nPermission is hereby granted, free of charge...\n"))

    def test_empty_or_none_is_not_matched(self):
        self.assertFalse(inst._is_template_license(""))
        self.assertFalse(inst._is_template_license(None))

    def test_the_seed_with_appended_terms_is_not_matched(self):
        # An adopter who kept our text verbatim but APPENDED their own extra terms is a different (superset)
        # license → normalizes differently → preserved, never cleared.
        self.assertFalse(inst._is_template_license(_template_license_text() + "\n\nExtra term added by the adopter.\n"))


def _seed_license_root(tmp, *, license_text=None):
    """Plant a fixture root for a _seed_license test: optionally a root LICENSE (its exact text). Omit to model the
    absent-file case. The caller holds the surrounding inst._redirect_root(tmp)."""
    if license_text is not None:
        with open(os.path.join(tmp, "LICENSE"), "w", encoding="utf-8") as fh:
            fh.write(license_text)


class TestSeedLicense(unittest.TestCase):
    def test_greenfield_clears_the_template_license_and_discloses(self):
        said = []
        with tempfile.TemporaryDirectory() as d:
            inst.os.makedirs(os.path.join(d, ".engine"))
            with inst._redirect_root(d):
                _seed_license_root(d, license_text=_template_license_text())
                outcome = inst._seed_license(said.append, inst.load_copy())
                exists = os.path.exists(os.path.join(d, "LICENSE"))
        self.assertEqual(outcome, "cleared")
        self.assertFalse(exists, "the traveled template license is removed")
        blob = "\n".join(said).lower()
        self.assertTrue(said, "the clear is disclosed, never silent")
        self.assertIn("license", blob)
        self.assertIn("removed", blob, "names what was removed (the clear is disclosed in plain words)")

    def test_no_replacement_is_seeded(self):
        with tempfile.TemporaryDirectory() as d:
            inst.os.makedirs(os.path.join(d, ".engine"))
            with inst._redirect_root(d):
                _seed_license_root(d, license_text=_template_license_text())
                inst._seed_license(lambda t: None, inst.load_copy())
                exists = os.path.exists(os.path.join(d, "LICENSE"))
        self.assertFalse(exists, "the engine seeds NO replacement license — the slot is left empty (the adopter's choice)")

    def test_brownfield_adopter_license_is_preserved_untouched(self):
        said = []
        mine = _template_license_text(holder="Acme Corp")     # our text, but THEIR name on the Licensor/copyright — their own license
        with tempfile.TemporaryDirectory() as d:
            inst.os.makedirs(os.path.join(d, ".engine"))
            with inst._redirect_root(d):
                _seed_license_root(d, license_text=mine)
                outcome = inst._seed_license(said.append, inst.load_copy())
                now = inst._read_text_or(os.path.join(d, "LICENSE"), "")
        self.assertEqual(outcome, "present")
        self.assertEqual(now, mine, "a license the adopter chose (different holder) is left exactly as it is")
        self.assertEqual(said, [], "no disclosure on a no-op")

    def test_rerun_after_a_clear_is_a_noop(self):
        said = []
        with tempfile.TemporaryDirectory() as d:
            inst.os.makedirs(os.path.join(d, ".engine"))
            with inst._redirect_root(d):
                _seed_license_root(d, license_text=_template_license_text())
                inst._seed_license(lambda t: None, inst.load_copy())          # first pass clears
                outcome = inst._seed_license(said.append, inst.load_copy())   # second pass: slot now empty
        self.assertEqual(outcome, "present", "the slot is now empty → second pass is a no-op")
        self.assertEqual(said, [], "a re-run never re-touches the root LICENSE")

    def test_absent_license_is_a_noop(self):
        with tempfile.TemporaryDirectory() as d:
            inst.os.makedirs(os.path.join(d, ".engine"))
            with inst._redirect_root(d):
                outcome = inst._seed_license(lambda t: None, inst.load_copy())   # no root LICENSE at all
        self.assertEqual(outcome, "present", "no LICENSE to recognize → preserve-on-doubt, delete nothing")


class TestSeedState(unittest.TestCase):
    """Reset a generated repo's traveled construction cursor to genesis; recognition is a structural,
    rename-immune foreign-register predicate + a home-repo belt; risk-oriented and fail-safe."""

    def setUp(self):
        # The belt now keys on repo_identity.is_home_repo (git origin == recorded home); default these tests to a
        # DEPLOYED repo (not home) so the register-recognition logic BELOW the belt is exercised. The belt itself
        # has its own test (test_home_repo_belt_preserves_the_real_cursor).
        p = mock.patch.object(inst.repo_identity, "is_home_repo", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def _write_cursor(self, d, register, phase="workshop phase #449", count=31):
        os.makedirs(os.path.join(d, ".engine", "state"), exist_ok=True)
        cur = {"schema_version": 1,
               "standing_situation": {"milestone": "wp", "phase": phase, "as_of": "2026-07-12T09:21:38Z"},
               "integration_debt": {"open_count": count, "as_of": "2026-07-12T09:21:38Z", "register": register}}
        with open(os.path.join(d, ".engine", "state", "state.json"), "w") as fh:
            json.dump(cur, fh, indent=2)

    def _cursor(self, d):
        with open(os.path.join(d, ".engine", "state", "state.json")) as fh:
            return json.load(fh)

    def test_genesis_constant_conforms_to_the_state_schema(self):
        # F3: the inline genesis constant is BOUND to the live schema, so a future state.v1 field can't
        # silently make it invalid (which would ship a generated repo a cursor read_state then refuses).
        schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "state.v1.json"))
        errs = list(validate.Draft202012Validator(schema).iter_errors(inst._GENESIS_CURSOR))
        self.assertEqual(errs, [], errs)

    def test_foreign_register_is_reset_to_genesis_and_disclosed(self):
        said = []
        with tempfile.TemporaryDirectory() as d, inst._redirect_root(d), \
                mock.patch.object(inst.boot, "repo_slug", return_value="acme/proj"):
            self._write_cursor(d, "https://github.com/StarshipSuperjam/engine-template/issues?q=is:open")
            outcome = inst._seed_state(said.append, inst.load_copy())
            cur = self._cursor(d)
        self.assertEqual(outcome, "reseeded")
        self.assertEqual(cur, inst._GENESIS_CURSOR, "the traveled workshop cursor is cleared to genesis")
        self.assertTrue(said, "the reset is disclosed, never silent")
        self.assertIn("clean slate", "\n".join(said).lower())

    def test_own_origin_cursor_is_preserved(self):
        said = []
        with tempfile.TemporaryDirectory() as d, inst._redirect_root(d), \
                mock.patch.object(inst.boot, "repo_slug", return_value="acme/proj"):
            self._write_cursor(d, "https://github.com/acme/proj/issues?q=is:open", phase="my real phase", count=7)
            outcome = inst._seed_state(said.append, inst.load_copy())
            cur = self._cursor(d)
        self.assertEqual(outcome, "present", "a cursor naming THIS repo's own origin is the operator's — preserved")
        self.assertEqual(cur["standing_situation"]["phase"], "my real phase")
        self.assertEqual(cur["integration_debt"]["open_count"], 7)
        self.assertEqual(said, [], "no disclosure on a no-op")

    def test_genesis_cursor_is_a_noop(self):
        with tempfile.TemporaryDirectory() as d, inst._redirect_root(d), \
                mock.patch.object(inst.boot, "repo_slug", return_value="acme/proj"):
            self._write_cursor(d, None)  # null register == genesis
            outcome = inst._seed_state(lambda t: None, inst.load_copy())
        self.assertEqual(outcome, "present")

    def test_malformed_cursor_is_preserved_never_crashes(self):
        with tempfile.TemporaryDirectory() as d, inst._redirect_root(d), \
                mock.patch.object(inst.boot, "repo_slug", return_value="acme/proj"):
            os.makedirs(os.path.join(d, ".engine", "state"))
            with open(os.path.join(d, ".engine", "state", "state.json"), "w") as fh:
                fh.write("{not valid json")
            outcome = inst._seed_state(lambda t: None, inst.load_copy())   # must not raise
        self.assertEqual(outcome, "present", "an unreadable/malformed cursor is preserved, never crashes the phase")

    def test_home_repo_belt_preserves_the_real_cursor(self):
        # BELT: in the engine's OWN home repo (repo_identity.is_home_repo — git origin == recorded home),
        # _seed_state preserves even a foreign-register cursor — it can NEVER clobber the workshop's real cursor.
        with tempfile.TemporaryDirectory() as d, inst._redirect_root(d), \
                mock.patch.object(inst.repo_identity, "is_home_repo", return_value=True), \
                mock.patch.object(inst.boot, "repo_slug", return_value="acme/proj"):
            self._write_cursor(d, "https://github.com/StarshipSuperjam/engine-template/issues", count=31)
            outcome = inst._seed_state(lambda t: None, inst.load_copy())
            cur = self._cursor(d)
        self.assertEqual(outcome, "present")
        self.assertEqual(cur["integration_debt"]["open_count"], 31, "the home-repo belt preserved the real cursor")

    def test_prefix_slug_org_sibling_is_not_falsely_preserved(self):
        # a bare substring would falsely PRESERVE a repo whose slug is a PREFIX of the template's within the
        # same org (own "acme/engine" vs a register naming ".../acme/engine-template/..."). The segment-anchored
        # match resets it correctly — the borrowed-cursor leak this exists to prevent.
        with tempfile.TemporaryDirectory() as d, inst._redirect_root(d), \
                mock.patch.object(inst.boot, "repo_slug", return_value="acme/engine"):
            self._write_cursor(d, "https://github.com/acme/engine-template/issues?q=is:open")
            outcome = inst._seed_state(lambda t: None, inst.load_copy())
            cur = self._cursor(d)
        self.assertEqual(outcome, "reseeded", "a prefix-slug org sibling is a foreign register, not preserved")
        self.assertEqual(cur["integration_debt"]["open_count"], 0)

    def test_unknown_origin_past_the_belt_resets(self):
        # repo_slug None (offline/no-remote) past the home-repo belt: a non-null register can only be the
        # traveled workshop cursor (a fresh repo hasn't set its own), so reset — risk-oriented, and safe
        # because the belt already ruled out the workshop.
        with tempfile.TemporaryDirectory() as d, inst._redirect_root(d), \
                mock.patch.object(inst.boot, "repo_slug", return_value=None):
            self._write_cursor(d, "https://github.com/StarshipSuperjam/engine-template/issues")
            outcome = inst._seed_state(lambda t: None, inst.load_copy())
            cur = self._cursor(d)
        self.assertEqual(outcome, "reseeded")
        self.assertEqual(cur["integration_debt"]["open_count"], 0)


# ==== the deployed-floor swap-in (issue #272) ========================================================

class TestRepoLicenseIsTheTemplateSeed(unittest.TestCase):
    """A durable parity guard on the TEMPLATE's OWN root LICENSE (issue #147): the committed LICENSE must stay
    recognizable as the engine's shipped template-license seed, or provisioning's first-run clear (_seed_license)
    silently stops recognizing it — and the template author's copyright would then travel and govern a generated
    repo's product. It also documents that the construction repo's own LICENSE WOULD be cleared by a
    non-redirected apply (which is why the apply-demo's isolation check lists LICENSE). Like the README guard above,
    it reads the REAL committed LICENSE and lives among the first-run assets the Retire phase deletes, so it never
    runs in a generated repo (where the traveled license is correctly cleared and gone)."""

    def test_committed_root_license_matches_the_template_seed_recognizer(self):
        license_text = inst._read_text_or(os.path.join(validate.ROOT, "LICENSE"), "")
        self.assertTrue(
            inst._is_template_license(license_text),
            "the template's root LICENSE must stay recognizable as the engine's shipped template-license seed "
            "(Apache-2.0 + Commons Clause); if it was re-worded (including a copyright-year bump), update "
            "inst._TEMPLATE_LICENSE_SEED to match, or first-run setup will stop clearing the traveled license and "
            "the template author's copyright would govern a generated repo's product.")
        # Byte-parity (stricter than recognize()'s cosmetic-tolerant match): CURRENT_SEED must equal the committed
        # root LICENSE exactly. A future relicense MUST append the new text to license_seeds.HISTORICAL_SEEDS (the
        # tail becomes CURRENT_SEED) — forget it and BOTH the first-run clear and the standing detector go silently
        # blind (#471). This makes the append-only law mechanical.
        self.assertEqual(
            license_seeds.CURRENT_SEED, license_text,
            "license_seeds.CURRENT_SEED must be byte-identical to the committed root LICENSE; append a re-licensed "
            "text to HISTORICAL_SEEDS so CURRENT_SEED follows the tail.")


class TestApplyDemoRunsGreen(unittest.TestCase):
    def test_apply_demo_exits_zero(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = inst.main(["apply-demo"])
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn("byte-for-byte unchanged", out, "the isolation guarantee is shown")
        self.assertIn("naming it, not hiding it", out, "the honest-ceiling banner leads the demo")
        self.assertIn("front page", out, "the README seed/replace scenario runs in the demo")
        self.assertIn("removed and nothing put in its place", out, "the LICENSE-clear scenario runs in the demo")


class TestApplyCli(unittest.TestCase):
    def test_apply_refuses_plainly_without_confirmation(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            buf = io.StringIO()
            with inst._redirect_root(d), contextlib.redirect_stdout(buf):
                rc = inst.main(["apply", "--first-run"])   # token present → reaches the not-confirmed refusal
            self.assertEqual(rc, 1)
            self.assertIn("hasn't been confirmed", buf.getvalue())

    def test_confirm_then_apply_chain(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d), contextlib.redirect_stdout(io.StringIO()):
                rc1 = inst.main(["confirm", "--tier", "solo", "--keep", "", "--handle", "octocat"])
            self.assertEqual(rc1, 0)
            self.assertTrue(inst.is_provisioned(d), "confirm wrote the checkpoint")


class TestFirstRunVerbGuards(unittest.TestCase):
    """#297 — the one-time lifecycle verbs refuse a bare hand-run so re-running them on an already-set-up project
    (or in this workshop) never re-fires the file-replacing setup steps. All three are gated by the `--first-run`
    token the setup walkthrough passes (#323: token-only, not a marker check — a fresh copy inherits the
    committed floor, so there is no construction file to key on); retire additionally refuses in the engine's own
    home repo (repo_identity.is_home_repo). A bare hand-run then refuses in the workshop AND in a fresh copy."""

    def test_bare_apply_is_a_noop_and_touches_nothing(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d), contextlib.redirect_stdout(io.StringIO()):
                inst.main(["confirm", "--tier", "solo", "--keep", "", "--handle", "octocat"])  # already set up
            buf = io.StringIO()
            with inst._redirect_root(d), contextlib.redirect_stdout(buf):
                rc = inst.main(["apply"])                  # bare hand-run — no first-run token
            self.assertEqual(rc, 0)
            self.assertIn(inst._APPLY_NOT_FIRST_RUN, buf.getvalue())
            # the one-time reconciles did NOT fire: the README front + LICENSE the seeds would change are untouched
            with open(os.path.join(d, "README.md"), encoding="utf-8") as fh:
                self.assertIn(inst._MARKETING_SEED_MARKER, fh.read(), "the README front was not replaced")
            self.assertTrue(os.path.isfile(os.path.join(d, "LICENSE")), "the LICENSE was not cleared")

    def test_apply_with_first_run_token_runs_the_real_logic(self):
        # The token lets apply through to its real logic — proven by it reaching the not-confirmed refusal on an
        # unconfirmed fixture (the locked resumable-apply path the bare-run guard must never block).
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)                          # confirmed = False
            buf = io.StringIO()
            with inst._redirect_root(d), contextlib.redirect_stdout(buf):
                rc = inst.main(["apply", "--first-run"])
            self.assertEqual(rc, 1)
            self.assertIn("hasn't been confirmed", buf.getvalue())
            self.assertNotIn(inst._APPLY_NOT_FIRST_RUN, buf.getvalue())

    def test_verify_refuses_in_the_workshop(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)                          # root CLAUDE.md is still the construction file
            buf = io.StringIO()
            with inst._redirect_root(d), contextlib.redirect_stdout(buf):
                rc = inst.main(["verify"])
            self.assertEqual(rc, 0)
            self.assertIn("not by hand", buf.getvalue())

    def test_verify_runs_after_setup_swapped_the_floor(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with inst._redirect_root(d):
                _finished_fixture(d)                        # full apply ran → root CLAUDE.md is the deployed floor
                with contextlib.redirect_stdout(buf):
                    rc = inst.main(["verify", "--first-run"])   # the walkthrough's token lets the real run through
            self.assertEqual(rc, 0)
            self.assertNotIn("not by hand", buf.getvalue(),
                             "a real first-run verify is not blocked by the guard")

    def test_retire_refuses_in_the_workshop_and_deletes_nothing(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            inst._plant_first_run_assets(d)                 # the real-tool stand-ins a stray retire would delete
            buf = io.StringIO()
            with inst._redirect_root(d), contextlib.redirect_stdout(buf):
                rc = inst.main(["retire"])
            self.assertEqual(rc, 0)
            self.assertIn("not by hand", buf.getvalue())
            self.assertTrue(os.path.isfile(os.path.join(d, ".engine", "tools", "instantiator.py")),
                            "a bare retire in the workshop must not self-delete the real setup tool")

    def test_retire_runs_after_setup_swapped_the_floor(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with inst._redirect_root(d):
                _finished_fixture(d)
                _make_deployed(d)                               # a real copy (origin != home) — not the workshop
                with contextlib.redirect_stdout(buf):
                    rc = inst.main(["retire", "--first-run"])   # the walkthrough's token lets the real run through
            self.assertEqual(rc, 0)
            self.assertNotIn("not by hand", buf.getvalue())
            self.assertFalse(os.path.isfile(os.path.join(d, ".engine", "tools", "instantiator.py")),
                             "a real first-run retire tidies the one-time setup tool away")

    def test_retire_refuses_without_the_token_even_after_the_floor_swap(self):
        # #323: post-swap the root is the DEPLOYED floor, so the retired construction-marker guard would have
        # let a bare retire self-delete the setup tooling. The token guard does not — a bare hand-run refuses
        # and deletes nothing, in a fresh copy pre-tidy exactly as in the workshop.
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with inst._redirect_root(d):
                _finished_fixture(d)                        # deployed floor in place, NOT the construction file
                with contextlib.redirect_stdout(buf):
                    rc = inst.main(["retire"])              # bare — no --first-run token
            self.assertEqual(rc, 0)
            self.assertIn("not by hand", buf.getvalue())
            self.assertTrue(os.path.isfile(os.path.join(d, ".engine", "tools", "instantiator.py")),
                            "a bare retire must refuse even after the floor swap, not self-delete the tooling")

    def test_retire_refuses_in_the_home_repo_even_with_the_token(self):
        # #323 home-repo belt: retire never self-deletes in the engine's OWN home repo (git origin == recorded
        # home), even carrying the --first-run token — the second belt beyond the token, both failing toward
        # refusing. A tokened hand-run in the workshop must change nothing.
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with inst._redirect_root(d):
                _finished_fixture(d)
                _make_deployed(d, origin="https://github.com/StarshipSuperjam/engine-template.git")  # origin == home
                with contextlib.redirect_stdout(buf):
                    rc = inst.main(["retire", "--first-run"])
            self.assertEqual(rc, 0)
            self.assertIn("home repository", buf.getvalue())
            self.assertTrue(os.path.isfile(os.path.join(d, ".engine", "tools", "instantiator.py")),
                            "retire must refuse in the home repo and delete nothing, even with the token")

    def test_retire_rederives_the_self_map_when_the_tree_carries_one(self):
        # #513: retire re-derives the wiring map beside the knowledge graph, so a deployed repo ships a map
        # rendered from the post-retire tree (the retired-and-absent filter lives in self_map.render_module;
        # its shape is proven in test_self_map — here we prove retire actually re-derives). The fixture tree
        # carries no map, so we seed a stale one; retire must rewrite it in place.
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d):
                _finished_fixture(d)
                # The minimal fixture manifest doesn't carry a map; give the seeded one an owner (the real
                # core manifest owns it via provides.foundation) so the consistency gate sees no orphan.
                mpath = os.path.join(d, ".engine", "modules", "core", "manifest.json")
                with open(mpath, encoding="utf-8") as fh:
                    m = json.load(fh)
                m.setdefault("provides", {})["foundation"] = [".engine/self-map.md"]
                with open(mpath, "w", encoding="utf-8") as fh:
                    json.dump(m, fh)
                with open(self_map.SELF_MAP_PATH, "w", encoding="utf-8") as fh:
                    fh.write("stale placeholder the retire step must replace\n")
                result = inst.retire(announce=lambda _s: None)
                map_path = self_map.SELF_MAP_PATH
                self.assertEqual(result["self_map"], "regenerated")
                self.assertTrue(map_path.startswith(d), "the map write must stay inside the fixture tree")
                with open(map_path, encoding="utf-8") as fh:
                    text = fh.read()
        self.assertNotIn("stale placeholder", text)
        self.assertIn("What this engine is made of", text)

    def test_retire_without_a_map_creates_none(self):
        # A tree that never carried a map (the demo's minimal practice project) gets none — creating one
        # there would orphan an unowned engine file; the honest status names the absence.
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d):
                _finished_fixture(d)
                result = inst.retire(announce=lambda _s: None)
                self.assertEqual(result["self_map"], "absent (nothing to re-derive)")
                self.assertFalse(os.path.isfile(self_map.SELF_MAP_PATH))

# ==== VERIFY + RETIRE ===============================================================

_FINISH_KEYS = ("verify-paused", "verify-next-actions", "verify-ok", "verify-gate-on",
                "verify-gate-pending", "retire-success")


def _finished_fixture(tmp, handle="octocat"):
    """Build + confirm + apply a fixture with the first-run assets planted, leaving a fully-installed,
    consistent practice engine ready for verify/retire. Caller holds the surrounding _redirect_root(tmp)."""
    inst._build_fixture(tmp)
    inst._plant_first_run_assets(tmp)
    inst.confirm([], "solo", engine_release="1.0.0", handle=handle)
    return inst._finish_apply(tmp)


def _make_deployed(tmp, *, origin="https://github.com/adopter/their-product.git",
                   home="StarshipSuperjam/engine-template"):
    """Make the fixture read as a real DEPLOYED copy: a git origin that DIFFERS from the recorded
    home_repository, so repo_identity.is_home_repo() is False and retire's home-repo belt (#323) lets the
    legitimate first-run cleanup proceed. Pass origin == home to model the WORKSHOP, which the belt refuses."""
    subprocess.run(["git", "-C", tmp, "init", "-q"], capture_output=True)
    subprocess.run(["git", "-C", tmp, "remote", "add", "origin", origin], capture_output=True)
    mpath = os.path.join(tmp, ".engine", "engine.json")
    if os.path.isfile(mpath):
        with open(mpath, encoding="utf-8") as fh:
            manifest = json.load(fh)
    else:
        manifest = {}
    manifest["home_repository"] = home
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)


class TestVerify(unittest.TestCase):
    def test_clean_setup_passes(self):
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d):
                _finished_fixture(d)
                res = inst.verify(announce=lambda t: None)
            self.assertFalse(res["paused"])
            self.assertEqual(res["findings"], [])
            self.assertEqual(res["steps"][0]["status"], "ok")

    def test_pauses_on_a_hard_finding_with_both_next_actions(self):
        with tempfile.TemporaryDirectory() as d:
            said = []
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst.wiring.apply(inst._ORPHAN_WIRE)         # a setting belonging to no installed module
                res = inst.verify(announce=said.append)
            self.assertTrue(res["paused"])
            self.assertEqual(len(res["findings"]), 1)
            self.assertEqual(res["steps"][0]["status"], "paused")
            blob = "\n".join(said).lower()
            self.assertIn("run setup again", blob, "the fix-and-retry next action is offered")
            self.assertIn("report it", blob, "the stop-and-report next action is offered (never a dead-end)")

    def test_resumable_after_repair(self):
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst.wiring.apply(inst._ORPHAN_WIRE)
                bad = inst.verify(announce=lambda t: None)
                inst.wiring.reverse(inst._ORPHAN_WIRE)       # the operator fixes it
                good = inst.verify(announce=lambda t: None)
            self.assertTrue(bad["paused"])
            self.assertFalse(good["paused"], "re-running after the repair re-checks clean (resumable)")

    def test_surfaces_gate_on_when_protected(self):
        with tempfile.TemporaryDirectory() as d:
            said = []
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst.verify(announce=said.append, control_status={"protected": True})
            self.assertIn("review gate is on", "\n".join(said).lower())

    def test_surfaces_gate_pending_when_not_protected(self):
        with tempfile.TemporaryDirectory() as d:
            said = []
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst.verify(announce=said.append, control_status={"protected": False})
            self.assertIn("isn't on yet", "\n".join(said).lower())

    def test_standalone_defers_the_gate_surface_to_boot(self):
        # With no review-gate status passed, verify says nothing about the gate — boot owns the standing surface.
        with tempfile.TemporaryDirectory() as d:
            said = []
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst.verify(announce=said.append)            # control_status=None
            self.assertNotIn("review gate", "\n".join(said).lower())


class TestRetire(unittest.TestCase):
    def test_refuses_on_a_hard_finding_and_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst.wiring.apply(inst._ORPHAN_WIRE)
                res = inst.retire(announce=lambda t: None)
                present = all(os.path.exists(os.path.join(d, rel)) for rel in inst._FIRST_RUN_ASSET_FILES)
            self.assertTrue(res["refused"])
            self.assertEqual(res["reason"], "inconsistent")
            self.assertEqual(res["deleted"], [])
            self.assertTrue(present, "the irreversible tidy-up never runs on an inconsistent setup")

    def test_tidies_assets_and_preserves_the_permanent_set(self):
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d):
                _finished_fixture(d)
                res = inst.retire(announce=lambda t: None)
                files_gone = all(not os.path.exists(os.path.join(d, rel))
                                 for rel in inst._FIRST_RUN_ASSET_FILES)
                dir_gone = not os.path.isdir(os.path.join(d, ".claude", "skills", "engine-setup"))
                catalog_kept = os.path.isfile(os.path.join(d, ".engine", "provisioning", "module-catalog.json"))
                still_clean = not inst._hard_findings()
            self.assertFalse(res["refused"])
            self.assertTrue(files_gone, "the one-time setup files are removed")
            self.assertTrue(dir_gone, "the setup skill is removed")
            self.assertTrue(catalog_kept, "the catalog the engine keeps is preserved")
            self.assertTrue(still_clean, "the result stays consistent after retire (the deployed repo stays green)")
            self.assertEqual(res["graph"], "regenerated")

    def test_a_brownfield_adopters_own_assets_directory_survives_retire(self):
        # #410, the blocking brownfield-safety property: retire() ALSO runs on the "add the engine to an
        # existing project" arrival, where assets/ is the OPERATOR's own directory (the engine provides none). So
        # the banner is retired as the specific FILE, never the whole assets/ dir — a whole-dir rmtree would delete
        # the adopter's own files. Plant an operator asset beside the engine banner and assert only the banner goes.
        with tempfile.TemporaryDirectory() as d:
            operator_asset = os.path.join(d, "assets", "company-logo.png")
            with inst._redirect_root(d):
                _finished_fixture(d)                            # plants the engine banner at assets/engine_banner.jpg
                with open(operator_asset, "w", encoding="utf-8") as fh:
                    fh.write("the operator's own logo")
                inst.retire(announce=lambda t: None)
                banner_gone = not os.path.exists(os.path.join(d, "assets", "engine_banner.jpg"))
                operator_kept = os.path.isfile(operator_asset)
                dir_kept = os.path.isdir(os.path.join(d, "assets"))
            self.assertTrue(banner_gone, "the engine's own banner is retired")
            self.assertTrue(operator_kept, "a brownfield adopter's own assets/ file must NOT be deleted by retire")
            self.assertTrue(dir_kept, "the operator's assets/ directory survives (only the engine banner is removed)")

    def test_regen_drops_a_stale_tool_entity(self):
        # The load-bearing deployed-repo guarantee: after the tools are gone, the saved information no longer
        # lists them, so the merge-time check stays green. Seed a STALE entry for the to-be-deleted tool and
        # assert retire's re-derive drops it.
        with tempfile.TemporaryDirectory() as d:
            graph_path = os.path.join(d, ".engine", "knowledge", "graph.json")
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst._write_json(graph_path, {"schema_version": 1, "entities": [
                    {"id": "tool:instantiator", "type": "tool", "name": ".engine/tools/instantiator.py"}]})
                inst.retire(announce=lambda t: None)
                with open(graph_path, encoding="utf-8") as fh:
                    regenerated = fh.read()
            self.assertNotIn("tool:instantiator", regenerated,
                             "the re-derive drops the deleted tool from the saved information")

    def test_idempotent_resume(self):
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst.retire(announce=lambda t: None)
                second = inst.retire(announce=lambda t: None)
            self.assertFalse(second["refused"], "a resumed retire is safe")
            self.assertEqual(second["deleted"], [], "the second pass finds everything already gone")
            self.assertTrue(set(inst._FIRST_RUN_ASSET_FILES).issubset(set(second["already_absent"])))


class TestRetireGuard(unittest.TestCase):
    """The fail-closed safety guard on retire(): no retirement target may resolve outside the engine's own
    paths, so a future bad manifest entry can never `rmtree`/`remove` a brownfield adopter's own file or
    directory. The guard runs first — before the consistency check and before any delete."""

    def test_the_committed_first_run_asset_set_is_all_engine_owned(self):
        # The load-bearing regression pin: every real retire entry (files AND dirs) is a safe, engine-owned
        # target today, and a future non-engine entry (e.g. "README.md" or the shared ".claude/skills") fails
        # this before it can ship.
        base = inst.validate.ROOT
        for rel in inst._FIRST_RUN_ASSET_FILES + inst._FIRST_RUN_ASSET_DIRS:
            self.assertIsNone(inst._unsafe_retire_reason(base, rel),
                              f"committed retire target is not engine-owned: {rel}")

    def test_the_guard_refuses_unsafe_targets_and_admits_engine_owned_ones(self):
        with tempfile.TemporaryDirectory() as d:
            unsafe = ["/etc/passwd", os.path.join("..", "outside"),
                      os.path.join(".claude", "skills"), os.path.join(".agents", "commands"),
                      "README.md", os.path.join("src", "main.py"), ".engine", "", "."]
            for rel in unsafe:
                self.assertIsNotNone(inst._unsafe_retire_reason(d, rel),
                                     f"the guard must refuse a non-engine target: {rel!r}")
            safe = [os.path.join(".engine", "tools", "x.py"), os.path.join(".engine", "audits", "d.md"),
                    os.path.join(".claude", "skills", "engine-setup"),
                    os.path.join(".agents", "skills", "engine-setup"),
                    os.path.join("assets", "engine_banner.jpg")]
            for rel in safe:
                self.assertIsNone(inst._unsafe_retire_reason(d, rel),
                                  f"the guard must admit an engine-owned target: {rel!r}")

    def _refuses_and_preserves(self, kind, rel, make):
        # Drive the REAL retire() on a coherent finished fixture (so a reverted guard would actually delete the
        # stand-in — keeping this falsifiable), with a dangerous `rel` injected into the retire set.
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d):
                _finished_fixture(d)
                victim = os.path.join(d, rel)
                make(victim)                                # planted AFTER the build, outside the engine roots
                orig_dirs, orig_files = inst._FIRST_RUN_ASSET_DIRS, inst._FIRST_RUN_ASSET_FILES
                if kind == "dir":
                    inst._FIRST_RUN_ASSET_DIRS = orig_dirs + (rel,)
                else:
                    inst._FIRST_RUN_ASSET_FILES = orig_files + (rel,)
                try:
                    res = inst.retire(announce=lambda _t: None)
                finally:
                    inst._FIRST_RUN_ASSET_DIRS, inst._FIRST_RUN_ASSET_FILES = orig_dirs, orig_files
                survived = os.path.exists(victim)
                engine_self_present = os.path.isfile(os.path.join(d, ".engine", "tools", "instantiator.py"))
        self.assertTrue(res["refused"])
        self.assertEqual(res["reason"], "unsafe-retire-target",
                         "refused for the guard's reason, distinct from the pre-existing inconsistent-refuse")
        self.assertEqual(res["target"], rel)
        self.assertEqual(res["deleted"], [], "fail-closed — nothing is deleted when a target is unsafe")
        self.assertTrue(survived, "the operator's own path must survive an unsafe retire entry")
        self.assertTrue(engine_self_present,
                        "no engine asset was retired either — the whole set is refused up front (no half-retire)")

    def test_refuses_an_unsafe_directory_entry_and_deletes_nothing(self):
        self._refuses_and_preserves("dir", os.path.join("src", "app"),
                                    lambda p: os.makedirs(p, exist_ok=True))

    def test_refuses_an_unsafe_file_entry_and_deletes_nothing(self):
        def _plant(p):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("the operator's own file\n")
        self._refuses_and_preserves("file", "IMPORTANT.md", _plant)


class TestRetireIsolation(unittest.TestCase):
    def test_retire_under_redirect_leaves_real_files_untouched(self):
        # The most dangerous demo: retire deletes its own source. A redirect leak would delete the REAL tool.
        snap = inst._snapshot_real_files()
        real_self = os.path.join(inst.validate.ROOT, ".engine", "tools", "instantiator.py")
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d):
                _finished_fixture(d)
                inst.retire(announce=lambda t: None)
        self.assertTrue(inst._assert_real_files_unchanged(snap), "a redirected retire must not touch real files")
        self.assertTrue(os.path.isfile(real_self), "the real setup tool must survive a redirected retire")


class TestFirstRunAssetsManifestParity(unittest.TestCase):
    """The committed .engine/provisioning/first-run-assets.json manifest mirrors the operational retire-set
    literal here, so the first-run reference-closure check can read the removed set without importing this
    (retired) module. The manifest is authored, not derived — this parity test is what binds them (it runs in
    the construction repo's CI, where both exist; both vanish together at retirement)."""
    def _manifest(self):
        with open(os.path.join(inst.validate.ROOT, ".engine", "provisioning", "first-run-assets.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)

    def test_manifest_files_match_the_retire_set_literal(self):
        self.assertEqual(sorted(self._manifest()["files"]), sorted(inst._FIRST_RUN_ASSET_FILES),
                         "first-run-assets.json `files` drifted from instantiator._FIRST_RUN_ASSET_FILES")

    def test_manifest_directories_match_the_retire_set_literal(self):
        self.assertEqual(sorted(self._manifest()["directories"]), sorted(inst._FIRST_RUN_ASSET_DIRS),
                         "first-run-assets.json `directories` drifted from instantiator._FIRST_RUN_ASSET_DIRS")

    def test_the_audit_digest_is_retired_so_a_generated_repo_starts_clean(self):
        # #404: the committed audit self-review digest is THIS template's construction history; it must be
        # in the retire set (both sources) so a generated repo starts with no inherited self-review — its absence
        # is the honest "not yet self-reviewed" state, and the audit cron writes a real one on its first run.
        self.assertIn(".engine/audits/audit-digest.md", inst._FIRST_RUN_ASSET_FILES)
        self.assertIn(".engine/audits/audit-digest.md", self._manifest()["files"])

    def test_the_467_namespace_demo_is_retired_so_it_does_not_travel(self):
        # #467: the deployment-eADR-namespace falsification is maintainer build evidence (covered by the
        # test_contract / test_knowledge regressions), not operator capability, so it retires at first-run
        # rather than ship into a generated repo. Mirrored in both sources (parity).
        self.assertIn(".engine/tools/demo_467_deployment_eadr_namespace.py", inst._FIRST_RUN_ASSET_FILES)
        self.assertIn(".engine/tools/demo_467_deployment_eadr_namespace.py", self._manifest()["files"])

    def test_the_marketing_banner_is_retired_so_a_generated_repo_carries_no_banner(self):
        # #410: the engine's marketing banner is referenced only by the template's marketing landing README
        # (which the first-run reseed replaces with a product starter). It must be in the retire set (both sources)
        # so a generated repo carries no engine marketing residue. Retired
        # as the specific FILE, not the assets/ DIRECTORY — see the brownfield-safety test below.
        self.assertIn("assets/engine_banner.jpg", inst._FIRST_RUN_ASSET_FILES)
        self.assertIn("assets/engine_banner.jpg", self._manifest()["files"])
        self.assertNotIn("assets", inst._FIRST_RUN_ASSET_DIRS,
                         "retiring the whole assets/ dir would delete a brownfield adopter's own assets/")


class TestFinishCopy(unittest.TestCase):
    def test_template_carries_every_finish_section(self):
        copy = inst.load_copy(inst.TEMPLATE_PATH)
        for key in _FINISH_KEYS:
            self.assertTrue(copy[key].strip(), f"finish copy section {key!r} missing from the template")


class TestFinishDemoRunsGreen(unittest.TestCase):
    def test_finish_demo_exits_zero(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = inst.main(["finish-demo"])
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn("byte-for-byte unchanged", out, "the isolation guarantee is shown")
        self.assertIn("still exists: True", out, "the demo proves the real setup tool survives its own tidy-up")
        self.assertIn("naming it, not hiding it", out, "the honest-ceiling banner leads the demo")


class TestFinishCli(unittest.TestCase):
    def _silent(self):
        import contextlib, io
        return contextlib.redirect_stdout(io.StringIO())

    def test_verify_verb_exits_zero_on_clean(self):
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d), self._silent():
                _finished_fixture(d)
                rc = inst.main(["verify", "--first-run"])
            self.assertEqual(rc, 0)

    def test_verify_verb_exits_one_on_a_hard_finding(self):
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d), self._silent():
                _finished_fixture(d)
                inst.wiring.apply(inst._ORPHAN_WIRE)
                rc = inst.main(["verify", "--first-run"])
            self.assertEqual(rc, 1, "a hard finding makes the verify verb exit non-zero")

    def test_retire_verb_completes_on_clean(self):
        with tempfile.TemporaryDirectory() as d:
            with inst._redirect_root(d), self._silent():
                _finished_fixture(d)
                _make_deployed(d)                               # a real copy (origin != home) — not the workshop
                rc = inst.main(["retire", "--first-run"])
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(os.path.join(d, ".engine", "tools", "instantiator.py")))


# ==== BROWNFIELD COLLISION CHECK ====================================================

_COLLISION_KEYS = ("collision-intro", "collision-exclusive", "collision-shared", "collision-codeowners",
                   "collision-none", "collision-unreadable")
# A representative engine-owned path set the live caller passes (from the release tree). Tests
# inject this for determinism rather than leaning on the construction repo's own owned set.
_COLLISION_ENGINE_PATHS = [".engine/engine.json", ".engine/tools/boot.py", ".github/CODEOWNERS",
                           "CLAUDE.md", ".github/workflows/engine-ci.yml"]


class TestCollisionCheck(unittest.TestCase):
    def _check(self, tmp, engine_paths=None):
        return inst.collision_check(root=tmp, engine_paths=engine_paths or _COLLISION_ENGINE_PATHS)

    def test_clean_project_has_no_overlaps(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_collision_fixture(d, populated=False)
            res = self._check(d)
            self.assertTrue(res["clean"])
            self.assertEqual(res["collisions"], [])

    def test_populated_project_surfaces_all_three_kinds_each_actionable(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_collision_fixture(d, populated=True)
            res = self._check(d)
            self.assertEqual({c["klass"] for c in res["collisions"]}, {1, 2, 3})
            for c in res["collisions"]:
                self.assertTrue(c["consequence"], "each overlap states a plain consequence, not a raw report")
                self.assertEqual(c["choices"], ["accept", "leave-as-is", "abort"])
                self.assertTrue(c["paths"], "each overlap names concrete project paths, never a bare pattern")

    def test_class1_names_the_product_file_at_an_engine_path(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_collision_fixture(d, populated=True)
            c1 = [c for c in self._check(d)["collisions"] if c["klass"] == 1][0]
            self.assertIn(".engine/legacy/notes.txt", c1["paths"])

    def test_class1_catches_a_symlink_at_an_engine_path(self):
        # A product symlink standing in for the engine's corner — os.path.isfile would miss it; exists/islink
        # catches it.
        with tempfile.TemporaryDirectory() as d:
            inst._build_collision_fixture(d, populated=False)
            target = os.path.join(d, "real.txt")
            open(target, "w").close()
            os.symlink(target, os.path.join(d, ".engine"))
            c1 = [c for c in self._check(d)["collisions"] if c["klass"] == 1]
            self.assertTrue(c1, "a symlink at an engine-exclusive path is a class-1 overlap")
            self.assertIn(".engine", c1[0]["paths"])

    def test_class2_is_per_file_kind_and_additive(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_collision_fixture(d, populated=True)
            c2 = {p for c in self._check(d)["collisions"] if c["klass"] == 2 for p in c["paths"]}
            self.assertIn(".gitignore", c2, "a fenced-text file with product content is an additive overlap")
            self.assertIn(".mcp.json", c2, "a keyed-JSON file with product content is an additive overlap")
            self.assertIn("CLAUDE.md", c2, "the project guide is surfaced by presence (additive)")

    def test_codeowners_and_claude_md_are_never_class1(self):
        # Decision 1/2: a pre-existing CODEOWNERS or project guide must co-exist (additive/shadow), NEVER be
        # reported as "the engine would replace it".
        with tempfile.TemporaryDirectory() as d:
            inst._build_collision_fixture(d, populated=True)
            c1 = {p for c in self._check(d)["collisions"] if c["klass"] == 1 for p in c["paths"]}
            self.assertNotIn(".github/CODEOWNERS", c1)
            self.assertNotIn("CLAUDE.md", c1)

    def test_shared_resume_does_not_reflag(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_collision_fixture(d, populated=True)
            inst._plant_engine_entries(d)                 # the engine's entries are now in place
            c2 = {p for c in self._check(d)["collisions"] if c["klass"] == 2 for p in c["paths"]}
            self.assertNotIn(".gitignore", c2, "an already-marked file is a resume, not re-flagged")
            self.assertNotIn(".mcp.json", c2, "an already-wired query-server file is not re-flagged")

    def test_empty_or_absent_shared_files_are_clean(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_collision_fixture(d, populated=False)
            open(os.path.join(d, ".gitignore"), "w").close()       # present but empty
            inst._write_json(os.path.join(d, ".mcp.json"), {})      # present but empty
            self.assertTrue(self._check(d)["clean"], "absent/empty shared files are a clean seed")

    def test_malformed_shared_file_is_surfaced_not_crashed(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_collision_fixture(d, populated=False)
            with open(os.path.join(d, ".mcp.json"), "w", encoding="utf-8") as fh:
                fh.write("{not json")
            hit = [c for c in self._check(d)["collisions"] if ".mcp.json" in c["paths"]]
            self.assertTrue(hit, "a malformed shared file is surfaced (leave-untouched), never crashed on")
            self.assertEqual(hit[0]["detail"].get("reason"), "unreadable")

    def test_non_utf8_shared_text_file_is_surfaced_not_crashed(self):
        # A mis-encoded TEXT shared file must fail-soft to the unreadable finding, never crash the check
        # (UnicodeDecodeError is a ValueError, not OSError — the original _read_text helper missed it).
        with tempfile.TemporaryDirectory() as d:
            inst._build_collision_fixture(d, populated=False)
            with open(os.path.join(d, ".gitignore"), "wb") as fh:
                fh.write(b"\xff\xfe not valid utf-8\n")
            hit = [c for c in self._check(d)["collisions"] if ".gitignore" in c["paths"]]
            self.assertTrue(hit, "a non-UTF-8 shared file is surfaced (leave-untouched), never crashed on")
            self.assertEqual(hit[0]["detail"].get("reason"), "unreadable")

    def test_class1_finds_hidden_files_in_the_engine_corner(self):
        # The engine corner is WALKED, not `**`-globbed: a product .engine/ whose contents are only under
        # dot-prefixed names must NOT escape class 1 (a `**` glob skips hidden entries on Python 3.9).
        with tempfile.TemporaryDirectory() as d:
            inst._build_collision_fixture(d, populated=False)
            os.makedirs(os.path.join(d, ".engine", ".hidden"))
            with open(os.path.join(d, ".engine", ".hidden", "f.txt"), "w", encoding="utf-8") as fh:
                fh.write("a hidden product file in the engine corner\n")
            c1 = {p for c in self._check(d)["collisions"] if c["klass"] == 1 for p in c["paths"]}
            self.assertIn(".engine/.hidden/f.txt", c1, "a hidden product file in the engine corner is caught")

    def test_class2_settings_json_additive_then_resume(self):
        # The most consequential shared file (products commonly ship their own): a product .claude/settings.json
        # is an additive overlap; once an engine hook is present it is a resume (no re-flag).
        with tempfile.TemporaryDirectory() as d:
            inst._build_collision_fixture(d, populated=True)
            c2 = {p for c in self._check(d)["collisions"] if c["klass"] == 2 for p in c["paths"]}
            self.assertIn(".claude/settings.json", c2, "a product settings file is an additive overlap")
            inst._plant_engine_entries(d)
            c2b = {p for c in self._check(d)["collisions"] if c["klass"] == 2 for p in c["paths"]}
            self.assertNotIn(".claude/settings.json", c2b, "an engine-hook-present settings file is a resume")

    def test_malformed_codeowners_block_is_surfaced_not_crashed(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".github"))
            with open(os.path.join(d, ".github", "CODEOWNERS"), "w", encoding="utf-8") as fh:
                fh.write(inst.wiring.FENCE_BEGIN.format(id="codeowners") + "\n* @x\n")   # begin, no end
            hit = [c for c in self._check(d)["collisions"] if ".github/CODEOWNERS" in c["paths"]]
            self.assertTrue(hit, "a malformed engine block in CODEOWNERS is surfaced, never crashed on")
            self.assertEqual(hit[0]["detail"].get("reason"), "unreadable")

    def test_class3_expansive_rule_flags_disjoint_rule_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".github"))
            with open(os.path.join(d, ".github", "CODEOWNERS"), "w", encoding="utf-8") as fh:
                fh.write("/src/ @team\n* @everyone\n")
            rules = [c["detail"]["rule"] for c in self._check(d)["collisions"] if c["klass"] == 3]
            self.assertTrue(any("@everyone" in r for r in rules), "the expansive rule shadows engine paths")
            self.assertFalse(any("/src/" in r for r in rules), "a disjoint product rule is not flagged")

    def test_class3_excludes_the_engines_own_block(self):
        # The engine's own review rules (inside its marked block) must never read as a product shadow.
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".github"))
            block = inst.wiring.render_codeowners("", [".engine/engine.json"], "@owner")
            with open(os.path.join(d, ".github", "CODEOWNERS"), "w", encoding="utf-8") as fh:
                fh.write(block)
            self.assertEqual([c for c in self._check(d)["collisions"] if c["klass"] == 3], [])

    def test_checked_counts_prove_non_vacuous(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_collision_fixture(d, populated=True)
            res = self._check(d)
            self.assertGreater(res["checked"]["exclusive_globs"], 0)
            self.assertGreater(res["checked"]["shared_files"], 0)
            self.assertEqual(res["checked"]["engine_paths"], len(_COLLISION_ENGINE_PATHS))

    def test_default_engine_paths_use_the_owned_set(self):
        # No engine_paths injected → the check uses the engine's own owned set (the live caller passes the
        # release set). Non-empty here (the construction repo owns many files); the detection still reads `root`.
        with tempfile.TemporaryDirectory() as d:
            inst._build_collision_fixture(d, populated=False)
            res = inst.collision_check(root=d)
            self.assertGreater(res["checked"]["engine_paths"], 0)
            self.assertTrue(res["clean"], "a clean product fixture is clean even against the real owned set")


class TestCollisionCopy(unittest.TestCase):
    def test_template_carries_every_collision_section(self):
        copy = inst.load_copy(inst.TEMPLATE_PATH)
        for key in _COLLISION_KEYS:
            self.assertTrue(copy[key].strip(), f"overlap copy section {key!r} missing from the template")


class TestCollisionDemoRunsGreen(unittest.TestCase):
    def test_collision_demo_exits_zero(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = inst.main(["collision-demo"])
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn("live first step", out.lower(),
                      "the now-live-trigger disclosure is printed (docstrings are code the operator can't read)")
        self.assertIn("byte-for-byte unchanged", out, "the isolation guarantee is shown")
        self.assertIn("naming it, not hiding it", out, "the honest-ceiling banner leads the demo")


class TestCollisionCli(unittest.TestCase):
    def test_collision_check_verb_short_circuits_in_the_workshop(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = inst.main(["collision-check"])
        self.assertEqual(rc, 0)
        self.assertIn("workshop", buf.getvalue().lower(),
                      "the bare verb short-circuits read-only here, never self-flagging the engine's own files")


class TestSharedStateClaudeFenceAware(unittest.TestCase):
    """The CLAUDE.md branch of _shared_state is fence-aware (#234 6b): an already-present engine floor is a
    'resume' (no flag), a pre-existing project guide is 'additive'."""
    def _state(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "CLAUDE.md")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
            return inst._shared_state("CLAUDE.md", p)

    def test_floor_fence_present_is_resume(self):
        import wiring
        fenced = wiring.fence_apply("# Our guide\n", inst._FLOOR_FENCE, ["Project status block."],
                                    style=wiring.MD_FENCE)
        self.assertEqual(self._state(fenced), "resume")

    def test_operator_guide_without_fence_is_additive(self):
        self.assertEqual(self._state("# Our own project guide\nBuild with make.\n"), "additive")

    def test_empty_is_empty(self):
        self.assertEqual(self._state("   \n"), "empty")

    def test_unreadable_is_unreadable(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "CLAUDE.md")
            with open(p, "wb") as fh:
                fh.write(b"\xff\xfe\x00bad")
            self.assertEqual(inst._shared_state("CLAUDE.md", p), "unreadable")


class TestCollisionClaudeFenceAware(unittest.TestCase):
    """Through the live collision_check: a brownfield CLAUDE.md already carrying the engine floor is a resume
    (not surfaced); an operator guide with no floor is surfaced as a class-2 additive overlap."""
    def test_existing_engine_floor_is_not_surfaced(self):
        import wiring
        with tempfile.TemporaryDirectory() as d:
            inst._build_collision_fixture(d, populated=True)
            fenced = wiring.fence_apply("# Our guide\n", inst._FLOOR_FENCE, ["Project status."],
                                        style=wiring.MD_FENCE)
            with open(os.path.join(d, "CLAUDE.md"), "w", encoding="utf-8") as fh:
                fh.write(fenced)
            res = inst.collision_check(root=d, engine_paths=_COLLISION_ENGINE_PATHS)
            shared = {p for c in res["collisions"] if c["klass"] == 2 for p in c["paths"]}
            self.assertNotIn("CLAUDE.md", shared)

    def test_operator_guide_is_surfaced_additive(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_collision_fixture(d, populated=True)   # plants an unfenced product CLAUDE.md
            res = inst.collision_check(root=d, engine_paths=_COLLISION_ENGINE_PATHS)
            shared = {p for c in res["collisions"] if c["klass"] == 2 for p in c["paths"]}
            self.assertIn("CLAUDE.md", shared)


class TestDetectTeam(unittest.TestCase):
    """Brownfield team detection: read-only, any one signal recommends team, degrades to not-detected when gh
    can't answer (never a false positive)."""
    def _codeowners(self, d, text):
        os.makedirs(os.path.join(d, ".github"), exist_ok=True)
        with open(os.path.join(d, ".github", "CODEOWNERS"), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_multi_owner_codeowners_is_a_local_signal(self):
        with tempfile.TemporaryDirectory() as d:
            self._codeowners(d, "* @org/alice @org/bob\n")
            res = inst.detect_team(root=d, gh_api=lambda path: None)
            self.assertTrue(res["detected"])
            self.assertTrue(res["reason"])

    def test_single_owner_no_gh_is_not_detected(self):
        with tempfile.TemporaryDirectory() as d:
            self._codeowners(d, "* @solo\n")
            res = inst.detect_team(root=d, gh_api=lambda path: None)
            self.assertFalse(res["detected"])

    def test_organization_owner_signals_team(self):
        with tempfile.TemporaryDirectory() as d:
            self._codeowners(d, "* @solo\n")
            gh = lambda path: {"owner": {"type": "Organization"}} if "required_pull_request_reviews" not in path \
                else None
            with mock.patch.object(inst.boot, "repo_slug", return_value="acme/widgets"):
                res = inst.detect_team(root=d, gh_api=gh)
            self.assertTrue(res["detected"])

    def test_existing_required_reviews_signals_team(self):
        with tempfile.TemporaryDirectory() as d:
            self._codeowners(d, "* @solo\n")
            gh = lambda path: {"required_approving_review_count": 1} \
                if "required_pull_request_reviews" in path else {"owner": {"type": "User"}}
            with mock.patch.object(inst.boot, "repo_slug", return_value="solo/widgets"):
                res = inst.detect_team(root=d, gh_api=gh)
            self.assertTrue(res["detected"])

    def test_gh_failure_degrades_to_not_detected(self):
        with tempfile.TemporaryDirectory() as d:
            self._codeowners(d, "* @solo\n")
            with mock.patch.object(inst.boot, "repo_slug", return_value="solo/widgets"):
                res = inst.detect_team(root=d, gh_api=lambda path: None)   # every gh read unavailable
            self.assertFalse(res["detected"])


class TestGatherTeamRecommendation(unittest.TestCase):
    def test_recommendation_shown_when_a_team_is_detected(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(inst.boot, "repo_slug", return_value="acme/widgets"):
            with inst._redirect_root(d):
                text = inst.present_gather(root=d, team={"detected": True, "reason": "x", "signals": ["x"]})
        self.assertIn("already has a team", text)

    def test_recommendation_absent_when_solo(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(inst.boot, "repo_slug", return_value="solo/widgets"):
            with inst._redirect_root(d):
                text = inst.present_gather(root=d, team={"detected": False, "reason": None, "signals": []})
        self.assertNotIn("already has a team", text)


class TestInsertFloor(unittest.TestCase):
    """The INSERT-on-arrival floor: append-when-absent, never a duplicate, sourced from the release's root
    CLAUDE.md `floor` fence (#323)."""
    def _release_with_floor(self, d):
        import wiring
        with open(os.path.join(d, "CLAUDE.md"), "w", encoding="utf-8") as fh:
            fh.write(wiring.fence_apply("", inst._FLOOR_FENCE,
                     ["# Your project runs on an Engine", "", "Project status block."], style=wiring.MD_FENCE))

    def test_inserts_into_operator_guide_keeping_content(self):
        import wiring
        with tempfile.TemporaryDirectory() as d:
            rel, tgt = os.path.join(d, "rel"), os.path.join(d, "tgt")
            os.makedirs(rel); os.makedirs(tgt)
            self._release_with_floor(rel)
            with open(os.path.join(tgt, "CLAUDE.md"), "w", encoding="utf-8") as fh:
                fh.write("# Our guide\n\nHow we work.\n")
            with inst._redirect_root(tgt):
                self.assertEqual(inst._insert_floor(rel), "inserted")
                after = inst._read_text_or(os.path.join(tgt, "CLAUDE.md"), "")
            self.assertIn("How we work.", after)                              # operator content kept
            self.assertEqual(after.count(wiring._MD_FENCE_BEGIN_TOKEN), 1)    # exactly one floor fence

    def test_present_floor_is_not_duplicated(self):
        import wiring
        with tempfile.TemporaryDirectory() as d:
            rel, tgt = os.path.join(d, "rel"), os.path.join(d, "tgt")
            os.makedirs(rel); os.makedirs(tgt)
            self._release_with_floor(rel)
            fenced = wiring.fence_apply("# Our guide\n", inst._FLOOR_FENCE, ["old floor"], style=wiring.MD_FENCE)
            with open(os.path.join(tgt, "CLAUDE.md"), "w", encoding="utf-8") as fh:
                fh.write(fenced)
            with inst._redirect_root(tgt):
                self.assertEqual(inst._insert_floor(rel), "present")
                after = inst._read_text_or(os.path.join(tgt, "CLAUDE.md"), "")
            self.assertEqual(after.count(wiring._MD_FENCE_BEGIN_TOKEN), 1)    # still exactly one

    def test_no_release_floor_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            rel, tgt = os.path.join(d, "rel"), os.path.join(d, "tgt")
            os.makedirs(rel); os.makedirs(tgt)
            with inst._redirect_root(tgt):
                self.assertEqual(inst._insert_floor(rel), "skipped")

    def test_malformed_local_fence_degrades(self):
        with tempfile.TemporaryDirectory() as d:
            rel, tgt = os.path.join(d, "rel"), os.path.join(d, "tgt")
            os.makedirs(rel); os.makedirs(tgt)
            self._release_with_floor(rel)
            import wiring
            with open(os.path.join(tgt, "CLAUDE.md"), "w", encoding="utf-8") as fh:
                fh.write(wiring.MD_FENCE_BEGIN.format(id=inst._FLOOR_FENCE) + "\nunterminated\n")  # no END
            with inst._redirect_root(tgt):
                self.assertEqual(inst._insert_floor(rel), "degraded")


def _arrive_fakes():
    """Every external boundary arrive() threads into apply/detect_team faked, so the REAL arrival runs with
    nothing real touched (mirrors _finish_apply) — including the GitHub team-detection read (gh_api)."""
    return dict(home_reader=lambda: {}, uv_present=lambda: None, uv_installer=lambda: "uv",
                uv_runner=lambda uv, g: True, consent=lambda kind: True,
                control_transport=inst._approve_transport(), gh_refresh=lambda s: True,
                control_issues=inst._FakeIssues(), gh_api=lambda path: None,
                control_repo="you/your-project", control_token="demo-token")


class TestArrive(unittest.TestCase):
    def test_surface_only_writes_nothing_even_with_no_overlaps(self):
        # The BLOCKING case: a clean project with no overlaps must NOT be installed by the read-only step.
        with tempfile.TemporaryDirectory() as d:
            target, release = os.path.join(d, "p"), os.path.join(d, "r")
            os.makedirs(os.path.join(target, "src"))
            with open(os.path.join(target, "src", "app.py"), "w") as fh:
                fh.write("x = 1\n")                                   # a clean project: nothing in the way
            inst._build_fixture(release)
            prs = []
            res = inst.arrive(target_root=target, release_tree=release, announce=lambda t: None,
                              opener=lambda **k: prs.append(k), **_arrive_fakes())  # apply_changes defaults False
            self.assertTrue(res["surfaced"])
            self.assertFalse(res["proceeded"])
            self.assertEqual(res["collisions"], [])
            self.assertFalse(os.path.isdir(os.path.join(target, ".engine")))   # nothing written
            self.assertEqual(prs, [])

    def test_surface_only_with_overlaps_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            target, release = os.path.join(d, "p"), os.path.join(d, "r")
            os.makedirs(target); inst._build_arrival_product(target); inst._build_fixture(release)
            snap = {p: inst._read_text_or(os.path.join(target, p), "")
                    for p in ("CLAUDE.md", ".gitignore", ".github/CODEOWNERS")}
            prs = []
            res = inst.arrive(target_root=target, release_tree=release, announce=lambda t: None,
                              opener=lambda **k: prs.append(k), **_arrive_fakes())  # surface-only
            self.assertTrue(res["surfaced"])
            self.assertFalse(res["proceeded"])
            self.assertTrue(res["collisions"])
            self.assertEqual({p: inst._read_text_or(os.path.join(target, p), "") for p in snap}, snap)
            self.assertFalse(os.path.isdir(os.path.join(target, ".engine")))
            self.assertEqual(prs, [])

    def test_abort_at_an_overlap_writes_nothing_and_opens_no_pr(self):
        with tempfile.TemporaryDirectory() as d:
            target, release = os.path.join(d, "p"), os.path.join(d, "r")
            os.makedirs(target); inst._build_arrival_product(target); inst._build_fixture(release)
            snap = {p: inst._read_text_or(os.path.join(target, p), "")
                    for p in ("CLAUDE.md", ".gitignore", ".github/CODEOWNERS")}
            prs = []
            res = inst.arrive(target_root=target, release_tree=release, decide=lambda c: "abort",
                              apply_changes=True, announce=lambda t: None,
                              opener=lambda **k: prs.append(k), **_arrive_fakes())
            self.assertFalse(res["proceeded"])
            self.assertEqual({p: inst._read_text_or(os.path.join(target, p), "") for p in snap}, snap)
            self.assertFalse(os.path.isdir(os.path.join(target, ".engine")))
            self.assertEqual(prs, [])

    def test_empty_release_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            target, release = os.path.join(d, "p"), os.path.join(d, "r")
            os.makedirs(target); inst._build_arrival_product(target); os.makedirs(release)  # release has no modules
            res = inst.arrive(target_root=target, release_tree=release, apply_changes=True,
                              decide=lambda c: "accept", announce=lambda t: None,
                              opener=lambda **k: {"number": 1}, **_arrive_fakes())
            self.assertEqual(res["stopped_on"], "release")
            self.assertFalse(os.path.isdir(os.path.join(target, ".engine")))

    def test_accept_proceeds_inserts_one_floor_and_opens_one_pr_for_the_target(self):
        import wiring
        with tempfile.TemporaryDirectory() as d:
            target, release = os.path.join(d, "p"), os.path.join(d, "r")
            os.makedirs(target); inst._build_arrival_product(target); inst._build_fixture(release)
            prs = []
            res = inst.arrive(target_root=target, release_tree=release, engine_release="v1", tier="team",
                              handle="you", decide=lambda c: "accept", apply_changes=True,
                              announce=lambda t: None,
                              opener=lambda **k: prs.append(k) or {"number": 1}, **_arrive_fakes())
            guide = inst._read_text_or(os.path.join(target, "CLAUDE.md"), "")
            self.assertTrue(res["proceeded"])
            self.assertEqual(guide.count(wiring._MD_FENCE_BEGIN_TOKEN), 1)
            self.assertIn("How we work here.", guide)                 # operator content preserved
            self.assertTrue(os.path.isfile(os.path.join(target, ".engine", "modules", "core", "manifest.json")))
            self.assertEqual(len(prs), 1)
            self.assertEqual(prs[0].get("repo"), "you/your-project")  # the PR is aimed at the TARGET's slug

    def test_live_writes_target_the_derived_slug_not_the_cwd(self):
        # No control_repo injected: the slug must be read from the TARGET's own git remote, not the process cwd.
        import subprocess
        fakes = _arrive_fakes(); fakes.pop("control_repo")
        with tempfile.TemporaryDirectory() as d:
            target, release = os.path.join(d, "p"), os.path.join(d, "r")
            os.makedirs(target); inst._build_arrival_product(target); inst._build_fixture(release)
            for args in (["git", "-C", target, "init", "-q"],
                         ["git", "-C", target, "remote", "add", "origin",
                          "https://github.com/acme/their-product.git"]):
                subprocess.run(args, check=True, capture_output=True)
            prs = []
            inst.arrive(target_root=target, release_tree=release, tier="solo", handle="you",
                        decide=lambda c: "accept", apply_changes=True, announce=lambda t: None,
                        opener=lambda **k: prs.append(k) or {"number": 1}, **fakes)
            self.assertEqual(prs[0].get("repo"), "acme/their-product")   # the target's remote, not cwd's

    def test_brownfield_seeding_leaves_owner_files_as_they_are(self):
        with tempfile.TemporaryDirectory() as d:
            target, release = os.path.join(d, "p"), os.path.join(d, "r")
            os.makedirs(target); inst._build_arrival_product(target); inst._build_fixture(release)
            inst.arrive(target_root=target, release_tree=release, tier="team", handle="you",
                        decide=lambda c: "accept", apply_changes=True, announce=lambda t: None,
                        opener=lambda **k: {"number": 1}, **_arrive_fakes())
            self.assertIn("security@ourproduct.example", inst._read_text_or(os.path.join(target, "SECURITY.md"), ""))
            self.assertIn("Our Product Inc.", inst._read_text_or(os.path.join(target, "LICENSE"), ""))
            self.assertNotIn(inst._MARKETING_SEED_MARKER, inst._read_text_or(os.path.join(target, "README.md"), ""))

    def test_does_not_touch_the_real_tree(self):
        snap = inst._snapshot_real_files()
        root_before = validate.ROOT
        with tempfile.TemporaryDirectory() as d:
            target, release = os.path.join(d, "p"), os.path.join(d, "r")
            os.makedirs(target); inst._build_arrival_product(target); inst._build_fixture(release)
            inst.arrive(target_root=target, release_tree=release, tier="team", handle="you",
                        decide=lambda c: "accept", apply_changes=True, announce=lambda t: None,
                        opener=lambda **k: {"number": 1}, **_arrive_fakes())
        self.assertTrue(inst._assert_real_files_unchanged(snap))
        self.assertEqual(validate.ROOT, root_before)   # ROOT restored after arrive's redirect


class TestArrivalDemoRunsGreen(unittest.TestCase):
    def test_arrival_demo_returns_true(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = inst.arrival_demo()
        out = buf.getvalue()
        self.assertTrue(ok, out)
        self.assertIn("the step the fixture cannot discharge", out, "the inductive ceiling is named")
        self.assertIn("byte-for-byte unchanged", out, "the isolation guarantee is shown")


class TestAugmentDemoRunsGreen(unittest.TestCase):
    def test_augment_demo_returns_true(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = inst.augment_demo()
        out = buf.getvalue()
        self.assertTrue(ok, out)
        self.assertIn("byte-for-byte unchanged", out, "the never-weaken guarantee is shown")
        self.assertIn("fixture cannot discharge", out, "the inductive ceiling is named")


class TestSeedProductVersion(unittest.TestCase):
    """The product-version seed (#516): a deployed repo gets its own product-version.json at first-run so the
    release path cuts the PRODUCT once deployed. Seed-iff-absent; the home-repo belt protects the workshop;
    disclosed on an actual seed."""

    def setUp(self):
        # The belt keys on repo_identity.is_home_repo; default to a DEPLOYED repo (not home) so the seed logic
        # runs. The belt itself has its own test (test_home_repo_belt_never_seeds).
        p = mock.patch.object(inst.repo_identity, "is_home_repo", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def _pv(self, d):
        with open(os.path.join(d, "product-version.json")) as fh:
            return json.load(fh)

    def test_seeds_product_version_in_a_deployed_root(self):
        said = []
        with tempfile.TemporaryDirectory() as d, inst._redirect_root(d):
            outcome = inst._seed_product_version(said.append, inst.load_copy())
            self.assertEqual(outcome, "seeded")
            self.assertEqual(self._pv(d), {"version": "0.0.0"})
        self.assertTrue(said, "the seed is disclosed, never silent")
        self.assertIn("product-version.json", "\n".join(said))

    def test_home_repo_belt_never_seeds(self):
        # BELT: in the engine's OWN home repo (repo_identity.is_home_repo — the workshop, where the engine IS the
        # product) -> never seed a product file; the engine cuts the engine version there.
        with tempfile.TemporaryDirectory() as d, inst._redirect_root(d), \
                mock.patch.object(inst.repo_identity, "is_home_repo", return_value=True):
            outcome = inst._seed_product_version(lambda t: None, inst.load_copy())
        self.assertEqual(outcome, "present")
        self.assertFalse(os.path.exists(os.path.join(d, "product-version.json")))

    def test_existing_file_is_an_idempotent_noop(self):
        said = []
        with tempfile.TemporaryDirectory() as d, inst._redirect_root(d):
            with open(os.path.join(d, "product-version.json"), "w") as fh:
                fh.write('{"version": "1.4.2"}\n')
            outcome = inst._seed_product_version(said.append, inst.load_copy())
            self.assertEqual(outcome, "present")
            self.assertEqual(self._pv(d), {"version": "1.4.2"}, "an operator's own version is never overwritten")
        self.assertEqual(said, [], "no disclosure on a no-op")


if __name__ == "__main__":
    unittest.main()
