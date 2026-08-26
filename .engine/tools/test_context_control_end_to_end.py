#!/usr/bin/env python3
"""End-to-end cover for the context-control spine, plus the static half of the powers-not-taken claim.

Two things live here that live nowhere else.

THE DEMO RUNS. `demo_context_control` is behavioral falsification with real negative controls, and a
demo nothing executes is documentation. Running it here is what makes it travel with the engine.

THE ABSENCES ARE ASSERTED. Criterion 9 of the plan is a claim about what the engine does NOT do, and
absence is exactly what a behavioral test is worst at proving — the demo can show that one run wrote
nothing, but not that no path anywhere writes. So the absences are asserted statically, and scoped to
what can be checked honestly rather than to what sounds most sweeping:

  - The auto-compact threshold key appears in no shipped tool and no wiring target. That one IS
    repository-wide and is the strongest of these, because the key is a single literal: if the engine
    ever writes that setting, it must name it, and naming it fails here.
  - The context-control surface spawns no process. This is what "never invokes clear, never initiates
    compaction" reduces to mechanically — neither is reachable except by running something.
  - The surface writes nothing at all. It used to be allowed one write — its own record of observed
    compactions — so the assertion could only say "nothing else". Nothing reads such a record now, so
    the claim is absolute and the check has no carve-out to get stale.
  - The surface reads no transcript and computes no length-based estimate. This is the narrowest of
    the three and is scoped to the functions themselves, because a repository-wide ban on `len(` would
    be theatre.

What none of this proves: that some future code could not do these things. It proves that the code
shipped here does not, which is the claim actually being made.
"""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator as bc      # noqa: E402
import quiet_call                   # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent

# The whole context-control surface, by function. Named explicitly rather than discovered, so adding a
# function to this surface is a deliberate act that has to come here and be argued for.
SURFACE = (
    bc.reground_handler,
    bc.reground_pointer,
    bc.resume_reasons,
    bc.verify_resume,
)


def _surface_source() -> str:
    return "\n".join(inspect.getsource(fn) for fn in SURFACE)


class TheDemoRuns(unittest.TestCase):
    def test_the_context_control_falsification_demo_passes(self):
        import demo_context_control as demo
        self.assertEqual(quiet_call.run(demo.main), 0)


class ThePowersNotTaken(unittest.TestCase):
    """Criterion 9, asserted as absence — and scoped so each assertion can actually fail."""

    def test_no_shipped_tool_names_the_auto_compact_threshold(self):
        # The strongest of these: the setting has exactly one name, so writing it means naming it.
        offenders = []
        for path in sorted(TOOLS.rglob("*.py")):
            if path.name.startswith(("test_", "demo_")):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if re.search(r"autoCompactWindow|auto_compact_window", text):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], "the engine writes no auto-compact setting, in any scope")

    def test_no_wiring_target_carries_the_auto_compact_threshold(self):
        for relative in (".claude/settings.json", ".codex/hooks.json"):
            path = ROOT / relative
            if not path.is_file():
                continue
            self.assertNotIn("autoCompactWindow", path.read_text(encoding="utf-8"),
                             f"{relative} must not carry an engine-written auto-compact threshold")
        for manifest in sorted((ROOT / ".engine/modules").rglob("manifest.json")):
            self.assertNotIn("autoCompactWindow", manifest.read_text(encoding="utf-8"),
                             f"{manifest} must not render an auto-compact threshold")

    def test_the_surface_spawns_no_process(self):
        # "Never invokes /clear, never initiates a compaction" reduces to this: neither is reachable
        # without running something. The engine's own git reads live outside this surface.
        source = _surface_source()
        for forbidden in ("subprocess.", "os.system", "os.exec", "os.spawn", "popen"):
            self.assertNotIn(forbidden, source.lower().replace("subprocess.run(\n", "subprocess.run("),
                             f"the context-control surface must not reach for {forbidden}")

    def test_the_surface_reads_no_transcript_and_estimates_nothing(self):
        source = _surface_source()
        for forbidden in ("transcript", "jsonl", "token", "utilization", "context_window"):
            self.assertNotIn(forbidden, source.lower(),
                             "utilization is never estimated from text — the engine reports what it "
                             "observed, never what it guessed")

    def test_the_surface_writes_nothing_at_all(self):
        # Stronger than the claim this replaced, and simpler to check. The surface used to be allowed
        # one kind of write — its own append-only record of compactions — which meant the assertion
        # had to carve out an exception and could only ever say "nothing ELSE". Nothing reads such a
        # record now, so the carve-out is gone and the claim is absolute. The behavioural half is the
        # demo's job: it runs the real handler against a real engine clone and compares every byte.
        for fn in SURFACE:
            source = inspect.getsource(fn)
            for forbidden in ("write_text", "atomic_write", "open(", ".observe("):
                with self.subTest(fn=fn.__name__, forbidden=forbidden):
                    self.assertNotIn(forbidden, source,
                                     f"{fn.__name__} must not write anything")


class TheGuaranteeDoesNotRestOnTheHook(unittest.TestCase):
    """The single most important property, asserted where it cannot be argued away.

    Everything else in this spine is best-effort: the hook fails open, the injection is advisory, an
    observation may never be written. If verification were conditional on any of that, the whole
    design would be decoration. So this asserts the two are genuinely independent.
    """

    def test_verification_reaches_the_same_verdict_with_no_observations_at_all(self):
        state = {"build": {"worktree": "/somewhere/else"}, "plan": {}}
        self.assertTrue(bc.resume_reasons(state, worktree="/here", head=None))

    def test_verification_does_not_consult_the_observation_record(self):
        source = inspect.getsource(bc.resume_reasons)
        self.assertNotIn("observations", source,
                         "resume_reasons must not read observations — that is what makes it "
                         "unconditional rather than compaction-triggered")

    def test_every_verb_absent_from_the_read_only_set_is_verified(self):
        import argparse
        self.assertTrue(bc._mutates(argparse.Namespace(command="some-future-verb")))
        for command, sub in bc._READ_ONLY_VERBS:
            namespace = argparse.Namespace(command=command)
            if sub:
                setattr(namespace, f"{command}_command", sub)
            self.assertFalse(bc._mutates(namespace), f"{command} {sub or ''} is read-only")

    def test_the_carved_out_recovery_verbs_really_do_skip_the_gate(self):
        """The gap this file used to hide rather than show.

        `_mutates` is a PREDICATE, and asserting it is not the same as asserting the gate runs. Two
        verbs it labels mutating — `state migrate` and `state supersede` — never reach verification,
        because main() resolves no store for them. Asserting the predicate alone read as if it proved
        the obligation while the gate was skipped, which is how the divergence survived a review.

        So this drives main() and watches for the call. The carve-out is deliberate (eADR-0045: both
        exist to handle a snapshot this session does not match, so gating them deadlocks recovery) —
        what must not happen is it being deliberate and invisible.
        """
        import argparse
        from unittest import mock

        # The predicate says these mutate...
        for sub in ("migrate", "supersede"):
            self.assertTrue(bc._mutates(argparse.Namespace(command="state", state_command=sub)),
                            f"state {sub} is classified as mutating")
        # ...and the gate does not run for them. Asserted through main(), not by reading the set.
        with mock.patch.object(bc, "verify_resume") as gate, \
                mock.patch.object(bc, "cmd_state_supersede"):
            bc.main(["state", "supersede", "--plan", "pln_0123456789ab", "--reason", "a demo"])
        gate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
