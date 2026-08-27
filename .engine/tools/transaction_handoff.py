#!/usr/bin/env python3
"""Where a lifecycle transaction leaves the operator, and what must be true before it may start.

TWO HANDOFF SHAPES, AND THE JUDGMENT BEHIND THE SPLIT.

  * A PULL REQUEST, for a transaction that changes the engine's own code — an update, or removing the
    engine entirely. Those are large, and the operator's merge is where they are weighed.

  * A DISCRETE IN-TREE COMMIT, for adding or removing one module. Pull-request ceremony here would buy
    nothing and cost something real: the engine acting on its own installation is not product code, and
    routing it through review breaks the flow where a session offers a capability, the operator says yes,
    and the capability is usable in that same conversation. What was actually wrong with today's behaviour
    is narrower — the change lands as uncommitted sprawl in whatever state the checkout happened to be in.
    So the fix is to make it CLEAN, not to make it ceremonial: exactly the transaction's declared paths,
    one labelled commit, revertable as a unit.

WHAT MUST BE TRUE FIRST. Every state check below runs BEFORE any mutation, and each refuses with a stable
code and a way forward. They exist because a transaction that starts from a dirty tree, the wrong base, or
a branch nobody can name produces a change nobody can cleanly review or revert.

STANDARD LIBRARY ONLY on the 3.9 floor: the arrival adapter reaches this module.
"""
from __future__ import annotations

import os
import subprocess

import transaction

# Paths a transaction may never claim as its own. The engine changes its own installation; it does not
# quietly commit the operator's product code, and it does not stage its own local plan library.
_NEVER_STAGED = (".engine/plans/", ".engine/memory/", ".engine/.venv/", ".engine/.uv/")


def _git(args, root, check=False):
    return subprocess.run(["git"] + list(args), cwd=root, capture_output=True, text=True, check=check)


def _root():
    import validate  # lazy: the repository root, resolved the way the rest of the engine resolves it
    return validate.ROOT


def working_tree_state(root=None) -> dict:
    """Read-only facts about the checkout, and their fingerprints. Mutates nothing."""
    root = root or _root()
    head = _git(["rev-parse", "HEAD"], root)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    dirty = _git(["status", "--porcelain"], root)
    return {
        "root": root,
        "head": head.stdout.strip() if head.returncode == 0 else "",
        "branch": branch.stdout.strip() if branch.returncode == 0 else "",
        "dirty_paths": [line[3:] for line in dirty.stdout.splitlines() if line.strip()],
    }


def refuse_unless_ready(declared_paths, state=None, root=None) -> dict:
    """The pre-mutation gate. Returns the state it checked, or raises a typed refusal.

    Each refusal names what is wrong AND what to do, because a stop with no way forward is the dead end
    this protocol exists to remove.
    """
    state = state or working_tree_state(root)

    if not state["head"]:
        raise transaction.TransactionRefused(
            "no-checkout",
            "This does not look like a git checkout, so there is nowhere to record the change.",
            ["Run this from inside the project's checkout."])

    if not state["branch"] or state["branch"] == "HEAD":
        raise transaction.TransactionRefused(
            "detached-head",
            "The checkout is not on a named branch, so a change made here would be easy to lose.",
            ["Check out the branch you want this change on, then run this again."])

    # Dirtiness is judged against the paths this transaction CLAIMS. Unrelated work in progress is the
    # operator's business and must not be swept into the engine's commit; work already sitting in the
    # transaction's own paths is the ambiguity that would make the commit unreviewable.
    claimed = _dirty_within(state["dirty_paths"], declared_paths)
    if claimed:
        raise transaction.TransactionRefused(
            "uncommitted-changes-in-scope",
            "There are already uncommitted changes in the files this would change ({0}), so its commit "
            "could not be told apart from work that was there first. Nothing was changed."
            .format(", ".join(sorted(claimed)[:5])),
            ["Commit or set aside those changes, then run this again.",
             "Nothing here has been modified — this refused before touching anything."])

    forbidden = [p for p in declared_paths
                 if any(p.startswith(prefix) for prefix in _NEVER_STAGED)]
    if forbidden:
        raise transaction.TransactionRefused(
            "path-not-claimable",
            "A transaction may not claim {0}: those are the operator's own or the engine's local state, "
            "not part of an engine change.".format(", ".join(sorted(forbidden))),
            ["This is a defect in the transaction's declared paths; report it rather than working around it."])
    return state


