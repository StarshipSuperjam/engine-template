#!/usr/bin/env python3
"""Behavioral FALSIFICATION that a Build survives being killed: its execution state is durable, and a cold
session standing in the worktree finds it again without having remembered anything.

THE FAILURE THIS CLOSES. The Build snapshot used to live in the OS temporary directory, keyed to the session
that made it, and a class refused to construct anywhere else — the code embodiment of "the Build's state is
never a durable leg". It was observed to vanish across a reboot, taking a session's planning with it
(StarshipSuperjam/engine-template#1012). When that happened after review had started, EVERY coordinator
record went with it: the plan binding, the approval, the review receipts, the finding dispositions. Recovery
meant reconstructing the snapshot by hand. The snapshot now lives in the plan library beside the sealed plan
that bound it, written owner-only through the same lock and compare-and-swap the library already uses.

The current demo exercises real reserve/create/activate and retirement transactions in a disposable
library, with selectable interruptions and independent competing processes. It verifies one owner,
cold discovery, exact archive preservation, permanent lock inodes, and an old caller refusing after
replacement reuses its worktree, locator and snapshot revision. Its old temporary-file loss control
remains visible. Review/approval records are synthetic fixtures, not claims about a live Build.

Durable is still not authoritative, and this demo does not blur that: the snapshot is a record of execution,
never of what was agreed. The sealed plan remains the authority, and the cold lookup asserts the recovered
snapshot still names the plan it was bound to rather than standing in for it.

Run:  uv run --directory .engine --frozen -- python tools/demo_build_resumes_after_a_kill.py
Vary: --interrupt retire-rename --contenders 4
Its companion test (`test_build_state_store.TheKillAndResumeDemo`) runs it, so it travels with the engine as
a permanent guard in every generated repository.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import json
import multiprocessing
from unittest import mock
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_coordinator_core as core   # noqa: E402
import build_state_store                # noqa: E402  (the durable store under test)
import plan_store                       # noqa: E402

_SCHEMA = (Path(__file__).resolve().parents[1] / "schemas" / "build-state.v2.json")


def _snapshot(worktree: str) -> dict:
    """A Build snapshot with real records in it — a binding, an approval, and a review receipt. Anything
    less would let the demo pass while proving only that an empty file round-trips."""
    return {
        "schema_version": "build-state.v2", "revision": 1,
        "build": {"repository": "o/r", "pr": 4242, "base_at_bind": "0" * 40,
                  "mode": "same-session", "worktree": worktree},
        "plan": {"plan_id": "pln_0123456789ab", "sealed_digest": "sha256:" + "e" * 64,
                 "diverged_from_seal": False, "digest": "sha256:" + "d" * 64,
                 "intent_digest": "sha256:" + "c" * 64, "spec_digest": None,
                 "authorizing_issue": None, "profile": "normal", "bound_head": "a" * 40},
        "approval": {"plan_digest": "sha256:" + "d" * 64, "spec_digest": None, "depth": "thorough"},
        "reviews": {"deliverable": {
            "packet_digest": "sha256:" + "1" * 64, "referent_digest": "sha256:" + "2" * 64,
            "required_lenses": ["usability"], "installed_lenses": ["usability"],
            "reviewer_contracts": [], "receipts": [
                {"lens": "usability", "packet_digest": "sha256:" + "1" * 64, "commit": "a" * 40,
                 "finding_ids": [], "code_execution": "none"}],
            "reviewed_commit": "a" * 40, "base_commit": "0" * 40}},
        "findings": [], "checkpoint": None,
        "progress": {"current_item": "N1", "completed": []},
        "validation": None, "repair": None, "repair_rounds": [], "plan_change_escalations": [],
        "reconciles": [], "preflights": [], "pr_contract": None, "submission": "draft",
        "checkout_snapshot": None, "work": {},
    }


def _records_intact(state: dict) -> bool:
    return bool(state and state.get("approval") and state["reviews"]["deliverable"]["receipts"])


class _Interrupted(Exception):
    pass


def _reserve_contender(root, slug, state, locator, start, result):
    """Independent processes contend for one real plan record, each naming a separate locator."""
    library = plan_store.PlanLibrary(root)
    start.wait(timeout=15)
    try:
        claim = build_state_store.reserve_build(library, slug, state,
            locator=locator, consent={'gate': 'bind', 'at': '2026-09-08T00:00:00Z'})
        result.put(('reserved', claim, state))
    except core.CoordinatorError as exc:
        result.put(('refused', str(exc), None))


def _contend(library, slug, root, state, count):
    ctx = multiprocessing.get_context('spawn')
    start, result = ctx.Barrier(count), ctx.Queue()
    children = []
    for number in range(count):
        candidate = copy.deepcopy(state)
        candidate['build'].update(pr=4242 + number, worktree=str(root / f'worktree-{number}'))
        child = ctx.Process(target=_reserve_contender, args=(str(library.root), slug, candidate,
            str(root / f'locator-{number}.json'), start, result))
        children.append(child)
    try:
        for child in children: child.start()
        for child in children:
            child.join(20)
            if child.is_alive() or child.exitcode != 0:
                raise RuntimeError('a competing attempt did not finish cleanly within the bound')
        return [result.get(timeout=3) for _ in children]
    finally:
        for child in children:
            if child.is_alive(): child.terminate(); child.join(3)
        result.close(); result.join_thread()


@contextlib.contextmanager
def _interrupt_at(point, library, path):
    """Interrupt AFTER one real persistence boundary; no alternative storage implementation."""
    fired = []
    write, record_write, replace = core.atomic_write, library.write_build_record_locked, Path.replace
    def hit():
        fired.append(True)
        raise _Interrupted(point)
    def writing(target, *args, **kwargs):
        answer = write(target, *args, **kwargs)
        if not fired and ((point == 'snapshot' and Path(target) == path) or
                (point == 'retire-archive' and Path(target).name.startswith('retired-'))): hit()
        return answer
    def recording(slug, record):
        answer = record_write(slug, record)
        current = record['build_lease']['current']
        if not fired and ((point == 'activation' and current and current['state'] == 'active') or
                          (point == 'release' and current is None)): hit()
        return answer
    def replacing(source, destination):
        answer = replace(source, destination)
        if not fired and point == 'retire-rename' and source == path: hit()
        return answer
    with mock.patch.object(core, 'atomic_write', side_effect=writing), \
            mock.patch.object(library, 'write_build_record_locked', side_effect=recording), \
            mock.patch.object(Path, 'replace', replacing):
        yield fired


def main(argv=()) -> int:
    parser = argparse.ArgumentParser(description='Disposable, real-transaction Build recovery demo. No live library or GitHub writes.')
    parser.add_argument('--interrupt', choices=['none', 'reservation', 'snapshot', 'activation',
                        'retire-archive', 'retire-rename', 'release'], default='snapshot')
    parser.add_argument('--contenders', type=int, choices=range(2, 9), default=2, metavar='2..8')
    args = parser.parse_args(argv)
    print(f'DEMO — {args.contenders} competing attempts; interrupt after {args.interrupt}')
    print('Synthetic plan/PR fixture, real locks and transactions, temporary library only.')
    failures = []
    def check(label, passed):
        print(f"  {'PASS' if passed else 'FAIL'}: {label}")
        if not passed: failures.append(label)
    try:
        with tempfile.TemporaryDirectory(prefix='build-resume-demo-') as directory:
            root = Path(directory)
            library = plan_store.PlanLibrary(root / 'plans')
            # Shared valid fixture shape, also used by the existing program demonstrations.
            from test_plan_store import _document
            document = _document()
            slug = library.create(document)
            record = library.read_record(slug)
            seal = {'revision': 1, 'reviewed_digest': record['current']['plan_digest'],
                    'sealed_digest': record['current']['plan_digest'],
                    'build_plan_digest': record['current']['build_plan_digest'],
                    'at': '2026-09-08T00:00:00Z', 'delta_judgment': 'none'}
            library.update_record(slug, lambda r: r.update(seal=seal,
                consent=[{'gate': 'seal', 'at': seal['at']}]))
            state = _snapshot(str(root / 'worktree'))
            state['plan'].update(sealed_digest=seal['sealed_digest'], digest=seal['build_plan_digest'])
            state['approval']['plan_digest'] = seal['build_plan_digest']
            outcomes = _contend(library, slug, root, state, args.contenders)
            winners = [outcome for outcome in outcomes if outcome[0] == 'reserved']
            check('exactly one reservation; every competing attempt refused',
                  len(winners) == 1 and sum(o[0] == 'refused' for o in outcomes) == args.contenders - 1)
            if len(winners) != 1: return 1
            _, claim, state = winners[0]
            identity = build_state_store.claim_identity(claim)
            path = Path(claim['snapshot'])
            # A matching retry is explicit and preserves the original reservation and consent.
            retry = build_state_store.reserve_build(library, slug, state, locator=claim['locator'],
                consent={'gate': 'bind', 'at': '2026-09-08T00:00:01Z'})
            check('reservation retry keeps its identity and one consent event', retry == claim and
                  len([c for c in library.read_record(slug)['consent'] if c['gate'] == 'bind']) == 1)
            if args.interrupt == 'reservation': print('  interrupted session after reservation; retrying its recorded inputs')
            def finish(): return build_state_store.finish_binding(library, slug, identity, state, _SCHEMA)
            if args.interrupt in ('snapshot', 'activation'):
                with _interrupt_at(args.interrupt, library, path) as fired:
                    try: finish()
                    except _Interrupted: pass
                check('selected bind interruption was reached', bool(fired))
            finish()
            found = build_state_store.bound_snapshots(state['build']['worktree'], library=library)
            cold = build_state_store.resolve_for_worktree(state['build']['worktree'], _SCHEMA,
                                                         library=library, identity=identity)
            resumed = cold.read()
            check('cold discovery finds the canonical Build with approval and review intact',
                  found == [(slug, path)] and _records_intact(resumed) and resumed['ownership'] == identity)
            cold.mutate(lambda s: s['progress'].update(current_item='last writer'), from_revision=1)
            original = path.read_bytes()
            locks = [build_state_store._legacy_lock(library, slug), path.with_name(path.name + '.lock')]
            inodes = [lock.stat().st_ino for lock in locks]
            def retire():
                return build_state_store.retire_build(library, slug, identity, _SCHEMA,
                    reason='demo replacement', expected_revision=2)
            if args.interrupt in ('retire-archive', 'retire-rename', 'release'):
                with _interrupt_at(args.interrupt, library, path) as fired:
                    try: retire()
                    except _Interrupted: pass
                check('selected retirement interruption was reached', bool(fired))
            archive = retire()
            check('archive keeps the last writer exactly; both permanent locks retain their inodes',
                  archive.read_bytes() == original and not path.exists() and
                  [lock.stat().st_ino for lock in locks] == inodes)
            replacement = build_state_store.reserve_build(library, slug, state, locator=claim['locator'],
                consent={'gate': 'bind', 'at': '2026-09-08T00:00:02Z'})
            new = build_state_store.finish_binding(library, slug,
                build_state_store.claim_identity(replacement), state, _SCHEMA)
            stale = build_state_store.ClaimedBuildStore(library, slug, _SCHEMA, 1, identity=identity)
            refused = False
            try: stale.mutate(lambda s: s['progress'].update(current_item='stale overwrite'))
            except core.CoordinatorError: refused = True
            check('old caller refuses although worktree, locator and revision are reused', refused and
                  core.json_file(Path(replacement['snapshot'])) == new and replacement['generation'] == 2)
            # Keep the original reboot-loss negative control explicit and isolated.
            legacy = root / 'old-session-temp.json'; legacy.write_bytes(original); legacy.unlink()
            check('negative control: deleting the old temporary file loses that copy', not legacy.exists())
    except (OSError, RuntimeError, core.CoordinatorError) as exc:
        failures.append(str(exc)); print(f'  FAIL: {exc}')
    print('DEMO FAILED' if failures else 'DEMO PASSED — one owner, preserved evidence, stale writers refused.')
    print('The temporary library was removed. This models interruptions; it does not certify hardware power-loss behavior.')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
