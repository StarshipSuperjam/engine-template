#!/usr/bin/env python3
"""Post-review checkout-integrity guard (StarshipSuperjam/engine-template#947).

A review fan-out must never mutate the git state of a checkout it did not create. Twice it has: a
`git stash` that clobbered uncommitted work, and a `git worktree add` + `git remote set-url` that
rewrote a shared checkout's `origin` (worktrees share `.git/config`) and left orphan worktree
registrations behind. The durable defence is the copy-based recipe now required in every
shell-capable review persona (agent_coherence_check.git_safety_findings). THIS tool is the mechanical
backstop that promotes the StarshipSuperjam/engine-template#935 repair sequence into a step the orchestrator runs around the review
gate: snapshot the mutation-sensitive git state before launching the passes, verify it unchanged
after.

It captures exactly what the two incidents moved:
  - the `origin` remote URL (incident 2 repointed it),
  - the registered worktree list (incident 2 left orphan registrations),
  - the current branch and HEAD OID (a review that detached HEAD or switched branch — a third
    observed mutation), and whether the stash stack grew (incident 1).

OFFLINE + READ-ONLY: it never fetches, never writes, never touches the network — it only reads local
git state, so it is safe to run against the shared checkout a peer session may be using. It reads the
same facts the online checkout-health snapshot pins (`checkout_health._checkout_snapshot`: origin URL,
current branch, HEAD OID) plus the worktree registry the sprawl detector parses
(`checkout_health.detect_product_build_sprawl`), but stays offline and checkout-agnostic so it can wrap
any path.

`verify` returns exit 3 when any captured fact moved, so a build step can fail closed. HONEST LIMITS:
this is a backstop that detects a mutation after the fact, not a lock that prevents one; a concurrent
peer session legitimately acting on the same shared checkout can also move these facts, so a flagged
change is a signal for the orchestrator to investigate, not proof of a review's fault. And it is a
before/after DELTA: a mutation fully reverted within the window (e.g. a `git stash` immediately popped
back) leaves the counts identical and is invisible here — deliberately, since a reverted change left no
lasting harm; the standing prohibition against it lives in the persona recipe, not this delta. The
shipped recipe is the prevention and the protected-branch merge gate is the real guarantee.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile


def _git(args: list, cwd: str) -> "str | None":
    """Run a read-only git command in `cwd`; return stripped stdout, or None on any failure. Never
    raises — a missing repo, a git error, or a timeout all read as None so a caller degrades to a
    fail-closed 'could not read' rather than crashing around the review gate."""
    try:
        out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                             timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _worktrees(checkout: str) -> "list | None":
    """The registered worktrees as a sorted list of [realpath, head_oid] pairs, parsed from
    `git worktree list --porcelain`. None when the listing cannot be read (fail-closed). A new or
    vanished entry between snapshots is the orphan-registration signal from incident 2."""
    listing = _git(["worktree", "list", "--porcelain"], checkout)
    if listing is None:
        return None
    entries: list = []
    path: "str | None" = None
    head = ""
    for line in listing.splitlines():
        if line.startswith("worktree "):
            if path is not None:
                entries.append([path, head])
            path = os.path.realpath(line[len("worktree "):].strip())
            head = ""
        elif line.startswith("HEAD "):
            head = line[len("HEAD "):].strip()
    if path is not None:
        entries.append([path, head])
    return sorted(entries)


def _stash_count(checkout: str) -> "int | None":
    """Number of entries on the stash stack. None when unreadable. A growth between snapshots is the
    incident-1 signal (a review that stashed the working tree)."""
    listing = _git(["stash", "list"], checkout)
    if listing is None:
        return None
    return len([ln for ln in listing.splitlines() if ln.strip()])


def snapshot(checkout: str) -> dict:
    """Capture the mutation-sensitive git state of the checkout at `checkout`. Offline, read-only.
    A field is None when its read failed; `verify` treats a None-vs-value or value-vs-None as a
    change, so an unreadable read fails closed rather than masking a mutation."""
    real = os.path.realpath(checkout)
    return {
        "checkout": real,
        "origin": _git(["remote", "get-url", "origin"], real),
        "branch": _git(["symbolic-ref", "--quiet", "--short", "HEAD"], real),
        "head": _git(["rev-parse", "--verify", "HEAD"], real),
        "stash_count": _stash_count(real),
        "worktrees": _worktrees(real),
    }


def compare(before: dict, after: dict, ignore: "set | tuple" = ()) -> list:
    """Plain-language descriptions of every mutation-sensitive fact that moved between two snapshots.
    Empty when nothing moved. Each line names what changed in terms the StarshipSuperjam/engine-template#947 incidents used, so the
    orchestrator (and the operator) can see at a glance whether a review disturbed the checkout.

    `ignore` names facts to skip — any of {"origin", "branch", "head", "stash", "worktrees"}. A caller
    that brackets a whole build (e.g. the coordinator across the review-and-repair window) ignores `head`,
    which legitimately advances with repair commits, and `worktrees`, which a concurrent peer session may
    legitimately add — leaving `origin`, `branch`, and `stash`, none of which a review ever legitimately
    changes, so the delta stays free of false positives."""
    ignore = set(ignore)
    changes: list = []
    if "origin" not in ignore and before.get("origin") != after.get("origin"):
        changes.append(f"the origin remote URL changed from {before.get('origin')!r} to "
                       f"{after.get('origin')!r} (the incident-2 repoint)")
    if "branch" not in ignore and before.get("branch") != after.get("branch"):
        changes.append(f"the checked-out branch changed from {before.get('branch')!r} to "
                       f"{after.get('branch')!r}")
    if "head" not in ignore and before.get("head") != after.get("head"):
        changes.append(f"HEAD moved from {before.get('head')!r} to {after.get('head')!r}")
    bc, ac = before.get("stash_count"), after.get("stash_count")
    if "stash" not in ignore and bc != ac:
        changes.append(f"the stash stack changed from {bc} entr{'y' if bc == 1 else 'ies'} to "
                       f"{ac} (the incident-1 stash)")
    if "worktrees" in ignore:
        return changes
    bw = {tuple(e) for e in (before.get("worktrees") or [])}
    aw = {tuple(e) for e in (after.get("worktrees") or [])}
    if (before.get("worktrees") is None) != (after.get("worktrees") is None):
        changes.append("the worktree registry became unreadable (or readable) between snapshots")
    else:
        added = sorted(p for p, _ in (aw - bw))
        removed = sorted(p for p, _ in (bw - aw))
        moved = sorted(p for p in {p for p, _ in aw} & {p for p, _ in bw}
                       if dict(before["worktrees"]).get(p) != dict(after["worktrees"]).get(p))
        if added:
            changes.append(f"new worktree registration(s): {added} (the incident-2 orphan worktrees)")
        if removed:
            changes.append(f"worktree registration(s) disappeared: {removed}")
        if moved:
            changes.append(f"a registered worktree's HEAD moved: {moved}")
    return changes


def _unreadable(snap: dict) -> bool:
    """True when every mutation-sensitive fact came back None — the checkout could not be read at all
    (git missing, corrupted repo, path gone). A real checkout always yields at least a worktree list and
    a HEAD, so all-None is a read failure, not a legitimate state."""
    return all(snap.get(k) is None for k in ("origin", "branch", "head", "stash_count", "worktrees"))


def verify(checkout: str, before: dict, ignore: "set | tuple" = ()) -> dict:
    """Re-snapshot the checkout and compare to `before`, skipping any facts in `ignore` (see compare).
    Returns {mutated, changes, before, after}. `mutated` is True when anything not ignored moved.

    Fails closed on a total read failure: if the current snapshot is entirely unreadable, that is reported
    as a mutation rather than a silent match, even against an equally-unreadable baseline — a symmetric
    read failure must never read as 'unchanged'."""
    after = snapshot(checkout)
    changes = compare(before, after, ignore=ignore)
    if not changes and _unreadable(after):
        changes = ["the checkout could not be read at verification time — failing closed"]
    return {"mutated": bool(changes), "changes": changes, "before": before, "after": after}


def _demo() -> int:
    """An operator-runnable demonstration over the REAL guard, in a throwaway git repo — nothing on
    disk outside the temp directory changes. It shows the guard staying silent when a checkout is
    untouched, and turning RED when a simulated bad review repoints origin, adds a stray worktree, and
    stashes — the exact three mutations StarshipSuperjam/engine-template#947 is about."""
    with tempfile.TemporaryDirectory() as tmp:
        main = os.path.join(tmp, "checkout")
        os.makedirs(main)
        env_setup = [
            ["init", "-q"],
            ["remote", "add", "origin", "https://github.com/acme/real.git"],
            ["config", "user.email", "demo@example.com"],
            ["config", "user.name", "demo"],
        ]
        for args in env_setup:
            _git(args, main)
        with open(os.path.join(main, "f.txt"), "w") as fh:
            fh.write("one\n")
        _git(["add", "f.txt"], main)
        _git(["commit", "-q", "-m", "one"], main)

        before = snapshot(main)
        print("A review is about to run. The engine snapshots the checkout's git state first:")
        print(f"  origin       {before['origin']}")
        print(f"  branch/HEAD  {before['branch']} @ {(before['head'] or '')[:8]}")
        print(f"  worktrees    {len(before['worktrees'] or [])}   stash entries  {before['stash_count']}\n")

        clean = verify(main, before)
        print("If the review behaves — works only in its own throwaway copy — nothing moved:")
        print(f"  -> integrity check: {'RED' if clean['mutated'] else 'all clear'} "
              f"({'; '.join(clean['changes']) if clean['changes'] else 'the checkout is untouched'})\n")

        # Simulate the StarshipSuperjam/engine-template#947 incidents: repoint origin, add a stray worktree, stash working state.
        _git(["remote", "set-url", "origin", "https://github.com/attacker/fake.git"], main)
        _git(["worktree", "add", "-q", os.path.join(tmp, "stray"), "-b", "stray"], main)
        with open(os.path.join(main, "f.txt"), "w") as fh:
            fh.write("two\n")
        _git(["stash", "-q"], main)

        bad = verify(main, before)
        print("Now suppose the review ran commands against the real checkout instead:")
        for line in bad["changes"]:
            print(f"  - {line}")
        print(f"  -> integrity check: {'RED — the review disturbed the checkout' if bad['mutated'] else 'all clear'}")

    if clean["mutated"] or not bad["mutated"]:
        print("\nDEMO UNEXPECTED: the guard did not behave as described.", file=sys.stderr)
        return 1
    return 0


