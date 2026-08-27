#!/usr/bin/env python3
"""Adversarial tests for the one-time deployed-record retirement migration."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checkout_health  # noqa: E402
import plan_store  # noqa: E402


MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "modules", "core", "migrations", "retire_eadr_records.py")


def _load():
    spec = importlib.util.spec_from_file_location("retire_eadr_records_under_test", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


migration = _load()


class RetirementMigrationTests(unittest.TestCase):
    VALID_NAMES = (
        "eADR-0000.md",
        "eADR-9999-use-dir-fds.md",
        "acme-eADR-0001.md",
        "acme-project-eADR-1234-long-title.md",
    )
    NEAR_MISSES = (
        "eadr-0001.md", "EADR-0001.md", "eADR-001.md", "eADR-00001.md",
        "eADR-0001-.md", "eADR-0001-Bad.md", "Acme-eADR-0001.md",
        "acme--eADR-0001.md", "acme-eADR-0001-bad--title.md", "eADR-0001.txt",
    )

    @staticmethod
    def _git(root, *args, input=None):
        proc = subprocess.run(["git", "-C", root, *args], input=input, capture_output=True,
                              text=isinstance(input, str) or input is None, check=False)
        if proc.returncode:
            raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
        return proc.stdout.strip() if isinstance(proc.stdout, str) else proc.stdout

    def _repo(self, names=None, *, overrides=True):
        tmp = tempfile.TemporaryDirectory()
        root = tmp.name
        self._git(root, "init", "-b", "main")
        self._git(root, "config", "user.email", "test@example.invalid")
        self._git(root, "config", "user.name", "Retirement Test")
        instance = os.path.join(root, ".engine", "contracts", "instance")
        os.makedirs(instance)
        with open(os.path.join(instance, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("former authoring guide\n")
        for name in self.VALID_NAMES if names is None else names:
            with open(os.path.join(instance, name), "w", encoding="utf-8") as handle:
                handle.write(f"historical bytes for {name}\n")
        if overrides:
            with open(os.path.join(root, ".engine", "operator-overrides.json"), "w", encoding="utf-8") as handle:
                json.dump({"attention": {"excerpt_chars": 900},
                           "contract-threshold": {"contract_rate_max": 8},
                           "unrelated": {"kept": True}}, handle, indent=2)
                handle.write("\n")
        self._git(root, "add", "-A")
        self._git(root, "commit", "-m", "deployed fixture")
        return tmp

    @contextlib.contextmanager
    def _empty_plans(self):
        with tempfile.TemporaryDirectory() as plans, mock.patch.dict(
                os.environ, {"ENGINE_PLAN_DIR": os.path.join(plans, "absent")}, clear=False):
            yield

    @staticmethod
    def _context(root, checkpoint=None):
        context = {"root": root, "kind": "tracked-content", "module_id": "core",
                   "from_version": "0.6.3", "to_version": "0.7.0", "engine_version": "0.7.0"}
        if checkpoint is not None:
            context["checkpoint"] = checkpoint
        return context

    def _preflight(self, root):
        with self._empty_plans():
            result = migration.preflight(self._context(root))
        self.assertEqual(result.get("status"), "ready", result)
        return result

    @staticmethod
    def _sealed(plan):
        return {
            "schema_version": "tracked-content-plan.v1",
            "migration_id": "core@0.7.0",
            "module_id": "core",
            "version": "0.7.0",
            "run": "migrations/retire_eadr_records.py",
            "scope": [
                ".engine/contracts/instance",
                ".engine/operator-overrides.json",
                ".engine/.engine-upgrade-retirement-quarantine-overrides",
                ".engine/.engine-upgrade-retirement-next-overrides",
            ],
            "targets": [{**target, "recovery_scope": sorted(target["recovery_scope"])}
                        for target in plan["targets"]],
        }

    def test_historical_filename_grammar_and_exact_target_receipt(self):
        for name in self.VALID_NAMES:
            self.assertIsNotNone(migration._RECORD_RE.fullmatch(name), name)
        for name in self.NEAR_MISSES:
            self.assertIsNone(migration._RECORD_RE.fullmatch(name), name)
        tmp = self._repo()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        plan = self._preflight(root)
        expected = {f".engine/contracts/instance/{n}" for n in ("README.md", *self.VALID_NAMES)}
        expected.add(".engine/operator-overrides.json")
        self.assertEqual({t["path"] for t in plan["targets"]}, expected)
        with self._empty_plans():
            receipt = migration.apply(self._context(root), self._sealed(plan))
        self.assertEqual(receipt["status"], "applied")
        self.assertEqual({c["path"] for c in receipt["changes"]}, expected)
        self.assertFalse(os.path.exists(os.path.join(root, ".engine", "contracts")))
        with open(os.path.join(root, ".engine", "operator-overrides.json"), encoding="utf-8") as handle:
            kept = json.load(handle)
        self.assertEqual(kept, {"attention": {"excerpt_chars": 900}, "unrelated": {"kept": True}})
        residue = [p.name for p in Path(root, ".engine").rglob("*")
                   if "upgrade-retirement" in p.name]
        self.assertEqual(residue, [])

    def test_unsafe_tree_and_near_miss_refusal_matrix(self):
        for name in self.NEAR_MISSES:
            with self.subTest(name=name):
                tmp = self._repo([name])
                with self._empty_plans():
                    result = migration.preflight(self._context(tmp.name))
                self.assertEqual(result["status"], "refused")
                self.assertEqual(result["refusals"][0]["code"], "unexpected-name")
                self.assertEqual(result["refusals"][0]["path"], f".engine/contracts/instance/{name}")
                tmp.cleanup()

        cases = []
        untracked = self._repo([])
        extra = os.path.join(untracked.name, ".engine", "contracts", "instance", "surprise.txt")
        with open(extra, "w", encoding="utf-8") as handle:
            handle.write("outside the exact record set\n")
        cases.append(("untracked", untracked, "unsafe-tree"))

        missing = self._repo([])
        os.unlink(os.path.join(missing.name, ".engine", "contracts", "instance", "README.md"))
        cases.append(("missing", missing, "missing-target"))

        nested = self._repo([])
        nested_path = os.path.join(nested.name, ".engine", "contracts", "instance", "nested")
        os.makedirs(nested_path)
        with open(os.path.join(nested_path, "eADR-0001.md"), "w", encoding="utf-8") as handle:
            handle.write("nested\n")
        self._git(nested.name, "add", "-A")
        self._git(nested.name, "commit", "-m", "nested unsafe entry")
        cases.append(("nested", nested, "nested-entry"))

        linked = self._repo([])
        readme = os.path.join(linked.name, ".engine", "contracts", "instance", "README.md")
        os.unlink(readme)
        os.symlink("/tmp/retirement-test-outside", readme)
        self._git(linked.name, "add", "-A")
        self._git(linked.name, "commit", "-m", "linked unsafe entry")
        cases.append(("symlink", linked, "special-mode"))

        dirty = self._repo(["eADR-0001.md"])
        with open(os.path.join(dirty.name, ".engine", "contracts", "instance", "eADR-0001.md"),
                  "a", encoding="utf-8") as handle:
            handle.write("uncommitted change\n")
        cases.append(("dirty", dirty, "entry-raced"))

        override_link = self._repo([])
        override = os.path.join(override_link.name, ".engine", "operator-overrides.json")
        os.unlink(override)
        os.symlink("/tmp/retirement-test-outside", override)
        cases.append(("override-symlink", override_link, "unsafe-override"))

        for label, tmp, code in cases:
            with self.subTest(label=label), self._empty_plans():
                result = migration.preflight(self._context(tmp.name))
                self.assertEqual(result["status"], "refused", result)
                self.assertEqual(result["refusals"][0]["code"], code)
            tmp.cleanup()

    def test_refusal_can_be_remediated_and_retried_without_residue(self):
        tmp = self._repo(["eADR-0001.md"])
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        record = os.path.join(root, ".engine", "contracts", "instance", "eADR-0001.md")
        with open(record, "a", encoding="utf-8") as handle:
            handle.write("dirty interruption\n")
        with self._empty_plans():
            refused = migration.preflight(self._context(root))
        self.assertEqual(refused["status"], "refused")
        self.assertEqual(refused["refusals"][0]["code"], "entry-raced")
        self.assertIn("restore the tracked bytes", refused["refusals"][0]["remediation"].lower())

        original = self._git(root, "show", "HEAD:.engine/contracts/instance/eADR-0001.md") + "\n"
        with open(record, "w", encoding="utf-8") as handle:
            handle.write(original)
        plan = self._preflight(root)
        with self._empty_plans():
            receipt = migration.apply(self._context(root), self._sealed(plan))
        self.assertEqual(receipt["status"], "applied")
        self.assertFalse(os.path.exists(os.path.join(root, ".engine", "contracts")))
        self.assertEqual([p.name for p in Path(root, ".engine").rglob("*")
                          if "upgrade-retirement" in p.name], [])

    def test_apply_uses_the_sealed_baseline_inventory_after_candidate_overlay_stages_deletions(self):
        tmp = self._repo(["eADR-0001.md"])
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        plan = self._preflight(root)

        # The real updater seals preflight against the baseline, then overlays/stages the candidate. Its index
        # therefore no longer lists either retired path even though phase two deliberately keeps the live bytes
        # for the tracked-content migration. Apply must verify the sealed bytes, not rediscover through that
        # now-candidate index.
        self._git(root, "rm", "--cached", "-r", ".engine/contracts/instance",
                  ".engine/operator-overrides.json")
        self.assertEqual(self._git(root, "ls-files", "--", ".engine/contracts/instance"), "")
        with self._empty_plans():
            receipt = migration.apply(self._context(root), self._sealed(plan))
        self.assertEqual(receipt["status"], "applied")
        self.assertEqual({entry["path"] for entry in receipt["changes"]},
                         {target["path"] for target in plan["targets"]})
        self.assertFalse(os.path.exists(os.path.join(root, ".engine", "contracts")))

    def test_actionable_plan_compatibility_matrix(self):
        plans_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(plans_tmp.cleanup)
        root = Path(plans_tmp.name)

        class Library:
            def __init__(self, records, heads):
                self.root, self._records, self._heads = root, records, heads
            def slugs(self):
                return sorted(self._records)
            def read_record(self, slug):
                return self._records[slug]
            def head(self, slug):
                return self._heads[slug]

        rows = {
            "current": ({"plan_id": "pln_current", "status": "draft",
                         "current": {"snapshot": "revisions/000002.json"}},
                        {"description": "Use eADR-0042 for the next executable step"}),
            "historical-only": ({"plan_id": "pln_history", "status": "draft",
                                  "current": {"snapshot": "revisions/000002.json"}},
                                 {"revision_note": "Earlier work retired eADR authority.",
                                  "raw_intent": "Remove the old .engine/contracts collection.",
                                  "build_plan": {"description": "Current executable work uses no retired surface"}}),
            "closed": ({"plan_id": "pln_closed", "status": "complete",
                        "current": {"snapshot": "revisions/000003.json"}},
                       {"description": "Completed head may still say contract.v1"}),
            "retired": ({"plan_id": "pln_retired", "status": "retired",
                          "current": {"snapshot": "revisions/000004.json"}},
                        {"build_plan": {"description":
                         "Run .engine/tools/authority_reservation_check.py before reopening"}}),
            "abandoned": ({"plan_id": "pln_abandoned", "status": "abandoned",
                            "current": {"snapshot": "revisions/000005.json"}},
                          {"build_plan": {"description":
                           "Author the removed .engine/templates/contract.md artifact"}}),
        }
        records = {k: v[0] for k, v in rows.items()}
        heads = {k: v[1] for k, v in rows.items()}
        with mock.patch.object(plan_store, "PlanLibrary", return_value=Library(records, heads)), \
                mock.patch.object(plan_store, "derived_status", side_effect=lambda r: r["status"]):
            refusals = migration._plan_refusals("unused")
        self.assertEqual(len(refusals), 3)
        self.assertTrue(all(refusal["code"] == "actionable-plan-incompatible" for refusal in refusals))
        rendered = "\n".join(refusal["reason"] for refusal in refusals)
        for plan_id in ("pln_current", "pln_retired", "pln_abandoned"):
            self.assertIn(plan_id, rendered)
        self.assertNotIn("pln_closed", rendered)
        self.assertIn("revisions/000002.json", "\n".join(r["path"] for r in refusals))

    def test_tracked_engine_text_has_no_generic_authority_shortcut(self):
        # The retirement must not replace named evidence with a new, unnamed authority sink. Construct the
        # phrase so the test does not create the very tracked occurrence it is meant to forbid.
        root = Path(__file__).resolve().parents[2]
        banned = "the established " + "design"
        result = subprocess.run(
            ["git", "-C", str(root), "grep", "-n", "-I", "-F", banned, "--", "."],
            capture_output=True, text=True, check=False,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_dirfd_ancestor_swap_and_leaf_race_leave_external_bytes_untouched(self):
        tmp = self._repo([])
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        with tempfile.TemporaryDirectory() as outside:
            sentinel = os.path.join(outside, "README.md")
            with open(sentinel, "w", encoding="utf-8") as handle:
                handle.write("outside sentinel\n")
            contracts = os.path.join(root, ".engine", "contracts")
            moved = contracts + ".moved"
            os.rename(contracts, moved)
            os.symlink(outside, contracts)
            with self._empty_plans():
                result = migration.preflight(self._context(root))
            self.assertEqual(result["status"], "refused")
            self.assertEqual(result["refusals"][0]["code"], "unsafe-tree")
            with open(sentinel, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "outside sentinel\n")

        tmp2 = self._repo([])
        self.addCleanup(tmp2.cleanup)
        with self._empty_plans(), mock.patch.object(migration, "_same_entry", return_value=False):
            raced = migration.preflight(self._context(tmp2.name))
        self.assertEqual(raced["status"], "refused")
        self.assertEqual(raced["refusals"][0]["code"], "entry-raced")

        tmp3 = self._repo([])
        self.addCleanup(tmp3.cleanup)
        with self._empty_plans(), mock.patch.object(migration, "_same_entry", side_effect=[True, False]):
            override_raced = migration.preflight(self._context(tmp3.name))
        self.assertEqual(override_raced["status"], "refused")
        self.assertEqual(override_raced["refusals"][0]["code"], "override-raced")

    def test_apply_time_quarantine_and_override_collisions_preserve_foreign_bytes(self):
        tmp = self._repo(["eADR-0001.md"])
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        plan = self._preflight(root)
        real = migration._rename_noreplace
        inserted = {"record": None}

        def collide_record(src, dst, *, src_dir_fd, dst_dir_fd):
            if inserted["record"] is None and dst.startswith(migration._Q_PREFIX):
                fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=dst_dir_fd)
                try:
                    os.write(fd, b"foreign record collision\n")
                finally:
                    os.close(fd)
                inserted["record"] = dst
            return real(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with self._empty_plans(), mock.patch.object(
                migration, "_rename_noreplace", side_effect=collide_record):
            with self.assertRaisesRegex(RuntimeError, "quarantine appeared"):
                migration.apply(self._context(root), self._sealed(plan))
        collision = os.path.join(root, ".engine", "contracts", "instance", inserted["record"])
        with open(collision, "rb") as handle:
            self.assertEqual(handle.read(), b"foreign record collision\n")

        tmp2 = self._repo([])
        self.addCleanup(tmp2.cleanup)
        root2 = tmp2.name
        plan2 = self._preflight(root2)

        def collide_override(src, dst, *, src_dir_fd, dst_dir_fd):
            if src == migration._OVERRIDE_NEXT and dst == "operator-overrides.json":
                fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=dst_dir_fd)
                try:
                    os.write(fd, b'{"foreign": true}\n')
                finally:
                    os.close(fd)
            return real(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with self._empty_plans(), mock.patch.object(
                migration, "_rename_noreplace", side_effect=collide_override):
            with self.assertRaisesRegex(RuntimeError, "all colliding bytes were preserved"):
                migration.apply(self._context(root2), self._sealed(plan2))
        with open(os.path.join(root2, ".engine", "operator-overrides.json"), "rb") as handle:
            self.assertEqual(handle.read(), b'{"foreign": true}\n')
        self.assertTrue(os.path.isfile(os.path.join(root2, ".engine", migration._OVERRIDE_Q)))
        self.assertTrue(os.path.isfile(os.path.join(root2, ".engine", migration._OVERRIDE_NEXT)))

    def test_apply_time_engine_swap_refuses_before_mutating_the_replacement(self):
        tmp = self._repo([])
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        plan = self._preflight(root)
        real = migration._verify_bindings
        swapped = {"done": False}
        replacement = os.path.join(root, ".engine", "outside-sentinel.txt")

        def swap_then_verify(root_links, root_fd, engine_fd, contracts_fd=None, instance_fd=None):
            if instance_fd is not None and not swapped["done"]:
                os.rename(os.path.join(root, ".engine"), os.path.join(root, ".engine.moved"))
                os.makedirs(os.path.join(root, ".engine"))
                with open(replacement, "w", encoding="utf-8") as handle:
                    handle.write("outside replacement bytes\n")
                swapped["done"] = True
            return real(root_links, root_fd, engine_fd, contracts_fd, instance_fd)

        with self._empty_plans(), mock.patch.object(
                migration, "_verify_bindings", side_effect=swap_then_verify):
            with self.assertRaisesRegex(RuntimeError, "repository root or .engine"):
                migration.apply(self._context(root), self._sealed(plan))
        with open(replacement, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "outside replacement bytes\n")
        moved_readme = os.path.join(root, ".engine.moved", "contracts", "instance", "README.md")
        self.assertTrue(os.path.isfile(moved_readme))

    def test_post_mutation_checkpoint_refuses_an_engine_swap_without_touching_replacement(self):
        tmp = self._repo(["eADR-0001.md"])
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        plan = self._preflight(root)
        footprint = sorted({p for target in plan["targets"] for p in target["recovery_scope"]})
        started = checkout_health.begin_upgrade_transaction(
            root, sealed_targets=plan["targets"], footprint=footprint)
        self.assertTrue(started["ok"], started)
        self.assertTrue(checkout_health.update_upgrade_transaction(root, "mutating")["ok"])
        sentinel = os.path.join(root, ".engine", "replacement-sentinel.txt")
        swapped = {"done": False}

        def checkpoint_after_swap():
            if not swapped["done"]:
                os.rename(os.path.join(root, ".engine"), os.path.join(root, ".engine.moved"))
                os.makedirs(os.path.join(root, ".engine"))
                with open(sentinel, "w", encoding="utf-8") as handle:
                    handle.write("replacement bytes\n")
                swapped["done"] = True
            return checkout_health.checkpoint_upgrade_transaction(root)

        with self._empty_plans():
            with self.assertRaisesRegex(RuntimeError, "outside the sealed rollback footprint"):
                migration.apply(self._context(root, checkpoint_after_swap), self._sealed(plan))
        with open(sentinel, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "replacement bytes\n")
        self.assertTrue(os.path.isdir(os.path.join(root, ".engine.moved")))

    def test_kill_and_restart_recovery_matrix_restores_exact_preupgrade_tree(self):
        helper_dir = os.path.dirname(checkout_health.__file__)
        child = (
            "import importlib.util,json,os,sys\n"
            "root,mpath,plan_path,kill_at,helper=sys.argv[1:]\n"
            "sys.path.insert(0,helper)\n"
            "import checkout_health\n"
            "spec=importlib.util.spec_from_file_location('retire_child',mpath)\n"
            "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "with open(plan_path) as h: plan=json.load(h)\n"
            "os.environ['ENGINE_RETIREMENT_KILL_AT']=kill_at\n"
            "m.apply({'root':root,'kind':'tracked-content','module_id':'core','from_version':'0.6.3',"
            "'to_version':'0.7.0','engine_version':'0.7.0',"
            "'checkpoint':lambda: checkout_health.checkpoint_upgrade_transaction(root)},plan)\n")
        boundaries = ("record-capture", "record-verified", "record-delete", "instance-delete",
                      "contracts-delete",
                      "override-capture", "override-rewrite", "override-replace", "override-delete")
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                tmp = self._repo(["eADR-0001.md"])
                root = tmp.name
                plan = self._preflight(root)
                footprint = sorted({p for t in plan["targets"] for p in t["recovery_scope"]})
                tx = checkout_health.begin_upgrade_transaction(
                    root, sealed_targets=plan["targets"], footprint=footprint)
                self.assertTrue(tx["ok"], tx)
                self.assertTrue(checkout_health.update_upgrade_transaction(root, "mutating")["ok"])
                with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
                    json.dump(self._sealed(plan), handle)
                    plan_path = handle.name
                self.addCleanup(lambda p=plan_path: os.path.exists(p) and os.unlink(p))
                env = dict(os.environ)
                with tempfile.TemporaryDirectory() as plans:
                    env["ENGINE_PLAN_DIR"] = os.path.join(plans, "absent")
                    killed = subprocess.run(
                        [sys.executable, "-c", child, root, MIGRATION_PATH, plan_path, boundary, helper_dir],
                        env=env, capture_output=True, text=True, check=False)
                self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stderr)
                restored = checkout_health.recover_upgrade_transaction(root)
                self.assertTrue(restored["ok"], restored)
                self.assertEqual(restored["state"], "restored")
                self.assertEqual(self._git(root, "status", "--porcelain"), "")
                self.assertTrue(os.path.isfile(os.path.join(
                    root, ".engine", "contracts", "instance", "eADR-0001.md")))
                with open(os.path.join(root, ".engine", "operator-overrides.json"), encoding="utf-8") as h:
                    self.assertIn("contract-threshold", json.load(h))
                self.assertEqual(checkout_health.inspect_upgrade_transaction(root)["state"], "none")
                tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
