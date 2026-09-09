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
import contextlib
import io
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

    def test_an_explicit_state_path_is_carried_as_a_locator_into_the_transaction(self):
        import build_coordinator as bc
        seen = {}
        with mock.patch.object(bc, "cmd_plan_bind", lambda a, s: seen.update(store=s, locator=a.state)), \
                mock.patch.object(bc, "_resolve_store", side_effect=AssertionError('bind must reserve first')):
            bc.main(["--state", "/tmp/x.json", "plan", "bind", "--plan", "pln_0123456789ab",
                     "--repository", "o/r", "--pr", "1", "--operator-decided"])
        self.assertIsNone(seen['store'])
        self.assertEqual(seen['locator'], '/tmp/x.json')

    def test_and_bind_reaches_the_durable_store_for_the_plan_it_binds(self):
        """The other half of the seam: given no store, bind asks the durable store for THIS plan's
        address rather than inventing one."""
        import build_coordinator as bc
        asked = {}

        def reserve(library, slug, state, **kwargs):
            asked["plan_id"] = state['plan']['plan_id']
            raise bc.CoordinatorError("stop here — the address lookup is what was under test")

        head = "a" * 40
        from test_build_coordinator import _entry_observation_fixture
        library = mock.MagicMock()
        library.read_record.return_value = {}
        with mock.patch.object(bc.entry, "observe_fresh", side_effect=_entry_observation_fixture), \
                mock.patch.object(bc.entry, "verify_frozen"), \
                mock.patch.object(bc.build_state_store, "reserve_build", reserve), \
                mock.patch.object(bc, "_sealed_plan",
                                  return_value=("pln_0123456789ab", "sha256:" + "f" * 64, PLAN)), \
                mock.patch.object(bc, "_verify_draft",
                                  return_value={"headRefOid": head, "baseRefOid": "0" * 40}), \
                mock.patch.object(bc, "_head", return_value=head), \
                mock.patch.object(bc, "_library", return_value=library), \
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

    def test_every_selectable_interruption_recovers(self):
        import demo_build_resumes_after_a_kill as demo
        import quiet_call
        for point in ('none', 'reservation', 'activation', 'retire-archive', 'retire-rename', 'release'):
            with self.subTest(point=point), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(quiet_call.run(demo.main, ['--interrupt', point, '--contenders', '3']), 0)

    def test_demo_fails_when_old_writer_fencing_is_broken(self):
        import demo_build_resumes_after_a_kill as demo
        import quiet_call
        with mock.patch.object(build_state_store.ClaimedBuildStore, '_check_write', return_value=None), \
                mock.patch('builtins.print') as printed:
            self.assertEqual(quiet_call.run(demo.main, ['--interrupt', 'none']), 1)
        self.assertTrue(any('FAIL: old caller refuses' in str(c) for c in printed.call_args_list))

    def test_demo_fails_when_competing_attempts_are_reported_as_winners(self):
        import demo_build_resumes_after_a_kill as demo
        import quiet_call
        actual = demo._contend
        def corrupt(*args):
            results = actual(*args)
            winner = next(r for r in results if r[0] == 'reserved')
            return [winner, winner]
        with mock.patch.object(demo, '_contend', side_effect=corrupt), \
                mock.patch('builtins.print') as printed:
            self.assertEqual(quiet_call.run(demo.main, []), 1)
        self.assertTrue(any('FAIL: exactly one reservation' in str(c) for c in printed.call_args_list))




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


# Frozen pre-upgrade command bodies from 8b354a81ae36015dbd7c872670f26b17cb757418.
# Shared parsing/locking helpers are reused; these exact old bodies own the challenged decisions.
_LEGACY_BIND = 'def cmd_plan_bind(args, store: Snapshot) -> None:\n    mode = getattr(args, "mode", "same-session")\n    plan_id, sealed_digest, plan = _sealed_plan(args.plan)\n    # The closed door. B2 made v1 unreachable at entry; the sunset removed the schemas and the\n    # converter, so this refusal is now terminal rather than a wait. Deliberately so: migrating a\n    # SEALED plan would invalidate the seal that is the only thing making it a plan a Build may enter,\n    # and a converter that produced a plan nobody approved would have been a way around the seal\n    # wearing the costume of a migration. Re-authoring is the path, and the refusal names it.\n    if _plan_version(plan) == "build-plan.v1":\n        raise CoordinatorError(\n            "this sealed plan carries a build-plan.v1 payload, and v1 no longer enters a Build. There "\n            "is no converter: a migration would invalidate the seal, and an unapproved plan is not a "\n            "plan. Re-author this work as a fresh plan through the Project Manager — its deliberation "\n            "can be imported from the old one — and seal that. If a v1 Build is already in flight, "\n            "finish it on the engine it started on.")\n    issue = args.issue\n    # Profile first, then authorization. Both can be true of one bad bind — a trivial plan handed an\n    # Issue and unattended mode breaks two rules at once — and the profile rule is the root cause: it\n    # says this plan may not run in this mode AT ALL, so no Issue could have fixed it. Reporting the\n    # authorization failure there would send the operator hunting for the right Issue number for a\n    # Build that was never going to be unattended.\n    if plan["profile"] == "trivial" and mode != "same-session":\n        raise CoordinatorError("trivial Builds are same-session only")\n    if plan["profile"] == "routine" and mode != "unattended":\n        raise CoordinatorError("routine plans require unattended mode and durable Issue authority")\n    _check_authorization(plan, issue, mode)\n    # The last consent gate, and the one the silent ceremony reached: a sealed plan bound to a fresh\n    # pull request in the same unattended breath as the seal that produced it. The operator\'s go for\n    # the BUILD to begin is its own decision, distinct from their go to seal the plan, and it is\n    # taken here where the Build actually starts. Recorded, not proven (issue 914\'s residual).\n    import moment\n    import plan_lifecycle\n    if not getattr(args, "operator_decided", False):\n        raise CoordinatorError(plan_lifecycle.missing_consent({}, "bind"))\n    consent = plan_lifecycle.attestation("bind", at=moment.utc_now())\n    pr = _verify_draft(args.repository, args.pr)\n    if pr.get("headRefOid") != _head():\n        raise CoordinatorError("the draft PR head does not match this worktree")\n    state = _initial_state(args.repository, args.pr, pr.get("baseRefOid") or _base(), plan_id,\n                           sealed_digest, plan, issue, mode)\n    # Where this Build\'s evidence lands. With no --state it goes to the durable store beside its own\n    # sealed plan, which is the default because the alternative is what actually happened: a killed\n    # Build whose approval, receipts, findings and progress were reconstructed by hand.\n    if store is None:\n        store = build_state_store.store_for_plan(plan_id, _state_schema_for, library=_library())\n    # The refusal `store.create` would raise is asserted HERE, before the binding write. Two cold\n    # reviewers independently proved the alternative: with the write first, a plain operator retry —\n    # bind again over an existing Build — rewrote `build_binding` to a PR that carries no Build and\n    # appended a consent attestation for a bind that was then refused. A refused command must leave\n    # nothing behind. (A crash BETWEEN the binding write and `create` still converges: the snapshot\n    # does not exist yet, so this check passes on the re-run and the same binding is rewritten.)\n    if store.path.exists():\n        raise CoordinatorError(\n            f"a durable Build snapshot already exists at {store.path} — this plan\'s Build is "\n            "already bound. Resume it (`status`, or `handoff export`) rather than re-binding; "\n            "nothing was written.")\n    _record_build_binding(plan_id, args.repository, args.pr, sealed_digest, state["plan"]["digest"],\n                          consent)\n    store.create(state)\n    # Tag the PR the coordinator just adopted, so it carries a durable "coordinator owns this workflow"\n    # marker (StarshipSuperjam/engine-template#1014). Best-effort and non-fatal: a labeling failure is\n    # disclosed on stderr and the Build proceeds — the stdout below stays a clean machine-readable line.\n    if not github.tag_coordinator_owned(ROOT, args.repository, args.pr):\n        print("build-coordinator: could not tag this PR \'engine-coordinator-owned\' (a non-blocking aid); "\n              "the Build proceeds — reach ready only through \'submit apply\', never a bare \'gh pr ready\'.",\n              file=sys.stderr)\n    _record_session_binding(state, pr_number=args.pr)\n    # The carrier rule, said at the kickoff itself (StarshipSuperjam/engine-template#1091): on\n    # stderr with the other human-facing notes, so stdout stays the one machine-readable line.\n    print(plan_lifecycle.CARRIER_RULE, file=sys.stderr)\n    print(json.dumps({"plan_digest": state["plan"]["digest"], "state": str(store.path)}))'
_LEGACY_MIGRATE = 'def migrate(source: Path | str, selector: str, schema, *,\n            library: plan_store.PlanLibrary | None = None, worktree: Path | str | None = None) -> Path:\n    """Move one OS-temp snapshot into the durable library, or refuse with a remedy.\n\n    PROVEN ON A COPY FIRST, and that ordering is the whole safety argument. This function is the one\n    place in the engine that can destroy live Build evidence, so nothing touches the real snapshot\n    until the migrated document has been built, validated against the schema it will be stored\n    under, and written to a scratch file inside the destination folder. Only then does the atomic\n    replace happen, and only then is the source left behind — left, never deleted, because a\n    migration that removes its own source has no way back if the operator disagrees with the result.\n\n    A build-state.v1 snapshot is REFUSED rather than converted, and the refusal names why. v1 is a\n    linear Build with no work ledger, and the current schema derives completion from integration\n    evidence that a v1 snapshot never recorded. Fabricating an empty ledger would produce a document\n    that validates and then wedges: every completed item would read as completed without the\n    evidence that earns it. So an in-flight v1 Build finishes on the engine it started on.\n    """\n    library = library or plan_store.PlanLibrary()\n    source_path = Path(source).resolve()\n    if not source_path.is_file():\n        raise BuildStateError(f"no snapshot to migrate at {source_path}")\n    state = core.json_file(source_path)\n    version = state.get("schema_version")\n    if version != CURRENT_SCHEMA_VERSION:\n        raise BuildStateError(\n            f"{source_path} is a {version or \'versionless\'} Build snapshot, and the durable store "\n            f"holds {CURRENT_SCHEMA_VERSION} only. It is not converted, because a {version} snapshot "\n            "carries no work ledger and the current schema derives completion from one — an invented "\n            "ledger would validate and then wedge the Build. Finish this Build on the engine it "\n            "started on, or abandon it and re-bind its sealed plan for a fresh Build. The file is "\n            "untouched.")\n    slug = library.resolve(selector)\n    destination = snapshot_path(library, slug)\n    if destination.exists():\n        raise BuildStateError(\n            f"{slug} already holds a durable Build snapshot at {destination}. Migrating over it would "\n            "destroy the evidence already there; supersede it explicitly if that is what you mean.")\n    if worktree is not None:\n        state.setdefault("build", {})["worktree"] = str(Path(worktree).resolve())\n    # Through the same forward migration the stores apply on load. This verb exists to move a document\n    # written by an older engine forward, so it is the last place that should refuse one for carrying a\n    # field that engine declared and this one retired.\n    state = core.forward_migrate(state)\n    core.validate(state, schema(state) if callable(schema) else schema)\n    plan_store.ensure_dir(destination.parent, within=library.root)\n    # The rehearsal: the exact bytes, written to a scratch name in the destination folder, so a full\n    # disk or a refused durable flush fails HERE, with the source still the only copy that matters.\n    rehearsal = destination.with_name(destination.name + ".migrating")\n    core.atomic_write(rehearsal, json.dumps(state, indent=2, sort_keys=True) + "\\n",\n                      durable=True, mode=plan_store.FILE_MODE)\n    rehearsal.replace(destination)\n    return destination'
_LEGACY_MUTATE = 'def mutate(self, change: Callable[[dict], Any], *, from_revision: int | None = None) -> Any:\n    with self._locked():\n        if not self.path.exists():\n            raise CoordinatorError(f"no {self.what} at {self.path}; {self.missing_remedy}")\n        state = forward_migrate(json_file(self.path))\n        validate(state, self._schema_for(state))\n        expected = self.expected_revision if self.expected_revision is not None else from_revision\n        assert_revision(state["revision"], expected, "snapshot", self.stale_remedy)\n        result = change(state)\n        state["revision"] += 1\n        self._write(state)\n        return result'

