#!/usr/bin/env python3
"""Tests for build_state_store — the durable Build snapshot.

Four groups, matching the four ways durability can be got wrong.

IT SURVIVES: the whole point. Evidence written by one process is read whole by the next, found by
the worktree rather than by something the crash could have taken.

IT DOES NOT LEAK: a durable file is a file that persists, so its permissions and its containment
stop being transient details. Owner-only, all the way down, and a slug from a record can never write
outside the library.

IT DOES NOT DESTROY: this store is the one place in the engine that can overwrite live Build
evidence. Migration is proven on a copy before the real file moves; a second Build never lands on the
first silently; a refusal leaves everything exactly where it was.

IT HAS ONE HOME: the lock, the compare-and-swap and the atomic write are inherited, not restated.
That is asserted structurally, because a second copy of a compare-and-swap is the kind of drift no
behavioural test notices until it has already lost an update.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import build_coordinator_core as core
import build_state_store
import plan_store
from test_build_coordinator import plan_v2

PLAN = plan_v2()

TOOLS = Path(__file__).resolve().parent
SCHEMA = TOOLS.parents[1] / ".engine" / "schemas" / "build-state.v2.json"


def _state(pr=1, worktree="/tmp/wt", revision=1, **over) -> dict:
    state = {
        "schema_version": "build-state.v2", "revision": revision,
        "build": {"repository": "o/r", "pr": pr, "base_at_bind": "a" * 40,
                  "mode": "same-session", "worktree": worktree},
        "plan": {"plan_id": "pln_0123456789ab", "sealed_digest": "sha256:" + "b" * 64,
                 "diverged_from_seal": False, "digest": "sha256:" + "c" * 64,
                 "intent_digest": "sha256:" + "d" * 64, "spec_digest": None,
                 "authorizing_issue": None, "profile": "normal", "bound_head": "e" * 40},
        "approval": None, "reviews": {"deliverable": {
            "packet_digest": None, "referent_digest": None, "required_lenses": [],
            "installed_lenses": [], "reviewer_contracts": [], "receipts": [],
            "reviewed_commit": None, "base_commit": None}},
        "findings": [], "checkpoint": None, "progress": {"current_item": None, "completed": []},
        "work": {}, "validation": None, "repair": None, "repair_rounds": [],
        "plan_change_escalations": [], "reconciles": [], "preflights": [],
        "pr_contract": None, "submission": "draft", "checkout_snapshot": None,
    }
    state.update(over)
    return state


class _Library(unittest.TestCase):
    """A plan library holding one real plan, in a throwaway directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.lib = plan_store.PlanLibrary(self.tmp / "plans")
        self.slug = plan_store.slug_for("a stored plan", "pln_0123456789ab")
        plan_store.ensure_dir(self.lib.plan_dir(self.slug), within=self.lib.root)
        # The store addresses a plan folder; it does not require a valid plan record to do so, and
        # coupling these tests to the plan document schema would make them fail for reasons that have
        # nothing to do with durability.
        (self.lib.plan_dir(self.slug) / "record.json").write_text(
            json.dumps({"plan_id": "pln_0123456789ab"}), encoding="utf-8")

    def _store(self, expected_revision=None):
        return build_state_store.DurableBuildStore(
            build_state_store.snapshot_path(self.lib, self.slug), SCHEMA, expected_revision,
            library_root=self.lib.root)


class Survives(_Library):
    """IT SURVIVES — across processes, and found again by the worktree alone."""

    def test_evidence_written_by_one_process_is_read_whole_by_the_next(self):
        wt = str(self.tmp / "wt")
        self._store().create(_state(worktree=wt))
        self._store().mutate(lambda s: s["progress"].update({"current_item": "only"}), from_revision=1)
        # A genuinely separate interpreter: the same in-process object could pass on cached bytes.
        read = subprocess.run(
            [sys.executable, "-c",
             "import sys, json; sys.path.insert(0, %r); import build_coordinator_core as core;"
             "print(json.dumps(core.json_file(__import__('pathlib').Path(%r))))"
             % (str(TOOLS), str(build_state_store.snapshot_path(self.lib, self.slug)))],
            capture_output=True, text=True, check=True)
        state = json.loads(read.stdout)
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["progress"]["current_item"], "only")

    def test_a_restarted_session_finds_its_snapshot_by_the_worktree_it_stands_in(self):
        wt = str(self.tmp / "wt")
        self._store().create(_state(worktree=wt))
        found = build_state_store.resolve_for_worktree(wt, SCHEMA, library=self.lib)
        self.assertEqual(found.read()["build"]["pr"], 1)

    def test_no_snapshot_for_this_worktree_refuses_with_the_way_forward(self):
        self._store().create(_state(worktree=str(self.tmp / "wt")))
        with self.assertRaises(core.CoordinatorError) as caught:
            build_state_store.resolve_for_worktree(self.tmp / "elsewhere", SCHEMA, library=self.lib)
        self.assertIn("plan bind", str(caught.exception))

    def test_two_snapshots_naming_one_worktree_refuse_rather_than_pick_one(self):
        wt = str(self.tmp / "wt")
        self._store().create(_state(worktree=wt))
        other = plan_store.slug_for("another plan", "pln_ba9876543210")
        plan_store.ensure_dir(self.lib.plan_dir(other), within=self.lib.root)
        (self.lib.plan_dir(other) / "record.json").write_text("{}", encoding="utf-8")
        build_state_store.DurableBuildStore(
            build_state_store.snapshot_path(self.lib, other), SCHEMA,
            library_root=self.lib.root).create(_state(pr=2, worktree=wt))
        with self.assertRaises(core.CoordinatorError) as caught:
            build_state_store.resolve_for_worktree(wt, SCHEMA, library=self.lib)
        self.assertIn("2 Build snapshots", str(caught.exception))

    def test_a_stale_writer_is_refused_and_changes_nothing(self):
        self._store().create(_state())
        self._store().mutate(lambda s: s["progress"].update({"current_item": "only"}), from_revision=1)
        with self.assertRaises(core.CoordinatorError):
            self._store().mutate(lambda s: s["progress"].update({"current_item": "other"}), from_revision=1)
        self.assertEqual(self._store().read()["progress"]["current_item"], "only")