def _usage() -> int:
    print("usage: review_integrity.py snapshot <checkout>\n"
          "       review_integrity.py verify <checkout> [<before-json-path>] [--ignore head,worktrees]\n"
          "         (before on stdin if the path is omitted; --ignore mirrors what a caller skips —\n"
          "          the coordinator's required gate uses --ignore head,worktrees)\n"
          "       review_integrity.py demo", file=sys.stderr)
    return 2


def main(argv: list) -> int:
    if not argv:
        return _usage()
    verb = argv[0]
    if verb == "demo":
        return _demo()
    if verb == "snapshot":
        if len(argv) < 2:
            return _usage()
        print(json.dumps(snapshot(argv[1])))
        return 0
    if verb == "verify":
        rest = argv[1:]
        ignore: set = set()
        if "--ignore" in rest:
            i = rest.index("--ignore")
            if i + 1 >= len(rest):
                return _usage()
            ignore = {tok.strip() for tok in rest[i + 1].split(",") if tok.strip()}
            rest = rest[:i] + rest[i + 2:]
        if not rest:
            return _usage()
        checkout = rest[0]
        if len(rest) >= 2:
            with open(rest[1], encoding="utf-8") as fh:
                raw = fh.read()
        else:
            raw = sys.stdin.read()
        try:
            before = json.loads(raw)
        except (ValueError, TypeError):
            print("review_integrity: could not read the 'before' snapshot JSON", file=sys.stderr)
            return 2
        result = verify(checkout, before, ignore=ignore)
        if result["mutated"]:
            print("review_integrity: the checkout's git state moved during the review pass:")
            for line in result["changes"]:
                print(f"  - {line}")
            return 3
        print("review_integrity: the checkout's git state is unchanged since before the review pass.")
        return 0
    return _usage()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
