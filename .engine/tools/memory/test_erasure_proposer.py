"""Unit tests for erasure_proposer.py — the Layer-2 erasure EMITTER.

The emitter selects an already-logically-retired note that has EARNED erasure, writes a content-free proposal at the
observer's fixed path, and AUTO-OPENS a single-purpose `engine-erasure` pull request. These tests pin the load-bearing
behavior with the GitHub network + the PR-opener stubbed (no live GitHub, no real git): the deterministic probe selects
the old hidden duplicate and skips the fresh / recalled / completed ones; the cost leaks NONE of the note's content
(text, session id, or tags); the written proposal is EXACTLY what the real observer reads back (the
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
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quiet_call  # noqa: E402  (capture a demo walkthrough's stdout so it can't bury the suite summary)
from memory import compact, consolidate, erasure_observer as obs  # noqa: E402
from memory import erasure_proposer as emit, forget, ledger, records  # noqa: E402

_DAY = 86400


def _contents(target: str) -> dict:
    """A base64 contents response naming `target` via the LEGACY single-target shape `{"target": …}` (the observer
    reads it as a one-note batch — this doubles as back-compat coverage of the legacy single-target on-disk grammar)."""
    raw = json.dumps({"target": target, "cost": "a paraphrase"}).encode("utf-8")
    return {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}


def _contents_batch(targets: list) -> dict:
    """A base64 contents response naming a BATCH via the batch grammar `{"targets": […], "costs": […]}`."""
    raw = json.dumps({"targets": list(targets), "costs": ["a paraphrase"] * len(targets)}).encode("utf-8")
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

    def _consolidated_raw(self, *, n=2, age_days=60, session="Scr", batch="bcr", text="a consolidated raw note"):
        """Plant a SETTLED consolidated session: an episodic + its closing marker (both aged `age_days`) and `n` raw
        turn-deltas captured just before the marker. Returns the turn-delta ids (the consolidated-raw erasure
        targets that `earned_consolidated_raw` yields once the gist is stable)."""
        m_ts = int(time.time()) - age_days * _DAY
        ep = consolidate._make_episodic(session, {"role": "decision", "text": "a summary stands in"}, batch)
        ep["ts"] = m_ts
        ledger.append(ep)
        mk = consolidate._make_marker(session, batch)
        mk["ts"] = m_ts
        ledger.append(mk)
        ids = []
        for i in range(n):
            rec = {"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, records.RECORD_ID_KEY: records.new_record_id(),
                   "session_id": session, "ts": m_ts - _DAY, "text": f"{text} {i}", "tags": []}
            ledger.append(rec)
            ids.append(rec[records.RECORD_ID_KEY])
        return ids


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


# --- the content-free proposal ---------------------------------------------------------------------

class ProposalTests(_Base):
    def test_proposal_is_exactly_targets_and_costs(self):
        rec = consolidate._make_episodic("S", {"role": "lesson", "text": "x"}, "b")
        proposal = emit.build_proposal([rec])
        self.assertEqual(set(proposal), {"targets", "costs"})
        self.assertEqual(proposal["targets"], [rec[records.RECORD_ID_KEY]])
        self.assertEqual(len(proposal["costs"]), 1)
        self.assertTrue(proposal["costs"][0])

    def test_a_batch_carries_one_cost_per_target_in_order(self):
        recs = [consolidate._make_episodic("S", {"role": "lesson", "text": "x"}, f"b{i}") for i in range(3)]
        proposal = emit.build_proposal(recs)
        self.assertEqual(proposal["targets"], [r[records.RECORD_ID_KEY] for r in recs])
        self.assertEqual(len(proposal["costs"]), len(proposal["targets"]))    # one-to-one, order preserved

    def test_cost_leaks_no_content_from_text_session_or_tags(self):
        # Each record handed to build_proposal carries text, session_id AND tags — distinctive tokens in all three
        # must appear NOWHERE in the serialized proposal (made to flip — the compact._slip_mentions_word mirror).
        # Scanned over a BATCH, so a leak from ANY note in the batch flips it red.
        recs = []
        for i, marker in enumerate(("qwerty", "floodgate", "zzsessionzz", "mytagxyz")):
            rec = consolidate._make_episodic(
                f"zzsession{marker}", {"role": "lesson", "text": f"the {marker} recipe", "tags": [f"tag{marker}"]}, f"b{i}")
            rec["ts"] = int(time.time()) - 60 * _DAY
            recs.append(rec)
        blob = json.dumps(emit.build_proposal(recs), ensure_ascii=False).lower()
        for token in ("qwerty", "floodgate", "recipe", "zzsession", "mytagxyz"):
            self.assertNotIn(token, blob, f"content token {token!r} leaked into the committed proposal")

    def test_cost_never_surfaces_engine_shorthand_role_tokens(self):
        # The COMPOUND role tokens (engine shorthand — a slash or a hyphen) must be mapped to plain words, never
        # surfaced raw. (Plain single words like "decision"/"lesson" are ordinary English and may appear.) Every cost
        # is non-empty and carries no slash.
        for role in consolidate.ROLE_VOCABULARY:
            rec = consolidate._make_episodic("S", {"role": role, "text": "x"}, "b")
            cost = emit.build_proposal([rec])["costs"][0].lower()
            self.assertTrue(cost)
            self.assertNotIn("/", cost, f"a slash from {role!r} surfaced in operator copy")
        for compound in ("rationale/pushback", "dead-end"):
            rec = consolidate._make_episodic("S", {"role": compound, "text": "x"}, "b")
            self.assertNotIn(compound, emit.build_proposal([rec])["costs"][0].lower())

    def test_an_unknown_role_degrades_to_a_neutral_phrase(self):
        rec = consolidate._make_episodic("S", {"role": "lesson", "text": "x"}, "b")
        rec["role"] = "some-future-role"
        self.assertIn("a note", emit.build_proposal([rec])["costs"][0])

    def test_build_proposal_refuses_a_record_without_a_content_free_id(self):
        rec = consolidate._make_episodic("S", {"role": "lesson", "text": "x"}, "b")
        rec[records.RECORD_ID_KEY] = "not-a-uuid"
        with self.assertRaises(ValueError):
            emit.build_proposal([rec])

    def test_build_proposal_refuses_an_empty_batch(self):
        with self.assertRaises(ValueError):
            emit.build_proposal([])


class WriteProposalTests(_Base):
    def test_writes_the_two_keys_at_the_observer_path(self):
        recs = [consolidate._make_episodic("S", {"role": "lesson", "text": "x"}, f"b{i}") for i in range(2)]
        dest = emit.write_proposal(emit.build_proposal(recs), root=self._tmp.name)
        self.assertEqual(dest, os.path.join(self._tmp.name, obs._PROPOSAL_PATH))
        with open(dest, encoding="utf-8") as fh:
            written = json.load(fh)
        self.assertEqual(set(written), {"targets", "costs"})
        self.assertEqual(written["targets"], [r[records.RECORD_ID_KEY] for r in recs])
        self.assertEqual(len(written["costs"]), len(written["targets"]))

    def test_refuses_to_write_a_non_record_id_target(self):
        with self.assertRaises(ValueError):
            emit.write_proposal({"targets": [""], "costs": ["x"]}, root=self._tmp.name)

    def test_refuses_to_write_an_empty_batch(self):
        with self.assertRaises(ValueError):
            emit.write_proposal({"targets": [], "costs": []}, root=self._tmp.name)

    def test_refuses_to_write_costs_that_do_not_match_the_targets(self):
        rid = "a" * 32
        with self.assertRaises(ValueError):
            emit.write_proposal({"targets": [rid], "costs": ["one", "two"]}, root=self._tmp.name)


# --- the emitter<->observer round-trip (the load-bearing contract proof) ------------------------------------

class RoundTripTests(_Base):
    def test_the_observer_reads_back_exactly_what_the_proposer_writes(self):
        older = self._retired("an old note to erase", age_days=90, batch="b1")
        younger = self._retired("another old note to erase", age_days=70, batch="b2")
        proposal = emit.build_proposal(emit.earned_targets())          # the whole earned batch, oldest-first
        dest = emit.write_proposal(proposal, root=self._tmp.name)
        with open(dest, "rb") as fh:
            raw = fh.read()

        def serve(method, path, body):
            if "/contents/" in path:
                return 200, {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}
            return 404, None

        resolved = obs._read_targets(obs._FakeGH(serve), "any-merge-sha")
        self.assertEqual(resolved, [older, younger])               # the real observer resolves the batch, in order
        self.assertTrue(all(obs._is_record_id(t) for t in proposal["targets"]))

    def test_the_emitter_reuses_the_observer_contract_so_it_cannot_drift(self):
        # Single source of truth: the emitter reuses the OBSERVER module's path/label/predicates, so the observer<->proposer
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
        self.assertEqual(result["targets"], [rid])
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

    def test_declines_when_only_the_dedup_list_is_unreadable_even_if_the_serializer_reads_clean(self):
        # ISOLATES the cross-PR dedup fail-SAFE from the one-in-flight serializer. `propose()` reads /issues? twice:
        # state=all (dedup) and state=open (serializer). Here the DEDUP read (state=all) is unreadable (503) but the
        # serializer read (state=open) is clean-and-empty. If `_proposed_targets(gh) is None` were coalesced to an
        # empty set (fail-OPEN), a duplicate erasure PR would open. It must DECLINE. (A single "503 on every /issues?"
        # test cannot catch this: the serializer's own decline masks whether the dedup path declined — D-of-the-gate.)
        self._retired("an old hidden duplicate", age_days=60, batch="b1")

        def transport(method, path, body):
            if "state=all" in path:
                return 503, None                                            # the DEDUP list is unreadable
            if "state=open" in path:
                return 200, []                                              # the serializer reads clean (none open)
            return 404, None

        result = emit.propose(opener=_raise_if_called, transport=transport, root=self._tmp.name)
        self.assertEqual(result["opened"], [])                              # declined on the dedup doubt alone
        self.assertIn("no duplicate risk", result["message"].lower())

    def test_no_earned_note_opens_nothing(self):
        self._retired("a fresh one", age_days=2, batch="b1")
        result = emit.propose(opener=_raise_if_called, transport=_no_existing_transport()[0], root=self._tmp.name)
        self.assertEqual(result["opened"], [])

    def test_a_transport_only_run_drives_the_real_api_opener_and_fails_safe(self):
        # With the hook-safe API opener, a transport-only run (no injected opener) exercises the REAL _open_erasure_pr
        # against the injected transport — fully offline. A transport that cannot complete the API sequence (here: no
        # git-ref handler) makes the opener FAIL-SAFE: no open, no crash. (The suite never opens against real GitHub
        # because every call injects a fake transport.)
        self._retired("an old hidden duplicate", age_days=60, batch="b1")
        result = emit.propose(transport=_no_existing_transport()[0], root=self._tmp.name)
        self.assertEqual(result["opened"], [])
        self.assertIn("could not open", result["message"].lower())


def _writable_transport(*, base_sha="basesha", pr_number=77):
    """A transport that COMPLETES the API opener's create-ref -> put-contents -> open-pull sequence (and reports no
    existing erasure PRs), recording every call — so the REAL `_open_erasure_pr` runs fully offline."""
    calls = []

    def transport(method, path, body):
        calls.append((method, path, body))
        if "/issues?" in path:                                     # dedup (state=all) + serializer (state=open): none
            return 200, []
        if "/git/ref/heads/" in path and method == "GET":
            return 200, {"object": {"sha": base_sha}}
        if path.endswith("/git/refs") and method == "POST":
            return 201, {"ref": (body or {}).get("ref")}
        if "/contents/" in path and method == "GET":                  # the committed placeholder's existing blob sha
            return 200, {"sha": "existingblobsha", "content": "", "encoding": "base64"}
        if "/contents/" in path and method == "PUT":
            return 201, {"commit": {"sha": "commitsha"}}
        if path.endswith("/pulls") and method == "POST":
            return 201, {"number": pr_number}
        if path.endswith("/labels") and method == "POST":
            return 200, []
        if path.endswith(f"/labels/{obs.ERASURE_LABEL}") and method == "GET":
            return 404, None
        return 404, None

    return transport, calls


class ApiOpenerTests(_Base):
    def test_opens_purely_via_the_api_and_commits_only_the_one_proposal_file(self):
        transport, calls = _writable_transport(pr_number=88)
        number = emit._open_erasure_pr(obs._FakeGH(transport), "erasure-x", "title", "body", '{"target":"x"}')
        self.assertEqual(number, 88)
        puts = [c for c in calls if c[0] == "PUT" and "/contents/" in c[1]]
        self.assertEqual(len(puts), 1, "exactly one file is committed (single-purpose merge tree)")
        self.assertTrue(puts[0][1].endswith(obs._PROPOSAL_PATH))
        self.assertEqual(puts[0][2].get("sha"), "existingblobsha", "updates the committed placeholder by its blob sha")
        self.assertFalse(any("checkout" in str(c) or "push" in str(c) for c in calls), "no local git")

    def test_a_transport_only_run_opens_through_the_real_api_opener(self):
        rid = self._retired("an old hidden duplicate", age_days=60, batch="b1")
        transport, _calls = _writable_transport(pr_number=77)
        result = emit.propose(transport=transport, root=self._tmp.name)
        self.assertEqual(result["opened"], [77])
        self.assertEqual(result["targets"], [rid])

    def test_fails_safe_when_the_base_ref_is_unreadable(self):
        def t(method, path, body):
            return (404, None)                                     # even the base ref read fails
        self.assertIsNone(emit._open_erasure_pr(obs._FakeGH(t), "br", "t", "b", "c"))

    def test_fails_safe_when_the_stale_branch_backing_check_is_unreadable(self):
        # On a 422 (stale branch), the opener VERIFIES the branch backs no open PR before replacing it. If that check
        # is unreadable (host doubt), it DECLINES — never delete on doubt, never duplicate.
        def t(method, path, body):
            if "/git/ref/heads/" in path and method == "GET":
                return 200, {"object": {"sha": "s"}}
            if path.endswith("/git/refs") and method == "POST":
                return 422, {"message": "Reference already exists"}
            return 404, None                                                   # the /pulls?head= check is unreadable
        self.assertIsNone(emit._open_erasure_pr(obs._FakeGH(t), "br", "t", "b", "c"))

    def test_a_stale_branch_with_no_open_pr_is_replaced_and_the_pr_opens(self):
        # Re-offer: a declined PR's branch lingers, so a same-set re-offer 422s. The opener verifies no open PR backs
        # it, DELETEs the stale ref (204 No Content), re-creates, and opens the fresh PR.
        creates = {"n": 0}
        deletes = {"n": 0}

        def t(method, path, body):
            if "/git/ref/heads/" in path and method == "GET":
                return 200, {"object": {"sha": "s"}}
            if path.endswith("/git/refs") and method == "POST":
                creates["n"] += 1
                return (422, {"message": "exists"}) if creates["n"] == 1 else (201, {"ref": (body or {}).get("ref")})
            if "/pulls?head=" in path and method == "GET":
                return 200, []                                                 # no open PR backs the branch
            if "/git/refs/heads/" in path and method == "DELETE":
                deletes["n"] += 1
                return 204, None                                               # a successful ref delete is 204
            if "/contents/" in path and method == "GET":
                return 200, {"sha": "blob", "content": "", "encoding": "base64"}
            if "/contents/" in path and method == "PUT":
                return 201, {}
            if path.endswith("/pulls") and method == "POST":
                return 201, {"number": 55}
            return 404, None

        self.assertEqual(emit._open_erasure_pr(obs._FakeGH(t), "erasure-x", "t", "b", "c"), 55)
        self.assertEqual((creates["n"], deletes["n"]), (2, 1), "422 -> delete once -> re-create -> open")

    def test_a_stale_branch_backed_by_an_open_pr_is_never_deleted(self):
        # The one duplicate hole the plan gate flagged: a label POST can fail-open, leaving an OPEN-but-unlabelled PR
        # the serializer cannot see. Deleting its head would orphan it and let a DUPLICATE open. So a branch that
        # backs any open PR is never deleted -> DECLINE.
        deletes = {"n": 0}

        def t(method, path, body):
            if "/git/ref/heads/" in path and method == "GET":
                return 200, {"object": {"sha": "s"}}
            if path.endswith("/git/refs") and method == "POST":
                return 422, {"message": "exists"}
            if "/pulls?head=" in path and method == "GET":
                return 200, [{"number": 9}]                                    # an OPEN PR backs the branch
            if "/git/refs/heads/" in path and method == "DELETE":
                deletes["n"] += 1
                return 204, None
            return 404, None

        self.assertIsNone(emit._open_erasure_pr(obs._FakeGH(t), "erasure-x", "t", "b", "c"))
        self.assertEqual(deletes["n"], 0, "never delete a branch backed by an open PR")

    def test_does_not_raise_on_a_transport_fault(self):
        def t(method, path, body):
            raise RuntimeError("network down")
        self.assertIsNone(emit._open_erasure_pr(obs._FakeGH(t), "br", "t", "b", "c"))


class SerializerTests(_Base):
    def test_holds_the_next_proposal_while_an_erasure_pr_is_already_open(self):
        # one-in-flight: an OPEN engine-erasure PR (for ANOTHER note, so per-target dedup does not fire) -> hold.
        self._retired("an old hidden duplicate", age_days=60, batch="b1")
        other = "f" * 32

        def transport(method, path, body):
            if "/issues?" in path:                                 # both state=all and state=open report PR #5 open
                return 200, [{"number": 5, "pull_request": {}}]
            if "/pulls/5" in path:
                return 200, {"number": 5, "merged_at": None, "merge_commit_sha": None, "head": {"sha": "abc"}}
            if "/contents/" in path and "ref=abc" in path:
                return 200, _contents(other)                       # the open PR targets a DIFFERENT note
            return 404, None

        result = emit.propose(opener=_raise_if_called, transport=transport, root=self._tmp.name)
        self.assertEqual(result["opened"], [])
        self.assertIn("already open", result["message"].lower())

    def test_declines_when_the_open_list_cannot_be_read(self):
        self._retired("an old hidden duplicate", age_days=60, batch="b1")

        def transport(method, path, body):
            if "state=open" in path:
                return 503, None                                   # serializer read fails -> DECLINE, never proceed
            if "/issues?" in path:
                return 200, []                                     # per-target dedup: none
            return 404, None

        result = emit.propose(opener=_raise_if_called, transport=transport, root=self._tmp.name)
        self.assertEqual(result["opened"], [])
        self.assertIn("declined", result["message"].lower())


class ThrottleTests(_Base):
    def test_checks_when_there_is_no_record(self):
        self.assertTrue(emit._should_check(1_000))

    def test_skips_within_the_cooldown(self):
        emit._record_check(1_000_000)
        self.assertFalse(emit._should_check(1_000_000 + emit._DAY))

    def test_checks_after_the_interval(self):
        emit._record_check(1_000_000)
        self.assertTrue(emit._should_check(1_000_000 + (emit.EARNED_ERASURE_CHECK_INTERVAL_DAYS + 1) * emit._DAY))

    def test_checks_on_a_future_timestamp_so_it_never_sticks_off(self):
        emit._record_check(2_000_000)                              # a 'future' last-check (clock skew / tampering)
        self.assertTrue(emit._should_check(1_000_000))

    def test_checks_on_a_corrupt_sidecar(self):
        with open(emit._state_path(), "w", encoding="utf-8") as fh:
            fh.write("not json {{")
        self.assertTrue(emit._should_check(1_000_000))

    def test_record_check_persists(self):
        emit._record_check(1234567)
        self.assertEqual(emit._last_check(), 1234567)


class SessionStartTests(_Base):
    def test_silent_no_op_when_throttled_and_does_not_even_check(self):
        emit._record_check(1_000_000)
        with mock.patch.object(emit, "propose", side_effect=AssertionError("must not check while throttled")) as p:
            out = emit._session_start_handler({}, now=1_000_000 + emit._DAY)        # within the cooldown
        self.assertEqual(p.call_count, 0)
        self.assertIsInstance(out, dict)

    def test_runs_and_relays_a_heads_up_on_a_new_open_and_stamps_the_check(self):
        with mock.patch.object(emit, "propose", return_value={"opened": [7], "targets": ["a" * 32, "b" * 32]}):
            out = emit._session_start_handler({}, now=2_000_000)
        blob = json.dumps(out)
        self.assertIn("#7", blob)
        self.assertIn("2 old notes", blob)                                           # the batch count is relayed
        self.assertEqual(emit._last_check(), 2_000_000)                              # the check was stamped

    def test_is_silent_when_nothing_was_opened(self):
        with mock.patch.object(emit, "propose", return_value={"opened": [], "targets": []}):
            out = emit._session_start_handler({}, now=2_000_000)
        self.assertNotIn("#", json.dumps(out))

    def test_fail_open_when_propose_raises(self):
        with mock.patch.object(emit, "propose", side_effect=RuntimeError("boom")):
            out = emit._session_start_handler({}, now=2_000_000)                     # must NOT raise
        self.assertIsInstance(out, dict)

    def test_the_offline_demo_never_reaches_the_real_opener(self):
        with mock.patch.object(emit, "_open_erasure_pr", side_effect=AssertionError("the real opener was reached")):
            ok = quiet_call.run(emit._demo_body, self._tmp.name)                     # injects a stub opener
        self.assertTrue(ok)


class HeadsUpTests(unittest.TestCase):
    def test_plain_language_names_the_pr_and_carries_no_jargon(self):
        text = emit._heads_up([7])
        self.assertIn("#7", text)
        self.assertIn("close", text.lower())
        self.assertIn("recoverable", text.lower())
        self.assertNotIn("target", text.lower())                                     # no record-id / engine jargon

    def test_number_agreement_singular_vs_batch(self):
        # A single-note relay must read singular throughout (no plural "them" against "one note"); a batch, plural.
        one = emit._heads_up([7], 1)
        self.assertIn("one old note", one)
        self.assertNotIn("them", one)                                                # "keep it / erase it", never "them"
        self.assertNotIn("those notes", one)
        many = emit._heads_up([7], 3)
        self.assertIn("3 old notes", many)
        self.assertIn("them", many)

    def test_relay_says_notes_may_be_offered_again(self):
        self.assertIn("again", emit._heads_up([7], 2).lower())                        # re-offer: not 'keep forever'
        self.assertNotIn("will not raise", emit._heads_up([7], 2).lower())


# --- structural + the committed placeholder ----------------------------------------------------------------

class StructuralTests(unittest.TestCase):
    def test_the_emitter_never_calls_the_slice_i_minter(self):
        # Belt-and-suspenders to test_forget's package-wide scan: the producer writes a file + opens a PR; it never
        # mints the erasure marker (that is compact's, gated on the merge + the observer).
        with open(emit.__file__, encoding="utf-8") as fh:
            self.assertNotIn("enact_erasure(", fh.read())

    def test_the_committed_proposal_is_well_formed_and_content_free(self):
        # The committed proposal carries EXACTLY {targets, costs}, and every target is CONTENT-FREE: the batch is
        # either the inert empty list the template ships between erasures, OR a list of valid content-free record-id
        # shapes during a live erasure proposal, with one cost line per target. The design LAW is that the committed PR "names the target(s) by a stable, content-free record id …
        # read at the merge identity" — so live record-ids here are REQUIRED for the flow, not a hazard (an earlier
        # blanket "target can NEVER validate" assertion contradicted that law and made every erasure PR red
        # engine-ci and so un-mergeable). The real safety is dynamic: the observer binds to the immutable merge
        # tree, acts only on a genuine merge, dedups per target, and WHOLE-BATCH-REJECTS any malformed element — it
        # never enacts off a stray read of main's HEAD. This is STRICTER on content-freeness (it forbids an offset /
        # path / leaked-content target) while permitting the two states the design intends.
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        with open(os.path.join(root, obs._PROPOSAL_PATH), encoding="utf-8") as fh:
            proposal = json.load(fh)
        self.assertEqual(set(proposal), {"targets", "costs"})
        targets = proposal["targets"]
        self.assertIsInstance(targets, list)
        self.assertTrue(all(obs._is_record_id(t) for t in targets),
                        "every committed proposal target must be a content-free record-id shape (or the list "
                        f"empty between erasures), never anything else (got {targets!r})")
        self.assertIsInstance(proposal["costs"], list)
        self.assertEqual(len(proposal["costs"]), len(targets), "one cost line per target")
        self.assertTrue(all(isinstance(c, str) for c in proposal["costs"]))


class PrBodyConsentTests(unittest.TestCase):
    """The auto-opened erasure PR is exempt from the eight-section body check — it carries a deliberate
    plain consent body, not the engineer template (ci_label_exempt on the engine-erasure label). That
    exemption removes the only GENERIC check on this body, so its consent essentials are pinned SPECIFICALLY
    here: an operator reading only this PR body must be able to decide whether to consent. The positive
    companion to HeadsUpTests, guarding the most consent-critical copy in the engine from a silent hollowing."""
    def test_the_pr_body_states_the_consent_picture_and_carries_no_jargon(self):
        body = emit._pr_body({"targets": ["f729d2c52dbc44be9eadfdc0ac0b51ba"],
                              "costs": ["a withdrawn note you set aside DISTINCT-COST-MARKER."]})
        low = body.lower()
        self.assertIn("DISTINCT-COST-MARKER", body)            # the plain-language cost is shown verbatim
        self.assertIn("permanently erase", low)                # what merging does
        self.assertIn("consent", low)                          # merging IS the consent act
        self.assertIn("close", low)                            # declining = close the PR
        self.assertIn("loses nothing", low)                    # closing is safe
        self.assertIn("recoverable", low)                      # still recoverable until erased
        self.assertIn("a later session", low)                  # erasure is DEFERRED — not immediate on merge,
        self.assertIn("the moment you merge", low)             # the clause that makes merge consent, not destruction
        self.assertNotIn("target", low)                        # no engine jargon
        self.assertNotIn("f729d2c5", low)                      # the opaque record-id never appears in the body

    def test_a_batch_body_enumerates_every_note_and_states_all_or_nothing(self):
        # The consent surface for a BATCH: the operator reads ONE line per note (never a bare count), so they see
        # the full list they consent to erase, plus the all-or-nothing nature and the safe decline. This is the
        # load-bearing property the plan gate flagged — a count-only body would be a weaker consent surface.
        costs = [f"a withdrawn note number {i} MARKER-{i}." for i in range(3)]
        body = emit._pr_body({"targets": ["a" * 32, "b" * 32, "c" * 32], "costs": costs})
        low = body.lower()
        for i in range(3):
            self.assertIn(f"MARKER-{i}", body, "every note's own cost line must appear (per-note enumeration)")
        self.assertIn("3 remembered notes", low)               # the scale is stated
        self.assertIn("all-or-nothing", low)                   # merging erases the whole batch
        self.assertIn("no way to keep just some", low)         # the honest trade: merge-all or keep-all
        self.assertIn("no per-note pick", low)                 # per-note selection does not exist
        self.assertIn("consent", low)
        self.assertIn("close", low)                            # the safe decline path
        self.assertIn("recoverable", low)
        self.assertNotIn("targets", low)                       # no engine jargon
        for opaque in ("a" * 32, "b" * 32, "c" * 32):
            self.assertNotIn(opaque, low)                      # opaque record-ids never appear in the body

    def test_the_body_refuses_an_empty_batch(self):
        with self.assertRaises(ValueError):
            emit._pr_body({"targets": [], "costs": []})


class BatchProposeTests(_Base):
    """One merge clears one COHERENT batch. `propose` bundles the oldest homogeneous group of currently-earned
    notes (same note-kind and vintage, minus any already scheduled or proposed) into ONE single-purpose pull
    request, so the operator consents to a coherent batch once and a large backlog clears over successive
    batches — a note the operator keeps only ever holds up its own group (issue #536)."""

    def test_one_homogeneous_group_bundles_into_one_pull_request(self):
        # Three duplicates of the same vintage (all in the "about a month ago" bucket) are one homogeneous
        # group, so they clear in ONE pull request — the operator consents to the coherent batch once.
        ids = {self._retired(f"old duplicate {i}", age_days=60 + i, batch=f"b{i}") for i in range(3)}
        opener = _OpenerSpy(number=42)
        transport, _labels = _no_existing_transport()
        result = emit.propose(opener=opener, transport=transport, root=self._tmp.name)
        self.assertEqual(result["opened"], [42])
        self.assertEqual(len(opener.calls), 1, "ONE pull request clears the coherent group, not one per note")
        self.assertEqual(set(result["targets"]), ids)
        self.assertIn("clearing 3 notes", result["message"])

    def test_a_heterogeneous_pool_offers_only_the_oldest_group(self):
        # Two vintages of duplicate: an older group (~100d → "about 3 months ago") and a newer group
        # (~40d → "about a month ago"). propose offers ONLY the oldest group, so the newer notes are not
        # dragged into a batch the operator hasn't seen as a unit (the #536 core: small coherent batches).
        older = {self._retired(f"older dup {i}", age_days=100 + i, batch=f"o{i}") for i in range(2)}
        newer = {self._retired(f"newer dup {i}", age_days=40 + i, batch=f"n{i}") for i in range(2)}
        opener = _OpenerSpy(number=55)
        transport, _labels = _no_existing_transport()
        result = emit.propose(opener=opener, transport=transport, root=self._tmp.name)
        self.assertEqual(set(result["targets"]), older, "only the oldest homogeneous group is offered")
        self.assertEqual(set(result["targets"]) & newer, set(), "a newer group is never dragged in")
        self.assertIn("clearing 2 notes", result["message"])

    def test_a_declined_group_steps_aside_so_a_newer_group_is_offered(self):
        # #536 partial-keep: the operator DECLINES the oldest group (wants to keep a note in it). The next
        # check must not re-offer that same oldest group forever and starve the newer one — it steps the
        # declined group aside and offers the NEWER group, so the keeper blocks only its own group.
        older = {self._retired(f"older dup {i}", age_days=100 + i, batch=f"o{i}") for i in range(2)}
        newer = {self._retired(f"newer dup {i}", age_days=40 + i, batch=f"n{i}") for i in range(2)}
        hub = emit._DemoHub(self._tmp.name)
        first = emit.propose(opener=hub.open, transport=hub.transport, root=self._tmp.name)
        self.assertEqual(set(first["targets"]), older)                    # the oldest group is offered first
        hub.close(first["opened"][0])                                     # the operator DECLINES it (keep)
        second = emit.propose(opener=hub.open, transport=hub.transport, root=self._tmp.name)
        self.assertEqual(set(second["targets"]), newer, "the declined group steps aside; the newer group is offered")
        self.assertTrue(second["opened"] and second["opened"][0] != first["opened"][0])

    def test_all_groups_declined_re_offers_the_oldest(self):
        # When every remaining group has been declined (nothing fresh is left), a decline is still "not this
        # time", not "keep forever" — the oldest declined group is re-offered rather than nothing at all.
        older = {self._retired(f"older dup {i}", age_days=100 + i, batch=f"o{i}") for i in range(2)}
        hub = emit._DemoHub(self._tmp.name)
        first = emit.propose(opener=hub.open, transport=hub.transport, root=self._tmp.name)
        self.assertEqual(set(first["targets"]), older)
        hub.close(first["opened"][0])                                     # decline the only group
        second = emit.propose(opener=hub.open, transport=hub.transport, root=self._tmp.name)
        self.assertEqual(set(second["targets"]), older, "the only group, though declined, is re-offered")
        self.assertTrue(second["opened"])

    def test_excludes_an_already_enacted_target_from_the_batch(self):
        keep = self._retired("still earned", age_days=60, batch="b1")
        done = self._retired("already scheduled", age_days=90, batch="b2")
        compact.enact_erasure(done, "somemergesha")                       # a retained marker already targets `done`
        opener = _OpenerSpy(number=51)
        transport, _labels = _no_existing_transport()
        result = emit.propose(opener=opener, transport=transport, root=self._tmp.name)
        self.assertEqual(set(result["targets"]), {keep})                  # the enacted one is not re-proposed
        self.assertNotIn(done, result["targets"])

    def test_excludes_an_already_proposed_target_from_the_batch(self):
        keep = self._retired("still earned", age_days=60, batch="b1")
        covered = self._retired("already in a PR", age_days=90, batch="b2")
        opener = _OpenerSpy(number=63)

        def transport(method, path, body):
            if path.endswith(f"/labels/{obs.ERASURE_LABEL}") and method == "GET":
                return 404, None
            if "/issues/" in path and path.endswith("/labels") and method == "POST":
                return 200, []
            if path.endswith("/labels") and method == "POST":
                return 201, {}
            if "state=open" in path:                                      # serializer: none open
                return 200, []
            if "/issues?" in path:                                        # dedup list: one PR covering `covered`
                return 200, [{"number": 9, "pull_request": {}}]
            if "/pulls/9" in path:
                return 200, {"number": 9, "merged_at": None, "merge_commit_sha": None, "head": {"sha": "sha9"}}
            if "/contents/" in path and "ref=sha9" in path:
                return 200, _contents(covered)                            # legacy single-target proposal shape
            return 404, None

        result = emit.propose(opener=opener, transport=transport, root=self._tmp.name)
        self.assertEqual(set(result["targets"]), {keep})                  # the already-proposed one is excluded
        self.assertNotIn(covered, result["targets"])

    def test_a_batch_of_already_covered_notes_opens_nothing(self):
        done = self._retired("already scheduled", age_days=90, batch="b1")
        compact.enact_erasure(done, "somemergesha")
        result = emit.propose(opener=_raise_if_called, transport=_no_existing_transport()[0], root=self._tmp.name)
        self.assertEqual(result["opened"], [])
        self.assertIn("already scheduled or proposed", result["message"])


class ConsolidatedRawClassTests(_Base):
    """The consolidated-raw evidence class flows through the emitter — `earned_targets` unions it with the
    crash-duplicate class, the role-less cost line is content-free and names the verbatim it ends, the body collapses
    identical lines to a per-vintage count, and neither the committed proposal nor the body leaks a session id."""

    def test_earned_targets_unions_both_classes(self):
        dup = self._retired("an old hidden duplicate", age_days=60, batch="b1")
        raw = set(self._consolidated_raw(n=2, age_days=60))
        got = {r[records.RECORD_ID_KEY] for r in emit.earned_targets()}
        self.assertEqual(got, {dup} | raw)

    def test_a_recalled_consolidated_session_is_withheld(self):
        raw = set(self._consolidated_raw(n=1, age_days=60, session="Sx", batch="bx"))
        ep = next(r[records.RECORD_ID_KEY] for r in ledger.iter_records()
                  if r.get("kind") == records.EPISODIC_KIND and r.get("session_id") == "Sx")
        forget.record_access(ep)                                  # the session's gist (an episodic) is in active use
        got = {r[records.RECORD_ID_KEY] for r in emit.earned_targets()}
        self.assertEqual(got & raw, set())                        # the veto flows through the union

    def test_the_raw_cost_line_is_content_free_and_names_the_verbatim_cost(self):
        rid = self._consolidated_raw(n=1, age_days=60, text="zebrafluxmigration")[0]
        rec = next(r for r in ledger.iter_records() if r.get(records.RECORD_ID_KEY) == rid)
        low = emit._cost_for(rec, int(time.time())).lower()
        self.assertIn("original wording", low)                    # product-S2: erasing ends the verbatim's recovery
        self.assertIn("summary", low)                             # the curated summary stays and stands in
        self.assertIn("recoverable until erased", low)
        self.assertNotIn("zebrafluxmigration", low)               # the note's own text never leaks
        self.assertNotIn("fuel", low)                             # the retired 'fuel' coinage is not reintroduced

    def test_the_committed_proposal_and_body_carry_no_session_id(self):
        self._consolidated_raw(n=2, age_days=60, session="ZZSECRETSESSION", batch="bcr")
        proposal = emit.build_proposal(emit.earned_targets())
        blob = json.dumps(proposal) + emit._pr_body(proposal)
        self.assertNotIn("ZZSECRETSESSION", blob)                 # arch-S1 / risk-N2: the grouping key never leaks

    def test_the_body_collapses_identical_lines_into_a_counted_row(self):
        line = "a raw turn-by-turn note the engine saved, now summarised — about a month ago; recoverable until erased."
        body = emit._pr_body({"targets": ["a" * 32] * 340, "costs": [line] * 340})
        self.assertIn("340 notes", body)                          # the per-vintage count is stated explicitly
        self.assertEqual(body.count("about a month ago"), 1)      # collapsed to ONE row, not 340 near-identical lines

    def test_the_body_states_notes_may_be_offered_again(self):
        body = emit._pr_body({"targets": ["a" * 32, "b" * 32], "costs": ["cost one.", "cost two."]}).lower()
        self.assertIn("offer these notes again", body)            # re-offer: closing is 'not now', not 'keep forever'
        self.assertNotIn("will not offer", body)                  # the old permanent-keep promise is gone

    def test_print_candidates_header_is_not_duplicate_specific(self):
        import contextlib
        import io
        self._consolidated_raw(n=1, age_days=60)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            emit._print_candidates()
        out = buf.getvalue().lower()
        self.assertIn("earned erasure", out)
        self.assertNotIn("hidden duplicate", out)                 # the header must not mislabel raw as a duplicate


class ReOfferTests(_Base):
    """Decline semantics (Shane's call): a CLOSED-unmerged (declined) erasure PR is re-offered at the next
    check; a MERGED one stays covered. A close is 'not this time', not 'keep forever' — and never fail-open."""

    @staticmethod
    def _labels_ok(method, path):
        if path.endswith(f"/labels/{obs.ERASURE_LABEL}") and method == "GET":
            return 404, None
        if "/issues/" in path and path.endswith("/labels") and method == "POST":
            return 200, []
        if path.endswith("/labels") and method == "POST":
            return 201, {}
        return None

    def test_a_declined_closed_pr_is_re_offered(self):
        rid = self._retired("an old hidden duplicate", age_days=60, batch="b1")

        def transport(method, path, body):
            lbl = self._labels_ok(method, path)
            if lbl is not None:
                return lbl
            if "state=open" in path:                              # serializer: none open (the declined one is closed)
                return 200, []
            if "/issues?" in path:                                # dedup list: one CLOSED-unmerged PR that named rid
                return 200, [{"number": 8, "pull_request": {}}]
            if "/pulls/8" in path:
                return 200, {"number": 8, "state": "closed", "merged_at": None,
                             "merge_commit_sha": None, "head": {"sha": "sha8"}}
            if "/contents/" in path and "ref=sha8" in path:
                return 200, _contents(rid)
            return 404, None

        opener = _OpenerSpy(number=71)
        result = emit.propose(opener=opener, transport=transport, root=self._tmp.name)
        self.assertEqual(result["targets"], [rid])                # re-offered, not permanently kept
        self.assertEqual(result["opened"], [71])

    def test_a_merged_pr_stays_covered(self):
        rid = self._retired("an old hidden duplicate", age_days=60, batch="b1")

        def transport(method, path, body):
            if "state=open" in path:
                return 200, []
            if "/issues?" in path:
                return 200, [{"number": 8, "pull_request": {}}]
            if "/pulls/8" in path:
                return 200, {"number": 8, "state": "closed", "merged_at": "2026-01-01T00:00:00Z",
                             "merge_commit_sha": "msha8", "head": {"sha": "sha8"}}
            if "/contents/" in path and "ref=msha8" in path:
                return 200, _contents(rid)
            return 404, None

        result = emit.propose(opener=_raise_if_called, transport=transport, root=self._tmp.name)
        self.assertEqual(result["opened"], [])                    # merged -> covered -> not re-proposed
        self.assertIn("already scheduled or proposed", result["message"])

    def test_declines_when_a_later_dedup_page_is_unreadable(self):
        # risk-N4: the open+merged dedup follows pages. If a LATER page is unreadable, `_proposed_targets` returns
        # None -> propose DECLINES (fail-safe) rather than dropping a merged-but-unenacted PR and re-proposing it.
        self._retired("an old hidden duplicate", age_days=60, batch="b1")

        def transport(method, path, body):
            if "state=open" in path:
                return 200, []
            if "state=all" in path and "&page=1" in path:
                return 200, [{"number": 8, "pull_request": {}}] * 100         # a full page -> a page 2 is fetched
            if "state=all" in path and "&page=2" in path:
                return 503, None                                              # the later page is unreadable
            return 404, None

        result = emit.propose(opener=_raise_if_called, transport=transport, root=self._tmp.name)
        self.assertEqual(result["opened"], [])
        self.assertIn("no duplicate risk", result["message"].lower())


if __name__ == "__main__":
    unittest.main()
