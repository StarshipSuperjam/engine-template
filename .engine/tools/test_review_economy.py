#!/usr/bin/env python3
"""The review economy: receipts bound to what a lens read, and reviews spent only on unread work.

Every class here replays a real incident rather than exercising a predicate. The operator's rule out of
the build that produced most of them: lenses run to do work; they are not ceremony.

  * `ThePr1063Replay` — the build where three separate mechanics combined to demand cold reviews that
    would have found nothing (StarshipSuperjam/engine-template#1065). Driven against a REAL throwaway
    git repository, because the whole change is commit-range arithmetic and a fake SHA proves none of it.
  * `TheBatchForm` — the collapsed shell array that made a receipt demand a bogus id
    (StarshipSuperjam/engine-template#1060).
  * `TheB1EffortShortfall` — the sealed `thorough` whose panel ran at `medium` and said nothing
    (StarshipSuperjam/engine-template#1067).
  * `ThreeCodeExecutionBehaviours` — B2's carried finding CO-1.
  * `Issue1012BookkeepingTraps` — the four traps that lost long sessions in their own ceremony.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator as bc  # noqa: E402
import build_coordinator_review as review  # noqa: E402
import build_review_range as ranges  # noqa: E402

DERIVED_PATH = ".engine/docs/ci-assurance.md"      # a real derived-state member, owned by the registry


class _RealRepo(unittest.TestCase):
    """A throwaway git repository with real commits. The range arithmetic asks git real questions, so a
    fixture of `aaa…`-shaped SHAs would test the failure path and nothing else."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="review-economy-"))
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
        self.git("init", "-q", "-b", "main")
        self.base = self.commit("seed.txt", "seed")

    def git(self, *args) -> str:
        out = subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                             capture_output=True, text=True, env=self.env)
        return out.stdout.strip()

    def commit(self, path: str, body: str) -> str:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body + "\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"touch {path}")
        return self.git("rev-parse", "HEAD")

    def receipt(self, lens: str, base: str, tip: str, **over) -> dict:
        return {"lens": lens, "packet_digest": "sha256:" + "1" * 64, "commit": tip,
                "finding_ids": [], "code_execution": "none", "delivered_effort": "high",
                "reviewed_range": {"base": base, "tip": tip}, **over}


