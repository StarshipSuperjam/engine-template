#!/usr/bin/env python3
"""Operator-checkout health — detect a stranded operator checkout AND offer a lossless un-stranding fix (StarshipSuperjam/engine-template#80).

The operator checkout — the top-level project folder the operator opens — is meant to sit on
its branch with the engine files present; build runs in per-session worktrees, never in it (the
never-strand-main floor, realized in the root CLAUDE.md floor). When it is **stranded** anyway — a detached
`HEAD`, or missing engine files — this module (a) DETECTS it offline+read-only so [boot](boot.py) can surface
it, and (b) on the operator's consent, REPAIRS it. Provisioning owns this mechanism; boot
invokes the detector in its SessionStart pack and OFFERS the fix; the assistant runs the fix only when the
operator says yes. The fix is the deployed-floor never-strand-main rule's ONE sanctioned write to the operator
checkout.

Design — the operator-checkout strand:
  - From the session's worktree, resolve the main checkout (`git worktree list` — main listed FIRST — with
    `--git-common-dir` as a fallback) and read its state LOCALLY (one shared `_resolve_state`).
  - **Two binary BROKEN states, checked every boot, OFFLINE:** a detached `HEAD`; missing engine files
    (`.claude/settings.json`, `.engine/`) — `detect_strand`. These two stay TWO.
  - **Off-main is the OFFLINE Stage-1 signal** — `detect_off_main` (StarshipSuperjam/engine-template#342). A healthy checkout PARKED on a
    non-default branch (the wrong-branch park) is caught on day one, before anything is even missing — the
    cheap-to-fix window. It fires only when the default branch is KNOWN with confidence (the persisted name or
    `origin/HEAD`), never on a heuristic guess, so a pre-persistence checkout raises no false standing nag.
  - **Behind-the-main-line is one ONLINE snapshot** — `detect_behind_origin` (StarshipSuperjam/engine-template#335, widened branch-agnostic for
    StarshipSuperjam/engine-template#342). Any upstream commit the checkout lacks is real drift, including squash/rebase/direct commits, and is
    surfaced whether the checkout is on the default branch OR parked on a side branch. Merge velocity controls
    only presentation: ordinary drift is a calm notice; more than roughly one active day's missing merges is a
    firm warning. A tightly bounded refresh is mandatory before claiming current or offering a write. If the
    remote/default/history cannot be freshly established, the result is explicitly `unavailable` — stale refs
    never produce a false all-clear. The snapshot pins remote identity, branch, HEAD, and target OIDs; a consented
    correction revalidates all of them, merges the exact assessed target, and verifies the postcondition so a
    racing external Git operation is never misreported as success. Whether a
    side branch's work is absorbed is an advisory tone only (`git cherry`, err-gentle), never a safety gate.
  - **The strand fix is lossless-or-it-does-not-run.** Safe iff `git -C <main> rev-list HEAD --not --branches`
    empty AND `stash list` empty (repo-global — a sibling worktree's stash fails it SAFE) AND `status
    --porcelain` clean AND no git operation paused mid-flight (`_op_in_progress` — a paused `rebase -i` leaves
    the tree clean yet moving HEAD would corrupt it) — decided OFFLINE. Its ONLY git mutations are
    ADDITIVE-or-post-rescue: `checkout -b` (create a ref), `commit` onto a FRESH rescue branch (saves work),
    `checkout <branch>` (only after at-risk work is rescued), and per-path `checkout HEAD -- <absent path>`
    (restore ONLY currently-absent tracked files). It **NEVER** runs `reset` / `clean` / `checkout -f` /
    `stash drop` / `push` / any force flag. When it cannot safely tell which branch to re-attach to (or a git
    operation is paused), it **REFUSES** (no mutation) rather than guess.
  - **The corrections are `catch_up` (on the default) and `return_to_default` (off it).** `catch_up` —
    an exact-old-OID update of the NAMED default followed by exact-target materialization — is the on-default
    arm. Naming the ref prevents a concurrent checkout from advancing the wrong branch; the exact old OID makes
    ref movement fail atomically. It requires the lossless gate clean and refuses divergence. `return_to_default`
    — the off-main arm — points a checkout parked on a side branch back at its
    default and fast-forwards: returning to a NAMED branch never orphans commits (the side branch ref keeps
    them, so no rescue), it runs only when the lossless gate is clean (else BLOCKS, no mutation), and its
    `checkout <default>` is defensive (never `-f`). Every destructive token stays forbidden (the tests
    source-scan for them, and behavioral tests
    pin that `catch_up` refuses divergence and `return_to_default` blocks on a paused operation).
  - **Both corrections share a third, rescue-first arm for the first-run-strand case (`_rescue_then_reconcile`,
    StarshipSuperjam/engine-template#810).** When the ONLY obstruction is uncommitted work whose every change is ALREADY present at the verified
    target — a first-run transformation the reviewed upstream absorbed, which the plain lossless gate would
    otherwise refuse forever — the arm commits the dirty tree to a RETAINED rescue branch FIRST, re-checks
    subsumption on that commit, then advances the default and lands the target. Unlike the two gates above it
    DOES switch branches (rescue then back), so here losslessness rests on the retained rescue branch, not on
    'no branch switch'; a wrong subsumption call adopts the target while the full dirty tree stays recoverable.
    A read-only pre-check (`_dirty_subsumed`) gates entry so genuine unrelated work still blocks as a true no-op.
  - **Fail-soft, never falsely current:** local strand detection remains quiet on unreadable state because a
    stranded checkout cannot reach the protected branch. Online checkout freshness is different: refresh or
    identity failure returns `unavailable`, which boot renders calmly and explicitly.

No operator prose lives in the detectors' return values (`{"states": [...], "main": <path>}`;
`{"state": "behind", "behind_commits": N, ...}`) — boot renders the plain-language line (the leaf law keeps git verbs
off the operator surface). The fixes return a structured result the runbook/boot relay in plain words.

CLI:  python tools/checkout_health.py            # classify THIS repo's main checkout (signal or "healthy")
      python tools/checkout_health.py unstrand   # dry-run: what the strand fix WOULD do (no mutation)
      python tools/checkout_health.py unstrand --apply   # repair THIS repo's checkout (only if stranded)
      python tools/checkout_health.py offmain    # report whether the checkout is parked off its default branch
      python tools/checkout_health.py returnmain # dry-run: what pointing it back WOULD do (no mutation)
      python tools/checkout_health.py returnmain --apply --target <OID> # apply the exact target previously shown
      python tools/checkout_health.py behind     # report whether the checkout is behind origin (online)
      python tools/checkout_health.py snapshot   # machine-readable fresh snapshot for a consented correction
      python tools/checkout_health.py catchup    # dry-run: what bringing it current WOULD do (no mutation)
      python tools/checkout_health.py catchup --apply --target <OID> # apply the exact target previously shown
      python tools/checkout_health.py demo       # detection + repair walkthroughs on throwaway fixtures
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_identity  # noqa: E402  (the single recorded default_branch reader — dependency-light, no cycle)

# The engine files whose absence marks a checkout stranded (the two binary states).
_ENGINE_FILES = (os.path.join(".claude", "settings.json"), ".engine")

# The fix's rescue branch (a "safe point" in operator words) + an inline identity so the rescue commit never
# fails for lack of a configured git user on the operator's checkout.
_RESCUE_PREFIX = "engine-rescue"
_RESCUE_IDENT = ["-c", "user.email=engine@local", "-c", "user.name=engine"]

# The behind-origin tail's network refresh is TIGHTLY bounded — it runs in boot's SessionStart pack, so a
# slow/hung remote must never stall the boot card. A failed refresh is an EXPLICIT unavailable snapshot, never
# silently re-read from a stale remote-tracking ref and never rendered as "up to date".
_FETCH_TIMEOUT = 6
# How many recent merges to sample when estimating the project's merge velocity (the staleness bar is
# velocity-relative). A window by COUNT, normalised by the date SPAN of those merges — data-relative, never
# the wall clock, so the bar is deterministic and testable.
_VELOCITY_SAMPLE = 50

# Upgrade transactions live in Git's per-worktree metadata, not in the checked-out tree. A killed process can
# therefore be detected by the next invocation even when the working copy is half-overlaid. The ref name is
# deliberately stable: a ref without its journal, or a journal without its ref, is a loud corrupt transaction
# rather than an older attempt being silently shadowed by a new one.
_UPGRADE_TX_JOURNAL = "engine-upgrade-transaction.json"
_UPGRADE_TX_REF = "refs/engine/upgrade-recovery"
_UPGRADE_TX_SCHEMA = "engine-upgrade-transaction.v1"
_GIT_OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _run(cmd: list, cwd: str | None = None, timeout: int = 30) -> str | None:
    """Run a local git command and return raw stdout, or None on any non-zero / failure. Never raises — every
    read is best-effort. Stdout is UNSTRIPPED so `--porcelain` stanza structure is preserved."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd)
        return out.stdout if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — a missing binary / timeout / OS error all degrade to "unavailable"
        return None


def _ok(cmd: list, cwd: str | None = None) -> bool:
    """Run a git MUTATION and report success (return code 0). Never raises. Used only for the additive /
    post-rescue operations the fix is allowed to make."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                              check=False, cwd=cwd).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _succeeds(cmd: list, cwd: str | None = None) -> bool:
    """Run a read-only predicate command whose truth is expressed by its exit status."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                              check=False, cwd=cwd).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _verified_remote_default(checkout_path: str) -> dict:
    """Read origin's authoritative HEAD symref and fetch that exact branch, returning the freshly-verified
    default branch and its advertised commit — WITHOUT rewriting the local `origin/HEAD` symref cache. Succeeds
    only when the fetched remote-tracking ref matches the advertisement, so a moved remote fails closed. This
    is the read core shared by `_refresh_origin` (which additionally rewrites the origin/HEAD cache for the
    correction path, and passes the operator's main checkout) and `fresh_default_head` (a freshness read that
    needs no cache write — from ANY session/build checkout, so a local-ref-write hiccup must not downgrade a
    genuine fresh read). Updates only remote-tracking objects/refs — never local HEAD, branches, index, or
    working tree. Returns `{"ok": True, "default", "oid"}` or `{"ok": False, "reason"}`."""
    try:
        started = time.monotonic()
        advertised = subprocess.run(["git", "-C", checkout_path, "ls-remote", "--symref", "origin", "HEAD"],
                                    capture_output=True, text=True, timeout=_FETCH_TIMEOUT, check=False)
        if advertised.returncode != 0:
            return {"ok": False, "reason": "remote-head-unreadable"}
        lines = advertised.stdout.splitlines()
        symref = next((line.split() for line in lines if line.startswith("ref: refs/heads/")), None)
        oid_line = next((line.split() for line in lines
                         if not line.startswith("ref:") and line.endswith("\tHEAD")), None)
        if not symref or len(symref) < 3 or not oid_line:
            return {"ok": False, "reason": "remote-head-unresolved"}
        default = symref[1].split("refs/heads/", 1)[1]
        advertised_oid = oid_line[0]
        remaining = _FETCH_TIMEOUT - (time.monotonic() - started)
        if remaining <= 0:
            return {"ok": False, "reason": "refresh-timeout"}
        tracking_ref = f"refs/remotes/origin/{default}"
        before_fetch = (_run(["git", "-C", checkout_path, "rev-parse", "--verify", tracking_ref]) or "").strip()
        fetched = subprocess.run(["git", "-C", checkout_path, "fetch", "--quiet", "origin",
                                  f"+refs/heads/{default}:{tracking_ref}"],
                                 capture_output=True, text=True, timeout=remaining, check=False)
        if fetched.returncode != 0:
            # Two session starts can fetch the same advertised OID concurrently. Git's ref CAS lets one win
            # and may make the peer command fail even though that winner installed the exact fresh target.
            # Wait only for that recognized ref lock to settle, then accept solely the just-advertised OID
            # with its commit object present. Every other fetch failure remains honestly unavailable.
            deadline = started + _FETCH_TIMEOUT
            lock_name = f"{tracking_ref}.lock"
            while _git_lock_is_present(checkout_path, lock_name) and time.monotonic() < deadline:
                time.sleep(0.05)
            if _git_lock_is_present(checkout_path, lock_name):
                return {"ok": False, "reason": "refresh-failed"}
            peer_actual = (_run(["git", "-C", checkout_path, "rev-parse", "--verify",
                                 tracking_ref]) or "").strip()
            peer_has_commit = _succeeds(["git", "-C", checkout_path, "cat-file", "-e",
                                         f"{advertised_oid}^{{commit}}"])
            # A cached exact ref is not evidence that this failed refresh completed. Require the ref to have
            # transitioned to the advertised OID during this fetch window; that is the peer-winner proof.
            if before_fetch == advertised_oid or peer_actual != advertised_oid or not peer_has_commit:
                return {"ok": False, "reason": "refresh-failed"}
        actual = (_run(["git", "-C", checkout_path, "rev-parse", "--verify",
                        tracking_ref]) or "").strip()
        if actual != advertised_oid:
            return {"ok": False, "reason": "remote-moved"}
        return {"ok": True, "default": default, "oid": advertised_oid}
    except Exception:  # noqa: BLE001 — timeout/offline/missing git -> an honest unavailable snapshot
        return {"ok": False, "reason": "refresh-failed"}


def _refresh_origin(main: str) -> dict:
    """Verify origin's authoritative default via `_verified_remote_default`, then rewrite the cached
    `refs/remotes/origin/HEAD` symref — a normal fetch does NOT refresh it, so trusting it would quietly follow
    an old default after a remote rename, and the correction path (`catch_up`) relies on it. Returns the
    verified default and its commit as `target_oid`, or a structured failure reason. Updates only
    remote-tracking metadata and objects — never local HEAD, branches, index, or working tree."""
    verified = _verified_remote_default(main)
    if not verified["ok"]:
        return verified
    default = verified["default"]
    if not _ok(["git", "-C", main, "symbolic-ref", "refs/remotes/origin/HEAD",
                f"refs/remotes/origin/{default}"]):
        return {"ok": False, "reason": "default-cache-write-failed"}
    return {"ok": True, "default": default, "target_oid": verified["oid"]}


def _main_checkout(cwd: str | None = None) -> tuple[str, bool] | None:
    """Resolve the operator's main checkout from this session's worktree, OFFLINE. Returns
    (main_path, is_detached) read straight from `git worktree list --porcelain` — the main worktree is
    listed FIRST by git, and its stanza carries `detached` (vs `branch refs/heads/...`), so no second git
    call is needed. Falls back to `--git-common-dir`'s parent when porcelain is unavailable. None when the
    main checkout cannot be resolved (or it is a bare repo — no working checkout to strand)."""
    porcelain = _run(["git", "worktree", "list", "--porcelain"], cwd=cwd)
    if porcelain:
        first = porcelain.split("\n\n", 1)[0]   # the first stanza is the main worktree
        path = None
        detached = False
        bare = False
        for line in first.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):].strip()
            elif line.strip() == "detached":
                detached = True
            elif line.strip() == "bare":
                bare = True
        if bare:
            return None        # a bare repo has no working checkout to strand (not an operator checkout)
        if path:
            return path, detached
    # Fallback: the common git dir's parent is the main checkout (for a normal non-bare repo, <main>/.git).
    common = _run(["git", "rev-parse", "--git-common-dir"], cwd=cwd)
    if common:
        common = common.strip()
        main = os.path.dirname(os.path.abspath(common)) if os.path.basename(
            os.path.normpath(common)) == ".git" else None
        if main:
            head = _run(["git", "-C", main, "symbolic-ref", "-q", "HEAD"])
            return main, head is None   # symbolic-ref fails (None) on a detached HEAD
    return None


def is_isolated_worktree(cwd: str | None = None) -> bool:
    """True ONLY when this session runs in a dedicated (linked) git worktree — NOT the operator's main
    checkout. The POSITIVE isolation signal the unattended Routine stance-entry requires before it grants a
    write stance: a scheduled run that mutated the operator's own checkout is the never-strand-main harm, so
    Routine writes only where isolation is PROVEN. Compares this working tree's root against the resolved
    main checkout; any inability to confirm — git absent, either query fails, a bare repo — returns False, so
    the safe floor is 'not isolated' (never merely un-disproven)."""
    top = _run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    resolved = _main_checkout(cwd)
    if not top or not resolved:
        return False
    return os.path.realpath(top.strip()) != os.path.realpath(resolved[0])


def _resolve_state(cwd: str | None = None) -> tuple[str, bool, bool, str] | None:
    """Resolve the operator's main checkout ONCE, OFFLINE, for all three classifiers (strand / off-main /
    behind) — so a single detection pass needs only one `git worktree list`. Returns
    (main, detached, missing_files, current) or None when the main checkout cannot be resolved (fail-soft
    quiet). `current` is the checked-out branch name ('' when detached). The DEFAULT branch is deliberately NOT
    resolved here: offline off-main detection uses its confident local sources, while online drift/correction
    requires the authoritative remote HEAD from the fresh snapshot."""
    resolved = _main_checkout(cwd)
    if not resolved:
        return None
    main, detached = resolved
    missing = any(not os.path.exists(os.path.join(main, rel)) for rel in _ENGINE_FILES)
    current = (_run(["git", "-C", main, "symbolic-ref", "--quiet", "--short", "HEAD"]) or "").strip()
    return main, detached, missing, current


