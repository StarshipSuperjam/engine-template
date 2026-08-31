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


EXPECTED_MAIN = '# Programs — portfolio\n\n<!-- generated from the program records; a read-only view — nothing here selects, starts, or advances work -->\n\nA shelf, not a queue: every OPEN program below, what it is for, how far along it is, and what is in flight — as facts, never a percentage and never a recommendation. Unknown is unknown, not done.\n\n## In flight (3)\n\n### Alpha\n- **Goal**: Give the operator a single durable place to see, reason about and act on every long-running program the engine is carrying at once…\n- **Program**: `prg_000000000101` · status active (derived)\n- **Last movement**: 2026-08-05\n- **In flight**: 2: the feature\n- **Settled on the chain**: 1 landed\n- **Obligations**: OB-1 — cut over\n\n### Bravo\n- **Goal**: Deliver Bravo in two PRs.\n- **Program**: `prg_000000000102` · status children-complete (derived)\n- **Last movement**: 2026-08-06\n- **In flight**: nothing — every live child has landed, but no one has recorded the PROGRAM complete; unwritten successors are unknown, not done\n- **Settled on the chain**: 1 landed\n- **Obligations**: none outstanding\n\n### Charlie\n- **Goal**: Deliver the review capability in three parts: the finder that surfaces candidates, the adjudicator that disposes each one, and the recorder that writes the outcome…\n- **Program**: `prg_000000000103` · status active (derived)\n- **Last movement**: 2026-08-09\n- **In flight**: finder; recorder\n- **Obligations**: none outstanding\n- **Lanes**: finding — pln_c00000000001; recording — pln_c00000000002\n'


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

    def test_last_clause_boundary_rejects_a_marker_inside_a_paren(self):
        # Directly: a window whose only late marker is a comma inside '(...)' has no clause boundary.
        window = "Do the thing (alpha, beta, gamma, delta, epsilon, zeta, eta, theta) and more"
        idx = program_projection._last_clause_boundary(window)
        if idx is not None:
            depth = window.count("(", 0, idx) - window.count(")", 0, idx)
            self.assertEqual(depth, 0, "returned a boundary sitting inside an open parenthetical")


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