class ThePr1063Replay(_RealRepo):
    """Three mechanics, one build, all three demanding reviews that would do no work."""

    def test_a_derived_artifact_commit_is_not_something_a_reviewer_owes_a_read_of(self):
        authored = self.commit("src.py", "real work")
        generated = self.commit(DERIVED_PATH, "regenerated")
        self.assertFalse(ranges.is_derived_only(self.repo, authored),
                         "a commit touching authored source is authored")
        self.assertTrue(ranges.is_derived_only(self.repo, generated),
                        "a commit touching only registry-owned generated output is not a reviewer's work")
        self.assertEqual(ranges.authored_only(self.repo, ranges.commits(self.repo, authored, generated)), [])

    def test_the_batched_classification_agrees_with_the_per_commit_one(self):
        """Two implementations of one question is how a swap silently drifts. The per-commit reader is
        still the definition; the batched `git log` is the fast path, and it must not disagree."""
        self.commit("src.py", "authored one")
        self.commit(DERIVED_PATH, "generated")
        self.commit("src.py", "authored two")
        self.git("commit", "-q", "--allow-empty", "-m", "empty")
        tip = self.git("rev-parse", "HEAD")
        batched = {sha: derived for sha, derived in ranges._classified_range(self.repo, self.base, tip)}
        for sha in ranges.commits(self.repo, self.base, tip):
            self.assertEqual(batched[sha], ranges.is_derived_only(self.repo, sha), sha[:12])
        self.assertEqual(ranges.authored_between(self.repo, self.base, tip),
                         ranges.authored_only(self.repo, ranges.commits(self.repo, self.base, tip)))

    def test_the_range_is_classified_once_per_command(self):
        """The cost the caching exists to remove: `repair assess` asks to decide and asks again to
        explain, and the status render asks a third time."""
        tip = self.commit("src.py", "authored")
        ranges._AUTHORED_CACHE.clear()
        calls = []
        real = ranges._git
        ranges._git = lambda root, args: (calls.append(args[0]), real(root, args))[1]
        try:
            for _ in range(3):
                ranges.authored_between(self.repo, self.base, tip)
        finally:
            ranges._git = real
        self.assertEqual(calls.count("log"), 1, f"the range was re-shelled: {calls}")

    def test_an_empty_commit_counts_as_authored_rather_than_free(self):
        """Not a technicality. `is_derived_only` decides whether a lens owes a read, and a commit that
        touched nothing carries no evidence either way — so it fails toward asking."""
        self.git("commit", "-q", "--allow-empty", "-m", "empty")
        head = self.git("rev-parse", "HEAD")
        self.assertFalse(ranges.is_derived_only(self.repo, head))

    def test_two_true_receipts_survive_a_re_bind_over_generated_output(self):
        """The incident itself. Two lenses cold-read the repair and returned findings; a later re-bind —
        forced by `sync-artifacts` moving HEAD — erased both receipts, leaving a choice between running
        them again for no new work and abandoning the evidence."""
        reviewed = self.commit("src.py", "the deliverable")
        repaired = self.commit("src.py", "the repair")
        generated = self.commit(DERIVED_PATH, "regenerated")
        receipts = [self.receipt("usability", reviewed, repaired),
                    self.receipt("spec-conformance", reviewed, repaired)]
        for item in receipts:
            self.assertTrue(ranges.receipt_covers(self.repo, item, repaired, generated),
                            "a receipt that read the repair still answers for a range that only added "
                            "generated output")

    def test_the_gate_asks_only_for_the_unread_delta(self):
        """The half that makes carry-forward safe: a lens that HAS missed authored work is still asked,
        and told exactly what it missed rather than 'run it again'."""
        reviewed = self.commit("src.py", "the deliverable")
        repaired = self.commit("src.py", "the repair")
        self.commit(DERIVED_PATH, "regenerated")
        late = self.commit("src.py", "a second repair nobody has read")
        read_it_all = self.receipt("usability", reviewed, late)
        missed_the_tail = self.receipt("spec-conformance", reviewed, repaired)
        self.assertTrue(ranges.receipt_covers(self.repo, read_it_all, repaired, late))
        self.assertFalse(ranges.receipt_covers(self.repo, missed_the_tail, repaired, late))
        report = ranges.coverage_report(self.repo, missed_the_tail, repaired, late)
        self.assertIn("1 authored commit(s) unread", report)
        self.assertIn(late[:12], report, "the report must name the commit, not just the count")

    def test_a_receipt_with_no_recorded_range_covers_nothing(self):
        """A receipt written before ranges existed makes no claim about what it read. Reading that
        silence as full coverage would carry a receipt forward over work nobody looked at."""
        reviewed = self.commit("src.py", "the deliverable")
        authored = self.commit("src.py", "unread work")
        legacy = {"lens": "usability", "packet_digest": "sha256:" + "1" * 64,
                  "commit": reviewed, "finding_ids": [], "code_execution": "none"}
        self.assertFalse(ranges.receipt_covers(self.repo, legacy, reviewed, authored))

    def test_an_unreadable_range_fails_closed(self):
        """Losing a cold review to an unreadable history costs a re-run. Carrying one forward on a claim
        that cannot be checked costs the audit trail, so the cheap loss is the one taken."""
        reviewed = self.commit("src.py", "the deliverable")
        gone = self.receipt("usability", "f" * 40, "e" * 40)
        self.assertFalse(ranges.receipt_covers(self.repo, gone, self.base, reviewed))
        self.assertIn("cannot be measured", ranges.coverage_report(self.repo, gone, self.base, reviewed))


