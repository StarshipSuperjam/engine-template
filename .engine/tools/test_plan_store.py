#!/usr/bin/env python3
"""Tests for plan_store — the durable local plan library.

Three groups, matching the three ways this store can fail an operator badly.

WHERE: a library resolved to the wrong root is invisible to the Build that needs it, and the failure
looks like success. The topology tests build real git repositories and real linked worktrees rather
than mocking the resolver, because the whole hazard is that a naive resolver LOOKS right.

WHAT SURVIVES: a plan that comes back altered, truncated, or silently missing is worse than one that
refuses to come back at all. Every read of a head re-derives the digest; corruption is refused, not
returned.

WHO ELSE IS WRITING: two sessions, one plan. The stale writer must be refused and must change
nothing at all — a refusal that has already written half its change is not a refusal.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import build_coordinator_core as core
import checkout_health
import plan_contract
import plan_store


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-qm", "seed")
    return path


def _payload() -> dict:
    return {
        "schema_version": "build-plan.v2",
        "profile": "normal",
        "intent_source": {"kind": "direct"},
        "raw_intent": "store a plan durably",
        "interpretation": "A minimal payload; the store does not judge payloads.",
        "objective": "Exercise the store.",
        "success_obligations": [{"outcome": "It stores.", "verification": "These tests."}],
        "evidence": [{"claim": "The store writes atomically.", "basis": "core.atomic_write", "kind": "observed"}],
        "assumptions": [],
        "scope_boundary": ["one node"],
        "non_goals": ["everything else"],
        "risks": ["none worth listing in a fixture"],
        "review_strategy": "These tests.",
        "spec": {"posture": "none", "selection_basis": "No specification governs a test fixture.",
                 "disclosure": "This payload exists only to give the store something valid to hold."},
        "parallelism": {"mode": "serial", "max_concurrency": 1},
        "work_items": [{
            "id": "only", "description": "The only node.", "paths": ["a.py"],
            "depends_on": [], "exclusive_resources": [], "executor_class": "integrator",
            "verification": ["it runs"],
            "output_contract": {"deliverable": "a.py", "artifact_kinds": ["code"],
                                "required_evidence": ["a green test"]}}],
    }


def _document(revision=1, plan_id="pln_0123456789ab", title="A stored plan", **over) -> dict:
    doc = {
        "schema_version": "engine-plan.v1",
        "plan_id": plan_id,
        "title": title,
        "revision": revision,
        "created_at": "2026-08-23T00:00:00Z",
        "revised_at": "2026-08-23T00:00:0%dZ" % min(revision, 9),
        "revision_note": f"Revision {revision}.",
        "intent": {"raw": "store it", "interpretation": "Durably.", "source": {"kind": "direct"}},
        "deliberation": {
            "problem_frame": "Planning state vanished across a reboot.",
            "case_against": "A durable local store is one more thing that can rot.",
            "alternatives": [{"option": "Keep it in OS temp", "disposition": "rejected",
                              "reason": "That is the observed failure."}],
            "failure_modes": ["The library resolves to the wrong root."],
            "unresolved_decisions": [],
        },
        "build_plan": _payload(),
    }
    doc.update(over)
    return doc


class _Library(unittest.TestCase):
    """A library in a throwaway directory, reached through the ENV override."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "plans"
        self.lib = plan_store.PlanLibrary(self.root)
        self.addCleanup(self._tmp.cleanup)

    def _create(self, **over):
        doc = _document(**over)
        return self.lib.create(doc), doc


