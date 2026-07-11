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

import audit_digest
import boot
import hooks
import module_coherence
import validate

DEPLOYED_FLOOR = os.path.join(validate.ROOT, "CLAUDE.deployed.md")
ROOT_CLAUDE = os.path.join(validate.ROOT, "CLAUDE.md")
SETTINGS_PATH = os.path.join(validate.ROOT, ".claude", "settings.json")


def _floor_text() -> str:
    """The deployed floor's text wherever it lives. In the construction repo the floor is CLAUDE.deployed.md
    (the root CLAUDE.md is the construction-governance file); in a GENERATED repo, first-run's swap-in (#272)
    makes the floor the root CLAUDE.md and removes CLAUDE.deployed.md. Read the floor from CLAUDE.deployed.md
    if present, else CLAUDE.md — so the present-marker contract is checked against the real floor in both, and
    the test never errors on an adopter's post-swap tree. Both paths are import-bound from validate.ROOT."""
    path = DEPLOYED_FLOOR if os.path.isfile(DEPLOYED_FLOOR) else ROOT_CLAUDE
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _offline():
    """Patch boot so no network is touched: no repo/token, a stable empty attention result, and a
    fixed recently-shipped digest. Returns a list of started patchers the caller stops."""
    patchers = [
        mock.patch.object(boot, "repo_slug", return_value=None),
        mock.patch.object(boot, "gh_token", return_value=None),
        mock.patch.object(boot, "recently_shipped", return_value=["#1 — a merged change"]),
        # No real git in offline tests: the work-in-hand focus derivation reads local git, so pin it empty
        # (a focused-read test opts back in by re-patching derive_focus with its own fixture).
        mock.patch.object(boot.attention, "derive_focus", return_value=([], 0)),
        # boot's rung-1 slice read touches the real .cache/graph; pin it absent so offline tests are hermetic
        # (source=None -> the reads run on knowledge_query exactly as before; threading is tested explicitly).
        mock.patch.object(boot.boot_slice, "read", return_value=None),
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
            "finding_fingerprint": None,
            "debt_count": 0, "debt_as_of": None, "att_lines": [],
            "att_degraded": [], "shipped": [], "stance": "Exploring", "strand": None,
            "behind_origin": None, "off_main": None,
            "pr_conflict": None, "restore_offer": None, "migration_revert": None, "audit_stale": None,
            "live_standing": None, "neighborhood": None, "map_rebuilt": False, "map_corrupt": False,
            "ledger_malformed": None}


def _signals(**over):
    s = dict(_SIGNALS)
    s.update(over)
    # Derive a fingerprint matching the count unless a test set one explicitly, so the count-based
    # collapse / worse / improvement tests exercise the identity-SET ledger value faithfully (gather_signals
    # couples them: a real count>=1 carries a real list; degraded -> both None).
    if "finding_fingerprint" not in over:
        n = s.get("finding_count") or 0
        s["finding_fingerprint"] = [f"#{i}" for i in range(n)] if n else None
    return s


class TestDegradedNotice(unittest.TestCase):
    """The 'I couldn't reach ... this session' notice fires ONLY on a real read failure (a non-empty degraded
    set), names the unreachable input(s) in plain words, and is ABSENT on a healthy boot. This is the fix for
    the permanent false 'couldn't rank by priority' caveat (telemetry was always in degraded_inputs)."""

    def test_healthy_boot_shows_no_degraded_notice(self):
        # Every substrate available -> no notice at all. The old caveat fired every session; it must not now.
        dash = boot.render_dashboard(_signals(att_degraded=False)).lower()
        self.assertNotIn("couldn't reach", dash)
        self.assertNotIn("couldn't rank", dash)                       # the old permanent wording is gone
        self.assertNotIn("priority order below may be incomplete", dash)
        self.assertNotIn("aren't wired up yet", dash)

    def test_unreachable_telemetry_is_named_in_plain_words(self):
        # A real failure to read the live debt register -> the notice names it concretely, with no jargon.
        dash = boot.render_dashboard(_signals(att_degraded=["telemetry"]))
        self.assertIn("I couldn't reach your open-problems list from GitHub this session", dash)
        self.assertIn("priority order below may be incomplete", dash)
        for jargon in ("telemetry", "substrate", "degraded_inputs", "ranking inputs"):
            self.assertNotIn(jargon, dash)

    def test_multiple_unreachable_inputs_join_in_plain_words(self):
        # degraded_inputs is sorted (git before telemetry); the names join as a readable clause.
        dash = boot.render_dashboard(_signals(att_degraded=["git", "telemetry"]))
        self.assertIn("your in-flight branches and pull requests and your open-problems list from GitHub", dash)

    def test_ranker_failure_does_not_leak_the_internal_name(self):
        # needs_attention reports ["attention"] when the ranker itself failed; the notice must name it in plain
        # words ("your work-priority ranking"), never leak the internal token "attention" into operator copy.
        dash = boot.render_dashboard(_signals(att_degraded=["attention"]))
        self.assertIn("I couldn't reach your work-priority ranking this session", dash)
        self.assertNotIn("I couldn't reach attention", dash)   # the internal noun must not reach the operator

    def test_live_rebuild_shows_a_distinct_heads_up_not_a_couldnt_reach(self):
        # When orientation ran on a LIVE rebuild (the committed graph.json is absent), the dashboard surfaces a
        # DISTINCT heads-up — inform + consequence, never the "couldn't reach" alarm: the map IS reachable, the
        # committed file is just missing. This is the operator-chosen separate signal for the graph-absent state.
        dash = boot.render_dashboard(_signals(map_rebuilt=True))
        self.assertIn("running on a rebuilt project map", dash)
        self.assertIn("regenerate it with", dash)                      # the fix is actionable...
        self.assertIn("knowledge_gen.py generate", dash)               # ...naming the canonical command,
        self.assertIn("commit the result", dash)                       # ...and that it must be committed
        self.assertNotIn("couldn't reach your project map", dash)       # NOT the unreachable alarm
        self.assertNotIn("couldn't reach", dash.lower())               # no degrade-alarm wording when only this

    def test_no_rebuild_heads_up_when_the_committed_map_is_present(self):
        # The normal case: committed map present (map_rebuilt False/absent) -> no rebuild heads-up at all.
        self.assertNotIn("rebuilt project map", boot.render_dashboard(_signals()))
        self.assertNotIn("rebuilt project map", boot.render_dashboard(_signals(map_rebuilt=False)))

    def test_rebuild_heads_up_and_couldnt_reach_can_coexist_distinctly(self):
        # A degraded substrate AND a live rebuild can fire together; the two read as separate advisories, the
        # rebuild line never folded into the "couldn't reach" clause (the conflation this whole change undoes).
        dash = boot.render_dashboard(_signals(att_degraded=["telemetry"], map_rebuilt=True))
        self.assertIn("I couldn't reach your open-problems list from GitHub this session", dash)
        self.assertIn("running on a rebuilt project map", dash)
        self.assertNotIn("couldn't reach your project map", dash)       # the map line stays the rebuild wording

    def test_corrupt_map_shows_a_damaged_heads_up_naming_the_right_repair(self):
        # map_corrupt (committed map PRESENT but unreadable) surfaces a distinct heads-up: it names the file
        # as DAMAGED (not missing — which would point at the wrong fix) and says regenerate REPLACES it.
        dash = boot.render_dashboard(_signals(map_corrupt=True))
        self.assertIn("running on a rebuilt project map", dash)
        self.assertIn("present but damaged", dash)
        self.assertIn("replace the damaged file", dash)
        self.assertIn("knowledge_gen.py generate", dash)               # the canonical command, committed
        self.assertNotIn("your committed map file is missing", dash)   # NOT the absent-map wording
        self.assertNotIn("couldn't reach", dash.lower())               # not the unreachable alarm

    def test_absent_and_corrupt_map_render_distinct_nouns(self):
        # The two live-rebuild causes never cross: absent -> "missing", damaged -> "present but damaged".
        absent = boot.render_dashboard(_signals(map_rebuilt=True))
        self.assertIn("your committed map file is missing", absent)
        self.assertNotIn("present but damaged", absent)
        corrupt = boot.render_dashboard(_signals(map_corrupt=True))
        self.assertIn("present but damaged", corrupt)
        self.assertNotIn("your committed map file is missing", corrupt)

    def test_a_rotting_ledger_shows_a_memory_health_heads_up_with_a_remedy(self):
        # #396 U07b: a positive unreadable-line count surfaces a peer-voice heads-up that reassures (rest of
        # recall intact) and names a remedy (the backup) — never a bare alarm a non-engineer can't act on.
        dash = boot.render_dashboard(_signals(ledger_malformed=3))
        self.assertIn("Your saved memory has 3 unreadable lines", dash)
        self.assertIn("the rest of your recall is intact", dash)
        self.assertIn("backup can restore a clean copy", dash)

    def test_the_memory_health_heads_up_is_singular_for_one_line(self):
        dash = boot.render_dashboard(_signals(ledger_malformed=1))
        self.assertIn("1 unreadable line.", dash)          # no plural 's'
        self.assertNotIn("1 unreadable lines", dash)

    def test_a_healthy_ledger_shows_no_memory_health_heads_up(self):
        # The normal state (0 / None) — and a torn-only ledger (gathered as 0) — surface nothing.
        for clean in (_signals(), _signals(ledger_malformed=0), _signals(ledger_malformed=None)):
            self.assertNotIn("unreadable line", boot.render_dashboard(clean))


