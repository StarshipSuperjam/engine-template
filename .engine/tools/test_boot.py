#!/usr/bin/env python3
"""Slice 20 — tests for boot, the SessionStart orientation pack.

These lock the load-bearing behaviours a non-engineer cannot read code to verify: the present-marker
byte-identity (boot's card title == the floor's verify-presence token in CLAUDE.deployed.md), that a
refused state cursor DEGRADES and never halts, that boot CONSUMES attention's order and never re-ranks,
that governance-critical alarms pin first and the protected-branch signal is honest in all three states
(off / unknown-never-green / on), that any reader failure fails open with the card still rendered, that
the SessionStart hook is wired on the session-start sources and NOT on compact, that boot clears the modes
stance signal at SessionStart and names the current stance (slice 21), and that the block-budget coherence
leg now validates modes' real explore-write-gate member.
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
        # No real git in offline tests: the work-in-hand focus derivation reads local git, so pin it empty
        # (a focused-read test opts back in by re-patching derive_focus with its own fixture).
        mock.patch.object(boot.attention, "derive_focus", return_value=[]),
    ]
    for p in patchers:
        p.start()
    return patchers


def _assert_ai_briefing(t, pack):
    """The pack is the AI-FACING briefing (not an operator card): it opens with the briefing header, says
    the operator cannot see it, and carries the `Project status` present-marker token on EVERY branch."""
    t.assertTrue(pack.splitlines()[0].startswith("=== ENGINE BOOT BRIEFING"),
                 "the pack is the AI-facing briefing, not a rendered card")
    t.assertIn("the operator CANNOT see this", pack)
    t.assertIn(boot.PRESENT_MARKER, pack)  # the present-marker token survives every branch


# A complete, valid signals dict for the pure renderers (render_dashboard / present_marker_line / must_push).
_SIGNALS = {"state": {"schema_version": 1, "standing_situation": {}, "integration_debt": {}},
            "refused": False, "gate": "on", "reason": None, "finding_count": 0, "register": "",
            "findings_unavailable": False, "debt_count": 0, "debt_as_of": None, "att_lines": [],
            "att_degraded": False, "shipped": [], "stance": "Exploring", "strand": None,
            "live_standing": None, "neighborhood": None}


def _signals(**over):
    s = dict(_SIGNALS)
    s.update(over)
    return s


class TestPresentMarker(unittest.TestCase):
    def test_marker_is_project_status_byte_identical_to_the_floor(self):
        # The locked present marker, and its byte-identical presence in the deployed floor (slice 19).
        self.assertEqual(boot.PRESENT_MARKER, "Project status")
        with open(DEPLOYED_FLOOR, encoding="utf-8") as fh:
            floor = fh.read()
        self.assertIn(boot.PRESENT_MARKER, floor,
                      "the floor's verify-presence instruction must name the exact card title boot renders")

    def test_dashboard_card_title_is_the_marker(self):
        # The operator-toned dashboard (the view the status verb ships) always leads with the card title.
        self.assertEqual(boot.render_dashboard(_signals()).splitlines()[0], f"## {boot.PRESENT_MARKER}")

    def test_pack_is_the_ai_facing_briefing(self):
        patchers = _offline()
        try:
            pack = boot.assemble_pack()
        finally:
            for p in patchers:
                p.stop()
        # The pack is no longer a rendered card — it is the AI-facing briefing that INSTRUCTS the assistant
        # to render the present-marker block first. Its first line is the briefing header, not the card.
        _assert_ai_briefing(self, pack)
        self.assertIn("Open your reply", pack)
        self.assertIn(f"`{boot.PRESENT_MARKER}` block", pack)


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
        _assert_ai_briefing(self, pack)
        self.assertIn("couldn't read where the project stands", pack)
        # the refused branch shows NO standing lines at all — neither "Where we are" nor "Milestone"
        self.assertNotIn("Where we are", pack)
        self.assertNotIn("**Milestone:**", pack)

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
        # offline (no repo/token) the live derive is skipped, so the card shows the cached standing lines —
        # an absent milestone renders as the honest normal "No milestone is open", and it is stale-labelled.
        self.assertIn("No milestone is open", pack)
        self.assertIn("Where we are", pack)
        self.assertIn("may be out of date", pack)   # the cached read names that it couldn't be refreshed
        self.assertNotIn("couldn't read where the project stands", pack)


class TestWhereWeAreLiveOrCached(unittest.TestCase):
    """The 'Where we are' line obeys the boot rendering law (D-198/D-199): show ONE of live-or-cached, never
    both; the live line when the GitHub derive succeeded; otherwise the committed offline cache, named with
    WHEN it was cached and that it may be stale; `none set` is an honest normal state, never an error."""

    def test_live_lines_shown_when_live_standing_present(self):
        dash = boot.render_dashboard(_signals(
            live_standing={"milestone": "Ship the beta", "phase": "Wire the login (issue #7)"},
            state={"standing_situation": {"milestone": "STALE", "phase": "STALE (issue #1)",
                                          "as_of": "2020-01-01T00:00:00Z"}}))
        self.assertIn("**Where we are:** Wire the login (issue #7)", dash)   # the active work
        self.assertIn("**Milestone:** Ship the beta", dash)                 # the plan marker, its own line
        self.assertNotIn("STALE", dash)                 # the live answer wins; the cache is not shown
        self.assertNotIn("may be out of date", dash)    # a live read carries no staleness caveat

    def test_cached_lines_are_stale_labelled_with_their_as_of_when_live_is_none(self):
        dash = boot.render_dashboard(_signals(
            live_standing=None,
            state={"standing_situation": {"milestone": None, "phase": "Wire the login (issue #7)",
                                          "as_of": "2026-06-15T12:00:00Z"}}))
        self.assertIn("**Where we are:** Wire the login (issue #7)", dash)
        self.assertIn("**Milestone:** No milestone is open", dash)          # absent milestone, plain language
        self.assertIn("as of 2026-06-15T12:00:00Z", dash)   # names WHEN it was cached (the provenance law)
        self.assertIn("may be out of date", dash)

    def test_cached_line_without_as_of_says_an_earlier_session(self):
        dash = boot.render_dashboard(_signals(
            live_standing=None,
            state={"standing_situation": {"milestone": None, "phase": None}}))  # no as_of -> honest fallback
        self.assertIn("as of an earlier session", dash)
        self.assertIn("**Where we are:** nothing in progress yet", dash)     # no tracked work -> plain phrase

    def test_exactly_one_where_we_are_line_is_rendered(self):
        # never both a live and a cached block — the law's "show one"
        for live in ({"milestone": "M", "phase": "P"}, None):
            dash = boot.render_dashboard(_signals(
                live_standing=live, state={"standing_situation": {"milestone": "C", "phase": "C2",
                                                                   "as_of": "2026-06-15T00:00:00Z"}}))
            self.assertEqual(dash.count("**Where we are:**"), 1)
            self.assertEqual(dash.count("**Milestone:**"), 1)

    def test_absent_milestone_renders_as_normal_not_an_error(self):
        dash = boot.render_dashboard(_signals(live_standing={"milestone": None, "phase": "Do the thing (issue #9)"}))
        self.assertIn("**Where we are:** Do the thing (issue #9)", dash)
        self.assertIn("**Milestone:** No milestone is open", dash)
        self.assertNotIn("none set", dash)              # the old confusing wording is gone
        for jargon in ("error", "⚠", "⛔"):             # an absent milestone is normal — no alarm framing
            mline = next(ln for ln in dash.splitlines() if ln.startswith("**Milestone"))
            self.assertNotIn(jargon, mline)


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
        with mock.patch.object(boot.attention, "derive_focus", return_value=[]), \
                mock.patch.object(boot.attention, "rank_live", return_value=result):
            lines, degraded, _ = boot.needs_attention(state)
        self.assertEqual(degraded, [])
        self.assertEqual(len(lines), 2)
        # orientation line first (it was first in the array), debt line second — array order preserved.
        # The orientation nudge is now a NEUTRAL re-ground line (it no longer quotes the committed cursor,
        # which is only the offline cache — "where we are" is shown live in the facts block).
        self.assertIn("where the project stands", lines[0].lower())
        self.assertNotIn("M1", lines[0])     # the stale cached value is not presented as the live to-do
        self.assertIn("integration debt", lines[1].lower())

    def test_caps_members_per_category_without_reordering(self):
        # An ACTION category (recent_decisions) — structural_neighbors are now routed to the pack
        # neighborhood block, not the action lines, so the per-category cap is exercised on a category that
        # still renders as action lines.
        members = [{"id": f"k:{i}", "rank": i} for i in range(10)]
        result = {"partition": [{"category": "recent_decisions", "precedence_rank": 3,
                                 "members": members}], "degraded_inputs": []}
        with mock.patch.object(boot.attention, "derive_focus", return_value=[]), \
                mock.patch.object(boot.attention, "rank_live", return_value=result):
            lines, _, _ = boot.needs_attention({})
        self.assertEqual(len(lines), boot.NEEDS_ATTENTION_CAP)  # a bounded prefix
        self.assertIn("0 (k)", lines[0])                        # member 0 first (the prefix, in order)
        self.assertIn(f"{boot.NEEDS_ATTENTION_CAP - 1} (k)", lines[-1])  # ...through member CAP-1


class TestFocusedNeighborhood(unittest.TestCase):
    """The orientation-time focused knowledge read (#37, D-224): a focus derived from the work in hand drives
    a BIDIRECTIONAL neighbourhood, rendered as an AI-facing block — PER SOURCE, by relationship, with the TRUE
    count disclosed when truncated — NOT operator action lines, and never an arbitrary capped few as if salient."""

    def _summary(self):
        # what attention.neighborhood_of returns: per-(member, relationship) groups with full counts + samples.
        return {"focus": ["tool:attention"], "groups": [
            {"source": "tool:attention", "predicate": "provided_by", "direction": "out",
             "total": 1, "sample": ["module:core"]},
            {"source": "tool:attention", "predicate": "targets", "direction": "in",
             "total": 2, "sample": ["check:policy-frontmatter", "check:policy-shape"]},
        ]}

    def _partition(self):
        # an in_flight action item AND a structural_neighbors entry (the ranked partition still carries the
        # flat slice for the CLI/budget); needs_attention must route structural_neighbors OUT of the action lines.
        return {"partition": [
            {"category": "in_flight", "precedence_rank": 2, "members": [{"id": "pr:161", "rank": 1}]},
            {"category": "structural_neighbors", "precedence_rank": 4,
             "members": [{"id": "module:core", "rank": 1}]},
        ], "degraded_inputs": ["telemetry"]}

    def test_structural_neighbors_never_become_action_lines_and_the_summary_is_carried(self):
        with mock.patch.object(boot.attention, "derive_focus", return_value=["tool:attention"]), \
                mock.patch.object(boot.attention, "rank_live", return_value=self._partition()), \
                mock.patch.object(boot.attention, "neighborhood_of", return_value=self._summary()):
            lines, degraded, nb = boot.needs_attention({})
        self.assertTrue(any("161" in ln for ln in lines))      # the in_flight item IS an action line
        self.assertFalse(any("core" in ln for ln in lines))    # the neighbours are NOT (they are the AI block)
        self.assertEqual(degraded, ["telemetry"])
        self.assertEqual(nb, self._summary())                  # the rich summary is carried verbatim to render

    def test_render_is_per_source_by_relationship_in_plain_words(self):
        block = "\n".join(boot.render_neighborhood(self._summary()))
        self.assertIn("You're touching: attention", block)
        self.assertIn("attention is part of core", block)                 # forward provided_by -> its module
        self.assertIn("attention is checked by: policy-frontmatter, policy-shape", block)  # reverse targets
        self.assertNotIn("tool:", block)                                  # no raw ids
        self.assertNotIn("module:", block)
        self.assertNotIn("provided_by", block)                            # no raw predicate vocabulary (§12)
        self.assertNotIn("targets", block)
        self.assertIn("knowledge neighborhood of your current work", block)

    def test_honest_truncation_discloses_the_true_count(self):
        # the maintainer's binding correction: a hub focus must NOT show an arbitrary capped few as if salient;
        # the render states the true total and frames the sample AS a sample (#37 / D-224).
        summary = {"focus": ["module:core"], "groups": [
            {"source": "module:core", "predicate": "provided_by", "direction": "in",
             "total": 147, "sample": ["audit_library", "boot", "close", "conduct"]}]}
        block = "\n".join(boot.render_neighborhood(summary))
        # the TRUE count, AND the shown few framed as arbitrary examples (never "the 4 that matter")
        self.assertIn("core provides 147 (showing 4 examples, not ranked by importance:", block)
        self.assertIn("audit_library, boot, close, conduct", block)
        self.assertNotIn("provides:", block)                              # not rendered as if it were the whole

    def test_no_focus_or_no_groups_renders_cleanly(self):
        self.assertEqual(boot.render_neighborhood(None), [])
        self.assertEqual(boot.render_neighborhood({"focus": [], "groups": []}), [])
        bare = "\n".join(boot.render_neighborhood({"focus": ["tool:x"], "groups": []}))
        self.assertIn("You're touching: x", bare)                         # the focus is still named
        self.assertIn("nothing else is connected", bare.lower())          # neutral, no-jargon, not an alarm
        with mock.patch.object(boot.attention, "derive_focus", return_value=[]), \
                mock.patch.object(boot.attention, "rank_live",
                                  return_value={"partition": [], "degraded_inputs": []}):
            _, _, nb = boot.needs_attention({})
        self.assertIsNone(nb)                                             # no work in hand -> no neighbourhood

    def test_pack_carries_the_neighborhood_block_when_focus_present(self):
        patchers = _offline()
        try:
            with mock.patch.object(boot.attention, "derive_focus", return_value=["tool:attention"]), \
                    mock.patch.object(boot.attention, "rank_live", return_value=self._partition()), \
                    mock.patch.object(boot.attention, "neighborhood_of", return_value=self._summary()):
                pack = boot.assemble_pack()
        finally:
            for p in patchers:
                p.stop()
        self.assertIn("knowledge neighborhood of your current work", pack)
        self.assertIn("You're touching: attention", pack)
        self.assertIn("attention is checked by", pack)

    def test_relation_phrase_covers_every_walk_edge_in_both_directions(self):
        # render_neighborhood SILENTLY skips a group whose (predicate, direction) has no phrase. Pin the
        # table to the full pinned edge set so a future walk edge can't make a real neighbour group vanish
        # unseen (the render must always be able to name the relationship the graph reaches a neighbour by).
        import knowledge_index
        for edge in knowledge_index.WALK_EDGE_KINDS:
            for direction in ("in", "out"):
                self.assertIn((edge, direction), boot._RELATION_PHRASE,
                              f"render_neighborhood has no plain-language phrase for ({edge}, {direction})")


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
        facts = next(i for i, ln in enumerate(lines) if ln.startswith("**Where we are"))
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


class TestStrandSurfacing(unittest.TestCase):
    """Slice B: a stranded operator checkout is surfaced read-only at the OPEN-FINDINGS tier — pinned BELOW
    the governance alarms (a stranded local checkout cannot reach the protected branch) and NOT in the
    must-push/INFORM set. Detection only — the line names that it cannot yet be repaired (the fix is slice C)."""
    _STRAND = {"states": ["detached"], "main": "/p"}

    def test_render_surfaces_the_strand_line_only_when_stranded(self):
        stranded = boot.render_dashboard(_signals(strand=self._STRAND))
        self.assertIn("drifted into a broken state", stranded)
        self.assertIn("say the word", stranded.lower())          # slice C: boot now OFFERS the fix
        self.assertIn("nothing is lost", stranded.lower())       # ...and names it lossless
        self.assertNotIn("drifted into a broken state", boot.render_dashboard(_signals(strand=None)))

    def test_strand_pins_below_the_governance_alarm(self):
        # gate off AND stranded: the safety-gate alarm pins ABOVE the strand heads-up (the tier order).
        pack = boot.render_dashboard(_signals(gate="off", reason="x", strand=self._STRAND))
        lines = pack.splitlines()
        gate = next(i for i, ln in enumerate(lines) if "safety gate is off" in ln.lower())
        strand = next(i for i, ln in enumerate(lines) if "drifted into a broken state" in ln.lower())
        self.assertLess(gate, strand, "the governance alarm must pin above the strand heads-up")

    def test_present_marker_reflects_a_strand_but_governance_outranks(self):
        self.assertEqual(boot.present_marker_line(_signals(strand=self._STRAND)),
                         f"⚠ {boot.PRESENT_MARKER}: your project folder needs attention")
        self.assertEqual(boot.present_marker_line(_signals(strand=None)),
                         f"{boot.PRESENT_MARKER}: all clear")
        # a governance alarm still wins the marker even when the folder is ALSO stranded
        self.assertEqual(boot.present_marker_line(_signals(gate="off", strand=self._STRAND)),
                         "⚠ Protected branch is off")

    def test_strand_is_not_in_the_must_push_set(self):
        # a strand is NOT governance-critical -> no INFORM marker (relayed via the needs-attention headline).
        self.assertEqual(boot.must_push(_signals(strand=self._STRAND)), [])
        self.assertFalse(any("folder" in it.lower() for it in
                             boot.must_push(_signals(gate="off", reason="x", strand=self._STRAND))))

    def test_gather_signals_relays_the_detector_and_degrades_quietly(self):
        patchers = _offline()
        try:
            with mock.patch.object(boot.checkout_health, "detect_strand", return_value=self._STRAND):
                relayed = boot.gather_signals()
            with mock.patch.object(boot.checkout_health, "detect_strand", side_effect=Exception("boom")):
                failed = boot.gather_signals()
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(relayed["strand"], self._STRAND)   # the detector's signal is relayed verbatim
        self.assertIsNone(failed["strand"])                 # a detector failure degrades quietly to None


class TestFailOpen(unittest.TestCase):
    def test_a_reader_exception_degrades_that_line_only(self):
        patchers = _offline()
        try:
            with mock.patch.object(boot.attention, "rank_live", side_effect=Exception("down")):
                lines, degraded, neighborhood = boot.needs_attention({})
                pack = boot.assemble_pack()
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(lines, [])
        self.assertEqual(degraded, ["attention"])
        self.assertIsNone(neighborhood)  # attention down -> no focused-read neighborhood either
        _assert_ai_briefing(self, pack)  # the briefing still assembles + carries the present-marker token

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
        _assert_ai_briefing(self, pack)
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


class TestBriefingRelay(unittest.TestCase):
    """The operator-presentation relay (D-187/D-188): the AI-facing briefing, the present-marker line the
    AI renders first, the INFORM-marked must-push partition, and the clean pure dashboard (the Slice-3 seam)."""

    def test_present_marker_line_all_clear_when_healthy(self):
        self.assertEqual(boot.present_marker_line(_signals(gate="on")), f"{boot.PRESENT_MARKER}: all clear")

    def test_present_marker_line_is_the_alarm_when_gate_off(self):
        self.assertEqual(boot.present_marker_line(_signals(gate="off")), "⚠ Protected branch is off")

    def test_present_marker_line_never_green_when_gate_unknown(self):
        # degrade-loud: a couldn't-verify gate is NEVER a green all-clear.
        line = boot.present_marker_line(_signals(gate="unknown"))
        self.assertTrue(line.startswith("⚠"))
        self.assertNotIn("all clear", line)

    def test_marker_token_in_briefing_on_the_alarm_branch(self):
        # On a governance-alarm branch the rendered marker line drops the literal "Project status" title,
        # but the briefing's instruction still names it — so the present-marker token is present on EVERY
        # branch, not only all-clear (the byte-identity contract holds where it most matters).
        with mock.patch.object(boot, "gather_signals",
                               return_value=_signals(gate="off", reason="no pull request")):
            pack = boot.assemble_pack()
        self.assertIn("⚠ Protected branch is off", pack)   # the rendered marker line (drops the title)
        self.assertIn(boot.PRESENT_MARKER, pack)            # ...but the instruction still names it
        self.assertIn(boot.RELAY_MARKER, pack)              # ...and the governance alarm is INFORM-marked

    def test_must_push_carries_the_inform_marker_for_governance(self):
        items = boot.must_push(_signals(gate="off", reason="no pull request"))
        self.assertTrue(items)
        self.assertTrue(all(i.startswith(boot.RELAY_MARKER) for i in items),
                        "every must-push item carries the imperative relay marker")

    def test_routine_status_carries_no_inform_marker(self):
        # a healthy session pushes nothing; and the routine dashboard NEVER carries the imperative marker
        self.assertEqual(boot.must_push(_signals(gate="on")), [])
        dash = boot.render_dashboard(_signals(gate="off", reason="x", finding_count=2, register="u"))
        self.assertNotIn(boot.RELAY_MARKER, dash)

    def test_render_dashboard_is_clean_and_pure(self):
        # no AI-facing markers, carries the operator-toned facts, computes nothing (pure over the dict).
        dash = boot.render_dashboard(_signals(att_lines=["do X"], shipped=["#1 — a change"]))
        self.assertNotIn(boot.RELAY_MARKER, dash)
        self.assertNotIn("ENGINE BOOT BRIEFING", dash)
        self.assertIn("**Where we are:**", dash)
        self.assertIn("**Stance:**", dash)
        self.assertIn("- do X", dash)

    def test_present_marker_survives_a_dashboard_exception(self):
        # the marker line is emitted BEFORE the dashboard, so a dashboard failure can't suppress it.
        with mock.patch.object(boot, "gather_signals", return_value=_signals(gate="off", reason="x")), \
             mock.patch.object(boot, "render_dashboard", side_effect=Exception("boom")):
            pack = boot.assemble_pack()
        self.assertIn("⚠ Protected branch is off", pack)   # the present-marker line still rendered
        self.assertIn(boot.PRESENT_MARKER, pack)
        self.assertIn("couldn't be assembled", pack)        # the degraded dashboard fallback
        self.assertLess(pack.index("⚠ Protected branch is off"), pack.index("couldn't be assembled"),
                        "the marker is emitted BEFORE the dashboard, so a dashboard failure can't suppress it")


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

    def test_every_sessionstart_command_points_into_engine_and_uses_the_venv(self):
        for g in self.settings["hooks"]["SessionStart"]:
            for h in g["hooks"]:                             # boot's AND memory's co-registered sweep (3b)
                self.assertEqual(h["type"], "command")
                self.assertIn(".engine/", h["command"])      # the wiring guard
                self.assertIn("/.venv/", h["command"])        # the runtime interpreter, never bare python

    def test_boot_is_wired_exactly_once_on_every_start_source(self):
        # memory-substrate co-registers its consolidation sweep on the same SessionStart sources (slice 3b),
        # so not every command names boot — but boot must still be present exactly once per source.
        for g in self.settings["hooks"]["SessionStart"]:
            boot_cmds = [h for h in g["hooks"] if "tools/boot.py" in h["command"]]
            self.assertEqual(len(boot_cmds), 1, f"boot wired once on the '{g['matcher']}' source")

    def test_start_sources_exclude_compact_and_are_valid_events(self):
        self.assertNotIn("compact", boot.SESSION_START_SOURCES)
        self.assertIn("SessionStart", hooks.EVENT_INVENTORY)  # the wired event is a real one


class TestBlockBudgetLeg(unittest.TestCase):
    """The block-budget coherence leg (validate.block_budget_findings, run live in module_coherence).
    Slice 20 born it green-but-present; slice 21 registered modes' explore write-gate (PreToolUse) and
    slice 22 registers close's findings-disposition gate (Stop), so it now validates TWO real members —
    and still has teeth for a misplaced block."""

    def test_registry_has_both_block_members_and_leg_is_green(self):
        # The registry assembles each owning system's declaration: modes' explore write-gate on PreToolUse
        # (slice 21) and close's findings-disposition gate on Stop (slice 22) — both block-eligible, so the
        # leg stays green over the whole assembled set.
        registry = module_coherence.block_eligible_registrations()
        self.assertIn({"event": "PreToolUse", "name": "explore-write-gate", "owner": "modes"}, registry)
        self.assertIn({"event": "Stop", "name": "findings-disposition", "owner": "close"}, registry)
        # every declared block sits on a block-eligible event -> the leg produces no finding.
        self.assertEqual(validate.block_budget_findings(registry, "hard", "move it."), [])

    def test_leg_has_teeth_when_a_block_is_misplaced(self):
        msg = "move it."
        self.assertEqual(validate.block_budget_findings([], "hard", msg), [])  # green-but-present
        fired = validate.block_budget_findings(
            [{"event": "SessionStart", "name": "x", "owner": "modes"}], "hard", msg)
        self.assertEqual(len(fired), 1)                       # a block on an ineligible event fires
        self.assertIn("SessionStart", fired[0]["message"])
        clean = validate.block_budget_findings(
            [{"event": "Stop", "name": "findings-disposition", "owner": "close"}], "hard", msg)
        self.assertEqual(clean, [])                           # an eligible event is clean


class TestStanceLine(unittest.TestCase):
    """Slice 21 — boot clears the modes stance signal at SessionStart and names the current stance."""

    def test_pack_names_the_explore_stance(self):
        patchers = _offline()
        try:
            pack = boot.assemble_pack()
        finally:
            for p in patchers:
                p.stop()
        # at boot the stance is always Explore (the handler clears the signal first); boot places modes'
        # own stance copy (modes owns the vocabulary).
        self.assertIn(boot.modes.describe_stance("explore"), pack)
        self.assertIn("Exploring", pack)

    def test_pack_carries_the_assistant_facing_explore_scope_note(self):
        # The AI-facing briefing grounds the model on what Explore actually permits/denies, so a session
        # does not over-restrict itself (the bug: switching to Build merely to log a GitHub issue, which
        # Explore allows). modes owns the copy; boot places it. It must stay AI-facing only.
        patchers = _offline()
        try:
            pack = boot.assemble_pack()
        finally:
            for p in patchers:
                p.stop()
        note = boot.modes.describe_explore_scope()
        self.assertIn(note, pack)                       # the briefing carries the gate-scope grounding
        self.assertIn("don't relay", pack.lower())      # self-labelled so the AI does not relay it
        # the note stays OUT of the operator's own dashboard view — the operator surface is unchanged.
        self.assertNotIn(note, boot.render_dashboard(_signals()))

    def test_pack_carries_the_status_pull_cue(self):
        # The status verb is operator-typed (non-resident), so the AI's standing cue to run engine_status.py
        # verbatim when the operator asks where things stand must live in the boot pack (D-200/D-201). Pin the
        # distinctive command string so the cue can't silently degrade to a vague paraphrase instruction.
        patchers = _offline()
        try:
            pack = boot.assemble_pack()
        finally:
            for p in patchers:
                p.stop()
        self.assertIn("uv run --directory .engine -- python tools/engine_status.py", pack)
        self.assertIn("show its output verbatim", pack)

    def test_handler_clears_the_stance_for_this_session(self):
        # the handler's FIRST job is to clear the stance signal for the session id the payload carries,
        # so every session — including a resume — boots Explore and never inherits a prior Build signal.
        patchers = _offline()
        try:
            with mock.patch.object(boot.modes, "clear_stance") as clear:
                boot.handler({"session_id": "sess-xyz"})
        finally:
            for p in patchers:
                p.stop()
        clear.assert_called_once_with("sess-xyz")


if __name__ == "__main__":
    unittest.main()
