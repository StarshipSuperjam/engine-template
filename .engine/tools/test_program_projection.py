#!/usr/bin/env python3
"""Tests for program_projection — the read-only portfolio (and, later, PROGRAM.md).

The portfolio answers the operator's actual question: which programs are open, what each is FOR in one
plain line, and how far along each is — as facts, never a percentage and never a recommendation. The
byte-pin fixtures hold the render EXACTLY over a seeded shelf whose objectives copy the live shelf's
real shapes (a single ~490-character sentence, a 322-character opening sentence with an inline list),
so the 180-character headline bound is exercised against the cases that defeat a naive first-sentence
rule. The rest pin the honesty properties one at a time: unknown never renders as done, a program whose
children are all closed says so rather than reading finished, a damaged record is shown as needing
attention rather than blanking the shelf, the closed tail caps at five by recency with a program_id
tie-break, and the whole verb is a pure read that reproduces byte-for-byte.
"""
from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import moment
import plan_program
import plan_store
import program_projection

from test_plan_store import _document

# The live shelf's hard cases, copied in shape: a single long sentence with no early period, and an
# opening sentence carrying an inline list. "The first sentence" is not a bound for either — the cap is.
LONG_490 = ("Give the operator a single durable place to see, reason about and act on every long-running "
            "program the engine is carrying at once, so that the state of the whole portfolio is legible "
            "from one command rather than reconstructed by opening each program in turn and holding the "
            "differences in your head across a dozen separate reads that never line up the same way twice "
            "no matter how carefully you try to keep them straight in a single sitting today")
INLINE_322 = ("Deliver the review capability in three parts: the finder that surfaces candidates, the "
              "adjudicator that disposes each one, and the recorder that writes the outcome, so that a "
              "review is a first-class artifact with a durable trail rather than a conversation that "
              "evaporates the moment the window closes and nobody can say what was decided or why later")


class _Clock:
    """A settable clock, so a seeded shelf can carry the distinct timestamps the tail-ordering and
    last-movement facts are read from."""

    def __init__(self, at="2026-08-01T00:00:00Z"):
        self.at = at

    def now(self):
        return self.at