class TestPresentMarker(unittest.TestCase):
    def test_marker_is_project_status_byte_identical_to_the_floor(self):
        # The locked present marker, and its byte-identical presence in the deployed floor (slice 19). The floor
        # is read wherever it lives — CLAUDE.deployed.md in this construction repo, the root CLAUDE.md in a
        # generated repo after first-run's swap-in (#272) removes CLAUDE.deployed.md — so the contract holds in
        # both and the test never errors on an adopter's post-swap tree.
        self.assertEqual(boot.PRESENT_MARKER, "Project status")
        floor = _floor_text()
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
    def setUp(self):
        p = mock.patch.object(boot.boot_slice, "read", return_value=None)   # hermetic: no real .cache read
        p.start()
        self.addCleanup(p.stop)

    def test_renders_attention_order_verbatim(self):
        # A partition whose ARRAY order is deliberately NOT precedence order: in_flight (precedence 2) appears
        # before blocking_debt (precedence 1). Boot must render in the GIVEN array order — proving it consumes
        # attention's ordering and never re-sorts by precedence_rank (relay, not re-rank). Both categories
        # render as action lines (orientation's standing-situation pointer is deliberately not surfaced —
        # see test_standing_situation_is_not_surfaced_as_an_action_line — so the order check uses these two).
        result = {"partition": [
            {"category": "in_flight", "precedence_rank": 2,
             "members": [{"id": "pr:99", "rank": 1}]},
            {"category": "blocking_debt", "precedence_rank": 1,
             "members": [{"id": "state:integration-debt", "rank": 1}]},
        ], "degraded_inputs": []}
        with mock.patch.object(boot.attention, "derive_focus", return_value=([], 0)), \
                mock.patch.object(boot.attention, "rank_live", return_value=result):
            lines, degraded, _ = boot.needs_attention({})
        self.assertEqual(degraded, [])
        self.assertEqual(len(lines), 2)
        # in_flight line first (it was first in the array), debt line second — array order preserved.
        self.assertIn("99", lines[0])                        # the in_flight pull request
        self.assertIn("integration debt", lines[1].lower())

    def test_standing_situation_is_not_surfaced_as_an_action_line(self):
        # The orientation standing-situation pointer is ranked (for the budget model) but NOT shown as an
        # action nudge: the live "Where we are" line (and its own stale-warning) already cover it, so a
        # separate "confirm where you stand" line would be redundant boilerplate every session.
        result = {"partition": [
            {"category": "orientation", "precedence_rank": 5,
             "members": [{"id": "state:standing-situation", "rank": 1}]},
        ], "degraded_inputs": []}
        with mock.patch.object(boot.attention, "derive_focus", return_value=([], 0)), \
                mock.patch.object(boot.attention, "rank_live", return_value=result):
            lines, _, _ = boot.needs_attention({})
        self.assertEqual(lines, [])   # no action line — the orientation pointer is not nagged

    def test_caps_members_per_category_without_reordering(self):
        # An ACTION category (recent_decisions) — structural_neighbors are now routed to the pack
        # neighborhood block, not the action lines, so the per-category cap is exercised on a category that
        # still renders as action lines.
        members = [{"id": f"k:{i}", "rank": i} for i in range(10)]
        result = {"partition": [{"category": "recent_decisions", "precedence_rank": 3,
                                 "members": members}], "degraded_inputs": []}
        with mock.patch.object(boot.attention, "derive_focus", return_value=([], 0)), \
                mock.patch.object(boot.attention, "rank_live", return_value=result):
            lines, _, _ = boot.needs_attention({})
        self.assertEqual(len(lines), boot.NEEDS_ATTENTION_CAP)  # a bounded prefix
        self.assertIn("0 (k)", lines[0])                        # member 0 first (the prefix, in order)
        self.assertIn(f"{boot.NEEDS_ATTENTION_CAP - 1} (k)", lines[-1])  # ...through member CAP-1

    def test_budget_size_governs_the_per_category_cap(self):
        # In a normal session boot passes a budget total, so each kind carries a budget_size — the policy's
        # reviewable share governs how many items it surfaces (the buried flat cap is retired). A kind whose
        # share the trim order shed under a tight budget carries budget_size 0 and so surfaces nothing.
        members = [{"id": f"k:{i}", "rank": i} for i in range(10)]
        result = {"partition": [
            {"category": "recent_decisions", "precedence_rank": 3, "budget_size": 2, "members": members},
            {"category": "in_flight", "precedence_rank": 2, "budget_size": 0,
             "members": [{"id": "pr:7", "rank": 1}]},
        ], "degraded_inputs": []}
        with mock.patch.object(boot.attention, "derive_focus", return_value=([], 0)), \
                mock.patch.object(boot.attention, "rank_live", return_value=result):
            lines, _, _ = boot.needs_attention({})
        self.assertEqual(len(lines), 2)                  # only the 2 budgeted recent_decisions items
        self.assertIn("0 (k)", lines[0])
        self.assertIn("1 (k)", lines[1])
        self.assertFalse(any("7" in ln for ln in lines))  # the budget_size-0 in_flight kind surfaces nothing