class TheRoundCounter(_RealRepo):
    """The third mechanic: the operator gate fired over accounting rather than over a failing build."""

    def _assess(self, state: dict, head: str, judgment="scoped", lenses=("usability",), **over):
        store = _Store(state)
        args = argparse.Namespace(judgment=judgment, rationale="r", lens=list(lenses) or None,
                                  guidance=None, **over)
        with mock.patch.object(bc, "ROOT", self.repo), \
                mock.patch.object(bc, "_head", return_value=head), \
                mock.patch.object(bc, "_must_run", return_value="1 file changed"), \
                mock.patch.object(bc, "_history_was_rewritten", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            bc.cmd_repair_assess(args, store)
        return store.state, err.getvalue()

    def test_a_bookkeeping_re_bind_does_not_open_a_new_round(self):
        reviewed = self.commit("src.py", "the deliverable")
        repaired = self.commit("src.py", "the repair")
        generated = self.commit(DERIVED_PATH, "regenerated")
        state = _state(reviewed_commit=reviewed, base_commit=self.base)
        state, _ = self._assess(state, repaired)
        self.assertEqual(len(state["repair_rounds"]), 1)
        # the round completed: its lens returned, so the anchor advances to the repaired commit
        state["repair"]["receipts"] = [self.receipt("usability", reviewed, repaired,
                                                    packet_digest=None)]
        state["repair"]["packet_digest"] = None
        state, err = self._assess(state, generated)
        self.assertEqual(len(state["repair_rounds"]), 1,
                         "re-pointing at a commit the engine generated itself is bookkeeping, not a round")
        self.assertIn("re-points the repair round already recorded", err)

    def test_real_work_past_a_completed_round_does_open_a_new_one(self):
        """The counter is not simply looser. Authored work the round's lenses have not read is a genuine
        second round and still counts toward the gate."""
        reviewed = self.commit("src.py", "the deliverable")
        repaired = self.commit("src.py", "the repair")
        state = _state(reviewed_commit=reviewed, base_commit=self.base)
        state, _ = self._assess(state, repaired)
        state["repair"]["receipts"] = [self.receipt("usability", reviewed, repaired, packet_digest=None)]
        more = self.commit("src.py", "a second repair")
        state, _ = self._assess(state, more)
        self.assertEqual(len(state["repair_rounds"]), 2)

    def test_a_repair_receipt_is_carried_forward_across_the_re_bind(self):
        reviewed = self.commit("src.py", "the deliverable")
        repaired = self.commit("src.py", "the repair")
        generated = self.commit(DERIVED_PATH, "regenerated")
        state = _state(reviewed_commit=reviewed, base_commit=self.base)
        state, _ = self._assess(state, repaired)
        state["repair"]["receipts"] = [self.receipt("usability", reviewed, repaired, packet_digest=None)]
        state, err = self._assess(state, generated)
        self.assertEqual([r["lens"] for r in state["repair"]["receipts"]], ["usability"])
        self.assertEqual(state["repair"]["receipts"][0]["reviewed_range"],
                         {"base": reviewed, "tip": repaired},
                         "the receipt is kept as it was recorded — never restamped onto the new packet, "
                         "because the finding keys hang off its digests")
        self.assertIn("carried 1 repair receipt(s) forward", err)

    def test_a_none_judgment_that_would_discard_evidence_stops_and_asks(self):
        """1012's destructive mid-stream `none`: it drops the receipt AND ends the loop with no
        re-review, in one step, from a status line that used to read like an instruction."""
        reviewed = self.commit("src.py", "the deliverable")
        repaired = self.commit("src.py", "the repair")
        unread = self.commit("src.py", "work nobody has read")
        state = _state(reviewed_commit=reviewed, base_commit=self.base)
        state, _ = self._assess(state, repaired)
        state["repair"]["receipts"] = [self.receipt("usability", reviewed, repaired, packet_digest=None)]
        with self.assertRaises(bc.CoordinatorError) as caught:
            self._assess(state, unread, judgment="none", lenses=())
        message = str(caught.exception)
        self.assertIn("would discard 1 recorded repair receipt", message)
        self.assertIn("--accept-receipt-loss", message)
        self.assertIn("`scoped`", message, "the refusal must name the honest alternative, not only the flag")

    def test_a_scoped_round_that_drops_a_receipt_warns_rather_than_refusing(self):
        """Calibration. A scoped round drops a receipt and then asks that lens to read the new range, so
        the evidence is replaced rather than lost — walling every ordinary second round behind a flag
        would make the flag a rubber stamp."""
        reviewed = self.commit("src.py", "the deliverable")
        repaired = self.commit("src.py", "the repair")
        unread = self.commit("src.py", "work nobody has read")
        state = _state(reviewed_commit=reviewed, base_commit=self.base)
        state, _ = self._assess(state, repaired)
        state["repair"]["receipts"] = [self.receipt("usability", reviewed, repaired, packet_digest=None)]
        state, err = self._assess(state, unread)
        self.assertIn("do not cover this new divergence and are being dropped", err)
        self.assertIn("1 authored commit(s) unread", err)


class _Store:
    """The snapshot interface `cmd_repair_assess` uses, over a plain dict — no schema, no lock. These
    tests are about commit ranges; the store discipline has its own suite."""

    def __init__(self, state):
        self.state = state

    def read(self):
        return json.loads(json.dumps(self.state))

    def mutate(self, change, from_revision=None):
        working = self.read()
        result = change(working)
        self.state = working if result is None else working
        self.state["revision"] = self.state.get("revision", 1) + 1


def _state(**delivery) -> dict:
    return {"revision": 1, "repair": None, "repair_rounds": [], "reconciles": [],
            "reviews": {"deliverable": {"packet_digest": None, "referent_digest": None,
                                        "required_lenses": [], "installed_lenses": [],
                                        "reviewer_contracts": [], "receipts": [],
                                        "reviewed_commit": None, "base_commit": None, **delivery}}}


class TheBatchForm(unittest.TestCase):
    """One file, both verbs, all or nothing (StarshipSuperjam/engine-template#1060)."""

    def _batch(self, findings, stage="deliverable", version="build-findings-batch.v1"):
        path = Path(tempfile.mkdtemp(prefix="findings-batch-"))
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        target = path / "batch.json"
        target.write_text(json.dumps({"schema_version": version, "stage": stage,
                                      "findings": findings}), encoding="utf-8")
        return str(target)

    def _finding(self, **over):
        return {"id": "A-1", "lens": "usability", "severity": "nit", "summary": "s",
                "disposition": "accepted-fixed", "rationale": "r", "blocks_this_pr": False, **over}

    def test_a_well_formed_batch_is_accepted_and_keeps_its_order(self):
        entries = bc._findings_batch(self._batch([self._finding(), self._finding(id="A-2")]),
                                     "deliverable")
        self.assertEqual([e["id"] for e in entries], ["A-1", "A-2"])

    def test_a_malformed_entry_records_nothing(self):
        """The whole point of the file form. A batch is validated entirely before anything is written, so
        a session cannot land in a half-applied state and have to work out which half."""
        bad = [self._finding(), self._finding(id="A-2", severity="catastrophic")]
        with self.assertRaises(bc.CoordinatorError) as caught:
            bc._findings_batch(self._batch(bad), "deliverable")
        self.assertIn("severity", str(caught.exception))

    def test_a_contradictory_disposition_is_refused_by_name_and_nothing_records(self):
        bad = [self._finding(), self._finding(id="A-2", disposition="accepted-fixed", blocks_this_pr=True)]
        with self.assertRaises(bc.CoordinatorError) as caught:
            bc._findings_batch(self._batch(bad), "deliverable")
        message = str(caught.exception)
        self.assertIn("A-2", message)
        self.assertIn("nothing was recorded", message)

    def test_a_batch_cut_for_another_stage_cannot_be_replayed_here(self):
        with self.assertRaises(bc.CoordinatorError) as caught:
            bc._findings_batch(self._batch([self._finding()], stage="repair"), "deliverable")
        self.assertIn("authored for the repair stage", str(caught.exception))

    def test_the_same_id_twice_is_refused(self):
        with self.assertRaises(bc.CoordinatorError) as caught:
            bc._findings_batch(self._batch([self._finding(), self._finding()]), "deliverable")
        self.assertIn("same id twice", str(caught.exception))

    def test_a_receipt_refuses_a_batch_carrying_another_lens_findings(self):
        batch = self._batch([self._finding(), self._finding(id="A-2", lens="spec-conformance")])
        with self.assertRaises(bc.CoordinatorError) as caught:
            bc._findings_batch(batch, "deliverable", lens="usability")
        self.assertIn("spec-conformance", str(caught.exception))

    def test_the_receipt_and_the_findings_cannot_disagree_because_they_are_one_file(self):
        """The failure this closes: repeated `--finding` values built from a shell array collapsed into a
        single string, so the receipt demanded one bogus id."""
        batch = self._batch([self._finding(), self._finding(id="A-2")])
        args = argparse.Namespace(stage="deliverable", lens="usability",
                                  findings_from_file=batch, finding=None)
        self.assertEqual(bc._receipt_finding_ids(args), ["A-1", "A-2"])

    def test_two_sources_for_one_list_is_refused_rather_than_merged(self):
        args = argparse.Namespace(stage="deliverable", lens="usability",
                                  findings_from_file=self._batch([self._finding()]), finding=["A-9"])
        with self.assertRaises(bc.CoordinatorError) as caught:
            bc._receipt_finding_ids(args)
        self.assertIn("not both", str(caught.exception))

    def test_an_unversioned_batch_is_refused_rather_than_guessed(self):
        with self.assertRaises(bc.CoordinatorError):
            bc._findings_batch(self._batch([self._finding()], version="build-findings-batch.v9"),
                               "deliverable")


class TheB1EffortShortfall(unittest.TestCase):
    """A sealed `thorough` whose panel actually ran at `medium`, and nothing said so
    (StarshipSuperjam/engine-template#1067)."""

    def test_a_shortfall_is_a_shortfall_and_an_unrecorded_effort_is_not(self):
        self.assertTrue(bc.effort_shortfall("medium", "high"))
        self.assertFalse(bc.effort_shortfall("high", "high"))
        self.assertFalse(bc.effort_shortfall("high", "medium"))
        self.assertFalse(bc.effort_shortfall(None, "high"),
                         "an unrecorded effort is an unknown, disclosed as such — never a fabricated number")
        self.assertFalse(bc.effort_shortfall("low", None))

    def test_the_b1_shape_is_disclosed_in_the_operator_s_own_words(self):
        state = {"approval": {"depth": "thorough"},
                 "reviews": {"deliverable": {"receipts": [
                     {"lens": "usability", "delivered_effort": "medium"},
                     {"lens": "spec-conformance", "delivered_effort": "high"}]}}}
        lines = bc._effort_shortfall_lines(state)
        self.assertEqual(len(lines), 1)
        self.assertIn("usability ran at medium", lines[0])
        self.assertIn("self-reported", lines[0],
                      "nothing here can verify the effort, and the disclosure must not pretend otherwise")

    def test_a_receipt_that_recorded_no_effort_is_named_as_unverified_not_as_met(self):
        state = {"approval": {"depth": "thorough"},
                 "reviews": {"deliverable": {"receipts": [{"lens": "usability"}]}}}
        lines = bc._effort_shortfall_lines(state)
        self.assertEqual(len(lines), 1)
        self.assertIn("unverified", lines[0])
        self.assertIn("usability", lines[0])

    def test_a_depth_that_promises_no_effort_claims_nothing(self):
        state = {"approval": {"depth": "quick"},
                 "reviews": {"deliverable": {"receipts": [{"lens": "usability"}]}}}
        self.assertEqual(bc._effort_shortfall_lines(state), [])

    def test_a_repair_panel_s_accepted_gap_is_disclosed_too(self):
        """The disclosure read `reviews.deliverable` alone, so a REPAIR round spawned under an accepted
        shortfall published nothing at all — the accepted gap and the session it was accepted against
        both lived on the repair stage."""
        state = {"approval": {"depth": "thorough"},
                 "reviews": {"deliverable": {"session_effort": "high", "receipts": []}},
                 "repair": {"session_effort": "medium", "effort_shortfall_accepted": True,
                            "receipts": []}}
        lines = bc._effort_shortfall_lines(state)
        self.assertEqual(len(lines), 1)
        self.assertIn("the repair panel", lines[0])
        self.assertIn("medium", lines[0])

    def test_the_spawning_session_is_read_off_the_receipt_not_inferred_from_a_list(self):
        """Inferring it from the repair stage's receipt list read correctly but could go stale: a repair
        receipt dropped there while its spliced copy survived in the deliverable stage produced a FALSE
        'reviewed above its session' line. The stamp is what the disclosure trusts; the list is only the
        fallback for receipts written before the field existed."""
        stale = {"lens": "security-governance", "delivered_effort": "high",
                 "spawn_session_effort": "high"}
        state = {"approval": {"depth": "thorough"},
                 "reviews": {"deliverable": {"session_effort": "medium", "receipts": [stale]}},
                 "repair": {"session_effort": "medium", "receipts": []}}
        self.assertEqual([line for line in bc._effort_shortfall_lines(state)
                          if "ABOVE the session" in line], [],
                         "a receipt that names its own session must not be measured against another's")

    def test_a_spliced_repair_receipt_answers_to_the_session_that_spawned_it(self):
        """A repair receipt is spliced into the deliverable stage, so comparing it against the
        DELIVERABLE session's effort measured it against a number it never ran under. The repair stage
        keeps its own copy of the receipt, and that is what makes the attribution readable."""
        receipt = {"lens": "security-governance", "delivered_effort": "high"}
        state = {"approval": {"depth": "thorough"},
                 "reviews": {"deliverable": {"session_effort": "high", "receipts": [receipt]}},
                 "repair": {"session_effort": "medium", "receipts": [receipt]}}
        lines = bc._effort_shortfall_lines(state)
        overclaim = [line for line in lines if "ABOVE the session" in line]
        self.assertEqual(len(overclaim), 1, lines)
        self.assertIn("security-governance (high)", overclaim[0])
        self.assertIn("a session reporting medium", overclaim[0])


class ThePlanPanelSEffortReachesTheMergeSurface(unittest.TestCase):
    """The plan side's own `--accept-effort-shortfall` promised, in its refusal text, that the gap it
    accepts is published in the pull request. Nothing read it: `delivered_efforts` and
    `effort_shortfall_accepted` went onto the plan record and the only reader in the tree was the seal's
    completeness check, which asserts the map is filled in and never that the level was met."""

    def _with_record(self, record, problem=None):
        original = bc._sealed_plan_record
        bc._sealed_plan_record = lambda state: (record, problem)
        self.addCleanup(lambda: setattr(bc, "_sealed_plan_record", original))

    def test_a_plan_panel_under_its_approved_depth_is_named(self):
        self._with_record({"approval": {"depth": "thorough"},
                           "plan_review": {"delivered_efforts": {"architecture": "medium",
                                                                 "feasibility": "high"},
                                           "effort_shortfall_accepted": True}})
        lines = bc._plan_effort_lines({})
        self.assertEqual(len(lines), 1)
        self.assertIn("PLAN panel", lines[0])
        self.assertIn("architecture (medium)", lines[0])
        self.assertNotIn("feasibility", lines[0])
        self.assertIn("self-reported", lines[0])

    def test_a_gap_nobody_acknowledged_says_so_rather_than_implying_consent(self):
        """`review amend` could pass the accept flag to the refusal without writing it down, which left a
        gap on the record with nothing saying anyone accepted it. That state is described, not smoothed."""
        self._with_record({"approval": {"depth": "thorough"},
                           "plan_review": {"delivered_efforts": {"architecture": "low"}}})
        self.assertIn("NO acknowledgement", bc._plan_effort_lines({})[0])

    def test_a_plan_panel_that_met_its_depth_claims_nothing(self):
        self._with_record({"approval": {"depth": "thorough"},
                           "plan_review": {"delivered_efforts": {"architecture": "high"}}})
        self.assertEqual(bc._plan_effort_lines({}), [])

    def test_a_panel_that_never_said_what_it_ran_at_is_named_as_unverified(self):
        """The seal deliberately permits a review with no effort map — its own comment says the Build's
        pull-request body "carries that honestly". It did not: an absent map returned nothing, so the
        body claimed the approved depth with no qualification for a panel that never stated anything.
        Every plan sealed before this field existed is in exactly that state."""
        self._with_record({"approval": {"depth": "thorough"},
                           "plan_review": {"lenses": ["architecture", "feasibility"]}})
        lines = bc._plan_effort_lines({})
        self.assertEqual(len(lines), 1)
        self.assertIn("unverified", lines[0])
        self.assertIn("architecture", lines[0])
        self.assertIn("feasibility", lines[0])

    def test_a_library_that_cannot_be_read_says_so_rather_than_publishing_nothing(self):
        """A hard disclosure hung off a read that swallowed every exception: an unreadable library made
        the accepted gap vanish, and `_plan_review_clause` then asserted no review had run at all — a
        false statement in the flattering direction."""
        self._with_record(None, problem="permission denied opening /private/plans/somebody-elses-plan")
        lines = bc._plan_effort_lines({})
        self.assertEqual(len(lines), 1)
        self.assertIn("could not be read", lines[0])
        self.assertNotIn("somebody-elses-plan", lines[0],
                         "the failure is named on the merge surface, never quoted — plans are private")
        clause = bc._plan_review_clause({"plan": {"plan_id": "pln_x"}, "approval": {"depth": "thorough"}})
        self.assertIn("could NOT be established", clause)
        self.assertNotIn("No cold plan review is recorded", clause)

    def test_it_rides_the_same_list_the_build_side_uses(self):
        self._with_record({"approval": {"depth": "thorough"},
                           "plan_review": {"delivered_efforts": {"architecture": "medium"},
                                           "effort_shortfall_accepted": True}})
        state = {"approval": {"depth": "thorough"},
                 "reviews": {"deliverable": {"session_effort": "high", "receipts": []}}}
        self.assertTrue(any("PLAN panel" in line for line in bc._effort_shortfall_lines(state)))


class AnAddedWorkflowDisclosureNeverFailsQuietly(unittest.TestCase):
    """This function is the only review an added workflow's triggers and token get. A git failure and
    "this change adds no workflows" produced the same empty string, at the one surface where the
    difference is the whole point."""

    def test_an_unresolvable_base_says_so_instead_of_claiming_nothing_was_added(self):
        text = bc._added_workflow_disclosure("no-such-ref-0000000000000000000000000000000000000000")
        self.assertIn("could not be determined", text)
        self.assertIn(".github/workflows/", text)


class TheBindingsStopAssertingWhatTheClaudeArmCannotDo(unittest.TestCase):
    """The three per-lens overrides took the operator's branch: effort assertions removed, model pins
    kept, the schema adjusted to allow model-only entries."""

    def setUp(self):
        import agent_bindings
        self.agent_bindings = agent_bindings
        self.root = str(Path(__file__).resolve().parents[2])
        self.bindings = agent_bindings.load_bindings(self.root)

    def test_no_reviewer_override_asserts_an_effort_any_more(self):
        for name, override in (self.bindings.get("overrides") or {}).items():
            self.assertNotIn("effort", override,
                             f"{name} pins an effort, but a reviewer persona carries no effort "
                             "frontmatter for it to ride on")

    def test_the_overrides_still_pin_their_models(self):
        overrides = self.bindings.get("overrides") or {}
        self.assertTrue(overrides, "the override table is the point; an empty one proves nothing")
        for name, override in overrides.items():
            self.assertIn("model", override, name)

    def test_a_model_only_override_keeps_the_tier_effort_rather_than_un_pinning_it(self):
        """`None` means 'deliberately un-pinned' to the stamper. Inferring that from a silent field would
        un-pin a worker whose author only meant to retune its model."""
        bindings = {"tiers": {"judgment": {"model": "opus", "effort": "high"}},
                    "overrides": {"x": {"model": "sonnet"}}}
        self.assertEqual(self.agent_bindings.resolve("x", "judgment", bindings),
                         {"model": "sonnet", "effort": "high"})

    def test_an_override_that_does_pin_an_effort_still_wins(self):
        bindings = {"tiers": {"judgment": {"model": "opus", "effort": "high"}},
                    "overrides": {"x": {"model": "sonnet", "effort": "low"}}}
        self.assertEqual(self.agent_bindings.resolve("x", "judgment", bindings),
                         {"model": "sonnet", "effort": "low"})

    def test_the_shipped_bindings_are_in_sync_with_the_installed_personas(self):
        self.assertEqual(self.agent_bindings.check(self.root), [])


class ThreeCodeExecutionBehaviours(unittest.TestCase):
    """B2's carried finding CO-1: a receipt rounded three real reviewer behaviours to two words."""

    def test_the_state_schema_admits_the_third_value(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "schemas"
                             / "build-state.v2.json").read_text())
        self.assertEqual(set(schema["$defs"]["review_receipt"]["properties"]["code_execution"]["enum"]),
                         {"none", "discarded-copy", "in-place"})

    def test_the_recording_verb_offers_all_three_and_no_more(self):
        """The enum, the CLI choices and the disclosure must name the same three behaviours; a value the
        schema accepts but the flag cannot express is a behaviour no reviewer can ever record."""
        schema = json.loads((Path(__file__).resolve().parents[1] / "schemas"
                             / "build-state.v2.json").read_text())
        self.assertEqual(set(bc.CODE_EXECUTION_KINDS),
                         set(schema["$defs"]["review_receipt"]["properties"]["code_execution"]["enum"]))
        source = Path(bc.__file__).read_text(encoding="utf-8")
        self.assertIn('choices=["none", "discarded-copy", "in-place"]', source)

    def test_running_code_in_place_is_not_described_as_a_throwaway_copy(self):
        """The claim that changed. 'In a throwaway copy — it never touched your project' is a materially
        different statement from 'directly in this checkout', and one of them was being published for
        both."""
        in_place = bc.code_execution_disclosure({"in-place"})
        self.assertIn("directly in this checkout", in_place)
        self.assertNotIn("never touched your project", in_place,
                         "the reassurance belongs to the throwaway-copy case and is false here")
        in_copy = bc.code_execution_disclosure({"discarded-copy"})
        self.assertIn("never touched your project", in_copy)
        self.assertNotIn("directly in this checkout", in_copy)
        self.assertIn("no reviewer executed", bc.code_execution_disclosure({"none"}))

    def test_a_mixed_panel_says_both_rather_than_picking_one(self):
        line = bc.code_execution_disclosure({"in-place", "discarded-copy"})
        self.assertIn("throwaway copy", line)
        self.assertIn("directly in this checkout", line)


