#!/usr/bin/env python3
"""Operator-checkout health — detect a stranded operator checkout AND offer a lossless un-stranding fix (#80).

The [operator checkout](glossary) — the top-level project folder the operator opens — is meant to sit on
its branch with the engine files present; build runs in per-session worktrees, never in it (the
never-strand-main floor, realized in CLAUDE.deployed.md). When it is **stranded** anyway — a detached
`HEAD`, or missing engine files — this module (a) DETECTS it offline+read-only so [boot](boot.py) can surface
it (slice B), and (b) on the operator's consent, REPAIRS it (slice C). Provisioning owns this mechanism; boot
invokes the detector in its SessionStart pack and OFFERS the fix; the assistant runs the fix only when the
operator says yes. The fix is the deployed-floor never-strand-main rule's ONE sanctioned write to the operator
checkout.

Design (systems/infrastructure/provisioning/README.md §"Operator-checkout strand"; D-189/D-190):
  - From the session's worktree, resolve the main checkout (`git worktree list` — main listed FIRST — with
    `--git-common-dir` as a fallback) and read its state LOCALLY.
  - **Two binary states, checked every boot, OFFLINE:** a detached `HEAD`; missing engine files
    (`.claude/settings.json`, `.engine/`). Ordinary *behind-origin* is the NORMAL state of the main and is
    **never** alarmed; the behind-origin fast-forward is the opt-in, network-gated tail — DEFERRED (this module
    does NO fast-forward / `--ff-only`).
  - **The fix is lossless-or-it-does-not-run.** Safe iff `git -C <main> rev-list HEAD --not --branches` empty
    AND `stash list` empty AND `status --porcelain` clean — decided OFFLINE. The ONLY git mutations are
    ADDITIVE-or-post-rescue: `checkout -b` (create a ref), `commit` onto a FRESH rescue branch (saves work),
    `checkout <branch>` (only after at-risk work is rescued), and per-path `checkout HEAD -- <absent path>`
    (restore ONLY currently-absent tracked files). It **NEVER** runs `reset` / `clean` / `checkout -f` /
    `stash drop` / `push` / `--ff-only` / any force flag (test_checkout_health source-scans for these).
    Losslessness is a property of THESE operations — a prior `reset`'s reflog-only orphans are git's reflog's,
    not this fix's. When it cannot safely tell which branch to re-attach to, it **REFUSES** (no mutation)
    rather than guess.
  - **Fail-soft = quiet** (detection): any git error / unresolvable main returns None (no strand surfaced) — a
    stranded local checkout cannot reach the protected branch, so it degrades quietly; the double-fault is the
    boot floor's present-marker backstop.

No operator prose lives in the detector's return value (`{"states": [...], "main": <path>}`) — boot renders the
plain-language line (the leaf law keeps git verbs off the operator surface). The fix returns a structured
result the runbook/boot relay in plain words.

CLI:  python tools/checkout_health.py            # classify THIS repo's main checkout (signal or "healthy")
      python tools/checkout_health.py unstrand   # dry-run: what the fix WOULD do (no mutation)
      python tools/checkout_health.py unstrand --apply   # repair THIS repo's checkout (only if stranded)
      python tools/checkout_health.py demo       # detection + a lossless-repair walkthrough on throwaway fixtures
"""
from __future__ import annotations

import os
import subprocess
import sys

# The engine files whose absence marks a checkout stranded (provisioning README: the two binary states).
_ENGINE_FILES = (os.path.join(".claude", "settings.json"), ".engine")

# The fix's rescue branch (a "safe point" in operator words) + an inline identity so the rescue commit never
# fails for lack of a configured git user on the operator's checkout.
_RESCUE_PREFIX = "engine-rescue"
_RESCUE_IDENT = ["-c", "user.email=engine@local", "-c", "user.name=engine"]


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