class TestFocusedNeighborhood(unittest.TestCase):
    """The orientation-time focused knowledge read (#37, D-224): a focus derived from the work in hand drives
    a BIDIRECTIONAL neighbourhood, rendered as an AI-facing block — PER SOURCE, by relationship, with the TRUE
    count disclosed when truncated — NOT operator action lines, and never an arbitrary capped few as if salient."""

    def setUp(self):
        # The direct needs_attention tests below don't go through _offline(); pin boot's rung-1 slice read
        # absent so they stay hermetic (source=None -> the knowledge_query path, exactly as before).
        p = mock.patch.object(boot.boot_slice, "read", return_value=None)
        p.start()
        self.addCleanup(p.stop)

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
        with mock.patch.object(boot.attention, "derive_focus", return_value=(["tool:attention"], 1)), \
                mock.patch.object(boot.attention, "rank_live", return_value=self._partition()), \
                mock.patch.object(boot.attention, "neighborhood_of", return_value=self._summary()):
            lines, degraded, nb = boot.needs_attention({})
        self.assertTrue(any("161" in ln for ln in lines))      # the in_flight item IS an action line
        self.assertFalse(any("core" in ln for ln in lines))    # the neighbours are NOT (they are the AI block)
        self.assertEqual(degraded, ["telemetry"])
        # the rich summary is carried to render, plus the true focus count for honest focus-truncation (#165)
        self.assertEqual(nb, {**self._summary(), "focus_total": 1})

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

    def test_focus_truncation_is_disclosed_too(self):
        # the SAME honesty one level up (#165): when more was changed than FOCUS_CAP shows, the header discloses
        # the true count, so the shown focus is never passed off as the whole change.
        summary = {"focus": ["tool:a", "tool:b", "tool:c", "tool:d", "tool:e"], "focus_total": 7, "groups": []}
        block = "\n".join(boot.render_neighborhood(summary))
        self.assertIn("You're touching: a, b, c, d, e (showing 5 of 7 you've changed).", block)

    def test_untruncated_focus_carries_no_count_noise(self):
        summary = {"focus": ["tool:a", "tool:b"], "focus_total": 2, "groups": []}
        block = "\n".join(boot.render_neighborhood(summary))
        self.assertIn("You're touching: a, b.", block)
        self.assertNotIn("you've changed", block)        # no truncation -> no disclosure clause

    def test_no_focus_or_no_groups_renders_cleanly(self):
        self.assertEqual(boot.render_neighborhood(None), [])
        self.assertEqual(boot.render_neighborhood({"focus": [], "groups": []}), [])
        bare = "\n".join(boot.render_neighborhood({"focus": ["tool:x"], "groups": []}))
        self.assertIn("You're touching: x", bare)                         # the focus is still named
        self.assertIn("nothing else is connected", bare.lower())          # neutral, no-jargon, not an alarm
        with mock.patch.object(boot.attention, "derive_focus", return_value=([], 0)), \
                mock.patch.object(boot.attention, "rank_live",
                                  return_value={"partition": [], "degraded_inputs": []}):
            _, _, nb = boot.needs_attention({})
        self.assertIsNone(nb)                                             # no work in hand -> no neighbourhood

    def test_pack_carries_the_neighborhood_block_when_focus_present(self):
        patchers = _offline()
        try:
            with mock.patch.object(boot.attention, "derive_focus", return_value=(["tool:attention"], 1)), \
                    mock.patch.object(boot.attention, "rank_live", return_value=self._partition()), \
                    mock.patch.object(boot.attention, "neighborhood_of", return_value=self._summary()):
                pack = boot.assemble_pack()
        finally:
            for p in patchers:
                p.stop()
        self.assertIn("knowledge neighborhood of your current work", pack)
        self.assertIn("You're touching: attention", pack)
        self.assertIn("attention is checked by", pack)

    def test_boot_reads_the_slice_once_and_threads_it_as_the_source(self):
        # boot's rung-1 boot-slice read (#37) is fetched ONCE and threaded into all three knowledge reads, so
        # orientation reads the gitignored cache, not the SQLite index. Re-patch read with a sentinel here
        # (setUp pinned it None) and assert every read received it.
        sentinel = object()
        with mock.patch.object(boot.boot_slice, "read", return_value=sentinel) as rd, \
                mock.patch.object(boot.attention, "derive_focus",
                                  return_value=(["tool:attention"], 1)) as df, \
                mock.patch.object(boot.attention, "rank_live", return_value=self._partition()) as rl, \
                mock.patch.object(boot.attention, "neighborhood_of", return_value=self._summary()) as no:
            boot.needs_attention({})
        rd.assert_called_once_with()                                   # one slice read for the whole pack
        self.assertIs(df.call_args.kwargs.get("source"), sentinel)
        self.assertIs(rl.call_args.kwargs.get("source"), sentinel)
        self.assertIs(no.call_args.kwargs.get("source"), sentinel)

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
        count, register = findings
        fp = [f"#{i}" for i in range(count)] if count else None   # open_findings now returns a 3rd value: the fingerprint
        patchers = _offline()
        try:
            with mock.patch.object(boot, "protected_branch_signal", return_value=gate), \
                 mock.patch.object(boot, "open_findings", return_value=(count, register, fp)), \
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

    def test_gate_off_dashboard_offers_the_built_fix_not_a_manual_repair(self):
        # #392 defect 1: the protection-off alarm must OFFER the already-built one-click fix, not hand a
        # non-engineer a settings walk-through or a false "an automated one-click fix is coming".
        dash = boot.render_dashboard(_signals(gate="off", reason="no required checks")).lower()
        self.assertIn("turn my safety gate back on", dash)   # the real consent handle
        self.assertNotIn("is coming", dash)                  # no false "a one-click fix is coming"
        self.assertNotIn("repository settings", dash)        # no manual-repair instruction

    def test_gate_off_full_relay_carries_the_fix_offer(self):
        # #392: the first-appearance spoken alarm (must_push / the full relay) carries the offer too, not
        # only the collapsed terse repeat.
        line = [l for l in boot.must_push(_signals(gate="off", reason="x")) if "safety gate" in l.lower()][0]
        self.assertIn("turn my safety gate back on", line.lower())

    def test_protected_branch_signal_three_states(self):
        # no repo/token -> unknown (never a false "on")
        self.assertEqual(boot.protected_branch_signal(None, None), ("unknown", None))
        # token present, ruleset fully in force -> on
        with mock.patch.object(boot.protection_guard, "get_json", return_value=[]), \
             mock.patch.object(boot.protection_guard, "missing_floor", return_value=[]):
            self.assertEqual(boot.protected_branch_signal("o/r", "t"), ("on", None))
        # token present, floor missing -> off (a nag)
        with mock.patch.object(boot.protection_guard, "get_json", return_value=[]), \
             mock.patch.object(boot.protection_guard, "missing_floor", return_value=["no pull request"]):
            state, reason = boot.protected_branch_signal("o/r", "t")
            self.assertEqual(state, "off")
            self.assertIn("no pull request", reason)
        # unreachable / auth failure -> unknown, never a false "on"
        with mock.patch.object(boot.protection_guard, "get_json", side_effect=Exception("boom")):
            self.assertEqual(boot.protected_branch_signal("o/r", "t"), ("unknown", None))
        # a 200 with a non-list body (an error object / null) is NOT a confirmation -> unknown, never "on"
        for body in ({"message": "Not Found"}, None, "nonsense"):
            with mock.patch.object(boot.protection_guard, "get_json", return_value=body):
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
                         "⚠ Your safety gate is off")

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


