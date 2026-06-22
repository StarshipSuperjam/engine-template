"""Unit tests for erasure_proposer.py — the Layer-2 erasure EMITTER (slice 4e PR iii).

The emitter selects an already-logically-retired note that has EARNED erasure, writes a content-free proposal at the
observer's fixed path, and AUTO-OPENS a single-purpose `engine-erasure` pull request. These tests pin the load-bearing
behavior with the GitHub network + the PR-opener stubbed (no live GitHub, no real git): the deterministic probe selects
the old hidden duplicate and skips the fresh / recalled / completed ones; the cost leaks NONE of the note's content
(text, session id, or tags — D-007); the written proposal is EXACTLY what the real observer reads back (the
emitter<->observer round-trip); auto-open de-duplicates against an existing PR or marker and DECLINES on host doubt; and
the real opener is never reached in the suite (every test injects). Throwaway ENGINE_MEMORY_DIR cabinet throughout.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import compact, consolidate, erasure_observer as obs  # noqa: E402
from memory import erasure_proposer as emit, forget, ledger, records  # noqa: E402

_DAY = 86400


def _contents(target: str) -> dict:
    """A base64 contents response naming `target` (the GitHub contents API shape the observer reads)."""
    raw = json.dumps({"target": target, "cost": "a paraphrase"}).encode("utf-8")
    return {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get(ledger.ENV_DIR)
        os.environ[ledger.ENV_DIR] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(ledger.ENV_DIR, None)
        else:
            os.environ[ledger.ENV_DIR] = self._prev
        self._tmp.cleanup()

    def _retired(self, text, *, role="lesson", age_days=60, batch="b", session="S", tags=None):
        """Plant a real, back-dated, logically-retired note (an episodic with an OPEN batch — a crash-duplicate
        orphan recall hides). Returns its content-free id."""
        rec = consolidate._make_episodic(session, {"role": role, "text": text, "tags": tags or []}, batch)
        rec["ts"] = int(time.time()) - age_days * _DAY
        ledger.append(rec)
        return rec[records.RECORD_ID_KEY]

    def _completed(self, text, *, role="decision", batch="bc", session="Sc"):
        """Plant a COMPLETED pass (episodic + its closing marker) — never retired. Returns the episodic's id."""
        rec = consolidate._make_episodic(session, {"role": role, "text": text}, batch)
        ledger.append(rec)
        ledger.append(consolidate._make_marker(session, batch))
        return rec[records.RECORD_ID_KEY]


# --- the deterministic probe -------------------------------------------------------------------------------

class ProbeTests(_Base):
    def test_an_old_retired_orphan_with_no_reinforcement_is_earned(self):
        rid = self._retired("an old hidden duplicate", age_days=60, batch="b1")
        self.assertEqual([r[records.RECORD_ID_KEY] for r in emit.earned_targets()], [rid])

    def test_a_fresh_retired_orphan_is_not_yet_earned(self):
        self._retired("a recent hidden duplicate", age_days=3, batch="b1")
        self.assertEqual(emit.earned_targets(), [])

    def test_a_completed_pass_is_not_retired_so_never_earned(self):
        self._completed("a completed, live note")
        self.assertEqual(emit.earned_targets(), [])

    def test_a_recalled_orphan_is_refused_by_the_safety_floor(self):
        rid = self._retired("an old but used duplicate", age_days=90, batch="b1")
        forget.record_access(rid)                                  # a reinforcement -> "never recalled" fails
        self.assertEqual(emit.earned_targets(), [])

    def test_earned_are_ordered_oldest_first(self):
        younger = self._retired("the younger earned note", age_days=40, batch="b1")
        older = self._retired("the older earned note", age_days=120, batch="b2")
        self.assertEqual([r[records.RECORD_ID_KEY] for r in emit.earned_targets()], [older, younger])

    def test_the_probe_is_deterministic(self):
        self._retired("note one", age_days=50, batch="b1")
        self._retired("note two", age_days=80, batch="b2")
        first = [r[records.RECORD_ID_KEY] for r in emit.earned_targets()]
        second = [r[records.RECORD_ID_KEY] for r in emit.earned_targets()]
        self.assertEqual(first, second)

    def test_the_threshold_is_the_recorded_leaf(self):
        # The window is a recorded build-spec leaf; a note straddling it flips on the leaf alone.
        self._retired("straddler", age_days=emit.EARNED_ERASURE_MIN_AGE_DAYS - 1, batch="b1")
        self.assertEqual(emit.earned_targets(), [])
        self._retired("well past", age_days=emit.EARNED_ERASURE_MIN_AGE_DAYS + 5, batch="b2")
        self.assertEqual(len(emit.earned_targets()), 1)


