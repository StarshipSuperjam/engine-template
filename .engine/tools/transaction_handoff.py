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
code and a way forward. They exist because a transaction that starts from a dirty tree or a branch nobody
can name produces a change nobody can cleanly review or revert.

BASE CURRENCY, AND EXACTLY WHAT "behind-origin" DOES AND DOES NOT PROMISE. Every pull-request-shaped
transaction — an upgrade, an upgrade rollback, a whole-engine removal — checks base currency before it
mutates, through `judge_base_currency` below, and refuses `wrong-base` (HEAD is not on the repository's
default branch), `behind-origin` (that branch is strictly behind the origin ref we last fetched), or
`diverged` (it is both ahead of and behind that ref). Two things this deliberately does NOT do. It never
touches the network: it judges only against `refs/remotes/origin/*` as they already are in this checkout,
so `behind-origin` means "behind what the LAST FETCH told us", never "behind the live remote" — a base that
passes can still be stale if origin moved since that fetch, and the currency note carried in the envelope
states what it was judged against and how old that knowledge is. And it never guesses: where the default
branch cannot be resolved (`refs/remotes/origin/HEAD` absent) or the origin ref was never fetched here, it
does NOT refuse and does NOT invent a branch name — it discloses `currency-unverified` and lets the
transaction proceed, because a refusal with no route to a fresh answer would be the dead end this protocol
exists to remove. The in-tree module add/remove flow takes none of this: it commits on the operator's
current branch by the design recorded in `transaction_adapters_module`, where "wrong base" is not a
coherent idea, so it is deliberately left unwired.