class TestBehindOriginSurfacing(unittest.TestCase):
    """The behind-origin tail (#335) is surfaced read-only at the strand tier (folder health, below the
    governance alarms), consequence-led and COUNT-FREE (the design's 'never a count' leaf law), with no git
    verbs and a concrete consent phrase. boot RELAYS; the assistant runs catch_up on consent."""
    # behind on the DEFAULT branch (#335): on_default True -> the original consequence copy. The branch-agnostic
    # side-line case (on_default False) is exercised in TestOffMainSurfacing below.
    _BEHIND = {"state": "behind", "main": "/p", "branch": "main", "current": "main", "on_default": True,
               "missing": 9, "latest": "2026-06-27", "advisory": "merged"}

    def test_render_surfaces_the_behind_line_only_when_behind(self):
        dash = boot.render_dashboard(_signals(behind_origin=self._BEHIND))
        self.assertIn("fallen behind", dash.lower())
        self.assertIn("2026-06-27", dash)                        # the felt date
        self.assertIn("bring it up to date", dash.lower())       # the concrete consent phrase
        self.assertIn("nothing you already have will be lost", dash.lower())
        self.assertNotIn("fallen behind", boot.render_dashboard(_signals(behind_origin=None)).lower())

    def test_behind_line_is_count_free_and_has_no_git_verbs(self):
        # the design's "never a count" + "git verbs never reach the operator surface" laws, on the actual line
        line = next(ln for ln in boot.render_dashboard(_signals(behind_origin=self._BEHIND)).splitlines()
                    if "fallen behind" in ln.lower())
        self.assertNotIn("9", line)                              # the missing-count never appears
        for verb in ("fast-forward", "ff-only", "fetch", "rebase", "ancestor", "origin/"):
            self.assertNotIn(verb, line.lower(), f"git verb leaked to the operator surface: {verb}")

    def test_behind_pins_below_the_governance_alarm_and_the_strand(self):
        pack = boot.render_dashboard(_signals(gate="off", reason="x",
                                              strand={"states": ["detached"], "main": "/p"},
                                              behind_origin=self._BEHIND))
        lines = [ln.lower() for ln in pack.splitlines()]
        gate = next(i for i, ln in enumerate(lines) if "safety gate is off" in ln)
        strand = next(i for i, ln in enumerate(lines) if "drifted into a broken state" in ln)
        behind = next(i for i, ln in enumerate(lines) if "fallen behind" in ln)
        self.assertLess(gate, behind, "the governance alarm must pin above the behind heads-up")
        self.assertLess(strand, behind, "a broken-state strand outranks the behind heads-up")

    def test_present_marker_reflects_behind_but_strand_and_governance_outrank(self):
        # on the DEFAULT branch the folder IS on its main line, only behind -> the headline says "fallen behind",
        # NOT "off your main line of work" (which would contradict the dashboard's on-default line). The off-main
        # headline is covered in TestOffMainSurfacing.
        self.assertIn("fallen behind your recent work",
                      boot.present_marker_line(_signals(behind_origin=self._BEHIND)))
        self.assertNotIn("isn't on your main line of work",
                         boot.present_marker_line(_signals(behind_origin=self._BEHIND)))
        self.assertEqual(boot.present_marker_line(_signals(behind_origin=None)),
                         f"{boot.PRESENT_MARKER}: all clear")
        # a strand (broken state) still wins the marker over a behind heads-up
        self.assertIn("needs attention",
                      boot.present_marker_line(_signals(strand={"states": ["detached"], "main": "/p"},
                                                        behind_origin=self._BEHIND)))

    def test_behind_is_not_in_the_must_push_set(self):
        # not governance-critical -> no INFORM marker (relayed via the dashboard heads-up, like the strand)
        self.assertEqual(boot.must_push(_signals(behind_origin=self._BEHIND)), [])

    def test_gather_signals_relays_the_detector_and_degrades_quietly(self):
        patchers = _offline()
        try:
            with mock.patch.object(boot.checkout_health, "detect_behind_origin", return_value=self._BEHIND):
                relayed = boot.gather_signals()
            with mock.patch.object(boot.checkout_health, "detect_behind_origin", side_effect=Exception("boom")):
                failed = boot.gather_signals()
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(relayed["behind_origin"], self._BEHIND)   # relayed verbatim
        self.assertIsNone(failed["behind_origin"])                 # a detector/network failure degrades to None


