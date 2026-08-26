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
                            "--pr", "1", "--operator-decision", "go"])
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
                     "--repository", "o/r", "--pr", "1", "--operator-decision", "go"])
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
                operator_decision="go", state=None,
                # Bind refuses without the operator's answer to the seal hand-back, and it refuses
                # BEFORE the address lookup this case is about. Supplied so the refusal under test
                # is the one the fixture actually installed, not the gate standing in front of it.
                session_model="opus-5", session_effort="high"), None)
        self.assertEqual(asked["plan_id"], "pln_0123456789ab")


class TheKillAndResumeDemo(unittest.TestCase):
    """The standalone reproducer, run end to end. Importing it here is also what keeps the demonstration
    alive for the census reference-closure, so it ships rather than retiring as construction evidence —
    a Build outliving the session that started it is forever-relevant to any deployed project."""

    def test_the_kill_and_resume_demo_passes(self):
        import quiet_call
        import demo_build_resumes_after_a_kill as demo
        self.assertEqual(quiet_call.run(demo.main), 0)


if __name__ == "__main__":
    unittest.main()