def detect_strand(cwd: str | None = None) -> dict | None:
    """Classify the operator's main checkout as stranded or not — OFFLINE, READ-ONLY. Returns None when
    healthy (on a branch AND both engine files present) OR when the check cannot run (fail-soft quiet).
    A strand returns {"states": [...], "main": <path>} with one or both of "detached" / "missing-files"."""
    st = _resolve_state(cwd)
    if not st:
        return None
    main, detached, missing, _current = st
    states: list[str] = []
    if detached:
        states.append("detached")
    if missing:
        states.append("missing-files")
    if not states:
        return None
    return {"states": states, "main": main}


# ---- the un-stranding fix: lossless-or-it-does-not-run (issue StarshipSuperjam/engine-template#80) ------------------

# Git operation-in-progress sentinels: a PAUSED merge / cherry-pick / revert / (interactive) rebase. Probed
# via `git rev-parse --git-path` so a worktree's own git dir is honored. A paused `rebase -i` leaves
# `status --porcelain` CLEAN, so this probe — not the porcelain check — is what catches it.
_INPROGRESS_PATHS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")


def _op_in_progress(main: str) -> bool:
    """OFFLINE: is a git operation paused mid-flight in the main checkout (merge / cherry-pick / revert /
    rebase)? Load-bearing for the lossless gate: such a state can leave `status --porcelain` CLEAN, yet moving
    HEAD then would corrupt or abandon the operation. Probes the sentinel paths git itself names via
    `rev-parse --git-path` (resolved against `main`, so a linked worktree's own git dir is honored). True if ANY
    sentinel is present."""
    for rel in _INPROGRESS_PATHS:
        p = (_run(["git", "-C", main, "rev-parse", "--git-path", rel]) or "").strip()
        if not p:
            continue
        full = p if os.path.isabs(p) else os.path.join(main, p)
        if os.path.exists(full):
            return True
    return False


def _is_lossless(main: str) -> tuple[bool, list[str]]:
    """OFFLINE: can the checkout be moved (re-attached, or returned to its default branch) without first
    rescuing? Safe iff no commit sits on no branch AND no stash (repo-global — a sibling worktree's stash fails
    this SAFE) AND a clean working tree AND no git operation paused mid-flight.
    Returns (safe, reasons). Shared by `unstrand` and `return_to_default`."""
    reasons: list[str] = []
    if (_run(["git", "-C", main, "rev-list", "HEAD", "--not", "--branches"]) or "").strip():
        reasons.append("off-branch-commits")   # committed work reachable from no branch (detached work)
    if (_run(["git", "-C", main, "stash", "list"]) or "").strip():
        reasons.append("stash")
    status = _run(["git", "-C", main, "status", "--porcelain"])
    if status is None:
        reasons.append("status-unreadable")
    elif status.strip():
        reasons.append("uncommitted")
    if _op_in_progress(main):
        reasons.append("op-in-progress")       # a paused merge/rebase/cherry-pick/revert — never move HEAD
    return (not reasons), reasons


def _persisted_default_branch(main: str) -> str | None:
    """The default-branch name the instantiator derived at first run and persisted as operator config in the
    engine manifest (`<main>/.engine/engine.json`, key `default_branch` — StarshipSuperjam/engine-template#342). Read OFFLINE via the single
    recorded reader `repo_identity.default_branch`. None when absent/unreadable/malformed — the construction
    repo and any pre-persistence checkout have no such key, so the caller falls back to live resolution. The
    try/except keeps this read fail-SOFT (never raises) so the off-main classifier that anchors on it
    degrades rather than crashes on an unreadable manifest (StarshipSuperjam/engine-template#567)."""
    try:
        return repo_identity.default_branch(main)
    except Exception:  # noqa: BLE001 — preserve the swallow-all contract the off-main classifier relies on
        return None