# --- the content-free proposal (D-007) ---------------------------------------------------------------------

class ProposalTests(_Base):
    def test_proposal_is_exactly_target_and_cost(self):
        rec = consolidate._make_episodic("S", {"role": "lesson", "text": "x"}, "b")
        proposal = emit.build_proposal(rec)
        self.assertEqual(set(proposal), {"target", "cost"})
        self.assertEqual(proposal["target"], rec[records.RECORD_ID_KEY])
        self.assertTrue(proposal["cost"])

    def test_cost_leaks_no_content_from_text_session_or_tags(self):
        # The record handed to build_proposal carries text, session_id AND tags — distinctive tokens in all three
        # must appear NOWHERE in the serialized proposal (D-007, made to flip — the compact._slip_mentions_word mirror).
        rec = consolidate._make_episodic(
            "zzsessionzz", {"role": "lesson", "text": "the qwerty floodgate recipe", "tags": ["mytagxyz"]}, "b")
        rec["ts"] = int(time.time()) - 60 * _DAY
        blob = json.dumps(emit.build_proposal(rec), ensure_ascii=False).lower()
        for token in ("qwerty", "floodgate", "recipe", "zzsessionzz", "mytagxyz"):
            self.assertNotIn(token, blob, f"content token {token!r} leaked into the committed proposal")

    def test_cost_never_surfaces_engine_shorthand_role_tokens(self):
        # The COMPOUND role tokens (engine shorthand — a slash or a hyphen) must be mapped to plain words, never
        # surfaced raw. (Plain single words like "decision"/"lesson" are ordinary English and may appear.) Every cost
        # is non-empty and carries no slash.
        for role in consolidate.ROLE_VOCABULARY:
            rec = consolidate._make_episodic("S", {"role": role, "text": "x"}, "b")
            cost = emit.build_proposal(rec)["cost"].lower()
            self.assertTrue(cost)
            self.assertNotIn("/", cost, f"a slash from {role!r} surfaced in operator copy")
        for compound in ("rationale/pushback", "dead-end"):
            rec = consolidate._make_episodic("S", {"role": compound, "text": "x"}, "b")
            self.assertNotIn(compound, emit.build_proposal(rec)["cost"].lower())

    def test_an_unknown_role_degrades_to_a_neutral_phrase(self):
        rec = consolidate._make_episodic("S", {"role": "lesson", "text": "x"}, "b")
        rec["role"] = "some-future-role"
        self.assertIn("a note", emit.build_proposal(rec)["cost"])

    def test_build_proposal_refuses_a_record_without_a_content_free_id(self):
        rec = consolidate._make_episodic("S", {"role": "lesson", "text": "x"}, "b")
        rec[records.RECORD_ID_KEY] = "not-a-uuid"
        with self.assertRaises(ValueError):
            emit.build_proposal(rec)