class Issue1012BookkeepingTraps(unittest.TestCase):
    """The four traps that lost long sessions in the coordinator's own ceremony. Each test names the
    incident shape rather than the predicate."""

    def test_a_fixed_blocker_clears_the_gate_without_being_recorded_a_second_time(self):
        """Trap 2. The submit gate keyed on the blocking FLAG, so a fixed blocker had to be re-recorded
        `--does-not-block-this-pr` with an operator summary; `accepted-fixed` alone cleared nothing."""
        fixed = {"disposition": "accepted-fixed", "blocks_this_pr": False, "severity": "blocking"}
        self.assertFalse(review.blocks_submission(fixed))
        tracked = {"disposition": "accepted-tracked", "blocks_this_pr": False, "severity": "serious"}
        self.assertFalse(review.blocks_submission(tracked))

    def test_but_an_escalated_finding_blocks_whatever_the_flag_says(self):
        """The gate got quieter, not weaker. `escalated` is the one disposition meaning 'the operator
        decides', so it holds the pull request regardless."""
        self.assertTrue(review.blocks_submission(
            {"disposition": "escalated", "blocks_this_pr": False, "severity": "nit"}))

    def test_a_partially_accepted_finding_still_answers_to_its_flag(self):
        """It says part of the finding stands, so the flag is the only thing that can say whether that
        part blocks — reading the disposition alone would settle something nobody settled."""
        self.assertTrue(review.blocks_submission(
            {"disposition": "partially-accepted", "blocks_this_pr": True, "severity": "blocking"}))
        self.assertFalse(review.blocks_submission(
            {"disposition": "partially-accepted", "blocks_this_pr": False, "severity": "blocking"}))

    def test_the_quieter_gate_does_not_quieten_the_merge_surface(self):
        """The safety property. A blocking-severity finding that stops blocking still publishes its
        disagreement line, whether the flag was flipped or the disposition settled it."""
        state = {"findings": [
            {"id": "A-1", "severity": "blocking", "disposition": "accepted-tracked",
             "blocks_this_pr": False, "operator_summary": "Tracked as its own issue."}]}
        lines = review.required_disagreement_lines(state)
        self.assertEqual(len(lines), 1)
        self.assertIn("A-1", lines[0])
        self.assertIn("Tracked as its own issue.", lines[0])

    def test_a_contradictory_pair_is_refused_where_the_session_still_knows_which_half_is_wrong(self):
        self.assertIsNotNone(review.disposition_conflict("accepted-fixed", True))
        self.assertIsNotNone(review.disposition_conflict("escalated", False))
        self.assertIsNone(review.disposition_conflict("partially-accepted", True))
        self.assertIsNone(review.disposition_conflict("accepted-fixed", False))

    def test_a_finding_is_keyed_to_the_receipt_that_demanded_it_not_to_the_live_packet(self):
        """Trap 3, the one with no way out. `missing_findings` matches a finding against the key of the
        receipt that asked for it. When the stage's reviewed commit advanced between the receipt and the
        disposition, reading the live packet first stamped the finding with a NEWER commit than its own
        receipt — so the demand could never be satisfied, and no amount of re-recording the FINDING
        fixed it. The real remedy was to re-record the receipt and its siblings together, which nothing
        ever said."""
        receipt = {"lens": "usability", "packet_digest": "sha256:" + "1" * 64,
                   "lens_packet_digest": "sha256:" + "2" * 64, "commit": "a" * 40,
                   "finding_ids": ["A-1"], "code_execution": "none"}
        state = {"reviews": {"deliverable": {"packet_digest": "sha256:" + "9" * 64,
                                             "receipts": [receipt]}},
                 "repair": None,
                 "findings": [{"id": "A-1", "stage": "repair", "lens": "usability",
                               "packet_digest": receipt["packet_digest"],
                               "lens_packet_digest": receipt["lens_packet_digest"],
                               "commit": receipt["commit"], "severity": "nit", "summary": "s",
                               "disposition": "accepted-fixed", "rationale": "r",
                               "blocks_this_pr": False}]}
        self.assertEqual(review.missing_findings(state), [],
                         "a finding recorded against its own receipt's key satisfies that receipt")
        drifted = json.loads(json.dumps(state))
        drifted["findings"][0]["commit"] = "b" * 40      # what keying on the live packet produced
        self.assertEqual(review.missing_findings(drifted), ["A-1"],
                         "and keying it to the advanced commit is exactly the unsatisfiable demand")

    def test_the_status_buckets_say_which_are_gates_and_which_are_judgments(self):
        """Trap 4's sibling. A session reading `status` while stuck could not tell a hard gate fact from
        a prompt for its own judgment, and read 'choose none, scoped or full' as a step to take — which
        is how a destructive `--judgment none` got run mid-stream."""
        source = Path(bc.__file__).read_text(encoding="utf-8")
        self.assertIn("the gate refuses to submit until each of these exists", source)
        self.assertIn("the coordinator reports these, it does not make them", source)
        self.assertIn("no action is demanded here", source)

    def test_the_reviewed_to_final_line_says_none_is_a_judgment_and_what_it_costs(self):
        source = Path(bc.__file__).read_text(encoding="utf-8")
        self.assertIn("`none` is a real judgment, not a skip", source)
        self.assertIn("clears the repair packet", source)


class TheV1SunsetDemo(unittest.TestCase):
    """The v1-sunset reproducer, run end to end — and kept alive for the census reference-closure."""

    def test_the_v1_sunset_demo_passes(self):
        import quiet_call
        import demo_v1_plan_sunset_refused as demo
        self.assertEqual(quiet_call.run(demo.main), 0)


if __name__ == "__main__":
    unittest.main()