class TestOffMainSurfacing(unittest.TestCase):
    """The off-main Stage-1 signal (#342/D-275): the top-level checkout parked on a side line of work is
    surfaced read-only at the strand tier (folder health, below the governance alarms), as a GENTLE INVITATION
    (not a defect report), COUNT-FREE, with no git verbs and the one shared consent phrase. The firm Stage-2
    (behind on a side line) supersedes it, with a two-tone advisory and — on escalation — a named lineage."""
    _OFF_MAIN = {"state": "off-main", "main": "/p", "branch": "feature-x", "main_branch": "main"}
    # behind on a SIDE line of work (on_default False): the branch-agnostic Stage-2 escalation
    _BEHIND_SIDE = {"state": "behind", "main": "/p", "branch": "main", "current": "feature-x",
                    "on_default": False, "missing": 7, "latest": "2026-06-28", "advisory": "carries-work"}

    def test_render_surfaces_a_gentle_off_main_line_only_when_off_main(self):
        dash = boot.render_dashboard(_signals(off_main=self._OFF_MAIN))
        self.assertIn("side line of work", dash.lower())
        self.assertIn("bring it up to date", dash.lower())          # the shared consent phrase
        self.assertIn("nothing's at risk", dash.lower())            # a gentle invitation, not a defect report
        self.assertNotIn("side line of work", boot.render_dashboard(_signals(off_main=None)).lower())

    def test_off_main_line_is_count_free_and_has_no_git_verbs(self):
        line = next(ln for ln in boot.render_dashboard(_signals(off_main=self._OFF_MAIN)).splitlines()
                    if "side line of work" in ln.lower())
        self.assertNotIn("feature-x", line)                         # the raw branch name never leaks
        for verb in ("fast-forward", "ff-only", "fetch", "rebase", "ancestor", "origin/", "checkout", "branch"):
            self.assertNotIn(verb, line.lower(), f"git verb leaked to the operator surface: {verb}")

    def test_off_main_pins_below_the_governance_alarm_and_the_strand(self):
        pack = boot.render_dashboard(_signals(gate="off", reason="x",
                                              strand={"states": ["detached"], "main": "/p"},
                                              off_main=self._OFF_MAIN))
        lines = [ln.lower() for ln in pack.splitlines()]
        gate = next(i for i, ln in enumerate(lines) if "safety gate is off" in ln)
        strand = next(i for i, ln in enumerate(lines) if "drifted into a broken state" in ln)
        off = next(i for i, ln in enumerate(lines) if "side line of work" in ln)
        self.assertLess(gate, off, "the governance alarm must pin above the off-main invitation")
        self.assertLess(strand, off, "a broken-state strand outranks the off-main invitation")

    def test_behind_on_a_side_line_supersedes_the_gentle_off_main_line(self):
        # both live (parked on a side line AND missing merged work) -> the FIRM Stage-2 line, not the gentle one
        dash = boot.render_dashboard(_signals(off_main=self._OFF_MAIN, behind_origin=self._BEHIND_SIDE))
        self.assertIn("missing finished work", dash.lower())        # the firm escalation
        self.assertIn("2026-06-28", dash)                           # the felt date
        self.assertNotIn("nothing's at risk", dash.lower())         # the gentle line is gone
        self.assertIn("bring it up to date", dash.lower())          # still one consent phrase

    def test_side_line_behind_two_tone_keeps_unfinished_work_when_it_may_carry_some(self):
        # carries-work advisory -> the keep-your-work-safe tone (errs gentle)
        carries = boot.render_dashboard(_signals(behind_origin=self._BEHIND_SIDE)).lower()
        self.assertIn("keep it exactly where it is", carries)
        # merged advisory -> the only-an-older-view tone
        merged = boot.render_dashboard(_signals(behind_origin={**self._BEHIND_SIDE, "advisory": "merged"})).lower()
        self.assertIn("older view", merged)
        self.assertIn("nothing here is unsaved or lost", merged)

    def test_present_marker_reflects_off_main_but_governance_outranks(self):
        self.assertIn("isn't on your main line of work",
                      boot.present_marker_line(_signals(off_main=self._OFF_MAIN)))
        # a governance alarm still wins the marker
        self.assertIn("open engine finding",
                      boot.present_marker_line(_signals(finding_count=3, off_main=self._OFF_MAIN)))

    def test_marker_says_off_main_for_a_side_line_behind_but_fallen_behind_on_the_default(self):
        # the headline must match the state: off the main line (side-line behind) -> "isn't on your main line";
        # on the main line but behind (on_default) -> "fallen behind". The two must never be conflated (the
        # on-default case is regression-guarded in TestBehindOriginSurfacing).
        self.assertIn("isn't on your main line of work",
                      boot.present_marker_line(_signals(behind_origin=self._BEHIND_SIDE)))

    def test_off_main_is_not_in_the_must_push_set(self):
        # gentle folder health -> not governance-critical, no INFORM marker (relayed via the dashboard heads-up)
        self.assertEqual(boot.must_push(_signals(off_main=self._OFF_MAIN)), [])

    def test_gather_signals_relays_the_detector_and_degrades_quietly(self):
        patchers = _offline()
        try:
            with mock.patch.object(boot.checkout_health, "detect_off_main", return_value=self._OFF_MAIN):
                relayed = boot.gather_signals()
            with mock.patch.object(boot.checkout_health, "detect_off_main", side_effect=Exception("boom")):
                failed = boot.gather_signals()
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(relayed["off_main"], self._OFF_MAIN)       # relayed verbatim
        self.assertIsNone(failed["off_main"])                      # a detector failure degrades to None


class TestPrConflictSurfacing(unittest.TestCase):
    """#136: a pull request stranded on the two derived index files is surfaced read-only at the STRAND tier —
    pinned BELOW the governance alarms (a conflicting PR cannot reach protected `main`), carried on the
    always-visible present-marker (so it cannot rot unnoticed), and DELIBERATELY NOT in the must-push/INFORM
    set. boot OFFERS the one-step fix; the assistant runs pr_reconcile.reconcile on the operator's consent."""
    _PR = {"pr": 7, "title": "My pull request"}

    def test_render_surfaces_the_offer_only_when_a_pr_is_stuck(self):
        stuck = boot.render_dashboard(_signals(pr_conflict=self._PR))
        self.assertIn("can't be merged", stuck.lower())
        self.assertIn("no work is lost", stuck.lower())          # leads with the reassurance (PR-1 framing)
        self.assertIn("reconcile it", stuck.lower())             # names the one-step fix the operator says
        # offers to CHECK, never asserts the diagnosis / promises keep-both before assess has classified it
        self.assertIn("needs your decision", stuck.lower())
        self.assertNotIn("can't be merged", boot.render_dashboard(_signals(pr_conflict=None)).lower())

    def test_pr_conflict_pins_below_the_governance_alarm(self):
        pack = boot.render_dashboard(_signals(gate="off", reason="x", pr_conflict=self._PR))
        lines = pack.splitlines()
        gate = next(i for i, ln in enumerate(lines) if "safety gate is off" in ln.lower())
        pr = next(i for i, ln in enumerate(lines) if "can't be merged" in ln.lower())
        self.assertLess(gate, pr, "the governance alarm must pin above the stuck-PR heads-up")

    def test_present_marker_reflects_a_stuck_pr_but_governance_outranks(self):
        self.assertEqual(
            boot.present_marker_line(_signals(pr_conflict=self._PR)),
            f"⚠ {boot.PRESENT_MARKER}: a pull request is stuck — say 'reconcile it' and I'll look into clearing it")
        self.assertEqual(boot.present_marker_line(_signals(pr_conflict=None)),
                         f"{boot.PRESENT_MARKER}: all clear")
        # a governance alarm (and a strand) still outranks the stuck-PR marker
        self.assertEqual(boot.present_marker_line(_signals(gate="off", pr_conflict=self._PR)),
                         "⚠ Your safety gate is off")

    def test_pr_conflict_is_not_in_the_must_push_set(self):
        # not governance-critical -> no INFORM marker; the always-visible present-marker carries it instead.
        self.assertEqual(boot.must_push(_signals(pr_conflict=self._PR)), [])

    def test_gather_signals_relays_the_detector_and_degrades_quietly(self):
        patchers = _offline()
        try:
            with mock.patch.object(boot.pr_reconcile, "detect_conflict", return_value=self._PR):
                relayed = boot.gather_signals()
            with mock.patch.object(boot.pr_reconcile, "detect_conflict", side_effect=Exception("boom")):
                failed = boot.gather_signals()
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(relayed["pr_conflict"], self._PR)   # the detector's signal is relayed verbatim
        self.assertIsNone(failed["pr_conflict"])             # a detector failure degrades quietly to None