class WriteProposalTests(_Base):
    def test_writes_the_two_keys_at_the_observer_path(self):
        rec = consolidate._make_episodic("S", {"role": "lesson", "text": "x"}, "b")
        dest = emit.write_proposal(emit.build_proposal(rec), root=self._tmp.name)
        self.assertEqual(dest, os.path.join(self._tmp.name, obs._PROPOSAL_PATH))
        with open(dest, encoding="utf-8") as fh:
            written = json.load(fh)
        self.assertEqual(set(written), {"target", "cost"})
        self.assertEqual(written["target"], rec[records.RECORD_ID_KEY])

    def test_refuses_to_write_a_non_record_id_target(self):
        with self.assertRaises(ValueError):
            emit.write_proposal({"target": "", "cost": "x"}, root=self._tmp.name)


# --- the emitter<->observer round-trip (the load-bearing contract proof) ------------------------------------

class RoundTripTests(_Base):
    def test_the_observer_reads_back_exactly_what_the_proposer_writes(self):
        rid = self._retired("an old note to erase", age_days=70, batch="b1")
        proposal = emit.build_proposal(emit.earned_targets()[0])
        dest = emit.write_proposal(proposal, root=self._tmp.name)
        with open(dest, "rb") as fh:
            raw = fh.read()

        def serve(method, path, body):
            if "/contents/" in path:
                return 200, {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}
            return 404, None

        resolved = obs._read_target(obs._FakeGH(serve), "any-merge-sha")
        self.assertEqual(resolved, rid)                          # the real observer resolves it to the planted note
        self.assertTrue(obs._is_record_id(proposal["target"]))

    def test_the_emitter_reuses_the_observer_contract_so_it_cannot_drift(self):
        # Single source of truth: the emitter reuses the OBSERVER module's path/label/predicates, so the (ii)<->(iii)
        # contract cannot drift between the two sides.
        self.assertIs(emit.observer, obs)
        self.assertEqual(obs._PROPOSAL_PATH, ".engine/erasures/proposal.json")
        self.assertEqual(obs.ERASURE_LABEL, "engine-erasure")


# --- auto-open: dedup, fail-safe, the happy path, the footgun guard -----------------------------------------

class _OpenerSpy:
    """A stub PR-opener that records its call and never touches git/network."""

    def __init__(self, number=99):
        self.calls = []
        self.number = number

    def __call__(self, branch, title, body, paths, *, repo=None, token=None):
        self.calls.append({"branch": branch, "title": title, "body": body, "paths": paths})
        return self.number


def _no_existing_transport():
    """A transport with no existing erasure PRs; answers the label ensure/apply so the happy path completes."""
    labels = set()

    def transport(method, path, body):
        if path.endswith(f"/labels/{obs.ERASURE_LABEL}") and method == "GET":
            return (200, {"name": obs.ERASURE_LABEL}) if obs.ERASURE_LABEL in labels else (404, None)
        if "/issues/" in path and path.endswith("/labels") and method == "POST":
            return 200, []
        if path.endswith("/labels") and method == "POST":
            labels.add((body or {}).get("name"))
            return 201, {}
        if "/issues?" in path:
            return 200, []
        return 404, None

    return transport, labels


def _raise_if_called(*a, **k):
    raise AssertionError("the opener must not be reached")