def _legacy_reservation_after_source_lock(root, slug, source, state, attempted, outcome):
    lib = plan_store.PlanLibrary(root)
    real = core.exclusive_lock
    @contextlib.contextmanager
    def observed(path):
        if Path(path).resolve() == Path(source).resolve().with_name(Path(source).name + '.lock'):
            attempted.set()
        with real(path):
            yield
    try:
        with mock.patch.object(core, 'exclusive_lock', observed):
            build_state_store.reserve_build(lib, slug, state, legacy_source=source,
                                            legacy_clients_stopped=True)
        outcome.put('reserved')
    except core.CoordinatorError as exc:
        outcome.put(str(exc))


def _competing_adopt(root, source, target, identity, barrier, outcome):
    lib = plan_store.PlanLibrary(Path(root))
    record = lib.read_record(target)
    def change(state):
        state['plan'].update(plan_id=record['plan_id'], sealed_digest=record['seal']['sealed_digest'],
                             digest=record['seal']['build_plan_digest'])
    barrier.wait(timeout=10)
    try:
        build_state_store.adopt_build(lib, source, target, identity, 1, SCHEMA, change=change,
            consent={'gate': 'adopt', 'at': '2026-09-08T00:00:00Z'})
        outcome.put(('active', target))
    except core.CoordinatorError as exc:
        outcome.put(('refused', str(exc)))


def _competing_bind(library_root, slug, worktree, locator, pr, barrier, outcome):
    import build_coordinator as bc
    from test_build_coordinator import _entry_observation_fixture
    library = plan_store.PlanLibrary(Path(library_root))
    stdout, stderr = io.StringIO(), io.StringIO()
    first_observation = True
    def draft(*args):
        nonlocal first_observation
        if first_observation:
            barrier.wait(timeout=10)
            first_observation = False
        return {'headRefOid': 'e' * 40, 'baseRefOid': 'a' * 40}
    with mock.patch.object(bc.entry, 'observe_fresh', side_effect=_entry_observation_fixture), \
            mock.patch.object(bc.entry, 'verify_frozen'), \
            mock.patch.object(bc, '_library', return_value=library), \
            mock.patch.object(bc, 'ROOT', Path(worktree)), \
            mock.patch.object(bc, '_head', return_value='e' * 40), \
            mock.patch.object(bc, '_verify_draft', side_effect=draft), \
            mock.patch.object(bc, '_record_session_binding'), \
            mock.patch.object(bc.github, 'tag_coordinator_owned', return_value=True), \
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = bc.main(['--state', locator, 'plan', 'bind', '--plan', slug,
                          '--repository', 'o/r', '--pr', str(pr), '--operator-decided'])
    outcome.put((pr, result, stdout.getvalue(), stderr.getvalue()))


def _race_mutator(root, slug, identity, locator, payload, worktree, held, release, replaced, outcome):
    import build_coordinator as bc
    library = plan_store.PlanLibrary(Path(root))
    store = build_state_store.ClaimedBuildStore(library, slug, SCHEMA, 1, identity=identity)
    def change(state):
        state['progress']['current_item'] = 'preserve-the-last-writer'
        held.set()
        if not release.wait(10): raise RuntimeError('mutator not released')
    store.mutate(change)
    if not replaced.wait(15): raise RuntimeError('replacement never became active')
    stdout, stderr = io.StringIO(), io.StringIO()
    with mock.patch.object(bc, '_library', return_value=library), \
            mock.patch.object(bc, 'ROOT', Path(worktree)), \
            mock.patch.object(bc, '_head', return_value='e' * 40), \
            mock.patch.object(bc, '_is_ancestor', return_value=True), \
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        # A NEW CLI invocation with the OLD caller identity: locator, worktree and revision
        # all repeat, so only the retained Build/generation expectation can fence this writer.
        code = bc.main(['--state', locator, '--expect-build-id', identity['build_id'],
                        '--expect-generation', str(identity['generation']), '--expect-revision', '1',
                        'approve', '--plan', payload, '--depth', 'quick'])
    outcome.put(('mutator', code, stderr.getvalue()))


def _race_superseder(root, slug, identity, held, attempted, retiring, release, outcome):
    library = plan_store.PlanLibrary(Path(root))
    if not held.wait(10): raise RuntimeError('mutator never acquired the lock')
    real = library.write_build_record_locked
    def journal(slug, record):
        real(slug, record)
        claim = record['build_lease']['current']
        if claim and claim['state'] == 'retiring':
            retiring.set()
            if not release.wait(10): raise RuntimeError('retirement not released')
    attempted.set()
    with mock.patch.object(library, 'write_build_record_locked', side_effect=journal):
        archive = build_state_store.supersede(library, slug, identity=identity, expected_revision=2,
                                             schema=SCHEMA, reason='race retirement')
    outcome.put(('superseder', str(archive)))


