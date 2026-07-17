#!/usr/bin/env python3
"""Regression tests for the migration rollback-presence inspector
(.engine/tools/migration_discipline/rollback.py)."""
from __future__ import annotations
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on sys.path
from migration_discipline import rollback  # noqa: E402
import quiet_call  # noqa: E402  (capture a demo walkthrough's stdout so it can't bury the suite summary)
import validate  # noqa: E402


class RollbackTests(unittest.TestCase):
    def _root(self, files: dict) -> str:
        """A throwaway root seeded with {relpath: body}."""
        d = tempfile.mkdtemp(prefix="engine-rollback-test-")
        self.addCleanup(shutil.rmtree, d, True)
        for rel, body in files.items():
            path = os.path.join(d, rel)
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    def _severities(self, fs) -> set:
        return {f["severity"] for f in fs}

    def _snapshot(self, root) -> dict:
        out = {}
        for cur, _dirs, names in os.walk(root):
            for n in names:
                p = os.path.join(cur, n)
                out[os.path.relpath(p, root)] = os.path.getsize(p)
        return out

    # --- the live nudge: separate up/down rollback convention ------------------------------------
    def test_up_without_down_yields_one_soft_nudge_naming_the_file(self):
        fs = rollback.findings("soft", root=self._root({"migrations/0001_init.up.sql": "x"}))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertEqual(fs[0]["location"], {"file": "migrations/0001_init.up.sql", "line": None})
        self.assertIn("no matching rollback script", fs[0]["message"])

    def test_paired_up_down_passes_cleanly(self):
        fs = rollback.findings("soft", root=self._root({
            "migrations/0001_init.up.sql": "x", "migrations/0001_init.down.sql": "y"}))
        self.assertEqual(fs, [])

    def test_pairing_is_case_insensitive(self):
        fs = rollback.findings("soft", root=self._root({
            "db/migrations/0001.UP.SQL": "x", "db/migrations/0001.DOWN.SQL": "y"}))
        self.assertEqual(fs, [])

    def test_multiple_ups_report_only_the_unpaired_ones(self):
        fs = rollback.findings("soft", root=self._root({
            "migrations/0001.up.sql": "x", "migrations/0001.down.sql": "y",
            "migrations/0002.up.sql": "x"}))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["location"]["file"], "migrations/0002.up.sql")

    # --- presence-first: a forward-only tool with hand-written rollbacks is checked, not waved off ---
    def test_presence_first_supabase_with_updown_is_checked(self):
        fs = rollback.findings("soft", root=self._root({
            "supabase/config.toml": "x", "supabase/migrations/0001.up.sql": "x"}))
        self.assertEqual(len(fs), 1)
        self.assertIn("no matching rollback script", fs[0]["message"])

    # --- Flyway: paired PER VERSION so one undo can't mask a later unpaired version (no silent pass) ---
    def test_flyway_with_undo_for_every_version_passes_cleanly(self):
        fs = rollback.findings("soft", root=self._root({
            "db/migration/V1__a.sql": "x", "db/migration/U1__a.sql": "x"}))
        self.assertEqual(fs, [])

    def test_flyway_partial_undo_nudges_the_unpaired_version(self):
        fs = rollback.findings("soft", root=self._root({
            "db/migration/V1__a.sql": "x", "db/migration/U1__a.sql": "x",
            "db/migration/V2__b.sql": "x"}))
        self.assertEqual(len(fs), 1, "an unpaired version must be nudged, not silently passed")
        self.assertEqual(fs[0]["location"]["file"], "db/migration/V2__b.sql")
        self.assertIn("no matching undo script", fs[0]["message"])

    def test_flyway_versioned_without_any_undo_discloses_forward_only(self):
        fs = rollback.findings("soft", root=self._root({"db/migration/V1__init.sql": "x"}))
        self.assertEqual(len(fs), 1)
        self.assertIn("Flyway", fs[0]["message"])
        self.assertIn("forward-only", fs[0]["message"])

    def test_flyway_prefix_requires_a_digit_version(self):
        # `Validate__x.sql` starts with V but is not a Flyway versioned migration (no digit version).
        fs = rollback.findings("soft", root=self._root({"sql/Validate__x.sql": "x"}))
        self.assertEqual(len(fs), 1)
        self.assertIn("isn't active here yet", fs[0]["message"])

    # --- forward-only frameworks: said plainly, never silent --------------------------------------
    def test_supabase_discloses_forward_only(self):
        fs = rollback.findings("soft", root=self._root({"supabase/config.toml": "x"}))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIsNone(fs[0]["location"])
        self.assertIn("Supabase", fs[0]["message"])
        self.assertIn("forward-only", fs[0]["message"])
        self.assertIn("soft note only", fs[0]["message"])

    def test_prisma_discloses_forward_only(self):
        fs = rollback.findings("soft", root=self._root({"prisma/schema.prisma": "x"}))
        self.assertEqual(len(fs), 1)
        self.assertIn("Prisma", fs[0]["message"])
        self.assertIn("forward-only", fs[0]["message"])

    def test_liquibase_changelog_discloses_forward_only(self):
        for changelog in ("db.changelog-master.xml", "changelog.yaml", "db/changelog-1.0.sql"):
            fs = rollback.findings("soft", root=self._root({changelog: "x"}))
            self.assertEqual(len(fs), 1, f"{changelog} should be detected as Liquibase")
            self.assertIn("Liquibase", fs[0]["message"])

    def test_liquibase_detection_does_not_overmatch_a_plain_changelog_filename(self):
        # `db_changelog.sql` is a plausible hand-written file, NOT a Liquibase changelog.
        fs = rollback.findings("soft", root=self._root({"db_changelog.sql": "x"}))
        self.assertEqual(len(fs), 1)
        self.assertIn("isn't active here yet", fs[0]["message"])

    # --- in-file / framework-managed rollback: said plainly ---------------------------------------
    def test_rails_discloses_in_file(self):
        fs = rollback.findings("soft", root=self._root({"db/migrate/0001_create_users.rb": "x"}))
        self.assertEqual(len(fs), 1)
        self.assertIn("Rails", fs[0]["message"])
        self.assertIn("inside the migration file", fs[0]["message"])

    def test_django_discloses_in_file(self):
        fs = rollback.findings("soft", root=self._root({
            "manage.py": "x", "app/migrations/__init__.py": "",
            "app/migrations/0001_initial.py": "x"}))
        self.assertEqual(len(fs), 1)
        self.assertIn("Django", fs[0]["message"])

    def test_alembic_discloses_in_file(self):
        fs = rollback.findings("soft", root=self._root({"alembic.ini": "[alembic]"}))
        self.assertEqual(len(fs), 1)
        self.assertIn("Alembic", fs[0]["message"])

    # --- a migrations dir that matched no known tool and has no separate rollback files -----------
    def test_unclassified_migrations_dir_discloses_generic(self):
        fs = rollback.findings("soft", root=self._root({"migrations/0001_init.sql": "x"}))
        self.assertEqual(len(fs), 1)
        self.assertIn("couldn't match it to a migration tool", fs[0]["message"])

    # --- the no-op (no migrations at all) --------------------------------------------------------
    def test_empty_project_discloses_the_no_op(self):
        fs = rollback.findings("soft", root=self._root({"README.md": "hi"}))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIsNone(fs[0]["location"])
        self.assertIn("isn't active here yet", fs[0]["message"])

    # --- engine/product wall / prune: a migration under a walled or vendored dir is never the product's own ---
    def test_engine_walled_migration_is_not_a_product_migration(self):
        fs = rollback.findings("soft", root=self._root({".engine/x/migrations/0001.up.sql": "x"}))
        self.assertEqual(len(fs), 1, "a migration under .engine/ must not count as a product migration")
        self.assertIn("isn't active here yet", fs[0]["message"])

    def test_vendored_migration_is_pruned(self):
        for pruned in ("node_modules/dep", ".venv/lib", "dist", "build", "vendor/pkg"):
            fs = rollback.findings("soft", root=self._root({f"{pruned}/migrations/0001.up.sql": "x"}))
            self.assertEqual(len(fs), 1, f"a migration under {pruned}/ must be pruned")
            self.assertIn("isn't active here yet", fs[0]["message"], f"{pruned} should yield the no-op")

    # --- the tier guarantee: never hard ----------------------------------------------------------
    def test_findings_are_never_hard(self):
        for files in ({}, {"migrations/0001.up.sql": "x"}, {"supabase/config.toml": "x"},
                      {"db/migration/V1__a.sql": "x"}, {"db/migrate/0001.rb": "x"},
                      {".engine/x/migrations/0001.up.sql": "x"}):
            fs = rollback.findings("soft", root=self._root(files))
            self.assertNotIn("hard", self._severities(fs))

    # --- read-only: a run never changes the tree -------------------------------------------------
    def test_inspection_is_read_only(self):
        root = self._root({"migrations/0001.up.sql": "x"})
        before = self._snapshot(root)
        rollback.findings("soft", root=root)
        self.assertEqual(self._snapshot(root), before)

    # --- the real repo can never turn engine-ci red from this check ------------------------------
    def test_real_repo_yields_no_hard_finding(self):
        fs = rollback.findings("soft")  # defaults to validate.ROOT (engine-template itself)
        self.assertNotIn("hard", self._severities(fs))

    # --- the falsifiable demo passes on the happy path -------------------------------------------
    def test_demo_passes(self):
        self.assertEqual(quiet_call.run(rollback.demo), 0)

    # --- the no-arg dispatch emits a JSON array (the custom/script contract) ----------------------
    def test_emit_findings_prints_a_json_array_and_returns_zero(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = rollback.emit_findings()
        self.assertEqual(rc, 0)
        parsed = json.loads(buf.getvalue())
        self.assertIsInstance(parsed, list)
        for f in parsed:
            self.assertIn("severity", f)
            self.assertIn("message", f)
            self.assertIn("location", f)

    def test_main_routes_demo_and_bare_invocation(self):
        self.assertEqual(quiet_call.run(rollback.main, ["demo"]), 0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(rollback.main([]), 0)
        self.assertIsInstance(json.loads(buf.getvalue()), list)


if __name__ == "__main__":
    unittest.main()