class DoesNotLeak(_Library):
    """IT DOES NOT LEAK — the guarantees the plan library carries, carried here too."""

    def test_the_snapshot_and_every_directory_to_it_are_owner_only(self):
        self._store().create(_state())
        path = build_state_store.snapshot_path(self.lib, self.slug)
        self.assertEqual(os.stat(path).st_mode & 0o777, plan_store.FILE_MODE)
        for directory in (path.parent, path.parent.parent, self.lib.root):
            self.assertEqual(os.stat(directory).st_mode & 0o777, plan_store.DIR_MODE,
                             f"{directory} is not owner-only")

    def test_a_slug_that_escapes_the_library_is_refused_before_anything_is_written(self):
        for escape in ("../../etc", "/etc"):
            with self.assertRaises(core.CoordinatorError):
                build_state_store.snapshot_path(self.lib, escape)
        self.assertFalse((self.tmp / "etc").exists())

    def test_a_store_pointed_outside_its_library_refuses_at_construction(self):
        with self.assertRaises(core.CoordinatorError):
            build_state_store.DurableBuildStore(self.tmp / "loose.json", SCHEMA,
                                                library_root=self.lib.root)

    def test_the_unreliable_volume_warnings_are_the_library_s_own(self):
        self.assertIsNone(build_state_store.plan_store.volume_warning(self.lib.root))
        warned = plan_store.volume_warning(Path("/Users/x/Dropbox/plans"))
        self.assertIsNotNone(warned)
        self.assertIn("compare-and-swap", warned)


class TerminalConditionContract(_Library):
    """The withdrawn Stop evaluator cannot leave a model-authored authority channel behind."""

    def test_legacy_null_placeholder_remains_readable(self):
        self._store().create(_state(terminal_condition=None))
        self.assertIsNone(self._store().read()["terminal_condition"])

    def test_non_null_terminal_claim_is_rejected(self):
        condition = {
            "kind": "operator_pause",
            "source": {"authority": "operator-command", "reference": "prompt:42",
                       "observed_at": "2026-08-27T18:00:00Z"},
        }
        with self.assertRaises(core.CoordinatorError):
            self._store().create(_state(terminal_condition=condition))