class Topology(unittest.TestCase):
    """WHERE the library lives — proven against real git layouts, not a mocked resolver."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop(plan_store.ENV_DIR, None)
        self.addCleanup(self._env.stop)

    def test_main_checkout_and_its_linked_worktree_resolve_to_one_library(self):
        # The hazard this locks out: a session running in a per-Build worktree writing its plans into
        # that worktree, where they vanish when the worktree is torn down.
        clone = _repo(self.tmp / "clone")
        linked = self.tmp / "linked"
        _git(clone, "worktree", "add", "-q", "-b", "side", str(linked))
        self.assertTrue(linked.is_dir(), "git worktree add did not produce a checkout")

        with mock.patch.object(checkout_health, "resolve_product_checkout", return_value=(None, None)):
            from_main = plan_store.library_root(str(clone))
            from_worktree = plan_store.library_root(str(linked))
        self.assertEqual(from_main, from_worktree)
        self.assertEqual(from_main, clone.resolve() / ".engine" / "plans")

    def test_an_owned_product_puts_plans_in_the_products_own_checkout(self):
        # The cross-repo case: planning runs in the engine's checkout, the Build runs in a worktree of
        # a DIFFERENT repository. Resolve by common dir alone and the plan lands where no Build looks.
        engine = _repo(self.tmp / "engine")
        product = _repo(self.tmp / "product")
        with mock.patch.object(checkout_health, "resolve_product_checkout",
                               return_value=(str(product), None)):
            resolved = plan_store.library_root(str(engine))
        self.assertEqual(resolved, product.resolve() / ".engine" / "plans")

    def test_an_ambiguous_root_is_refused_not_guessed(self):
        # A recorded product target whose local path is unset. Falling back to the engine root here
        # would create a second library that looks fine and is invisible to every Build.
        with mock.patch.object(checkout_health, "resolve_product_checkout",
                               return_value=(None, "path-unset")):
            with self.assertRaises(plan_store.PlanStoreError) as caught:
                plan_store.library_root(str(self.tmp))
        self.assertIn("no unambiguous home", str(caught.exception))

    def test_an_unresolvable_engine_checkout_is_refused_not_guessed(self):
        with mock.patch.object(checkout_health, "resolve_product_checkout", return_value=(None, None)), \
             mock.patch.object(checkout_health, "engine_common_checkout", return_value=None):
            with self.assertRaisesRegex(plan_store.PlanStoreError, "worktree-local library"):
                plan_store.library_root(str(self.tmp))

    def test_the_env_override_wins_and_expands_home(self):
        os.environ[plan_store.ENV_DIR] = str(self.tmp / "elsewhere")
        self.assertEqual(plan_store.library_root(), (self.tmp / "elsewhere").resolve())

    def test_a_dirty_product_checkout_does_not_make_plans_unreadable(self):
        # resolve_build_target refuses on a dirty tree, which is right for entering a Build and wrong
        # for reading a plan. This store must never acquire that leg.
        product = _repo(self.tmp / "product")
        (product / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
        (product / "seed.txt").write_text("modified\n", encoding="utf-8")
        self.assertTrue(_git(product, "status", "--short").stdout.strip(), "the fixture is not dirty")

        with mock.patch.object(checkout_health, "resolve_product_checkout",
                               return_value=(str(product), None)):
            root = plan_store.library_root(str(product))
            lib = plan_store.PlanLibrary(root)
            slug = lib.create(_document())
            self.assertEqual(lib.head(slug)["revision"], 1)

    def test_the_store_does_not_depend_on_the_killswitch_tier_build_entry(self):
        # Read the parsed module rather than its text: the docstring EXPLAINS both exclusions by name,
        # so a substring scan would flag the explanation and, worse, could be silenced by deleting it.
        import ast
        tree = ast.parse(Path(plan_store.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("mechanic_build", imported)
        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        called |= {node.func.id for node in ast.walk(tree)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertNotIn("resolve_build_target", called)
        # The two resolvers it DOES ride, so the exclusion above cannot be met by resolving nothing.
        self.assertIn("resolve_product_checkout", called)
        self.assertIn("engine_common_checkout", called)


class CreateAndRead(_Library):
    def test_a_created_plan_reads_back_identically(self):
        slug, doc = self._create()
        self.assertEqual(self.lib.head(slug), doc)
        record = self.lib.read_record(slug)
        self.assertEqual(record["current"]["revision"], 1)
        self.assertEqual(record["current"]["plan_digest"], core.digest(doc))
        self.assertEqual(record["current"]["build_plan_digest"], core.digest(doc["build_plan"]))

    def test_the_slug_carries_the_id_so_two_plans_named_alike_do_not_collide(self):
        first, _ = self._create(plan_id="pln_aaaaaaaaaaaa", title="Same name")
        second, _ = self._create(plan_id="pln_bbbbbbbbbbbb", title="Same name")
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.lib.slugs()), 2)

    def test_a_second_revision_does_not_move_the_folder_when_the_title_changes(self):
        slug, _ = self._create()
        self.lib.append_revision(slug, _document(revision=2, title="A renamed plan"), expected_revision=1)
        self.assertEqual(self.lib.slugs(), [slug])
        self.assertEqual(self.lib.read_record(slug)["title"], "A renamed plan")

    def test_creating_the_same_plan_twice_is_refused(self):
        slug, doc = self._create()
        with self.assertRaisesRegex(plan_store.PlanStoreError, "already exists"):
            self.lib.create(doc)

    def test_a_plan_must_start_at_revision_one(self):
        with self.assertRaisesRegex(plan_store.PlanStoreError, "starts at revision 1"):
            self.lib.create(_document(revision=2))

    def test_an_invalid_document_never_reaches_disk(self):
        bad = _document()
        del bad["deliberation"]
        with self.assertRaises(plan_store.PlanStoreError):
            self.lib.create(bad)
        self.assertEqual(self.lib.slugs(), [])


class Selection(_Library):
    def test_nothing_auto_selects_not_even_the_only_plan(self):
        self._create()
        with self.assertRaisesRegex(plan_store.PlanStoreError, "nothing is selected by default"):
            self.lib.resolve("")

    def test_selection_by_slug_full_id_and_unique_prefix(self):
        slug, doc = self._create()
        self.assertEqual(self.lib.resolve(slug), slug)
        self.assertEqual(self.lib.resolve(doc["plan_id"]), slug)
        self.assertEqual(self.lib.resolve(doc["plan_id"][:9]), slug)

    def test_an_ambiguous_prefix_fails_and_names_its_candidates(self):
        self._create(plan_id="pln_abc111111111", title="First")
        self._create(plan_id="pln_abc222222222", title="Second")
        with self.assertRaises(plan_store.PlanStoreError) as caught:
            self.lib.resolve("pln_abc")
        message = str(caught.exception)
        self.assertIn("matches 2 plans", message)
        self.assertIn("pln_abc111111111", message)
        self.assertIn("pln_abc222222222", message)

    def test_an_unknown_selector_lists_what_is_there(self):
        slug, _ = self._create()
        with self.assertRaises(plan_store.PlanStoreError) as caught:
            self.lib.resolve("pln_ffffffffffff")
        self.assertIn(slug, str(caught.exception))

    def test_a_half_created_folder_is_not_a_plan(self):
        # Exactly the state a crashed seeding run left behind: subdirectories, no record.
        (self.root / "orphan--abc123" / "revisions").mkdir(parents=True)
        self._create()
        self.assertNotIn("orphan--abc123", self.lib.slugs())


class ConcurrentWriters(_Library):
    def test_a_stale_writer_is_refused(self):
        slug, _ = self._create()
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        with self.assertRaisesRegex(plan_store.PlanStoreError, "another session revised this plan"):
            self.lib.append_revision(slug, _document(revision=2), expected_revision=1)

    def test_a_refused_writer_changes_no_file(self):
        # A refusal that has already written half its change is not a refusal.
        slug, _ = self._create()
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        before = {p.name: p.read_bytes() for p in sorted((self.root / slug).rglob("*")) if p.is_file()}
        with self.assertRaises(plan_store.PlanStoreError):
            self.lib.append_revision(slug, _document(revision=2, title="Clobbered"), expected_revision=1)
        after = {p.name: p.read_bytes() for p in sorted((self.root / slug).rglob("*")) if p.is_file()}
        self.assertEqual(before, after)

    def test_a_revision_number_out_of_step_is_refused(self):
        slug, _ = self._create()
        with self.assertRaisesRegex(plan_store.PlanStoreError, "next revision here is 2"):
            self.lib.append_revision(slug, _document(revision=5), expected_revision=1)

    def test_a_revision_from_a_different_plan_is_refused(self):
        slug, _ = self._create()
        with self.assertRaisesRegex(plan_store.PlanStoreError, "belongs to plan"):
            self.lib.append_revision(slug, _document(revision=2, plan_id="pln_ffffffffffff"),
                                     expected_revision=1)

    def _approve(self, slug, document):
        self.lib.update_record(slug, lambda r: r.update(
            {"approval": {"revision": document["revision"], "plan_digest": core.digest(document),
                          "depth": "standard", "at": "2026-08-23T01:00:00Z"}}))

    def _review(self, slug, document):
        self.lib.update_record(slug, lambda r: r.update(
            {"plan_review": {"revision": document["revision"], "plan_digest": core.digest(document),
                             "packet_digest": core.digest(document), "at": "2026-08-23T02:00:00Z",
                             "lenses": ["architecture"]}}))

    def test_revising_before_review_makes_the_approval_stale_without_erasing_it(self):
        # The approval is NOT cleared. It records what was approved and when, which is what an
        # operator needs in order to judge whether re-approving is warranted; staleness is derived so
        # the evidence survives.
        slug, document = self._create()
        self._approve(slug, document)
        self.assertEqual(plan_store.derived_status(self.lib.read_record(slug)), "awaiting-review")

        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        record = self.lib.read_record(slug)
        self.assertIsNotNone(record["approval"], "the approval evidence was destroyed")
        self.assertEqual(record["approval"]["revision"], 1)
        self.assertTrue(plan_store.approval_is_stale(record))
        self.assertEqual(plan_store.derived_status(record), "draft")

    def test_revising_after_review_keeps_the_approval_live(self):
        # This IS the agreed cadence: approve, review once, fold fixes in as revisions, judge the
        # delta, seal. Clearing the approval on revision would make that sequence impossible.
        slug, document = self._create()
        self._approve(slug, document)
        self._review(slug, document)
        for revision in (2, 3):
            self.lib.append_revision(slug, _document(revision=revision), expected_revision=revision - 1)
        record = self.lib.read_record(slug)
        self.assertFalse(plan_store.approval_is_stale(record))
        self.assertEqual(record["approval"]["revision"], 1)
        self.assertEqual(plan_store.derived_status(record), "review-recorded")

    def test_update_record_enforces_the_same_compare_and_swap(self):
        slug, _ = self._create()
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        with self.assertRaises(plan_store.PlanStoreError):
            self.lib.update_record(slug, lambda r: r.update({"closure": None}), expected_revision=1)


class Integrity(_Library):
    def test_a_corrupt_head_is_detected_and_recovered_to_the_intact_ancestor(self):
        slug, _ = self._create()
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        head_path = self.root / slug / self.lib.read_record(slug)["current"]["snapshot"]
        document = json.loads(head_path.read_text(encoding="utf-8"))
        document["title"] = "Tampered"
        head_path.write_text(json.dumps(document), encoding="utf-8")

        with self.assertRaisesRegex(plan_store.PlanStoreError, "does not match its recorded digest"):
            self.lib.head(slug)
        revision, skipped = self.lib.recover_head(slug)
        self.assertEqual(revision, 1)
        self.assertEqual(len(skipped), 1)
        # Recovery does not repair: the record still points at the damaged head, because that is a
        # fact the operator needs, and rewriting it would destroy the evidence.
        self.assertEqual(self.lib.read_record(slug)["current"]["revision"], 2)

    def test_a_truncated_write_cannot_reach_the_head_at_all(self):
        # atomic_write renames a fully-written temp file into place, so a write interrupted before the
        # rename leaves the previous head untouched rather than a half-written document.
        slug, doc = self._create()
        real_replace = os.replace

        def fail_before_replace(src, dst, *a, **k):
            if str(dst).endswith(".json"):
                raise OSError("crash between write and rename")
            return real_replace(src, dst, *a, **k)

        with mock.patch("os.replace", side_effect=fail_before_replace):
            with self.assertRaises(OSError):
                self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        self.assertEqual(self.lib.head(slug), doc)
        self.assertEqual(self.lib.verify_chain(slug), [])
        self.assertFalse(list((self.root / slug / "revisions").glob("*.json.*")),
                         "a temp file was left behind")

    def test_a_deleted_ancestor_is_named_as_loss_not_intent(self):
        slug, _ = self._create()
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        (self.root / slug / self.lib.read_record(slug)["ledger"][0]["snapshot"]).unlink()
        problems = self.lib.verify_chain(slug)
        self.assertEqual(len(problems), 1)
        self.assertIn("loss rather than intent", problems[0])
        # The head is unaffected: the damage is at revision 1, and that is what the message says.
        self.assertEqual(self.lib.head(slug)["revision"], 2)

    def test_a_sound_chain_reports_no_problems(self):
        slug, _ = self._create()
        for revision in (2, 3):
            self.lib.append_revision(slug, _document(revision=revision), expected_revision=revision - 1)
        self.assertEqual(self.lib.verify_chain(slug), [])

    def test_nothing_intact_is_said_plainly_rather_than_crashing(self):
        slug, _ = self._create()
        (self.root / slug / self.lib.read_record(slug)["current"]["snapshot"]).unlink()
        with self.assertRaisesRegex(plan_store.PlanStoreError, "no intact revision"):
            self.lib.recover_head(slug)


class Redaction(_Library):
    def _two_revisions(self):
        slug, _ = self._create()
        self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        return slug

    def test_redaction_excises_the_body_and_leaves_the_chain_verifiable(self):
        slug = self._two_revisions()
        snapshot = self.lib.read_record(slug)["ledger"][0]["snapshot"]
        record = self.lib.redact_revision(slug, 1, reason="raw intent held a credential")
        self.assertFalse((self.root / slug / snapshot).exists())
        entry = record["ledger"][0]
        self.assertEqual(entry["redacted"]["reason"], "raw intent held a credential")
        # The entry, its digest and its place in the chain all survive: the redaction is visible.
        self.assertEqual(entry["revision"], 1)
        self.assertTrue(entry["plan_digest"].startswith("sha256:"))
        self.assertEqual(self.lib.verify_chain(slug), [])

    def test_reading_a_redacted_revision_says_it_was_deliberate(self):
        slug = self._two_revisions()
        self.lib.redact_revision(slug, 1, reason="a name that should not have been written down")
        with self.assertRaisesRegex(plan_store.PlanStoreError, "redacted"):
            self.lib.read_revision(slug, 1)

    def test_the_head_cannot_be_redacted(self):
        slug = self._two_revisions()
        with self.assertRaisesRegex(plan_store.PlanStoreError, "cannot be redacted"):
            self.lib.redact_revision(slug, 2, reason="whatever")

    def test_a_redaction_needs_a_reason(self):
        slug = self._two_revisions()
        with self.assertRaisesRegex(plan_store.PlanStoreError, "stated reason"):
            self.lib.redact_revision(slug, 1, reason="   ")

    def test_redacting_twice_is_a_no_op_rather_than_an_error(self):
        slug = self._two_revisions()
        first = self.lib.redact_revision(slug, 1, reason="once")
        second = self.lib.redact_revision(slug, 1, reason="again")
        self.assertEqual(first["ledger"][0]["redacted"], second["ledger"][0]["redacted"])

    def test_the_head_still_reads_after_an_ancestor_is_redacted(self):
        slug = self._two_revisions()
        self.lib.redact_revision(slug, 1, reason="tidying")
        self.assertEqual(self.lib.head(slug)["revision"], 2)


class Permissions(_Library):
    def test_directories_are_0700_and_files_0600_under_a_permissive_umask(self):
        previous = os.umask(0o000)
        try:
            slug, _ = self._create()
            self.lib.append_revision(slug, _document(revision=2), expected_revision=1)
        finally:
            os.umask(previous)
        plan_dir = self.root / slug
        for directory in [plan_dir] + [p for p in plan_dir.iterdir() if p.is_dir()]:
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700, directory)
        # The LIBRARY ROOT too. This is the one that actually went wrong: mkdir(parents=True) applies
        # its mode only to the leaf, so the root was created 0755 while every plan folder inside it
        # was 0700 — the revisions unreadable but the slug names, which carry plan titles, not.
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700, self.root)
        for path in plan_dir.rglob("*"):
            if path.is_file() and path.suffix == ".json":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)

    def test_tightening_never_reaches_outside_the_library(self):
        # ensure_dir walks upward fixing modes; the walk must stop at the library root and never
        # touch a parent that belongs to the operator or the system.
        outside = self.root.parent
        outside.chmod(0o755)
        previous = os.umask(0o000)
        try:
            self._create()
        finally:
            os.umask(previous)
        self.assertEqual(outside.stat().st_mode & 0o777, 0o755,
                         "the store modified a directory above its own root")


class DurabilityWiring(_Library):
    def test_the_store_writes_through_the_platform_barrier_and_fsyncs_the_directory(self):
        # The trap this locks out: reusing an atomic-but-not-durable writer and calling the store
        # durable. On Darwin a plain os.fsync is not a barrier, so the obligation would go unmet with
        # a green suite. Assert the real calls happen.
        with mock.patch.object(core, "durable_fsync", wraps=core.durable_fsync) as barrier, \
             mock.patch.object(core, "fsync_dir", wraps=core.fsync_dir) as dir_sync:
            self._create()
        self.assertTrue(barrier.called, "the store did not use the platform durability barrier")
        self.assertTrue(dir_sync.called, "the store did not fsync the directory after the rename")

    @unittest.skipUnless(hasattr(__import__("fcntl"), "F_FULLFSYNC"), "Darwin-only barrier")
    def test_the_barrier_is_f_fullfsync_where_the_platform_has_it(self):
        self.assertIsNotNone(core._F_FULLFSYNC)


class VolumeWarnings(unittest.TestCase):
    def test_a_synced_folder_is_warned_about_not_refused(self):
        warning = plan_store.volume_warning(
            Path("/Users/someone/Library/Mobile Documents/com~apple~CloudDocs/.engine/plans"))
        self.assertIsNotNone(warning)
        self.assertIn("sync", warning.lower())

    def test_a_dropbox_folder_is_warned_about(self):
        self.assertIsNotNone(plan_store.volume_warning(Path("/Users/someone/Dropbox/repo/.engine/plans")))

    def test_an_ordinary_local_path_is_not_warned_about(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(plan_store.volume_warning(Path(tmp) / ".engine" / "plans"))

    def test_an_undeterminable_volume_reads_as_unknown_rather_than_safe(self):
        with mock.patch.object(plan_store, "_filesystem_type", return_value=None):
            with tempfile.TemporaryDirectory() as tmp:
                self.assertIsNone(plan_store.volume_warning(Path(tmp)))


class DerivedStatus(unittest.TestCase):
    def _record(self, **over):
        record = {"approval": None, "plan_review": None, "seal": None,
                  "build_binding": None, "closure": None,
                  "current": {"revision": 1, "plan_digest": "sha256:" + "a" * 64,
                              "build_plan_digest": "sha256:" + "b" * 64, "snapshot": "x.json"}}
        record.update(over)
        return record

    def _live_approval(self):
        """An approval bound to the head digest — the not-stale case."""
        return {"revision": 1, "plan_digest": "sha256:" + "a" * 64, "depth": "standard",
                "at": "2026-08-23T01:00:00Z"}

    def test_every_status_is_derived_from_evidence(self):
        self.assertEqual(plan_store.derived_status(self._record()), "draft")
        self.assertEqual(plan_store.derived_status(self._record(), head_blockers=["something open"]), "draft")
        self.assertEqual(plan_store.derived_status(self._record(), head_blockers=[]), "awaiting-approval")
        self.assertEqual(plan_store.derived_status(self._record(approval=self._live_approval())),
                         "awaiting-review")
        self.assertEqual(
            plan_store.derived_status(self._record(approval=self._live_approval(), plan_review={"x": 1})),
            "review-recorded")
        # An approval bound to a digest that is no longer the head, with no review: back to draft.
        stale = dict(self._live_approval(), plan_digest="sha256:" + "c" * 64)
        self.assertEqual(plan_store.derived_status(self._record(approval=stale)), "draft")
        self.assertEqual(plan_store.derived_status(self._record(seal={"x": 1})), "sealed")
        self.assertEqual(plan_store.derived_status(self._record(seal={}, build_binding={"x": 1})), "active")
        for state in ("complete", "retired", "abandoned"):
            self.assertEqual(
                plan_store.derived_status(self._record(seal={}, closure={"state": state})), state)

    def test_the_enumeration_is_complete_and_nothing_is_stored(self):
        self.assertEqual(len(plan_store.STATUSES), 9)
        schema = json.loads(plan_store.RECORD_SCHEMA.read_text(encoding="utf-8"))
        self.assertNotIn("status", schema["properties"])
        self.assertNotIn("phase", schema["properties"])


class SharedPrimitives(unittest.TestCase):
    def test_both_stores_ride_the_same_lock_and_compare_and_swap(self):
        # Not decoration: a second copy of either would be the thing that drifts.
        state_source = Path(core.__file__).read_text(encoding="utf-8")
        store_source = Path(plan_store.__file__).read_text(encoding="utf-8")
        self.assertIn("exclusive_lock(self.lock)", state_source)
        self.assertIn("core.exclusive_lock(", store_source)
        self.assertIn("core.assert_revision(", store_source)
        self.assertEqual(state_source.count("fcntl.flock"), 1,
                         "the flock call should exist in exactly one place")


class RealDogfoodPlan(unittest.TestCase):
    """The plan for the PR that built this store, driven through the store itself."""

    def test_the_seeded_plan_round_trips_with_its_recorded_digests(self):
        document = _seeded_plan()
        with tempfile.TemporaryDirectory() as tmp:
            lib = plan_store.PlanLibrary(Path(tmp) / "plans")
            slug = lib.create(document)
            self.assertEqual(lib.head(slug), document)
            record = lib.read_record(slug)
            self.assertEqual(record["current"]["plan_digest"], plan_contract.document_digest(document))
            self.assertEqual(record["current"]["build_plan_digest"],
                             plan_contract.build_plan_digest(document))
            self.assertEqual(lib.verify_chain(slug), [])


def _seeded_plan() -> dict:
    """A trimmed but structurally real engine-plan.v1 revision, held inline rather than in
    .engine/_fixtures/, whose README reserves that namespace for deliberately-broken negative inputs.
    The full-fidelity dogfood lives in test_plan_dogfood.py."""
    document = _document(plan_id="pln_e910c2029ffe", title="Local-First Plan Coordinator — PR A")
    document["intent"]["raw"] = (
        "The build is only as good as the plan, and the plan is only as good as the spec. "
        "We need to take on the plan issue now.")
    document["deliberation"]["problem_frame"] = (
        "The Build Coordinator becomes authoritative only at plan bind; everything upstream of that "
        "is convention, so a Build is only ever as good as a plan nothing owns.")
    document["deliberation"]["case_against"] = (
        "A second coordinator is a second thing to keep true, and the plan harness people already have "
        "works for single efforts.")
    document["build_plan"]["objective"] = (
        "Give planning a mechanical lifecycle owner with a durable, operator-browsable local record.")
    return document


if __name__ == "__main__":
    unittest.main()