def _confident_default_branch(main: str) -> str | None:
    """The default branch ONLY when KNOWN with confidence, resolved OFFLINE: the PERSISTED derived name first
    (validated as an existing local branch, so a stale name can never mislead), else `origin/HEAD`'s target.
    None for the heuristic last resorts (a local main/master, or the sole branch) that `_default_branch` adds —
    off-main detection uses THIS so a pre-persistence checkout with no `origin/HEAD` raises no false standing
    nag on a GUESSED default (StarshipSuperjam/engine-template#342 risk-S2)."""
    persisted = _persisted_default_branch(main)
    if persisted and _run(["git", "-C", main, "rev-parse", "--verify", "--quiet", f"refs/heads/{persisted}"]):
        return persisted   # validated: an existing local branch — safe to anchor on (gate S3)
    head = _run(["git", "-C", main, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if head and head.strip():
        ref = head.strip()
        return ref.split("origin/", 1)[1] if ref.startswith("origin/") else ref
    return None


def _default_branch(main: str) -> str | None:
    """The branch to re-attach a detached HEAD to (and the behind tail's main-line anchor), resolved OFFLINE:
    the CONFIDENT default first (persisted-validated, else origin/HEAD — see `_confident_default_branch`), then
    the heuristic last resorts a re-attach/ff can still safely use — a local main/master, else the sole local
    branch. None when it cannot be safely determined — the fix then REFUSES rather than move HEAD to a guessed
    branch."""
    confident = _confident_default_branch(main)
    if confident:
        return confident
    names = [n.strip() for n in
             (_run(["git", "-C", main, "branch", "--format=%(refname:short)"]) or "").split("\n") if n.strip()]
    for cand in ("main", "master"):
        if cand in names:
            return cand
    return names[0] if len(names) == 1 else None


def _in_head(main: str, rel: str) -> bool:
    """Is `rel` a tracked path in the current HEAD commit? (Guards the per-path re-materialize so a
    never-tracked path can't abort the whole restore — `git checkout HEAD -- a b` is all-or-nothing.)"""
    return _run(["git", "-C", main, "cat-file", "-e", f"HEAD:{rel}"]) is not None


def save_recovery_point(main: str, *, message: str) -> str | None:
    """Create a fresh rescue branch (a "safe point") at the current HEAD — capturing any off-branch commits —
    and, if the tree is dirty, commit the working changes onto it with `message`, so NOTHING at risk is left
    unsaved before HEAD moves. Returns the rescue branch name, or None if it could not be created/committed
    (the caller then refuses). Shared by the strand repair and `rollback` — each supplies its own commit
    message; the primitive (collision-safe naming, inline identity, verify-the-commit-took) is single-homed."""
    sha = (_run(["git", "-C", main, "rev-parse", "--short", "HEAD"]) or "").strip() or "head"
    name = f"{_RESCUE_PREFIX}/{sha}"
    n = 1
    while _run(["git", "-C", main, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"]) is not None:
        n += 1                                   # a re-run collided on the same sha — liveness, not safety
        name = f"{_RESCUE_PREFIX}/{sha}-{n}"
    if not _ok(["git", "-C", main, "checkout", "-b", name]):   # creates + switches; carries the dirty tree
        return None
    if (_run(["git", "-C", main, "status", "--porcelain"]) or "").strip():   # dirty -> save it on the rescue
        _ok(["git", "-C", main, "add", "-A"])
        _ok(["git", "-C", main, *_RESCUE_IDENT, "commit", "-m", message])
        if (_run(["git", "-C", main, "status", "--porcelain"]) or "").strip():
            return None   # the rescue commit did not take -> REFUSE (the work stays safe + uncommitted on
            #               this rescue branch; HEAD never moves on to the default branch) — losslessness is
            #               then self-evident, not reliant on git's later checkout-refusal as a backstop
    return name


# ---- durable tracked-content upgrade transaction -------------------------------------------------

def _tx_run(root: str, args: list[str], *, env=None, text_input: str | None = None):
    """Run one bounded Git transaction command without a shell. The caller interprets failure; this helper
    never guesses or rewrites an error into success."""
    try:
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                              input=text_input, env=env, timeout=60, check=False)
    except Exception:  # noqa: BLE001 — callers surface a typed refusal
        return None


def _tx_git_path(root: str) -> str | None:
    proc = _tx_run(root, ["rev-parse", "--git-path", _UPGRADE_TX_JOURNAL])
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return None
    path = proc.stdout.strip()
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(root, path))


def _tx_ref_oid(root: str) -> str | None:
    proc = _tx_run(root, ["rev-parse", "--verify", "--quiet", _UPGRADE_TX_REF])
    return proc.stdout.strip() if proc is not None and proc.returncode == 0 and proc.stdout.strip() else None


def _tx_safe_path(value) -> str | None:
    """Normalize one repository-relative FILE path. Git metadata and directory-wide targets are forbidden:
    a declarative migration must seal the exact files it can touch, never a recursive ambient root."""
    if not isinstance(value, str) or not value.strip() or "\0" in value or os.path.isabs(value):
        return None
    raw = value.replace("\\", "/")
    norm = os.path.normpath(raw).replace(os.sep, "/")
    if norm in ("", ".", "..") or norm.startswith("../") or norm == ".git" or norm.startswith(".git/"):
        return None
    return norm


def _tx_blob_identity(root: str, rel: str) -> str | None:
    """Identity of the working-tree bytes using Git's own blob hash, or ``absent``. A symlink hashes its
    link text, matching Git's tracked representation rather than following it outside the repository."""
    path = os.path.join(root, *rel.split("/"))
    if not os.path.lexists(path):
        return "absent"
    try:
        if os.path.islink(path):
            data = os.fsencode(os.readlink(path))
        else:
            with open(path, "rb") as fh:
                data = fh.read()
    except OSError:
        return None
    proc = subprocess.run(["git", "-C", root, "hash-object", "--stdin"], input=data, capture_output=True,
                          timeout=30, check=False)
    if proc.returncode != 0:
        return None
    return "git-blob:" + proc.stdout.decode("ascii", errors="replace").strip()


def _tx_tree_identity(root: str, commit: str, rel: str) -> str | None:
    """Identity of ``rel`` in one committed tree, or ``absent``.

    ``ls-tree -z -- <path>`` keeps hostile whitespace and path punctuation out of the parser and lets the
    compatibility adoption path record the true pre-overlay bytes even though the candidate overlay is
    already present in the working tree.  A non-blob entry is not an exact file footprint and fails closed.
    """
    proc = _tx_run(root, ["ls-tree", "-z", commit, "--", rel])
    if proc is None or proc.returncode != 0:
        return None
    entries = [entry for entry in proc.stdout.split("\0") if entry]
    if not entries:
        return "absent"
    if len(entries) != 1:
        return None
    try:
        header, path = entries[0].split("\t", 1)
        _mode, kind, oid = header.split(" ", 2)
    except ValueError:
        return None
    if path != rel or kind != "blob" or not _GIT_OID_RE.fullmatch(oid):
        return None
    return "git-blob:" + oid


def _tx_dirty_paths(root: str) -> set[str] | None:
    """All changed paths without porcelain rename parsing: staged, unstaged, then untracked. None means the
    guard could not prove the set and the transaction must refuse."""
    paths: set[str] = set()
    for args in (["diff", "--name-only", "-z"], ["diff", "--cached", "--name-only", "-z"],
                 ["ls-files", "--others", "--exclude-standard", "-z"]):
        proc = _tx_run(root, list(args))
        if proc is None or proc.returncode != 0:
            return None
        paths.update(p for p in proc.stdout.split("\0") if p)
    return paths


def _tx_fsync_dir(path: str) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def _tx_write_journal(path: str, record: dict) -> bool:
    """Atomic replace plus file and directory fsync. A JSON file that merely reached Python's buffers is not
    a restart receipt."""
    parent = os.path.dirname(path)
    try:
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".engine-upgrade-transaction-", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            return _tx_fsync_dir(parent)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except (OSError, TypeError, ValueError):
        return False


def inspect_upgrade_transaction(root: str) -> dict:
    """Read and validate the restart receipt. A one-sided or malformed journal/ref pair is ``corrupt`` with
    manual recovery evidence; it is never treated as no transaction."""
    journal_path = _tx_git_path(root)
    if not journal_path:
        return {"state": "corrupt", "code": "git-path-unavailable",
                "reason": "Git could not resolve the upgrade transaction journal path."}
    journal_exists = os.path.isfile(journal_path)
    ref_oid = _tx_ref_oid(root)
    if not journal_exists and not ref_oid:
        return {"state": "none", "journal_path": journal_path, "recovery_ref": _UPGRADE_TX_REF}
    evidence = {"journal_path": journal_path, "recovery_ref": _UPGRADE_TX_REF,
                "recovery_commit": ref_oid}
    if not journal_exists or not ref_oid:
        return {"state": "corrupt", "code": "transaction-pair-incomplete", **evidence,
                "reason": "The upgrade recovery journal and Git ref are not both present."}
    try:
        with open(journal_path, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError) as exc:
        return {"state": "corrupt", "code": "journal-unreadable", **evidence,
                "reason": f"The upgrade recovery journal is unreadable: {exc}"}
    required = {"schema_version", "phase", "original_head", "original_branch", "sealed_targets",
                "footprint", "before", "recovery_ref", "recovery_commit"}
    if not isinstance(record, dict) or record.get("schema_version") != _UPGRADE_TX_SCHEMA \
            or not required.issubset(record):
        return {"state": "corrupt", "code": "journal-malformed", **evidence,
                "reason": "The upgrade recovery journal is missing required typed fields."}
    if record.get("recovery_ref") != _UPGRADE_TX_REF or record.get("recovery_commit") != ref_oid:
        return {"state": "corrupt", "code": "recovery-ref-mismatch", **evidence,
                "reason": "The upgrade recovery journal does not name the recovery ref's commit."}
    footprint = record.get("footprint")
    before = record.get("before")
    phases = {"prepared", "mutating", "mutated", "committed", "pr-opened", "rolling-back"}
    branch_check = (_tx_run(root, ["check-ref-format", "--branch", record.get("original_branch")])
                    if isinstance(record.get("original_branch"), str) else None)
    if record.get("phase") not in phases or branch_check is None or branch_check.returncode != 0 \
            or not isinstance(record.get("original_head"), str) \
            or not _GIT_OID_RE.fullmatch(record["original_head"]):
        return {"state": "corrupt", "code": "journal-state-malformed", **evidence,
                "reason": "The upgrade recovery journal's phase or original Git position is malformed."}
    if not isinstance(footprint, list) or not isinstance(before, dict) or len(set(footprint)) != len(footprint) \
            or any(_tx_safe_path(p) != p for p in footprint) or set(before) != set(footprint):
        return {"state": "corrupt", "code": "footprint-malformed", **evidence,
                "reason": "The upgrade recovery journal's dynamic footprint is malformed."}
    if any(v != "absent" and (not isinstance(v, str) or not _GIT_OID_RE.fullmatch(v.removeprefix("git-blob:"))
                              or not v.startswith("git-blob:"))
           for v in before.values()):
        return {"state": "corrupt", "code": "before-identity-malformed", **evidence,
                "reason": "The upgrade recovery journal contains a malformed before identity."}
    targets = record.get("sealed_targets")
    if not isinstance(targets, list) or any(
            not isinstance(t, dict) or set(t) != {"path", "operation", "before_identity", "recovery_scope"}
            or t.get("path") not in before or t.get("operation") not in {"create", "replace", "delete"}
            or t.get("before_identity") != before.get(t.get("path")) or not isinstance(t.get("recovery_scope"), list)
            or t.get("path") not in t.get("recovery_scope")
            or any(p not in before for p in t.get("recovery_scope")) for t in targets):
        return {"state": "corrupt", "code": "sealed-targets-malformed", **evidence,
                "reason": "The upgrade recovery journal's sealed target set is malformed."}
    parent = _tx_run(root, ["rev-parse", f"{ref_oid}^"])
    recovery_tree = _tx_run(root, ["rev-parse", f"{ref_oid}^{{tree}}"])
    original_tree = _tx_run(root, ["rev-parse", f"{record['original_head']}^{{tree}}"])
    if any(p is None or p.returncode != 0 for p in (parent, recovery_tree, original_tree)) \
            or parent.stdout.strip() != record["original_head"] \
            or recovery_tree.stdout.strip() != original_tree.stdout.strip():
        return {"state": "corrupt", "code": "recovery-commit-malformed", **evidence,
                "reason": "The recovery ref is not an exact child snapshot of the recorded original commit."}
    return {"state": "active", "journal_path": journal_path, "recovery_ref": _UPGRADE_TX_REF,
            "record": record}


def begin_upgrade_transaction(root: str, *, sealed_targets: list[dict], footprint: list[str],
                              adopt_existing: bool = False) -> dict:
    """Seal a lossless pre-update recovery point before any upgrade mutation. Uses a TEMPORARY index to
    write a commit without touching the operator's index, anchors it at a dedicated ref, then durably writes
    the Git-path journal. Dirty work normally refuses. ``adopt_existing`` is the bounded cross-version bridge
    for a deployed parent older than this transaction protocol: the freshly-overlaid child may adopt ONLY
    already-dirty paths inside the complete dynamic footprint, while every sealed tracked-content target must
    still be pristine. Its before identities come from HEAD, not from overlaid working bytes, so recovery is
    still byte-identical to the true pre-update tree. Foreign work and dirty targets always refuse."""
    prior = inspect_upgrade_transaction(root)
    if prior.get("state") != "none":
        return {"ok": False, "code": "transaction-already-present",
                "reason": "An earlier engine update transaction still needs recovery.", "transaction": prior}
    normalized = [_tx_safe_path(p) for p in footprint]
    if any(p is None for p in normalized) or len(set(normalized)) != len(normalized):
        return {"ok": False, "code": "footprint-invalid",
                "reason": "The upgrade's dynamic rollback footprint is not a unique list of exact repository files."}
    footprint = sorted(normalized)
    head_p = _tx_run(root, ["rev-parse", "HEAD"])
    branch_p = _tx_run(root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if head_p is None or head_p.returncode != 0 or branch_p is None or branch_p.returncode != 0:
        return {"ok": False, "code": "head-unresolved",
                "reason": "Git could not resolve the current commit and branch without guessing."}
    original_head, original_branch = head_p.stdout.strip(), branch_p.stdout.strip()
    dirty = _tx_dirty_paths(root)
    if dirty is None:
        return {"ok": False, "code": "worktree-unreadable",
                "reason": "Git could not prove which working-tree paths are changed."}
    foreign = sorted(dirty - set(footprint))
    sealed_paths_declared = {t.get("path") for t in sealed_targets if isinstance(t, dict)}
    dirty_targets = sorted(dirty & sealed_paths_declared)
    if foreign or dirty_targets or (dirty and not adopt_existing):
        touched = sorted(dirty & set(footprint))
        code = "foreign-work" if foreign else "target-dirty"
        return {"ok": False, "code": code, "paths": (foreign or dirty_targets or touched)[:20],
                "reason": ("The repository has changes outside this update's sealed footprint."
                           if foreign else "A file this update would touch already has uncommitted changes.")}
    before = ({p: _tx_tree_identity(root, original_head, p) for p in footprint}
              if adopt_existing else {p: _tx_blob_identity(root, p) for p in footprint})
    if any(v is None for v in before.values()):
        return {"ok": False, "code": "before-identity-unreadable",
                "reason": "At least one file in the rollback footprint could not be identified."}
    sealed_paths = set()
    for target in sealed_targets:
        if not isinstance(target, dict) or set(target) != {
                "path", "operation", "before_identity", "recovery_scope"}:
            return {"ok": False, "code": "sealed-target-malformed",
                    "reason": "A tracked-content target is not a typed record."}
        rel = _tx_safe_path(target.get("path"))
        rec = target.get("recovery_scope")
        if not rel or rel not in before or target.get("before_identity") != before[rel] \
                or target.get("operation") not in {"create", "replace", "delete"} \
                or not isinstance(rec, list) or rel not in rec or any(p not in before for p in rec):
            return {"ok": False, "code": "sealed-target-drift", "path": target.get("path"),
                    "reason": "A tracked-content target no longer matches its preflight identity."}
        if (target["operation"] == "create") != (before[rel] == "absent"):
            return {"ok": False, "code": "sealed-operation-mismatch", "path": rel,
                    "reason": "The sealed create/replace/delete operation does not match the target's presence."}
        if rel in sealed_paths:
            return {"ok": False, "code": "sealed-target-duplicate", "path": rel,
                    "reason": "Two tracked-content declarations target the same path."}
        sealed_paths.add(rel)
    journal_path = _tx_git_path(root)
    if not journal_path:
        return {"ok": False, "code": "git-path-unavailable",
                "reason": "Git could not resolve its transaction journal path."}
    with tempfile.TemporaryDirectory(prefix="engine-upgrade-index-") as work:
        index_path = os.path.join(work, "index")
        env = {**os.environ, "GIT_INDEX_FILE": index_path}
        read = _tx_run(root, ["read-tree", original_head], env=env)
        tree = _tx_run(root, ["write-tree"], env=env) if read is not None and read.returncode == 0 else None
        if tree is None or tree.returncode != 0:
            return {"ok": False, "code": "recovery-index-failed",
                    "reason": "Git could not write the temporary-index recovery tree."}
        commit = _tx_run(root, ["-c", "user.email=engine@local", "-c", "user.name=engine",
                                "commit-tree", tree.stdout.strip(), "-p", original_head,
                                "-m", "engine: pre-upgrade recovery transaction"])
    if commit is None or commit.returncode != 0 or not commit.stdout.strip():
        return {"ok": False, "code": "recovery-commit-failed",
                "reason": "Git could not create the pre-upgrade recovery commit."}
    recovery_commit = commit.stdout.strip()
    update = _tx_run(root, ["update-ref", _UPGRADE_TX_REF, recovery_commit])
    if update is None or update.returncode != 0:
        return {"ok": False, "code": "recovery-ref-failed",
                "reason": "Git could not anchor the pre-upgrade recovery commit."}
    record = {"schema_version": _UPGRADE_TX_SCHEMA, "phase": "prepared",
              "original_head": original_head, "original_branch": original_branch,
              "sealed_targets": sealed_targets, "footprint": footprint, "before": before,
              "recovery_ref": _UPGRADE_TX_REF, "recovery_commit": recovery_commit,
              "receipts": [], "pull_request": None, "adopted_existing": bool(adopt_existing)}
    if not _tx_write_journal(journal_path, record):
        return {"ok": False, "code": "journal-write-failed", "recovery_ref": _UPGRADE_TX_REF,
                "recovery_commit": recovery_commit, "journal_path": journal_path,
                "reason": "The recovery ref exists, but its durable transaction journal could not be written."}
    return {"ok": True, "journal_path": journal_path, "recovery_ref": _UPGRADE_TX_REF,
            "recovery_commit": recovery_commit, "footprint": footprint}


def update_upgrade_transaction(root: str, phase: str, *, receipts=None, pull_request=None) -> dict:
    """Durably advance the journal. The closed phase vocabulary makes a corrupted or invented state loud."""
    if phase not in {"prepared", "mutating", "mutated", "committed", "pr-opened", "rolling-back"}:
        return {"ok": False, "code": "phase-invalid", "reason": f"Unknown transaction phase {phase!r}."}
    current = inspect_upgrade_transaction(root)
    if current.get("state") != "active":
        return {"ok": False, "code": "transaction-unavailable", "transaction": current,
                "reason": "The upgrade transaction cannot be advanced safely."}
    record = dict(current["record"])
    allowed = {"prepared": {"mutating", "rolling-back"},
               "mutating": {"mutated", "rolling-back"},
               "mutated": {"committed", "rolling-back"},
               "committed": {"pr-opened", "rolling-back"},
               "pr-opened": {"rolling-back"},
               "rolling-back": {"rolling-back"}}
    if phase != record.get("phase") and phase not in allowed.get(record.get("phase"), set()):
        return {"ok": False, "code": "phase-transition-invalid",
                "reason": f"The transaction cannot move from {record.get('phase')!r} to {phase!r}."}
    if phase == "committed" and (not isinstance(pull_request, dict)
                                  or not pull_request.get("branch") or not pull_request.get("commit")):
        return {"ok": False, "code": "commit-receipt-invalid",
                "reason": "The committed phase requires the exact upgrade branch and commit."}
    if phase == "pr-opened" and (not isinstance(pull_request, dict)
                                  or not any(pull_request.get(k) for k in ("number", "url", "html_url"))):
        return {"ok": False, "code": "pull-request-receipt-invalid",
                "reason": "The pull-request phase requires a durable pull-request identifier."}
    record["phase"] = phase
    if receipts is not None:
        record["receipts"] = receipts
    if pull_request is not None:
        record["pull_request"] = pull_request
    if not _tx_write_journal(current["journal_path"], record):
        return {"ok": False, "code": "journal-write-failed",
                "reason": "The upgrade transaction phase could not be made durable."}
    return {"ok": True, "phase": phase}


def finish_upgrade_transaction(root: str) -> dict:
    """Clear the pair only after the caller has made a durable PR state, or after verified rollback. Ref first,
    journal second: a kill between them leaves a loud one-sided pair, never a false clean state."""
    current = inspect_upgrade_transaction(root)
    if current.get("state") == "none":
        return {"ok": True, "state": "none"}
    if current.get("state") != "active":
        return {"ok": False, "code": "transaction-corrupt", "transaction": current}
    phase = current["record"].get("phase")
    if phase not in {"pr-opened", "rolling-back"} \
            or (phase == "pr-opened" and not current["record"].get("pull_request")):
        return {"ok": False, "code": "transaction-not-terminal",
                "reason": "The transaction is not durably opened for review or verified as rolled back."}
    delete = _tx_run(root, ["update-ref", "-d", _UPGRADE_TX_REF, current["record"]["recovery_commit"]])
    if delete is None or delete.returncode != 0:
        return {"ok": False, "code": "recovery-ref-delete-failed"}
    try:
        os.unlink(current["journal_path"])
        if not _tx_fsync_dir(os.path.dirname(current["journal_path"])):
            return {"ok": False, "code": "journal-directory-fsync-failed"}
    except OSError:
        return {"ok": False, "code": "journal-delete-failed"}
    return {"ok": True, "state": "cleared"}


def recover_upgrade_transaction(root: str) -> dict:
    """Restart path. A completed PR receipt is finalized; every earlier phase restores the exact dynamic
    footprint from the recovery commit, returns to the original branch, verifies byte identities, then clears
    the journal/ref. New foreign work stops recovery with manual evidence instead of being overwritten."""
    current = inspect_upgrade_transaction(root)
    if current.get("state") == "none":
        return {"ok": True, "state": "none"}
    if current.get("state") != "active":
        return {"ok": False, "state": "manual", "code": current.get("code"),
                "reason": current.get("reason"), "journal_path": current.get("journal_path"),
                "recovery_ref": current.get("recovery_ref"),
                "recovery_commit": current.get("recovery_commit")}
    record = current["record"]
    if record.get("phase") == "pr-opened" and record.get("pull_request"):
        cleared = finish_upgrade_transaction(root)
        return {**cleared, "ok": bool(cleared.get("ok")), "state": "finalized"}
    dirty = _tx_dirty_paths(root)
    if dirty is None:
        return {"ok": False, "state": "manual", "code": "worktree-unreadable",
                "reason": "Git could not prove which files changed during recovery.",
                "journal_path": current["journal_path"], "recovery_ref": _UPGRADE_TX_REF}
    foreign = sorted(dirty - set(record["footprint"]))
    if foreign:
        return {"ok": False, "state": "manual", "code": "foreign-work", "paths": foreign[:20],
                "reason": "New work exists outside the sealed rollback footprint.",
                "journal_path": current["journal_path"], "recovery_ref": _UPGRADE_TX_REF}
    advanced = update_upgrade_transaction(root, "rolling-back")
    if not advanced.get("ok"):
        return {"ok": False, "state": "manual", **advanced}
    commit = record["recovery_commit"]
    for rel in record["footprint"]:
        exists = _tx_run(root, ["cat-file", "-e", f"{commit}:{rel}"])
        if exists is not None and exists.returncode == 0:
            restore = _tx_run(root, ["restore", "--source", commit, "--staged", "--worktree", "--", rel])
            if restore is None or restore.returncode != 0:
                return {"ok": False, "state": "manual", "code": "path-restore-failed", "path": rel,
                        "reason": "Git could not restore one file from the recovery commit.",
                        "journal_path": current["journal_path"], "recovery_ref": _UPGRADE_TX_REF}
        else:
            path = os.path.join(root, *rel.split("/"))
            try:
                if os.path.islink(path) or os.path.isfile(path):
                    os.unlink(path)
                elif os.path.isdir(path):
                    return {"ok": False, "state": "manual", "code": "unexpected-directory", "path": rel,
                            "reason": "A rollback file path became a directory; it was left untouched."}
            except OSError:
                return {"ok": False, "state": "manual", "code": "path-remove-failed", "path": rel}
            staged = _tx_run(root, ["add", "-A", "--", rel])
            if staged is None or staged.returncode != 0:
                # A footprint deliberately includes release candidates that may be absent both before and
                # after. Git reports pathspec failure for such a never-indexed absent path; that is already the
                # exact desired state, not a failed restore. A path still present in the index must not receive
                # this exemption.
                indexed = _tx_run(root, ["ls-files", "--error-unmatch", "--", rel])
                if indexed is None or indexed.returncode == 0:
                    return {"ok": False, "state": "manual", "code": "path-stage-failed", "path": rel}
    for rel, expected in record["before"].items():
        if _tx_blob_identity(root, rel) != expected:
            return {"ok": False, "state": "manual", "code": "rollback-identity-mismatch", "path": rel,
                    "reason": "A restored path does not match its sealed pre-update identity."}
    branch_ref = _tx_run(root, ["rev-parse", "--verify", f"refs/heads/{record['original_branch']}"])
    if branch_ref is None or branch_ref.returncode != 0 or branch_ref.stdout.strip() != record["original_head"]:
        return {"ok": False, "state": "manual", "code": "original-branch-moved",
                "reason": "The original branch no longer points at the sealed pre-update commit.",
                "journal_path": current["journal_path"], "recovery_ref": _UPGRADE_TX_REF,
                "recovery_commit": commit}
    current_branch = _tx_run(root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if current_branch is None or current_branch.returncode != 0:
        return {"ok": False, "state": "manual", "code": "current-branch-unresolved"}
    if current_branch.stdout.strip() != record["original_branch"]:
        switch = _tx_run(root, ["checkout", record["original_branch"]])
        if switch is None or switch.returncode != 0:
            return {"ok": False, "state": "manual", "code": "original-branch-restore-failed",
                    "reason": "The files were restored, but Git could not return to the original branch.",
                    "journal_path": current["journal_path"], "recovery_ref": _UPGRADE_TX_REF}
    for rel, expected in record["before"].items():
        if _tx_blob_identity(root, rel) != expected:
            return {"ok": False, "state": "manual", "code": "post-switch-identity-mismatch", "path": rel}
    cleared = finish_upgrade_transaction(root)
    return {**cleared, "ok": bool(cleared.get("ok")), "state": "restored",
            "branch": record["original_branch"], "recovery_commit": commit}


def _make_rescue(main: str) -> str | None:
    """The strand repair's rescue: a "safe point" before un-stranding the checkout (see save_recovery_point)."""
    return save_recovery_point(main, message="engine: saved unsaved work before un-stranding the checkout")


def assess(cwd: str | None = None) -> dict:
    """OFFLINE, no mutation: resolve the strand, whether a lossless fix is possible, the re-attach branch, and
    a step plan. status ∈ healthy | needs-manual (can't resolve a branch) | fixable."""
    strand = detect_strand(cwd)
    if not strand:
        return {"status": "healthy"}
    main = strand["main"]
    detached = "detached" in strand["states"]
    missing = "missing-files" in strand["states"]
    branch = _default_branch(main) if detached else None
    if detached and not branch:
        return {"status": "needs-manual", "main": main, "reason": "no-default-branch",
                "strand": strand["states"]}
    lossless, reasons = _is_lossless(main)
    if "op-in-progress" in reasons:
        # a paused merge/rebase/cherry-pick/revert must be resolved by hand — never auto-fixed around it
        return {"status": "needs-manual", "main": main, "reason": "op-in-progress",
                "strand": strand["states"]}
    plan: list[str] = []
    if detached and not lossless:
        plan.append("rescue")          # save at-risk work before moving HEAD
    if detached:
        plan.append("reattach")
    if missing:
        plan.append("rematerialize")   # always safe — restores only absent tracked files
    return {"status": "fixable", "main": main, "branch": branch, "lossless": lossless,
            "reasons": reasons, "plan": plan, "strand": strand["states"]}


def unstrand(cwd: str | None = None, apply: bool = False) -> dict:
    """Repair a stranded operator checkout, LOSSLESS-or-rescue-then-update. Dry-run (apply=False) returns the
    plan without mutating. apply=True executes: when re-attaching is not lossless, RESCUE the at-risk work to a
    fresh branch FIRST; then re-attach the detached HEAD to its default branch; then re-materialize absent
    engine files per-path. Never loses work; REFUSES (no mutation) when it cannot safely determine the branch
    or a step is blocked. Every mutation targets `git -C <main>` — never the session's own worktree."""
    a = assess(cwd)
    if a["status"] != "fixable":
        return {**a, "applied": False}            # healthy / needs-manual: nothing to apply
    if not apply:
        return {**a, "applied": False}
    main, branch, plan = a["main"], a["branch"], a["plan"]
    did: list[str] = []
    rescue = None
    if "rescue" in plan:
        rescue = _make_rescue(main)
        if not rescue:
            return {"status": "needs-manual", "main": main, "reason": "rescue-failed",
                    "applied": False, "did": did}
        did.append(f"saved at-risk work to {rescue}")
    if "reattach" in plan:
        if not _ok(["git", "-C", main, "checkout", branch]):   # never -f; a blocked switch reports, never forces
            return {"status": "needs-manual", "main": main, "reason": "reattach-blocked",
                    "rescue": rescue, "did": did, "applied": bool(did)}
        did.append(f"re-attached to {branch}")
    if "rematerialize" in plan:
        for rel in _ENGINE_FILES:                  # per-path: a never-tracked path can't abort the others
            if not os.path.exists(os.path.join(main, rel)) and _in_head(main, rel):
                if _ok(["git", "-C", main, "checkout", "HEAD", "--", rel]):
                    did.append(f"restored {rel}")
    return {"status": "fixed", "main": main, "rescue": rescue, "did": did, "applied": True}


# ---- the off-main signal: offline Stage-1 wrong-branch park (StarshipSuperjam/engine-template#342) -------------------

def detect_off_main(cwd: str | None = None) -> dict | None:
    """OFFLINE, READ-ONLY: is the operator's main checkout PARKED on a non-default branch (the wrong-branch
    park — StarshipSuperjam/engine-template#342, the Stage-1 signal)? Returns {"state":"off-main","main","branch","main_branch"} when the
    checkout is on a branch that is NOT the default, is not detached, and is not a broken strand, AND the
    default branch is KNOWN with confidence (persisted / origin-HEAD) — else None. The confidence gate keeps a
    pre-persistence checkout with no `origin/HEAD` from raising a standing nag on a GUESSED default (risk-S2).
    `branch` is where it is parked; `main_branch` is the default it has drifted off. Offline and fires on day
    one (0-behind) — the cheap-to-fix window; *being* behind the merged main line is the separate online tail."""
    st = _resolve_state(cwd)
    if not st:
        return None
    main, detached, missing, current = st
    if detached or missing:
        return None                # a broken strand is the strand detector's territory, not this signal
    default = _confident_default_branch(main)
    if not current or not default or current == default:
        return None                # no branch / no confident default / already on the default -> not off-main
    return {"state": "off-main", "main": main, "branch": current, "main_branch": default}


# ---- the absent update-home signal: the engine can't fetch its own updates (StarshipSuperjam/engine-template#367) ----

def detect_absent_home(cwd: str | None = None) -> dict | None:
    """OFFLINE, READ-ONLY: does this engine's manifest record NO update home (`home_repository`)? A repo
    generated before that coordinate shipped carries an installed engine that cannot fetch its own updates —
    the update path refuses rather than guess a home, and never falls back to this repo's own origin
    (StarshipSuperjam/engine-template#367). Returns {"state":"absent-home","main"} when the manifest is present and readable but
    records no home, else None (no manifest / a broken strand / a home already recorded is the normal state).
    Offline by nature — telling that an update cannot be reached needs no network. boot OFFERS recording the
    home; the assistant records it on the operator's consent (the strand model)."""
    st = _resolve_state(cwd)
    if not st:
        return None
    main, detached, missing, _current = st
    if detached or missing:
        return None                # a broken strand is the strand detector's territory, not this signal
    try:
        with open(os.path.join(main, ".engine", "engine.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except Exception:  # noqa: BLE001 — no manifest / unreadable -> not an installed engine we can judge
        return None
    home = manifest.get("home_repository")
    if isinstance(home, str) and home.strip():
        return None                # a home is recorded -> the normal state
    return {"state": "absent-home", "main": main}


def recorded_product_repository(cwd: str | None = None) -> str | None:
    """OFFLINE, READ-ONLY: the engine's recorded PRODUCT repository (`product_repository` in the manifest) — the
    repo this engine builds/works ON when that is a repository DIFFERENT from the one it is deployed into (the
    fork-native / engine-mechanic case). None when no product is recorded, in which case the product IS this
    repository itself (the common self-building case) and the caller derives it live from origin rather than
    relaying a stored duplicate. A pure manifest read (the detect_absent_home idiom); it NEVER fetches from,
    executes against, or writes to the value — the coordinate is a display-only label (see engine.v1.json).
    boot RELAYS this signal; it does not read the manifest itself (its read-only relay discipline)."""
    st = _resolve_state(cwd)
    if not st:
        return None
    main, detached, missing, _current = st
    if detached or missing:
        return None                # a broken strand is the strand detector's territory, not this signal
    try:
        with open(os.path.join(main, ".engine", "engine.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except Exception:  # noqa: BLE001 — no manifest / unreadable -> nothing to relay
        return None
    product = manifest.get("product_repository")
    return product if isinstance(product, str) and product.strip() else None


# ---- the engine-mechanic executable build target (the established design): the OWNED product the mechanic delivers PRs INTO.
# Unlike recorded_product_repository (a display label), product_build_target is EXECUTABLE — a fail-closed belt
# gates every use, and the per-machine checkout path is local by nature (the slug travels on a fork; the path
# does not). The readers here are OFFLINE and READ-ONLY (fail-soft-quiet, this module's convention); the
# host-anchored belt and the write path are the mechanic build entry's — mechanic_build.py, a GUARDED,
# fail-closed gate — so this module never authorizes the cross-repo write, it only reports the facts the gate
# reads (`recorded_product_build_target`, `resolve_product_checkout`, `checkout_lossless`).

# The per-machine path to the product checkout — an env var first (the trusted, session-set seam), then a
# gitignored per-machine fallback file. NEVER committed: the slug identifies the product and travels with the
# engine, but the path names a folder on THIS computer and is each maintainer's to set once.
_PRODUCT_CHECKOUT_ENV = "ENGINE_PRODUCT_CHECKOUT"
_PRODUCT_CHECKOUT_FILE_REL = os.path.join(".engine", "mechanic", "product-checkout-path")


def recorded_product_build_target(cwd: str | None = None) -> str | None:
    """OFFLINE, READ-ONLY: the engine's recorded EXECUTABLE build target (`product_build_target` in the manifest)
    — the OWNED repository this engine-mechanic delivers pull requests into (the established design). None when absent, which
    is the normal self-building state (the engine builds its own repo and records no executable target). A pure
    manifest read (the recorded_product_repository idiom); it NEVER fetches, executes, or writes — the belt and
    the mechanic build entry are the only things that ACT on the value, and only after the fail-closed check."""
    st = _resolve_state(cwd)
    if not st:
        return None
    main, detached, missing, _current = st
    if detached or missing:
        return None
    try:
        with open(os.path.join(main, ".engine", "engine.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except Exception:  # noqa: BLE001 — no manifest / unreadable -> nothing recorded
        return None
    target = manifest.get("product_build_target")
    return target if isinstance(target, str) and target.strip() else None


def _read_checkout_path_file(cwd: str | None = None) -> str | None:
    """The per-machine product-checkout path from the gitignored fallback file (a bare single-line path), read
    from the operator's main checkout. None when absent/unreadable — the env var is the primary seam and this is
    only the convenience fallback."""
    st = _resolve_state(cwd)
    if not st:
        return None
    main = st[0]
    try:
        with open(os.path.join(main, _PRODUCT_CHECKOUT_FILE_REL), encoding="utf-8") as fh:
            path = fh.read().strip()
        return path or None
    except Exception:  # noqa: BLE001 — absent / unreadable per-machine file -> no path recorded
        return None


def _product_checkout_path(cwd: str | None = None) -> str | None:
    """The per-machine product-checkout path with NO manifest read: env `ENGINE_PRODUCT_CHECKOUT` first (the
    trusted session-set seam), then the gitignored fallback file. Stripped and `~`-expanded, or None when neither
    is set. The single place the two path seams are combined, shared by the callers that have already established
    this is a mechanic — so the manifest is read once by the caller, not again here.

    `~` is expanded because a home-relative path is the most natural thing an operator writes for a folder on
    their own machine, and every consumer hands the value to `git -C`, which does NOT expand it (a shell would):
    left raw, `~/code/product` fails as "cannot change to '~/code/product'" for a folder that plainly exists."""
    path = os.environ.get(_PRODUCT_CHECKOUT_ENV) or _read_checkout_path_file(cwd)
    if not (path and path.strip()):
        return None
    return os.path.expanduser(path.strip())


def resolve_product_checkout(cwd: str | None = None) -> tuple[str | None, str | None]:
    """Two-state resolution of the per-machine product-checkout path. Returns `(path, state)`:
      - `(None, None)` — SILENT: no `product_build_target` is recorded. The normal self-building deployment (and
        the construction repo); NOT a mechanic, so there is nothing to resolve and nothing to nag about.
      - `(path, None)` — a target IS recorded and a local path resolved (env `ENGINE_PRODUCT_CHECKOUT` first, then
        the gitignored fallback file).
      - `(None, "path-unset")` — LOUD state: a target is recorded but this machine's local checkout path is unset
        (the fork case — the committed slug travelled, the local path was never set). The caller/boot renders the
        plain-language line (this module keeps operator prose out of its return values).
    The path is inherently per-machine, so it is never committed; the slug travels, the path is local.

    Deliberately does NOT judge whether anything is AT the path — it returns a recorded path as resolved even if
    the folder is absent. That is not an oversight and must not be "harmonized" with `mechanic_orientation`'s
    `path-unreachable`: this reader feeds the BUILD entry, whose fail-closed belt then judges the checkout far
    more strictly (right origin, trusted host, clean tree) and refuses with a precise reason. Pre-judging here
    would only turn one of those precise refusals into a vaguer one. `mechanic_orientation` classifies existence
    because it feeds a session-start CARD, which must never affirm a readiness nobody checked."""
    if not recorded_product_build_target(cwd):
        return (None, None)                     # silent: not a mechanic
    path = _product_checkout_path(cwd)
    if path:
        return (path, None)
    return (None, "path-unset")                 # loud: target recorded, local path missing


def engine_common_checkout(cwd: str | None = None) -> str | None:
    """OFFLINE, READ-ONLY: the absolute path to THIS engine's DURABLE main checkout (the shared clone root),
    resolved even when the session runs from a linked (harness) worktree. It is the single answer to "where is
    the engine root" — reusing `_resolve_state`'s resolver, never a second `--git-common-dir` parse that could
    drift from it. Returns None (fail-soft QUIET) when it cannot be resolved (git absent, a bare repo, either
    query fails). The mechanic homes its per-build product worktrees under this root's
    `.engine/mechanic/worktrees/`, so a build workspace lives in the durable clone and survives the teardown of
    the harness session worktree that created it."""
    st = _resolve_state(cwd)
    return st[0] if st else None


def confident_default_branch(checkout_path: str) -> str | None:
    """OFFLINE, READ-ONLY: the default branch of the checkout at `checkout_path`, ONLY when known with
    confidence (persisted-and-validated, else `origin/HEAD`) — never a heuristic guess. None when it cannot be
    confidently determined, so a caller that cuts from `origin/<default>` fails closed rather than build off a
    guessed base. A thin public seam over `_confident_default_branch` for the mechanic build entry."""
    return _confident_default_branch(checkout_path)


def fresh_default_head(checkout_path: str) -> dict:
    """FACT SEAM (StarshipSuperjam/engine-template#957): resolve the checkout's remote default branch FRESHLY —
    read origin's authoritative HEAD symref, fetch that exact branch, and verify the fetched commit matches the
    advertisement. Returns `{"ok": True, "default": <name>, "sha": <freshly-verified commit>, "slug":
    <owner/repo of origin, or None>}`, or `{"ok": False, "reason": <why>}` when the remote cannot be read
    freshly (offline, timeout, ambiguous symref, moved remote). `sha` is the advertised OID validated against
    the fetch, so a moved remote fails closed. Unlike the correction path it does NOT depend on rewriting the
    local `origin/HEAD` cache, so a transient local-ref-write hiccup never downgrades a genuine fresh read.
    `slug` lets a caller confirm this checkout IS the repository a claim is about before trusting a read taken
    here — a mismatch must be treated as unverified, never as resolved.

    Read-only to your working tree, index, HEAD, and local branches; it DOES fetch, updating only
    remote-tracking objects/refs — the same mutation profile boot's behind-origin snapshot already performs."""
    verified = _verified_remote_default(checkout_path)
    if not verified["ok"]:
        return verified
    return {"ok": True, "default": verified["default"], "sha": verified["oid"],
            "slug": repo_identity.origin_slug(checkout_path)}


def claim_at_fresh_head(checkout_path: str, rel_path: str, still_present) -> dict:
    """FACT REPORTER for the issue-filing freshness preflight (StarshipSuperjam/engine-template#957): report
    whether a repository-state defect claim still holds at the checkout's FRESH remote default-branch commit,
    so a session never files an engine Issue for work already merged — nor suppresses a real one. This REPORTS
    facts and names no filing decision; the caller (the build-orchestration freshness rule) maps these facts to
    file / already-resolved / unverified.

    Returns, on a readable claim, `{"ok": True, "slug", "sha", "readable": True, "present_at_head": <bool>}`,
    where `present_at_head` is `still_present(content_at_head)` — the caller's claim expressed as a predicate
    over the file's content AT the pinned fresh commit (`git show <sha>:<path>`, never the moving branch ref,
    so a concurrent fetch cannot shift it). If the fresh default cannot be read, returns fresh_default_head's
    `{"ok": False, "reason"}` (no content inspected — the caller must treat this as unverified, not resolved).
    If the fresh default is read but `rel_path` is absent at that commit, returns `{"ok": True, "slug", "sha",
    "readable": False}` — the predicate is NOT consulted, so a git-show failure short-circuits before any
    possibly-empty content could reach `still_present`; the caller decides what an absent path means for its
    claim.

    BOUNDARY: covers a claim expressible as a predicate over ONE file readable as a blob at head — the common
    'a section/line is missing from an existing file' shape (the StarshipSuperjam/engine-template#911 incident).
    An absent path, or one that is not a single file (a directory), surfaces as `readable: False`; a multi-file
    or absence-premise claim ('file X was never created') the caller handles from `readable: False`.
    `still_present` is the one irreducibly defect-specific judgment; everything around it is fact. Read-only to
    your working tree/index/HEAD/local branches (it fetches remote-tracking refs, like boot)."""
    head = fresh_default_head(checkout_path)
    if not head["ok"]:
        return head
    sha = head["sha"]
    # Only a blob is a readable single file; `git show <sha>:<dir>` exits 0 with a tree listing, so gate on the
    # object type first — a directory or absent path is `readable: False`, never a synthetic listing string fed
    # to `still_present`.
    kind = _run(["git", "-C", checkout_path, "cat-file", "-t", f"{sha}:{rel_path}"])
    if kind is None or kind.strip() != "blob":
        return {"ok": True, "slug": head["slug"], "sha": sha, "readable": False}
    content = _run(["git", "-C", checkout_path, "show", f"{sha}:{rel_path}"])
    if content is None:
        return {"ok": True, "slug": head["slug"], "sha": sha, "readable": False}
    return {"ok": True, "slug": head["slug"], "sha": sha, "readable": True,
            "present_at_head": bool(still_present(content))}


# A stray build workspace with no git activity in this many days is "stale" and worth a cleanup nudge; one
# touched more recently is treated as a possibly-live session's and left alone (StarshipSuperjam/engine-template#950). A detection
# threshold, deliberately a code constant here rather than a briefing-budget dial — that policy governs the
# pack's byte-fit, not detector tuning, and blurring the two would cross the established design's boundary.
SPRAWL_STALE_DAYS = 7


def _worktree_admin_dir(wt: str) -> str | None:
    """The git admin directory backing a checkout at `wt`: its own `.git` when that is a real directory (the main
    checkout / a plain clone), else the `gitdir:` target its `.git` pointer file names (a linked worktree keeps
    its HEAD/index under `<repo>/.git/worktrees/<id>/`). None when neither is readable."""
    dotgit = os.path.join(wt, ".git")
    try:
        if os.path.isdir(dotgit):
            return dotgit
        with open(dotgit, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("gitdir:"):
                    target = stripped[len("gitdir:"):].strip()
                    return target if os.path.isabs(target) else os.path.normpath(os.path.join(wt, target))
    except OSError:
        return None
    return None


def _idle_days(wt: str, now: float) -> "int | None":
    """Whole days since the most recent git activity in the checkout at `wt` — the max mtime of its admin dir and
    that dir's HEAD/index/ORIG_HEAD. ANY git operation touches one of these, including the `git status` a live
    session runs constantly, so a workspace a session is actively using reads as fresh (idle ≈ 0); that is the
    signal, not "real work happened" — deliberately, because the question is whether a session may be USING it.
    A hint, not a fact (mtimes are coarse and trivially changed); None when nothing can be stat'd."""
    admin = _worktree_admin_dir(wt)
    if not admin:
        return None
    newest = None
    for name in ("", "HEAD", "index", "ORIG_HEAD"):
        target = admin if name == "" else os.path.join(admin, name)
        try:
            mtime = os.path.getmtime(target)
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    if newest is None:
        return None
    return max(0, int((now - newest) // 86400))


def detect_product_build_sprawl(cwd: str | None = None) -> dict | None:
    """OFFLINE, READ-ONLY: the negative control for the worktree-isolated build model (StarshipSuperjam/engine-template#902).
    Reports build workspaces of the product that are NOT the sanctioned kind — the sprawl the model exists to
    end, so a regression is CAUGHT (boot surfaces it), not just prevented. Two shapes:
      - `stray_worktrees` — worktrees of the product REGISTERED at a path OUTSIDE the mechanic's own
        `.engine/mechanic/worktrees/` (a session that cut a worktree the old way, e.g. in the product's own
        `.claude/worktrees/` or a `~/Developer` sibling);
      - `sibling_clones` — separate CLONES of the product (same `origin`) sitting beside it as `<name>-*`
        folders (the `engine-template-656-labels` sprawl the operator flagged).
    ACTIVITY-AWARE (StarshipSuperjam/engine-template#950): a stray whose git admin files were touched within `SPRAWL_STALE_DAYS` is
    treated as a possibly-live session's workspace and NOT reported (counted in `active_skipped` instead), so the
    nudge never fires on the worktrees of the operator's other open sessions. A `locked` worktree is skipped
    (deliberately parked); a `prunable` one is reported regardless of age (git itself calls it removable).
    Unpushed commits are deliberately NOT used as the staleness signal — a squash-merge leaves a merged branch
    looking unpushed forever — so that check stays a pre-DELETE safeguard, not a detection input.
    Returns `{"state":"build-sprawl","product",<stray_worktrees>,<sibling_clones>,"active_skipped"}` — each stray
    an `{"path","idle_days"}` entry — with at least one list non-empty, or None when this is not a mechanic, the
    product path is unset/absent, or nothing STALE is found (fail-soft QUIET). It never judges the shared
    checkout's BRANCH — under this model that no longer matters. Read-only: it lists, it never removes."""
    path, state = resolve_product_checkout(cwd)
    if state is not None or not path or not os.path.isdir(path):
        return None                              # not a mechanic / path unset / nothing there -> nothing to say
    product = os.path.realpath(path)
    root = engine_common_checkout(cwd)
    sanctioned = os.path.realpath(os.path.join(root, ".engine", "mechanic", "worktrees")) if root else None
    now = time.time()
    active_skipped = 0
    # Parse the porcelain into per-worktree records so `locked`/`prunable` flags (their own lines in each
    # record, blank-line separated) are visible, not just the `worktree ` path line.
    listing = _run(["git", "-C", product, "worktree", "list", "--porcelain"]) or ""
    records: list = []
    current: dict | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            current = {"path": os.path.realpath(line[len("worktree "):].strip()),
                       "locked": False, "prunable": False}
            records.append(current)
        elif current is not None and line.startswith("locked"):
            current["locked"] = True
        elif current is not None and line.startswith("prunable"):
            current["prunable"] = True
        elif not line.strip():
            current = None
    registered: set = {rec["path"] for rec in records}
    stray_worktrees: list = []
    for rec in records:
        wt = rec["path"]
        if wt == product:
            continue                             # the main worktree is the product itself — expected
        if sanctioned and (wt == sanctioned or wt.startswith(sanctioned + os.sep)):
            continue                             # a sanctioned build worktree — the whole point of the model
        if rec["locked"]:
            continue                             # deliberately parked — never a cleanup nudge
        idle = _idle_days(wt, now)
        if not rec["prunable"] and idle is not None and idle < SPRAWL_STALE_DAYS:
            active_skipped += 1                  # recent git activity: a live session may be using it
            continue
        stray_worktrees.append({"path": wt, "idle_days": idle})
    sibling_clones: list = []
    origin = _run(["git", "-C", product, "remote", "get-url", "origin"])
    origin = origin.strip() if origin and origin.strip() else None
    parent = os.path.dirname(product)
    base = os.path.basename(product)
    if origin and base and os.path.isdir(parent):
        for entry in sorted(os.listdir(parent)):
            cand = os.path.join(parent, entry)
            if entry == base or not entry.startswith(base + "-") or not os.path.isdir(cand):
                continue
            if os.path.realpath(cand) in registered:
                continue                         # a linked worktree, already counted above — not a clone
            cand_origin = _run(["git", "-C", cand, "remote", "get-url", "origin"])
            if cand_origin and cand_origin.strip() == origin:
                idle = _idle_days(cand, now)
                if idle is not None and idle < SPRAWL_STALE_DAYS:
                    active_skipped += 1          # a clone with recent activity — a live session may hold it
                    continue
                sibling_clones.append({"path": os.path.realpath(cand), "idle_days": idle})
    if not stray_worktrees and not sibling_clones:
        return None
    return {"state": "build-sprawl", "product": product,
            "stray_worktrees": stray_worktrees, "sibling_clones": sibling_clones,
            "active_skipped": active_skipped}


def mechanic_orientation(cwd: str | None = None) -> dict | None:
    """OFFLINE, READ-ONLY: the one value boot relays to orient a mechanic session — or None when this is NOT a
    mechanic (no `product_build_target` recorded), the normal self-building deployment. When it IS a mechanic:
    `{"product": <owned slug>, "checkout": <local path | None>, "state": ...}` with state one of

      - `"path-unset"`      — no local path recorded at all (the fork case: the slug travelled, the path did not);
      - `"path-unreachable"` — a path IS recorded but nothing is there (a typo, or a clone since moved/deleted);
      - `"resolved"`        — a path is recorded and a directory is there.

    The unreachable state exists so boot never makes an affirmative readiness claim it has not checked: a bare
    "a value is set" would report a typo'd path as ready to build in, and — because the setup offer is keyed off
    the unset state — would then go silent forever, leaving a mid-build refusal as the only signal. The check is a
    single `isdir`: whether the path is genuinely the right product, on a trusted origin, and safe to write in is
    NOT decided here — that is the fail-closed belt in `mechanic_build`, and this reader must never be mistaken
    for it. Fail-soft is the caller's job (boot degrades the one signal to None on error)."""
    target = recorded_product_build_target(cwd)
    if not target:
        return None
    path = _product_checkout_path(cwd)
    if not path:
        return {"product": target, "checkout": None, "state": "path-unset"}
    return {"product": target, "checkout": path,
            "state": "resolved" if os.path.isdir(path) else "path-unreachable"}


def checkout_lossless(checkout_path: str) -> tuple[bool, list[str]] | None:
    """OFFLINE, READ-ONLY: is the checkout AT `checkout_path` SAFE for the mechanic to branch and build in
    without disturbing work — on a branch (not detached), engine files present, and lossless (clean tree, no
    stash, no off-branch commits, no paused git op)? Returns `(safe, reasons)`, or None when the checkout cannot
    be resolved (fail-soft QUIET, this module's convention). This is a REPORTER, not a gate: the mechanic build
    entry (mechanic_build.resolve_build_target) makes the fail-closed decision and treats BOTH None and
    `(False, …)` as 'do not write here', so a mechanic never branches on top of the operator's unsaved work in
    their separate, real product checkout. Health is assessed for the MAIN checkout `_resolve_state` resolves
    from `checkout_path` (the product is a normal, separate clone — its own main); were the product kept in a
    linked worktree, that main is assessed, not the linked worktree at the path."""
    st = _resolve_state(checkout_path)
    if not st:
        return None
    main, detached, missing, _current = st
    reasons: list[str] = []
    if detached:
        reasons.append("detached")
    if missing:
        reasons.append("missing-files")
    _safe, loss = _is_lossless(main)
    reasons += loss
    return (not reasons, reasons)


# ---- the behind-the-main-line snapshot + fast-forward corrections (StarshipSuperjam/engine-template#335; StarshipSuperjam/engine-template#342) ----

def _days_between(a: str, b: str) -> int:
    """Whole days between two `YYYY-MM-DD` dates (git `%cs`), or 1 if either is unparseable. Data-relative —
    no wall clock — so the velocity bar is deterministic."""
    try:
        return abs((datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days)
    except Exception:  # noqa: BLE001 — a malformed/empty date degrades to the 1-day floor, never raises
        return 1


def _velocity_threshold(main: str, upstream: str) -> int:
    """The felt-consequence bar in MERGES: roughly one active day's worth of merges on `upstream`, from the
    DATE SPAN of the most recent merges (data-relative, never `--since`/the wall clock). Floor of 1 so a
    near-idle project still needs MORE THAN one missing merge before presentation becomes firm. Ordinary drift
    at the project's own pace remains visible as a calm notice."""
    dates = [d.strip() for d in (_run(["git", "-C", main, "log", "--merges", "-n", str(_VELOCITY_SAMPLE),
                                       "--format=%cs", upstream]) or "").splitlines() if d.strip()]
    if len(dates) < 2:
        return 1                                   # too little history to estimate a pace -> the floor
    span = max(1, _days_between(dates[-1], dates[0]))   # %cs is newest-first; oldest..newest of the sample
    return max(1, round(len(dates) / span))


def _merged_advisory(main: str, base: str, branch: str) -> str:
    """ADVISORY ONLY — never a safety gate: is the work on `branch` already absorbed into
    `base` (the up-to-date main line, e.g. origin/<default>)? Via `git cherry <base> <branch>`: a line starting
    '+' is a commit with NO equivalent in base, '-' one already present. Asymmetric-safe — it OVER-reports
    unfinished work (a MULTI-commit squash reads as '+' lines) and NEVER false-says 'all merged', so we return
    'merged' only when cherry is wholly clean (no '+'), and err to 'carries-work' otherwise AND on any git error.
    The surfacing reads 'carries-work' to choose the gentle keep-your-unfinished-work-safe tone."""
    out = _run(["git", "-C", main, "cherry", base, branch])
    if out is None:
        return "carries-work"                      # cherry failed (unrelated histories / error) -> gentle default
    return "carries-work" if any(ln.startswith("+") for ln in out.splitlines()) else "merged"


def _remote_default_branch(main: str) -> str | None:
    """The remote-backed default only: `origin/HEAD`, never the persisted/local heuristic fallbacks used by
    offline strand recovery. Online catch-up mutates a real checkout, so an unconfirmed default refuses rather
    than guessing which branch the remote considers primary."""
    head = _run(["git", "-C", main, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if not head or not head.strip().startswith("origin/"):
        return None
    return head.strip().split("origin/", 1)[1] or None


def _unavailable(main: str | None, reason: str) -> dict:
    return {"state": "unavailable", "main": main, "reason": reason, "fresh": False}


def _checkout_snapshot(cwd: str | None = None, *, do_fetch: bool = True) -> dict:
    """One descriptive snapshot for boot and both corrections. It separates three facts that used to be
    conflated: whether the remote was freshly readable (`state`), whether ANY upstream commit is missing
    (`behind_commits`), and whether missing MERGES exceed the project's velocity bar (`presentation`).

    The snapshot is branch-agnostic and never predicts whether a write is safe. The corrections retain
    `git merge --ff-only` as the mutation-time arbiter and revalidate the snapshot immediately before acting.
    status/state is current | behind | unavailable. A behind snapshot pins the origin URL, current branch,
    HEAD OID, default branch, remote-tracking ref, and exact target OID so consent can be bound to what was
    actually assessed rather than to a mutable branch name."""
    st = _resolve_state(cwd)
    if not st:
        return _unavailable(None, "checkout-unresolved")
    main, detached, missing_files, current = st
    if detached or missing_files:
        return _unavailable(main, "broken-strand")  # the strand detector owns the repair
    if not do_fetch:
        return _unavailable(main, "refresh-skipped")

    origin_before = (_run(["git", "-C", main, "remote", "get-url", "origin"]) or "").strip()
    if not origin_before:
        return _unavailable(main, "origin-unresolved")
    remote_head = _refresh_origin(main)
    if not remote_head or not remote_head.get("ok"):
        return _unavailable(main, (remote_head or {}).get("reason", "refresh-failed"))
    origin_after = (_run(["git", "-C", main, "remote", "get-url", "origin"]) or "").strip()
    if origin_after != origin_before:
        return _unavailable(main, "origin-changed")

    default, advertised_oid = remote_head["default"], remote_head["target_oid"]
    if _remote_default_branch(main) != default:
        return _unavailable(main, "default-unresolved")
    upstream = f"refs/remotes/origin/{default}"
    target_oid = (_run(["git", "-C", main, "rev-parse", "--verify", upstream]) or "").strip()
    head_oid = (_run(["git", "-C", main, "rev-parse", "--verify", "HEAD"]) or "").strip()
    default_oid = (_run(["git", "-C", main, "rev-parse", "--verify", f"refs/heads/{default}"]) or "").strip()
    if not target_oid or target_oid != advertised_oid or not head_oid or not default_oid:
        return _unavailable(main, "upstream-unresolved")

    behind_raw = _run(["git", "-C", main, "rev-list", "--count", f"{head_oid}..{target_oid}"])
    merges_raw = _run(["git", "-C", main, "rev-list", "--merges", "--count",
                       f"{head_oid}..{target_oid}"])
    try:
        behind_commits = int((behind_raw or "").strip())
        missing_merges = int((merges_raw or "").strip())
    except (TypeError, ValueError):
        return _unavailable(main, "history-unreadable")

    base = {"main": main, "branch": default, "current": current, "on_default": current == default,
            "origin": origin_before, "upstream": upstream, "head_oid": head_oid,
            "default_oid": default_oid, "target_oid": target_oid, "behind_commits": behind_commits,
            "missing_merges": missing_merges, "fresh": True}
    if behind_commits == 0:
        return {**base, "state": "current", "presentation": "current"}

    presentation = "warning" if missing_merges > _velocity_threshold(main, upstream) else "notice"
    latest = (_run(["git", "-C", main, "log", "-1", "--format=%cs",
                    f"{head_oid}..{target_oid}"]) or "").strip()
    return {**base, "state": "behind", "presentation": presentation, "latest": latest,
            "advisory": _merged_advisory(main, target_oid, current)}


def detect_behind_origin(cwd: str | None = None, *, do_fetch: bool = True) -> dict | None:
    """ONLINE, READ-ONLY operator-checkout signal. Returns a complete `behind` snapshot for ANY missing
    upstream commit, an explicit `unavailable` snapshot when freshness cannot be established, and None only
    when the freshly-read checkout is current. Boot relays the snapshot unchanged; `presentation` decides calm
    notice versus firm warning without changing the underlying behind fact."""
    snapshot = checkout_snapshot(cwd, do_fetch=do_fetch)
    if snapshot.get("reason") == "broken-strand":
        return None                         # the strand detector owns this louder, actionable state
    return None if snapshot.get("state") == "current" else snapshot


def checkout_snapshot(cwd: str | None = None, *, do_fetch: bool = True) -> dict:
    """Public one-read checkout-health snapshot for boot and consent routing. Unlike the behind-only adapter,
    this preserves a freshly-current snapshot because `on_default` is still needed to derive off-main state
    from the same authoritative remote default."""
    return _checkout_snapshot(cwd, do_fetch=do_fetch)


def _snapshot_unchanged(snapshot: dict) -> bool:
    """Apply-time consent check: repository, default, current branch, HEAD, and target must still be exactly
    the snapshot that authorized the action. Any movement visible at this last preflight refuses; no mutable ref
    is merged, and the caller separately verifies the postcondition after Git returns."""
    main = snapshot["main"]
    reads = {
        "origin": (_run(["git", "-C", main, "remote", "get-url", "origin"]) or "").strip(),
        "branch": _remote_default_branch(main),
        "current": (_run(["git", "-C", main, "symbolic-ref", "--quiet", "--short", "HEAD"]) or "").strip(),
        "head_oid": (_run(["git", "-C", main, "rev-parse", "--verify", "HEAD"]) or "").strip(),
        "default_oid": (_run(["git", "-C", main, "rev-parse", "--verify",
                               f"refs/heads/{snapshot['branch']}"]) or "").strip(),
        "target_oid": (_run(["git", "-C", main, "rev-parse", "--verify", snapshot["upstream"]]) or "").strip(),
    }
    return all(reads[key] == snapshot[key] for key in reads)


def _advance_named_default(main: str, branch: str, before: str, target: str) -> bool:
    """Atomically advance the NAMED default ref from its exact assessed OID. Unlike `git merge`, this can never
    resolve a concurrently switched HEAD and fast-forward the wrong branch."""
    return _ok(["git", "-C", main, "update-ref", f"refs/heads/{branch}", target, before])


def _materialize_target(main: str, before: str, target: str) -> bool:
    """Two-tree, index-locked worktree update. Git refuses at mutation time if tracked work changed after the
    clean preflight; unlike unconditional restore, it cannot erase a late editor write."""
    return _ok(["git", "-C", main, "read-tree", "-u", "-m", before, target])


# The reconcile arm's rescue message (StarshipSuperjam/engine-template#810): a first-run transformation the reviewed upstream already absorbed.
_RECONCILE_MSG = "engine: saved your uncommitted setup changes before bringing the folder current"


def _rescue_branches(main: str) -> list[str]:
    """The `engine-rescue/*` branch names currently in `main` — used to spot a stray one a partial rescue left."""
    out = _run(["git", "-C", main, "branch", "--list", f"{_RESCUE_PREFIX}/*", "--format=%(refname:short)"])
    return [n for n in (out or "").split() if n]


def _on_branch(main: str, branch: str) -> bool:
    """True when the checkout's HEAD is the named branch (not detached, not a sibling) — the restore postcondition."""
    return (_run(["git", "-C", main, "symbolic-ref", "--quiet", "--short", "HEAD"]) or "").strip() == branch


def _dirty_subsumed(main: str, target_oid: str) -> bool:
    """OFFLINE, READ-ONLY pre-check (StarshipSuperjam/engine-template#810): are ALL of the checkout's uncommitted changes ALREADY present at the
    verified target? True only when every dirty path — tracked edits/deletes AND untracked non-ignored files —
    already matches the target's content, so bringing the folder to the target would drop nothing that is not
    already upstream. This is what tells a first-run transformation the reviewed upstream has absorbed (reconcile
    it) from genuine unrelated work (leave it untouched). It reads only the working tree and the target commit —
    never mutates, never rescues — so a False answer keeps the ordinary local-work block a TRUE no-op for the
    common case. CONSERVATIVE: any unreadable comparison returns False (block), so unrelated work is never
    disturbed on an inconclusive read."""
    if not target_oid:
        return False
    tracked_raw = _run(["git", "-C", main, "diff", "--name-only", "HEAD"])
    untracked_raw = _run(["git", "-C", main, "ls-files", "--others", "--exclude-standard"])
    if tracked_raw is None or untracked_raw is None:
        return False                                   # unreadable diff/list -> conservative: block, never guess
    tracked = [p for p in tracked_raw.splitlines() if p]
    untracked = [p for p in untracked_raw.splitlines() if p]
    if not tracked and not untracked:
        return False                                   # nothing uncommitted to reconcile
    if tracked and not _succeeds(["git", "-C", main, "diff", "--quiet", target_oid, "--", *tracked]):
        return False                                   # a tracked edit/delete diverges from the target
    for path in untracked:
        wt = (_run(["git", "-C", main, "hash-object", "--", path]) or "").strip()
        at_target = (_run(["git", "-C", main, "rev-parse", "--verify", "--quiet",
                           f"{target_oid}:{path}"]) or "").strip()
        if not wt or not at_target or wt != at_target:
            return False                               # an untracked file is absent at, or differs from, target
    return True


def _commit_subsumed(main: str, base_oid: str, rescue_ref: str, target_oid: str) -> bool:
    """OFFLINE, READ-ONLY AUTHORITATIVE gate (StarshipSuperjam/engine-template#810), evaluated on the rescue COMMIT (post `add -A`, so
    formerly-untracked files are captured with no blind spot). The transformation is the set of paths the rescue
    commit changed over the pre-rescue HEAD (`base_oid`); it is subsumed when the target already agrees on every
    one of those paths (an empty change set counts as subsumed). CONSERVATIVE on any git error (returns False):
    an unreadable name-diff is NOT the same as an empty one, so it must not be read as 'nothing changed → subsumed'."""
    named = _run(["git", "-C", main, "diff", "--name-only", base_oid, rescue_ref])
    if named is None:
        return False                                   # unreadable diff -> conservative: NOT subsumed (block)
    paths = [p for p in named.splitlines() if p]
    if not paths:
        return True
    return _succeeds(["git", "-C", main, "diff", "--quiet", target_oid, rescue_ref, "--", *paths])


def _rescue_then_reconcile(snapshot: dict, *, original_branch: str) -> dict:
    """LOSSLESS, CONSENT-BOUND reconcile of a behind checkout whose uncommitted changes are already SUBSUMED by
    the verified target (StarshipSuperjam/engine-template#810 — a first-run transformation the reviewed upstream absorbed, which the plain
    lossless gate would otherwise refuse). RESCUE-FIRST: the dirty tree is committed onto a RETAINED
    `engine-rescue/<sha>` branch BEFORE anything else, so losslessness rests on that branch, NEVER on the
    subsumption judgment — a wrong 'subsumed' call can only adopt the target while the working tree (all tracked
    edits/deletes and untracked non-ignored files, via `add -A`) stays recoverable on the rescue branch. Then,
    only if the AUTHORITATIVE post-rescue check still holds, the NAMED
    default is CAS-advanced from its exact assessed OID and the checkout returns to it at the exact target. Any
    block/failure returns HEAD to `original_branch` (the default for the on-default arm, the operator's side
    branch for the off-main arm) and rolls back any ref advance — nothing is lost (the rescue branch, and any
    named side branch, retain the work). The caller supplies its ALREADY-VALIDATED snapshot; this NEVER fetches.
    Every mutation targets `git -C <main>`; no destructive git verb is used (`checkout <branch>` is never `-f`)."""
    main, default = snapshot["main"], snapshot["branch"]
    head_oid, default_oid, target = snapshot["head_oid"], snapshot["default_oid"], snapshot["target_oid"]
    # Last-moment race re-checks (OFFLINE, no fetch): the exact assessed snapshot must still hold and the default
    # must be a strict ancestor of the target — else REFUSE with NO mutation, so the pre-rescue path stays a true
    # no-op (the caller's `_dirty_subsumed` gate already ran read-only).
    if not _snapshot_unchanged(snapshot):
        return {"status": "blocked", "reason": "checkout-changed", "main": main, "branch": default,
                "applied": False}
    if not _succeeds(["git", "-C", main, "merge-base", "--is-ancestor", default_oid, target]):
        return {"status": "blocked", "reason": "diverged", "main": main, "branch": default, "applied": False}
    before_rescues = set(_rescue_branches(main))
    rescue = save_recovery_point(main, message=_RECONCILE_MSG)
    if rescue is None:
        # save_recovery_point returns None two ways, and they must be reported differently. (A) the rescue commit
        # never landed (a rejecting commit hook, `commit.gpgsign` with no key, an index.lock): it left HEAD on a
        # stray branch at the ORIGINAL commit with the dirty tree — return HEAD to the original branch (the gate
        # excludes off-branch commits, so the tree carries back cleanly) and safe-delete the empty stray, so
        # "nothing moved" is literally true. (B) the commit DID land but the tree was dirty again afterward (a
        # post-commit hook wrote a file), so save_recovery_point refused: the work is now committed on the stray
        # branch. `git branch -d` REFUSES that branch (it carries a unique commit), which is exactly how we detect
        # case B — we keep and NAME it rather than claim "couldn't save".
        _ok(["git", "-C", main, "checkout", original_branch])
        saved = None
        for stray in set(_rescue_branches(main)) - before_rescues:
            if not _ok(["git", "-C", main, "branch", "-d", stray]):   # -d refused -> a real rescue commit landed
                saved = stray
        restored = _on_branch(main, original_branch)
        if saved is not None:
            return {"status": "blocked", "reason": "rescue-incomplete", "rescue": saved, "restored": restored,
                    "main": main, "branch": default, "applied": True}
        return {"status": "blocked", "reason": "rescue-failed", "restored": restored,
                "main": main, "branch": default, "applied": not restored}
    # From here HEAD is on the rescue branch, the working tree is CLEAN, and the default ref is still default_oid.
    # Every block below has already MUTATED (the dirty tree is committed on the retained rescue branch), so each
    # returns HEAD to `original_branch` and reports `restored` — never the peers' "left everything untouched".
    if not _commit_subsumed(main, head_oid, rescue, target):
        restored = _ok(["git", "-C", main, "checkout", original_branch]) and _on_branch(main, original_branch)
        return {"status": "blocked", "reason": "local-work", "rescue": rescue, "reconciled": False,
                "restored": restored, "main": main, "branch": default, "applied": True}
    if not _advance_named_default(main, default, default_oid, target):
        restored = _ok(["git", "-C", main, "checkout", original_branch]) and _on_branch(main, original_branch)
        return {"status": "blocked", "reason": "target-changed", "rescue": rescue,
                "restored": restored, "main": main, "branch": default, "applied": True}
    if not _ok(["git", "-C", main, "checkout", default]):
        _ok(["git", "-C", main, "update-ref", f"refs/heads/{default}", default_oid, target])   # roll back advance
        restored = _ok(["git", "-C", main, "checkout", original_branch]) and _on_branch(main, original_branch)
        return {"status": "blocked", "reason": "checkout-failed", "rescue": rescue,
                "restored": restored, "main": main, "branch": default, "applied": True}
    after = (_run(["git", "-C", main, "rev-parse", "HEAD"]) or "").strip()
    after_branch = (_run(["git", "-C", main, "symbolic-ref", "--quiet", "--short", "HEAD"]) or "").strip()
    clean = not (_run(["git", "-C", main, "status", "--porcelain"]) or "").strip()
    if after == target and after_branch == default and clean:
        return {"status": "fixed", "reconciled": True, "rescue": rescue, "main": main, "branch": default,
                "before": head_oid, "after": after, "target_oid": target, "applied": True}
    # A process raced the tiny advance->checkout window: never claim success. Stop, retain the rescue branch, and
    # report for inspection (mirrors catch_up's postcondition posture; further mutation risks a messier tree).
    return {"status": "blocked", "reason": "postcondition-failed", "rescue": rescue,
            "main": main, "branch": default, "after": after, "applied": True}


def _git_lock_path(main: str, lock_name: str) -> str | None:
    """Resolve a per-worktree Git lock path without assuming a `.git` directory layout."""
    raw = (_run(["git", "-C", main, "rev-parse", "--git-path", lock_name]) or "").strip()
    return raw if raw and os.path.isabs(raw) else os.path.join(main, raw) if raw else None


def _acquire_git_lock(main: str, lock_name: str, contents: str) -> str | None:
    """Create one Git-recognised worktree lock, declining rather than stealing another process's lock."""
    path = _git_lock_path(main, lock_name)
    if not path:
        return None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(contents)
        return path
    except OSError:
        return None


def _git_lock_is_present(main: str, lock_name: str) -> bool:
    """Whether another Git operation has already reserved this worktree mutation seam."""
    path = _git_lock_path(main, lock_name)
    return bool(path and os.path.exists(path))


def _acquire_index_lock(main: str) -> str | None:
    """Reserve the index before advancing the checked-out default branch.

    A normal branch switch needs the same per-worktree ``index.lock``.  Reserving it first closes the
    otherwise open advance->HEAD.lock interval: no checkout can begin while the named default ref moves and
    this controller obtains ``HEAD.lock`` for materialisation.  It is boot-only because the consented public
    paths retain their existing timing and interface.
    """
    return _acquire_git_lock(main, "index.lock", "Engine automatic checkout catch-up\n")


def _acquire_head_lock(main: str) -> str | None:
    """Reserve this worktree's HEAD against a concurrent branch switch for one automatic mutation.

    Git writes ``HEAD.lock`` before changing a worktree's checked-out branch.  Holding that exact per-worktree
    lock closes the otherwise unavoidable gap between proving that HEAD names the default branch and
    materialising the target tree: a concurrent ``git switch`` now refuses rather than leaving ``read-tree`` to
    write the selected side branch's index/worktree.  The lock is deliberately opt-in for the boot controller;
    existing consented public operations retain their established interface and timing.
    """
    return _acquire_git_lock(main, "HEAD.lock", "Engine automatic checkout catch-up\n")


def _release_head_lock(path: str) -> None:
    """Release only the HEAD lock this controller successfully created; failure stays fail-safe."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _advance_clean_default_snapshot(snapshot: dict, *, protect_head: bool = False) -> dict:
    """Apply an already-assessed default-branch snapshot without rescuing work or switching branches.

    This is the narrow mutation seam shared by consented ``catch_up`` and automatic session-start catch-up.
    Callers keep their own eligibility policy; this primitive owns the common identity recheck, clean
    lossless gate, named-ref compare-and-swap, index-locked materialization, rollback, and postcondition.
    It deliberately never calls ``_dirty_subsumed`` or ``_rescue_then_reconcile``. ``protect_head`` is the
    boot-only branch-switch interlock: it reserves Git's index before the named-ref compare-and-swap, then
    holds Git's per-worktree HEAD lock through the branch recheck and materialisation.  Together those two
    Git-recognised locks mean a concurrent branch change cannot start in the ref-advance interval or redirect
    the target tree into a side worktree.
    """
    if not protect_head:
        return _advance_clean_default_snapshot_locked(snapshot)
    return _advance_clean_default_snapshot_head_interlocked(snapshot)


def _advance_clean_default_snapshot_locked(snapshot: dict) -> dict:
    """The established manual clean fast-forward sequence; its public timing and outcomes remain unchanged."""
    main, default = snapshot["main"], snapshot["branch"]
    if not _snapshot_unchanged(snapshot):
        return {**snapshot, "status": "blocked", "reason": "checkout-changed", "applied": False}
    if not _succeeds(["git", "-C", main, "merge-base", "--is-ancestor",
                      snapshot["head_oid"], snapshot["target_oid"]]):
        return {**snapshot, "status": "blocked", "reason": "diverged", "applied": False}
    lossless, reasons = _is_lossless(main)
    if not lossless:
        return {**snapshot, "status": "blocked", "reason": "local-work", "reasons": reasons,
                "applied": False}
    advanced = _advance_named_default(main, default, snapshot["head_oid"], snapshot["target_oid"])
    still_default = ((_run(["git", "-C", main, "symbolic-ref", "--quiet", "--short", "HEAD"]) or "").strip()
                     == default)
    materialized = (advanced and still_default and
                    _materialize_target(main, snapshot["head_oid"], snapshot["target_oid"]))
    if advanced and not materialized:
        _ok(["git", "-C", main, "update-ref", f"refs/heads/{default}",
             snapshot["head_oid"], snapshot["target_oid"]])
    after = (_run(["git", "-C", main, "rev-parse", "HEAD"]) or "").strip()
    after_branch = (_run(["git", "-C", main, "symbolic-ref", "--quiet", "--short", "HEAD"]) or "").strip()
    if materialized and after == snapshot["target_oid"] and after_branch == default:
        return {"status": "fixed", "main": main, "branch": default,
                "brought_in": snapshot["behind_commits"], "before": snapshot["head_oid"], "after": after,
                "target_oid": snapshot["target_oid"], "applied": True}
    changed = after != snapshot["head_oid"] or after_branch != default
    return {"status": "blocked", "main": main, "branch": default,
            "reason": "postcondition-failed" if changed else "clash",
            "before": snapshot["head_oid"], "after": after, "applied": changed}


def _advance_clean_default_snapshot_head_interlocked(snapshot: dict) -> dict:
    """Boot-only clean advancement whose materialisation cannot follow a concurrent branch switch.

    Updating the currently checked-out branch itself requires Git's HEAD lock, so the named-ref CAS cannot
    hold that lock directly. The controller reserves ``index.lock`` first (which makes a normal branch switch
    refuse), advances the named default, then reserves ``HEAD.lock`` before letting ``read-tree`` take the
    index lock. A competing HEAD lock after the CAS is given the established bounded refresh window to clear
    only so the controller can roll back the exact CAS. It never resumes materialisation after that clash:
    the competing operation may have changed checkout identity or local work while it owned the lock.
    """
    main, default = snapshot["main"], snapshot["branch"]
    # Detect an in-progress (or stale) branch transition *before* the named ref can move.  A normal checkout
    # cannot sneak in after this check because the index reservation below is acquired before our ref CAS.
    if _git_lock_is_present(main, "HEAD.lock"):
        return {**snapshot, "status": "blocked", "reason": "checkout-changed", "applied": False}
    index_lock = _acquire_index_lock(main)
    if not index_lock:
        return {**snapshot, "status": "blocked", "reason": "checkout-changed", "applied": False}
    lock = None
    advanced = False
    materialized = False
    after = ""
    after_branch = ""
    try:
        # These are intentionally repeated inside the branch-switch interlock.  A stale assessment, late edit,
        # ref movement, or newly started Git operation all decline before this automatic path changes a ref.
        if _git_lock_is_present(main, "HEAD.lock") or not _snapshot_unchanged(snapshot):
            return {**snapshot, "status": "blocked", "reason": "checkout-changed", "applied": False}
        if not _succeeds(["git", "-C", main, "merge-base", "--is-ancestor",
                          snapshot["head_oid"], snapshot["target_oid"]]):
            return {**snapshot, "status": "blocked", "reason": "diverged", "applied": False}
        lossless, reasons = _is_lossless(main)
        if not lossless:
            return {**snapshot, "status": "blocked", "reason": "local-work", "reasons": reasons,
                    "applied": False}
        advanced = _advance_named_default(main, default, snapshot["head_oid"], snapshot["target_oid"])
        if not advanced:
            return {**snapshot, "status": "blocked", "reason": "checkout-changed", "applied": False}

        # `index.lock` excludes normal branch switches until this exact lock is held. If a competing Git
        # operation owns HEAD after the CAS, release the index reservation so that operation can settle, then
        # wait only so Git can perform the exact rollback. Never retry materialisation after the clash:
        # arbitrary edits do not take Git locks, so the original full identity and losslessness proof no
        # longer governs the checkout.
        lock = _acquire_head_lock(main)
        if not lock:
            _release_head_lock(index_lock)
            index_lock = None
            deadline = time.monotonic() + _FETCH_TIMEOUT
            while _git_lock_is_present(main, "HEAD.lock") and time.monotonic() < deadline:
                time.sleep(0.05)
            restored = _advance_named_default(main, default, snapshot["target_oid"], snapshot["head_oid"])
            after = (_run(["git", "-C", main, "rev-parse", "HEAD"]) or "").strip()
            after_branch = (_run(["git", "-C", main, "symbolic-ref", "--quiet", "--short", "HEAD"]) or "").strip()
            restored_exactly = restored and after == snapshot["head_oid"] and after_branch == default
            return {"status": "blocked", "main": main, "branch": default,
                    "reason": "checkout-changed" if restored_exactly else "rollback-failed",
                    "before": snapshot["head_oid"], "after": after, "applied": not restored_exactly}
        # read-tree takes index.lock itself. Release our branch-switch reservation only after HEAD.lock now
        # excludes a checkout, and keep that HEAD lock through all remaining branch and tree checks.
        _release_head_lock(index_lock)
        index_lock = None
        after = (_run(["git", "-C", main, "rev-parse", "HEAD"]) or "").strip()
        after_branch = (_run(["git", "-C", main, "symbolic-ref", "--quiet", "--short", "HEAD"]) or "").strip()
        if after != snapshot["target_oid"] or after_branch != default:
            return {"status": "blocked", "main": main, "branch": default, "reason": "checkout-changed",
                    "before": snapshot["head_oid"], "after": after, "applied": after != snapshot["head_oid"]}
        materialized = _materialize_target(main, snapshot["head_oid"], snapshot["target_oid"])
        after = (_run(["git", "-C", main, "rev-parse", "HEAD"]) or "").strip()
        after_branch = (_run(["git", "-C", main, "symbolic-ref", "--quiet", "--short", "HEAD"]) or "").strip()
        if materialized and after == snapshot["target_oid"] and after_branch == default:
            return {"status": "fixed", "main": main, "branch": default,
                    "brought_in": snapshot["behind_commits"], "before": snapshot["head_oid"], "after": after,
                    "target_oid": snapshot["target_oid"], "applied": True}
    finally:
        if lock:
            _release_head_lock(lock)
        if index_lock:
            _release_head_lock(index_lock)

    # Materialisation failed while HEAD was reserved, so no branch switch can have redirected the tree. Roll
    # the named default ref back by exact CAS after releasing HEAD.lock (Git itself needs that lock to update a
    # checked-out ref); a late edit remains in the worktree and is never overwritten.
    if advanced and not materialized:
        _ok(["git", "-C", main, "update-ref", f"refs/heads/{default}",
             snapshot["head_oid"], snapshot["target_oid"]])
    after = (_run(["git", "-C", main, "rev-parse", "HEAD"]) or "").strip()
    after_branch = (_run(["git", "-C", main, "symbolic-ref", "--quiet", "--short", "HEAD"]) or "").strip()
    changed = after != snapshot["head_oid"] or after_branch != default
    return {"status": "blocked", "main": main, "branch": default,
            "reason": "postcondition-failed" if changed else "clash",
            "before": snapshot["head_oid"], "after": after, "applied": changed}


def catch_up(cwd: str | None = None, apply: bool = False, *, do_fetch: bool = True,
             expected_target: str | None = None) -> dict:
    """Bring a behind main checkout current, on the operator's consent — the ON-DEFAULT arm. Two cases, each
    lossless. CLEAN case (lossless gate clean): lossless BY CONSTRUCTION — proves strict ancestry, atomically
    advances the NAMED default from its exact assessed OID, then materializes the exact target WITHOUT ever
    leaving the default branch. DIRTY-SUBSUMED case (StarshipSuperjam/engine-template#810): when the ONLY obstruction is uncommitted work whose
    every change is already present at the verified target (a first-run transformation the reviewed upstream
    absorbed), it DELEGATES to the rescue-first `_rescue_then_reconcile` arm — which does switch branches while
    it saves the dirty tree to a retained rescue branch, so here losslessness rests on that rescue branch, not on
    'no branch switch'. Any OTHER local obstruction (a stash, off-branch commit, paused op, or dirty work NOT
    subsumed) still BLOCKS 'local-work' with no mutation, exactly as before. A concurrent checkout cannot advance
    the wrong branch; divergence refuses. When PARKED ON A SIDE BRANCH, returning it is `return_to_default`'s job
    — catch_up never fast-forwards a side branch, so it declines ('off-main'). Dry-run (apply=False) reports
    without mutating. Every mutation targets `git -C <main>` — never the session's own worktree.
    status ∈ healthy | behind | off-main | unavailable | fixed | blocked."""
    behind = _checkout_snapshot(cwd, do_fetch=do_fetch)
    if behind["state"] == "unavailable":
        return {**behind, "status": "unavailable", "applied": False}
    if behind["state"] == "current":
        return {**behind, "status": "healthy", "applied": False}
    if not behind.get("on_default"):
        # behind, but parked on a side branch: returning to the default is return_to_default's job, not a
        # fast-forward of the side branch (catch_up's "no branch switch" invariant). Decline, no mutation.
        return {"status": "off-main", "main": behind["main"], "branch": behind["branch"],
                "current": behind.get("current"), "applied": False}
    main, default, missing = behind["main"], behind["branch"], behind["behind_commits"]
    if not apply:
        return {**behind, "status": "behind", "applied": False}
    if expected_target is None:
        return {**behind, "status": "blocked", "reason": "consent-target-required", "applied": False}
    if behind["target_oid"] != expected_target:
        return {**behind, "status": "blocked", "reason": "target-changed", "applied": False}
    if not _snapshot_unchanged(behind):
        return {**behind, "status": "blocked", "reason": "checkout-changed", "applied": False}
    if not _succeeds(["git", "-C", main, "merge-base", "--is-ancestor",
                      behind["head_oid"], behind["target_oid"]]):
        return {**behind, "status": "blocked", "reason": "diverged", "applied": False}
    lossless, reasons = _is_lossless(main)
    if not lossless:
        # StarshipSuperjam/engine-template#810: the only obstruction being uncommitted work already SUBSUMED by the verified target is the
        # first-run-strand case — reconcile it losslessly (rescue-first). Any other obstruction (stash,
        # off-branch commit, paused op, or dirty work that is NOT subsumed) still blocks with no mutation.
        if reasons == ["uncommitted"] and _dirty_subsumed(main, behind["target_oid"]):
            return _rescue_then_reconcile(behind, original_branch=default)
        return {**behind, "status": "blocked", "reason": "local-work", "reasons": reasons, "applied": False}
    return _advance_clean_default_snapshot(behind)


def return_to_default(cwd: str | None = None, apply: bool = False, *, do_fetch: bool = True,
                      expected_target: str | None = None) -> dict:
    """Point an operator checkout PARKED ON A NON-DEFAULT BRANCH back at its default branch (and bring it
    current), on the operator's consent — the correction for the off-main state (StarshipSuperjam/engine-template#342). LOSSLESS: returning to a
    NAMED branch never orphans commits (the side branch ref keeps them — no rescue needed, unlike unstrand's
    detached arm), and the switch runs ONLY when the lossless gate is clean (no uncommitted edits, no stash, no
    paused git operation); otherwise it BLOCKS with no mutation, nothing lost. The `git checkout <default>` is
    defensive — never `-f` — so a refusal blocks rather than forces. Having returned, it fast-forwards to the
    exact target OID from the freshly-read snapshot with the same `--ff-only` proof catch_up uses. The local
    default/target relationship is checked before the switch, so divergence refuses without moving the checkout.
    Dry-run (apply=False) reports without mutating. Every mutation targets `git -C <main>` — never the session's
    own worktree.
    status ∈ healthy | off-main | unavailable | blocked | fixed."""
    snapshot = checkout_snapshot(cwd, do_fetch=do_fetch)
    if snapshot["state"] == "unavailable":
        return {**snapshot, "status": "unavailable", "applied": False}
    if snapshot.get("on_default"):
        return {**snapshot, "status": "healthy", "applied": False}
    off = {"state": "off-main", "main": snapshot["main"], "branch": snapshot["current"],
           "main_branch": snapshot["branch"]}
    main, default, current = off["main"], snapshot["branch"], off["branch"]
    if not apply:
        return {**snapshot, **off, "status": "off-main", "applied": False}
    if expected_target is None:
        return {**snapshot, "status": "blocked", "reason": "consent-target-required", "applied": False}
    if snapshot["target_oid"] != expected_target:
        return {**snapshot, "status": "blocked", "reason": "target-changed", "applied": False}
    lossless, reasons = _is_lossless(main)
    if not lossless:
        # StarshipSuperjam/engine-template#810 off-main sibling of catch_up's arm: when the ONLY obstruction is uncommitted work already
        # SUBSUMED by the verified target, reconcile it losslessly (rescue-first), then land on the default at
        # the target. `_rescue_then_reconcile` returns HEAD to the side branch (`current`) on any block.
        if reasons == ["uncommitted"] and _dirty_subsumed(main, snapshot["target_oid"]):
            return _rescue_then_reconcile(snapshot, original_branch=current)
        # dirty tree / stash / paused op not subsumed: returning would risk work -> block, no mutation
        return {"status": "blocked", "main": main, "branch": default, "from": current,
                "reasons": reasons, "applied": False}
    if not _snapshot_unchanged(snapshot):
        return {"status": "blocked", "main": main, "branch": default, "from": current,
                "reason": "checkout-changed", "applied": False}
    local_default = snapshot["default_oid"]
    target = snapshot["target_oid"]
    local_is_ancestor = _succeeds(["git", "-C", main, "merge-base", "--is-ancestor", local_default, target])
    target_is_ancestor = _succeeds(["git", "-C", main, "merge-base", "--is-ancestor", target, local_default])
    if not local_is_ancestor and not target_is_ancestor:
        return {"status": "blocked", "main": main, "branch": default, "from": current,
                "reason": "diverged", "applied": False}
    advanced_default = local_is_ancestor and local_default != target
    if advanced_default:
        if not _advance_named_default(main, default, local_default, target):
            return {"status": "blocked", "main": main, "branch": default, "from": current,
                    "reason": "checkout-changed", "applied": False}
    if not _ok(["git", "-C", main, "checkout", default]):   # defensive; never -f; a refusal blocks, never forces
        rolled_back = (not advanced_default or
                       _ok(["git", "-C", main, "update-ref", f"refs/heads/{default}", local_default, target]))
        return {"status": "blocked", "main": main, "branch": default, "from": current,
                "reason": "checkout-failed", "applied": not rolled_back}
    brought_current = _succeeds(["git", "-C", main, "merge-base", "--is-ancestor", target, "HEAD"])
    post_branch = (_run(["git", "-C", main, "symbolic-ref", "--quiet", "--short", "HEAD"]) or "").strip()
    post_contains_target = _succeeds(["git", "-C", main, "merge-base", "--is-ancestor", target, "HEAD"])
    if not brought_current or post_branch != default or not post_contains_target:
        ref_restored = (not advanced_default or
                        _ok(["git", "-C", main, "update-ref", f"refs/heads/{default}", local_default, target]))
        restored = _ok(["git", "-C", main, "checkout", current])
        return {"status": "blocked", "main": main, "branch": default, "from": current,
                "reason": "postcondition-failed", "restored": restored and ref_restored,
                "applied": not (restored and ref_restored)}
    return {"status": "fixed", "main": main, "branch": default, "from": current,
            "brought_current": brought_current, "target_oid": snapshot["target_oid"], "applied": True}


# ---- the operator-runnable demo (synthetic fixtures; deterministic) -------------------------

def _fixture(tmp: str, name: str, *, detach: bool, drop_settings: bool) -> str:
    """A throwaway git repo so the detector can be SEEN classifying it — no live alarm needed."""
    root = os.path.join(tmp, name)
    os.makedirs(os.path.join(root, ".claude"))
    os.makedirs(os.path.join(root, ".engine"))
    if not drop_settings:
        with open(os.path.join(root, ".claude", "settings.json"), "w") as fh:
            fh.write("{}")
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.email=e@x", "-c", "user.name=n",
                                              "commit", "-q", "-m", "seed", "--allow-empty"]):
        _run(["git", "-C", root] + c)
    if detach:
        sha = (_run(["git", "-C", root, "rev-parse", "HEAD"]) or "").strip()
        _run(["git", "-C", root, "checkout", "-q", "--detach", sha])
    return root


def _stranded_with_at_risk_work(tmp: str) -> str:
    """A stranded fixture carrying RECOGNIZABLE at-risk work: a detached HEAD with a committed file
    `my-important-note.txt` ("DO NOT LOSE THIS") reachable from NO branch — exactly the work a naive re-attach
    would orphan. Lets the operator SEE that the danger is real and that the fix saves it."""
    root = _fixture(tmp, "stranded", detach=True, drop_settings=False)
    with open(os.path.join(root, "my-important-note.txt"), "w") as fh:
        fh.write("DO NOT LOSE THIS")
    _run(["git", "-C", root, "add", "-A"])
    _run(["git", "-C", root, "-c", "user.email=e@x", "-c", "user.name=n",
          "commit", "-q", "-m", "important note (off-branch)"])
    return root


def _behind_fixture(tmp: str) -> str:
    """A throwaway 'origin' advanced by several DATED merge commits + a `work` clone left behind it — so the
    checkout snapshot and firm presentation can be seen, followed by catch-up, all on a LOCAL remote (no network,
    deterministic). Returns the `work` checkout path."""
    import subprocess as sp
    origin = os.path.join(tmp, "origin")
    os.makedirs(os.path.join(origin, ".claude"))
    os.makedirs(os.path.join(origin, ".engine"))
    with open(os.path.join(origin, ".claude", "settings.json"), "w") as fh:
        fh.write("{}")
    with open(os.path.join(origin, ".engine", "marker"), "w") as fh:   # a tracked file so .engine survives the clone
        fh.write("e")

    def g(date: str, *args: str) -> None:
        env = dict(os.environ, GIT_AUTHOR_DATE=f"{date}T12:00:00", GIT_COMMITTER_DATE=f"{date}T12:00:00")
        sp.run(["git", "-C", origin, "-c", "user.email=e@x", "-c", "user.name=n", *args],
               capture_output=True, text=True, check=False, env=env)

    _run(["git", "-C", origin, "init", "-q", "-b", "main"])
    g("2026-06-01", "add", "-A")
    g("2026-06-01", "commit", "-q", "-m", "seed")
    work = os.path.join(tmp, "work")
    sp.run(["git", "clone", "-q", origin, work], capture_output=True, text=True, check=False)
    for i, date in enumerate(["2026-06-03", "2026-06-05", "2026-06-07", "2026-06-09"], start=1):
        _run(["git", "-C", origin, "checkout", "-q", "-b", f"pr{i}", "main"])
        with open(os.path.join(origin, f"f{i}.txt"), "w") as fh:
            fh.write(f"pr{i}\n")
        g(date, "add", "-A")
        g(date, "commit", "-q", "-m", f"work {i}")
        _run(["git", "-C", origin, "checkout", "-q", "main"])
        g(date, "merge", "--no-ff", "-q", "-m", f"Merge pull request #{i}", f"pr{i}")
    return work


def _off_main_fixture(tmp: str) -> str:
    """A `work` clone (so `origin/HEAD` -> main: the default is KNOWN with confidence) left checked out on a
    side branch carrying its own unmerged commit — the wrong-branch park (StarshipSuperjam/engine-template#342). Returns the `work` path."""
    work = _behind_fixture(tmp)                  # a clone on main, behind origin by several merged PRs
    _run(["git", "-C", work, "checkout", "-q", "-b", "my-feature"])
    with open(os.path.join(work, "my-feature-note.txt"), "w") as fh:
        fh.write("WORK IN PROGRESS")
    _run(["git", "-C", work, "add", "-A"])
    _run(["git", "-C", work, "-c", "user.email=e@x", "-c", "user.name=n",
          "commit", "-q", "-m", "my unfinished feature work"])
    return work


# Plain-language renderings of the internal plan/result, for the operator-facing CLI + demo (the structured
# {plan, did} stay machine-shaped; these translate them so no internal token reaches the operator surface).
_STEP_WORDS = {"rescue": "save your at-risk work to a safe point",
               "reattach": "put your folder back on its branch",
               "rematerialize": "restore the engine's files"}


def _plan_words(plan: list) -> str:
    return ", then ".join(_STEP_WORDS.get(s, s) for s in plan) or "nothing — it's already healthy"


def _demo() -> int:
    import tempfile
    print("1) What checkout_health DETECTS — is your top-level project folder healthy or stranded:\n")
    states = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name, label, kw in (
            ("healthy", "a healthy folder (on its branch, engine files present)", {"detach": False, "drop_settings": False}),
            ("detached", "a folder stuck off its branch (detached HEAD)", {"detach": True, "drop_settings": False}),
            ("missing", "a folder missing the engine's files", {"detach": False, "drop_settings": True})):
            states[name] = detect_strand(cwd=_fixture(tmp, name, **kw))
            print(f"  • {label}:\n      {states[name]}")

    print("\n2) The REPAIR, on a throwaway example folder (never your real one):\n")
    with tempfile.TemporaryDirectory() as tmp:
        root = _stranded_with_at_risk_work(tmp)
        print("   Before: this folder is stuck off its branch, and it holds work — the file")
        print("   'my-important-note.txt' (\"DO NOT LOSE THIS\") — that is on NO branch. Re-attaching the")
        print("   normal way would leave that work behind. Watch where it goes.\n")
        print(f"   What I'd do, in plain terms: {_plan_words(unstrand(cwd=root)['plan'])}.")
        result = unstrand(cwd=root, apply=True)
        healed = detect_strand(cwd=root) is None
        print(f"   After the repair: folder healthy now? {healed}")
        print(f"   I saved your at-risk work first to the safe point (a rescue branch): {result.get('rescue')}")
        note = _run(["git", "-C", root, "show", f"{result['rescue']}:my-important-note.txt"])
        print(f"   Proof it survived — 'my-important-note.txt' on the safe point still reads: {note!r}")

    print("\n3) The checkout snapshot (#335) — any missing shared work is visible, with calm/firm presentation:\n")
    with tempfile.TemporaryDirectory() as tmp:
        work = _behind_fixture(tmp)
        behind = detect_behind_origin(cwd=work, do_fetch=True)
        print("   Before: this folder is on its branch and healthy, but shared updates have landed that it")
        print("   doesn't have yet. Every missing commit is reported; merge velocity only chooses whether")
        print("   the operator sees a calm notice or a firm warning:")
        print(f"      {behind}")
        result = catch_up(cwd=work, apply=True, do_fetch=True, expected_target=behind["target_oid"])
        caught_up = detect_behind_origin(cwd=work, do_fetch=True) is None
        print(f"   After bringing it up to date (a safe fast-forward): up to date now? {caught_up} "
              f"(brought in {result.get('brought_in')} commits)")

    print("\n4) The 'parked on the wrong branch' state (#342) — your folder is on a side branch, not your main")
    print("   one. Caught on day one, before anything is even missing; the engine offers to point it back —\n")
    with tempfile.TemporaryDirectory() as tmp:
        work = _off_main_fixture(tmp)
        off = detect_off_main(cwd=work)
        print("   Before: this folder is healthy but parked on a side branch instead of its main one. The")
        print("   off-main signal is OFFLINE and fires straight away (no network, no waiting to fall behind):")
        print(f"      {off}")
        feature_sha = (_run(["git", "-C", work, "rev-parse", "my-feature"]) or "").strip()
        pinned = detect_behind_origin(cwd=work, do_fetch=True)
        result = return_to_default(cwd=work, apply=True, do_fetch=True,
                                   expected_target=pinned["target_oid"])
        back_on_main = detect_off_main(cwd=work) is None
        feature_kept = (_run(["git", "-C", work, "rev-parse", "my-feature"]) or "").strip() == feature_sha
        feature_note = _run(["git", "-C", work, "show", "my-feature:my-feature-note.txt"])
        print(f"   After pointing it back: on the main branch now? {back_on_main}; your side-branch work left")
        print(f"   exactly where it was? {feature_kept} — 'my-feature-note.txt' on 'my-feature' still reads: "
              f"{feature_note!r}")

    print("\n5) The plain-language lines the operator sees — a strand, calm drift, then firm drift (all OFFERS):\n")
    import boot  # lazy: avoids the boot<->checkout_health import cycle (boot is fully loaded by demo time)
    signals = boot.gather_signals()
    signals["strand"] = {"states": ["detached"], "main": "/your/project/folder"}
    signals["behind_origin"] = None   # show the strand line first, alone
    print(boot.render_dashboard(signals))
    print()
    signals["strand"] = None          # then calm below-velocity drift (synthetic — no live network)
    signals["behind_origin"] = {"state": "behind", "main": "/your/project/folder", "branch": "main",
                                "current": "main", "on_default": True, "behind_commits": 1,
                                "missing_merges": 0, "presentation": "notice", "latest": "2026-06-27",
                                "advisory": "merged"}
    print(boot.render_dashboard(signals))
    print()
    signals["behind_origin"] = {**signals["behind_origin"], "behind_commits": 9,
                                "missing_merges": 5, "presentation": "warning"}
    print(boot.render_dashboard(signals))
    # Self-check: detection separates a healthy folder from the two stranded shapes; the strand repair heals
    # the folder and the at-risk work survives on the rescue branch; the snapshot reports drift and the
    # catch-up brings the folder current; AND the off-main signal fires on a side branch and return_to_default
    # points it back losslessly (the side-branch work stays put on its branch).
    ok = (states.get("healthy") is None and states.get("detached") is not None
          and states.get("missing") is not None and healed and "DO NOT LOSE THIS" in (note or "")
          and behind is not None and behind.get("state") == "behind" and caught_up
          and off is not None and off.get("state") == "off-main"
          and back_on_main and feature_kept and "WORK IN PROGRESS" in (feature_note or ""))
    if not ok:
        print("\nDEMO UNEXPECTED: strand detection/repair, the behind-origin signal/catch-up, or the off-main "
              "signal/return-to-default, did not behave as expected.", file=sys.stderr)
        return 1
    return 0


def _plain_unstrand(apply: bool) -> int:
    """The operator-runnable `unstrand` CLI over THIS repo's real checkout, summarized in plain words."""
    r = unstrand(apply=apply)
    if r["status"] == "healthy":
        print("Your project folder is healthy — nothing to fix.")
    elif r["status"] == "needs-manual":
        print("Your project folder needs attention, but I can't fix it automatically without risking your "
              "work — so I won't touch it. It's safest to sort this one out by hand.")
    elif not apply:
        print("Your project folder has drifted into a broken state. I can fix it safely (I'll save anything "
              "at risk to a safe point first). Re-run with --apply to do it.")
    elif r["status"] == "fixed":
        msg = "Fixed your project folder — it's healthy again."
        if r.get("rescue"):
            msg += f" I saved your at-risk work to a safe point first (the branch '{r['rescue']}')."
        print(msg)
    else:
        print("I started but couldn't safely finish — so I stopped, leaving your work untouched. "
              "It's safest to sort this one out by hand.")
    return 0


def _plain_catch_up(apply: bool, expected_target: str | None = None) -> int:
    """The operator-runnable behind-origin CLI over THIS repo's real checkout, in plain words (no git verbs)."""
    r = catch_up(apply=apply, expected_target=expected_target)
    if r["status"] == "healthy":
        print("Your project folder is up to date — nothing to bring in.")
    elif r["status"] == "unavailable":
        _print_unavailable(r)
    elif r["status"] == "fixed" and r.get("reconciled"):
        print("Brought your project folder up to date. Your uncommitted setup changes were already part of the "
              f"shared project, so I saved a copy to a safe point first (the branch '{r['rescue']}'), then "
              "brought the folder current — nothing was lost.")
    elif r["status"] == "fixed":
        print("Brought your project folder up to date — it now has the recent shared work it was missing.")
    elif r["status"] == "blocked" and r.get("reason") == "rescue-failed":
        if r.get("restored"):
            print("I couldn't safely save your uncommitted changes to a safe point, so I stopped and put your "
                  "folder back exactly as it was — your changes are still here, nothing is lost.")
        else:
            print("I couldn't save your uncommitted changes to a safe point and couldn't fully put your folder "
                  "back, so I stopped. Your changes are still here — please check the folder before trying again.")
    elif r["status"] == "blocked" and r.get("reason") == "rescue-incomplete":
        print(f"I did save your uncommitted changes to a safe point (the branch '{r['rescue']}'), but something on "
              "your machine — most likely a commit hook — stopped me from finishing. Your changes are safe on that "
              "branch; your folder is back on its main branch without them, so recover them from that branch.")
    elif r["status"] == "blocked" and r.get("reason") == "local-work" and r.get("rescue"):
        print("Your uncommitted changes turned out not to be part of the shared project after all, so I did not "
              f"change your main line. I saved them safely to a safe point (the branch '{r['rescue']}') and your "
              "folder is now clean — bring it up to date whenever you're ready.")
    elif r["status"] == "blocked" and r.get("reason") == "postcondition-failed":
        extra = f" Your uncommitted changes are safe on the branch '{r['rescue']}'." if r.get("rescue") else ""
        print("Another project operation raced the final update check, so I stopped without claiming success." +
              extra + " Inspect the folder's current line and history before doing anything else.")
    elif r["status"] == "blocked" and r.get("rescue"):
        print(f"I saved your uncommitted changes to a safe point (the branch '{r['rescue']}') but the project "
              "changed before I could finish bringing your folder current, so I stopped. Nothing was lost — "
              "check the folder before trying again.")
    elif r["status"] == "blocked" and r.get("reason") == "consent-target-required":
        print("The exact confirmation target is missing, so I left your folder untouched. Run the dry check "
              "first, then use the complete apply command it prints.")
    elif r["status"] == "blocked" and r.get("reason") in {"target-changed", "checkout-changed"}:
        print("The project changed since it was checked, so I left your folder untouched. Check it again and "
              "confirm the newly reported target before applying the update.")
    elif r["status"] == "blocked" and r.get("reason") == "diverged":
        print("Your main line and the shared project have both moved, so I left everything untouched. This "
              "needs a deliberate reconciliation rather than an automatic catch-up.")
    elif r["status"] == "blocked":
        print("Your project folder is behind, but you have unsaved changes that clash with the incoming work, "
              "so I left everything untouched — nothing is lost. Save or set those changes aside and ask again.")
    elif not apply:
        if r.get("presentation") == "warning":
            print("Your project folder has fallen behind recent shared work. I can bring it up to date safely "
                  "using the exact target checked here.")
        else:
            print("Your project folder has newer shared work available. I can bring it up to date safely using "
                  "the exact target checked here.")
        print(f"To apply exactly this checked version, run `catchup --apply --target {r['target_oid']}`.")
    else:
        print("I couldn't bring your project folder up to date safely, so I left it untouched — nothing is lost.")
    return 0


def _plain_return_to_default(apply: bool, expected_target: str | None = None) -> int:
    """The operator-runnable return-to-default CLI over THIS repo's real checkout, in plain words (no git verbs)."""
    r = return_to_default(apply=apply, expected_target=expected_target)
    if r["status"] == "healthy":
        print("Your project folder is on your main branch already — nothing to move.")
    elif r["status"] == "unavailable":
        _print_unavailable(r)
    elif r["status"] == "fixed" and r.get("reconciled"):
        print("Pointed your project folder back at your main branch and brought it up to date. Your uncommitted "
              f"setup changes were already part of the shared project, so I saved a copy to a safe point first "
              f"(the branch '{r['rescue']}'); your other work stays on its own branch — nothing was lost.")
    elif r["status"] == "fixed" and r.get("brought_current"):
        print("Pointed your project folder back at your main branch and brought it up to date. Your other work "
              "is untouched — it's still saved on its own branch, exactly where it was.")
    elif r["status"] == "fixed":
        print("Pointed your project folder back at your main branch — your other work is untouched, still saved "
              "on its own branch. I left your main branch exactly as it was (it has some local changes of its "
              "own that aren't on the shared copy yet), so it may not be fully up to date.")
    elif r["status"] == "blocked" and r.get("reason") == "rescue-failed":
        if r.get("restored"):
            print("I couldn't safely save your uncommitted changes to a safe point, so I stopped and put your "
                  "folder back exactly where it was — your changes are still here, nothing is lost.")
        else:
            print("I couldn't save your uncommitted changes to a safe point and couldn't fully put your folder "
                  "back, so I stopped. Your changes are still here — please check the folder before trying again.")
    elif r["status"] == "blocked" and r.get("reason") == "rescue-incomplete":
        print(f"I did save your uncommitted changes to a safe point (the branch '{r['rescue']}'), but something on "
              "your machine — most likely a commit hook — stopped me from finishing. Your changes are safe on that "
              "branch; recover them from there when you're ready.")
    elif r["status"] == "blocked" and r.get("reason") == "local-work" and r.get("rescue"):
        print("Your uncommitted changes turned out not to be part of the shared project after all, so I did not "
              f"move your main branch. I saved them safely to a safe point (the branch '{r['rescue']}') and put "
              "your folder back on its side branch — bring it up to date whenever you're ready.")
    elif r["status"] == "blocked" and r.get("reason") == "postcondition-failed":
        if r.get("restored"):
            extra = f" Your uncommitted changes are safe on the branch '{r['rescue']}'." if r.get("rescue") else ""
            print("The final update check failed, so I put your folder back on its original side line." + extra +
                  " Nothing was lost; inspect the repository state before trying again.")
        else:
            extra = f" Your uncommitted changes are safe on the branch '{r['rescue']}'." if r.get("rescue") else ""
            print("The final update check failed and I couldn't restore the original side line automatically." +
                  extra + " I stopped immediately; inspect the folder state before doing anything else.")
    elif r["status"] == "blocked" and r.get("rescue"):
        print(f"I saved your uncommitted changes to a safe point (the branch '{r['rescue']}') but the project "
              "changed before I could finish, so I stopped and put your folder back on its side branch. Nothing "
              "was lost — check the folder before trying again.")
    elif r["status"] == "blocked" and r.get("reason") == "consent-target-required":
        print("The exact confirmation target is missing, so I left your folder exactly where it is. Run the "
              "dry check first, then use the complete apply command it prints.")
    elif r["status"] == "blocked" and r.get("reason") in {"target-changed", "checkout-changed"}:
        print("The project changed since it was checked, so I left your folder exactly where it is. Check it "
              "again and confirm the newly reported target before applying the update.")
    elif r["status"] == "blocked" and r.get("reason") == "diverged":
        print("Your main line and the shared project have both moved, so I left your folder on its current side "
              "line. This needs a deliberate reconciliation; nothing moved and nothing was lost.")
    elif r["status"] == "blocked":
        print("Your project folder is parked on another branch, but it has unsaved changes (or a git operation "
              "paused mid-way), so I left everything exactly where it is — nothing moved, nothing lost. Save or "
              "set those aside and ask again.")
    elif not apply:
        parked = r.get("branch")
        where = f"the branch '{parked}'" if parked else "another branch"
        print(f"Your project folder is parked on {where} instead of your main one. I can point it back safely — "
              f"your work there stays saved on that branch.")
        print(f"To apply exactly this checked version, run `returnmain --apply --target {r['target_oid']}`.")
    else:
        print("I couldn't safely point your project folder back at your main branch, so I left it untouched — "
              "nothing is lost.")
    return 0


def _plain_offmain() -> int:
    """Report (plain words, no git verbs) whether THIS repo's checkout is parked off its default branch."""
    off = detect_off_main()
    if not off:
        print("Your project folder is on your main branch — not parked off it.")
    else:
        print(f"Your project folder is parked on the branch '{off['branch']}' instead of your main one "
              f"('{off['main_branch']}'). Run `returnmain` to see how I'd point it back — your work on "
              f"'{off['branch']}' stays saved on that branch.")
    return 0


def _print_unavailable(r: dict) -> None:
    """Cause-aware CLI remedy: retry remote access failures; inspect persistent local/configuration failures."""
    reason = r.get("reason")
    if reason in {"refresh-failed", "refresh-timeout", "remote-head-unreadable"}:
        print("I couldn't freshly reach the shared project, so I changed nothing and won't call this folder up "
              "to date. Check the connection or repository access, then try again.")
    elif reason in {"origin-changed", "checkout-changed", "remote-moved"}:
        print("The project changed while I was checking it, so I changed nothing. Inspect the project sharing "
              "address and current folder state, then run the check again.")
    else:
        print("I couldn't verify this folder's shared-project setup, so I changed nothing and won't call it up "
              "to date. Inspect the repository address, remote default, and local history before trying again.")


def _plain_behind() -> int:
    """Report (plain words, no git verbs) whether THIS repo's checkout lacks shared work (online)."""
    behind = detect_behind_origin()
    if not behind:
        print("Your project folder is up to date — it has the current shared work.")
        return 0
    if behind.get("state") == "unavailable":
        print("I couldn't freshly check the shared project, so I won't call this folder up to date. Nothing "
              "was changed; check the connection and ask again.")
        return 0
    verb = "catchup" if behind.get("on_default") else "returnmain"
    if behind.get("presentation") == "warning":
        lead = "Your project folder has fallen behind recent shared work."
    else:
        lead = "Your project folder has newer shared work available."
    print(f"{lead} Run `{verb}` to see how I'd bring it current safely — nothing you already have will be lost.")
    return 0


def _target_arg(argv: list) -> str | None:
    """Read the assistant-supplied consent target without exposing it in operator prose."""
    for i, arg in enumerate(argv):
        if arg.startswith("--target="):
            return arg.split("=", 1)[1] or None
        if arg == "--target" and i + 1 < len(argv):
            return argv[i + 1]
    return None


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    if argv and argv[0] == "unstrand":
        return _plain_unstrand(apply="--apply" in argv)
    if argv and argv[0] == "catchup":
        return _plain_catch_up(apply="--apply" in argv, expected_target=_target_arg(argv))
    if argv and argv[0] == "returnmain":
        return _plain_return_to_default(apply="--apply" in argv, expected_target=_target_arg(argv))
    if argv and argv[0] == "offmain":
        return _plain_offmain()
    if argv and argv[0] == "behind":
        return _plain_behind()
    if argv and argv[0] == "snapshot":
        snapshot = checkout_snapshot()
        public = {key: snapshot.get(key) for key in (
            "state", "reason", "branch", "current", "on_default", "target_oid",
            "behind_commits", "missing_merges", "presentation", "latest", "fresh")
                  if key in snapshot}
        print(json.dumps(public, sort_keys=True))
        return 0
    result = detect_strand()
    print(result if result else "healthy — no strand detected")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