class DoesNotDestroy(_Library):
    """IT DOES NOT DESTROY — the one store that can overwrite live Build evidence."""

    def _os_temp_snapshot(self, **over) -> Path:
        path = self.tmp / "ostemp" / "build-state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_state(**over)), encoding="utf-8")
        return path

    def test_migration_lands_in_the_library_and_keeps_its_source(self):
        source = self._os_temp_snapshot(pr=7)
        landed = build_state_store.migrate(source, "pln_0123456789ab", SCHEMA, library=self.lib,
                                           worktree=self.tmp / "wt")
        self.assertEqual(landed, build_state_store.snapshot_path(self.lib, self.slug))
        self.assertEqual(json.loads(landed.read_text())["build"]["pr"], 7)
        self.assertTrue(source.is_file(), "migration deleted its own source; there is no way back")

    def test_migration_carries_a_snapshot_written_before_a_field_was_retired(self):
        """The verb whose whole job is moving an older document forward must actually move it forward.

        This call site was added while answering a finding about untested call sites, and was itself
        untested: deleting the migration from `migrate` left the entire suite green. An OS-temp snapshot
        is exactly the document most likely to predate a retirement, because it is the one written by
        the engine version before the relocation.
        """
        source = self._os_temp_snapshot(
            pr=11, repair_rounds=[{"reviewed_commit": "a" * 40, "final_commit": "b" * 40,
                                   "judgment": "scoped", "lenses": ["usability"], "guidance": None,
                                   "spent": True}])
        landed = build_state_store.migrate(source, "pln_0123456789ab", SCHEMA, library=self.lib)
        self.assertNotIn("spent", json.loads(landed.read_text())["repair_rounds"][0])
        self.assertIn("spent", json.loads(source.read_text())["repair_rounds"][0],
                      "migration copies forward; it never edits the source it kept as the way back")

    def test_rolling_the_engine_back_across_the_relocation_finds_its_snapshot_where_it_was(self):
        """The rollback answer, and it is the reason migration keeps its source.

        An engine version before this change reads a snapshot only from the path it is handed, and
        knows nothing of the library. So the question a rollback asks is simply: is the OS-temp file
        still there, unaltered? It is, because migration copies rather than moves — and that is a
        deliberate cost (two copies until the operator removes one), paid so a downgrade mid-Build is
        a recoverable inconvenience instead of lost evidence.
        """
        source = self._os_temp_snapshot(pr=9)
        before = source.read_text()
        build_state_store.migrate(source, "pln_0123456789ab", SCHEMA, library=self.lib)
        self.assertEqual(source.read_text(), before)
        # And an old-shaped store, pointed at that path, still reads it whole.
        rolled_back = core.StateStore(str(source), SCHEMA)
        self.assertEqual(rolled_back.read()["build"]["pr"], 9)

    def test_a_migrated_snapshot_lands_in_the_current_schema_only(self):
        source = self._os_temp_snapshot()
        build_state_store.migrate(source, "pln_0123456789ab", SCHEMA, library=self.lib)
        landed = json.loads(build_state_store.snapshot_path(self.lib, self.slug).read_text())
        self.assertEqual(landed["schema_version"], build_state_store.CURRENT_SCHEMA_VERSION)

    def test_a_v1_snapshot_is_refused_with_a_remedy_and_left_untouched(self):
        source = self._os_temp_snapshot()
        source.write_text(json.dumps({"schema_version": "build-state.v1", "revision": 1}),
                          encoding="utf-8")
        before = source.read_text()
        with self.assertRaises(core.CoordinatorError) as caught:
            build_state_store.migrate(source, "pln_0123456789ab", SCHEMA, library=self.lib)
        message = str(caught.exception)
        self.assertIn("build-state.v1", message)
        self.assertIn("Finish this Build on the engine it started on", message)
        self.assertEqual(source.read_text(), before)
        self.assertFalse(build_state_store.snapshot_path(self.lib, self.slug).exists(),
                         "a refused migration left a partial snapshot behind")

    def test_migration_refuses_rather_than_land_on_an_existing_snapshot(self):
        self._store().create(_state(pr=1))
        source = self._os_temp_snapshot(pr=2)
        with self.assertRaises(core.CoordinatorError):
            build_state_store.migrate(source, "pln_0123456789ab", SCHEMA, library=self.lib)
        self.assertEqual(self._store().read()["build"]["pr"], 1)

    def test_no_rehearsal_file_survives_a_completed_migration(self):
        build_state_store.migrate(self._os_temp_snapshot(), "pln_0123456789ab", SCHEMA,
                                  library=self.lib)
        leftovers = [p.name for p in build_state_store.builds_dir(self.lib, self.slug).iterdir()
                     if p.name.endswith(".migrating")]
        self.assertEqual(leftovers, [])

    def test_a_second_build_of_one_plan_cannot_start_without_superseding(self):
        self._store().create(_state(pr=1))
        with self.assertRaises(core.CoordinatorError):
            self._store().create(_state(pr=2))

    def test_supersede_keeps_the_displaced_snapshot_and_says_why(self):
        self._store().create(_state(pr=1))
        retired = build_state_store.supersede(self.lib, self.slug, reason="the first Build wedged")
        self.assertTrue(retired.is_file())
        self.assertEqual(json.loads(retired.read_text())["build"]["pr"], 1)
        reason = json.loads(retired.with_suffix(".reason.json").read_text())
        self.assertEqual(reason["reason"], "the first Build wedged")
        self.assertFalse(build_state_store.snapshot_path(self.lib, self.slug).exists())
        # And the second Build may now start, in the place the first one held.
        self._store().create(_state(pr=2))
        self.assertEqual(self._store().read()["build"]["pr"], 2)

    def test_superseding_twice_at_one_revision_refuses_rather_than_overwrite(self):
        self._store().create(_state(pr=1))
        build_state_store.supersede(self.lib, self.slug, reason="first")
        self._store().create(_state(pr=2))
        with self.assertRaises(core.CoordinatorError):
            build_state_store.supersede(self.lib, self.slug, reason="second")

    def test_supersede_removes_the_binding_from_worktree_discovery_but_keeps_the_evidence(self):
        """A resuming session finds its Build by the worktree it is standing in (`bound_snapshots`).
        Clearing a confirmed-stale binding must make that discovery come up empty — the binding is
        gone — WHILE the displaced snapshot and its reason are retained beside it as evidence, not
        destroyed. This is the behaviour the advisory promises when it offers supersede."""
        wt = "/tmp/wt"
        self._store().create(_state(pr=1, worktree=wt))
        self.assertEqual([slug for slug, _ in build_state_store.bound_snapshots(wt, library=self.lib)],
                         [self.slug])
        retired = build_state_store.supersede(self.lib, self.slug, reason="confirmed stale after submission")
        # The binding is no longer discoverable for the worktree a resuming session would hold.
        self.assertEqual(build_state_store.bound_snapshots(wt, library=self.lib), [])
        # But the evidence is retained, unaltered, beside a .reason.json.
        self.assertTrue(retired.is_file())
        self.assertTrue(retired.with_suffix(".reason.json").is_file())
        self.assertEqual(json.loads(retired.read_text())["build"]["worktree"], wt)


