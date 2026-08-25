"""What a review lens actually READ, as a range of commits — and which of those commits a human wrote.

WHY THIS EXISTS. A review receipt used to bind to one commit and one packet digest, which made it a
claim about a MOMENT rather than about a body of work. Every mechanic downstream then had to treat any
movement as total invalidation: `repair assess` cleared every recorded receipt on a re-bind, a
`sync-artifacts` commit put the repair's final commit behind HEAD and forced that re-bind, and the
round counter charged the resulting bookkeeping against the operator-escalation gate. One observed
build (StarshipSuperjam/engine-template#1063) hit all three at once and was left choosing between
re-running two cold lenses that would find nothing and discarding the evidence that they had already
run. The operator's rule out of that build: lenses run to do work; they are not ceremony
(StarshipSuperjam/engine-template#1065).

A receipt that names the RANGE it read can answer the only question that matters at a re-bind — is
there anything here this lens has not seen? — so this module homes that arithmetic:

  * `commits` turns a (base, tip) pair into the exact set of commits between them.
  * `authored_only` drops the commits the engine generated itself, so machine output cannot be
    mistaken for work a reviewer owes a read of.
  * `unread_authored` is the whole gate in one call: the authored commits inside a NEW range that a
    receipt's recorded range does not already cover. Empty means the receipt still stands.

WHAT THIS DELIBERATELY IS NOT. It never rewrites a receipt. A receipt is a fact about what a lens read,
and carrying it forward must not restamp it onto a packet the lens never saw — the finding keys hang
off those digests, and restamping them would supersede every disposition recorded against the receipt,
which is the same evidence loss by another route. So the receipt stays byte-identical and it is the
GATE'S QUESTION that changes: `build_coordinator_review.missing_receipts` takes a coverage predicate
built from this module, instead of demanding an exact packet match.

DERIVED-ARTIFACT COMMITS. A commit every one of whose paths is owned by the derived-state registry is
machine output — `sync-artifacts` generated it, no reviewer would read it, and it must not invalidate
anything. Ownership is asked of the registry itself (`derived_state.owner_of` plus the dynamic members'
concrete outputs, exactly the predicate `cmd_sync_artifacts` enforces its own commits against), never a
hand-maintained list of paths that would drift the moment a new generated surface shipped. A commit
touching NO paths (an empty commit) is not derived output and is not free: it is unclassifiable, so it
counts as authored — the fail-toward-more-review direction.
"""
from __future__ import annotations

from pathlib import Path

import build_coordinator_core as core


class RangeUnreadable(Exception):
    """A range could not be resolved in this checkout. Never silently treated as 'nothing to read'."""


def _git(root: Path, args: list[str]) -> str:
    out = core.run(["git", *args], root=root)
    if out.returncode != 0:
        raise RangeUnreadable(f"`git {' '.join(args)}` failed: {(out.stderr or '').strip()}")
    return out.stdout or ""


def commits(root: Path, base: str | None, tip: str | None) -> list[str]:
    """The commits in `base..tip`, newest first. A missing end raises rather than returning empty: an
    unreadable range is not an empty one, and the difference decides whether a lens owes a read."""
    if not base or not tip:
        raise RangeUnreadable("a commit range needs both a base and a tip")
    if base == tip:
        return []
    return [line for line in _git(root, ["rev-list", f"{base}..{tip}"]).splitlines() if line]


_AUTHORED_CACHE: dict = {}


def _classified_range(root: Path, base: str, tip: str) -> list[tuple[str, bool]]:
    """[(sha, is_derived_only)] for `base..tip`, newest first, in ONE `git log` rather than two
    subprocesses per commit.

    Cached per (root, base, tip). Callers ask the same question more than once by design — `repair assess`
    decides with it and then explains the decision with it, and the status render asks again — and every
    one of those was re-shelling the whole range. On a long branch that multiplied the cost of the very
    gate this module exists to make cheap. The cache lives for the process, which is the life of one
    command; nothing here is a daemon, and git history within one command does not change under us."""
    key = (str(root), base, tip)
    if key in _AUTHORED_CACHE:
        return _AUTHORED_CACHE[key]
    shas = commits(root, base, tip)
    if not shas:
        _AUTHORED_CACHE[key] = []
        return []
    owned = _derived_owner(root)
    # `-m --first-parent` so a merge reports the paths it actually brought in rather than nothing.
    text = _git(root, ["log", "--no-renames", "--first-parent", "-m", "--name-only",
                       "--pretty=format:%x00%H", f"{base}..{tip}"])
    seen: dict = {}
    for chunk in text.split("\x00"):
        if not chunk.strip():
            continue
        head, _, rest = chunk.partition("\n")
        sha = head.strip()
        paths = [line for line in rest.splitlines() if line.strip()]
        # A commit that touched NOTHING is not derived-only: it carries no evidence either way, and the
        # safe direction for a gate deciding whether a reviewer owes a read is to call it authored.
        seen[sha] = bool(paths) and all(owned(path) for path in paths)
    out = [(sha, seen.get(sha, False)) for sha in shas]
    _AUTHORED_CACHE[key] = out
    return out