STANDARD LIBRARY ONLY on the 3.9 floor: the arrival adapter reaches this module.
"""
from __future__ import annotations

import os
import re
import subprocess
import time

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


# ── Base currency ────────────────────────────────────────────────────────────────────────────────────
# Is the base this transaction would build on current with what we last knew of origin? Answered with
# LOCAL git plumbing only (rev-parse, rev-list, symbolic-ref against refs already fetched) — never a fetch,
# an ls-remote, or any other network call. See the module docstring for what that costs and why it is the
# right trade. The verdict is a plain dict the caller turns into a refusal (wrong-base / behind-origin /
# diverged) or an envelope currency note (current, with an attestation; or unverified, with a disclosure).

# The only git subcommands this check may run. The "no network" test asserts every invocation is one of
# these, so a fetch or ls-remote slipping in later fails a test rather than reaching a remote.
_CURRENCY_GIT_SUBCOMMANDS = frozenset({"rev-parse", "rev-list", "symbolic-ref"})


def _current_branch(root) -> str:
    result = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    return result.stdout.strip() if result.returncode == 0 else ""


def _default_branch(root):
    """The repository's default branch, read from refs/remotes/origin/HEAD. None when it is not set here —
    never guessed, because guessing `main` would turn an honest disclosure into a false refusal on a repo
    whose default is something else."""
    result = _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], root)
    ref = result.stdout.strip() if result.returncode == 0 else ""
    prefix = "refs/remotes/origin/"
    return ref[len(prefix):] if ref.startswith(prefix) and len(ref) > len(prefix) else None


def _ref_commit(ref, root):
    """The commit `ref` points at, or None when it does not resolve here (e.g. never fetched)."""
    result = _git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], root)
    resolved = result.stdout.strip()
    return resolved if result.returncode == 0 and resolved else None


def _ahead_behind(local_ref, remote_ref, root):
    """(ahead, behind): commits the local ref has that the remote ref does not, and vice versa. None when
    the two cannot be counted."""
    result = _git(["rev-list", "--left-right", "--count", "{0}...{1}".format(remote_ref, local_ref)], root)
    if result.returncode != 0:
        return None
    parts = result.stdout.split()
    if len(parts) != 2:
        return None
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return ahead, behind


def _humanise_age(seconds: int) -> str:
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = seconds // size
            return "last fetched {0} {1}{2} ago".format(count, unit, "s" if count != 1 else "")
    return "last fetched under a minute ago"


def _fetch_age(root) -> str:
    """How stale our knowledge of origin is, in plain words — the freshness bound on every judged verdict.
    Read from the mtime of `.git/FETCH_HEAD` (rewritten on every fetch); local only, never a remote."""
    git_dir = _git(["rev-parse", "--git-dir"], root)
    if git_dir.returncode != 0 or not git_dir.stdout.strip():
        return "fetch time unknown"
    fetch_head = os.path.join(root, git_dir.stdout.strip(), "FETCH_HEAD")
    try:
        seconds = max(0, int(time.time() - os.path.getmtime(fetch_head)))
    except OSError:
        return "no fetch recorded in this checkout"
    return _humanise_age(seconds)


def _refusal_verdict(code, explanation, next_actions) -> dict:
    return {"status": code, "refuses": True, "code": code, "explanation": explanation,
            "next_actions": list(next_actions), "currency": None}


def _note_verdict(status, currency) -> dict:
    return {"status": status, "refuses": False, "code": None, "explanation": "",
            "next_actions": [], "currency": currency}


def _unverified(reason: str) -> dict:
    return _note_verdict("unverified", {
        "verified": False,
        "note": ("Base currency was not checked: {0}. The change proceeds; whether its base is current "
                 "with origin was not established.".format(reason)),
    })


def judge_base_currency(root=None) -> dict:
    """Judge base currency with local git only. Returns a verdict dict and raises nothing.

    `status` is one of: `current` or `unverified` (both non-refusing, each carrying an envelope currency
    note), or `wrong-base` / `behind-origin` / `diverged` (refusing, each carrying a stable code, a plain
    explanation, and a remedy). No remedy ever suggests removing or re-pointing the remote — what is wrong
    is the base this is running on, never origin itself.
    """
    root = root or _root()
    default = _default_branch(root)
    if default is None:
        return _unverified("this checkout has no record of the repository's default branch "
                           "(refs/remotes/origin/HEAD is not set here), and the check never guesses one")
    branch = _current_branch(root)
    if branch != default:
        # `git rev-parse --abbrev-ref HEAD` answers the literal "HEAD" on a detached HEAD (and "" only when
        # the call itself fails), so both spellings of "no branch here" map to the friendlier phrasing rather
        # than reading as a branch literally named HEAD.
        where = "a detached or unnamed HEAD" if branch in ("", "HEAD") else "{0!r}".format(branch)
        return _refusal_verdict(
            "wrong-base",
            "This is running on {0}, not the repository's default branch {1!r}, so the change it would "
            "propose would build on the wrong base. Nothing was changed.".format(where, default),
            ["Check out {0!r} and run this again.".format(default)])
    remote_ref = "refs/remotes/origin/{0}".format(default)
    origin_commit = _ref_commit(remote_ref, root)
    if origin_commit is None:
        return _unverified("this checkout has no fetched record of origin/{0} to compare against"
                           .format(default))
    counted = _ahead_behind(default, remote_ref, root)
    if counted is None:
        return _unverified("origin/{0} and this branch could not be compared".format(default))
    ahead, behind = counted
    if behind > 0 and ahead > 0:
        return _refusal_verdict(
            "diverged",
            "This branch and origin/{0} have both moved since the last fetch — {1} commit(s) here that "
            "origin does not have, {2} there this does not — so a change proposed from here would build on "
            "a base that no longer matches the line it targets. Nothing was changed.".format(
                default, ahead, behind),
            ["Reconcile this branch with origin/{0} (fetch, then rebase or merge), and run this again."
             .format(default)])
    if behind > 0:
        return _refusal_verdict(
            "behind-origin",
            "This branch is {1} commit(s) behind origin/{0} as last fetched, so a change proposed from "
            "here would build on a stale base. Nothing was changed.".format(default, behind),
            ["Update this branch to origin/{0} (fetch, then fast-forward or rebase), and run this again."
             .format(default)])
    age = _fetch_age(root)
    return _note_verdict("current", {
        "verified": True,
        "note": "Base is current with origin/{0} ({1}); judged against commit {2}.".format(
            default, age, origin_commit[:12]),
        "judged_against": {"default_branch": default, "origin_commit": origin_commit, "fetch_age": age},
    })


def refuse_if_stale_base(root=None) -> dict:
    """The pre-mutation currency gate for a pull-request-shaped transaction. Raises TransactionRefused for
    wrong-base / behind-origin / diverged; otherwise returns the envelope currency note (current or
    unverified) for the caller to carry into its envelope and its operator-facing text."""
    verdict = judge_base_currency(root)
    if verdict["refuses"]:
        raise transaction.TransactionRefused(
            verdict["code"], verdict["explanation"], verdict["next_actions"])
    return verdict["currency"]


def currency_summary_line(currency) -> str:
    """The plain-language currency note for operator-facing text (a handoff summary, a door's output).
    Empty string when there is nothing to say, so a caller can append it unconditionally."""
    if not currency:
        return ""
    return currency.get("note") or ""


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


# CREDENTIAL SHAPES. Deliberately narrow — a scrubber that guesses would mangle legitimate body prose,
# and a mangled body is a real cost paid against an imagined leak. These are the prefixes GitHub itself
# documents for its own credential formats, plus the Authorization header shape.
_CREDENTIAL_PREFIXES = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")
REDACTED = "[redacted]"


def redact_credential_values(text, live_token=None):
    """Strip credential-shaped values out of operator-facing text bound for a pull request.

    NOT the same job as `module_manager._redact_credentials`, and named apart from it deliberately: that
    one strips the userinfo out of a URL in git's own error output. This one strips credential VALUES out
    of text the engine composed. Both rules are applied here, because a composed body can carry either.

    Two rules, and the split matters. The STRONG one is exact: whatever the live token actually is, that
    exact string never survives — no pattern-guessing, no false positives, and it holds for a credential
    format this code has never heard of. The SHAPE rules are the backstop for a credential that reached
    the text from somewhere other than the resolved token (a plan input the operator typed, a value read
    out of a release payload), where there is nothing exact to compare against.

    This is a last line, not the design. Credentials are passed as parameters and never composed into a
    body in the first place; this exists so that stops being something a reader has to take on trust.
    """
    if not text:
        return text
    result = re.sub(r"(https?://)[^/\s@]+@", r"\1***@", str(text))
    if live_token and len(str(live_token)) >= 8:
        result = result.replace(str(live_token), REDACTED)
    for prefix in _CREDENTIAL_PREFIXES:
        start = 0
        while True:
            at = result.find(prefix, start)
            if at < 0:
                break
            end = at + len(prefix)
            # A credential runs to the first character that cannot be part of one. Underscores are kept
            # because `github_pat_` tokens contain them.
            while end < len(result) and (result[end].isalnum() or result[end] == "_"):
                end += 1
            if end == at + len(prefix):
                # The bare prefix as a word ("tokens look like ghp_") — nothing secret to strip HERE.
                # Advance past it and keep scanning: this used to `break`, which abandoned the whole
                # prefix, so a real credential appearing LATER in the same body survived untouched.
                # Ordering was the entire bug, and every seeded test used one token in isolation, so
                # none of them could see it.
                start = end
                continue
            result = result[:at] + REDACTED + result[end:]
            start = at + len(REDACTED)
    return result