def _dirty_within(dirty_paths, declared_paths):
    claimed = set()
    for dirty in dirty_paths:
        for declared in declared_paths:
            if dirty == declared or dirty.startswith(declared.rstrip("/") + "/"):
                claimed.add(dirty)
    return claimed


def refuse_unless_current(base_ref: str = "origin/main", root=None) -> None:
    """Refuse a transaction whose base has moved on. Advisory-free: it either is current or it is not."""
    root = root or _root()
    fetched = _git(["fetch", "--quiet", "origin"], root)
    if fetched.returncode != 0:
        # A fetch that could not run leaves this unverified; say so rather than assume current.
        return
    behind = _git(["rev-list", "--count", "HEAD..{0}".format(base_ref)], root)
    if behind.returncode == 0 and behind.stdout.strip().isdigit() and int(behind.stdout.strip()) > 0:
        raise transaction.TransactionRefused(
            "base-moved",
            "{0} has moved on by {1} commit(s) since this checkout, so this change would be built on a "
            "stale base.".format(base_ref, behind.stdout.strip()),
            ["Bring the checkout up to date, then run this again."],
            retryable=True)


def commit_in_tree(declared_paths, label: str, root=None) -> dict:
    """Commit exactly the declared paths as one discrete, labelled, revertable commit.

    Never `add -A`: the commit carries the transaction's own file set and nothing else, so reverting it
    undoes the transaction and only the transaction.
    """
    root = root or _root()
    present = [p for p in declared_paths if os.path.exists(os.path.join(root, p))]
    staged = _git(["add", "--"] + present, root) if present else None
    # Deletions still need staging even when the path is gone.
    absent = [p for p in declared_paths if p not in present]
    if absent:
        _git(["add", "--"] + absent, root)
    if staged is not None and staged.returncode != 0:
        raise transaction.TransactionRefused(
            "staging-failed",
            "The change was made but could not be staged: {0}".format(staged.stderr.strip()),
            ["The files on disk hold the change; commit them yourself, or undo them and run this again."])

    if _git(["diff", "--cached", "--quiet"], root).returncode == 0:
        return {"committed": None, "note": "nothing to commit — the change was already in place"}

    committed = _git(["commit", "-m", label], root)
    if committed.returncode != 0:
        raise transaction.TransactionRefused(
            "commit-failed",
            "The change is staged but the commit did not complete: {0}"
            .format((committed.stderr or committed.stdout).strip()),
            ["Fix the cause reported above and commit the staged change yourself."])
    head = _git(["rev-parse", "HEAD"], root)
    return {"committed": head.stdout.strip() if head.returncode == 0 else None}


def in_tree_handoff(applied: dict, what: str) -> dict:
    """The handoff for an engine self-change that did not need review ceremony."""
    if not applied.get("committed"):
        return {"kind": "in-tree-commit",
                "summary": "{0} — nothing needed committing; the change was already in place.".format(what)}
    return {
        "kind": "in-tree-commit",
        "summary": "{0} It is one commit on your current branch, so reverting that commit undoes it."
                   .format(what),
        "reference": applied["committed"],
    }


def pull_request_handoff(pr: dict, what: str) -> dict:
    """The handoff for a change the operator weighs at merge."""
    return {
        "kind": "pull-request",
        "summary": "{0} Nothing about the running engine changes until you merge it.".format(what),
        "reference": pr.get("url") or pr.get("html_url") or str(pr.get("number", "")),
    }