def _paths_in(root: Path, commit: str) -> list[str]:
    """Every path this commit changed against its first parent (against the empty tree for a root
    commit, so an initial commit is classified rather than crashing)."""
    out = core.run(["git", "rev-parse", "--verify", "--quiet", commit + "^"], root=root)
    if out.returncode != 0:
        listed = _git(root, ["show", "--pretty=format:", "--name-only", commit])
    else:
        listed = _git(root, ["diff", "--name-only", f"{commit}^..{commit}"])
    return [line for line in listed.splitlines() if line]


def _derived_owner(root: Path):
    """The registry predicate for 'this path is generated output'. Imported at CALL time from the
    checkout under measurement, so a test driving a temporary repo classifies against that repo's
    registry rather than the engine's own."""
    import sys
    sys.path.insert(0, str(Path(root) / ".engine" / "tools"))
    import derived_state
    dynamic_files = {o.path for m in derived_state.MEMBERS if m.dynamic
                     for o in derived_state._concrete_outputs(m, str(root))}

    def owned(path: str) -> bool:
        return derived_state.owner_of(path) is not None or path in dynamic_files
    return owned


def is_derived_only(root: Path, commit: str, owned=None) -> bool:
    """True when every path this commit touched is generated output the engine itself produced.

    A commit that touched nothing is NOT derived-only: an empty commit carries no evidence either way,
    and the safe direction for a gate that decides whether a reviewer owes a read is to call it
    authored."""
    owned = owned or _derived_owner(root)
    paths = _paths_in(root, commit)
    return bool(paths) and all(owned(path) for path in paths)


def authored_only(root: Path, shas: list[str]) -> list[str]:
    """`shas` minus the commits that are pure derived-artifact regeneration, order preserved.

    Kept for callers holding a bare list of shas; the range-shaped callers go through
    `authored_between`, which classifies the whole span in one git invocation."""
    if not shas:
        return []
    owned = _derived_owner(root)
    return [sha for sha in shas if not is_derived_only(root, sha, owned)]


def authored_between(root: Path, base: str | None, tip: str | None) -> list[str]:
    """The authored commits in `base..tip` — the range-shaped question, answered once and cached."""
    return [sha for sha, derived in _classified_range(root, base, tip) if not derived]


def unread_authored(root: Path, read: dict | None, new_base: str | None, new_tip: str | None) -> list[str]:
    """The authored commits in `new_base..new_tip` that `read` — a recorded `{base, tip}` range — does
    not already cover.

    Empty means the lens holding this receipt has genuinely seen every piece of authored work the new
    range asks about, so its receipt still stands and re-running it would find nothing. A receipt with
    no recorded range (one written before ranges existed) covers nothing and so is never carried: an
    absent range is not a claim of coverage, and inventing one would launder an unread delta.
    """
    if not read or not read.get("base") or not read.get("tip"):
        return authored_between(root, new_base, new_tip)
    already = set(commits(root, read["base"], read["tip"]))
    return [sha for sha, derived in _classified_range(root, new_base, new_tip)
            if not derived and sha not in already]


def receipt_covers(root: Path, receipt: dict, new_base: str | None, new_tip: str | None) -> bool:
    """Whether this receipt still answers for the range `new_base..new_tip`.

    Fails CLOSED: any range this checkout cannot resolve (a garbage-collected anchor, an orphan left by
    a rewrite) means the receipt is not carried and the lens is asked again. Losing a cold review to an
    unreadable history costs a re-run; carrying one on an unverifiable claim costs the audit trail."""
    try:
        return not unread_authored(root, receipt.get("reviewed_range"), new_base, new_tip)
    except RangeUnreadable:
        return False


def coverage_report(root: Path, receipt: dict, new_base: str | None, new_tip: str | None) -> str:
    """One human line saying what this lens still owes, for the status render and the carry-forward
    refusals. Names the count and the range, because 'go re-read something' without saying what is the
    wall the whole change exists to remove."""
    try:
        unread = unread_authored(root, receipt.get("reviewed_range"), new_base, new_tip)
    except RangeUnreadable as exc:
        return f"{receipt['lens']}: coverage cannot be measured ({exc})"
    if not unread:
        return f"{receipt['lens']}: already read every authored commit in this range"
    return (f"{receipt['lens']}: {len(unread)} authored commit(s) unread"
            f" ({', '.join(sha[:12] for sha in unread[:4])}"
            f"{', …' if len(unread) > 4 else ''})")
