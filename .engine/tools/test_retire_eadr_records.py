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
    def _context(root):
        return {"root": root, "kind": "tracked-content", "module_id": "core",
                "from_version": "0.6.3", "to_version": "0.7.0", "engine_version": "0.7.0"}

    def _preflight(self, root):
        with self._empty_plans():
            result = migration.preflight(self._context(root))
        self.assertEqual(result.get("status"), "ready", result)
        return result

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
            receipt = migration.apply(self._context(root), plan)
        self.assertEqual(receipt["status"], "applied")
        self.assertEqual({c["path"] for c in receipt["changes"]}, expected)
        self.assertFalse(os.path.exists(os.path.join(root, ".engine", "contracts", "instance")))
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
            receipt = migration.apply(self._context(root), plan)
        self.assertEqual(receipt["status"], "applied")
        self.assertFalse(os.path.exists(os.path.join(root, ".engine", "contracts", "instance")))
        self.assertEqual([p.name for p in Path(root, ".engine").rglob("*")
                          if "upgrade-retirement" in p.name], [])

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
                                 {"description": "Current head uses no retired surface"}),
            "closed": ({"plan_id": "pln_closed", "status": "complete",
                        "current": {"snapshot": "revisions/000003.json"}},
                       {"description": "Completed head may still say contract.v1"}),
        }
        records = {k: v[0] for k, v in rows.items()}
        heads = {k: v[1] for k, v in rows.items()}
        with mock.patch.object(plan_store, "PlanLibrary", return_value=Library(records, heads)), \
                mock.patch.object(plan_store, "derived_status", side_effect=lambda r: r["status"]):
            refusals = migration._plan_refusals("unused")
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["code"], "actionable-plan-incompatible")
        self.assertIn("pln_current", refusals[0]["reason"])
        self.assertIn("revisions/000002.json", refusals[0]["path"])

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

    def test_kill_and_restart_recovery_matrix_restores_exact_preupgrade_tree(self):
        helper_dir = os.path.dirname(checkout_health.__file__)
        child = (
            "import importlib.util,json,os,sys\n"
            "root,mpath,plan_path,kill_at,helper=sys.argv[1:]\n"
            "sys.path.insert(0,helper)\n"
            "spec=importlib.util.spec_from_file_location('retire_child',mpath)\n"
            "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "with open(plan_path) as h: plan=json.load(h)\n"
            "os.environ['ENGINE_RETIREMENT_KILL_AT']=kill_at\n"
            "m.apply({'root':root,'kind':'tracked-content','module_id':'core','from_version':'0.6.3',"
            "'to_version':'0.7.0','engine_version':'0.7.0'},plan)\n")
        boundaries = ("record-capture", "record-verified", "record-delete", "instance-delete",
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
                    json.dump(plan, handle)
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