def detect_strand(cwd: str | None = None) -> dict | None:
    """Classify the operator's main checkout as stranded or not — OFFLINE, READ-ONLY. Returns None when
    healthy (on a branch AND both engine files present) OR when the check cannot run (fail-soft quiet).
    A strand returns {"states": [...], "main": <path>} with one or both of "detached" / "missing-files"."""
    resolved = _main_checkout(cwd)
    if not resolved:
        return None
    main, detached = resolved
    states: list[str] = []
    if detached:
        states.append("detached")
    if any(not os.path.exists(os.path.join(main, rel)) for rel in _ENGINE_FILES):
        states.append("missing-files")
    if not states:
        return None
    return {"states": states, "main": main}


# ---- the un-stranding fix: lossless-or-it-does-not-run (issue #80, slice C) ------------------

def _is_lossless(main: str) -> tuple[bool, list[str]]:
    """OFFLINE: can a strand be fixed without first rescuing? Safe iff no commit sits on no branch AND no
    stash AND a clean working tree (provisioning L473-474). Returns (safe, reasons)."""
    reasons: list[str] = []
    if (_run(["git", "-C", main, "rev-list", "HEAD", "--not", "--branches"]) or "").strip():
        reasons.append("off-branch-commits")   # committed work reachable from no branch (detached work)
    if (_run(["git", "-C", main, "stash", "list"]) or "").strip():
        reasons.append("stash")
    if (_run(["git", "-C", main, "status", "--porcelain"]) or "").strip():
        reasons.append("uncommitted")
    return (not reasons), reasons


def _default_branch(main: str) -> str | None:
    """The branch to re-attach a detached HEAD to, resolved OFFLINE: origin/HEAD's target, else a local
    main/master, else the sole local branch. None when it cannot be safely determined — the fix then REFUSES
    rather than move HEAD to a guessed branch."""
    head = _run(["git", "-C", main, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if head and head.strip():
        ref = head.strip()
        return ref.split("origin/", 1)[1] if ref.startswith("origin/") else ref
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
    with tempfile.TemporaryDirectory() as tmp:
        for name, label, kw in (
            ("healthy", "a healthy folder (on its branch, engine files present)", {"detach": False, "drop_settings": False}),
            ("detached", "a folder stuck off its branch (detached HEAD)", {"detach": True, "drop_settings": False}),
            ("missing", "a folder missing the engine's files", {"detach": False, "drop_settings": True})):
            print(f"  • {label}:\n      {detect_strand(cwd=_fixture(tmp, name, **kw))}")

    print("\n2) The REPAIR, on a throwaway example folder (never your real one):\n")
    with tempfile.TemporaryDirectory() as tmp:
        root = _stranded_with_at_risk_work(tmp)
        print("   Before: this folder is stuck off its branch, and it holds work — the file")
        print("   'my-important-note.txt' (\"DO NOT LOSE THIS\") — that is on NO branch. Re-attaching the")
        print("   normal way would leave that work behind. Watch where it goes.\n")
        print(f"   What I'd do, in plain terms: {_plan_words(unstrand(cwd=root)['plan'])}.")
        result = unstrand(cwd=root, apply=True)
        print(f"   After the repair: folder healthy now? {detect_strand(cwd=root) is None}")
        print(f"   I saved your at-risk work first to the safe point (a rescue branch): {result.get('rescue')}")
        note = _run(["git", "-C", root, "show", f"{result['rescue']}:my-important-note.txt"])
        print(f"   Proof it survived — 'my-important-note.txt' on the safe point still reads: {note!r}")

    print("\n3) The plain-language line the operator sees when their folder is stranded (now an OFFER):\n")
    import boot  # lazy: avoids the boot<->checkout_health import cycle (boot is fully loaded by demo time)
    signals = boot.gather_signals()
    signals["strand"] = {"states": ["detached"], "main": "/your/project/folder"}
    print(boot.render_dashboard(signals))
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


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    if argv and argv[0] == "unstrand":
        return _plain_unstrand(apply="--apply" in argv)
    result = detect_strand()
    print(result if result else "healthy — no strand detected")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