class TestRestoreOfferSurfacing(unittest.TestCase):
    """Slice 6b (Floor 3): when local memory is empty AND a backup is configured, boot surfaces a plain-language
    auto-restore OFFER — a recovery opportunity (NOT a ⚠ governance alarm), pinned BELOW the governance alarms,
    carried on the always-visible present-marker, and DELIBERATELY NOT in the must-push/INFORM set. boot OFFERS;
    the assistant runs restore_vault on the operator's consent. Memory owns the detector; boot owns the wording."""
    _OFFER = {"configured": True}

    def test_render_surfaces_the_offer_only_when_present(self):
        offered = boot.render_dashboard(_signals(restore_offer=self._OFFER))
        self.assertIn("restore my memory", offered.lower())
        self.assertIn("looks empty", offered.lower())
        self.assertIn("until you say so", offered.lower())       # the consent-first reassurance
        self.assertNotIn("restore my memory", boot.render_dashboard(_signals(restore_offer=None)).lower())

    def test_offer_pins_below_the_governance_alarm(self):
        pack = boot.render_dashboard(_signals(gate="off", reason="x", restore_offer=self._OFFER))
        lines = pack.splitlines()
        gate = next(i for i, ln in enumerate(lines) if "safety gate is off" in ln.lower())
        offer = next(i for i, ln in enumerate(lines) if "restore my memory" in ln.lower())
        self.assertLess(gate, offer, "the governance alarm must pin above the restore offer")

    def test_present_marker_reflects_the_offer_but_every_alarm_outranks(self):
        self.assertEqual(
            boot.present_marker_line(_signals(restore_offer=self._OFFER)),
            f"{boot.PRESENT_MARKER}: your saved memory looks empty — say 'restore my memory' and I'll try to bring "
            "back your backup")
        self.assertEqual(boot.present_marker_line(_signals(restore_offer=None)),
                         f"{boot.PRESENT_MARKER}: all clear")
        # a governance alarm AND a stuck PR both outrank the offer marker (it is ranked last)
        self.assertEqual(boot.present_marker_line(_signals(gate="off", restore_offer=self._OFFER)),
                         "⚠ Your safety gate is off")
        self.assertEqual(
            boot.present_marker_line(_signals(pr_conflict={"pr": 7}, restore_offer=self._OFFER)),
            f"⚠ {boot.PRESENT_MARKER}: a pull request is stuck — say 'reconcile it' and I'll look into clearing it")

    def test_offer_is_not_in_the_must_push_set(self):
        # a recovery opportunity, not governance-critical -> no INFORM marker; the present-marker carries it.
        self.assertEqual(boot.must_push(_signals(restore_offer=self._OFFER)), [])

    def test_gather_signals_relays_the_local_detector_and_degrades_quietly(self):
        patchers = _offline()
        try:
            from memory import restore_vault
            with mock.patch.object(restore_vault, "detect_restore_offer", return_value=self._OFFER):
                relayed = boot.gather_signals()
            with mock.patch.object(restore_vault, "detect_restore_offer", side_effect=Exception("boom")):
                failed = boot.gather_signals()
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(relayed["restore_offer"], self._OFFER)   # the local detector's signal is relayed verbatim
        self.assertIsNone(failed["restore_offer"])                # a detector/import failure degrades quietly to None


class TestMigrationRevertOffer(unittest.TestCase):
    """Slice 3 (D-264 floor a, #303): boot RELAYS memory's code-older-than-data detector as a one-action recovery
    OFFER, by plain handle (never the raw tag the signal carries), pinned below the governance alarms, carried on the
    present-marker, and NOT in must_push. boot OFFERS; the assistant runs memory.restore_pre_migration on consent."""
    _OFFER = {"store_label": "recall-ledger", "stamped": "2.0.0", "running": "1.0.0",
              "tag": "engine-snapshot/abc123/core-2.0.0"}

    def test_render_surfaces_the_offer_by_plain_handle_never_the_tag(self):
        offered = boot.render_dashboard(_signals(migration_revert=self._OFFER))
        self.assertIn("the copy saved before that update", offered.lower())
        self.assertIn("restore my memory from before the update", offered.lower())
        self.assertIn("until you say so", offered.lower())            # the consent-first reassurance
        # floor (a) / D-265 S1: the raw tag is opaque executor payload, never rendered to the operator
        self.assertNotIn("engine-snapshot/", offered)
        self.assertNotIn(self._OFFER["tag"], offered)
        self.assertNotIn("the copy saved before that update",
                         boot.render_dashboard(_signals(migration_revert=None)).lower())

    def test_offer_pins_below_the_governance_alarm(self):
        pack = boot.render_dashboard(_signals(gate="off", reason="x", migration_revert=self._OFFER))
        lines = pack.splitlines()
        gate = next(i for i, ln in enumerate(lines) if "safety gate is off" in ln.lower())
        offer = next(i for i, ln in enumerate(lines) if "before that update" in ln.lower())
        self.assertLess(gate, offer, "the governance alarm must pin above the recovery offer")

    def test_present_marker_reflects_the_offer_but_alarms_outrank_and_carries_no_tag(self):
        marker = boot.present_marker_line(_signals(migration_revert=self._OFFER))
        self.assertIn("ahead of the engine", marker)
        self.assertIn("restore my memory from before the update", marker)
        self.assertNotIn("engine-snapshot/", marker)                  # no raw tag on the marker either
        self.assertEqual(boot.present_marker_line(_signals(migration_revert=None)),
                         f"{boot.PRESENT_MARKER}: all clear")
        self.assertEqual(boot.present_marker_line(_signals(gate="off", migration_revert=self._OFFER)),
                         "⚠ Your safety gate is off")                 # a governance alarm outranks the offer

    def test_offer_is_not_in_the_must_push_set(self):
        self.assertEqual(boot.must_push(_signals(migration_revert=self._OFFER)), [])

    def test_gather_signals_relays_the_detector_and_degrades_quietly(self):
        patchers = _offline()
        try:
            from memory import restore_vault
            with mock.patch.object(restore_vault, "detect_migration_revert", return_value=self._OFFER):
                relayed = boot.gather_signals()
            with mock.patch.object(restore_vault, "detect_migration_revert", side_effect=Exception("boom")):
                failed = boot.gather_signals()
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(relayed["migration_revert"], self._OFFER)    # the detector's signal is relayed verbatim
        self.assertIsNone(failed["migration_revert"])                 # a detector/import failure degrades quietly to None