class ASnapshotWrittenByTheEngineBeforeThisOne(unittest.TestCase):
    """IT SURVIVES A FIELD BEING REMOVED — the direction a schema-first rule does not cover.

    The Build schemas forbid unknown properties, so dropping a field breaks every snapshot already
    written with it: the store re-validates the WHOLE document on read and on write, so such a Build
    becomes unreadable the moment the removal lands, and the refusal names no verb that recovers it.
    """

    def _round(self, **over):
        entry = {"reviewed_commit": "a" * 40, "final_commit": "b" * 40, "judgment": "scoped",
                 "lenses": ["usability"], "guidance": None}
        entry.update(over)
        return entry

    def test_a_round_recorded_with_the_superseded_spent_flag_still_loads(self):
        migrated = core.forward_migrate({"repair_rounds": [self._round(spent=True)]})
        self.assertEqual(migrated["repair_rounds"], [self._round()],
                         "`spent` is dropped, not tolerated: the ledger records what a round cost "
                         "once, as `counted`")

    def test_the_migration_touches_nothing_it_does_not_have_to(self):
        clean = {"repair_rounds": [self._round(counted=True)], "revision": 4}
        self.assertIs(core.forward_migrate(clean), clean,
                      "a snapshot with nothing to migrate is returned as it stands, not rebuilt")
        self.assertEqual(core.forward_migrate({"revision": 1}), {"revision": 1})
        self.assertEqual(core.forward_migrate({"repair_rounds": []}), {"repair_rounds": []})

    def test_the_migration_does_not_mutate_the_document_it_was_handed(self):
        original = {"repair_rounds": [self._round(spent=True)]}
        core.forward_migrate(original)
        self.assertIn("spent", original["repair_rounds"][0],
                      "the loaded document is copied, never edited underneath its caller")

    def test_a_real_snapshot_file_carrying_it_loads_and_saves_through_a_real_store(self):
        """The WIRING, not the function. Calling `forward_migrate` directly proves the function; it
        proves nothing about whether anything calls it, and deleting either call site left every unit
        test green -- the guard-with-no-teeth shape this build keeps meeting."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            state = _state()
            state["repair_rounds"] = [self._round(spent=True)]
            path.write_text(json.dumps(state), encoding="utf-8")
            store = core.StateStore(str(path), SCHEMA)
            loaded = store.read()                      # refuses outright if the load does not migrate
            self.assertNotIn("spent", loaded["repair_rounds"][0])
            store.mutate(lambda s: s.update({"submission": "draft"}), from_revision=loaded["revision"])
            self.assertNotIn("spent", json.loads(path.read_text())["repair_rounds"][0])

    def test_the_handoff_strip_runs_BEFORE_the_document_is_validated(self):
        """The half that was not closed the first time, pinned where it actually went wrong.

        The strip was placed in the restore BUILDER, which validation never reaches: the verb validates
        the handoff first, so a document carrying `spent` was refused outright -- the exact failure the
        migration exists to prevent. Order is the whole property, so order is what is asserted; a unit
        test on the function could not see it, and neither could any test in the suite.
        """
        import inspect
        import build_coordinator as bc
        source = inspect.getsource(bc.cmd_handoff_restore)
        # Anchored on the ASSIGNMENT, not the first `forward_migrate` token anywhere in the function:
        # a comment mentioning the name above the validate call would otherwise satisfy this while the
        # real call sat after it.
        migrate_at = source.index('value["repair_rounds"] = core.forward_migrate')
        validate_at = source.index("_validate(value, HANDOFF_SCHEMA_V2)")
        self.assertLess(migrate_at, validate_at,
                        "a strip that must beat a schema has to run before the validate call")

    def test_a_stripped_handoff_validates_and_restores_with_the_field_gone(self):
        """The narrower half, stated as what it is. This checks the OUTCOME against the real schema --
        a migrated document validates and restores clean -- and NOT the ordering, which it cannot see:
        it migrates its own fixture rather than driving the verb. The ordering is pinned above."""
        import build_coordinator as bc
        base = _state()
        rounds = core.forward_migrate({"repair_rounds": [self._round(spent=True)]})["repair_rounds"]
        handoff = {"schema_version": "build-handoff.v2", "repair_rounds": rounds,
                   "build": base["build"], "plan": base["plan"], "approval": None,
                   "reviews": base["reviews"], "work": {}, "finding_summaries": [],
                   "progress": base["progress"], "validation": None, "repair": None,
                   "preflights": [], "pr_contract": None}
        core.validate(handoff, bc.HANDOFF_SCHEMA_V2)
        self.assertNotIn("spent", bc._restore_base_state(handoff, "build-state.v2")["repair_rounds"][0])

    def test_a_legacy_round_keeps_costing_its_slot(self):
        """Dropping the flag must not refund the round. `_round_counted` reads an absent `counted` as
        counted -- the fail-toward-spent direction."""
        import build_coordinator as bc
        migrated = core.forward_migrate({"repair_rounds": [self._round(spent=True)]})
        self.assertTrue(bc._round_counted(migrated["repair_rounds"][0]))


class OneHome(unittest.TestCase):
    """IT HAS ONE HOME — asserted structurally, because drift here is silent."""

    # The modules that keep a revisioned store: the Build's snapshot, the durable Build snapshot, and
    # the plan library. Scoped to these deliberately. Other modules in this tree lock files for
    # unrelated reasons (a boot alarm ledger, telemetry), and folding them in would make this
    # assertion a repository-wide ban on `flock` rather than the claim it is actually making — that
    # the STORES share one implementation of the discipline instead of each carrying a copy.
    STORE_MODULES = ("build_coordinator.py", "build_state_store.py", "plan_store.py")

    def test_the_durable_store_is_a_peer_of_the_state_store_not_a_subclass(self):
        self.assertTrue(issubclass(build_state_store.DurableBuildStore, core.RevisionedStore))
        self.assertFalse(issubclass(build_state_store.DurableBuildStore, core.StateStore))

    def test_no_store_reimplements_the_lock_or_the_atomic_replace(self):
        for name in self.STORE_MODULES:
            source = (TOOLS / name).read_text(encoding="utf-8")
            for primitive in ("fcntl.flock", "os.replace", "mkstemp"):
                self.assertNotIn(primitive, source,
                                 f"{name} implements {primitive} itself instead of using the one in "
                                 "build_coordinator_core")
        core_source = (TOOLS / "build_coordinator_core.py").read_text(encoding="utf-8")
        for primitive in ("fcntl.flock", "os.replace", "mkstemp"):
            self.assertIn(primitive, core_source)

    def test_the_durable_store_restates_none_of_the_revisioned_store_discipline(self):
        source = (TOOLS / "build_state_store.py").read_text(encoding="utf-8")
        for restated in ("def mutate", "def read", "def _write", "assert_revision", "flock"):
            self.assertNotIn(restated, source,
                             f"build_state_store re-expresses {restated!r} instead of inheriting it")

    def test_the_os_temp_refusal_is_gone_from_the_state_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "not-in-os-temp.json"
            store = core.StateStore(str(path), SCHEMA)
            store.create(_state())
            self.assertEqual(store.read()["build"]["pr"], 1)


class TheSeamAnOperatorActuallyCrosses(unittest.TestCase):
    """`plan bind` with NO --state must land the Build in the durable store. That is the whole restart
    story, and until now nothing drove it.

    Every test that exercised binding handed `cmd_plan_bind` a store object, and the one end-to-end CLI
    demonstration always passed `--state <path>` — so both bypassed the default entirely. The store's own
    mechanics were well covered and the wiring read correctly, but the seam from 'an operator types
    `plan bind` with no flags' to 'the Build now lives beside its sealed plan' was joined by inspection
    only. A regression in the `deferred` condition, or a swallowed failure in the fallback, would have
    silently defeated the central promise of this change with a green suite."""

    def test_main_defers_the_store_for_bind_and_only_for_bind(self):
        """Driven through main()'s real argument parsing, not a hand-built namespace — the seam IS the
        parsing, so a namespace test would assume exactly what is in question."""
        import build_coordinator as bc
        seen = {}

        def spy(args, store):
            seen["store"] = store
            seen["command"] = f"{args.command}/{getattr(args, 'plan_command', None)}"

        with mock.patch.object(bc, "cmd_plan_bind", spy), \
                mock.patch.object(bc, "_resolve_store", side_effect=AssertionError(
                    "bind must NOT resolve a snapshot: it is the command that creates one")):
            code = bc.main(["plan", "bind", "--plan", "pln_0123456789ab", "--repository", "o/r",
                            "--pr", "1", "--operator-decided"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["command"], "plan/bind")
        self.assertIsNone(seen["store"], "without --state, bind must choose its own durable address")

    def test_but_an_explicit_state_path_still_wins(self):
        """The escape hatch has to keep working, or every existing invocation breaks."""
        import build_coordinator as bc
        seen = {}
        with mock.patch.object(bc, "cmd_plan_bind", lambda a, s: seen.update(store=s)), \
                mock.patch.object(bc, "_resolve_store", return_value="explicit"):
            bc.main(["--state", "/tmp/x.json", "plan", "bind", "--plan", "pln_0123456789ab",
                     "--repository", "o/r", "--pr", "1", "--operator-decided"])
        self.assertEqual(seen["store"], "explicit")

    def test_and_bind_reaches_the_durable_store_for_the_plan_it_binds(self):
        """The other half of the seam: given no store, bind asks the durable store for THIS plan's
        address rather than inventing one."""
        import build_coordinator as bc
        asked = {}

        def store_for_plan(plan_id, schema_for, library=None):
            asked["plan_id"] = plan_id
            raise bc.CoordinatorError("stop here — the address lookup is what was under test")

        head = "a" * 40
        with mock.patch.object(bc.build_state_store, "store_for_plan", store_for_plan), \
                mock.patch.object(bc, "_sealed_plan",
                                  return_value=("pln_0123456789ab", "sha256:" + "f" * 64, PLAN)), \
                mock.patch.object(bc, "_verify_draft",
                                  return_value={"headRefOid": head, "baseRefOid": "0" * 40}), \
                mock.patch.object(bc, "_head", return_value=head), \
                mock.patch.object(bc, "_library", return_value=mock.MagicMock()), \
                self.assertRaises(bc.CoordinatorError):
            bc.cmd_plan_bind(argparse.Namespace(
                plan="pln_0123456789ab", repository="o/r", pr=1, issue=None, mode="same-session",
                operator_decided=True, state=None), None)
        self.assertEqual(asked["plan_id"], "pln_0123456789ab")


class TheKillAndResumeDemo(unittest.TestCase):
    """The standalone reproducer, run end to end. Importing it here is also what keeps the demonstration
    alive for the census reference-closure, so it ships rather than retiring as construction evidence —
    a Build outliving the session that started it is forever-relevant to any deployed project."""

    def test_the_kill_and_resume_demo_passes(self):
        import quiet_call
        import demo_build_resumes_after_a_kill as demo
        self.assertEqual(quiet_call.run(demo.main), 0)


# Actual supersede from a54c8119, frozen to challenge the compatibility boundary.
_LEGACY_SUPERSEDE = 'def supersede(library: plan_store.PlanLibrary, slug: str, *, reason: str) -> Path | None:\n    """Clear a confirmed-stale binding: set the current snapshot aside so a fresh Build of the same\n    plan may start. Never silent.\n\n    This is deliberately NOT the resume path — a genuine continuation keeps its worktree and\n    re-verifies the binding in place, and never comes here. Nor is it needed to start a Build of some\n    OTHER plan: each plan gets its own snapshot, so a different plan just binds fresh. But this plan\n    cannot bind fresh in a different worktree — snapshots are keyed by plan, not worktree, so while this\n    snapshot exists a re-bind of the same plan is refused. Superseding clears that one snapshot so its\n    slot is free again. Once cleared, the plan no longer answers `bound_snapshots` (the live snapshot is\n    gone), so a resuming session sees no live work for it. Superseding neither completes the plan nor\n    touches the PR.\n\n    The displaced snapshot is MOVED, not removed: it becomes `superseded-<revision>.json` beside the\n    new one, byte-for-byte as it stood, with the reason recorded in a sibling `.reason.json`. An\n    operator superseding a Build usually does so because something went wrong, which is precisely\n    when the evidence of what went wrong is worth keeping — and keeping the snapshot itself\n    unaltered is what lets it still be read as the schema-valid document it is.\n    """\n    current = snapshot_path(library, slug)\n    if not current.is_file():\n        return None\n    state = core.json_file(current)\n    revision = state.get("revision", 0)\n    retired = current.with_name(f"superseded-{revision:06d}.json")\n    if retired.exists():\n        raise BuildStateError(\n            f"{retired} already exists, so superseding again would overwrite a snapshot already set "\n            "aside. Move or delete it first — this store does not silently destroy evidence.")\n    core.atomic_write(retired, json.dumps(state, indent=2, sort_keys=True) + "\\n",\n                      durable=True, mode=plan_store.FILE_MODE)\n    core.atomic_write(retired.with_suffix(".reason.json"),\n                      json.dumps({"at": moment.utc_now(), "reason": reason,\n                                  "superseded_revision": revision}, indent=2, sort_keys=True) + "\\n",\n                      durable=True, mode=plan_store.FILE_MODE)\n    current.unlink()\n    lock = current.with_name(current.name + ".lock")\n    if lock.exists():\n        lock.unlink()\n    return retired'

def _paused_legacy_supersede(library_root, slug, after_read, paused, resume, outcome):
    namespace = dict(vars(build_state_store))
    namespace['snapshot_path'] = build_state_store._legacy_slot
    exec(_LEGACY_SUPERSEDE, namespace)
    read = core.json_file
    def pause_read(path):
        result = read(path) if after_read else None
        paused.set()
        if not resume.wait(10):
            raise RuntimeError('parent did not release legacy reader')
        return result if after_read else read(path)
    try:
        with mock.patch.object(core, 'json_file', side_effect=pause_read):
            namespace['supersede'](plan_store.PlanLibrary(Path(library_root)), slug, reason='old process')
    except (OSError, core.CoordinatorError):
        outcome.put('refused')
    else:
        outcome.put('retired')


class TransactionalOwnership(unittest.TestCase):
    def setUp(self):
        from test_plan_store import _document
        import plan_contract
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lib = plan_store.PlanLibrary(self.root / 'plans')
        doc = _document()
        self.slug = self.lib.create(doc)
        record = self.lib.read_record(self.slug)
        stamp = '2026-09-08T00:00:00Z'
        self.seal = {'revision': 1, 'reviewed_digest': record['current']['plan_digest'],
                     'sealed_digest': record['current']['plan_digest'],
                     'build_plan_digest': plan_contract.build_plan_digest(doc),
                     'at': stamp, 'delta_judgment': 'none'}
        self.lib.update_record(self.slug, lambda r: r.update(
            seal=self.seal, consent=[{'gate': 'seal', 'at': stamp}]))
        self.state = _state(worktree=str(self.root / 'worktree'))
        self.state['plan'].update(sealed_digest=self.seal['sealed_digest'],
                                  digest=self.seal['build_plan_digest'])
        self.consent = {'gate': 'bind', 'at': stamp}

    def reserve(self, **kwargs):
        return build_state_store.reserve_build(self.lib, self.slug, self.state,
                                                consent=self.consent, **kwargs)

    def finish(self, claim):
        return build_state_store.finish_binding(self.lib, self.slug,
            build_state_store.claim_identity(claim), self.state, SCHEMA)

    def test_reservation_retry_preserves_identity_and_consent(self):
        claim = self.reserve()
        self.assertEqual(claim, self.reserve())
        record = self.lib.read_record(self.slug)
        self.assertEqual(record['consent'], [{'gate': 'seal', 'at': self.consent['at']}, self.consent])
        self.assertEqual(claim['state'], 'preparing')
        self.assertFalse(Path(claim['snapshot']).exists())
        self.state['build']['pr'] = 2
        with self.assertRaisesRegex(core.CoordinatorError, 'different Build'):
            self.reserve(locator=str(self.root / 'other'))
        self.assertEqual(self.lib.read_record(self.slug), record)

    def test_missing_identity_and_wrong_generation_cannot_mutate(self):
        claim = self.reserve(); self.finish(claim)
        for identity in [None, {'build_id': claim['build_id'], 'generation': 2}]:
            store = build_state_store.ClaimedBuildStore(self.lib, self.slug, SCHEMA, identity=identity)
            with self.assertRaisesRegex(core.CoordinatorError, 'identity'):
                store.mutate(lambda s: s['progress'].update(current_item='wrong'))
        store = build_state_store.ClaimedBuildStore(self.lib, self.slug, SCHEMA,
            identity=build_state_store.claim_identity(claim))
        store.mutate(lambda s: s['progress'].update(current_item='right'))
        self.assertEqual(store.read()['progress']['current_item'], 'right')

    def test_directory_flush_failure_is_recoverable_without_new_identity(self):
        claim = self.reserve()
        with mock.patch.object(core, 'fsync_dir', return_value=False):
            with self.assertRaisesRegex(core.CoordinatorError, 'durability is uncertain'):
                self.finish(claim)
        self.assertEqual(self.lib.read_record(self.slug)['build_lease']['current']['state'], 'preparing')
        self.assertEqual(self.reserve()['build_id'], claim['build_id'])
        saved = self.finish(claim)
        self.assertEqual(saved['ownership'], build_state_store.claim_identity(claim))

    def test_activation_failure_retries_existing_evidence(self):
        claim = self.reserve()
        real = self.lib.write_build_record_locked
        def fail_activation(slug, record):
            if record['build_lease']['current']['state'] == 'active':
                raise OSError('interrupted activation')
            real(slug, record)
        with mock.patch.object(self.lib, 'write_build_record_locked', side_effect=fail_activation):
            with self.assertRaisesRegex(OSError, 'activation'):
                self.finish(claim)
        before = Path(claim['snapshot']).read_bytes()
        self.state['progress']['current_item'] = 'retry must not overwrite'
        self.finish(claim)
        self.assertEqual(Path(claim['snapshot']).read_bytes(), before)

    def test_reservation_visible_before_failed_flush_retries_without_duplicate_consent(self):
        real = core.atomic_write
        def interrupted(path, text, **kwargs):
            real(path, text, **kwargs)
            if path.name == plan_store.RECORD_FILENAME:
                raise OSError('killed after reservation write')
        with mock.patch.object(core, 'atomic_write', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.reserve()
        claim = self.lib.read_record(self.slug)['build_lease']['current']
        self.assertEqual(self.reserve(), claim)
        self.assertEqual(sum(c['gate'] == 'bind' for c in self.lib.read_record(self.slug)['consent']), 1)
        self.finish(claim)

    def test_legacy_cutover_after_rename_is_recovered_from_preserved_original(self):
        old = build_state_store._legacy_slot(self.lib, self.slug)
        build_state_store.DurableBuildStore(old, SCHEMA, library_root=self.lib.root).create(self.state)
        original = old.read_bytes()
        self.lib.update_record(self.slug, lambda r: r.update(build_binding={
            'sealed_digest': self.seal['sealed_digest'], 'build_plan_digest': self.seal['build_plan_digest'],
            'repository': 'o/r', 'pull_request': 1, 'at': self.consent['at']}))
        claim = self.reserve(legacy_source=old)
        with mock.patch.object(build_state_store, '_flush_directory', side_effect=OSError('cutover interrupted')):
            with self.assertRaises(OSError):
                self.finish(claim)
        self.assertFalse(old.exists())
        preserved = Path(claim['snapshot']).parent / 'legacy-original.json'
        self.assertEqual(preserved.read_bytes(), original)
        self.finish(claim)
        self.assertTrue(old.is_dir())
        self.assertEqual(preserved.read_bytes(), original)

    def test_old_supersede_cannot_remove_tombstone_or_evidence_or_lock(self):
        claim = self.reserve(); self.finish(claim)
        namespace = dict(vars(build_state_store))
        namespace['snapshot_path'] = build_state_store._legacy_slot
        exec(_LEGACY_SUPERSEDE, namespace)
        lock = build_state_store._legacy_lock(self.lib, self.slug)
        inode = lock.stat().st_ino
        evidence = Path(claim['snapshot']).read_bytes()
        self.assertIsNone(namespace['supersede'](self.lib, self.slug, reason='old client'))
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertEqual(Path(claim['snapshot']).read_bytes(), evidence)
        self.assertTrue(build_state_store._legacy_slot(self.lib, self.slug).is_dir())

    def test_legacy_cutover_preserves_original_and_old_lock_inode(self):
        old = build_state_store._legacy_slot(self.lib, self.slug)
        build_state_store.DurableBuildStore(old, SCHEMA, library_root=self.lib.root).create(self.state)
        old_bytes = old.read_bytes()
        binding = {'sealed_digest': self.seal['sealed_digest'],
                   'build_plan_digest': self.seal['build_plan_digest'],
                   'repository': 'o/r', 'pull_request': 1, 'at': self.consent['at']}
        self.lib.update_record(self.slug, lambda r: r.update(build_binding=binding))
        inode = build_state_store._legacy_lock(self.lib, self.slug).stat().st_ino
        with self.assertRaisesRegex(core.CoordinatorError, 'explicit state migrate'):
            self.reserve()
        claim = self.reserve(legacy_source=old)
        self.finish(claim)
        self.assertTrue(old.is_dir())
        self.assertEqual((Path(claim['snapshot']).parent / 'legacy-original.json').read_bytes(), old_bytes)
        self.assertEqual(build_state_store._legacy_lock(self.lib, self.slug).stat().st_ino, inode)
        with self.assertRaises((OSError, core.CoordinatorError)):
            core.StateStore(str(old), SCHEMA).create(self.state)

    def test_corrupt_reserved_snapshot_is_never_replaced(self):
        claim = self.reserve()
        path = Path(claim['snapshot']); path.parent.mkdir(parents=True)
        path.write_text('broken')
        with self.assertRaises(core.CoordinatorError):
            self.finish(claim)
        self.assertEqual(path.read_text(), 'broken')

    def test_invalid_claim_identity_is_schema_rejected(self):
        self.reserve()
        for field, value in [('build_id', 'bad'), ('generation', 0), ('state', 'invented')]:
            record = self.lib.read_record(self.slug)
            record['build_lease']['current'][field] = value
            with self.assertRaises(core.CoordinatorError):
                core.validate(record, plan_store.RECORD_SCHEMA)

    def test_legacy_superseder_paused_before_and_after_read_cannot_destroy_upgraded_build(self):
        for after_read in (False, True):
            # Each witness has its own real plan and independent process.
            fixture = TransactionalOwnership(); fixture.setUp()
            try:
                old = build_state_store._legacy_slot(fixture.lib, fixture.slug)
                build_state_store.DurableBuildStore(old, SCHEMA, library_root=fixture.lib.root).create(fixture.state)
                fixture.lib.update_record(fixture.slug, lambda r: r.update(build_binding={
                    'sealed_digest': fixture.seal['sealed_digest'],
                    'build_plan_digest': fixture.seal['build_plan_digest'], 'repository': 'o/r',
                    'pull_request': 1, 'at': fixture.consent['at']}))
                ctx = multiprocessing.get_context('spawn')
                paused, resume, result = ctx.Event(), ctx.Event(), ctx.Queue()
                child = ctx.Process(target=_paused_legacy_supersede,
                    args=(str(fixture.lib.root), fixture.slug, after_read, paused, resume, result))
                child.start()
                try:
                    self.assertTrue(paused.wait(10), 'legacy command did not reach barrier')
                    lock = build_state_store._legacy_lock(fixture.lib, fixture.slug)
                    inode = lock.stat().st_ino
                    claim = fixture.reserve(legacy_source=old); fixture.finish(claim)
                    evidence = Path(claim['snapshot']).read_bytes()
                    resume.set(); child.join(10)
                    self.assertFalse(child.is_alive(), 'legacy command deadlocked')
                    self.assertEqual(child.exitcode, 0)
                    self.assertEqual(result.get(timeout=2), 'refused')
                    self.assertEqual(lock.stat().st_ino, inode)
                    self.assertEqual(Path(claim['snapshot']).read_bytes(), evidence)
                    self.assertTrue(old.is_dir())
                finally:
                    resume.set()
                    if child.is_alive():
                        child.terminate(); child.join(5)
                    result.close(); result.join_thread()
            finally:
                fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