class AutoOpenTests(_Base):
    def test_opens_one_single_purpose_labelled_pr_for_the_earned_note(self):
        rid = self._retired("an old hidden duplicate", age_days=60, batch="b1")
        opener = _OpenerSpy(number=99)
        transport, labels = _no_existing_transport()
        result = emit.propose(opener=opener, transport=transport, root=self._tmp.name)
        self.assertEqual(result["opened"], [99])
        self.assertEqual(result["target"], rid)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(opener.calls[0]["paths"], [obs._PROPOSAL_PATH])    # SINGLE-PURPOSE: only the proposal staged
        self.assertIn(obs.ERASURE_LABEL, labels)                            # the label was ensured + applied
        self.assertIs(result["labelled"], True)

    def test_the_pr_body_carries_the_cost_but_none_of_the_notes_words(self):
        self._retired("the secret zibbleflux migration", role="lesson", age_days=60, batch="b1")
        opener = _OpenerSpy()
        transport, _ = _no_existing_transport()
        emit.propose(opener=opener, transport=transport, root=self._tmp.name)
        self.assertNotIn("zibbleflux", opener.calls[0]["body"].lower())

    def test_skips_a_target_already_covered_by_an_existing_erasure_pr(self):
        rid = self._retired("an old hidden duplicate", age_days=60, batch="b1")
        prs = {5: {"number": 5, "merged_at": None, "merge_commit_sha": None, "head": {"sha": "abc"}}}

        def transport(method, path, body):
            if "/issues?" in path:
                return 200, [{"number": 5, "pull_request": {}}]
            if "/pulls/5" in path:
                return 200, prs[5]
            if "/contents/" in path and "ref=abc" in path:
                return 200, _contents(rid)
            return 404, None

        result = emit.propose(opener=_raise_if_called, transport=transport, root=self._tmp.name)
        self.assertEqual(result["opened"], [])

    def test_skips_a_target_already_enacted_in_the_ledger(self):
        rid = self._retired("an old hidden duplicate", age_days=60, batch="b1")
        compact.enact_erasure(rid, "somemergesha")                          # a retained marker already targets it
        result = emit.propose(opener=_raise_if_called, transport=_no_existing_transport()[0], root=self._tmp.name)
        self.assertEqual(result["opened"], [])

    def test_declines_to_open_when_the_pr_list_cannot_be_read(self):
        self._retired("an old hidden duplicate", age_days=60, batch="b1")

        def transport(method, path, body):
            if "/issues?" in path:
                return 503, None                                            # host doubt reading the list
            return 404, None

        result = emit.propose(opener=_raise_if_called, transport=transport, root=self._tmp.name)
        self.assertEqual(result["opened"], [])                              # fail-SAFE: declined, no duplicate risk

    def test_no_earned_note_opens_nothing(self):
        self._retired("a fresh one", age_days=2, batch="b1")
        result = emit.propose(opener=_raise_if_called, transport=_no_existing_transport()[0], root=self._tmp.name)
        self.assertEqual(result["opened"], [])

    def test_a_transport_only_practice_run_writes_but_never_reaches_the_real_opener(self):
        # The footgun guard (mirrors module_manager): an injected transport with NO opener writes the proposal but
        # opens nothing — so the REAL _open_erasure_pr is never reached in the suite (construction-repo safety, proven).
        self._retired("an old hidden duplicate", age_days=60, batch="b1")
        result = emit.propose(transport=_no_existing_transport()[0], root=self._tmp.name)
        self.assertEqual(result["opened"], [])
        self.assertIn("practice", result["message"].lower())
        self.assertTrue(os.path.exists(os.path.join(self._tmp.name, obs._PROPOSAL_PATH)))


# --- structural + the committed placeholder ----------------------------------------------------------------

class StructuralTests(unittest.TestCase):
    def test_the_emitter_never_calls_the_slice_i_minter(self):
        # Belt-and-suspenders to test_forget's package-wide scan: the producer writes a file + opens a PR; it never
        # mints the erasure marker (that is compact's, gated on the merge + the observer).
        with open(emit.__file__, encoding="utf-8") as fh:
            self.assertNotIn("enact_erasure(", fh.read())

    def test_the_committed_placeholder_is_structurally_inert(self):
        # The template ships a permanent placeholder proposal whose target CANNOT validate — so a stray read of
        # main's proposal (between real erasures) is a guaranteed no-op.
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        with open(os.path.join(root, obs._PROPOSAL_PATH), encoding="utf-8") as fh:
            placeholder = json.load(fh)
        self.assertEqual(set(placeholder), {"target", "cost"})
        self.assertFalse(obs._is_record_id(placeholder["target"]))


if __name__ == "__main__":
    unittest.main()
