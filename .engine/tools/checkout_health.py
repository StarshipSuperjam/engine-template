#!/usr/bin/env python3
"""Operator-checkout health — detect a stranded operator checkout AND offer a lossless un-stranding fix (#80).

The operator checkout — the top-level project folder the operator opens — is meant to sit on
its branch with the engine files present; build runs in per-session worktrees, never in it (the
never-strand-main floor, realized in CLAUDE.deployed.md). When it is **stranded** anyway — a detached
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
  - **Off-main is the OFFLINE Stage-1 signal** — `detect_off_main` (#342). A healthy checkout PARKED on a
    non-default branch (the wrong-branch park) is caught on day one, before anything is even missing — the
    cheap-to-fix window. It fires only when the default branch is KNOWN with confidence (the persisted name or
    `origin/HEAD`), never on a heuristic guess, so a pre-persistence checkout raises no false standing nag.
  - **Behind-the-main-line is the ONLINE, consequence-gated Stage-2 tail** — `detect_behind_origin` (#335,
    widened branch-agnostic for #342). The harmful essence is *missing your merged main line of work* —
    NOT which branch you sit on — so it fires whether the checkout is on the default branch OR parked on a side
    branch, whenever origin/<default> carries merged work the checkout lacks past the bar. It surfaces a felt
    consequence (never a bare count). It is the one path that touches the network: a best-effort, tightly
    bounded `git fetch` (degrades SILENTLY to None offline — the signal is online-only). It fires only past a
    velocity-relative bar (missing more than ~one active day's worth of merges, computed from the merge-date span
    of recent merges — data-relative, never the wall clock), so ordinary drift never alarms. Whether the missing
    work is fully absorbed or the branch still carries its own is an ADVISORY tone only (`git cherry`, err-gentle
    — `_merged_advisory`), never a safety gate.
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
    `git merge --ff-only origin/<default>` — is the on-default arm: `--ff-only` is git's own
    refuse-if-not-a-fast-forward guard, advancing the branch only along a strict-ancestor linear path and
    ABORTING (no mutation, no loss — keeping any uncommitted edits) if local work would be overwritten, so it is
    lossless **by construction**, needs no rescue branch and no branch switch, and a diverged branch is refused,
    never forced. `return_to_default` — the off-main arm — points a checkout parked on a side branch back at its
    default and fast-forwards: returning to a NAMED branch never orphans commits (the side branch ref keeps
    them, so no rescue), it runs only when the lossless gate is clean (else BLOCKS, no mutation), and its
    `checkout <default>` is defensive (never `-f`). `--ff-only` is the SINGLE sanctioned non-additive git verb;
    every destructive token stays forbidden (test_checkout_health source-scans for them, and behavioral tests
    pin that `catch_up` refuses divergence and `return_to_default` blocks on a paused operation).
  - **Fail-soft = quiet** (detection): any git error / unresolvable main returns None (no strand surfaced) — a
    stranded local checkout cannot reach the protected branch, so it degrades quietly; the double-fault is the
    boot floor's present-marker backstop.

No operator prose lives in the detectors' return values (`{"states": [...], "main": <path>}`;
`{"state": "behind", "missing": N, ...}`) — boot renders the plain-language line (the leaf law keeps git verbs
off the operator surface). The fixes return a structured result the runbook/boot relay in plain words.

CLI:  python tools/checkout_health.py            # classify THIS repo's main checkout (signal or "healthy")
      python tools/checkout_health.py unstrand   # dry-run: what the strand fix WOULD do (no mutation)
      python tools/checkout_health.py unstrand --apply   # repair THIS repo's checkout (only if stranded)
      python tools/checkout_health.py offmain    # report whether the checkout is parked off its default branch
      python tools/checkout_health.py returnmain # dry-run: what pointing it back WOULD do (no mutation)
      python tools/checkout_health.py returnmain --apply # point THIS repo's checkout back at its default branch
      python tools/checkout_health.py behind     # report whether the checkout is behind origin (online)
      python tools/checkout_health.py catchup    # dry-run: what bringing it current WOULD do (no mutation)
      python tools/checkout_health.py catchup --apply    # bring THIS repo's checkout current (only if behind)
      python tools/checkout_health.py demo       # detection + repair walkthroughs on throwaway fixtures
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys

# The engine files whose absence marks a checkout stranded (the two binary states).
_ENGINE_FILES = (os.path.join(".claude", "settings.json"), ".engine")

# The fix's rescue branch (a "safe point" in operator words) + an inline identity so the rescue commit never
# fails for lack of a configured git user on the operator's checkout.
_RESCUE_PREFIX = "engine-rescue"
_RESCUE_IDENT = ["-c", "user.email=engine@local", "-c", "user.name=engine"]

# The behind-origin tail's network fetch is best-effort and TIGHTLY bounded — it runs in boot's SessionStart
# pack, so a slow/hung remote must never stall the boot card. On timeout/offline it degrades to None (the
# signal is online-only). Single-digit seconds, deliberately far below _run's 30s local-git default.
_FETCH_TIMEOUT = 6
# How many recent merges to sample when estimating the project's merge velocity (the staleness bar is
# velocity-relative). A window by COUNT, normalised by the date SPAN of those merges — data-relative, never
# the wall clock, so the bar is deterministic and testable.
_VELOCITY_SAMPLE = 50


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


def _resolve_state(cwd: str | None = None) -> tuple[str, bool, bool, str] | None:
    """Resolve the operator's main checkout ONCE, OFFLINE, for all three classifiers (strand / off-main /
    behind) — so a single detection pass needs only one `git worktree list`. Returns
    (main, detached, missing_files, current) or None when the main checkout cannot be resolved (fail-soft
    quiet). `current` is the checked-out branch name ('' when detached). The DEFAULT branch is deliberately NOT
    resolved here: off-main needs the CONFIDENT default (persisted / origin-HEAD only) while behind tolerates
    the heuristic fallback, so each caller resolves its own (see `_confident_default_branch` / `_default_branch`)."""
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


# ---- the un-stranding fix: lossless-or-it-does-not-run (issue #80) ------------------

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
    if (_run(["git", "-C", main, "status", "--porcelain"]) or "").strip():
        reasons.append("uncommitted")
    if _op_in_progress(main):
        reasons.append("op-in-progress")       # a paused merge/rebase/cherry-pick/revert — never move HEAD
    return (not reasons), reasons


def _persisted_default_branch(main: str) -> str | None:
    """The default-branch name the instantiator derived at first run and persisted as operator config in the
    engine manifest (`<main>/.engine/engine.json`, key `default_branch` — #342). Read OFFLINE. None when
    absent/unreadable/malformed — the construction repo and any pre-persistence checkout have no such key, so
    the caller falls back to live resolution."""
    try:
        with open(os.path.join(main, ".engine", "engine.json"), encoding="utf-8") as fh:
            val = json.load(fh).get("default_branch")
        return val.strip() if isinstance(val, str) and val.strip() else None
    except Exception:  # noqa: BLE001 — absent / unreadable / malformed manifest -> no persisted name
        return None


def _confident_default_branch(main: str) -> str | None:
    """The default branch ONLY when KNOWN with confidence, resolved OFFLINE: the PERSISTED derived name first
    (validated as an existing local branch, so a stale name can never mislead), else `origin/HEAD`'s target.
    None for the heuristic last resorts (a local main/master, or the sole branch) that `_default_branch` adds —
    off-main detection uses THIS so a pre-persistence checkout with no `origin/HEAD` raises no false standing
    nag on a GUESSED default (#342 risk-S2)."""
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


def _make_rescue(main: str) -> str | None:
    """Create a fresh rescue branch (a "safe point") at the current HEAD — capturing any off-branch commits —
    and, if the tree is dirty, commit the working changes onto it, so NOTHING at risk is left unsaved before
    HEAD moves. Returns the rescue branch name, or None if it could not be created (then the fix refuses)."""
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
        _ok(["git", "-C", main, *_RESCUE_IDENT, "commit", "-m",
             "engine: saved unsaved work before un-stranding the checkout"])
        if (_run(["git", "-C", main, "status", "--porcelain"]) or "").strip():
            return None   # the rescue commit did not take -> REFUSE (the work stays safe + uncommitted on
            #               this rescue branch; HEAD never moves on to the default branch) — losslessness is
            #               then self-evident, not reliant on git's later checkout-refusal as a backstop
    return name


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


# ---- the off-main signal: offline Stage-1 wrong-branch park (#342) -------------------

def detect_off_main(cwd: str | None = None) -> dict | None:
    """OFFLINE, READ-ONLY: is the operator's main checkout PARKED on a non-default branch (the wrong-branch
    park — #342, the Stage-1 signal)? Returns {"state":"off-main","main","branch","main_branch"} when the
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


# ---- the absent update-home signal: the engine can't fetch its own updates (#367) ----

def detect_absent_home(cwd: str | None = None) -> dict | None:
    """OFFLINE, READ-ONLY: does this engine's manifest record NO update home (`home_repository`)? A repo
    generated before that coordinate shipped carries an installed engine that cannot fetch its own updates —
    the update path refuses rather than guess a home, and never falls back to this repo's own origin
    (#367). Returns {"state":"absent-home","main"} when the manifest is present and readable but
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


# ---- the behind-the-main-line tail: online signal + the fast-forward corrections (#335; #342) ----

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
    near-idle project still needs MORE THAN one missing merge before the signal speaks. The behind signal
    fires only when missing merges exceed this — ordinary drift at the project's own pace stays quiet."""
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


def detect_behind_origin(cwd: str | None = None, *, do_fetch: bool = True) -> dict | None:
    """ONLINE, READ-ONLY behind-the-main-line signal (#335; widened branch-agnostic for #342). Returns a
    consequence-shaped dict when the operator's main checkout — ON ITS DEFAULT BRANCH or PARKED ON A SIDE
    BRANCH — is missing merged work that has landed on origin/<default>, PAST the velocity bar. Else None.
    Online-only: a fetch failure (offline/timeout) or an unresolvable default degrades SILENTLY to None
    (honest, never a false all-clear).

    Branch-agnostic by design: the harmful essence is *missing your merged main line of work*, not which
    branch you sit on, so it no longer requires HEAD to be the default branch OR a strict ancestor of
    origin/<default> — a side branch carrying its own commits still surfaces. The ANCESTRY / clean-ff question
    moves entirely to the CORRECTIONS (catch_up on the default, return_to_default off it), which block losslessly
    when a fast-forward is not possible. A broken strand is ceded to the strand detector. Return:
    {"state":"behind","main","branch":<default>,"current":<branch>,"on_default":bool,"missing":N,
    "latest":<YYYY-MM-DD>,"advisory":"merged"|"carries-work"} — `missing` is merge commits on origin/<default>
    the checkout lacks; `advisory` is the ADVISORY merged-vs-carries-work tone (errs gentle; see _merged_advisory)."""
    st = _resolve_state(cwd)
    if not st:
        return None
    main, detached, missing_files, current = st
    if detached or missing_files:
        return None                                # a broken strand is the strand detector's territory
    default = _default_branch(main)
    if not default:
        return None                                # can't resolve the main line -> silent (degrade honest)
    if do_fetch:
        # best-effort, tightly bounded; updates ONLY the remote-tracking ref (never the working tree or HEAD).
        # A failure leaves origin/<default> as-is and the count below simply finds nothing -> None.
        _run(["git", "-C", main, "fetch", "--quiet", "origin", default], timeout=_FETCH_TIMEOUT)
    upstream = f"origin/{default}"
    # merged work on origin/<default> the checkout lacks — counted regardless of ancestry (so a diverged HEAD or
    # a side-branch HEAD still surfaces). An absent upstream -> rev-list errors -> 0 -> quiet (online-only).
    missing = int((_run(["git", "-C", main, "rev-list", "--merges", "--count", f"HEAD..{upstream}"])
                   or "0").strip() or "0")
    if missing <= _velocity_threshold(main, upstream):
        return None                                # level/ahead, or below the felt bar (normal drift) -> quiet
    latest = (_run(["git", "-C", main, "log", "--merges", "-1", "--format=%cs", f"HEAD..{upstream}"]) or "").strip()
    return {"state": "behind", "main": main, "branch": default, "current": current,
            "on_default": current == default, "missing": missing, "latest": latest,
            "advisory": _merged_advisory(main, upstream, current)}


def catch_up(cwd: str | None = None, apply: bool = False, *, do_fetch: bool = True) -> dict:
    """Bring a behind main checkout current, on the operator's consent — the ON-DEFAULT arm. LOSSLESS by
    construction: `git merge --ff-only` advances the branch only along a strict-ancestor linear path and ABORTS
    (no mutation, no loss — uncommitted edits to untouched files are kept) if local work would be overwritten,
    so there is no rescue branch AND NO BRANCH SWITCH (unlike unstrand's detached arm), and a diverged branch is
    refused — never forced. When the checkout is PARKED ON A SIDE BRANCH, returning it to the default is
    `return_to_default`'s job — catch_up never fast-forwards a side branch, so it declines ('off-main'). Dry-run
    (apply=False) reports without mutating. Every mutation targets `git -C <main>` — never the session's own
    worktree. status ∈ healthy | behind | off-main | fixed | blocked."""
    behind = detect_behind_origin(cwd, do_fetch=do_fetch)
    if not behind:
        return {"status": "healthy", "applied": False}     # not behind (or can't tell) -> nothing to do
    if not behind.get("on_default"):
        # behind, but parked on a side branch: returning to the default is return_to_default's job, not a
        # fast-forward of the side branch (catch_up's "no branch switch" invariant). Decline, no mutation.
        return {"status": "off-main", "main": behind["main"], "branch": behind["branch"],
                "current": behind.get("current"), "applied": False}
    main, default, missing = behind["main"], behind["branch"], behind["missing"]
    if not apply:
        return {**behind, "status": "behind", "applied": False}
    # --ff-only is git's OWN refuse-if-not-a-fast-forward guard (the single sanctioned non-additive verb): it
    # advances on a strict ancestor and aborts otherwise, so a diverged branch or a clashing local edit can
    # never be force-merged or clobbered.
    if _ok(["git", "-C", main, "merge", "--ff-only", f"origin/{default}"]):
        return {"status": "fixed", "main": main, "branch": default, "brought_in": missing, "applied": True}
    # git refused: diverged history, or local edits clash with incoming files. Nothing changed, nothing lost.
    return {"status": "blocked", "main": main, "branch": default, "applied": False}


def return_to_default(cwd: str | None = None, apply: bool = False, *, do_fetch: bool = True) -> dict:
    """Point an operator checkout PARKED ON A NON-DEFAULT BRANCH back at its default branch (and bring it
    current), on the operator's consent — the correction for the off-main state (#342). LOSSLESS: returning to a
    NAMED branch never orphans commits (the side branch ref keeps them — no rescue needed, unlike unstrand's
    detached arm), and the switch runs ONLY when the lossless gate is clean (no uncommitted edits, no stash, no
    paused git operation); otherwise it BLOCKS with no mutation, nothing lost. The `git checkout <default>` is
    defensive — never `-f` — so a refusal blocks rather than forces. Having returned, it fast-forwards to
    origin/<default> with the same `--ff-only` proof catch_up uses (best-effort — being safely back on the
    freshly-returned default is already the win). Dry-run (apply=False) reports without mutating. Every mutation
    targets `git -C <main>` — never the session's own worktree. status ∈ healthy | off-main | blocked | fixed."""
    off = detect_off_main(cwd)
    if not off:
        return {"status": "healthy", "applied": False}     # on the default branch (or can't tell) -> nothing
    main, default, current = off["main"], off["main_branch"], off["branch"]
    if not apply:
        return {**off, "status": "off-main", "applied": False}
    lossless, reasons = _is_lossless(main)
    if not lossless:
        # dirty tree / stash / paused op: returning would risk work -> block, no mutation
        return {"status": "blocked", "main": main, "branch": default, "from": current,
                "reasons": reasons, "applied": False}
    if not _ok(["git", "-C", main, "checkout", default]):   # defensive; never -f; a refusal blocks, never forces
        return {"status": "blocked", "main": main, "branch": default, "from": current, "applied": False}
    if do_fetch:
        _run(["git", "-C", main, "fetch", "--quiet", "origin", default], timeout=_FETCH_TIMEOUT)
    # safely back on the default; bring it current with the lossless --ff-only (best-effort, never forced).
    # --ff-only succeeds when it advances OR when already up to date, and fails (no mutation) only when the
    # LOCAL default has itself diverged from origin/<default> — so its result is exactly "is the default now
    # current?". The return already succeeded losslessly; we report the catch-up honestly rather than assume it.
    brought_current = _ok(["git", "-C", main, "merge", "--ff-only", f"origin/{default}"])
    return {"status": "fixed", "main": main, "branch": default, "from": current,
            "brought_current": brought_current, "applied": True}


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
    behind-origin signal can be SEEN firing past the velocity bar and the catch-up bringing it current, all on
    a LOCAL remote (no network, deterministic). Returns the `work` checkout path."""
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
    side branch carrying its own unmerged commit — the wrong-branch park (#342). Returns the `work` path."""
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

    print("\n3) The behind-origin tail (#335) — your folder is FINE, just missing recently-merged work:\n")
    with tempfile.TemporaryDirectory() as tmp:
        work = _behind_fixture(tmp)
        behind = detect_behind_origin(cwd=work, do_fetch=True)
        print("   Before: this folder is on its branch and healthy, but merged updates have landed on the")
        print("   remote that it doesn't have yet. The signal speaks ONLY past the project's own pace (a")
        print("   velocity bar), never on a bare count:")
        print(f"      {behind}")
        result = catch_up(cwd=work, apply=True, do_fetch=False)
        caught_up = detect_behind_origin(cwd=work, do_fetch=True) is None
        print(f"   After bringing it up to date (a safe fast-forward): up to date now? {caught_up} "
              f"(brought in {result.get('brought_in')} merged updates)")

    print("\n4) The 'parked on the wrong branch' state (#342) — your folder is on a side branch, not your main")
    print("   one. Caught on day one, before anything is even missing; the engine offers to point it back —\n")
    with tempfile.TemporaryDirectory() as tmp:
        work = _off_main_fixture(tmp)
        off = detect_off_main(cwd=work)
        print("   Before: this folder is healthy but parked on a side branch instead of its main one. The")
        print("   off-main signal is OFFLINE and fires straight away (no network, no waiting to fall behind):")
        print(f"      {off}")
        feature_sha = (_run(["git", "-C", work, "rev-parse", "my-feature"]) or "").strip()
        result = return_to_default(cwd=work, apply=True, do_fetch=True)
        back_on_main = detect_off_main(cwd=work) is None
        feature_kept = (_run(["git", "-C", work, "rev-parse", "my-feature"]) or "").strip() == feature_sha
        feature_note = _run(["git", "-C", work, "show", "my-feature:my-feature-note.txt"])
        print(f"   After pointing it back: on the main branch now? {back_on_main}; your side-branch work left")
        print(f"   exactly where it was? {feature_kept} — 'my-feature-note.txt' on 'my-feature' still reads: "
              f"{feature_note!r}")

    print("\n5) The plain-language lines the operator sees — a stranded folder, then a behind one (both OFFERS):\n")
    import boot  # lazy: avoids the boot<->checkout_health import cycle (boot is fully loaded by demo time)
    signals = boot.gather_signals()
    signals["strand"] = {"states": ["detached"], "main": "/your/project/folder"}
    signals["behind_origin"] = None   # show the strand line first, alone
    print(boot.render_dashboard(signals))
    print()
    signals["strand"] = None          # then the behind line, alone (synthetic — no live network)
    signals["behind_origin"] = {"state": "behind", "main": "/your/project/folder", "branch": "main",
                                "missing": 9, "latest": "2026-06-27"}
    print(boot.render_dashboard(signals))
    # Self-check: detection separates a healthy folder from the two stranded shapes; the strand repair heals
    # the folder and the at-risk work survives on the rescue branch; the behind tail fires past the bar and the
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


def _plain_catch_up(apply: bool) -> int:
    """The operator-runnable behind-origin CLI over THIS repo's real checkout, in plain words (no git verbs)."""
    r = catch_up(apply=apply)
    if r["status"] == "healthy":
        print("Your project folder is up to date — nothing to bring in.")
    elif r["status"] == "fixed":
        print("Brought your project folder up to date — it now has the recent merged work it was missing.")
    elif r["status"] == "blocked":
        print("Your project folder is behind, but you have unsaved changes that clash with the incoming work, "
              "so I left everything untouched — nothing is lost. Save or set those changes aside and ask again.")
    elif not apply:
        print("Your project folder has fallen behind — it's missing recent merged work. I can bring it up to "
              "date safely; re-run with --apply to do it.")
    else:
        print("I couldn't bring your project folder up to date safely, so I left it untouched — nothing is lost.")
    return 0


def _plain_return_to_default(apply: bool) -> int:
    """The operator-runnable return-to-default CLI over THIS repo's real checkout, in plain words (no git verbs)."""
    r = return_to_default(apply=apply)
    if r["status"] == "healthy":
        print("Your project folder is on your main branch already — nothing to move.")
    elif r["status"] == "fixed" and r.get("brought_current"):
        print("Pointed your project folder back at your main branch and brought it up to date. Your other work "
              "is untouched — it's still saved on its own branch, exactly where it was.")
    elif r["status"] == "fixed":
        print("Pointed your project folder back at your main branch — your other work is untouched, still saved "
              "on its own branch. I left your main branch exactly as it was (it has some local changes of its "
              "own that aren't on the shared copy yet), so it may not be fully up to date.")
    elif r["status"] == "blocked":
        print("Your project folder is parked on another branch, but it has unsaved changes (or a git operation "
              "paused mid-way), so I left everything exactly where it is — nothing moved, nothing lost. Save or "
              "set those aside and ask again.")
    elif not apply:
        parked = r.get("branch")
        where = f"the branch '{parked}'" if parked else "another branch"
        print(f"Your project folder is parked on {where} instead of your main one. I can point it back safely — "
              f"your work there stays saved on that branch. Re-run with --apply to do it.")
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


def _plain_behind() -> int:
    """Report (plain words, no git verbs) whether THIS repo's checkout is missing recent merged work (online)."""
    behind = detect_behind_origin()
    if not behind:
        print("Your project folder is up to date — it's not missing recent merged work (or I'm offline).")
        return 0
    verb = "catchup" if behind.get("on_default") else "returnmain"
    print("Your project folder is missing recent merged work that's landed on the shared copy. Run "
          f"`{verb}` to see how I'd bring it current safely — nothing you already have will be lost.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    if argv and argv[0] == "unstrand":
        return _plain_unstrand(apply="--apply" in argv)
    if argv and argv[0] == "catchup":
        return _plain_catch_up(apply="--apply" in argv)
    if argv and argv[0] == "returnmain":
        return _plain_return_to_default(apply="--apply" in argv)
    if argv and argv[0] == "offmain":
        return _plain_offmain()
    if argv and argv[0] == "behind":
        return _plain_behind()
    result = detect_strand()
    print(result if result else "healthy — no strand detected")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