class _Shelf(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lib = plan_store.PlanLibrary(Path(self._tmp.name) / "plans")
        self.progs = plan_program.ProgramLibrary(self.lib)
        self.clock = _Clock()
        self._counter = 0

    def _mint(self):
        self._counter += 1
        return "prg_%012x" % (0x100 + self._counter)

    @contextlib.contextmanager
    def _seeding(self):
        with mock.patch.object(moment, "utc_now", self.clock.now), \
                mock.patch.object(plan_store, "_now", self.clock.now), \
                mock.patch.object(plan_program, "_now", self.clock.now), \
                mock.patch.object(plan_program, "mint_program_id", self._mint):
            yield

    def _seed_child(self, plan_id, title, program_id, *, predecessor=None, obligations=None,
                    closure=None, binding=False):
        doc = _document(plan_id=plan_id, title=title)
        program = {"program_id": program_id}
        if predecessor:
            program["predecessor_plan_id"] = predecessor
        if obligations is not None:
            program["carried_obligations"] = obligations
        doc["program"] = program
        self.lib.create(doc)
        if closure:
            self.lib.update_record(self.lib.resolve(plan_id), lambda r: r.update({"closure": closure}))
        if binding:
            self.lib.update_record(self.lib.resolve(plan_id), lambda r: r.update({"build_binding": {
                "sealed_digest": "sha256:" + "a" * 64, "build_plan_digest": "sha256:" + "b" * 64,
                "at": "2026-08-25T00:00:00Z", "repository": "o/r", "pull_request": 9}}))

    def _corrupt(self, slug, text="{ not json"):
        (self.lib.root / "programs" / slug / "record.json").write_text(text, encoding="utf-8")


def _build_main_shelf(shelf):
    """Three open programs: an active chain mid-flight carrying a debt (long-sentence objective), a
    program whose one child has landed but which is not recorded complete, and a lane-split program
    (inline-list objective). No closed programs, so the tail is absent here."""
    with shelf._seeding():
        shelf.clock.at = "2026-08-02T00:00:00Z"
        sa = shelf.progs.create("Alpha", LONG_490)
        pid_a = shelf.progs.read(sa)["program_id"]
        shelf.clock.at = "2026-08-03T00:00:00Z"
        shelf._seed_child("pln_a00000000001", "Alpha — 1: foundations", pid_a,
                          obligations=[{"id": "OB-1", "statement": "carry the cutover", "state": "carried"}],
                          closure={"state": "complete", "at": "2026-08-04T00:00:00Z", "reason": "merged"})
        shelf.progs.add_child(sa, "pln_a00000000001")
        shelf.clock.at = "2026-08-05T00:00:00Z"
        shelf._seed_child("pln_a00000000002", "Alpha — 2: the feature", pid_a,
                          predecessor="pln_a00000000001",
                          obligations=[{"id": "OB-1", "statement": "cut over", "state": "carried"}],
                          binding=True)
        shelf.progs.add_child(sa, "pln_a00000000002", predecessor="pln_a00000000001")

        shelf.clock.at = "2026-08-06T00:00:00Z"
        sb = shelf.progs.create("Bravo", "Deliver Bravo in two PRs.")
        pid_b = shelf.progs.read(sb)["program_id"]
        shelf._seed_child("pln_b00000000001", "Bravo — 1", pid_b,
                          closure={"state": "complete", "at": "2026-08-07T00:00:00Z", "reason": "merged"})
        shelf.progs.add_child(sb, "pln_b00000000001")

        shelf.clock.at = "2026-08-08T00:00:00Z"
        sc = shelf.progs.create("Charlie", INLINE_322)
        pid_c = shelf.progs.read(sc)["program_id"]
        shelf._seed_child("pln_c00000000001", "Charlie — finder", pid_c, binding=True)
        shelf.progs.add_child(sc, "pln_c00000000001")
        shelf._seed_child("pln_c00000000002", "Charlie — recorder", pid_c,
                          predecessor="pln_c00000000001", binding=True)
        shelf.progs.add_child(sc, "pln_c00000000002", predecessor="pln_c00000000001")
        shelf.clock.at = "2026-08-09T00:00:00Z"
        shelf.progs.set_lanes(sc, [{"name": "finding", "children": ["pln_c00000000001"]},
                                   {"name": "recording", "children": ["pln_c00000000002"]}],
                              "they touch different files")


EXPECTED_MAIN = '# Programs — portfolio\n\n<!-- generated from the program records; a read-only view — nothing here selects, starts, or advances work -->\n\nA shelf, not a queue: every OPEN program below, what it is for, how far along it is, and what is in flight — as facts, never a percentage and never a recommendation. Unknown is unknown, not done.\n\n## In flight (3)\n\n### Alpha\n- **Goal**: Give the operator a single durable place to see, reason about and act on every long-running program the engine is carrying at once…\n- **Program**: `prg_000000000101` · status active (derived)\n- **Last movement**: 2026-08-05\n- **In flight**: 2: the feature\n- **Settled on the chain**: 1 landed\n- **Obligations**: OB-1 — cut over\n\n### Bravo\n- **Goal**: Deliver Bravo in two PRs.\n- **Program**: `prg_000000000102` · status children-complete (derived)\n- **Last movement**: 2026-08-06\n- **In flight**: nothing — every live child has landed, but no one has recorded the PROGRAM complete; unwritten successors are unknown, not done\n- **Settled on the chain**: 1 landed\n- **Obligations**: none outstanding\n\n### Charlie\n- **Goal**: Deliver the review capability in three parts: the finder that surfaces candidates, the adjudicator that disposes each one, and the recorder that writes the outcome…\n- **Program**: `prg_000000000103` · status active (derived)\n- **Last movement**: 2026-08-09\n- **In flight**: finder; recorder\n- **Obligations**: none outstanding\n- **Lanes** — decided 2026-08-09:\n  - **finding**: in flight finder\n  - **recording**: in flight recorder\n  - Cross-lane merge-order risk: 1 edge(s) — see `program show`\n'


class ThePortfolioRendersTheOpenShelf(_Shelf):
    def test_the_render_is_pinned_byte_for_byte(self):
        _build_main_shelf(self)
        self.assertEqual(program_projection.render_portfolio(self.progs), EXPECTED_MAIN)

    def test_the_headline_is_capped_at_a_word_boundary_with_an_ellipsis(self):
        _build_main_shelf(self)
        render = program_projection.render_portfolio(self.progs)
        for line in render.splitlines():
            if line.startswith("- **Goal**: "):
                headline = line[len("- **Goal**: "):]
                self.assertLessEqual(len(headline), program_projection._HEADLINE_CAP)
                if headline.endswith("…"):
                    self.assertFalse(headline[:-1].endswith(" "), "cut left a dangling space")

    def test_no_ratio_or_percentage_or_recommendation_voice_in_the_program_blocks(self):
        _build_main_shelf(self)
        # Scope past the intro (which honestly SAYS "never a percentage and never a recommendation") to
        # the program blocks and the closed tail, where a completion ratio or a next-move voice would
        # actually appear.
        import re
        body = program_projection.render_portfolio(self.progs).split("## In flight", 1)[1].lower()
        self.assertNotIn("%", body)
        self.assertNotIn("percent", body)
        self.assertIsNone(re.search(r"\d+\s*/\s*\d+", body), "a completion ratio slipped into the render")
        for banned in ("you should", "recommend", "next up", "we suggest", "consider running"):
            self.assertNotIn(banned, body)

    def test_a_program_whose_children_all_landed_reads_as_unknown_not_done(self):
        _build_main_shelf(self)
        render = program_projection.render_portfolio(self.progs)
        block = render.split("### Bravo", 1)[1].split("###", 1)[0]
        self.assertIn("children-complete", block)
        self.assertIn("unknown, not done", block)
        self.assertNotIn("done.", block.replace("not done", ""))

    def test_the_render_is_byte_stable_across_two_reads(self):
        _build_main_shelf(self)
        first = program_projection.render_portfolio(self.progs)
        second = program_projection.render_portfolio(self.progs)
        self.assertEqual(first, second)

    def test_the_portfolio_is_a_pure_read(self):
        _build_main_shelf(self)
        programs_dir = self.lib.root / "programs"
        before = {p: p.read_bytes() for p in programs_dir.rglob("record.json")}
        program_projection.render_portfolio(self.progs)
        after = {p: p.read_bytes() for p in programs_dir.rglob("record.json")}
        self.assertEqual(before, after, "rendering the portfolio must not touch a record")


EXPECTED_FOUR_LANE_BLOCK = (
    "- **Lanes** — decided 2026-08-12:\n"
    "  - **docs**: in flight RC docs pass\n"
    "  - **core**: in flight RC core repair; RC core hardening\n"
    "  - **tests**: in flight RC test coverage · 1 landed\n"
    "  - **cli**: in flight RC cli surface\n"
    "  - Cross-lane merge-order risk: 3 edge(s) — see `program show`")


class ThePortfolioRendersLaneStanding(_Shelf):
    """The per-lane glance in the portfolio (issue StarshipSuperjam/engine-template#1173), a bounded
    formatter over plan_program.lane_standing. The centrepiece is the operator's own lived pattern —
    a backlog split into concurrent lanes by file territory, one session per lane — byte-pinned; the
    rest pin the bounds and the honesty rules one at a time."""

    def _four_lane_program(self):
        D, C1, C2 = "pln_0000000000d1", "pln_0000000000c1", "pln_0000000000c2"
        T1, T2, L1 = "pln_0000000000a1", "pln_0000000000a2", "pln_0000000000e1"
        with self._seeding():
            self.clock.at = "2026-08-10T00:00:00Z"
            slug = self.progs.create(
                "Release backlog",
                "Clear the release-candidate debt across concurrent lanes drawn by file territory.")
            pid = self.progs.read(slug)["program_id"]
            rows = [(D, "RC docs pass", None, None), (C1, "RC core repair", D, None),
                    (C2, "RC core hardening", C1, None),
                    (T1, "RC test determinism", C2,
                     {"state": "complete", "at": "2026-08-11T00:00:00Z", "reason": "merged"}),
                    (T2, "RC test coverage", T1, None), (L1, "RC cli surface", T2, None)]
            for cpid, title, pred, closure in rows:
                self._seed_child(cpid, title, pid, predecessor=pred, closure=closure)
                self.progs.add_child(slug, cpid, predecessor=pred)
            self.clock.at = "2026-08-12T00:00:00Z"
            self.progs.set_lanes(slug, [{"name": "docs", "children": [D]},
                                        {"name": "core", "children": [C1, C2]},
                                        {"name": "tests", "children": [T1, T2]},
                                        {"name": "cli", "children": [L1]}],
                                 "four lanes by file territory")
        return slug

    def _lanes_block(self, render):
        return render[render.index("- **Lanes**"):].rstrip()

    def test_the_four_lane_lived_pattern_is_pinned(self):
        self._four_lane_program()
        render = program_projection.render_portfolio(self.progs)
        self.assertEqual(self._lanes_block(render), EXPECTED_FOUR_LANE_BLOCK)

    def test_no_standing_split_renders_no_lane_section(self):
        with self._seeding():
            slug = self.progs.create("Solo", "One lane's worth of work, never split.")
            pid = self.progs.read(slug)["program_id"]
            self._seed_child("pln_0000000000b1", "the only child", pid)
            self.progs.add_child(slug, "pln_0000000000b1")
        render = program_projection.render_portfolio(self.progs)
        self.assertNotIn("- **Lanes**", render)

    def test_in_flight_members_beyond_the_cap_fold_to_a_count(self):
        with self._seeding():
            slug = self.progs.create("Wide", "One lane holding more live children than the cap names.")
            pid = self.progs.read(slug)["program_id"]
            kids = [f"pln_00000000f0{i}0" for i in range(4)]
            prev = None
            for cpid in kids:
                self._seed_child(cpid, f"child {cpid[-4:]}", pid, predecessor=prev)
                self.progs.add_child(slug, cpid, predecessor=prev)
                prev = cpid
            self.progs.set_lanes(slug, [{"name": "everything", "children": kids}], "all in one lane")
        block = self._lanes_block(program_projection.render_portfolio(self.progs))
        self.assertIn("(+1 more)", block)     # 4 in flight, cap 3

    def test_unlaned_children_beyond_the_cap_fold_to_a_count(self):
        with self._seeding():
            slug = self.progs.create("Sparse", "Only one child is laned; the rest are unlaned.")
            pid = self.progs.read(slug)["program_id"]
            kids = [f"pln_00000000e0{i}0" for i in range(5)]
            prev = None
            for cpid in kids:
                self._seed_child(cpid, f"child {cpid[-4:]}", pid, predecessor=prev)
                self.progs.add_child(slug, cpid, predecessor=prev)
                prev = cpid
            self.progs.set_lanes(slug, [{"name": "only", "children": [kids[0]]}], "lane just the first")
        block = self._lanes_block(program_projection.render_portfolio(self.progs))
        self.assertIn("Unlaned children:", block)
        self.assertIn("(+1 more)", block)     # 4 unlaned, cap 3

    def test_more_lanes_than_the_cap_fold_to_a_count(self):
        from test_plan_program import force_lane_split
        with self._seeding():
            slug = self.progs.create("Many", "More lanes than the glance names.")
            pid = self.progs.read(slug)["program_id"]
            self._seed_child("pln_0000000000b1", "sole real child", pid)
            self.progs.add_child(slug, "pln_0000000000b1")
        # Six lanes, most naming a member no longer in the program — forged past set_lanes, which
        # would refuse them — so the lane-count fold is exercised without needing six real children.
        lanes = [{"name": "real", "children": ["pln_0000000000b1"]}]
        lanes += [{"name": f"lane{i}", "children": [f"pln_00000000cc0{i}"]} for i in range(5)]
        force_lane_split(self.progs, slug, lanes)
        block = self._lanes_block(program_projection.render_portfolio(self.progs))
        self.assertIn("(+1 more lane(s) — see `program show`)", block)   # 6 lanes, cap 5

    def test_a_member_no_longer_in_the_program_is_disclosed_as_unknown_never_zero(self):
        from test_plan_program import force_lane_split
        with self._seeding():
            slug = self.progs.create("Departed", "A split naming a child that left the program.")
            pid = self.progs.read(slug)["program_id"]
            self._seed_child("pln_0000000000b1", "still here", pid)
            self.progs.add_child(slug, "pln_0000000000b1")
        # Forged through the allowlisted helper: this module never touches the raw lane record keys.
        force_lane_split(self.progs, slug,
                         [{"name": "lane", "children": ["pln_0000000000b1", "pln_00000000dead"]}])
        block = self._lanes_block(program_projection.render_portfolio(self.progs))
        self.assertIn("1 unknown", block)
        self.assertNotIn("0 unknown", block)
        self.assertNotIn("0 settled", block)

    def test_settled_members_fold_to_per_state_counts_and_an_unreachable_marked_one_stays_unknown(self):
        # The two halves of one honesty rule, at the portfolio surface. A landed member and a validly
        # superseded member fold to per-state counts in the program-wide settled line's own voice —
        # never a lump of "settled" that hides whether work merged or died. And a member that is both
        # unreachable and marked superseded is disclosed as unknown: an unreachable plan may not
        # vouch for its own settled end, however its marker reads.
        from test_plan_program import force_lane_split
        with self._seeding():
            slug = self.progs.create("Mixed", "One lane carrying every settled shape at once.")
            pid = self.progs.read(slug)["program_id"]
            self._seed_child("pln_0000000000b1", "landed work", pid,
                             closure={"state": "complete", "at": "2026-08-11T00:00:00Z",
                                      "reason": "merged"})
            self.progs.add_child(slug, "pln_0000000000b1")
            self._seed_child("pln_0000000000b2", "replaced work", pid,
                             predecessor="pln_0000000000b1",
                             closure={"state": "retired", "at": "2026-08-11T00:00:00Z",
                                      "reason": "superseded"})
            self.progs.add_child(slug, "pln_0000000000b2", predecessor="pln_0000000000b1")
        record = self.progs.read(slug)
        for child in record["children"]:
            if child["plan_id"] == "pln_0000000000b2":
                child["superseded_by"] = "pln_0000000000b1"
        # A stored child whose plan is gone from the library, marked superseded by a real child: the
        # marker validates, the plan does not read. Appended directly (add_child needs a readable
        # plan) and laned through the allowlisted forge (set_lanes rightly refuses the unreadable).
        record["children"].append({"plan_id": "pln_00000000dead", "position": 3,
                                   "added_at": "2026-08-11T00:00:00Z",
                                   "predecessor_plan_id": "pln_0000000000b2",
                                   "superseded_by": "pln_0000000000b1"})
        self.progs._write(slug, record)
        force_lane_split(self.progs, slug,
                         [{"name": "lane", "children": ["pln_0000000000b1", "pln_0000000000b2",
                                                        "pln_00000000dead"]}])
        block = self._lanes_block(program_projection.render_portfolio(self.progs))
        self.assertIn("1 landed, 1 superseded", block)
        self.assertIn("1 unknown", block)
        self.assertNotIn("settled", block)   # the lump never renders — only per-state counts do

    def test_the_lane_section_carries_no_recommendation_voice(self):
        self._four_lane_program()
        block = self._lanes_block(program_projection.render_portfolio(self.progs)).lower()
        for banned in ("you should", "recommend", "next up", "we suggest", "consider", "priority"):
            self.assertNotIn(banned, block)

    def test_rendering_lane_standing_does_not_touch_a_record(self):
        self._four_lane_program()
        programs_dir = self.lib.root / "programs"
        before = {p: p.read_bytes() for p in programs_dir.rglob("record.json")}
        program_projection.render_portfolio(self.progs)
        after = {p: p.read_bytes() for p in programs_dir.rglob("record.json")}
        self.assertEqual(before, after)


class GoalHeadlineCutsOnACompleteThought(unittest.TestCase):
    """goal_headline directly, at the boundaries the live shelf actually defeats. The portfolio tests
    exercise it through the render; these pin the cut rule itself — most importantly that a clause cut
    never lands inside an open parenthetical, the regression a real open program surfaced (a goal
    'Every lifecycle transaction (upgrade, rollback, …' cut on the first inner comma and hid the verb)."""

    def test_a_sentence_within_the_cap_is_returned_whole(self):
        obj = "Keep the whole portfolio legible from one command."
        self.assertEqual(program_projection.goal_headline(obj), obj)

    def test_a_clause_boundary_is_preferred_over_a_bare_word_break(self):
        # A comma past the 0.55 floor is the cut point; everything after the clause is dropped.
        obj = "A" * 100 + ", and then " + "B" * 100 + "."
        self.assertEqual(program_projection.goal_headline(obj), "A" * 100 + "…")

    def test_it_falls_back_to_a_word_boundary_when_no_clause_boundary_is_late_enough(self):
        obj = "alpha " * 60 + "omega."                 # no clause marker anywhere
        headline = program_projection.goal_headline(obj)
        self.assertTrue(headline.endswith("…"))
        self.assertLessEqual(len(headline), program_projection._HEADLINE_CAP)
        self.assertFalse(headline[:-1].endswith(" "), "cut left a dangling space")
        self.assertTrue(headline.startswith("alpha alpha"))

    def test_it_never_cuts_inside_an_open_parenthetical(self):
        # The live shape: a parenthetical list whose inner commas sit past the floor. The old rule cut
        # on the first inner comma, leaving a dangling '(' and no main clause; the cut must clear the
        # closing ')' (or fall back past it), never end mid-aside.
        obj = ("Every lifecycle transaction (upgrade, rollback, module add/remove, whole-engine removal, "
               "control-plane bootstrap/finalize, and their arrival) runs through one stateless typed "
               "protocol with a single durable ledger that never loses a step.")
        headline = program_projection.goal_headline(obj)
        self.assertEqual(headline.count("("), headline.count(")"),
                         "headline ended inside an unclosed parenthetical")
        self.assertIn("runs through", headline, "the main clause after the aside was lost")

    def test_a_parenthetical_that_never_closes_before_the_cap_is_dropped_whole(self):
        # The fallback path: the aside opens before the floor and runs past the 180-char cap, so no
        # clause marker inside it qualifies AND the plain word break would land mid-aside. The balance
        # guard must drop from the '(' onward rather than leave a dangling open paren with no verb.
        obj = ("Every lifecycle transaction (upgrade, rollback, module add or remove, whole engine "
               "removal, control plane bootstrap or finalize, arrival, seal transitions, revision "
               "replay, snapshot capture, and rollback verification) runs through one typed protocol.")
        headline = program_projection.goal_headline(obj)
        self.assertEqual(headline.count("("), headline.count(")"), "left a dangling open parenthesis")
        self.assertNotIn("(", headline, "the unclosed aside was not dropped whole")
        self.assertTrue(headline.startswith("Every lifecycle transaction"))
        self.assertTrue(headline.endswith("…"))

    def test_balanced_prefix_drops_from_the_outermost_unmatched_open(self):
        self.assertEqual(program_projection._balanced_prefix("keep this (drop, this, list"), "keep this ")
        self.assertEqual(program_projection._balanced_prefix("nested (a (b, c"), "nested ")
        self.assertEqual(program_projection._balanced_prefix("already (balanced) fine"),
                         "already (balanced) fine")            # nothing to drop

    def test_balanced_prefix_handles_an_unclosed_open_at_the_very_start(self):
        # The index-0 case: the aside IS the whole text (open at position 0, never closed). Truthiness
        # on the open index once let this slip through as a no-op, leaving a lone dangling '('. Now the
        # leading '(' is stripped and what follows is re-balanced, so no unmatched '(' survives.
        self.assertEqual(program_projection._balanced_prefix("(only an open aside here"),
                         "only an open aside here")
        self.assertEqual(program_projection._balanced_prefix("((doubly opened aside"), "doubly opened aside")

    def test_no_truncated_headline_ever_ends_inside_an_open_parenthetical(self):
        # The invariant, swept across paren positions that a single-case test keeps missing: index 0,
        # mid-sentence, nested, never-closing, and several asides. Every objective here exceeds the cap
        # so it is truncated; the output must always carry balanced parentheses.
        filler = "and the sentence continues on well past the one hundred and eighty character cap "
        shapes = [
            "(" + "an aside opening at the very start that never closes " + filler * 3,
            "Lead in text (a short aside) " + filler * 3,
            "Lead (outer (inner nested aside that never closes " + filler * 3,
            "Every transaction (upgrade, rollback, add, remove, bootstrap, finalize, arrival, replay "
            + filler * 3,
            "First (a) then (b) then (c) then a long unclosed (d aside " + filler * 3,
            "Plain prose with no parentheses at all just running long " + filler * 3,
        ]
        for obj in shapes:
            headline = program_projection.goal_headline(obj)
            with self.subTest(obj=obj[:40]):
                self.assertGreater(len(obj), program_projection._HEADLINE_CAP)   # it really is truncated
                self.assertEqual(headline.count("("), headline.count(")"),
                                 f"unbalanced parentheses in {headline!r}")
                self.assertTrue(headline.endswith("…"))

    def test_a_numbered_list_marker_close_paren_is_kept_not_dropped(self):
        # Deliberate scope: the balance guard drops unmatched OPENS (the dangling-'(' harm) but leaves
        # a ')' used as a list marker intact — '1) ... 2) ... 3) ...' reads naturally and dropping the
        # markers would mangle it. This pins that decision so a later 'make it fully balanced' change
        # cannot silently regress the numbered-list phrasing the plan names as a real shelf shape.
        obj = ("Deliver the capability in four parts: 1) design the schema and finalize the stored "
               "contract, 2) build the ingestion pipeline end to end, 3) ship the operator-facing UI "
               "with its docs, and 4) write the migration and rollback playbooks before the launch.")
        headline = program_projection.goal_headline(obj)
        self.assertGreater(len(obj), program_projection._HEADLINE_CAP)     # it is truncated
        self.assertIn("1) design", headline)                              # markers preserved, not stripped
        self.assertIn("2) build", headline)
        self.assertEqual(headline.count("("), 0)                          # no open paren was introduced

    def test_last_clause_boundary_returns_none_when_every_late_marker_is_inside_a_paren(self):
        # A true pin on the guard: the ONLY markers past the 0.55 floor are commas inside an unclosed
        # '(...)', so a guarded boundary search finds nothing (None). Unguarded, rfind would return an
        # in-paren comma — so this fails the moment the paren check is removed.
        window = "Begin the whole sentence right here now (alpha, beta, gamma, delta, epsilon, zeta, eta"
        self.assertIsNone(program_projection._last_clause_boundary(window))
        # Control: the very same commas, once the paren is gone, ARE real top-level boundaries — proving
        # the None above is the guard at work, not markers that merely fell short of the floor.
        self.assertIsNotNone(program_projection._last_clause_boundary(window.replace("(", "")))


class TheClosedTailIsBounded(_Shelf):
    def test_the_tail_caps_at_five_orders_by_recency_and_counts_the_remainder(self):
        with self._seeding():
            self.progs.create("Open one", "Still going.")
            rows = [("2026-08-10T00:00:00Z", "retired"), ("2026-08-20T00:00:00Z", "abandoned"),
                    ("2026-08-20T00:00:00Z", "retired"), ("2026-08-20T00:00:00Z", "complete"),
                    ("2026-08-15T00:00:00Z", "retired"), ("2026-08-22T00:00:00Z", "abandoned"),
                    ("2026-08-05T00:00:00Z", "retired")]
            closed = []
            for i, (at, state) in enumerate(rows, start=1):
                slug = self.progs.create(f"Closed {i}", f"Objective {i}.")
                pid = self.progs.read(slug)["program_id"]
                self.progs._write(slug, {**self.progs.read(slug),
                                         "closure": {"state": state, "at": at, "reason": "done"}})
                closed.append((pid, at, state))
        render = program_projection.render_portfolio(self.progs)
        tail = render.split("## Recently closed", 1)[1]
        self.assertIn("(5 of 7)", tail)
        self.assertIn("… and 2 more closed program(s) not shown", tail)
        # The five shown are the most recent by (closure.at, program_id) descending.
        expected = sorted(closed, key=lambda c: (c[1], c[0]), reverse=True)[:5]
        shown_ids = [line.split("`")[1] for line in tail.splitlines() if line.startswith("- `")]
        self.assertEqual(shown_ids, [c[0] for c in expected])


class ADamagedRecordNeedsAttentionAndDoesNotBlankTheShelf(_Shelf):
    def test_a_corrupt_record_is_shown_not_dropped(self):
        with self._seeding():
            good = self.progs.create("Healthy", "A readable program.")
            bad = self.progs.create("Broken", "About to be corrupted.")
        self._corrupt(bad)
        render = program_projection.render_portfolio(self.progs)
        self.assertIn("Healthy", render)                      # the shelf is not blanked
        self.assertIn("## Needs attention (1)", render)
        self.assertIn(bad, render)                            # the damaged program is named, not dropped


class ThePROGRAMmdProjection(_Shelf):
    """PROGRAM.md is `program show` for a program at rest in the library, headed by the moment it
    reflects and its staleness window. Pure output — regenerating it never touches a record — and the
    library-wide sweep continues past a damaged program by writing it a needs-attention file."""

    def _program(self):
        with self._seeding():
            slug = self.progs.create("Alpha", "Deliver Alpha across several PRs.")
        return slug

    def test_the_generated_at_and_staleness_are_VISIBLE_not_hidden_in_a_comment(self):
        slug = self._program()
        text = program_projection.render_program_md(self.progs, self.progs.read(slug),
                                                    at="2026-01-01T00:00:00Z")
        # The trust signal must survive markdown rendering, so it is visible body text (a blockquote),
        # not an HTML comment a renderer would strip. The moment is the first thing in the file.
        self.assertTrue(text.startswith("> **Generated 2026-01-01T00:00:00Z.**"), text[:80])
        self.assertNotIn("<!--", text.split("# Alpha", 1)[0])  # nothing trust-critical is in a comment
        self.assertIn("go stale", text)                       # the window is disclosed, not promised away
        self.assertIn("child plan changed outside a program verb".lower(), text.lower())
        self.assertIn("# Alpha", text)                        # the program show body is present
        self.assertIn("## Objective", text)

    def test_regeneration_is_byte_stable_apart_from_the_generated_at_line(self):
        slug = self._program()
        first = program_projection.render_program_md(self.progs, self.progs.read(slug),
                                                     at="2026-01-01T00:00:00Z")
        second = program_projection.render_program_md(self.progs, self.progs.read(slug),
                                                      at="2026-12-31T23:59:59Z")
        self.assertNotEqual(first, second)                    # the generated-at moved
        drop = lambda t: "\n".join(line for line in t.splitlines()
                                   if not line.startswith("> **Generated "))
        self.assertEqual(drop(first), drop(second))           # everything else is identical

    def test_projecting_a_program_leaves_its_record_untouched(self):
        slug = self._program()
        record_path = self.lib.root / "programs" / slug / "record.json"
        before = record_path.read_bytes()
        program_projection.project_program(self.progs, slug)
        self.assertEqual(record_path.read_bytes(), before)
        self.assertTrue((self.lib.root / "programs" / slug / "PROGRAM.md").exists())

    def test_the_sweep_writes_a_needs_attention_file_and_continues_past_a_damaged_program(self):
        with self._seeding():
            good = self.progs.create("Healthy", "A readable program.")
            bad = self.progs.create("Broken", "About to break.")
        self._corrupt(bad)
        written = program_projection.project_all(self.progs, at="2026-01-01T00:00:00Z")
        self.assertEqual(set(written), {good, bad})           # the sweep did not die on the damaged one
        good_md = (self.lib.root / "programs" / good / "PROGRAM.md").read_text(encoding="utf-8")
        bad_md = (self.lib.root / "programs" / bad / "PROGRAM.md").read_text(encoding="utf-8")
        self.assertIn("# Healthy", good_md)
        self.assertIn("Needs attention", bad_md)
        self.assertIn(bad, bad_md)


if __name__ == "__main__":
    unittest.main()
