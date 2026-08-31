#!/usr/bin/env python3
"""Tests for program_manager — the program surface at its OWN address.

The CLI-level program tests live here now, moved whole from test_project_manager.py when the surface
moved: same scenarios, same assertions, only reached through `program_manager.main` instead of
`project_manager.main`. Two things are pinned here that the move itself is answerable for:

- THE MOVE IS FAITHFUL. `TheProgramGoldenTranscript` replays a transcript captured from the merge-base
  tree BEFORE the move and asserts byte-identity against the new tool, save the one itemized delta.
- THE OLD DOOR IS CLOSED. `TheOldProgramDoorRefuses` pins that project_manager's `program` word now
  refuses with one pointer and exit 2, forwarding nothing.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from unittest import mock

import plan_program
import plan_store
import program_manager
import program_projection
import project_manager

from test_plan_store import _document



# --- golden behaviour-identity harness ----------------------------------------
#
# PR 4 relocates the program surface to its own address, and the promise is that it is a FAITHFUL move.
# The honest evidence is a transcript captured from the merge-base tree BEFORE the move, replayed
# against the tool AFTER it, with ONE itemized delta allowed and everything else pinned byte-for-byte.
# The trap it closes is circular evidence — capturing the "before" side from the post-move tool proves
# nothing — so GOLDEN_TRANSCRIPT below was captured once, from GOLDEN_SOURCE_COMMIT, and is committed as
# data. CI never re-captures it (the old door is gone); it only replays and folds.
#
# THE ONE PERMITTED DELTA: the tool NAME inside printed hints (project_manager.py -> program_manager.py),
# and the refusal PREFIX (project-manager: -> program-manager:). `_fold_new_to_old` rewrites exactly
# those on the new tool's output; any other difference survives the fold and fails the identity test.
#
# DETERMINISM: every volatile input is pinned — the wall clock (`moment.utc_now` and each tool's bound
# `_now`, aliased at import so patching `moment` alone misses them), the minted program id, and the
# library path (normalised to <LIB>). Child plans are seeded with fixed ids, so none is minted.

GOLDEN_SOURCE_COMMIT = 'aad51d0989415126dff4f2f55c59712b3d815974'
_GOLDEN_FIXED_NOW = "2026-01-01T00:00:00Z"
_GOLDEN_FIXED_PROGRAM_ID = "prg_a1a1a1a1a1a1"


def _golden_obligation(identifier, statement, state="carried"):
    return {"id": identifier, "statement": statement, "state": state}


def _golden_seed_plan(lib, plan_id, title, *, program_id=None, predecessor=None, obligations=None):
    document = _document(plan_id=plan_id, title=title)
    if program_id is not None:
        program = {"program_id": program_id}
        if predecessor:
            program["predecessor_plan_id"] = predecessor
        if obligations is not None:
            program["carried_obligations"] = obligations
        document["program"] = program
    lib.create(document)


def _golden_normalize(text, root):
    # Normalise the library path to a stable token, robustly across platforms. On macOS the temp root
    # from mkdtemp is '/var/folders/…' but a tool that resolves it prints '/private/var/folders/…', so
    # replacing only `root` left a stray '/private<LIB>' — output that matched on the capturing machine
    # but diverged on a runner that resolved the path differently. Replace the realpath form first
    # (it is the longer, '/private'-prefixed one) and then the raw root, so either printed form collapses.
    import os
    for candidate in (os.path.realpath(root), root):
        text = text.replace(candidate, "<LIB>")
    return text


_PORTFOLIO_HINT_BLOCK = "\nEvery open program at a glance, qualitatively — `program portfolio`.\n"


def _fold_new_to_old(entry):
    """Rewrite the new tool's output by the permitted deltas, so what remains must match the golden
    byte-for-byte. Applied only to the replay side; the golden is the pre-move truth, untouched.

    The move's own two deltas are the tool name in printed hints and the refusal prefix. A THIRD,
    intended and disclosed, is added by the portfolio work: `list` and `show` now print a next-step
    hint naming `program portfolio`, which did not exist pre-move — it is folded out here and its
    presence is asserted positively in TheListAndShowPointAtThePortfolio, so the identity check still
    pins that NOTHING ELSE about the moved behaviour changed."""
    stdout = entry["stdout"].replace("program_manager.py", "project_manager.py")
    # `list` and `show` append exactly this block (a leading blank line, then the hint line). Strip it
    # as a suffix so the rest of their pre-move output is compared untouched — no other line moves.
    if stdout.endswith(_PORTFOLIO_HINT_BLOCK):
        stdout = stdout[:-len(_PORTFOLIO_HINT_BLOCK)]
    return {
        "argv": entry["argv"],
        "code": entry["code"],
        "stdout": stdout,
        "stderr": (entry["stderr"].replace("program-manager:", "project-manager:")
                   .replace("program_manager.py", "project_manager.py")),
    }


def _golden_script(program_id):
    o = _golden_obligation
    return [
        ("invoke", ["program", "new", "--title", "Alpha",
                    "--objective", "Deliver the Alpha capability across several PRs."]),
        ("invoke", ["program", "list"]),
        ("seed", dict(plan_id="pln_a00000000001", title="PR A", program_id=program_id,
                      obligations=[o("OB-1", "PR B carries the cut-over.")])),
        ("invoke", ["program", "add", program_id, "pln_a00000000001"]),
        ("invoke", ["program", "show", program_id]),
        ("seed", dict(plan_id="pln_b00000000002", title="PR B", program_id=program_id,
                      predecessor="pln_a00000000001",
                      obligations=[o("OB-1", "Cut over.", "satisfied")])),
        ("invoke", ["program", "add", program_id, "pln_b00000000002",
                    "--after", "pln_a00000000001"]),
        ("seed", dict(plan_id="pln_d00000000004", title="PR D drops it", program_id=program_id,
                      predecessor="pln_b00000000002", obligations=[])),
        ("invoke", ["program", "add", program_id, "pln_d00000000004",
                    "--after", "pln_b00000000002"]),
        ("invoke", ["program", "revise-objective", program_id,
                    "--objective", "Deliver the Alpha capability, refined.",
                    "--reason", "the first wording undersold it"]),
        ("invoke", ["program", "lanes", "propose", program_id]),
        ("invoke", ["program", "list"]),
        ("invoke", ["program", "complete", program_id, "--reason", "the objective is met"]),
        ("invoke", ["program", "reopen", program_id, "--reason", "one more PR after all"]),
        ("invoke", ["program", "retire", program_id, "--reason", "shelving the whole line"]),
        ("invoke", ["program", "show", program_id]),
    ]


def golden_capture(tool_module):
    """Run the scenario against `tool_module.main`, deterministically, and return the transcript. Used
    to REPLAY against program_manager; the committed GOLDEN_TRANSCRIPT was captured this way against
    the pre-move project_manager, and cannot be reproduced on a post-move tree (the old door is gone)."""
    import contextlib
    import io
    import tempfile
    from unittest import mock
    import moment
    import plan_program as _pp
    import plan_store as _ps

    with tempfile.TemporaryDirectory() as tmp:
        root = str(Path(tmp) / "plans")
        lib = _ps.PlanLibrary(root)
        recorded = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(moment, "utc_now", lambda: _GOLDEN_FIXED_NOW))
            stack.enter_context(mock.patch.object(
                _pp, "mint_program_id", lambda: _GOLDEN_FIXED_PROGRAM_ID))
            for module in (_ps, _pp):
                stack.enter_context(mock.patch.object(module, "_now", lambda: _GOLDEN_FIXED_NOW))
            for name in ("project_manager", "program_manager"):
                try:
                    module = __import__(name)
                except Exception:  # noqa: BLE001
                    continue
                if hasattr(module, "_now"):
                    stack.enter_context(mock.patch.object(module, "_now", lambda: _GOLDEN_FIXED_NOW))
            for kind, payload in _golden_script(_GOLDEN_FIXED_PROGRAM_ID):
                if kind == "seed":
                    _golden_seed_plan(lib, **payload)
                    continue
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = tool_module.main(["--library", root, *payload])
                recorded.append({
                    "argv": payload,
                    "code": code,
                    "stdout": _golden_normalize(out.getvalue(), root),
                    "stderr": _golden_normalize(err.getvalue(), root),
                })
    return recorded


# Captured once from GOLDEN_SOURCE_COMMIT against the pre-move project_manager. Do not hand-edit —
# regenerate by running `golden_capture(project_manager)` on the pre-move tree.
GOLDEN_TRANSCRIPT = [{'argv': ['program', 'new', '--title', 'Alpha', '--objective', 'Deliver the Alpha capability across several PRs.'], 'code': 0, 'stdout': 'created program prg_a1a1a1a1a1a1 at <LIB>/programs/alpha--a1a1a1\nAdd its first child with `program add`. Order records a decision — nothing here starts, selects, or advances a child.\n', 'stderr': ''}, {'argv': ['program', 'list'], 'code': 0, 'stdout': 'prg_a1a1a1a1a1a1  empty              0 child(ren), 0 obligation(s) outstanding  Alpha\n', 'stderr': ''}, {'argv': ['program', 'add', 'prg_a1a1a1a1a1a1', 'pln_a00000000001'], 'code': 0, 'stdout': 'added pln_a00000000001 as child 1 of prg_a1a1a1a1a1a1\n\n1 obligation(s) are now carried into the next child ON THIS BRANCH:\n  - OB-1: PR B carries the cut-over.\n\nThe next child on this branch must answer for each — satisfied, still carried, or released with a stated reason. None of them can be dropped by saying nothing.\n', 'stderr': ''}, {'argv': ['program', 'show', 'prg_a1a1a1a1a1a1'], 'code': 0, 'stdout': '# Alpha\n\n<!-- generated from the program record and its children; edits here are overwritten -->\n\n- **Program**: `prg_a1a1a1a1a1a1`\n- **Status**: in-progress — derived from the children, never stored\n- **Children**: 1\n\n## Objective\n\nDeliver the Alpha capability across several PRs.\n\n## Children, in the order their predecessor edges declare\n\n| # | Plan | Status | Succeeds |\n|---:|---|---|---|\n| 1 | PR A (`pln_a00000000001`) | draft | — |\n\n_Order records a decision. Nothing here selects, starts, or advances a child._\n\n## Obligations still carried\n\n- Carried at `pln_a00000000001`, where that branch currently ends:\n  - **OB-1** — PR B carries the cut-over.\n\nEach debt carried at a branch END must be answered by the next child on ITS OWN branch — satisfied, still carried, or released with a reason; a mid-chain debt above already names its own door. None can be dropped by saying nothing.\n\n', 'stderr': ''}, {'argv': ['program', 'add', 'prg_a1a1a1a1a1a1', 'pln_b00000000002', '--after', 'pln_a00000000001'], 'code': 0, 'stdout': 'added pln_b00000000002 as child 2 of prg_a1a1a1a1a1a1\n', 'stderr': ''}, {'argv': ['program', 'add', 'prg_a1a1a1a1a1a1', 'pln_d00000000004', '--after', 'pln_b00000000002'], 'code': 0, 'stdout': 'added pln_d00000000004 as child 3 of prg_a1a1a1a1a1a1\n', 'stderr': ''}, {'argv': ['program', 'revise-objective', 'prg_a1a1a1a1a1a1', '--objective', 'Deliver the Alpha capability, refined.', '--reason', 'the first wording undersold it'], 'code': 0, 'stdout': 'revised the objective of prg_a1a1a1a1a1a1\n  the first wording undersold it\n\nPreviously: Deliver the Alpha capability across several PRs.\nNow:        Deliver the Alpha capability, refined.\n\nNothing was overwritten silently — `program show` lists every prior objective with when it was replaced and why.\n', 'stderr': ''}, {'argv': ['program', 'lanes', 'propose', 'prg_a1a1a1a1a1a1'], 'code': 0, 'stdout': 'Proposing lanes for prg_a1a1a1a1a1a1 — at most 4 lane(s). Nothing is written.\n\n## Lanes\n- **lane-1**: pln_a00000000001 [draft], pln_b00000000002 [draft], pln_d00000000004 [draft]\n    territory: a.py\n\n**Concurrency is not recommended for the children in lane lane-1.** They collide on shared territory (a.py), so they are grouped to run in sequence rather than split into a manufactured concurrency.\n\n_This reasons from the declared work-item paths only. Generated or incidental writes are invisible to it — this repository regenerates derived artifacts on most builds — so lanes shown as disjoint can still collide on regenerated files at rebase._\n\nTo record this recommendation (edit the reason to your own):\n  python tools/project_manager.py program lanes set prg_a1a1a1a1a1a1 --reason "<why>" --lane lane-1=pln_a00000000001,pln_b00000000002,pln_d00000000004\n', 'stderr': ''}, {'argv': ['program', 'list'], 'code': 0, 'stdout': 'prg_a1a1a1a1a1a1  in-progress        3 child(ren), 0 obligation(s) outstanding  Alpha\n', 'stderr': ''}, {'argv': ['program', 'complete', 'prg_a1a1a1a1a1a1', '--reason', 'the objective is met'], 'code': 2, 'stdout': '', 'stderr': 'project-manager: this program cannot be recorded complete yet:\n  - these children are not complete: pln_a00000000001 (draft), pln_b00000000002 (draft), pln_d00000000004 (draft)\n  - no child of this program is complete, so nothing has actually shipped\nCompletion says the objective is MET. Recording it over unfinished work would be the same lie in a different place. If the program is being set down rather than finished, `program retire` is the verb — it refuses over outstanding debts too, so settle or release those either way.\n'}, {'argv': ['program', 'reopen', 'prg_a1a1a1a1a1a1', '--reason', 'one more PR after all'], 'code': 2, 'stdout': '', 'stderr': 'project-manager: this program is not closed\n'}, {'argv': ['program', 'retire', 'prg_a1a1a1a1a1a1', '--reason', 'shelving the whole line'], 'code': 0, 'stdout': 'prg_a1a1a1a1a1a1 is now retired: shelving the whole line\nNothing was deleted — every child plan and every revision stays on the shelf.\n', 'stderr': ''}, {'argv': ['program', 'show', 'prg_a1a1a1a1a1a1'], 'code': 0, 'stdout': '# Alpha\n\n<!-- generated from the program record and its children; edits here are overwritten -->\n\n- **Program**: `prg_a1a1a1a1a1a1`\n- **Status**: retired — recorded by an explicit close, not derived\n- **Children**: 3\n\n## Objective\n\nDeliver the Alpha capability, refined.\n\n## Children, in the order their predecessor edges declare\n\n| # | Plan | Status | Succeeds |\n|---:|---|---|---|\n| 1 | PR A (`pln_a00000000001`) | draft | — |\n| 2 | PR B (`pln_b00000000002`) | draft | `pln_a00000000001` |\n| 3 | PR D drops it (`pln_d00000000004`) | draft | `pln_b00000000002` |\n\n_Order records a decision. Nothing here selects, starts, or advances a child._\n\n## Obligations still carried\n\n_None outstanding._\n\n## How the objective has been revised\n\n- Replaced 2026-01-01T00:00:00Z: the first wording undersold it\n  - Previously: Deliver the Alpha capability across several PRs.\n\n', 'stderr': ''}]


class _ProgramSurface(unittest.TestCase):
    """A throwaway library, reached through the program surface's own address. `run_command` routes a
    `program` verb to program_manager (its home now) and any other verb to project_manager — the
    routing a caller performs by choosing which tool to invoke."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "plans"
        self.lib = plan_store.PlanLibrary(self.root)
        self.addCleanup(self._tmp.cleanup)

    def run_command(self, *argv) -> tuple[int, str, str]:
        tool = program_manager if argv and argv[0] == "program" else project_manager
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(["--library", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def _plan(self, **over):
        document = _document(**over)
        return self.lib.create(document), document

    def _write_document(self, document) -> str:
        path = Path(self._tmp.name) / f"{document['plan_id']}-{document['revision']}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)


class TheProgramGoldenTranscript(unittest.TestCase):
    """The move's behaviour-identity proof. GOLDEN_TRANSCRIPT was captured from the pre-move tree named
    in GOLDEN_SOURCE_COMMIT and lives here as data; this replays it against program_manager and folds
    the ONE permitted delta (tool name in hints; refusal prefix). Any other divergence fails."""

    def test_the_committed_transcript_replays_identically(self):
        import program_manager
        replay = [_fold_new_to_old(e) for e in golden_capture(program_manager)]
        self.assertEqual(len(GOLDEN_TRANSCRIPT), len(replay),
                         "the scenario length changed under the move")
        for i, (want, got) in enumerate(zip(GOLDEN_TRANSCRIPT, replay)):
            self.assertEqual(
                want, got,
                f"entry {i} {want['argv']} diverged from the pre-move transcript beyond the one "
                "permitted delta (tool name in printed hints, and the refusal prefix)")

    def test_the_transcript_names_the_merge_base_source_commit(self):
        self.assertRegex(GOLDEN_SOURCE_COMMIT, r"^[0-9a-f]{40}$")
        self.assertGreaterEqual(len(GOLDEN_TRANSCRIPT), 10,
                                "too thin a transcript to stand as behaviour-identity evidence")


class TheOldProgramDoorRefuses(unittest.TestCase):
    """project_manager's `program` word is a refusing stub now: one pointer to the new address, on
    stderr, exit 2, whatever verb or flags trail it — so an in-process caller branching on the return
    code cannot read the dead door as success, and nothing is silently forwarded."""

    def _run(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = project_manager.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_the_bare_program_word_refuses_with_the_pointer_on_stderr(self):
        code, out, err = self._run("program")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("project-manager:"), err)
        self.assertIn("program_manager.py program", err)

    def test_any_trailing_verb_and_flags_still_reach_the_pointer_and_exit_two(self):
        for argv in (["program", "show", "prg_x"],
                     ["program", "lanes", "set", "p", "--lane", "a=b", "--reason", "r"],
                     ["program", "not-even-a-verb", "--wat"]):
            with self.subTest(argv=argv):
                code, out, err = self._run(*argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("program_manager.py program", err)

    def test_the_cut_is_clean_and_forwards_nothing(self):
        _, _, err = self._run("program", "list")
        self.assertIn("forwards nothing", err)

    def test_asking_the_dead_door_for_help_reaches_the_pointer_not_a_false_success(self):
        # `-h` is the most natural way a confused caller self-orients; argparse's automatic help would
        # fire before the refusal and exit 0 with an empty usage — a false success at the exact
        # discovery path. The dead door refuses uniformly instead: pointer, exit 2, no stdout.
        for flag in ("-h", "--help"):
            with self.subTest(flag=flag):
                code, out, err = self._run("program", flag)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("program_manager.py program", err)

    def test_the_dead_door_refuses_even_after_the_library_global(self):
        code, out, err = self._run("--library", "/tmp/nowhere", "program", "portfolio")
        self.assertEqual(code, 2)
        self.assertIn("program_manager.py program", err)

    def test_a_help_flag_before_the_program_word_still_shows_the_tools_own_help(self):
        # argparse fires -h/--help immediately, before any subcommand, so `-h program` asks for the
        # TOOL's help, not the dead door. The short-circuit honours that precedence — swallowing a
        # general help request would be its own false step — while `program -h` (the door's own help,
        # tested above) still refuses.
        for flag in ("-h", "--help"):
            with self.subTest(flag=flag):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as caught:
                        project_manager.main([flag, "program"])
                self.assertEqual(caught.exception.code, 0)                    # argparse's real success
                self.assertNotIn("program_manager.py program", out.getvalue())  # not the door refusal
                self.assertIn("usage", out.getvalue().lower())


class ProgramVerbs(_ProgramSurface):
    def _obligation(self, identifier, statement, state="carried"):
        return {"id": identifier, "statement": statement, "state": state}

    def _program_with_child(self):
        code, out, err = self.run_command("program", "new", "--title", "Two-PR program",
                                          "--objective", "Delivered across two PRs.")
        self.assertEqual(code, 0, err)
        program_id = out.split()[2]
        document = _document(plan_id="pln_aaaaaaaaaaaa", title="PR A")
        document["program"] = {"program_id": program_id, "carried_obligations": [
            self._obligation("OB-1", "PR B cuts the Build Coordinator over.")]}
        self.lib.create(document)
        self.assertEqual(self.run_command("program", "add", program_id, "pln_aaaaaaaaaaaa")[0], 0)
        return program_id

    def test_a_program_is_creatable_and_readable_from_the_command_line(self):
        program_id = self._program_with_child()
        code, out, _ = self.run_command("program", "show", program_id)
        self.assertEqual(code, 0)
        self.assertIn("PR A", out)
        self.assertIn("OB-1", out)
        self.assertIn(program_id, self.run_command("program", "list")[1])

    def test_the_verb_refuses_a_successor_that_drops_an_obligation(self):
        program_id = self._program_with_child()
        successor = _document(plan_id="pln_bbbbbbbbbbbb", title="PR B")
        successor["program"] = {"program_id": program_id,
                                "predecessor_plan_id": "pln_aaaaaaaaaaaa",
                                "carried_obligations": []}
        self.lib.create(successor)
        code, _, err = self.run_command("program", "add", program_id, "pln_bbbbbbbbbbbb",
                                        "--after", "pln_aaaaaaaaaaaa")
        self.assertEqual(code, 2)
        self.assertIn("OB-1", err)
        self.assertIn("does not answer for", err)

    def test_the_verb_reports_what_the_next_child_now_owes(self):
        program_id = self._program_with_child()
        out = self.run_command("program", "show", program_id)[1]
        self.assertIn("None can be dropped by saying nothing", out)

    def _plan_doc(self, program_id, plan_id, title, *obligations, predecessor=None):
        document = _document(plan_id=plan_id, title=title)
        program = {"program_id": program_id}
        if obligations:
            program["carried_obligations"] = list(obligations)
        if predecessor:
            program["predecessor_plan_id"] = predecessor
        document["program"] = program
        self.lib.create(document)

    def test_insert_places_a_plan_before_an_existing_child_and_says_which_edges_moved(self):
        program_id = self._program_with_child()
        self._plan_doc(program_id, "pln_bbbbbbbbbbbb", "PR B",
                       self._obligation("OB-1", "Cut over.", "satisfied"),
                       predecessor="pln_aaaaaaaaaaaa")
        self.assertEqual(self.run_command("program", "add", program_id, "pln_bbbbbbbbbbbb",
                                          "--after", "pln_aaaaaaaaaaaa")[0], 0)
        # X re-declares OB-1 as carried, so it answers for A; B satisfies OB-1, so it answers for X.
        self._plan_doc(program_id, "pln_cccccccccccc", "PR X",
                       self._obligation("OB-1", "Still carried, now by X."))
        code, out, err = self.run_command("program", "insert", program_id, "pln_cccccccccccc",
                                          "--before", "pln_bbbbbbbbbbbb")
        self.assertEqual(code, 0, err)
        self.assertIn("inserted pln_cccccccccccc as child 2", out)
        self.assertIn("pln_aaaaaaaaaaaa -> pln_cccccccccccc -> pln_bbbbbbbbbbbb", out)
        self.assertIn("Nothing was renumbered", out)
        # The half an operator does not picture: the displaced child now answers for the newcomer.
        self.assertIn("pln_bbbbbbbbbbbb now answers for 1 obligation(s)", out)
        shown = self.run_command("program", "show", program_id)[1]
        self.assertLess(shown.index("PR X"), shown.index("PR B"))

    def test_insert_refuses_at_the_command_line_when_the_displaced_child_cannot_answer(self):
        program_id = self._program_with_child()
        self._plan_doc(program_id, "pln_bbbbbbbbbbbb", "PR B",
                       self._obligation("OB-1", "Cut over.", "satisfied"),
                       predecessor="pln_aaaaaaaaaaaa")
        self.assertEqual(self.run_command("program", "add", program_id, "pln_bbbbbbbbbbbb",
                                          "--after", "pln_aaaaaaaaaaaa")[0], 0)
        self._plan_doc(program_id, "pln_cccccccccccc", "PR X",
                       self._obligation("OB-1", "Still carried, now by X."),
                       self._obligation("OB-NEW", "Something B has never heard of."))
        code, _, err = self.run_command("program", "insert", program_id, "pln_cccccccccccc",
                                        "--before", "pln_bbbbbbbbbbbb")
        self.assertEqual(code, 2)
        self.assertIn("OB-NEW", err)
        self.assertIn("Revise pln_bbbbbbbbbbbb", err)

    def _superseded_setup(self):
        """A -> B, where B is sealed and about to be replaced. Returns the program id.

        The seal is load-bearing, not scenery: supersede refuses an unsealed target — a draft is
        revised, not replaced — and this fixture claimed "B is sealed" for two rounds while never
        sealing it, which is exactly how the draft-supersede hole went unnoticed.
        """
        program_id = self._program_with_child()
        self._plan_doc(program_id, "pln_bbbbbbbbbbbb", "PR B",
                       self._obligation("OB-1", "Cut over.", "satisfied"),
                       predecessor="pln_aaaaaaaaaaaa")
        self.assertEqual(self.run_command("program", "add", program_id, "pln_bbbbbbbbbbbb",
                                          "--after", "pln_aaaaaaaaaaaa")[0], 0)
        slug_b = self.lib.resolve("pln_bbbbbbbbbbbb")
        digest = self.lib.read_record(slug_b)["current"]["plan_digest"]
        self.lib.update_record(slug_b, lambda r: r.update({"seal": {
            "revision": 1, "reviewed_digest": digest, "sealed_digest": digest,
            "build_plan_digest": digest, "at": "2026-08-23T03:00:00Z", "delta_judgment": "none"}}))
        return program_id

    def test_supersede_retires_the_plan_first_and_then_marks_the_record(self):
        program_id = self._superseded_setup()
        self._plan_doc(program_id, "pln_cccccccccccc", "PR B, second attempt",
                       self._obligation("OB-1", "Cut over, properly this time.", "satisfied"),
                       predecessor="pln_aaaaaaaaaaaa")
        code, out, err = self.run_command("program", "supersede", program_id, "pln_bbbbbbbbbbbb",
                                          "--with", "pln_cccccccccccc",
                                          "--reason", "the cut-over shape was wrong")
        self.assertEqual(code, 0, err)
        self.assertIn("retired pln_bbbbbbbbbbbb", out)
        self.assertIn("pln_cccccccccccc supersedes pln_bbbbbbbbbbbb", out)
        # The plan is retired through the ordinary close path, so `show` reports it that way.
        self.assertIn("retired", self.run_command("show", "pln_bbbbbbbbbbbb")[1])
        rendered = self.run_command("program", "show", program_id)[1]
        self.assertIn("superseded by `pln_cccccccccccc`", rendered)
        self.assertIn("PR B, second attempt", rendered)
        self.assertIn("PR B", rendered)          # nothing was deleted to make room

    def test_a_build_binding_arriving_late_stops_the_retirement_under_the_lock(self):
        """The TOCTOU guard, which shipped without a fixture until a reviewer said so.

        supersede reads the target's status before it writes anything, and that read is unlocked. A
        Build binding in the window would retire a plan underneath running work — the outcome
        supersede's own refusal calls unrecoverable. The guard re-asserts the precondition inside
        the mutator; this drives that path directly by binding the plan after the pre-check has
        already passed, which is exactly the race, made deterministic.
        """
        import project_manager
        program_id = self._superseded_setup()
        slug_b = self.lib.resolve("pln_bbbbbbbbbbbb")
        self.lib.update_record(slug_b, lambda r: r.update({"build_binding": {
            "sealed_digest": "sha256:" + "a" * 64, "build_plan_digest": "sha256:" + "b" * 64,
            "at": "2026-08-29T07:00:00Z", "repository": "owner/repo", "pull_request": 7}}))
        with self.assertRaises(project_manager.ProjectManagerError) as caught:
            project_manager.close_plan_record(self.lib, slug_b, "retired", "replaced",
                                              refuse_if_active=True)
        self.assertIn("strand that Build", str(caught.exception))
        # Nothing was written: raising from inside the mutator must abort before the record lands.
        self.assertIsNone(self.lib.read_record(slug_b).get("closure"))
        # And without the flag the same call writes, so the fixture is testing the guard and not
        # some unrelated refusal standing in front of it.
        project_manager.close_plan_record(self.lib, slug_b, "retired", "replaced")
        self.assertEqual(self.lib.read_record(slug_b)["closure"]["state"], "retired")

    def test_supersede_refuses_before_it_retires_anything(self):
        """A refusal must not leave the replaced plan out of play for a supersession that never landed."""
        program_id = self._superseded_setup()
        self._plan_doc(program_id, "pln_cccccccccccc", "A replacement that forgot",
                       predecessor="pln_aaaaaaaaaaaa")     # says nothing about OB-1
        code, _, err = self.run_command("program", "supersede", program_id, "pln_bbbbbbbbbbbb",
                                        "--with", "pln_cccccccccccc", "--reason", "no")
        self.assertEqual(code, 2)
        self.assertIn("OB-1", err)
        self.assertNotIn("retired", self.run_command("show", "pln_bbbbbbbbbbbb")[1])

    def test_supersede_refuses_an_unsealed_target_from_the_command_line(self):
        """A draft is revised, not replaced — and letting it be superseded was the reachable hole:
        supersede retires its target, an unsealed retirement reopens, and a reopened draft could
        seal and bind while the program still recorded it replaced."""
        program_id = self._program_with_child()
        self._plan_doc(program_id, "pln_bbbbbbbbbbbb", "PR B, still a draft",
                       self._obligation("OB-1", "Cut over.", "satisfied"),
                       predecessor="pln_aaaaaaaaaaaa")
        self.assertEqual(self.run_command("program", "add", program_id, "pln_bbbbbbbbbbbb",
                                          "--after", "pln_aaaaaaaaaaaa")[0], 0)
        self._plan_doc(program_id, "pln_cccccccccccc", "A replacement",
                       self._obligation("OB-1", "Cut over.", "satisfied"),
                       predecessor="pln_aaaaaaaaaaaa")
        code, _, err = self.run_command("program", "supersede", program_id, "pln_bbbbbbbbbbbb",
                                        "--with", "pln_cccccccccccc", "--reason", "premature")
        self.assertEqual(code, 2)
        self.assertIn("not sealed", err)
        self.assertNotIn("retired", self.run_command("show", "pln_bbbbbbbbbbbb")[1])

    def test_reopen_refuses_a_superseded_child_even_when_it_is_not_sealed(self):
        """The belt behind the seal requirement, for records the old code already wrote: a
        superseded child gave its place away, and reopening it would stand two plans in one
        position — sealed or not."""
        program_id = self._program_with_child()
        self._plan_doc(program_id, "pln_bbbbbbbbbbbb", "PR B, a draft the old code superseded",
                       self._obligation("OB-1", "Cut over.", "satisfied"),
                       predecessor="pln_aaaaaaaaaaaa")
        self.assertEqual(self.run_command("program", "add", program_id, "pln_bbbbbbbbbbbb",
                                          "--after", "pln_aaaaaaaaaaaa")[0], 0)
        self.assertEqual(self.run_command("retire", "pln_bbbbbbbbbbbb",
                                          "--reason", "replaced, the legacy way")[0], 0)
        programs = plan_program.ProgramLibrary(self.lib)
        slug = programs.resolve(program_id)
        record = programs.read(slug)
        for child in record["children"]:
            if child["plan_id"] == "pln_bbbbbbbbbbbb":
                child["superseded_by"] = "pln_cccccccccccc"
        programs._write(slug, record)
        code, _, err = self.run_command("reopen", "pln_bbbbbbbbbbbb")
        self.assertEqual(code, 2)
        self.assertIn("superseded by pln_cccccccccccc", err)
        self.assertEqual(self.lib.read_record(
            self.lib.resolve("pln_bbbbbbbbbbbb"))["closure"]["state"], "retired")

    def test_reopen_refuses_a_child_under_a_complete_program_until_the_program_reopens(self):
        """Recorded completion must not become false without anyone deciding so: completion
        ignores a retired child as a decision, and reopening that child would un-decide it
        underneath the program's closure."""
        code, out, err = self.run_command("program", "new", "--title", "Completable",
                                          "--objective", "One landed, one set aside.")
        self.assertEqual(code, 0, err)
        program_id = out.split()[2]
        self._plan_doc(program_id, "pln_aaaaaaaaaaaa", "PR A")
        self.assertEqual(self.run_command("program", "add", program_id, "pln_aaaaaaaaaaaa")[0], 0)
        self._plan_doc(program_id, "pln_bbbbbbbbbbbb", "PR B, set aside",
                       predecessor="pln_aaaaaaaaaaaa")
        self.assertEqual(self.run_command("program", "add", program_id, "pln_bbbbbbbbbbbb",
                                          "--after", "pln_aaaaaaaaaaaa")[0], 0)
        self.assertEqual(self.run_command("retire", "pln_bbbbbbbbbbbb",
                                          "--reason", "set aside")[0], 0)
        self.assertEqual(self.run_command("complete", "pln_aaaaaaaaaaaa",
                                          "--reason", "merged")[0], 0)
        self.assertEqual(self.run_command("program", "complete", program_id,
                                          "--reason", "objective met")[0], 0)
        code, _, err = self.run_command("reopen", "pln_bbbbbbbbbbbb")
        self.assertEqual(code, 2)
        self.assertIn("recorded complete", err)
        self.assertIn("program reopen", err)
        # The named door opens, and then the plan's own reopen goes through.
        self.assertEqual(self.run_command("program", "reopen", program_id,
                                          "--reason", "PR B is back on")[0], 0)
        self.assertEqual(self.run_command("reopen", "pln_bbbbbbbbbbbb")[0], 0)

    def test_supersede_refuses_an_ordinarily_retired_unsealed_draft(self):
        """The two-step dodge a reviewer proved: retire a draft for unrelated reasons, then
        supersede it — the closure used to exempt the target from the seal requirement, so a plan
        that was never terminal got marked replaced, with `reopen` refusing it forever after."""
        program_id = self._program_with_child()
        self._plan_doc(program_id, "pln_bbbbbbbbbbbb", "PR B, a draft",
                       self._obligation("OB-1", "Cut over.", "satisfied"),
                       predecessor="pln_aaaaaaaaaaaa")
        self.assertEqual(self.run_command("program", "add", program_id, "pln_bbbbbbbbbbbb",
                                          "--after", "pln_aaaaaaaaaaaa")[0], 0)
        self.assertEqual(self.run_command("retire", "pln_bbbbbbbbbbbb",
                                          "--reason", "just tidying up")[0], 0)
        self._plan_doc(program_id, "pln_cccccccccccc", "A replacement",
                       self._obligation("OB-1", "Cut over.", "satisfied"),
                       predecessor="pln_aaaaaaaaaaaa")
        code, _, err = self.run_command("program", "supersede", program_id, "pln_bbbbbbbbbbbb",
                                        "--with", "pln_cccccccccccc", "--reason", "sneaking past?")
        self.assertEqual(code, 2)
        self.assertIn("not sealed", err)
        record = plan_program.ProgramLibrary(self.lib).read(
            plan_program.ProgramLibrary(self.lib).resolve(program_id))
        self.assertIsNone(next(c for c in record["children"]
                               if c["plan_id"] == "pln_bbbbbbbbbbbb").get("superseded_by"))
        # And the draft is still reopenable — its closure was ordinary bookkeeping all along.
        self.assertEqual(self.run_command("reopen", "pln_bbbbbbbbbbbb")[0], 0)

    def test_reopen_fails_closed_when_a_program_record_naming_the_plan_will_not_read(self):
        """A record that cannot be read might mark this child superseded or its program complete;
        skipping it silently was the one fail-open on this file's governance surface."""
        program_id = self._program_with_child()
        self.assertEqual(self.run_command("retire", "pln_aaaaaaaaaaaa",
                                          "--reason", "set aside")[0], 0)
        programs = plan_program.ProgramLibrary(self.lib)
        slug = programs.resolve(program_id)
        path = programs.program_dir(slug) / "record.json"
        record = json.loads(path.read_text())
        record["a_field_this_schema_does_not_know"] = True
        path.write_text(json.dumps(record), encoding="utf-8")
        code, _, err = self.run_command("reopen", "pln_aaaaaaaaaaaa")
        self.assertEqual(code, 2)
        self.assertIn("cannot be read", err)
        self.assertEqual(self.lib.read_record(
            self.lib.resolve("pln_aaaaaaaaaaaa"))["closure"]["state"], "retired")
        # Repair the record and the same reopen goes through.
        record.pop("a_field_this_schema_does_not_know")
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(self.run_command("reopen", "pln_aaaaaaaaaaaa")[0], 0)

    def test_reopen_fails_closed_when_the_claimed_program_record_will_not_parse(self):
        """The strictly more damaged case a reviewer proved open: a TRUNCATED record names no
        children, so the record-side sweep saw nothing — while the plan's own back-link, the
        evidence that survives an unparseable record, was never consulted. It is now, the same
        way the seal path consults it."""
        program_id = self._program_with_child()
        self.assertEqual(self.run_command("retire", "pln_aaaaaaaaaaaa",
                                          "--reason", "set aside")[0], 0)
        programs = plan_program.ProgramLibrary(self.lib)
        path = programs.program_dir(programs.resolve(program_id)) / "record.json"
        path.write_text("{ truncated", encoding="utf-8")
        code, _, err = self.run_command("reopen", "pln_aaaaaaaaaaaa")
        self.assertEqual(code, 2)
        self.assertIn("cannot be read", err)
        self.assertEqual(self.lib.read_record(
            self.lib.resolve("pln_aaaaaaaaaaaa"))["closure"]["state"], "retired")

    def test_reopen_refuses_when_both_membership_sources_are_dark(self):
        """Two damaged files at once — an unparseable program record AND no readable back-link —
        used to pass in silence, while the sealing gate meets the identical library state and says
        so out loud. With both sources dark, membership is genuinely unknowable, so the door
        refuses like its neighbours."""
        program_id = self._program_with_child()
        self.assertEqual(self.run_command("retire", "pln_aaaaaaaaaaaa",
                                          "--reason", "set aside")[0], 0)
        programs = plan_program.ProgramLibrary(self.lib)
        path = programs.program_dir(programs.resolve(program_id)) / "record.json"
        path.write_text("{ not json at all", encoding="utf-8")
        # Strip the plan's own back-link, the second source.
        slug_a = self.lib.resolve("pln_aaaaaaaaaaaa")
        head = dict(self.lib.head(slug_a))
        head.pop("program", None)
        head["revision"] = 2
        head["revision_note"] = "a legacy child with no back-link"
        self.lib.append_revision(slug_a, head, expected_revision=1)
        code, _, err = self.run_command("reopen", "pln_aaaaaaaaaaaa")
        self.assertEqual(code, 2)
        self.assertIn("cannot even be parsed", err)
        self.assertIn("no readable program back-link", err)
        self.assertEqual(self.lib.read_record(slug_a)["closure"]["state"], "retired")

    def test_reopen_hears_the_veto_of_every_record_naming_the_plan(self):
        """Two-program membership is off-design but constructible by a legacy record, and the
        first-found narrowing silently lost the second record's say — a reviewer reopened a child
        under a program recorded complete that way."""
        program_id = self._program_with_child()
        self.assertEqual(self.run_command("retire", "pln_aaaaaaaaaaaa",
                                          "--reason", "set aside")[0], 0)
        # A second, hand-shaped record that also names the plan and is recorded complete.
        programs = plan_program.ProgramLibrary(self.lib)
        first_slug = programs.resolve(program_id)
        import shutil
        second_slug = "zeta-also-names-it--ffffff"
        shutil.copytree(programs.program_dir(first_slug), programs.program_dir(second_slug))
        record_path = programs.program_dir(second_slug) / "record.json"
        record = json.loads(record_path.read_text())
        record["program_id"] = "prg_ffffffffffff"
        record["slug"] = second_slug
        record["closure"] = {"state": "complete", "at": "2026-08-29T09:00:00Z",
                             "reason": "recorded done"}
        record_path.write_text(json.dumps(record), encoding="utf-8")
        code, _, err = self.run_command("reopen", "pln_aaaaaaaaaaaa")
        self.assertEqual(code, 2)
        self.assertIn("prg_ffffffffffff", err)
        self.assertIn("program reopen", err)

    def test_a_schema_invalid_child_record_is_unreadable_not_a_crash(self):
        """One child whose record fails schema validation used to crash every reader stacked on
        `child_view` — report, render, and BOTH closure gates — leaving its program permanently
        unclosable through every verb. It is an `unreadable` row with the acknowledged exit."""
        program_id = self._program_with_child()
        slug_a = self.lib.resolve("pln_aaaaaaaaaaaa")
        record_path = self.lib.plan_dir(slug_a) / "record.json"
        record = json.loads(record_path.read_text())
        record.pop("schema_version")
        record_path.write_text(json.dumps(record), encoding="utf-8")
        code, out, _ = self.run_command("program", "show", program_id)
        self.assertEqual(code, 0)
        self.assertIn("unreadable", out)
        code, _, err = self.run_command("program", "abandon", program_id, "--reason", "wrecked")
        self.assertEqual(code, 2)
        self.assertIn("cannot be computed", err)
        code, out, _ = self.run_command("program", "abandon", program_id, "--reason", "wrecked",
                                        "--acknowledge-unknown", "the child record is corrupt")
        self.assertEqual(code, 0, out)

    def test_supersede_recovery_converges_only_over_a_retirement(self):
        """An abandoned or completed target is someone's different decision, not this verb's own
        crash debris — converging over it would fold that decision into a supersession nobody
        made of it."""
        program_id = self._superseded_setup()
        self.assertEqual(self.run_command("abandon", "pln_bbbbbbbbbbbb",
                                          "--reason", "dropped, separately")[0], 0)
        self._plan_doc(program_id, "pln_cccccccccccc", "A replacement",
                       self._obligation("OB-1", "Cut over.", "satisfied"),
                       predecessor="pln_aaaaaaaaaaaa")
        code, _, err = self.run_command("program", "supersede", program_id, "pln_bbbbbbbbbbbb",
                                        "--with", "pln_cccccccccccc", "--reason", "converge?")
        self.assertEqual(code, 2)
        self.assertIn("not the half-finished supersession", err)
        record = plan_program.ProgramLibrary(self.lib).read(
            plan_program.ProgramLibrary(self.lib).resolve(program_id))
        self.assertIsNone(next(c for c in record["children"]
                               if c["plan_id"] == "pln_bbbbbbbbbbbb").get("superseded_by"))

    def test_supersede_recovery_over_a_retirement_completes_and_reprojects(self):
        """The genuine half-state — retired by an earlier run that died — converges on re-run,
        including the projection the earlier run may also have died before."""
        program_id = self._superseded_setup()
        self.assertEqual(self.run_command("retire", "pln_bbbbbbbbbbbb",
                                          "--reason", "an earlier run got this far")[0], 0)
        self._plan_doc(program_id, "pln_cccccccccccc", "A replacement",
                       self._obligation("OB-1", "Cut over.", "satisfied"),
                       predecessor="pln_aaaaaaaaaaaa")
        code, out, err = self.run_command("program", "supersede", program_id, "pln_bbbbbbbbbbbb",
                                          "--with", "pln_cccccccccccc", "--reason", "converge")
        self.assertEqual(code, 0, err)
        self.assertIn("was already retired; completing the supersession", out)
        self.assertIn("pln_cccccccccccc supersedes pln_bbbbbbbbbbbb", out)

    def test_a_repeated_release_reports_the_reason_the_record_holds(self):
        """Idempotence must not narrate a write that never happened: the second run's differing
        reason is not stored, so it is not what gets printed."""
        program_id = self._program_with_child()
        self._plan_doc(program_id, "pln_bbbbbbbbbbbb", "PR B",
                       self._obligation("OB-1", "Still carried."),
                       predecessor="pln_aaaaaaaaaaaa")
        self.assertEqual(self.run_command("program", "add", program_id, "pln_bbbbbbbbbbbb",
                                          "--after", "pln_aaaaaaaaaaaa")[0], 0)
        self.assertEqual(self.run_command("abandon", "pln_bbbbbbbbbbbb",
                                          "--reason", "dropped")[0], 0)
        first = self.run_command("program", "release", program_id, "pln_aaaaaaaaaaaa",
                                 "--obligation", "OB-1", "--reason", "the original reason")
        self.assertEqual(first[0], 0, first[2])
        code, out, err = self.run_command("program", "release", program_id, "pln_aaaaaaaaaaaa",
                                          "--obligation", "OB-1", "--reason", "a different story")
        self.assertEqual(code, 0, err)
        self.assertIn("the original reason", out)
        self.assertIn("was not written", out)
        self.assertNotIn("a different story\n", out)

    def test_clone_supersedes_sources_obligations_from_the_predecessor(self):
        """Not from the plan being replaced: its own claims describe work that never landed."""
        program_id = self._superseded_setup()
        code, out, err = self.run_command("clone", "pln_bbbbbbbbbbbb", "--supersedes",
                                          "pln_bbbbbbbbbbbb", "--reason", "the shape was wrong")
        self.assertEqual(code, 0, err)
        self.assertIn("Pre-filled to supersede pln_bbbbbbbbbbbb", out)
        self.assertIn("OB-1", out)
        clone_id = out.split("into ")[1].split()[0]
        document = self.lib.head(self.lib.resolve(clone_id))
        program = document["program"]
        self.assertEqual(program["program_id"], program_id)
        self.assertEqual(program["predecessor_plan_id"], "pln_aaaaaaaaaaaa")
        # B SATISFIED OB-1. The clone re-declares it as CARRIED, because B never landed.
        self.assertEqual([(o["id"], o["state"]) for o in program["carried_obligations"]],
                         [("OB-1", "carried")])
        record = self.lib.read_record(self.lib.resolve(clone_id))
        for evidence in ("approval", "plan_review", "seal"):
            self.assertIsNone(record.get(evidence), evidence)

    def test_clone_supersedes_does_not_resurrect_a_released_obligation(self):
        """The one release-subtraction that can fire, and the only one worth a fixture.

        The predecessor here has been a child for as long as the release has existed, so a release
        against it is real. Pre-filling it back as `carried` would quietly reverse a decision the
        operator recorded, in a generated block they are least likely to re-read.
        """
        program_id = self._superseded_setup()          # A carries OB-1; B satisfies it
        # A release needs no live successor left to answer — which is exactly the shape supersede
        # meets, since the plan being replaced is retired first. Retire B, then the release is open.
        self._close_plan_record("pln_bbbbbbbbbbbb", "retired", "the approach was wrong")
        self.assertEqual(self.run_command("program", "release", program_id, "pln_aaaaaaaaaaaa",
                                          "--obligation", "OB-1",
                                          "--reason", "the work it awaited is void")[0], 0)
        code, out, err = self.run_command("clone", "pln_bbbbbbbbbbbb", "--supersedes",
                                          "pln_bbbbbbbbbbbb", "--reason", "try again")
        self.assertEqual(code, 0, err)
        self.assertIn("nothing outstanding to inherit", out)
        clone_id = out.split("into ")[1].split()[0]
        program = self.lib.head(self.lib.resolve(clone_id))["program"]
        self.assertNotIn("carried_obligations", program,
                         "a released obligation must not come back as carried")

    def test_clone_supersedes_refuses_a_plan_in_no_program(self):
        self._plan_doc_standalone = _document(plan_id="pln_dddddddddddd", title="Standalone")
        self.lib.create(self._plan_doc_standalone)
        code, _, err = self.run_command("clone", "pln_dddddddddddd", "--supersedes",
                                        "pln_dddddddddddd", "--reason", "why")
        self.assertEqual(code, 2)
        self.assertIn("is not a child of any program", err)

    def _close_plan_record(self, plan_id, state, reason="x"):
        self.lib.update_record(self.lib.resolve(plan_id), lambda r: r.update({"closure": {
            "state": state, "at": "2026-08-29T05:00:00Z", "reason": reason}}))

    def test_closing_over_a_debt_is_refused_at_the_command_line_and_release_clears_it(self):
        program_id = self._program_with_child()          # child A carries OB-1
        code, _, err = self.run_command("program", "retire", program_id, "--reason", "setting down")
        self.assertEqual(code, 2)
        self.assertIn("OB-1", err)
        self.assertIn("program release", err)

        code, out, err = self.run_command("program", "release", program_id, "pln_aaaaaaaaaaaa",
                                          "--obligation", "OB-1",
                                          "--reason", "the successor was abandoned, so it is void")
        self.assertEqual(code, 0, err)
        self.assertIn("released OB-1", out)
        self.assertIn("PROGRAM level", out)
        self.assertEqual(self.run_command("program", "retire", program_id,
                                          "--reason", "setting down")[0], 0)
        self.assertIn("released at PROGRAM level",
                      self.run_command("program", "show", program_id)[1])

    def test_completion_takes_the_verb_and_the_list_never_says_complete_on_its_own(self):
        program_id = self._program_with_child()
        self.run_command("program", "release", program_id, "pln_aaaaaaaaaaaa",
                         "--obligation", "OB-1", "--reason", "void")
        self._close_plan_record("pln_aaaaaaaaaaaa", "complete", "merged")
        listed = self.run_command("program", "list")[1]
        # The one word an operator scans must not be the one they will read as finished, so assert
        # the status COLUMN rather than a substring — `complete` legitimately occurs inside the
        # token, and a substring check would pass for the very lie this is guarding against.
        status = listed.split()[1]
        self.assertEqual(status, "children-complete")
        self.assertNotEqual(status, "complete")
        shown = self.run_command("program", "show", program_id)[1]
        self.assertIn("This program is not recorded as complete", shown)

        code, out, err = self.run_command("program", "complete", program_id,
                                          "--reason", "the objective is met")
        self.assertEqual(code, 0, err)
        self.assertIn("is recorded complete", out)
        self.assertIn("Recorded, not derived", out)
        self.assertIn("recorded by an explicit close, not derived",
                      self.run_command("program", "show", program_id)[1])

    def test_reopen_needs_a_reason_and_the_undone_closure_is_rendered(self):
        program_id = self._program_with_child()
        self.run_command("program", "release", program_id, "pln_aaaaaaaaaaaa",
                         "--obligation", "OB-1", "--reason", "void")
        self.assertEqual(self.run_command("program", "retire", program_id, "--reason", "down")[0], 0)
        with self.assertRaises(SystemExit):        # argparse refuses the missing --reason itself
            self.run_command("program", "reopen", program_id)
        code, out, err = self.run_command("program", "reopen", program_id,
                                          "--reason", "the evidence changed")
        self.assertEqual(code, 0, err)
        self.assertIn("it was retired", out)
        shown = self.run_command("program", "show", program_id)[1]
        self.assertIn("Closures that were undone", shown)
        self.assertIn("the evidence changed", shown)

    def test_an_unknown_closes_only_through_the_recorded_acknowledgement(self):
        program_id = self._program_with_child()
        self.run_command("program", "release", program_id, "pln_aaaaaaaaaaaa",
                         "--obligation", "OB-1", "--reason", "void")
        programs_root = self.lib.root / "programs"
        record_path = next(programs_root.iterdir()) / "record.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["children"].append({"plan_id": "pln_ffffffffffff",
                                   "added_at": "2026-08-29T06:00:00Z",
                                   "predecessor_plan_id": "pln_aaaaaaaaaaaa"})
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

        code, _, err = self.run_command("program", "abandon", program_id, "--reason", "giving up")
        self.assertEqual(code, 2)
        self.assertIn("--acknowledge-unknown", err)
        code, out, err = self.run_command("program", "abandon", program_id, "--reason", "giving up",
                                          "--acknowledge-unknown", "that child was never authored")
        self.assertEqual(code, 0, err)
        self.assertIn("Closed over an unknown", out)
        self.assertIn("Closed over an unknown",
                      self.run_command("program", "show", program_id)[1])

    def test_a_program_level_release_unblocks_the_seal_side_check(self):
        """The seal gate itself, not only the sweep it reads.

        A release honored by `obligation_report` but not by the gate that refuses a seal would be
        the worst shape available: the debt disappears from the count an operator scans and
        reappears at the door refusing their next action. This drives the real seal-side check.
        """
        import project_manager
        program_id = self._program_with_child()
        self._plan_doc(program_id, "pln_bbbbbbbbbbbb", "PR B", predecessor="pln_aaaaaaaaaaaa")
        # B answers for nothing, so it can only join once OB-1 is released at program level.
        code, _, err = self.run_command("program", "add", program_id, "pln_bbbbbbbbbbbb",
                                        "--after", "pln_aaaaaaaaaaaa")
        self.assertEqual(code, 2)
        self.assertIn("OB-1", err)

        slug_b = self.lib.resolve("pln_bbbbbbbbbbbb")
        record_b = self.lib.read_record(slug_b)
        document_b = self.lib.head(slug_b)

        self.run_command("program", "release", program_id, "pln_aaaaaaaaaaaa",
                         "--obligation", "OB-1", "--reason", "its successor was abandoned")
        self.assertEqual(self.run_command("program", "add", program_id, "pln_bbbbbbbbbbbb",
                                          "--after", "pln_aaaaaaaaaaaa")[0], 0)
        refusals, _ = project_manager._program_check(self.lib, record_b, document_b)
        self.assertEqual([r for r in refusals if "OB-1" in r], [],
                         "the seal-side check must honour the release the report already honoured")

    def test_revise_objective_shows_both_texts_and_keeps_the_old_one(self):
        program_id = self._program_with_child()
        code, out, err = self.run_command("program", "revise-objective", program_id,
                                          "--objective", "What it is actually for.",
                                          "--reason", "the original wording went stale")
        self.assertEqual(code, 0, err)
        self.assertIn("Previously: Delivered across two PRs.", out)
        self.assertIn("Now:        What it is actually for.", out)
        shown = self.run_command("program", "show", program_id)[1]
        self.assertIn("How the objective has been revised", shown)
        self.assertIn("Delivered across two PRs.", shown)
        self.assertIn("the original wording went stale", shown)

    def test_the_verb_writes_no_position_for_either_door(self):
        program_id = self._program_with_child()
        record = json.loads((self.lib.root / "programs" / next(
            d.name for d in (self.lib.root / "programs").iterdir()) / "record.json")
            .read_text(encoding="utf-8"))
        self.assertTrue(all("position" not in child for child in record["children"]))


class LaneCommands(ProgramVerbs):
    """`program lanes set|clear` at the command line: it records the operator's decided split,
    surfaces every input refusal honestly, and the emitted set line round-trips."""

    def _two_child_program(self):
        program_id = self._program_with_child()   # child pln_aaaaaaaaaaaa, carrying OB-1
        self._plan_doc(program_id, "pln_bbbbbbbbbbbb", "PR B",
                       self._obligation("OB-1", "Cut over.", "satisfied"),
                       predecessor="pln_aaaaaaaaaaaa")
        self.assertEqual(self.run_command("program", "add", program_id, "pln_bbbbbbbbbbbb",
                                          "--after", "pln_aaaaaaaaaaaa")[0], 0)
        return program_id

    def _record(self, program_id):
        programs = plan_program.ProgramLibrary(self.lib)
        return programs.read(programs.resolve(program_id))

    def test_lanes_set_records_the_decided_split(self):
        program_id = self._two_child_program()
        code, out, err = self.run_command(
            "program", "lanes", "set", program_id,
            "--lane", "fast=pln_aaaaaaaaaaaa", "--lane", "slow=pln_bbbbbbbbbbbb",
            "--reason", "the two touch different files")
        self.assertEqual(code, 0, err)
        self.assertIn("2-lane split", out)
        self.assertIn("Advisory only", out)
        record = self._record(program_id)
        self.assertEqual(record["lanes"]["lanes"],
                         [{"name": "fast", "children": ["pln_aaaaaaaaaaaa"]},
                          {"name": "slow", "children": ["pln_bbbbbbbbbbbb"]}])
        self.assertEqual(record["lanes"]["reason"], "the two touch different files")

    def test_set_override_then_clear_keeps_a_discriminated_history(self):
        program_id = self._two_child_program()
        self.assertEqual(self.run_command(
            "program", "lanes", "set", program_id,
            "--lane", "both=pln_aaaaaaaaaaaa,pln_bbbbbbbbbbbb", "--reason", "together")[0], 0)
        self.assertEqual(self.run_command(
            "program", "lanes", "set", program_id,
            "--lane", "a=pln_aaaaaaaaaaaa", "--lane", "b=pln_bbbbbbbbbbbb", "--reason", "apart")[0], 0)
        code, out, _ = self.run_command("program", "lanes", "clear", program_id,
                                        "--reason", "hold off on concurrency")
        self.assertEqual(code, 0)
        self.assertIn("cleared the lane split", out)
        record = self._record(program_id)
        self.assertNotIn("lanes", record)   # nothing stands after a clear
        self.assertEqual([entry["ended_by"] for entry in record["lanes_history"]],
                         ["replaced", "cleared"])

    def test_a_lane_refusal_surfaces_at_the_command_line(self):
        program_id = self._two_child_program()
        code, _, err = self.run_command("program", "lanes", "set", program_id,
                                        "--lane", "L=pln_ffffffffffff", "--reason", "r")
        self.assertEqual(code, 2)
        self.assertIn("not stored in this program", err)

    def test_the_missing_from_library_refusal_is_honest_at_the_cli(self):
        program_id = self._two_child_program()
        programs = plan_program.ProgramLibrary(self.lib)
        slug = programs.resolve(program_id)
        record = programs.read(slug)
        record["children"].append({"plan_id": "pln_d00000000004",
                                   "added_at": "2026-01-01T00:00:00Z",
                                   "predecessor_plan_id": "pln_bbbbbbbbbbbb"})
        programs._write(slug, record)
        code, _, err = self.run_command("program", "lanes", "set", program_id,
                                        "--lane", "L=pln_d00000000004", "--reason", "r")
        self.assertEqual(code, 2)
        self.assertIn("missing from this library", err)

    def test_a_malformed_lane_spec_is_refused_with_guidance(self):
        program_id = self._two_child_program()
        code, _, err = self.run_command("program", "lanes", "set", program_id,
                                        "--lane", "no-equals-here", "--reason", "r")
        self.assertEqual(code, 2)
        self.assertIn("NAME=plan", err)

    def test_a_set_line_round_trips_through_the_cli(self):
        # `program lanes propose` ends its output with exactly this command shape; the round-trip is
        # pinned here so that emitted line records the split it printed.
        program_id = self._two_child_program()
        argv = ["program", "lanes", "set", program_id, "--reason", "proposed split",
                "--lane", "fast=pln_aaaaaaaaaaaa", "--lane", "slow=pln_bbbbbbbbbbbb"]
        self.assertEqual(self.run_command(*argv)[0], 0)
        record = self._record(program_id)
        self.assertEqual([lane["name"] for lane in record["lanes"]["lanes"]], ["fast", "slow"])
        self.assertEqual([lane["children"] for lane in record["lanes"]["lanes"]],
                         [["pln_aaaaaaaaaaaa"], ["pln_bbbbbbbbbbbb"]])

    def _territory_plan(self, program_id, plan_id, paths, predecessor=None):
        document = _document(plan_id=plan_id, title=plan_id[-4:])
        document["build_plan"]["work_items"] = [{
            "id": "w", "description": "d", "paths": list(paths), "depends_on": [],
            "exclusive_resources": [], "executor_class": "builder", "verification": ["v"],
            "output_contract": {"deliverable": "x", "artifact_kinds": ["code"],
                                "required_evidence": ["t"]}}]
        program = {"program_id": program_id}
        if predecessor:
            program["predecessor_plan_id"] = predecessor
        document["program"] = program
        self.lib.create(document)
        after = ("--after", predecessor) if predecessor else ()
        self.assertEqual(self.run_command("program", "add", program_id, plan_id, *after)[0], 0)

    def _disjoint_program(self):
        _, out, _ = self.run_command("program", "new", "--title", "Lanes",
                                     "--objective", "Ride in parallel.")
        program_id = out.split()[2]
        self._territory_plan(program_id, "pln_aaaaaaaaaaaa", ["x.py"])
        self._territory_plan(program_id, "pln_bbbbbbbbbbbb", ["y.py"],
                             predecessor="pln_aaaaaaaaaaaa")
        return program_id

    def test_propose_renders_lanes_and_a_set_line_and_writes_nothing(self):
        program_id = self._disjoint_program()
        code, out, err = self.run_command("program", "lanes", "propose", program_id)
        self.assertEqual(code, 0, err)
        self.assertIn("## Lanes", out)
        self.assertIn("program lanes set", out)
        self.assertIn("declared work-item paths only", out)
        self.assertNotIn("lanes", self._record(program_id))   # a pure read wrote nothing

    def test_the_emitted_set_line_round_trips_through_the_real_cli(self):
        import shlex
        program_id = self._disjoint_program()
        expected = plan_program.ProgramLibrary(self.lib).propose_lanes(
            plan_program.ProgramLibrary(self.lib).resolve(program_id))
        out = self.run_command("program", "lanes", "propose", program_id)[1]
        set_line = next(line for line in out.splitlines() if "program lanes set" in line)
        parts = shlex.split(set_line)          # python tools/program_manager.py program lanes set ...
        self.assertIn("python", parts)         # the emitted line is runnable, not a bare `program ...`
        parts = parts[parts.index("program"):]  # slice from the subcommand for this in-process runner
        code, _, err = self.run_command(*parts)
        self.assertEqual(code, 0, err)
        recorded = self._record(program_id)["lanes"]["lanes"]
        self.assertEqual([{"name": lane["name"], "children": lane["children"]} for lane in recorded],
                         [{"name": lane["name"], "children": lane["members"]}
                          for lane in expected["lanes"]])

    def test_propose_amend_and_fresh_are_labelled(self):
        program_id = self._disjoint_program()
        self.assertEqual(self.run_command(
            "program", "lanes", "set", program_id,
            "--lane", "keep=pln_aaaaaaaaaaaa", "--reason", "recorded")[0], 0)
        amend = self.run_command("program", "lanes", "propose", program_id)[1]
        self.assertIn("amending around the recorded split", amend)
        fresh = self.run_command("program", "lanes", "propose", program_id, "--fresh")[1]
        self.assertIn("set aside", fresh)

    def test_a_cap_forced_merge_is_not_called_a_collision_at_the_cli(self):
        program_id = self._disjoint_program()   # two disjoint children over x.py and y.py
        out = self.run_command("program", "lanes", "propose", program_id, "--max-lanes", "1")[1]
        # The false-collision headline must NOT appear for a capacity merge (the word "collide" itself
        # legitimately appears in the standing declared-paths caveat, so assert the headline, not the word).
        self.assertNotIn("Concurrency is not recommended", out)
        self.assertIn("lane ceiling", out)
        self.assertIn("because of a territory collision", out)
        self.assertIn("Raise --max-lanes", out)

    def test_the_emitted_set_line_is_runnable_as_printed(self):
        program_id = self._disjoint_program()
        out = self.run_command("program", "lanes", "propose", program_id)[1]
        set_line = next(line for line in out.splitlines() if "program lanes set" in line)
        self.assertIn("python tools/program_manager.py program lanes set", set_line)

    def test_a_child_contending_with_two_lanes_renders_in_the_unplaced_section(self):
        _, out, _ = self.run_command("program", "new", "--title", "Bridge",
                                     "--objective", "A bridging child.")
        program_id = out.split()[2]
        self._territory_plan(program_id, "pln_aaaaaaaaaaaa", ["a.py"])
        self._territory_plan(program_id, "pln_bbbbbbbbbbbb", ["b.py"], predecessor="pln_aaaaaaaaaaaa")
        self._territory_plan(program_id, "pln_cccccccccccc", ["a.py", "b.py"],
                             predecessor="pln_bbbbbbbbbbbb")
        shown = self.run_command("program", "lanes", "propose", program_id)[1]
        self.assertIn("Unplaced — contends with more than one open lane", shown)
        self.assertIn("pln_cccccccccccc", shown.split("Unplaced")[1])

    def test_program_show_renders_the_lanes_section_and_history(self):
        program_id = self._disjoint_program()
        # No section before a split is recorded.
        self.assertNotIn("## Lanes", self.run_command("program", "show", program_id)[1])
        self.assertEqual(self.run_command(
            "program", "lanes", "set", program_id,
            "--lane", "fast=pln_aaaaaaaaaaaa", "--lane", "slow=pln_bbbbbbbbbbbb",
            "--reason", "by territory")[0], 0)
        shown = self.run_command("program", "show", program_id)[1]
        self.assertIn("## Lanes", shown)
        self.assertIn("by territory", shown)
        self.assertIn("**fast**", shown)
        # After a clear, no current section but a discriminated history entry.
        self.assertEqual(self.run_command("program", "lanes", "clear", program_id,
                                          "--reason", "pause")[0], 0)
        cleared = self.run_command("program", "show", program_id)[1]
        self.assertIn("## Lane splits that stopped standing", cleared)
        self.assertIn("**cleared**", cleared)


class TheListAndShowPointAtThePortfolio(_ProgramSurface):
    """The move ships the portfolio verb before PR 5's doctrine exists, so the tool points at it: list
    and show both print the next-step hint. The verb itself renders and writes nothing."""

    def _new_program(self):
        out = self.run_command("program", "new", "--title", "Pointer",
                               "--objective", "Deliver the thing across PRs.")[1]
        return out.split()[2]

    def test_list_names_the_portfolio_verb(self):
        self._new_program()
        self.assertIn("`program portfolio`", self.run_command("program", "list")[1])

    def test_show_names_the_portfolio_verb(self):
        program_id = self._new_program()
        self.assertIn("`program portfolio`", self.run_command("program", "show", program_id)[1])

    def test_portfolio_renders_the_open_program(self):
        program_id = self._new_program()
        code, out, err = self.run_command("program", "portfolio")
        self.assertEqual(code, 0, err)
        self.assertIn("# Programs — portfolio", out)
        self.assertIn("## In flight (1)", out)
        self.assertIn("Pointer", out)

    def test_portfolio_writes_nothing_to_the_record(self):
        program_id = self._new_program()
        programs = plan_program.ProgramLibrary(self.lib)
        slug = programs.resolve(program_id)
        before = (self.lib.root / "programs" / slug / "record.json").read_bytes()
        self.run_command("program", "portfolio")
        after = (self.lib.root / "programs" / slug / "record.json").read_bytes()
        self.assertEqual(before, after, "portfolio is a pure read; it must not touch a record")


class TheProgramMdRefreshesWithEveryMutatingVerb(_ProgramSurface):
    """Every mutating program verb regenerates the touched program's PROGRAM.md after its record write,
    and does it OUTSIDE its own failure path: a projection failure warns and leaves the verb succeeding,
    the record written, and the stale file to converge on the next verb or a `program reproject` sweep."""

    def _md(self, program_id):
        programs = plan_program.ProgramLibrary(self.lib)
        return (self.lib.root / "programs" / programs.resolve(program_id) / "PROGRAM.md").read_text(
            encoding="utf-8")

    def _new(self, title="Alpha", objective="Deliver Alpha across PRs."):
        return self.run_command("program", "new", "--title", title, "--objective", objective)[1].split()[2]

    def _seed_child(self, program_id, plan_id, title):
        doc = _document(plan_id=plan_id, title=title)
        doc["program"] = {"program_id": program_id}
        self.lib.create(doc)

    def _seal(self, plan_id):
        slug = self.lib.resolve(plan_id)
        digest = self.lib.read_record(slug)["current"]["plan_digest"]
        self.lib.update_record(slug, lambda r: r.update({"seal": {
            "revision": 1, "reviewed_digest": digest, "sealed_digest": digest,
            "build_plan_digest": digest, "at": "2026-08-23T03:00:00Z", "delta_judgment": "none"}}))

    def test_new_writes_a_fresh_projection_with_a_visible_generated_line(self):
        program_id = self._new()
        md = self._md(program_id)
        self.assertTrue(md.startswith("> **Generated "))       # visible, not an HTML comment
        self.assertIn("Alpha", md)

    def test_add_reflects_the_new_child(self):
        program_id = self._new()
        self._seed_child(program_id, "pln_aaaaaaaaaaaa", "First child")
        self.assertEqual(self.run_command("program", "add", program_id, "pln_aaaaaaaaaaaa")[0], 0)
        self.assertIn("First child", self._md(program_id))

    def test_insert_reflects_the_reordered_chain(self):
        program_id = self._new()
        self._seed_child(program_id, "pln_aaaaaaaaaaaa", "First child")
        self.run_command("program", "add", program_id, "pln_aaaaaaaaaaaa")
        self._seed_child(program_id, "pln_cccccccccccc", "Inserted ahead")
        self.assertEqual(self.run_command("program", "insert", program_id, "pln_cccccccccccc",
                                          "--before", "pln_aaaaaaaaaaaa")[0], 0)
        self.assertIn("Inserted ahead", self._md(program_id))

    def test_supersede_reflects_the_replacement(self):
        program_id = self._new()
        self._seed_child(program_id, "pln_aaaaaaaaaaaa", "First child")
        self.run_command("program", "add", program_id, "pln_aaaaaaaaaaaa")
        self._seal("pln_aaaaaaaaaaaa")
        self._seed_child(program_id, "pln_dddddddddddd", "The replacement")
        code, _, err = self.run_command("program", "supersede", program_id, "pln_aaaaaaaaaaaa",
                                        "--with", "pln_dddddddddddd", "--reason", "wrong shape")
        self.assertEqual(code, 0, err)
        self.assertIn("The replacement", self._md(program_id))

    def test_release_reflects_the_released_obligation(self):
        program_id = self._new()
        doc = _document(plan_id="pln_aaaaaaaaaaaa", title="First child")
        doc["program"] = {"program_id": program_id,
                          "carried_obligations": [{"id": "OB-9", "statement": "a debt", "state": "carried"}]}
        self.lib.create(doc)
        self.run_command("program", "add", program_id, "pln_aaaaaaaaaaaa")
        before = self._md(program_id)                                  # OB-9 is outstanding before the release
        self.assertNotIn("released along the way", before)             # ...and not yet in the released tail
        self.assertIn("OB-9", before.split("still carried", 1)[1].split("released along", 1)[0])
        self.run_command("program", "release", program_id, "pln_aaaaaaaaaaaa",
                         "--obligation", "OB-9", "--reason", "its successor was abandoned")
        md = self._md(program_id)                               # the release is reflected...
        self.assertIn("released along the way", md)
        self.assertIn("OB-9", md.split("released along the way", 1)[1])  # ...OB-9 now sits under released

    def test_revise_objective_reflects_the_new_objective(self):
        program_id = self._new()
        self.run_command("program", "revise-objective", program_id,
                         "--objective", "A wholly new objective.", "--reason", "clarity")
        self.assertIn("A wholly new objective.", self._md(program_id))

    def test_retire_reflects_the_recorded_closure(self):
        program_id = self._new()
        self.run_command("program", "retire", program_id, "--reason", "shelving")
        self.assertIn("retired", self._md(program_id))

    def test_complete_reflects_the_recorded_completion(self):
        program_id = self._new()          # completion needs something shipped: a child recorded complete
        self._seed_child(program_id, "pln_aaaaaaaaaaaa", "First child")
        self.run_command("program", "add", program_id, "pln_aaaaaaaaaaaa")
        self.lib.update_record(self.lib.resolve("pln_aaaaaaaaaaaa"), lambda r: r.update(
            {"closure": {"state": "complete", "at": "2026-08-29T05:00:00Z", "reason": "merged"}}))
        code, _, err = self.run_command("program", "complete", program_id, "--reason", "the objective is met")
        self.assertEqual(code, 0, err)
        self.assertIn("recorded by an explicit close", self._md(program_id))   # the closure, not a derivation

    def test_reopen_reflects_the_reversed_closure(self):
        program_id = self._new()
        self.run_command("program", "retire", program_id, "--reason", "shelving")
        self.assertIn("retired", self._md(program_id))
        self.run_command("program", "reopen", program_id, "--reason", "back on")
        self.assertNotIn("Status**: retired", self._md(program_id))   # no longer a retired program

    def test_lanes_set_reflects_the_split(self):
        program_id = self._new()
        self._seed_child(program_id, "pln_aaaaaaaaaaaa", "First child")
        self.run_command("program", "add", program_id, "pln_aaaaaaaaaaaa")
        self.run_command("program", "lanes", "set", program_id,
                         "--lane", "solo=pln_aaaaaaaaaaaa", "--reason", "one lane")
        self.assertIn("solo", self._md(program_id))

    def test_lanes_clear_reflects_the_withdrawn_split(self):
        program_id = self._new()
        self._seed_child(program_id, "pln_aaaaaaaaaaaa", "First child")
        self.run_command("program", "add", program_id, "pln_aaaaaaaaaaaa")
        self.run_command("program", "lanes", "set", program_id,
                         "--lane", "solo=pln_aaaaaaaaaaaa", "--reason", "one lane")
        self.run_command("program", "lanes", "clear", program_id, "--reason", "no longer split")
        self.assertIn("stopped standing", self._md(program_id))       # the cleared split, in lane history

    def test_a_projection_failure_never_fails_the_verb_and_converges_on_a_later_sweep(self):
        program_id = self._new()          # this new() already wrote a fresh projection
        with mock.patch.object(program_projection, "project_program",
                               side_effect=RuntimeError("disk full")):
            code, _, err = self.run_command("program", "revise-objective", program_id,
                                            "--objective", "The second objective.", "--reason", "x")
        self.assertEqual(code, 0, "a projection failure must not fail the verb")
        self.assertIn("could not be regenerated", err)          # it warned on stderr
        programs = plan_program.ProgramLibrary(self.lib)
        self.assertEqual(programs.read(programs.resolve(program_id))["objective"],
                         "The second objective.", "the record itself must be written and correct")
        # The projection is stale (still the first objective); the record is right; a sweep converges it.
        self.assertNotIn("The second objective.", self._md(program_id))
        self.assertEqual(self.run_command("program", "reproject")[0], 0)
        self.assertIn("The second objective.", self._md(program_id))


if __name__ == "__main__":
    unittest.main()
