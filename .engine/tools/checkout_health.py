#!/usr/bin/env python3
"""Operator-checkout health — detect a stranded operator checkout (issue #80, slice B).

The [operator checkout](glossary) — the top-level project folder the operator opens — is meant to sit on
its branch with the engine files present; build runs in per-session worktrees, never in it (the
never-strand-main floor, realized in CLAUDE.deployed.md). When it is **stranded** anyway — a detached
`HEAD`, or missing/critically-stale engine files — this detector notices it, OFFLINE and READ-ONLY, so
[boot](boot.py) can surface it. Provisioning owns this mechanism; boot invokes it in its SessionStart pack
and relays the result (boot computes no new state). The operator-consented *un-stranding fix* — the only
write to the operator checkout — is the NEXT slice (C); this slice detects and surfaces only.

Design (systems/infrastructure/provisioning/README.md §"Operator-checkout strand"; D-189/D-190):
  - From the session's worktree, resolve the main checkout (`git worktree list` — the main worktree is
    listed FIRST — with `--git-common-dir` as a fallback) and read its state LOCALLY.
  - **Two binary states, checked every boot, OFFLINE:** a detached `HEAD`; missing engine files
    (`.claude/settings.json`, `.engine/`).
  - Ordinary *behind-origin* is the NORMAL state of the main under the worktree-and-PR model, so it is
    **never** alarmed on a bare distance; a behind-origin rot signal needs a network fetch and is an opt-in,
    consequence-gated tail — DEFERRED (not in this offline path).
  - **Fail-soft = quiet.** Any git error / unresolvable main / unreadable tree returns None (no strand
    surfaced). A strand-check that cannot run is low-stakes — a stranded local checkout cannot reach the
    protected branch — so, unlike boot's governance reads, it degrades QUIETLY rather than nagging a
    "couldn't check your folder" line every boot. The double-fault (no runtime at all) is the boot floor's
    present-marker backstop, not this detector's.

No operator prose lives here: the detector returns a relay-ready structured signal (`{"states": [...],
"main": <path>}`) and boot renders the plain-language line (the leaf law keeps git verbs off the operator
surface).

CLI:  python tools/checkout_health.py        # classify THIS repo's main checkout (prints the signal or "healthy")
      python tools/checkout_health.py demo   # classify synthetic healthy / detached / missing-files fixtures,
                                             #   then print the operator-facing dashboard line a strand surfaces
"""
from __future__ import annotations

import os
import subprocess
import sys

# The engine files whose absence marks a checkout stranded (provisioning README: the two binary states).
_ENGINE_FILES = (os.path.join(".claude", "settings.json"), ".engine")


def _run(cmd: list, cwd: str | None = None, timeout: int = 10) -> str | None:
    """Run a local git command and return raw stdout, or None on any failure. Never raises — every read is
    best-effort and degrades. Stdout is returned UNSTRIPPED so `--porcelain` stanza structure is preserved."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd)
        return out.stdout if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — a missing binary / timeout / OS error all degrade to "unavailable"
        return None


def _main_checkout(cwd: str | None = None) -> tuple[str, bool] | None:
    """Resolve the operator's main checkout from this session's worktree, OFFLINE. Returns
    (main_path, is_detached) read straight from `git worktree list --porcelain` — the main worktree is
    listed FIRST by git, and its stanza carries `detached` (vs `branch refs/heads/...`), so no second git
    call is needed. Falls back to `--git-common-dir`'s parent when porcelain is unavailable (then the
    detached state is read with one extra call). None when the main checkout cannot be resolved at all."""
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
    A strand returns {"states": [...], "main": <path>} with one or both of "detached" / "missing-files" —
    a relay-ready signal; the plain-language line is boot's to render."""
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


# ---- the operator-runnable demo (synthetic fixtures; deterministic) -------------------------

def _fixture(tmp: str, name: str, *, detach: bool, drop_settings: bool) -> str:
    """Build a throwaway git repo fixture so the detector can be SEEN classifying it — no live alarm needed
    (the real construction-repo checkout is healthy). Mirrors the 27d collision-check fixture pattern."""
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


def _demo() -> int:
    import tempfile
    print("What checkout_health detects — whether your top-level project folder is healthy or stranded:\n")
    with tempfile.TemporaryDirectory() as tmp:
        for name, label, kw in (
            ("healthy", "a healthy folder (on its branch, engine files present)", {"detach": False, "drop_settings": False}),
            ("detached", "a folder stuck off its branch (detached HEAD)", {"detach": True, "drop_settings": False}),
            ("missing", "a folder missing the engine's files", {"detach": False, "drop_settings": True})):
            root = _fixture(tmp, name, **kw)
            print(f"  • {label}:\n      {detect_strand(cwd=root)}")
    # Show the actual operator-facing line a strand surfaces (the words the operator would see), rendered
    # through boot's real dashboard so the demo proves the COPY, not just the internal signal.
    import boot  # lazy: avoids the boot<->checkout_health import cycle (boot is fully loaded by demo time)
    print("\nAnd here is the plain-language line the operator would see when their folder is stranded:\n")
    signals = boot.gather_signals()
    signals["strand"] = {"states": ["detached"], "main": "/your/project/folder"}
    print(boot.render_dashboard(signals))
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    result = detect_strand()
    print(result if result else "healthy — no strand detected")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