class TestAuditStaleness(unittest.TestCase):
    """audit-library 3c: boot RELAYS audit_digest's self-review freshness on the operator's return. A SOFT
    finding (hasn't-run-yet / has-gone-stale) surfaces gently in the needs-attention body — NEVER pinned, in
    the present-marker, or in must_push, so a never-armed repo still reads "all clear" and it never becomes a
    forced every-session alarm; a `note` (current) digest adds nothing; the read fails open to None."""

    def _never_run(self):
        # The REAL never-run finding from audit_digest (an absent digest path) — pins the actual relayed text,
        # so a future drift in that message is caught here, not only in test_audit_digest.
        return audit_digest.staleness(path="/no/such/audit-digest.md")

    def test_soft_advisory_surfaces_in_the_needs_attention_body(self):
        f = self._never_run()
        self.assertEqual(f["severity"], "soft")
        body = boot.render_dashboard(_signals(audit_stale=f))
        self.assertIn(f["message"], body)
        lines = body.splitlines()
        heading = next(i for i, ln in enumerate(lines) if ln.startswith("### Needs your attention"))
        msg = next(i for i, ln in enumerate(lines) if f["message"] in ln)
        self.assertGreater(msg, heading, "the self-review advisory belongs in the needs-attention body")

    def test_marker_stays_all_clear_and_advisory_is_not_force_relayed(self):
        # The acceptance criterion (Shane's "softer" choice): a never-armed repo — soft staleness, nothing
        # else wrong — still reads all-clear, and the assistant is NOT compelled to relay it (raised with
        # judgment via the needs-attention headline, never the forced governance-critical must_push set).
        s = _signals(audit_stale=self._never_run())
        self.assertEqual(boot.present_marker_line(s), f"{boot.PRESENT_MARKER}: all clear")
        self.assertEqual(boot.must_push(s), [])

    def test_a_stale_finding_renders_the_same_gentle_way(self):
        stale = validate.finding("soft", "STALE-SELF-REVIEW-MARKER: re-arm it", None)
        self.assertIn("STALE-SELF-REVIEW-MARKER", boot.render_dashboard(_signals(audit_stale=stale)))

    def test_a_current_digest_adds_no_line(self):
        fresh = validate.finding("note", "FRESH-MARKER: the self-review is current", None)
        body = boot.render_dashboard(_signals(audit_stale=fresh))
        self.assertNotIn("FRESH-MARKER", body)            # a `note` digest is silent — its silence is healthy
        self.assertIn("Nothing is blocking right now", body)

    def test_absent_signal_renders_clean_and_never_raises(self):
        # None (the degraded / not-read state) renders no advisory and never raises a KeyError on the subscript.
        self.assertIn("Nothing is blocking right now", boot.render_dashboard(_signals(audit_stale=None)))

    def test_gather_signals_relays_staleness_and_degrades_quietly(self):
        patchers = _offline()
        try:
            sentinel = validate.finding("soft", "RELAYED-STALENESS", None)
            with mock.patch.object(boot.audit_digest, "staleness", return_value=sentinel):
                relayed = boot.gather_signals()
            with mock.patch.object(boot.audit_digest, "staleness", side_effect=Exception("boom")):
                failed = boot.gather_signals()
                pack = boot.assemble_pack()
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(relayed["audit_stale"], sentinel)   # the detector's finding is relayed verbatim
        self.assertIsNone(failed["audit_stale"])             # a read failure degrades quietly to None
        _assert_ai_briefing(self, pack)                      # the pack still assembles on the failure path


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
                 mock.patch.object(boot.protection_guard, "get_json", return_value={"message": "x"}):
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
        self.assertEqual(boot.present_marker_line(_signals(gate="off")), "⚠ Your safety gate is off")

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
        self.assertIn("⚠ Your safety gate is off", pack)   # the rendered marker line (drops the title)
        self.assertIn(boot.PRESENT_MARKER, pack)            # ...but the instruction still names it
        self.assertIn(boot.RELAY_MARKER, pack)              # ...and the governance alarm is INFORM-marked

    def test_collapse_contract_bounds_the_relay_to_the_grounding_reply(self):
        # The AI-facing collapse contract must not just say HOW to render a collapsed alarm — it must bound
        # WHEN: a once-per-session act in this grounding reply, with no invented "boot check" preamble and no
        # re-surfacing of the "(unchanged since last session)" framing on later turns. This is the guard
        # against a model restapling the boot wrapper mid-session (the leak the operator caught).
        with mock.patch.object(boot, "gather_signals",
                               return_value=_signals(gate="off", reason="no pull request")):
            pack = boot.assemble_pack()
        self.assertIn("Relay each alarm once", pack)            # once-per-session bound
        self.assertIn("do not invent a 'boot check'", pack)    # no invented preamble
        self.assertIn("later turns of the same session", pack)  # no mid-session re-surfacing

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
        self.assertIn("⚠ Your safety gate is off", pack)   # the present-marker line still rendered
        self.assertIn(boot.PRESENT_MARKER, pack)
        self.assertIn("couldn't be assembled", pack)        # the degraded dashboard fallback
        self.assertLess(pack.index("⚠ Your safety gate is off"), pack.index("couldn't be assembled"),
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

    def test_pack_carries_the_standing_knowledge_faculty_note(self):
        # #92: a cold session must be told the wiring map exists and when to reach for it. _offline() leaves
        # NO work in hand (boot_slice.read -> None, so render_neighborhood is empty), so this also pins that
        # the line renders at a genuine cold boot — the actual value case — not piggybacking on the
        # work-gated #37 neighbourhood block.
        patchers = _offline()
        try:
            pack = boot.assemble_pack()
        finally:
            for p in patchers:
                p.stop()
        self.assertIn(boot.KNOWLEDGE_FACULTY_NOTE, pack)            # present even with no work in hand
        self.assertIn("knowledge-impact-check.md", pack)           # and it points at the runbook
        # AI-facing only: it stays OUT of the operator's own dashboard view (§12).
        self.assertNotIn(boot.KNOWLEDGE_FACULTY_NOTE, boot.render_dashboard(_signals()))

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


class TestAntiHabituationCollapse(unittest.TestCase):
    """D-269 — the standing-alarm collapse applied in the hook path (_relay_lines / assemble_pack
    use_ledger). An unchanged alarm collapses to a terse reminder that keeps its consequence + fix offer;
    a new/worsened one relays in full; the degrade-loud tells never collapse; and — the #313 grounding
    invariant — the present-marker line and the all-clear render NEVER collapse."""

    def setUp(self):
        # isolate the ledger in a tmp dir via the env override, so the collapse is exercised hermetically
        self.dir = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {boot.boot_alarm_ledger.ENV_DIR: self.dir})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_findings_alarm_collapses_when_unchanged_keeping_the_offer(self):
        s = _signals(finding_count=20, register="https://x/issues")
        first = boot._relay_lines(s)                                    # no ledger -> full (neutral)
        self.assertTrue(any("20 open engine finding" in l and "still" not in l.lower() for l in first))
        second = boot._relay_lines(s)                                   # same condition -> terse
        terse = [l for l in second if "finding" in l][0]
        self.assertIn("still", terse.lower())
        self.assertIn("review", terse.lower())                          # the offer is kept
        self.assertIn("issues", terse)                                  # the register link is kept

    def test_findings_worsening_relays_full_with_the_worse_label(self):
        boot._relay_lines(_signals(finding_count=20, register="u"))     # seed
        boot._relay_lines(_signals(finding_count=20, register="u"))     # collapse
        worse = boot._relay_lines(_signals(finding_count=25, register="u"))
        line = [l for l in worse if "finding" in l][0]
        self.assertNotIn("still", line.lower())
        self.assertIn("grown", line.lower())                            # the lexical "got worse" signal

    def test_findings_improvement_relays_full_not_a_stale_still(self):
        boot._relay_lines(_signals(finding_count=20, register="u"))     # seed
        better = boot._relay_lines(_signals(finding_count=17, register="u"))
        line = [l for l in better if "finding" in l][0]
        self.assertIn("17", line)                                       # the new (lower) number is shown
        self.assertNotIn("still", line.lower())                         # never collapsed to a stale count

    def test_findings_equal_count_different_set_relays_full_not_a_false_still(self):
        # #392 defect 3 / D-269 R19: a finding closing while a different one opens (SAME count, different
        # identities) is a real change — it must relay full, never mis-collapse to "unchanged". The bare-count
        # fingerprint could not tell these apart; the identity SET can.
        boot._relay_lines(_signals(finding_count=3, register="u",
                                   finding_fingerprint=["#1", "#2", "#3"]))            # seed (full)
        changed = boot._relay_lines(_signals(finding_count=3, register="u",
                                             finding_fingerprint=["#1", "#2", "#9"]))  # equal count, new set
        line = [l for l in changed if "finding" in l][0]
        self.assertNotIn("still", line.lower())                         # not mis-collapsed to a stale "still"
        self.assertNotIn("unchanged", line.lower())
        self.assertIn("3 open engine finding", line)                    # the neutral full first-appearance form

    def test_findings_old_int_ledger_degrades_to_full_never_crashes(self):
        # An operator upgrading from the bare-COUNT ledger has an INT on disk. The new list-valued fingerprint
        # must compare unequal (fail-toward-full) and _worse must NOT len() the int — _worse runs OUTSIDE
        # decide's try/except, so a crash here would suppress the WHOLE boot briefing every session.
        path = boot.boot_alarm_ledger.ledger_path()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"findings": {"value": 20, "shown_in_full": True}}, fh)
        lines = boot._relay_lines(_signals(finding_count=20, register="u"))   # list current vs int prior
        line = [l for l in lines if "finding" in l][0]
        self.assertNotIn("still", line.lower())        # not collapsed against the incompatible int prior
        self.assertNotIn("grown", line.lower())        # _worse guarded (int prior) -> neutral full, no crash
        self.assertIn("20 open engine finding", line)

    def test_gate_alarm_collapses_keeping_consequence_and_fix(self):
        s = _signals(gate="off", reason="no required checks")
        boot._relay_lines(s)                                            # seed (full)
        line = [l for l in boot._relay_lines(s) if "gate" in l.lower()][0]
        self.assertIn("still", line.lower())
        self.assertIn("turn my safety gate back on", line.lower())      # the REAL fix offer is kept (not a manual repair)
        self.assertIn("main", line.lower())                            # the consequence is kept

    def test_degrade_loud_tells_never_collapse(self):
        # a couldn't-verify gate and a refused cursor always render full, even on repeat (never softened)
        for over in (dict(gate="unknown"), dict(refused=True)):
            boot._relay_lines(_signals(**over))
            again = boot._relay_lines(_signals(**over))
            self.assertFalse(any("unchanged since last session" in l.lower() for l in again),
                             f"{over} must never collapse (degrade-loud)")

    def test_present_marker_never_collapses(self):
        # the #313 grounding invariant: the marker line is independent of the ledger and names the alarm
        # every session, even as the relay behind it collapses.
        s = _signals(finding_count=7, register="u")
        boot._relay_lines(s); boot._relay_lines(s)                      # the relay collapses on the repeat
        self.assertEqual(boot.present_marker_line(s),
                         f"⚠ {boot.PRESENT_MARKER}: 7 open engine finding(s) to review")

    def test_all_clear_never_collapses(self):
        self.assertEqual(boot._relay_lines(_signals(gate="on")), [])    # no eligible alarms -> empty relay
        self.assertEqual(boot.present_marker_line(_signals(gate="on")),
                         f"{boot.PRESENT_MARKER}: all clear")

    def test_hook_path_collapses_but_the_fresh_pack_cli_does_not(self):
        with mock.patch.object(boot, "gather_signals", return_value=_signals(gate="off", reason="x")):
            first = boot.assemble_pack(use_ledger=True)                 # the real hook path
            second = boot.assemble_pack(use_ledger=True)
            fresh = boot.assemble_pack()                                # the `pack` debug CLI (no ledger)
        self.assertIn("their safety gate is off", first)               # full on first
        self.assertIn("still off", second.lower())                     # terse on the repeat
        self.assertIn("their safety gate is off", fresh)               # the fresh render never collapses
        self.assertNotIn("still off", fresh.lower())

    # --- the gentle off-main signal collapses through the SAME single decide() call (#342, blocking B2) ---
    _OM = {"state": "off-main", "main": "/p", "branch": "feature-x", "main_branch": "main"}

    def test_off_main_collapses_to_terse_when_unchanged(self):
        s = _signals(off_main=dict(self._OM))
        boot._relay_lines(s)                                # fresh ledger -> full (neutral)
        self.assertFalse(s["off_main"]["collapsed"])
        self.assertIn("nothing's at risk", boot.render_dashboard(s).lower())
        boot._relay_lines(s)                                # same condition -> collapse
        self.assertTrue(s["off_main"]["collapsed"])
        terse = boot.render_dashboard(s).lower()
        self.assertIn("unchanged since last session", terse)
        self.assertIn("bring it up to date", terse)         # the offer is kept in the terse line

    def test_off_main_renders_full_when_no_collapse_flag_is_set(self):
        # the pure status-verb path never runs _relay_lines -> the off-main line renders FULL (fail-toward-full)
        dash = boot.render_dashboard(_signals(off_main=dict(self._OM))).lower()
        self.assertIn("nothing's at risk", dash)
        self.assertNotIn("unchanged since last session", dash)

    def test_off_main_collapse_coexists_with_the_governance_baselines(self):
        # the single decide() call must collapse off-main WITHOUT dropping the gate/findings ledger entries
        s = _signals(gate="off", reason="x", finding_count=4, register="u", off_main=dict(self._OM))
        boot._relay_lines(s)                                # seed gate + findings + off-main together
        lines = boot._relay_lines(s)                        # repeat -> all three collapse, none dropped
        self.assertTrue(any("still off" in l.lower() for l in lines), "gate baseline must survive")
        self.assertTrue(any("still" in l.lower() and "finding" in l.lower() for l in lines),
                        "findings baseline must survive")
        self.assertTrue(s["off_main"]["collapsed"], "off-main collapses on the same pass")

    def test_off_main_escalating_to_behind_relays_the_firm_line_with_its_lineage(self):
        side_behind = {"state": "behind", "main": "/p", "branch": "main", "current": "feature-x",
                       "on_default": False, "missing": 5, "latest": "2026-06-28", "advisory": "carries-work"}
        boot._relay_lines(_signals(off_main=dict(self._OM)))     # session 1: gentle park (seed)
        boot._relay_lines(_signals(off_main=dict(self._OM)))     # session 2: still gentle (collapse)
        s = _signals(off_main=dict(self._OM), behind_origin=side_behind)
        boot._relay_lines(s)                                     # session 3: now also behind -> worsened
        self.assertTrue(s["off_main"]["worsened"])
        self.assertIn("flagged earlier", boot.render_dashboard(s).lower())   # the named lineage

    def test_off_main_first_full_relay_after_an_established_ledger_carries_the_disclosure_note(self):
        # an established ledger (earlier sessions ran) that never saw off-main -> the first off-main full relay
        # explains the new check, so a folder reported healthy before isn't silently re-cast as freshly broken
        boot._relay_lines(_signals(finding_count=3, register="u"))   # seed the ledger from a prior session
        boot._relay_lines(_signals(finding_count=3, register="u"))   # a second prior session (ledger is real now)
        s = _signals(finding_count=3, register="u", off_main=dict(self._OM))
        boot._relay_lines(s)
        self.assertTrue(s["off_main"]["first_sighting"])
        self.assertIn("newer check", boot.render_dashboard(s).lower())

    def test_off_main_disclosure_note_does_not_repeat_once_seen(self):
        boot._relay_lines(_signals(finding_count=3, register="u"))
        boot._relay_lines(_signals(finding_count=3, register="u"))
        s = _signals(finding_count=3, register="u", off_main=dict(self._OM))
        boot._relay_lines(s)                                 # first off-main full relay (disclosure shown)
        boot._relay_lines(s)                                 # repeat -> collapse, no disclosure
        self.assertTrue(s["off_main"]["collapsed"])
        self.assertNotIn("newer check", boot.render_dashboard(s).lower())


if __name__ == "__main__":
    unittest.main()
