#!/usr/bin/env python3
"""Slice 20 — tests for boot, the SessionStart orientation pack.

These lock the load-bearing behaviours a non-engineer cannot read code to verify: the present-marker
byte-identity (boot's card title == the floor's verify-presence token in CLAUDE.deployed.md), that a
refused state cursor DEGRADES and never halts, that boot CONSUMES attention's order and never re-ranks,
that governance-critical alarms pin first and the protected-branch signal is honest in all three states
(off / unknown-never-green / on), that any reader failure fails open with the card still rendered, that
the SessionStart hook is wired on the session-start sources and NOT on compact, and that the block-budget
coherence leg born this slice has teeth.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from unittest import mock

import boot
import hooks
import module_coherence
import validate

DEPLOYED_FLOOR = os.path.join(validate.ROOT, "CLAUDE.deployed.md")
SETTINGS_PATH = os.path.join(validate.ROOT, ".claude", "settings.json")


def _offline():
    """Patch boot so no network is touched: no repo/token, a stable empty attention result, and a
    fixed recently-shipped digest. Returns a list of started patchers the caller stops."""
    patchers = [
        mock.patch.object(boot, "repo_slug", return_value=None),
        mock.patch.object(boot, "gh_token", return_value=None),
        mock.patch.object(boot, "recently_shipped", return_value=["#1 — a merged change"]),
    ]
    for p in patchers:
        p.start()
    return patchers


class TestPresentMarker(unittest.TestCase):
    def test_marker_is_project_status_byte_identical_to_the_floor(self):
        # The locked present marker, and its byte-identical presence in the deployed floor (slice 19).
        self.assertEqual(boot.PRESENT_MARKER, "Project status")
        with open(DEPLOYED_FLOOR, encoding="utf-8") as fh:
            floor = fh.read()
        self.assertIn(boot.PRESENT_MARKER, floor,
                      "the floor's verify-presence instruction must name the exact card title boot renders")

    def test_card_title_is_the_marker(self):
        patchers = _offline()
        try:
            pack = boot.assemble_pack()
        finally:
            for p in patchers:
                p.stop()
        # The card title is always the first line — its presence is exactly what the floor's
        # double-fault check looks for, so it must render even on the degraded/offline path.
        self.assertEqual(pack.splitlines()[0], f"## {boot.PRESENT_MARKER}")


class TestRefusedState(unittest.TestCase):
    def test_read_state_accepts_v1_and_refuses_otherwise(self):
        with tempfile.TemporaryDirectory() as d:
            good = os.path.join(d, "good.json")
            with open(good, "w") as fh:
                json.dump({"schema_version": 1, "standing_situation": {}, "integration_debt": {}}, fh)
            with mock.patch.object(boot, "STATE_PATH", good):
                state, refused = boot.read_state()
            self.assertFalse(refused)
            self.assertIsNotNone(state)

            bad = os.path.join(d, "bad.json")
            with open(bad, "w") as fh:
                json.dump({"schema_version": 2}, fh)  # not a v1 cursor
            with mock.patch.object(boot, "STATE_PATH", bad):
                state, refused = boot.read_state()
            self.assertTrue(refused)
            self.assertIsNone(state)

            with mock.patch.object(boot, "STATE_PATH", os.path.join(d, "absent.json")):
                _state, refused = boot.read_state()
            self.assertTrue(refused)  # absent cursor also degrades, never raises

    def test_refused_state_degrades_in_the_pack_but_card_still_renders(self):
        patchers = _offline()
        try:
            with mock.patch.object(boot, "read_state", return_value=(None, True)):
                pack = boot.assemble_pack()
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(pack.splitlines()[0], f"## {boot.PRESENT_MARKER}")
        self.assertIn("couldn't read where the project stands", pack)
        # healthy-empty ("none set yet") must NOT be confused with the refused line
        self.assertNotIn("none set yet", pack)

    def test_healthy_empty_reads_differently_from_refused(self):
        patchers = _offline()
        try:
            with mock.patch.object(boot, "read_state",
                                   return_value=({"schema_version": 1, "standing_situation": {},
                                                  "integration_debt": {"open_count": 0}}, False)):
                pack = boot.assemble_pack()
        finally:
            for p in patchers:
                p.stop()
        self.assertIn("none set yet", pack)
        self.assertNotIn("couldn't read where the project stands", pack)


class TestConsumesAttentionNeverReRanks(unittest.TestCase):
    def test_renders_attention_order_verbatim(self):
        # A partition whose ARRAY order is deliberately NOT precedence order: orientation (precedence 5)
        # appears before blocking_debt (precedence 1). Boot must render in the GIVEN array order — proving
        # it consumes attention's ordering and never re-sorts by precedence_rank (relay, not re-rank).
        result = {"partition": [
            {"category": "orientation", "precedence_rank": 5,
             "members": [{"id": "state:standing-situation", "rank": 1}]},
            {"category": "blocking_debt", "precedence_rank": 1,
             "members": [{"id": "state:integration-debt", "rank": 1}]},
        ], "degraded_inputs": []}
        state = {"standing_situation": {"milestone": "M1", "phase": "core"},
                 "integration_debt": {"open_count": 3}}
        with mock.patch.object(boot.attention, "rank_live", return_value=result):
            lines, degraded = boot.needs_attention(state)
        self.assertEqual(degraded, [])
        self.assertEqual(len(lines), 2)
        # orientation line first (it was first in the array), debt line second — array order preserved.
        self.assertIn("M1", lines[0])
        self.assertIn("integration debt", lines[1].lower())

    def test_caps_members_per_category_without_reordering(self):
        members = [{"id": f"k:{i}", "rank": i} for i in range(10)]
        result = {"partition": [{"category": "structural_neighbors", "precedence_rank": 4,
                                 "members": members}], "degraded_inputs": []}
        with mock.patch.object(boot.attention, "rank_live", return_value=result):
            lines, _ = boot.needs_attention({})
        self.assertEqual(len(lines), boot.NEEDS_ATTENTION_CAP)  # a bounded prefix
        self.assertIn("0 (k)", lines[0])                        # member 0 first (the prefix, in order)
        self.assertIn(f"{boot.NEEDS_ATTENTION_CAP - 1} (k)", lines[-1])  # ...through member CAP-1


class TestGovernanceAlarms(unittest.TestCase):
    def _pack_with(self, gate, findings):
        patchers = _offline()
        try:
            with mock.patch.object(boot, "protected_branch_signal", return_value=gate), \
                 mock.patch.object(boot, "open_findings", return_value=findings), \
                 mock.patch.object(boot, "read_state",
                                   return_value=({"schema_version": 1, "standing_situation": {},
                                                  "integration_debt": {"open_count": 0}}, False)):
                return boot.assemble_pack()
        finally:
            for p in patchers:
                p.stop()

    def test_gate_off_pins_a_loud_alarm_before_the_facts(self):
        pack = self._pack_with(("off", "a pull request is not required"), (0, "u"))
        lines = pack.splitlines()
        alarm = next(i for i, ln in enumerate(lines) if ln.startswith("> ") and "safety gate is off" in ln.lower())
        facts = next(i for i, ln in enumerate(lines) if ln.startswith("**Milestone"))
        self.assertLess(alarm, facts, "the governance alarm must pin above the status facts")

    def test_gate_unknown_is_never_a_green_all_clear(self):
        pack = self._pack_with(("unknown", None), (None, None))
        self.assertIn("don't assume", pack.lower())
        self.assertNotIn("safety gate is off", pack.lower())  # not a false positive either

    def test_gate_on_is_silent(self):
        pack = self._pack_with(("on", None), (0, "u"))
        self.assertNotIn("safety gate", pack.lower())

    def test_open_findings_pin_when_present(self):
        pack = self._pack_with(("on", None), (2, "https://example/issues"))
        self.assertIn("2 open engine finding", pack)
        self.assertIn("https://example/issues", pack)

    def test_protected_branch_signal_three_states(self):
        # no repo/token -> unknown (never a false "on")
        self.assertEqual(boot.protected_branch_signal(None, None), ("unknown", None))
        # token present, ruleset fully in force -> on
        with mock.patch.object(boot.protection_guard, "api_get", return_value=[]), \
             mock.patch.object(boot.protection_guard, "missing_floor", return_value=[]):
            self.assertEqual(boot.protected_branch_signal("o/r", "t"), ("on", None))
        # token present, floor missing -> off (a nag)
        with mock.patch.object(boot.protection_guard, "api_get", return_value=[]), \
             mock.patch.object(boot.protection_guard, "missing_floor", return_value=["no pull request"]):
            state, reason = boot.protected_branch_signal("o/r", "t")
            self.assertEqual(state, "off")
            self.assertIn("no pull request", reason)
        # unreachable / auth failure -> unknown, never a false "on"
        with mock.patch.object(boot.protection_guard, "api_get", side_effect=Exception("boom")):
            self.assertEqual(boot.protected_branch_signal("o/r", "t"), ("unknown", None))
        # a 200 with a non-list body (an error object / null) is NOT a confirmation -> unknown, never "on"
        for body in ({"message": "Not Found"}, None, "nonsense"):
            with mock.patch.object(boot.protection_guard, "api_get", return_value=body):
                self.assertEqual(boot.protected_branch_signal("o/r", "t"), ("unknown", None),
                                 f"a non-list body ({body!r}) must read unknown, never on")


class TestFailOpen(unittest.TestCase):
    def test_a_reader_exception_degrades_that_line_only(self):
        patchers = _offline()
        try:
            with mock.patch.object(boot.attention, "rank_live", side_effect=Exception("down")):
                lines, degraded = boot.needs_attention({})
                pack = boot.assemble_pack()
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(lines, [])
        self.assertEqual(degraded, ["attention"])
        self.assertEqual(pack.splitlines()[0], f"## {boot.PRESENT_MARKER}")  # the card still renders

    def test_a_bad_protection_body_never_blanks_the_whole_pack(self):
        # A governance reader returning a surprise (a 200 with a non-list body) must degrade THAT line
        # only — the card title must still render, or the operator loses the whole orientation to one
        # sibling read's bad response (and with it the safety-gate alarm).
        patchers = _offline()
        try:
            with mock.patch.object(boot, "repo_slug", return_value="o/r"), \
                 mock.patch.object(boot, "gh_token", return_value="t"), \
                 mock.patch.object(boot.protection_guard, "api_get", return_value={"message": "x"}):
                pack = boot.assemble_pack()
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(pack.splitlines()[0], f"## {boot.PRESENT_MARKER}")
        self.assertIn("don't assume", pack.lower())  # the unknown-gate line, not a green all-clear

    def test_handler_never_raises_and_injects(self):
        patchers = _offline()
        try:
            decision = boot.handler({})
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(decision.get("action"), "inject")
        self.assertIn(boot.PRESENT_MARKER, decision.get("context", ""))

    def test_run_hook_end_to_end_never_halts(self):
        # SessionStart is not block-eligible and run_hook fail-opens, so the exit code is the proceed/
        # inject code (0), never the blocking code (2) — a boot crash can never halt a session.
        patchers = _offline()
        out, err = io.StringIO(), io.StringIO()
        try:
            code = hooks.run_hook("SessionStart", boot.handler,
                                  stdin=io.StringIO('{"source":"startup"}'), stdout=out, stderr=err)
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(code, hooks.EXIT_PROCEED)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn(boot.PRESENT_MARKER, payload["hookSpecificOutput"]["additionalContext"])


class TestHookRegistration(unittest.TestCase):
    def setUp(self):
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            self.settings = json.load(fh)

    def test_sessionstart_wired_on_the_start_sources_not_compact(self):
        groups = self.settings["hooks"]["SessionStart"]
        matchers = {g["matcher"] for g in groups}
        self.assertEqual(matchers, set(boot.SESSION_START_SOURCES))
        self.assertNotIn("compact", matchers,
                         "boot must NOT re-render on compaction (negative law: no compact re-render)")

    def test_every_command_points_into_engine_and_names_boot(self):
        for g in self.settings["hooks"]["SessionStart"]:
            for h in g["hooks"]:
                self.assertEqual(h["type"], "command")
                self.assertIn(".engine/", h["command"])      # the wiring guard
                self.assertIn("tools/boot.py", h["command"])
                self.assertIn("/.venv/", h["command"])        # the runtime interpreter, never bare python

    def test_start_sources_exclude_compact_and_are_valid_events(self):
        self.assertNotIn("compact", boot.SESSION_START_SOURCES)
        self.assertIn("SessionStart", hooks.EVENT_INVENTORY)  # the wired event is a real one


class TestBlockBudgetLeg(unittest.TestCase):
    """The block-budget coherence leg born this slice (validate.block_budget_findings, run live in
    module_coherence). It is green-but-present today and must have teeth for slices 21/22."""

    def test_registry_is_empty_today_and_leg_is_green(self):
        self.assertEqual(module_coherence.block_eligible_registrations(), [])
        block = [f for f in module_coherence.check_coherence("hard")]
        # the whole module set is coherent, so check_coherence is empty — and in particular carries no
        # block-budget finding (the registry is empty).
        self.assertEqual(block, [])

    def test_leg_has_teeth_when_a_block_is_misplaced(self):
        msg = "move it."
        self.assertEqual(validate.block_budget_findings([], "hard", msg), [])  # green-but-present
        fired = validate.block_budget_findings(
            [{"event": "SessionStart", "name": "x", "owner": "modes"}], "hard", msg)
        self.assertEqual(len(fired), 1)                       # a block on an ineligible event fires
        self.assertIn("SessionStart", fired[0]["message"])
        clean = validate.block_budget_findings(
            [{"event": "Stop", "name": "disposition", "owner": "close"}], "hard", msg)
        self.assertEqual(clean, [])                           # an eligible event is clean


if __name__ == "__main__":
    unittest.main()