def _race_creator(root, slug, state, locator, consent, retiring, attempted, replaced, outcome):
    library = plan_store.PlanLibrary(Path(root))
    if not retiring.wait(15): raise RuntimeError('retirement never entered its critical section')
    attempted.set()
    claim = build_state_store.reserve_build(library, slug, state, locator=locator, consent=consent)
    build_state_store.finish_binding(library, slug, build_state_store.claim_identity(claim), state, SCHEMA)
    replaced.set()
    outcome.put(('creator', claim))


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
        if kwargs.get('legacy_source'):
            kwargs.setdefault('legacy_clients_stopped', True)
        return build_state_store.reserve_build(self.lib, self.slug, self.state,
                                                consent=self.consent, **kwargs)

    def finish(self, claim):
        return build_state_store.finish_binding(self.lib, self.slug,
            build_state_store.claim_identity(claim), self.state, SCHEMA)

    def successor(self, plan_id='pln_fedcba987654'):
        from test_plan_store import _document
        import plan_contract
        doc = _document(plan_id=plan_id, title='Successor ' + plan_id)
        slug = self.lib.create(doc)
        record = self.lib.read_record(slug)
        seal = dict(self.seal, sealed_digest=record['current']['plan_digest'],
                    reviewed_digest=record['current']['plan_digest'],
                    build_plan_digest=plan_contract.build_plan_digest(doc))
        self.lib.update_record(slug, lambda r: r.update(seal=seal,
            intake={'provenance': 'test successor', 'predecessors': [self.state['plan']['plan_id']]},
            consent=[{'gate': 'seal', 'at': self.consent['at']}]))
        def change(state):
            state['plan'].update(plan_id=plan_id, sealed_digest=seal['sealed_digest'],
                                 digest=seal['build_plan_digest'])
        return slug, change

    def adopt(self, claim, slug, change):
        return build_state_store.adopt_build(self.lib, self.slug, slug,
            build_state_store.claim_identity(claim), 1, SCHEMA, change=change,
            consent={'gate': 'adopt', 'at': self.consent['at']})

    def test_adoption_moves_one_identity_preserves_evidence_and_refuses_old_writer(self):
        self.state['progress']['current_item'] = 'preserved'
        locator = self.root / 'locator.json'
        claim = self.reserve(locator=locator); self.finish(claim)
        original = Path(claim['snapshot']).read_bytes()
        slug, change = self.successor()
        saved = self.adopt(claim, slug, change)
        target = self.lib.read_record(slug)['build_lease']['current']
        predecessor = self.lib.read_record(self.slug)
        self.assertIsNone(predecessor['build_lease']['current'])
        self.assertEqual(Path(predecessor['build_lease']['history'][0]['archive']).read_bytes(), original)
        self.assertEqual(saved['ownership'], {'build_id': claim['build_id'], 'generation': 2})
        self.assertEqual(saved['progress']['current_item'], 'preserved')
        self.assertEqual(target['state'], 'active')
        self.assertEqual(core.json_file(locator)['snapshot'], target['snapshot'])
        self.assertEqual(self.adopt(claim, slug, change), saved)
        source_slug, archived = build_state_store.adoption_source(self.lib, saved['ownership'] | {'generation': 1}, SCHEMA)
        self.assertEqual(source_slug, self.slug)
        self.assertEqual(archived.read()['progress']['current_item'], 'preserved')
        stale = build_state_store.ClaimedBuildStore(self.lib, slug, SCHEMA, 2,
            identity=build_state_store.claim_identity(claim))
        with self.assertRaises(core.CoordinatorError):
            stale.mutate(lambda s: s['progress'].update(current_item='stale'))

    def test_command_entry_fences_stale_identity_and_revision(self):
        claim = self.reserve(); self.finish(claim)
        identity = build_state_store.claim_identity(claim)
        for owner, revision in ((dict(identity, generation=2), 1), (identity, 0)):
            with self.subTest(owner=owner, revision=revision):
                store = build_state_store.ClaimedBuildStore(self.lib, self.slug, SCHEMA,
                    revision, identity=owner)
                with self.assertRaises(core.CoordinatorError):
                    store.verify_mutation_entry()
        self.assertEqual(core.json_file(Path(claim['snapshot']))['revision'], 1)

    def test_own_successful_writes_advance_revision_but_competing_writes_still_fence(self):
        claim = self.reserve(); self.finish(claim)
        identity = build_state_store.claim_identity(claim)
        store = build_state_store.ClaimedBuildStore(self.lib, self.slug, SCHEMA, 1, identity=identity)
        store.mutate(lambda s: s.update(submission='ready'))
        store.mutate(lambda s: s.update(submission='draft'))
        other = build_state_store.ClaimedBuildStore(self.lib, self.slug, SCHEMA, 3, identity=identity)
        other.mutate(lambda s: s['progress'].update(current_item='another caller'))
        with self.assertRaises(core.CoordinatorError):
            store.mutate(lambda s: s.update(submission='ready'))
        saved = store.read()
        self.assertEqual(saved['submission'], 'draft')
        self.assertEqual(saved['progress']['current_item'], 'another caller')

    def test_stale_cli_cannot_reach_contract_sync_or_submit_side_effects(self):
        import build_coordinator as bc
        locator = self.root / 'locator.json'
        claim = self.reserve(locator=locator); self.finish(claim)
        self.retire(claim)
        replacement = self.reserve(locator=locator); self.finish(replacement)
        before = Path(replacement['snapshot']).read_bytes()
        verbs = [(['sync-artifacts'], 'cmd_sync_artifacts'),
                 (['submit', 'apply', '--plan', 'unused'], 'cmd_submit_apply'),
                 (['contract', 'apply', '--plan', 'unused', '--claim', 'unused',
                   '--source-body-digest', 'unused'], 'cmd_contract_apply')]
        for verb, handler in verbs:
            with self.subTest(verb=verb), mock.patch.object(bc, '_library', return_value=self.lib), \
                    mock.patch.object(bc, 'ROOT', self.root / 'worktree'), \
                    mock.patch.object(bc, handler) as side_effect, contextlib.redirect_stderr(io.StringIO()):
                result = bc.main(['--state', str(locator), '--expect-build-id', claim['build_id'],
                    '--expect-generation', str(claim['generation']), '--expect-revision', '1', *verb])
                self.assertEqual(result, 2)
                side_effect.assert_not_called()
        self.assertEqual(Path(replacement['snapshot']).read_bytes(), before)

    def test_old_adoption_retry_cannot_revive_retiring_successor(self):
        claim = self.reserve(); self.finish(claim)
        slug, change = self.successor()
        saved = self.adopt(claim, slug, change)
        store = build_state_store.ClaimedBuildStore(self.lib, slug, SCHEMA, 2,
            identity=saved['ownership'])
        store.mutate(lambda s: s['progress'].update(current_item='later progress'))
        real_replace = Path.replace
        def interrupted(source, target):
            result = real_replace(source, target)
            if source == store.path:
                raise OSError('retirement rename cut')
            return result
        with mock.patch.object(Path, 'replace', interrupted):
            with self.assertRaisesRegex(OSError, 'retirement rename cut'):
                build_state_store.retire_build(self.lib, slug, saved['ownership'], SCHEMA,
                    reason='stop successor', expected_revision=3)
        target = self.lib.read_record(slug)['build_lease']['current']
        archive = Path(target['archive']); evidence = archive.read_bytes()
        with self.assertRaisesRegex(core.CoordinatorError, 'retiring'):
            self.adopt(claim, slug, change)
        self.assertFalse(store.path.exists())
        self.assertEqual(archive.read_bytes(), evidence)
        self.assertEqual(core.json_file(archive)['progress']['current_item'], 'later progress')
        import build_coordinator as bc
        output = io.StringIO()
        with mock.patch.object(bc, '_library', return_value=self.lib), \
                mock.patch.object(bc, 'ROOT', self.root / 'worktree'), contextlib.redirect_stdout(output):
            bc.cmd_state_where(argparse.Namespace(), None)
        guidance = output.getvalue()
        self.assertNotIn('interrupted adoption', guidance)
        self.assertIn('stop successor', guidance)
        self.assertIn('"expect_revision": 3', guidance)
        self.assertIn(str(archive), guidance)
        self.assertIn('state supersede', guidance)

    def test_matching_abandon_and_retire_retry_after_visible_release(self):
        import project_manager as pm
        for closure in ('abandoned', 'retired'):
            case = TransactionalOwnership(); case.setUp()
            try:
                claim = case.reserve(); case.finish(claim)
                identity = build_state_store.claim_identity(claim)
                actual = case.lib.write_build_record_locked
                def release_cut(slug, record):
                    actual(slug, record)
                    if record['build_lease']['current'] is None:
                        raise OSError('visible release')
                with mock.patch.object(case.lib, 'write_build_record_locked', side_effect=release_cut):
                    with self.assertRaisesRegex(OSError, 'visible release'):
                        pm.close_plan_record(case.lib, case.slug, closure, 'operator stopped',
                            identity=identity, expected_revision=1)
                pm.close_plan_record(case.lib, case.slug, closure, 'operator stopped',
                    identity=identity, expected_revision=1)
                with self.assertRaises(core.CoordinatorError):
                    pm.close_plan_record(case.lib, case.slug,
                        'retired' if closure == 'abandoned' else 'abandoned', 'operator stopped',
                        identity=identity, expected_revision=1)
            finally:
                case.doCleanups()

    def test_pending_closure_retry_preserves_the_recorded_action(self):
        import project_manager as pm
        claim = self.reserve(); self.finish(claim)
        identity = build_state_store.claim_identity(claim)
        actual = core.atomic_write
        def stop_archive(path, *args, **kwargs):
            if Path(path).name.startswith('retired-'):
                raise OSError('archive boundary')
            return actual(path, *args, **kwargs)
        with mock.patch.object(core, 'atomic_write', side_effect=stop_archive):
            with self.assertRaisesRegex(OSError, 'archive boundary'):
                pm.close_plan_record(self.lib, self.slug, 'abandoned', 'operator stopped',
                                     identity=identity, expected_revision=1)
        pending = self.lib.read_record(self.slug)['build_lease']['current']
        self.assertEqual(pending['state'], 'retiring')
        self.assertEqual(pending['close_state'], 'abandoned')
        with self.assertRaisesRegex(core.CoordinatorError, 'recorded plan closure'):
            pm.close_plan_record(self.lib, self.slug, 'retired', 'operator stopped',
                                 identity=identity, expected_revision=1)
        self.assertEqual(self.lib.read_record(self.slug)['build_lease']['current'], pending)
        pm.close_plan_record(self.lib, self.slug, 'abandoned', 'operator stopped',
                             identity=identity, expected_revision=1)
        self.assertEqual(self.lib.read_record(self.slug)['closure']['state'], 'abandoned')

    def test_cli_adoption_retry_recovers_source_when_successor_closed_before_reservation(self):
        import build_coordinator as bc
        import project_manager as pm
        claim = self.reserve(); self.finish(claim)
        slug, change = self.successor()
        actual = self.lib.write_build_record_locked
        def before_target(target, record):
            if target == slug:
                raise OSError('before target reservation')
            actual(target, record)
        with mock.patch.object(self.lib, 'write_build_record_locked', side_effect=before_target):
            with self.assertRaisesRegex(OSError, 'before target reservation'):
                self.adopt(claim, slug, change)
        original = Path(claim['snapshot']).read_bytes()
        pm.close_plan_record(self.lib, slug, 'abandoned', 'withdrawn successor')
        payload = self.root / 'plan.json'
        payload.write_text(json.dumps(self.lib.head(self.slug)['build_plan']))
        with mock.patch.object(bc, '_library', return_value=self.lib), \
                mock.patch.object(bc, 'resume_reasons', return_value=[]), \
                contextlib.redirect_stderr(io.StringIO()):
            result = bc.main(['--expect-build-id', claim['build_id'], '--expect-generation', '1',
                '--expect-revision', '1', 'plan', 'adopt', '--successor', slug,
                '--input', str(payload), '--operator-decided'])
        self.assertEqual(result, 2)
        self.assertEqual(self.lib.read_record(self.slug)['build_lease']['current']['state'], 'active')
        self.assertEqual(Path(claim['snapshot']).read_bytes(), original)
        self.assertIsNone(self.lib.read_record(slug).get('build_lease'))

    def test_adoption_recovers_every_durable_write_boundary_with_original_consent(self):
        # All strict writes (records, snapshot, archive, locator) are faulted before and after
        # persistence. Rename and directory-flush recovery is independently covered by retirement.
        for after in (False, True):
            for cut in range(1, 8):
                with self.subTest(after=after, cut=cut):
                    case = TransactionalOwnership(); case.setUp()
                    try:
                        claim = case.reserve(locator=case.root / 'locator.json'); case.finish(claim)
                        slug, change = case.successor()
                        actual = core.atomic_write
                        calls = [0]
                        def fail(path, *args, **kwargs):
                            calls[0] += 1
                            hit = calls[0] == cut
                            if hit and not after: raise OSError('injected adoption cut')
                            result = actual(path, *args, **kwargs)
                            if hit: raise OSError('injected adoption cut')
                            return result
                        with mock.patch.object(core, 'atomic_write', side_effect=fail):
                            with self.assertRaisesRegex(OSError, 'injected adoption cut'):
                                case.adopt(claim, slug, change)
                        saved = case.adopt(claim, slug, change)
                        self.assertEqual(saved['ownership']['generation'], 2)
                        record = case.lib.read_record(slug)
                        self.assertEqual(record['build_lease']['current']['state'], 'active')
                        self.assertEqual(len([c for c in record['consent'] if c['gate'] == 'adopt']), 1)
                        self.assertIsNone(case.lib.read_record(case.slug)['build_lease']['current'])
                    finally:
                        case.doCleanups()

    def test_two_processes_cannot_activate_two_successors(self):
        claim = self.reserve(); self.finish(claim)
        targets = [self.successor(plan_id)[0] for plan_id in ('pln_fedcba987654', 'pln_999999999999')]
        ctx = multiprocessing.get_context('spawn')
        barrier, outcome = ctx.Barrier(2), ctx.Queue()
        processes = [ctx.Process(target=_competing_adopt, args=(str(self.lib.root), self.slug,
            target, build_state_store.claim_identity(claim), barrier, outcome)) for target in targets]
        try:
            for process in processes: process.start()
            for process in processes:
                process.join(15)
                self.assertFalse(process.is_alive(), 'adoption deadlocked')
                self.assertEqual(process.exitcode, 0)
            results = [outcome.get(timeout=3)[0] for _ in processes]
            self.assertEqual(sorted(results), ['active', 'refused'])
            active = [self.lib.read_record(t).get('build_lease', {}).get('current') for t in targets]
            self.assertEqual(sum(bool(c and c['state'] == 'active') for c in active), 1)
            self.assertIsNone(self.lib.read_record(self.slug)['build_lease']['current'])
        finally:
            for process in processes:
                if process.is_alive(): process.terminate(); process.join(3)
            outcome.close(); outcome.join_thread()

    def test_transfer_preparation_cannot_be_finished_or_retired_as_an_ordinary_bind(self):
        claim = self.reserve(); self.finish(claim)
        slug, change = self.successor()
        actual = build_state_store._durable_json
        def stop_snapshot(path, value):
            if Path(path).name == 'snapshot.json': raise OSError('snapshot cut')
            return actual(path, value)
        with mock.patch.object(build_state_store, '_durable_json', side_effect=stop_snapshot):
            with self.assertRaisesRegex(OSError, 'snapshot cut'): self.adopt(claim, slug, change)
        target = self.lib.read_record(slug)['build_lease']['current']
        identity = build_state_store.claim_identity(target)
        state = json.loads(json.dumps(self.state)); change(state)
        with self.assertRaisesRegex(core.CoordinatorError, 'adoption'):
            build_state_store.finish_binding(self.lib, slug, identity, state, SCHEMA)
        with self.assertRaisesRegex(core.CoordinatorError, 'adoption'):
            build_state_store.retire_build(self.lib, slug, identity, SCHEMA, reason='cancel', expected_revision=0)
        self.assertEqual(self.adopt(claim, slug, change)['ownership'], identity)

    def test_completion_matches_every_identity_field_and_is_idempotent(self):
        import project_manager as pm
        claim = self.reserve(); self.finish(claim)
        identity = build_state_store.claim_identity(claim)
        proof = {k: claim[k] for k in ('build_id', 'generation', 'snapshot', 'repository',
                                      'pull_request', 'sealed_digest')}
        proof['merged'] = True
        original = Path(claim['snapshot']).read_bytes()
        for field in proof:
            wrong = dict(proof); wrong[field] = False if field == 'merged' else 'wrong'
            with self.subTest(field=field), self.assertRaisesRegex(core.CoordinatorError, 'completion requires'):
                pm.close_plan_record(self.lib, self.slug, 'complete', 'merged', identity=identity,
                                     expected_revision=1, completion=wrong)
            self.assertEqual(Path(claim['snapshot']).read_bytes(), original)
        for _ in range(2):
            pm.close_plan_record(self.lib, self.slug, 'complete', 'merged', identity=identity,
                                 expected_revision=1, completion=proof)
        record = self.lib.read_record(self.slug)
        self.assertEqual(record['closure']['state'], 'complete')
        self.assertEqual(len(record['build_lease']['history']), 1)
        self.assertEqual(Path(record['build_lease']['history'][0]['archive']).read_bytes(), original)
        with self.assertRaises(core.CoordinatorError): self.reserve()

    def _check_active_bind_has_no_new_admission(self, with_admission):
        import build_coordinator as bc
        import plan_lifecycle
        if with_admission:
            self.state['admission'] = self._entry_observation()
        claim = self.reserve(); self.finish(claim)
        snapshot = Path(claim['snapshot'])
        before = snapshot.read_bytes()
        record = (self.lib.plan_dir(self.slug) / 'record.json').read_bytes()
        args = argparse.Namespace(plan=self.state['plan']['plan_id'], mode='same-session',
            repository=self.state['build']['repository'], pr=self.state['build']['pr'],
            issue=None, operator_decided=False)
        with mock.patch.object(bc, 'ROOT', Path(self.state['build']['worktree'])), \
                mock.patch.object(bc, '_library', return_value=self.lib), \
                mock.patch.object(bc, '_head', return_value=self.state['plan']['bound_head']), \
                mock.patch.object(bc, '_is_ancestor', return_value=True), \
                mock.patch.object(bc, '_record_session_binding'), \
                mock.patch.object(bc, '_verify_draft', side_effect=AssertionError('must not observe PR')), \
                mock.patch.object(bc.entry, 'observe_fresh', side_effect=AssertionError('must not admit again')), \
                mock.patch.object(plan_lifecycle, 'attestation', side_effect=AssertionError('must not mint consent')), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            stale_callers = [
                {'expect_build_id': 'bld_' + 'f' * 32, 'expect_generation': claim['generation']},
                {'expect_build_id': claim['build_id'], 'expect_generation': claim['generation'] + 1},
                {'expect_build_id': claim['build_id'], 'expect_generation': claim['generation'],
                 'expect_revision': self.state['revision'] + 1},
            ]
            for stale in stale_callers:
                with self.subTest(stale=stale), self.assertRaisesRegex(core.CoordinatorError, 'stale Build'):
                    bc.cmd_plan_bind(argparse.Namespace(**vars(args), **stale), None)
                self.assertEqual(snapshot.read_bytes(), before)
                self.assertEqual((self.lib.plan_dir(self.slug) / 'record.json').read_bytes(), record)
            bc.cmd_plan_bind(args, None)
        result = json.loads(out.getvalue())
        self.assertTrue(result['continuation'])
        self.assertEqual(result['admission'], 'original' if with_admission else 'legacy-unverified')
        self.assertEqual(snapshot.read_bytes(), before)
        self.assertEqual((self.lib.plan_dir(self.slug) / 'record.json').read_bytes(), record)

    def test_active_bind_retains_original_admission_without_network_or_consent(self):
        self._check_active_bind_has_no_new_admission(True)

    def test_legacy_active_bind_continues_without_claiming_new_freshness(self):
        self._check_active_bind_has_no_new_admission(False)

    def test_preparing_without_admission_refuses_before_network_and_preserves_claim(self):
        import build_coordinator as bc
        self.reserve()
        record_path = self.lib.plan_dir(self.slug) / 'record.json'
        before = record_path.read_bytes()
        args = argparse.Namespace(plan=self.state['plan']['plan_id'], mode='same-session',
            repository=self.state['build']['repository'], pr=self.state['build']['pr'], issue=None)
        with mock.patch.object(bc, '_library', return_value=self.lib), \
                mock.patch.object(bc, '_verify_draft', side_effect=AssertionError('must not observe PR')), \
                self.assertRaisesRegex(core.CoordinatorError, 'no frozen admission'):
            bc.cmd_plan_bind(args, None)
        self.assertEqual(record_path.read_bytes(), before)

    def _entry_observation(self):
        return {'observed_at': '2026-09-08T00:00:00Z', 'material': {
            'repository': self.state['build']['repository'], 'pr': self.state['build']['pr'],
            'head_repository': self.state['build']['repository'], 'head_ref': 'codex/build',
            'head': self.state['plan']['bound_head'],
            'target_repository': self.state['build']['repository'], 'target_ref': 'main',
            'target_tip': self.state['build']['base_at_bind'], 'issues': [17],
            'overlap': {'coverage': 'complete', 'matches': [], 'errors': [],
                        'local_digest': 'sha256:' + '1' * 64}, 'override': None}}

    def _check_entry_retry(self, after_snapshot):
        self.state['admission'] = self._entry_observation()
        original = json.loads(json.dumps(self.state['admission']))
        claim = self.reserve()
        identity = build_state_store.claim_identity(claim)
        if after_snapshot:
            with mock.patch.object(build_state_store, '_cutover_locked', side_effect=OSError('after snapshot')):
                with self.assertRaisesRegex(OSError, 'after snapshot'):
                    self.finish(claim)
        snapshot = Path(claim['snapshot'])
        record_path = self.lib.plan_dir(self.slug) / 'record.json'
        before_record = record_path.read_bytes()
        before_snapshot = snapshot.read_bytes() if snapshot.exists() else None
        variants = {
            'head': 'f' * 40, 'target_tip': 'f' * 40, 'issues': [18],
            'overlap': {'coverage': 'complete', 'matches': ['another build'], 'errors': [],
                        'local_digest': 'sha256:' + '2' * 64},
            'override': {'observation_digest': 'sha256:' + '3' * 64, 'reason': 'changed exception'},
        }
        for field, changed in variants.items():
            with self.subTest(after_snapshot=after_snapshot, field=field):
                candidate = json.loads(json.dumps(self.state))
                candidate['admission']['material'][field] = changed
                candidate['admission']['observed_at'] = '2026-09-09T00:00:00Z'
                with self.assertRaises(core.CoordinatorError):
                    build_state_store.reserve_build(self.lib, self.slug, candidate, consent=self.consent)
                with self.assertRaisesRegex(core.CoordinatorError, 'admission changed'):
                    build_state_store.finish_binding(self.lib, self.slug, identity, candidate, SCHEMA)
                self.assertEqual(record_path.read_bytes(), before_record)
                self.assertEqual(snapshot.read_bytes() if snapshot.exists() else None, before_snapshot)
        self.state['admission']['observed_at'] = '2026-09-10T00:00:00Z'
        retried = self.reserve()
        self.assertEqual(retried['admission'], original)
        self.assertEqual(retried['admission_digest'], core.digest(original['material']))
        saved = self.finish(retried)
        self.assertEqual(saved['admission'], original)
        self.assertEqual(saved['ownership'], identity)
        current = self.lib.read_record(self.slug)['build_lease']['current']
        self.assertEqual(current['state'], 'active')
        self.assertEqual(current['admission'], original)

    def test_entry_material_cannot_change_after_reservation_and_retry_keeps_original_time(self):
        self._check_entry_retry(after_snapshot=False)

    def test_entry_material_cannot_change_after_snapshot_write_and_retry_keeps_original_time(self):
        self._check_entry_retry(after_snapshot=True)

    def test_entry_local_validation_refusals_do_not_reserve_or_activate(self):
        self.state['admission'] = self._entry_observation()
        record_path = self.lib.plan_dir(self.slug) / 'record.json'
        before = record_path.read_bytes()
        def refuse():
            raise core.CoordinatorError('local admission inputs moved')
        with self.assertRaisesRegex(core.CoordinatorError, 'inputs moved'):
            self.reserve(validate_entry=refuse)
        self.assertEqual(record_path.read_bytes(), before)
        claim = self.reserve()
        reserved = record_path.read_bytes()
        with self.assertRaisesRegex(core.CoordinatorError, 'inputs moved'):
            build_state_store.finish_binding(self.lib, self.slug,
                build_state_store.claim_identity(claim), self.state, SCHEMA, validate_entry=refuse)
        self.assertEqual(record_path.read_bytes(), reserved)
        self.assertFalse(Path(claim['snapshot']).exists())

    def test_handoff_restores_only_the_live_generation_at_its_canonical_address(self):
        import build_coordinator as bc
        from test_build_coordinator import TestPreflightHandoffAndSubmission
        self.state['findings'] = [TestPreflightHandoffAndSubmission()._blocking_finding_with_private('private retained note')]
        claim = self.reserve(); saved = self.finish(claim)
        value = bc._handoff(saved); value['snapshot'] = claim['snapshot']
        restored = bc._restore_base_state(value, 'build-state.v2'); restored['work'] = {}
        def restore(export):
            return build_state_store.restore_handoff(self.lib, self.slug, export, restored, SCHEMA,
                worktree=self.state['build']['worktree'], projection=bc._handoff)
        wrong = dict(value, snapshot=str(self.root / 'former.json'))
        with self.assertRaisesRegex(core.CoordinatorError, 'former or different'): restore(wrong)
        updated = restore(value)
        self.assertNotIn('private retained note', json.dumps(value))
        self.assertEqual(updated['findings'][0]['private_reference'], 'private retained note')
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(updated['ownership'], saved['ownership'])
        with self.assertRaises(core.CoordinatorError): restore(value)
        self.retire(claim, revision=2)
        with self.assertRaises(core.CoordinatorError): restore(value)
        self.assertFalse(Path(claim['snapshot']).exists())

    def test_handoff_progress_validator_reads_private_canonical_recovery_and_refuses_atomically(self):
        import build_coordinator as bc
        claim = self.reserve(); saved = self.finish(claim)
        identity = build_state_store.claim_identity(claim)
        store = build_state_store.ClaimedBuildStore(self.lib, self.slug, SCHEMA, identity=identity)
        preparation = {"id": "sha256:" + "1" * 64, "source_head": "a" * 40,
            "source_base": "b" * 40, "target_ref": "main", "target_tip": "c" * 40,
            "prepared_revision": 1, "identity": {"ownership": identity,
                "plan_digest": saved["plan"]["digest"], "repository": saved["build"]["repository"],
                "pr": saved["build"]["pr"], "worktree": saved["build"]["worktree"], "branch": "codex/build"}}
        event = {"preparation": preparation, "to_commit": "d" * 40, "divergent_paths": [],
                 "invalidated_nodes": [], "prior_work": {},
                 "prior_progress": {"current_item": None, "completed": []}}
        store.mutate(lambda s: s.update(rewrite_recoveries=[event]))
        saved = store.read()
        value = bc._handoff(saved); value['snapshot'] = claim['snapshot']
        self.assertNotIn('rewrite_recoveries', value)
        restored = bc._restore_base_state(value, 'build-state.v2'); restored['work'] = {}
        before_snapshot = Path(claim['snapshot']).read_bytes()
        before_record = (self.lib.plan_dir(self.slug) / 'record.json').read_bytes()
        observed = []
        def inspect_canonical(current):
            observed.append(current['ownership'])
            self.assertEqual(current['rewrite_recoveries'], [event])
            self.assertEqual(current['revision'], saved['revision'])
        def refuse(current):
            inspect_canonical(current)
            raise core.CoordinatorError('original recovery proof cannot be re-derived')
        def restore(validator):
            return build_state_store.restore_handoff(self.lib, self.slug, value, restored, SCHEMA,
                worktree=self.state['build']['worktree'], projection=bc._handoff,
                validate_progress=validator)
        with self.assertRaisesRegex(core.CoordinatorError, 'cannot be re-derived'):
            restore(refuse)
        self.assertEqual(Path(claim['snapshot']).read_bytes(), before_snapshot)
        self.assertEqual((self.lib.plan_dir(self.slug) / 'record.json').read_bytes(), before_record)
        updated = restore(inspect_canonical)
        self.assertEqual(observed, [identity, identity])
        self.assertEqual(updated['ownership'], identity)
        self.assertEqual(updated['revision'], saved['revision'] + 1)
        self.assertEqual(updated['rewrite_recoveries'], [event])

    def test_handoff_export_refuses_managed_or_existing_destinations_without_replacement(self):
        import build_coordinator as bc
        locator = self.root / 'locator.json'
        claim = self.reserve(locator=locator); self.finish(claim)
        store = build_state_store.ClaimedBuildStore(self.lib, self.slug, SCHEMA,
            identity=build_state_store.claim_identity(claim))
        snapshot = Path(claim['snapshot'])
        archive = snapshot.parent / 'retained.json'; archive.write_text('retained evidence')
        alias = self.root / 'alias.json'; alias.symlink_to(snapshot)
        external = self.root / 'existing.json'; external.write_text('unrelated evidence')
        paths = [snapshot, archive, self.lib.plan_dir(self.slug) / 'record.json', locator,
                 Path(str(snapshot) + '.lock'), build_state_store._legacy_lock(self.lib, self.slug),
                 alias, external, snapshot.parent / 'unused.json']
        before = {path: (path.read_bytes(), path.stat().st_ino) for path in paths if path.exists()}
        with mock.patch.object(bc, '_sealed_plan', return_value=(self.state['plan']['plan_id'],
                self.seal['sealed_digest'], self.lib.head(self.slug)['build_plan'])), \
                mock.patch.object(bc, '_assert_spec_boundary'), contextlib.redirect_stdout(io.StringIO()):
            for path in paths:
                with self.subTest(path=path), self.assertRaisesRegex(core.CoordinatorError, 'new|exists'):
                    bc.cmd_handoff_export(argparse.Namespace(output=str(path)), store)
            out = self.root / 'new-export.json'
            bc.cmd_handoff_export(argparse.Namespace(output=str(out)), store)
        for path, evidence in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_ino), evidence)
        self.assertFalse((snapshot.parent / 'unused.json').exists())
        self.assertEqual(core.json_file(out)['schema_version'], 'build-handoff.v2')
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)

    def test_private_export_publication_cannot_replace_a_concurrent_creator(self):
        output = self.root / 'raced.json'
        actual = os.link
        def competing_link(source, target):
            output.write_text('other creator won')
            return actual(source, target)
        with mock.patch.object(os, 'link', side_effect=competing_link):
            with self.assertRaisesRegex(core.CoordinatorError, 'already exists'):
                core.write_private_path(output, 'export', replace=False)
        self.assertEqual(output.read_text(), 'other creator won')
        self.assertEqual(list(self.root.glob('raced.json.*')), [])

    def test_handoff_restore_preserves_private_integration_verification(self):
        import build_coordinator as bc
        self.state['work'] = {'N1': {'attempt_count': 1, 'claim': None, 'latest_result': None,
            'latest_failure': None, 'integration': {'attempt_id': '0' * 32, 'commit': 'a' * 40,
                'focused_verification': 'private verification evidence', 'restored': False}}}
        claim = self.reserve(); saved = self.finish(claim)
        value = bc._handoff(saved); value['snapshot'] = claim['snapshot']
        restored = bc._restore_base_state(value, 'build-state.v2')
        restored['work'] = bc._restore_work(value['work'])
        self.assertNotIn('private verification evidence', json.dumps(value))
        updated = build_state_store.restore_handoff(self.lib, self.slug, value, restored, SCHEMA,
            worktree=self.state['build']['worktree'], projection=bc._handoff)
        self.assertEqual(updated['work']['N1']['integration']['focused_verification'],
                         'private verification evidence')
        self.assertTrue(updated['work']['N1']['integration']['restored'])

    def test_adoption_recreates_missing_locator_from_verified_transfer_on_retry(self):
        locator = self.root / 'locator.json'
        claim = self.reserve(locator=locator); self.finish(claim)
        before = Path(claim['snapshot']).read_bytes()
        slug, change = self.successor()
        actual = build_state_store._durable_json
        def cut_locator(path, value):
            if Path(path).resolve() == locator.resolve(): raise OSError('locator publication cut')
            return actual(path, value)
        with mock.patch.object(build_state_store, '_durable_json', side_effect=cut_locator):
            with self.assertRaisesRegex(OSError, 'locator publication cut'):
                self.adopt(claim, slug, change)
        locator.unlink()
        saved = self.adopt(claim, slug, change)
        self.assertEqual(core.json_file(locator)['ownership'], saved['ownership'])
        self.assertEqual(locator.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.adopt(claim, slug, change), saved)
        history = self.lib.read_record(self.slug)['build_lease']['history']
        self.assertEqual(Path(history[-1]['archive']).read_bytes(), before)

    def test_cli_migrates_recorded_operator_revision_without_replacing_historical_seal(self):
        import build_coordinator as bc
        source = self.root / 'legacy.json'
        legacy = build_state_store.DurableBuildStore(source, SCHEMA)
        legacy.create(self.state)
        self.lib.update_record(self.slug, lambda r: r.update(build_binding={
            'sealed_digest': self.seal['sealed_digest'], 'build_plan_digest': self.seal['build_plan_digest'],
            'repository': 'o/r', 'pull_request': 1, 'at': self.consent['at']}))
        plan = self.lib.head(self.slug)['build_plan']; plan['objective'] += ' with authorized correction'
        payload = self.root / 'revised.json'; payload.write_text(json.dumps(plan))
        with mock.patch.object(bc, '_head', return_value='e' * 40), \
                mock.patch.object(bc, '_read_now'), contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_plan_revise(argparse.Namespace(input=str(payload), operator_change='Operator directed this revision'), legacy)
        revised = legacy.read(); original = source.read_bytes()
        for defect in ('missing', 'unconnected', 'empty_authority', 'flag', 'contradictory_tail', 'empty_tail'):
            bad = json.loads(json.dumps(revised))
            if defect == 'missing': bad['plan_change_escalations'] = []
            elif defect == 'unconnected': bad['plan_change_escalations'][0]['reviewed_plan_digest'] = 'sha256:' + 'f' * 64
            elif defect == 'empty_authority': bad['plan_change_escalations'][0]['operator_change'] = ''
            elif defect == 'flag': bad['plan']['diverged_from_seal'] = False
            else:
                bad['plan_change_escalations'].append({
                    'reviewed_plan_digest': 'sha256:' + 'f' * 64 if defect == 'contradictory_tail' else bad['plan']['digest'],
                    'plan_digest': 'sha256:' + 'e' * 64,
                    'operator_change': 'contradictory later entry' if defect == 'contradictory_tail' else ''})
            source.write_text(json.dumps(bad))
            with self.subTest(defect=defect), self.assertRaisesRegex(core.CoordinatorError, 'sealed plan|revision chain'):
                build_state_store.reserve_build(self.lib, self.slug, bad,
                    legacy_source=source, legacy_clients_stopped=True)
            self.assertIsNone(self.lib.read_record(self.slug).get('build_lease'))
        source.write_bytes(original)
        outputs = []
        with mock.patch.object(bc, '_library', return_value=self.lib), \
                mock.patch.object(bc, 'ROOT', Path(self.state['build']['worktree'])):
            for _ in range(2):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(bc.main(['state', 'migrate', '--legacy-clients-stopped',
                        '--source', str(source), '--plan', self.slug]), 0)
                outputs.append(json.loads(out.getvalue()))
        self.assertEqual(outputs[0], outputs[1])
        saved = core.json_file(Path(outputs[0]['migrated']))
        self.assertEqual(saved['plan'], revised['plan'])
        self.assertEqual(saved['plan_change_escalations'], revised['plan_change_escalations'])
        self.assertEqual(self.lib.read_record(self.slug)['seal'], self.seal)
        self.assertEqual(source.read_bytes(), original)

    def test_preparing_discovery_prints_recorded_retry_inputs_and_revision_zero(self):
        import build_coordinator as bc
        locator = self.root / 'locator.json'
        claim = self.reserve(locator=locator)
        out = io.StringIO()
        with mock.patch.object(bc, '_library', return_value=self.lib), \
                mock.patch.object(bc, 'ROOT', Path(self.state['build']['worktree'])), \
                contextlib.redirect_stdout(out):
            bc.cmd_state_where(argparse.Namespace(), None)
        text = out.getvalue()
        for value in (claim['snapshot'], str(locator), '"expect_revision": 0', '"repository": "o/r"', '"pr": 1'):
            self.assertIn(value, text)

    def test_export_refuses_existing_and_concurrently_created_dangling_symlinks(self):
        import build_coordinator as bc
        claim = self.reserve(); self.finish(claim)
        store = build_state_store.ClaimedBuildStore(self.lib, self.slug, SCHEMA,
            identity=build_state_store.claim_identity(claim))
        target = Path(claim['snapshot']).parent / 'must-not-be-created.json'
        output = self.root / 'export.json'
        actual = core.write_private_path
        def redirect(path, rendered, **kwargs):
            output.symlink_to(target)
            return actual(path, rendered, **kwargs)
        with mock.patch.object(bc, '_sealed_plan', return_value=(self.state['plan']['plan_id'],
                self.seal['sealed_digest'], self.lib.head(self.slug)['build_plan'])), \
                mock.patch.object(bc, '_assert_spec_boundary'):
            output.symlink_to(target)
            with self.assertRaisesRegex(core.CoordinatorError, 'already exists'):
                bc.cmd_handoff_export(argparse.Namespace(output=str(output)), store)
            self.assertTrue(output.is_symlink()); self.assertFalse(target.exists())
            output.unlink()
            with mock.patch.object(core, 'write_private_path', side_effect=redirect):
                with self.assertRaisesRegex(core.CoordinatorError, 'already exists'):
                    bc.cmd_handoff_export(argparse.Namespace(output=str(output)), store)
            self.assertTrue(output.is_symlink()); self.assertFalse(target.exists())

    def test_completed_adoption_retry_repairs_missing_and_refuses_foreign_locator(self):
        locator = self.root / 'locator.json'
        claim = self.reserve(locator=locator); self.finish(claim)
        slug, change = self.successor()
        actual = self.lib.write_build_record_locked
        def visible_activation(target, record):
            actual(target, record)
            current = (record.get('build_lease') or {}).get('current')
            if target == slug and current and current['state'] == 'active':
                raise OSError('visible activation before flush')
        with mock.patch.object(self.lib, 'write_build_record_locked', side_effect=visible_activation):
            with self.assertRaisesRegex(OSError, 'visible activation before flush'):
                self.adopt(claim, slug, change)
        locator.unlink()
        saved = self.adopt(claim, slug, change)
        self.assertEqual(core.json_file(locator)['ownership'], saved['ownership'])
        before = Path(self.lib.read_record(slug)['build_lease']['current']['snapshot']).read_bytes()
        foreign = core.json_file(locator); foreign['ownership']['build_id'] = 'bld_' + 'f' * 32
        locator.write_text(json.dumps(foreign)); locator.chmod(0o600)
        with self.assertRaisesRegex(core.CoordinatorError, 'another owner'):
            self.adopt(claim, slug, change)
        self.assertEqual(core.json_file(locator), foreign)
        self.assertEqual(Path(self.lib.read_record(slug)['build_lease']['current']['snapshot']).read_bytes(), before)

    def test_unattended_reservation_only_cli_retry_keeps_mode_and_issue(self):
        import build_coordinator as bc
        from test_build_coordinator import _entry_observation_fixture
        locator = self.root / 'locator.json'
        common = ['--state', str(locator), 'plan', 'bind', '--plan', self.slug,
                  '--repository', 'o/r', '--pr', '1', '--operator-decided']
        original = common + ['--mode', 'unattended', '--issue', '41']
        with mock.patch.object(bc.entry, 'observe_fresh', side_effect=_entry_observation_fixture), \
                mock.patch.object(bc.entry, 'verify_frozen'), \
                mock.patch.object(bc, '_library', return_value=self.lib), \
                mock.patch.object(bc, 'ROOT', Path(self.state['build']['worktree'])), \
                mock.patch.object(bc, '_head', return_value='e' * 40), \
                mock.patch.object(bc, '_verify_draft', return_value={'headRefOid': 'e' * 40, 'baseRefOid': 'a' * 40}), \
                mock.patch.object(bc, '_check_authorization'), \
                mock.patch.object(bc, '_record_session_binding'), \
                mock.patch.object(bc.github, 'tag_coordinator_owned', return_value=True), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(build_state_store, 'finish_binding', side_effect=OSError('reservation only')):
                self.assertEqual(bc.main(original), 2)
            claim = self.lib.read_record(self.slug)['build_lease']['current']
            self.assertFalse(Path(claim['snapshot']).exists())
            self.assertEqual((claim['mode'], claim['authorizing_issue']), ('unattended', 41))
            out = io.StringIO()
            with contextlib.redirect_stdout(out): bc.cmd_state_where(argparse.Namespace(), None)
            self.assertIn('"mode": "unattended"', out.getvalue())
            self.assertIn('"issue": 41', out.getvalue())
            self.assertEqual(bc.main(common), 2)
            self.assertEqual(bc.main(common + ['--mode', 'unattended', '--issue', '42']), 2)
            self.assertEqual(self.lib.read_record(self.slug)['build_lease']['current'], claim)
            self.assertEqual(bc.main(original), 0)
        saved = core.json_file(Path(claim['snapshot']))
        self.assertEqual(saved['ownership'], build_state_store.claim_identity(claim))
        self.assertEqual((saved['build']['mode'], saved['plan']['authorizing_issue']), ('unattended', 41))
        self.assertEqual(saved['admission'], claim['admission'])
        self.assertEqual(sum(c['gate'] == 'bind' for c in self.lib.read_record(self.slug)['consent']), 1)

    def test_single_plan_operations_take_snapshot_locks_in_the_declared_path_order(self):
        claim = self.reserve()
        actual = core.exclusive_lock
        seen = []
        @contextlib.contextmanager
        def observe(path):
            seen.append(Path(path))
            with actual(path): yield
        with mock.patch.object(core, 'exclusive_lock', side_effect=observe):
            self.finish(claim)
        expected = [self.lib.plan_dir(self.slug) / 'record.json.lock'] + sorted([
            build_state_store._legacy_lock(self.lib, self.slug), Path(claim['snapshot'] + '.lock')])
        self.assertEqual(seen, expected)
        seen.clear()
        with mock.patch.object(core, 'exclusive_lock', side_effect=observe):
            self.retire(claim)
        self.assertEqual(seen, expected)

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

    def retire(self, claim, *, reason='confirmed stale', revision=1):
        return build_state_store.supersede(self.lib, self.slug, reason=reason,
            identity=build_state_store.claim_identity(claim), expected_revision=revision, schema=SCHEMA)

    def test_retirement_keeps_evidence_reason_and_both_permanent_lock_inodes(self):
        claim = self.reserve(); self.finish(claim)
        path = Path(claim['snapshot']); original = path.read_bytes()
        locks = [build_state_store._legacy_lock(self.lib, self.slug), path.with_name(path.name + '.lock')]
        inodes = [p.stat().st_ino for p in locks]
        archive = self.retire(claim)
        self.assertEqual(archive.read_bytes(), original)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertFalse(path.exists())
        self.assertEqual([p.stat().st_ino for p in locks], inodes)
        self.assertEqual(build_state_store.bound_snapshots(self.state['build']['worktree'], library=self.lib), [])
        record = self.lib.read_record(self.slug)
        self.assertIsNone(record['build_binding'])
        self.assertIsNone(record['build_lease']['current'])
        self.assertEqual(record['build_lease']['history'][0]['reason'], 'confirmed stale')
        self.assertEqual(self.retire(claim), archive)

    def test_equal_revisions_in_successive_generations_keep_both_archives(self):
        first = self.reserve(); self.finish(first); a = self.retire(first)
        before = a.read_bytes()
        self.state['build']['pr'] = 2
        second = self.reserve(); self.finish(second); b = self.retire(second)
        self.assertEqual(second['generation'], first['generation'] + 1)
        self.assertNotEqual(second['build_id'], first['build_id'])
        self.assertNotEqual(a, b)
        self.assertEqual(a.read_bytes(), before)
        self.assertEqual(core.json_file(b)['build']['pr'], 2)
        self.assertEqual(len(self.lib.read_record(self.slug)['build_lease']['history']), 2)

    def test_unwritten_reservation_can_be_explicitly_retired_and_rebound(self):
        claim = self.reserve()
        archive = self.retire(claim, revision=0)
        self.assertTrue(core.json_file(archive)['unwritten_preparation'])
        replacement = self.reserve(); self.finish(replacement)
        self.assertEqual(replacement['generation'], 2)
        with self.assertRaisesRegex(core.CoordinatorError, 'identity'):
            self.retire(claim, revision=0)

    def test_retirement_faults_are_retryable_without_losing_evidence(self):
        stages = ['journal-before', 'journal-after', 'archive-before', 'archive-after',
                  'rename-before', 'rename-after', 'rename-flush', 'release-before', 'release-after']
        for stage in stages:
            with self.subTest(stage=stage):
                f = TransactionalOwnership(); f.setUp()
                try:
                    claim = f.reserve(); f.finish(claim)
                    path = Path(claim['snapshot']); before = path.read_bytes()
                    real_record = f.lib.write_build_record_locked
                    real_write, real_replace, real_flush = core.atomic_write, Path.replace, core.fsync_dir
                    fired = []
                    def record_write(slug, record):
                        current = record['build_lease']['current']
                        kind = 'journal' if current else 'release'
                        if stage == kind + '-before' and not fired:
                            fired.append(True); raise OSError(stage)
                        real_record(slug, record)
                        if stage == kind + '-after' and not fired:
                            fired.append(True); raise OSError(stage)
                    def write(target, text, **kwargs):
                        is_archive = target.name.startswith('retired-')
                        if is_archive and stage == 'archive-before' and not fired:
                            fired.append(True); raise OSError(stage)
                        real_write(target, text, **kwargs)
                        if is_archive and stage == 'archive-after' and not fired:
                            fired.append(True); raise OSError(stage)
                    def replace(source, target):
                        if source == path and stage == 'rename-before' and not fired:
                            fired.append(True); raise OSError(18, 'cross-device link')
                        result = real_replace(source, target)
                        if source == path and stage == 'rename-after' and not fired:
                            fired.append(True); raise OSError(stage)
                        return result
                    def flush(directory):
                        if stage == 'rename-flush' and directory == path.parent and not path.exists() and not fired:
                            fired.append(True); return False
                        return real_flush(directory)
                    with mock.patch.object(f.lib, 'write_build_record_locked', side_effect=record_write), \
                            mock.patch.object(core, 'atomic_write', side_effect=write), \
                            mock.patch.object(Path, 'replace', replace), \
                            mock.patch.object(core, 'fsync_dir', side_effect=flush):
                        with self.assertRaises((OSError, core.CoordinatorError)):
                            f.retire(claim)
                    self.assertTrue(fired, 'fault boundary was not reached')
                    if stage == 'rename-before': self.assertEqual(path.read_bytes(), before)
                    archive = f.retire(claim)
                    self.assertEqual(archive.read_bytes(), before)
                    self.assertFalse(path.exists())
                    self.assertIsNone(f.lib.read_record(f.slug)['build_lease']['current'])
                finally:
                    f.doCleanups()

    def test_three_process_retirement_serializes_last_writer_and_fences_its_next_cli(self):
        locator = str(self.root / 'same-locator.json')
        claim = self.reserve(locator=locator); self.finish(claim)
        identity = build_state_store.claim_identity(claim)
        old_path = Path(claim['snapshot'])
        locks = [build_state_store._legacy_lock(self.lib, self.slug), old_path.with_name(old_path.name + '.lock')]
        inodes = [p.stat().st_ino for p in locks]
        payload = self.root / 'payload.json'
        payload.write_text(json.dumps(self.lib.head(self.slug)['build_plan']))
        ctx = multiprocessing.get_context('spawn')
        held, release_mutator, attempted_retire = ctx.Event(), ctx.Event(), ctx.Event()
        retiring, attempted_create, release_retire, replaced = ctx.Event(), ctx.Event(), ctx.Event(), ctx.Event()
        outcome = ctx.Queue()
        children = [
            ctx.Process(target=_race_mutator, args=(str(self.lib.root), self.slug, identity, locator,
                str(payload), self.state['build']['worktree'], held, release_mutator, replaced, outcome)),
            ctx.Process(target=_race_superseder, args=(str(self.lib.root), self.slug, identity, held,
                attempted_retire, retiring, release_retire, outcome)),
            ctx.Process(target=_race_creator, args=(str(self.lib.root), self.slug, self.state, locator,
                self.consent, retiring, attempted_create, replaced, outcome))]
        try:
            for child in children: child.start()
            self.assertTrue(held.wait(10))
            self.assertTrue(attempted_retire.wait(10))
            release_mutator.set()
            self.assertTrue(retiring.wait(10))
            self.assertTrue(attempted_create.wait(10))
            release_retire.set()
            for child in children:
                child.join(20)
                self.assertFalse(child.is_alive(), 'retirement race deadlocked')
                self.assertEqual(child.exitcode, 0)
            results = {r[0]: r[1:] for r in [outcome.get(timeout=2) for _ in children]}
            self.assertEqual(results['mutator'][0], 2)
            self.assertIn('stale or missing Build identity', results['mutator'][1])
            archive = Path(results['superseder'][0]); prior = core.json_file(archive)
            self.assertEqual(prior['revision'], 2)
            self.assertEqual(prior['progress']['current_item'], 'preserve-the-last-writer')
            new_claim = results['creator'][0]
            self.assertEqual(new_claim['generation'], 2)
            current = core.json_file(Path(new_claim['snapshot']))
            self.assertEqual(current['revision'], 1)
            self.assertIsNone(current['progress']['current_item'])
            self.assertEqual(current['ownership'], build_state_store.claim_identity(new_claim))
            self.assertEqual([p.stat().st_ino for p in locks], inodes)
            self.assertFalse(old_path.exists())
            record = self.lib.read_record(self.slug)
            self.assertEqual(record['build_lease']['current']['build_id'], new_claim['build_id'])
            self.assertEqual(len(record['build_lease']['history']), 1)
        finally:
            release_mutator.set(); release_retire.set(); replaced.set()
            for child in children:
                if child.is_alive(): child.terminate(); child.join(5)
            outcome.close(); outcome.join_thread()

    def test_two_process_binds_to_distinct_explicit_paths_reserve_only_one_build(self):
        ctx = multiprocessing.get_context('spawn')
        barrier, outcome = ctx.Barrier(2), ctx.Queue()
        children = [ctx.Process(target=_competing_bind, args=(str(self.lib.root), self.slug,
            self.state['build']['worktree'], str(self.root / f'locator-{pr}.json'), pr,
            barrier, outcome)) for pr in (1, 2)]
        try:
            for child in children: child.start()
            for child in children:
                child.join(15)
                self.assertFalse(child.is_alive(), 'bind race deadlocked')
                self.assertEqual(child.exitcode, 0)
            results = [outcome.get(timeout=2) for _ in children]
            self.assertEqual(sorted(r[1] for r in results), [0, 2], results)
            winner = next(r for r in results if r[1] == 0)
            loser = next(r for r in results if r[1] == 2)
            record = self.lib.read_record(self.slug)
            claim = record['build_lease']['current']
            self.assertEqual(claim['state'], 'active')
            self.assertEqual(claim['pull_request'], winner[0])
            self.assertEqual(sum(c['gate'] == 'bind' for c in record['consent']), 1)
            self.assertFalse((self.root / f'locator-{loser[0]}.json').exists())
            locator = self.root / f'locator-{winner[0]}.json'
            value = core.json_file(locator)
            self.assertEqual(value['ownership'], build_state_store.claim_identity(claim))
            self.assertNotIn('findings', value)
            self.assertEqual(locator.stat().st_mode & 0o777, 0o600)
            saved = core.json_file(Path(claim['snapshot']))
            self.assertEqual(saved['ownership'], value['ownership'])
            self.assertEqual(saved['build']['pr'], winner[0])
            store = build_state_store.resolve_explicit(locator, SCHEMA, library=self.lib,
                                                       identity=value['ownership'])
            self.assertEqual(store.read(), saved)
        finally:
            for child in children:
                if child.is_alive(): child.terminate(); child.join(5)
            outcome.close(); outcome.join_thread()

    def test_preparing_claim_is_discovered_even_without_a_snapshot(self):
        claim = self.reserve()
        found = build_state_store.bound_snapshots(self.state['build']['worktree'], library=self.lib)
        self.assertEqual(found, [(self.slug, Path(claim['snapshot']))])
        store = build_state_store.resolve_for_worktree(self.state['build']['worktree'], SCHEMA,
                                                       library=self.lib)
        self.assertIsInstance(store, build_state_store.ClaimedBuildStore)
        self.assertFalse(store.path.exists())

    def test_cli_requires_caller_expectations_and_continuation_verifies_exact_pr(self):
        import build_coordinator as bc
        claim = self.reserve(); self.finish(claim)
        before = Path(claim['snapshot']).read_bytes()
        with mock.patch.object(bc, '_library', return_value=self.lib), \
                mock.patch.object(bc, 'ROOT', Path(self.state['build']['worktree'])), \
                mock.patch.object(bc, '_head', return_value='e' * 40), \
                mock.patch.object(bc, '_is_ancestor', return_value=True):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = bc.main(['approve', '--plan', 'not-read.json', '--depth', 'quick'])
            self.assertEqual(code, 2)
            self.assertIn('--expect-build-id', err.getvalue())
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = bc.main(['state', 'continue', '--plan', self.slug, '--repository', 'o/r', '--pr', '2'])
            self.assertEqual(code, 2)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = bc.main(['state', 'continue', '--plan', self.slug, '--repository', 'o/r', '--pr', '1'])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())['ownership'], build_state_store.claim_identity(claim))
        self.assertEqual(Path(claim['snapshot']).read_bytes(), before)

    def test_cli_legacy_migration_preserves_progress_and_retries_same_identity(self):
        import build_coordinator as bc
        old = build_state_store._legacy_slot(self.lib, self.slug)
        self.state['progress']['current_item'] = 'keep-progress'
        build_state_store.DurableBuildStore(old, SCHEMA, library_root=self.lib.root).create(self.state)
        self.lib.update_record(self.slug, lambda r: r.update(build_binding={
            'sealed_digest': self.seal['sealed_digest'], 'build_plan_digest': self.seal['build_plan_digest'],
            'repository': 'o/r', 'pull_request': 1, 'at': self.consent['at']}))
        outputs = []
        with mock.patch.object(bc, '_library', return_value=self.lib), \
                mock.patch.object(bc, 'ROOT', Path(self.state['build']['worktree'])):
            for _ in range(2):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = bc.main(['state', 'migrate', '--legacy-clients-stopped', '--source', str(old), '--plan', self.slug])
                self.assertEqual(code, 0)
                outputs.append(json.loads(out.getvalue()))
        self.assertEqual(outputs[0]['ownership'], outputs[1]['ownership'])
        saved = core.json_file(Path(outputs[0]['migrated']))
        self.assertEqual(saved['progress']['current_item'], 'keep-progress')
        self.assertEqual(saved['revision'], self.state['revision'])

    def test_ambiguous_missing_and_terminal_legacy_records_preserve_their_evidence(self):
        for scenario in ('missing', 'conflicting-pr', 'two-sources', 'terminal'):
            with self.subTest(scenario=scenario):
                case = TransactionalOwnership(); case.setUp()
                try:
                    source = case.root / 'legacy.json'
                    if scenario != 'missing':
                        build_state_store.DurableBuildStore(source, SCHEMA).create(case.state)
                    binding = {'sealed_digest': case.seal['sealed_digest'],
                        'build_plan_digest': case.seal['build_plan_digest'], 'repository': 'o/r',
                        'pull_request': 2 if scenario == 'conflicting-pr' else 1, 'at': case.consent['at']}
                    case.lib.update_record(case.slug, lambda r: r.update(build_binding=binding))
                    if scenario == 'two-sources':
                        old = build_state_store._legacy_slot(case.lib, case.slug)
                        build_state_store.DurableBuildStore(old, SCHEMA, library_root=case.lib.root).create(case.state)
                    if scenario == 'terminal':
                        case.lib.update_record(case.slug, lambda r: r.update(closure={
                            'state': 'complete', 'reason': 'legacy closure', 'at': case.consent['at']}))
                    before = case.lib.read_record(case.slug)
                    original = source.read_bytes() if source.exists() else None
                    with self.assertRaises(core.CoordinatorError): case.reserve(legacy_source=source)
                    self.assertEqual(case.lib.read_record(case.slug), before)
                    self.assertEqual(source.read_bytes() if source.exists() else None, original)
                finally:
                    case.doCleanups()

    def test_completion_rejects_the_pre_migration_address(self):
        import build_coordinator as bc
        source = self.root / 'external-legacy.json'
        build_state_store.DurableBuildStore(source, SCHEMA).create(self.state)
        self.lib.update_record(self.slug, lambda r: r.update(build_binding={
            'sealed_digest': self.seal['sealed_digest'], 'build_plan_digest': self.seal['build_plan_digest'],
            'repository': 'o/r', 'pull_request': 1, 'at': self.consent['at']}))
        with mock.patch.object(bc, '_library', return_value=self.lib), \
                mock.patch.object(bc, 'ROOT', Path(self.state['build']['worktree'])), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bc.main(['state', 'migrate', '--legacy-clients-stopped', '--source', str(source), '--plan', self.slug]), 0)
        claim = self.lib.read_record(self.slug)['build_lease']['current']
        proof = {k: claim[k] for k in ('build_id', 'generation', 'snapshot', 'repository', 'pull_request', 'sealed_digest')}
        proof.update(merged=True, snapshot=str(source))
        with self.assertRaisesRegex(core.CoordinatorError, 'completion requires'):
            build_state_store.retire_build(self.lib, self.slug, build_state_store.claim_identity(claim),
                SCHEMA, reason='merged', expected_revision=1, terminal_state='complete', completion=proof)
        self.assertTrue(source.is_file())
        self.assertTrue(Path(claim['snapshot']).is_file())

    def test_completion_cli_verifies_github_before_local_terminalization(self):
        import project_manager as pm
        import build_coordinator_github as github
        claim = self.reserve(); self.finish(claim)
        proof = {k: claim[k] for k in ('build_id', 'generation', 'snapshot', 'repository', 'pull_request', 'sealed_digest')}
        proof['merged'] = True
        evidence = self.root / 'merge-observation.json'; evidence.write_text(json.dumps(proof))
        args = ['--library', str(self.lib.root), 'complete', self.slug, '--reason', 'merged',
                '--expect-build-id', claim['build_id'], '--expect-generation', '1', '--expect-revision', '1',
                '--completion-evidence', str(evidence)]
        with mock.patch.object(github, 'pr_state', return_value={'number': 1, 'state': 'OPEN'}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(pm.main(args), 2)
        self.assertTrue(Path(claim['snapshot']).is_file())
        with mock.patch.object(github, 'pr_state', return_value={'number': 1, 'state': 'MERGED'}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.main(args), 0)
            self.assertEqual(pm.main(args), 0)
        self.assertEqual(self.lib.read_record(self.slug)['closure']['state'], 'complete')

    def test_deleting_the_plan_leaves_no_private_evidence_in_the_external_locator(self):
        import shutil
        locator = self.root / 'outside.json'
        claim = self.reserve(locator=locator); self.finish(claim)
        value = core.json_file(locator)
        self.assertEqual(set(value), {'schema_version', 'plan_id', 'ownership', 'snapshot'})
        shutil.rmtree(self.lib.plan_dir(self.slug))
        self.assertTrue(locator.exists())
        self.assertFalse(Path(value['snapshot']).exists())
        with self.assertRaises(core.CoordinatorError):
            build_state_store.resolve_explicit(locator, SCHEMA, library=self.lib)

    def test_locator_cannot_overwrite_an_unrelated_file_or_follow_a_symlink(self):
        path = self.root / 'keep.json'; path.write_text('{"keep":true}')
        before = path.read_bytes()
        with self.assertRaises(core.CoordinatorError): self.reserve(locator=path)
        self.assertEqual(path.read_bytes(), before)
        link = self.root / 'link'; link.symlink_to(path)
        with self.assertRaisesRegex(core.CoordinatorError, 'symlink'): self.reserve(locator=link)
        self.assertIsNone(self.lib.read_record(self.slug)['build_binding'])

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

    def test_retiring_copied_legacy_preparation_finishes_cutover_before_release(self):
        old = build_state_store._legacy_slot(self.lib, self.slug)
        build_state_store.DurableBuildStore(old, SCHEMA, library_root=self.lib.root).create(self.state)
        original = old.read_bytes()
        self.lib.update_record(self.slug, lambda r: r.update(build_binding={
            'sealed_digest': self.seal['sealed_digest'], 'build_plan_digest': self.seal['build_plan_digest'],
            'repository': 'o/r', 'pull_request': 1, 'at': self.consent['at']}))
        claim = self.reserve(legacy_source=old)
        with mock.patch.object(build_state_store, '_cutover_locked', side_effect=OSError('before cutover')):
            with self.assertRaisesRegex(OSError, 'before cutover'):
                self.finish(claim)
        self.assertTrue(Path(claim['snapshot']).is_file())
        self.retire(claim)
        self.assertTrue(old.is_dir())
        self.assertEqual((Path(claim['snapshot']).parent / 'legacy-original.json').read_bytes(), original)
        replacement = self.reserve(); self.finish(replacement)
        self.assertGreater(replacement['generation'], claim['generation'])

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

    def test_actual_old_bind_mutate_and_migrate_refuse_completed_canonical_barrier(self):
        import build_coordinator as bc
        claim = self.reserve(); self.finish(claim)
        old = build_state_store._legacy_slot(self.lib, self.slug)
        lock = build_state_store._legacy_lock(self.lib, self.slug)
        inode = lock.stat().st_ino
        evidence = Path(claim['snapshot']).read_bytes()
        old_store = core.StateStore(str(old), SCHEMA)
        namespace = dict(vars(bc))
        binding = mock.Mock()
        namespace.update(_sealed_plan=lambda *a: (self.state['plan']['plan_id'],
            self.seal['sealed_digest'], self.lib.head(self.slug)['build_plan']),
            _verify_draft=lambda *a: {'headRefOid': 'e' * 40, 'baseRefOid': 'a' * 40},
            _head=lambda: 'e' * 40, _initial_state=lambda *a: self.state,
            _record_build_binding=binding)
        exec(_LEGACY_BIND, namespace)
        args = argparse.Namespace(plan=self.slug, issue=None, repository='o/r', pr=1,
                                  mode='same-session', operator_decided=True)
        with self.assertRaisesRegex(core.CoordinatorError, 'already exists'):
            namespace['cmd_plan_bind'](args, old_store)
        binding.assert_not_called()
        namespace = dict(vars(core)); exec(_LEGACY_MUTATE, namespace)
        change = mock.Mock()
        with self.assertRaises((core.CoordinatorError, OSError)):
            namespace['mutate'](old_store, change, from_revision=1)
        change.assert_not_called()
        source = self.root / 'retained-external.json'; source.write_text(json.dumps(self.state))
        namespace = dict(vars(build_state_store)); namespace['snapshot_path'] = build_state_store._legacy_slot
        exec(_LEGACY_MIGRATE, namespace)
        with self.assertRaisesRegex(core.CoordinatorError, 'already holds'):
            namespace['migrate'](source, self.slug, SCHEMA, library=self.lib)
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertEqual(Path(claim['snapshot']).read_bytes(), evidence)
        self.assertTrue(old.is_dir())

    def test_old_superseder_before_cutover_demonstrates_why_clients_must_be_stopped(self):
        old = build_state_store._legacy_slot(self.lib, self.slug)
        build_state_store.DurableBuildStore(old, SCHEMA, library_root=self.lib.root).create(self.state)
        lock = build_state_store._legacy_lock(self.lib, self.slug)
        namespace = dict(vars(build_state_store)); namespace['snapshot_path'] = build_state_store._legacy_slot
        exec(_LEGACY_SUPERSEDE, namespace)
        with core.exclusive_lock(lock):
            # Old supersede ignores this held lock and unlinks its pathname. This is the
            # unsupported boundary, deliberately a negative witness rather than a claimed guarantee.
            archive = namespace['supersede'](self.lib, self.slug, reason='negative witness')
            self.assertFalse(lock.exists())
            self.assertFalse(old.exists())
            self.assertEqual(core.json_file(archive), self.state)

    def test_migration_acknowledgement_refuses_before_reservation(self):
        import build_coordinator as bc
        with mock.patch.object(bc, '_library') as library, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bc.main(['state', 'migrate', '--source', 'unused', '--plan', self.slug]), 2)
            library.assert_not_called()
        with self.assertRaisesRegex(core.CoordinatorError, 'legacy-clients-stopped'):
            build_state_store.reserve_build(self.lib, self.slug, self.state, legacy_source=self.root / 'unused')
        self.assertIsNone(self.lib.read_record(self.slug).get('build_lease'))

    def test_external_source_change_after_copy_refuses_activation_and_preserves_both_copies(self):
        source = self.root / 'external.json'
        build_state_store.DurableBuildStore(source, SCHEMA).create(self.state)
        original = source.read_bytes()
        self.lib.update_record(self.slug, lambda r: r.update(build_binding={
            'sealed_digest': self.seal['sealed_digest'], 'build_plan_digest': self.seal['build_plan_digest'],
            'repository': 'o/r', 'pull_request': 1, 'at': self.consent['at']}))
        claim = self.reserve(legacy_source=source)
        with self.assertRaisesRegex(core.CoordinatorError, 'retry state migrate'):
            self.reserve()
        with mock.patch.object(build_state_store, '_cutover_locked', side_effect=OSError('after copy')):
            with self.assertRaisesRegex(OSError, 'after copy'): self.finish(claim)
        canonical = Path(claim['snapshot']); copied = canonical.read_bytes()
        build_state_store.DurableBuildStore(source, SCHEMA).mutate(
            lambda s: s['progress'].update(current_item='newer external progress'), from_revision=1)
        changed = source.read_bytes()
        with self.assertRaisesRegex(core.CoordinatorError, 'Restore the original'):
            self.finish(claim)
        self.assertEqual(source.read_bytes(), changed)
        self.assertEqual(canonical.read_bytes(), copied)
        self.assertEqual(self.lib.read_record(self.slug)['build_lease']['current']['state'], 'preparing')
        source.unlink()
        with self.assertRaisesRegex(core.CoordinatorError, 'Restore the original'):
            self.finish(claim)
        self.assertEqual(canonical.read_bytes(), copied)
        source.write_bytes(original)
        self.finish(claim)
        self.assertEqual(source.read_bytes(), original)

    def test_external_source_lock_serializes_reservation_with_cooperating_writer(self):
        import queue
        source = self.root / 'external.json'
        build_state_store.DurableBuildStore(source, SCHEMA).create(self.state)
        self.lib.update_record(self.slug, lambda r: r.update(build_binding={
            'sealed_digest': self.seal['sealed_digest'], 'build_plan_digest': self.seal['build_plan_digest'],
            'repository': 'o/r', 'pull_request': 1, 'at': self.consent['at']}))
        ctx = multiprocessing.get_context('spawn'); attempted = ctx.Event(); outcome = ctx.Queue()
        process = ctx.Process(target=_legacy_reservation_after_source_lock,
            args=(str(self.lib.root), self.slug, str(source), self.state, attempted, outcome))
        try:
            with core.exclusive_lock(source.with_name(source.name + '.lock')):
                process.start()
                self.assertTrue(attempted.wait(10), 'reservation never reached the source lock')
                with self.assertRaises(queue.Empty): outcome.get(timeout=0.2)
                updated = core.json_file(source); updated['revision'] += 1
                updated['progress']['current_item'] = 'writer finished first'
                core.atomic_write(source, json.dumps(updated), durable=True, mode=0o600)
            process.join(10); self.assertFalse(process.is_alive(), 'source lock deadlocked')
            self.assertEqual(process.exitcode, 0)
            self.assertIn('legacy source changed', outcome.get(timeout=3))
            self.assertIsNone(self.lib.read_record(self.slug).get('build_lease'))
            self.assertEqual(core.json_file(source)['progress']['current_item'], 'writer finished first')
        finally:
            if process.is_alive(): process.terminate(); process.join(3)
            outcome.close(); outcome.join_thread()

    def test_retirement_of_copied_migration_rechecks_changed_external_evidence(self):
        source = self.root / 'external.json'
        build_state_store.DurableBuildStore(source, SCHEMA).create(self.state)
        self.lib.update_record(self.slug, lambda r: r.update(build_binding={
            'sealed_digest': self.seal['sealed_digest'], 'build_plan_digest': self.seal['build_plan_digest'],
            'repository': 'o/r', 'pull_request': 1, 'at': self.consent['at']}))
        claim = self.reserve(legacy_source=source)
        with mock.patch.object(build_state_store, '_cutover_locked', side_effect=OSError('copy cut')):
            with self.assertRaisesRegex(OSError, 'copy cut'): self.finish(claim)
        canonical = Path(claim['snapshot']); before = canonical.read_bytes()
        original = source.read_bytes()
        build_state_store.DurableBuildStore(source, SCHEMA).mutate(
            lambda s: s['progress'].update(current_item='new external evidence'), from_revision=1)
        changed = source.read_bytes()
        with self.assertRaisesRegex(core.CoordinatorError, 'Restore the original'):
            self.retire(claim)
        self.assertEqual(source.read_bytes(), changed)
        self.assertEqual(canonical.read_bytes(), before)
        self.assertEqual(self.lib.read_record(self.slug)['build_lease']['current']['state'], 'preparing')
        source.write_bytes(original)
        archive = self.retire(claim)
        self.assertEqual(archive.read_bytes(), before)
        self.assertFalse(canonical.exists())

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
