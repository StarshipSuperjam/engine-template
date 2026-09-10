#!/usr/bin/env python3
"""The review economy: receipts bound to what a lens read, and reviews spent only on unread work.

Every class here replays a real incident rather than exercising a predicate. The operator's rule out of
the build that produced most of them: lenses run to do work; they are not ceremony.

  * `ThePr1063Replay` — the build where three separate mechanics combined to demand cold reviews that
    would have found nothing (StarshipSuperjam/engine-template#1065). Driven against a REAL throwaway
    git repository, because the whole change is commit-range arithmetic and a fake SHA proves none of it.
  * `TheBatchForm` — the collapsed shell array that made a receipt demand a bogus id
    (StarshipSuperjam/engine-template#1060).
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
import shlex
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
                "finding_ids": [], "code_execution": "none",
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


class TheCleanTargetMergeProof(_RealRepo):
    """A target catch-up is exempt only when the recorded merge tree is reproducible."""

    def setUp(self):
        from test_build_coordinator import ScrubbedGitRepo
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = ScrubbedGitRepo(Path(temporary.name) / "repo")
        self.repo, self.env = Path(fixture.path), fixture.env
        self.base = self.commit("seed.txt", "seed")

    def target(self, tip):
        return {"target_repository": "owner/repo", "target_ref": "main", "target_tip": tip}

    def clean_merge(self, *, before=False, after=False, edited=False):
        self.git("checkout", "-q", "-b", "build")
        reviewed = self.commit("src.py", "reviewed")
        repaired = self.commit("src.py", "repaired")
        local_before = self.commit("before.py", "unread before") if before else None
        self.git("checkout", "-q", "main")
        target = self.commit("upstream.py", "target work")
        self.git("checkout", "-q", "build")
        if edited:
            self.git("merge", "--no-ff", "--no-commit", "main")
            merge = self.commit("hidden.py", "edit hidden in merge")
        else:
            self.git("merge", "--no-ff", "--no-edit", "main")
            merge = self.git("rev-parse", "HEAD")
        local_after = self.commit("after.py", "unread after") if after else None
        return reviewed, repaired, target, merge, local_before, local_after

    def test_clean_target_merge_preserves_receipt_bytes_and_requires_a_proof(self):
        reviewed, repaired, target, merge, _, _ = self.clean_merge()
        receipt = self.receipt("usability", reviewed, repaired)
        original = json.dumps(receipt, sort_keys=True)
        proof = ranges.prove_base_advance(self.repo, merge, self.target(target))
        self.assertIsNotNone(proof)
        self.assertEqual(proof["first_parent"], repaired)
        self.assertEqual(proof["merge_tree"], self.git("rev-parse", merge + "^{tree}"))
        self.assertFalse(ranges.receipt_covers(self.repo, receipt, repaired, merge))
        self.assertTrue(ranges.receipt_covers(self.repo, receipt, repaired, merge, [proof]))
        self.assertEqual(ranges.authored_between(self.repo, repaired, merge, [proof]), [])
        self.assertEqual(ranges.unread_authored(self.repo, receipt["reviewed_range"], repaired, merge, [proof]), [])
        self.assertIn("already read every authored commit", ranges.coverage_report(self.repo, receipt, repaired, merge, [proof]))
        self.assertEqual(json.dumps(receipt, sort_keys=True), original)

    def test_local_authored_commits_before_and_after_merge_still_need_a_read(self):
        reviewed, repaired, target, merge, before, after = self.clean_merge(before=True, after=True)
        proof = ranges.prove_base_advance(self.repo, merge, self.target(target))
        self.assertIsNotNone(proof)
        receipt = self.receipt("usability", reviewed, repaired)
        self.assertEqual(set(ranges.authored_between(self.repo, repaired, after, [proof])), {before, after})
        self.assertFalse(ranges.receipt_covers(self.repo, receipt, repaired, after, [proof]))
        report = ranges.coverage_report(self.repo, receipt, repaired, after, [proof])
        self.assertIn(before[:12], report)
        self.assertIn(after[:12], report)

    def test_edited_merge_tree_is_not_an_automatic_target_merge(self):
        reviewed, repaired, target, merge, _, _ = self.clean_merge(edited=True)
        self.assertIsNone(ranges.prove_base_advance(self.repo, merge, self.target(target)))
        self.assertFalse(ranges.receipt_covers(self.repo, self.receipt("usability", reviewed, repaired), repaired, merge))

    def test_conflict_resolution_is_never_granted_clean_merge_coverage(self):
        self.git("checkout", "-q", "-b", "build")
        build = self.commit("seed.txt", "our resolution input")
        self.git("checkout", "-q", "main")
        target = self.commit("seed.txt", "their resolution input")
        self.git("checkout", "-q", "build")
        result = subprocess.run(["git", "-C", str(self.repo), "merge", "--no-ff", "main"],
                                env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CONFLICT", result.stdout)
        merge = self.commit("seed.txt", "manually chosen resolution")
        self.assertIsNone(ranges.prove_base_advance(self.repo, merge, self.target(target)))
        self.assertTrue(ranges.authored_between(self.repo, build, merge))

    def _install_untracked_merge_driver(self, pattern, resolution):
        """A harmless reproducer: if executed, it writes only its result and a private sentinel."""
        marker = self.repo.parent / "custom-driver-ran"
        script = self.repo.parent / "custom-driver.sh"
        script.write_text("#!/bin/sh\nprintf %s " + shlex.quote(resolution) + " > \"$1\"\n"
                          "printf ran > " + shlex.quote(str(marker)) + "\n")
        self.git("config", "merge.injected.driver", "sh " + shlex.quote(str(script)) + " %A")
        (self.repo / ".git" / "info" / "attributes").write_text(pattern + " merge=injected\n")
        global_config = self.repo.parent / "untrusted-global.gitconfig"
        global_config.write_text("[merge \"injected\"]\n\tdriver = sh " + str(script) + " %A\n")
        return marker, global_config

    def test_conflict_cannot_gain_clean_proof_from_untracked_attributes_and_driver(self):
        """SG-1: local attributes once laundered a manual resolution into automatic coverage."""
        self.git("checkout", "-q", "-b", "build")
        first_parent = self.commit("seed.txt", "ours")
        self.git("checkout", "-q", "main")
        target = self.commit("seed.txt", "theirs")
        self.git("checkout", "-q", "build")
        conflict = subprocess.run(["git", "-C", str(self.repo), "merge", "--no-ff", "main"],
                                  env=self.env, capture_output=True, text=True)
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("CONFLICT", conflict.stdout)
        merged = self.commit("seed.txt", "manually chosen resolution")
        marker, config = self._install_untracked_merge_driver("seed.txt", "manually chosen resolution\n")
        # Establish that mutable Git inputs really reproduce the maliciously claimed automatic tree.
        poisoned_tree = self.git("merge-tree", "--write-tree", first_parent, target).splitlines()[0]
        self.assertEqual(poisoned_tree, self.git("rev-parse", merged + "^{tree}"))
        self.assertTrue(marker.exists())
        marker.unlink()
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_SYSTEM": str(config),
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "merge.default", "GIT_CONFIG_VALUE_0": "injected"}):
            self.assertIsNone(ranges.prove_base_advance(self.repo, merged, self.target(target)))
        self.assertFalse(marker.exists(), "proof verification must never execute a locally selected merge driver")
        self.assertEqual((self.repo / ".git" / "info" / "attributes").read_text(), "seed.txt merge=injected\n")

    def test_clean_merge_proof_ignores_mutable_attributes_and_environment_without_driver_execution(self):
        self.commit("seed.txt", "one\ntwo\nthree\nfour\nfive\nsix")
        self.git("checkout", "-q", "-b", "build")
        self.commit("seed.txt", "ONE\ntwo\nthree\nfour\nfive\nsix")
        self.git("checkout", "-q", "main")
        target = self.commit("seed.txt", "one\ntwo\nthree\nfour\nfive\nSIX")
        self.git("checkout", "-q", "build")
        self.git("merge", "--no-ff", "--no-edit", "main")
        merged = self.git("rev-parse", "HEAD")
        original_proof = ranges.prove_base_advance(self.repo, merged, self.target(target))
        self.assertIsNotNone(original_proof)
        marker, config = self._install_untracked_merge_driver("seed.txt", "untrusted replacement\n")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_SYSTEM": str(config),
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "merge.default", "GIT_CONFIG_VALUE_0": "injected"}):
            self.assertEqual(ranges.prove_base_advance(self.repo, merged, self.target(target)), original_proof)
        self.assertFalse(marker.exists())

    def test_replacement_refs_cannot_change_a_real_merge_proof(self):
        _, repaired, target, merged, _, _ = self.clean_merge()
        original = ranges.prove_base_advance(self.repo, merged, self.target(target))
        self.assertIsNotNone(original)
        self.git("replace", merged, repaired)
        self.assertEqual(ranges.prove_base_advance(self.repo, merged, self.target(target)), original)
        self.assertEqual(self.git("for-each-ref", "--format=%(objectname)", "refs/replace/"), repaired)

    def test_unrelated_target_missing_object_and_tampered_proof_do_not_cover(self):
        reviewed, repaired, target, merge, _, _ = self.clean_merge()
        self.assertIsNone(ranges.prove_base_advance(self.repo, merge, self.target(repaired)))
        self.assertIsNone(ranges.prove_base_advance(self.repo, "f" * 40, self.target(target)))
        proof = ranges.prove_base_advance(self.repo, merge, self.target(target))
        receipt = self.receipt("usability", reviewed, repaired)
        for field in ("merge_tree", "first_parent", "target_tip", "merge_commit"):
            with self.subTest(field=field):
                altered = dict(proof, **{field: "f" * 40})
                self.assertFalse(ranges.receipt_covers(self.repo, receipt, repaired, merge, [altered]))

    def test_octopus_merge_cannot_supply_a_two_parent_target_proof(self):
        self.git("checkout", "-q", "-b", "build")
        self.commit("build.py", "build")
        self.git("checkout", "-q", "-b", "side", self.base)
        self.commit("side.py", "side")
        self.git("checkout", "-q", "main")
        target = self.commit("upstream.py", "target")
        self.git("checkout", "-q", "build")
        self.git("merge", "--no-ff", "--no-edit", "main", "side")
        merge = self.git("rev-parse", "HEAD")
        self.assertEqual(len(self.git("rev-list", "--parents", "-n", "1", merge).split()), 4)
        self.assertIsNone(ranges.prove_base_advance(self.repo, merge, self.target(target)))

    def test_valid_proof_on_an_unrelated_branch_cannot_cover_this_tip(self):
        reviewed, repaired, target, merge, _, _ = self.clean_merge()
        proof = ranges.prove_base_advance(self.repo, merge, self.target(target))
        self.git("checkout", "-q", "-b", "other-build", repaired)
        unread = self.commit("other.py", "unread")
        self.assertFalse(ranges.receipt_covers(self.repo, self.receipt("usability", reviewed, repaired),
                                               repaired, unread, [proof]))


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

    def test_a_generated_commit_cannot_absorb_a_fan_out_that_never_came_back(self):
        """StarshipSuperjam/engine-template#1065, on the path the spend-counting rewrite reopened.

        A round whose panel was dispatched and never returned is PAID FOR. StarshipSuperjam/engine-template#1071 recorded that
        on the
        ledger entry so no later assess could absorb it; the spend-counting rewrite dropped that flag and
        read the fact live off the open repair record instead -- but keyed the live read on the commit
        pair, which is exactly the identity keying
        StarshipSuperjam/engine-template#1065 named as the defect. One `sync-artifacts` commit
        then moved the head, `_same_episode` read the generated commit as `nothing authored landed`, and
        the abandoned round was re-pointed instead of counted. Repeated, the ledger never grew: ten
        dispatched panels, one entry, and BOTH bounds silent -- the counted budget refunded every time and
        the absolute ceiling never reached, because the ceiling counts entries."""
        reviewed = self.commit("src.py", "the deliverable")
        repaired = self.commit("src.py", "the repair")
        state = _state(reviewed_commit=reviewed, base_commit=self.base)
        state, _ = self._assess(state, repaired)
        self.assertEqual(len(state["repair_rounds"]), 1)
        # The panel was cut and the lenses never returned: no receipt, packet still open.
        state["repair"]["packet_digest"] = "sha256:" + "5" * 64
        generated = self.commit(DERIVED_PATH, "regenerated")
        state, err = self._assess(state, generated)
        self.assertEqual(len(state["repair_rounds"]), 2,
                         "a dispatched-and-abandoned round is paid for; a generated commit must not "
                         "absorb it and refund its slot")
        self.assertNotIn("re-points the repair round already recorded", err)

    def test_abandoned_panels_reach_the_counted_budget_they_are_supposed_to_reach(self):
        """The consequence stated as the obligation states it. The headline promises no class of round
        repeats unbounded while the operator is away; that is only true if every dispatched panel leaves
        an entry behind."""
        reviewed = self.commit("src.py", "the deliverable")
        state = _state(reviewed_commit=reviewed, base_commit=self.base)
        head = self.commit("src.py", "the repair")
        for attempt in range(3):
            state, _ = self._assess(state, head, lenses=("usability", "technical-integrity"))
            state["repair"]["packet_digest"] = "sha256:" + "5" * 64   # dispatched, never returned
            head = self.commit(DERIVED_PATH, f"regenerated {attempt}")
        # Three panels, three entries -- each abandoned, each still on the ledger. The whole budget.
        self.assertEqual(len(state["repair_rounds"]), 3)
        with self.assertRaises(bc.CoordinatorError) as caught:
            self._assess(state, head, lenses=("usability", "technical-integrity"))
        self.assertIn("counted budget", str(caught.exception))

    def test_abandoned_cheap_checks_reach_the_absolute_ceiling(self):
        """The other half of the same promise, and the half only the CEILING can keep. A single-lens
        check spends no counted budget, so if an abandoned one could be absorbed nothing would ever stop
        it -- the ceiling is the only bound in play, and it counts ledger entries."""
        reviewed = self.commit("src.py", "the deliverable")
        state = _state(reviewed_commit=reviewed, base_commit=self.base)
        head = self.commit("src.py", "the repair")
        for attempt in range(6):
            state, _ = self._assess(state, head, lenses=("usability",))
            state["repair"]["packet_digest"] = "sha256:" + "5" * 64   # dispatched, never returned
            head = self.commit(DERIVED_PATH, f"regenerated {attempt}")
        self.assertEqual(len(state["repair_rounds"]), 6)
        self.assertEqual([r["counted"] for r in state["repair_rounds"]], [False] * 6,
                         "a one-lens check never spends the counted budget")
        with self.assertRaises(bc.CoordinatorError) as caught:
            self._assess(state, head, lenses=("usability",))
        self.assertIn("absolute ceiling", str(caught.exception))

    def test_re_judging_a_completed_panel_does_not_refund_it(self):
        """A replacement re-judges a round; it never un-spends one. The entry used to take the fresh
        answer, so a completed two-lens panel re-assessed as a single-lens check came back UNCOUNTED --
        the ledger forgot a panel it had paid for, and every later round read the short number."""
        reviewed = self.commit("src.py", "the deliverable")
        repaired = self.commit("src.py", "the repair")
        state = _state(reviewed_commit=reviewed, base_commit=self.base)
        state, _ = self._assess(state, repaired, lenses=("usability", "technical-integrity"))
        self.assertEqual([r["counted"] for r in state["repair_rounds"]], [True])
        # The panel came back, then a generated commit moved the head: the same episode, re-pointed.
        state["repair"]["receipts"] = [self.receipt("usability", reviewed, repaired, packet_digest=None)]
        state["repair"]["packet_digest"] = None
        generated = self.commit(DERIVED_PATH, "regenerated")
        state, _ = self._assess(state, generated, lenses=("usability",))
        self.assertEqual(len(state["repair_rounds"]), 1)
        self.assertEqual([r["counted"] for r in state["repair_rounds"]], [True],
                         "the panel was dispatched and paid for; re-judging it cheap cannot refund it")

    def test_a_judgment_abandoned_before_the_packet_was_cut_is_not_charged_as_a_panel(self):
        """The other edge of counted-stickiness. Keeping a round counted must key on evidence the lenses
        WENT OUT, not on the roster the entry recorded: judge two lenses, think better of it before
        cutting the packet, re-judge with one, and nothing was ever dispatched. Charging it anyway made
        the merge surface contradict itself -- counted among the rounds that "dispatched a review panel"
        while its own line read "one cold check"."""
        reviewed = self.commit("src.py", "the deliverable")
        repaired = self.commit("src.py", "the repair")
        state = _state(reviewed_commit=reviewed, base_commit=self.base)
        state, _ = self._assess(state, repaired, lenses=("usability", "technical-integrity"))
        self.assertEqual([r["counted"] for r in state["repair_rounds"]], [True])
        self.assertIsNone(state["repair"]["packet_digest"], "nothing was dispatched")
        state, _ = self._assess(state, repaired, lenses=("usability",))
        self.assertEqual(len(state["repair_rounds"]), 1)
        self.assertEqual([r["counted"] for r in state["repair_rounds"]], [False],
                         "no packet was cut and no receipt came back, so no panel was ever paid for")

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

    def test_an_override_pins_an_effort_only_where_the_persona_has_one_to_ride_on(self):
        """The PROPERTY, not the count. This asserted that no override anywhere pins an effort, which was
        true of the three reviewers and became false the moment a worker persona with real effort
        frontmatter joined the table — a merge turned a correct rule into a wrong one. What actually
        matters is whether the persona the override names carries an effort for the pin to ride on: a
        reviewer does not (its depth arrives through the spawning session), a mechanical worker does."""
        for name, override in (self.bindings.get("overrides") or {}).items():
            persona = Path(self.root) / ".claude" / "agents" / f"{name}.md"
            self.assertTrue(persona.is_file(), f"{name} is overridden but has no persona file")
            frontmatter = persona.read_text(encoding="utf-8").split("---")[1]
            stamped = any(line.strip().startswith("effort:") for line in frontmatter.splitlines())
            if "effort" in override:
                self.assertTrue(stamped,
                                f"{name} pins an effort, but its persona carries no effort frontmatter "
                                "for that pin to ride on — the bindings would be asserting something "
                                "this runtime cannot deliver")

    def test_the_reviewer_personas_still_carry_no_effort_of_their_own(self):
        """The other half, kept explicit: the reason the reviewer overrides dropped their effort pins."""
        reviewers = [name for name in (self.bindings.get("overrides") or {}) if "-qa-review-" in name]
        self.assertTrue(reviewers, "the reviewer overrides are the subject; an empty set proves nothing")
        for name in reviewers:
            frontmatter = (Path(self.root) / ".claude" / "agents" / f"{name}.md").read_text(
                encoding="utf-8").split("---")[1]
            self.assertFalse(any(line.strip().startswith("effort:") for line in frontmatter.splitlines()),
                             f"{name} now stamps an effort; the override table's reasoning needs revisiting")

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
