#!/usr/bin/env python3
"""license_health — the standing foreign-template-`LICENSE` detector (issue #471).

Catches when the operator's main checkout still carries the engine's OWN template LICENSE at its committed root
— a repo generated before the first-run clear shipped, or one that drifted the seed back into the slot — so
[boot] can OFFER a reviewed removal on the operator's consent. The detect / surface / consent split mirrors the
stranded-checkout detector (`checkout_health`): provisioning detects, boot surfaces, the operator consents.

OFFLINE + READ-ONLY at the core. `detect_foreign_license()` resolves the main checkout from this session's
worktree (`git worktree list` / `--git-common-dir`) and reads the COMMITTED root LICENSE (`HEAD:LICENSE`) —
the file that governs the product and that a reviewed removal changes (an uncommitted working-tree edit is
neither), so the fire/resolved verdict stays honest. It emits NO operator prose (the leaf law keeps git and
license verbs off the operator surface); boot renders the plain-language offer.

The open-removal-PR DEDUPE is a SEPARATE, best-effort, ONLINE step (`removal_pr_open`) — a network round-trip
never sits on the offline detector's critical path (the `checkout_health` offline/online seam).

Fix-never-here: the removal lands as a reviewed pull request the operator merges (build-orchestration's trivial
fast path), never a boot-time delete (D-303/§8). No-op in the engine's OWN template/construction repo, where the
root LICENSE is legitimately the engine's, not a leftover — judged against the EXAMINED checkout's committed
`HEAD:CLAUDE.md` (not this process's repo), so the guard tracks the repo whose LICENSE is being judged.
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import license_seeds  # noqa: E402
import memory_pointer_public_safety_check as _construction  # noqa: E402  (reuse the construction-marker constant)

# The fixed title of the reviewed removal PR — the SHARED CONTRACT between the fix author and this dedupe. The
# boot-session-start repair-offer wiring instructs the assistant to title the cleanup PR EXACTLY this, and
# `removal_pr_open` matches open PRs by it — one source of truth on both sides, so the "opens no duplicate"
# guarantee holds (a `Maintenance:` upkeep change, per build-orchestration's kind grammar). If this string
# changes, the boot-session-start bullet must change with it.
REMOVAL_PR_TITLE = "Maintenance: remove the leftover template LICENSE"

# The construction-governance marker (the engine's own template repo's root CLAUDE.md header, superseded at v1).
# Imported, never restated, so the two guards can never drift apart.
_CONSTRUCTION_MARKER = _construction._CONSTRUCTION_MARKER


def _run(cmd: list, cwd: str | None = None, timeout: int = 30) -> str | None:
    """Run a local git command and return raw stdout, or None on any non-zero / failure. Never raises — every
    read is best-effort (the `checkout_health` convention)."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd)
        return out.stdout if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — a missing binary / timeout / OS error all degrade to "unavailable"
        return None


def _main_checkout(cwd: str | None = None) -> str | None:
    """Resolve the operator's main checkout from this session's worktree, OFFLINE — the path only (we read its
    committed HEAD, so no branch/detached state is needed). The main worktree is listed FIRST by
    `git worktree list --porcelain`; falls back to `--git-common-dir`'s parent. None on a bare repo (no product
    checkout) or when it cannot be resolved."""
    porcelain = _run(["git", "worktree", "list", "--porcelain"], cwd=cwd)
    if porcelain:
        first = porcelain.split("\n\n", 1)[0]   # the first stanza is the main worktree
        path = None
        bare = False
        for line in first.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):].strip()
            elif line.strip() == "bare":
                bare = True
        if bare:
            return None
        if path:
            return path
    common = _run(["git", "rev-parse", "--git-common-dir"], cwd=cwd)
    if common:
        common = common.strip()
        # git may return a path relative to `cwd` (the subprocess dir), NOT this process's cwd — resolve it
        # against `cwd` so the fallback stays correct when the two differ.
        base = cwd or os.getcwd()
        abs_common = common if os.path.isabs(common) else os.path.join(base, common)
        abs_common = os.path.normpath(os.path.abspath(abs_common))
        if os.path.basename(abs_common) == ".git":
            return os.path.dirname(abs_common)
    return None


def _committed(main: str, rel: str) -> str | None:
    """The committed content of `rel` on the main checkout's HEAD, OFFLINE. None when absent (git show fails)."""
    return _run(["git", "-C", main, "show", f"HEAD:{rel}"])


def _is_engine_template_repo(main: str) -> bool:
    """True iff the EXAMINED main checkout is the engine's own template/construction repo — its committed root
    CLAUDE.md carries the construction-governance marker — where the root LICENSE is legitimately the engine's,
    never a leftover to offer removing (the engine==product carve-out). Read from HEAD so it tracks the same
    committed state as the LICENSE read. Unreadable/absent CLAUDE.md -> False (treat as a product repo, so a real
    leftover is still caught; the reviewed-PR consent gate is the backstop against a wrong offer)."""
    text = _committed(main, "CLAUDE.md")
    return bool(text) and _CONSTRUCTION_MARKER in text.lower()


def detect_foreign_license(cwd: str | None = None) -> dict | None:
    """OFFLINE, READ-ONLY. Returns {"present": True, "main": <path>, "fingerprint": <seed id>} when the main
    checkout's committed root LICENSE positively matches one of the engine's OWN historically-shipped template
    seeds; else None — healthy (the product's own license, or no LICENSE), unresolvable, or the engine's own
    template/construction repo. All non-fire paths are fail-soft quiet (never a crash into boot's SessionStart).
    The `fingerprint` is the matched-seed id: stable session-to-session for the same license (so the ledger
    collapse works), changing if the license becomes a DIFFERENT recognizable seed (so a retired finding
    re-surfaces on a new leak)."""
    main = _main_checkout(cwd)
    if main is None:
        return None
    if _is_engine_template_repo(main):
        return None                       # the engine's OWN template repo: its root LICENSE is legitimately ours
    text = _committed(main, "LICENSE")
    if text is None:
        return None                       # no committed LICENSE -> nothing to clear
    fingerprint = license_seeds.matched_seed_id(text)
    if fingerprint is None:
        return None                       # the product's own / anchor-edited license -> preserve, no offer
    return {"present": True, "main": main, "fingerprint": fingerprint}


def removal_pr_open(repo: str | None, token: str | None) -> bool | None:
    """ONLINE, best-effort, READ-ONLY: is a scoped LICENSE-removal PR already open? True / False, or None when it
    can't be determined (no repo/token, offline, or any error) — the caller treats None as "not deduped, offer
    normally". Kept OFF `detect_foreign_license` so a network round-trip never sits on the offline detector's
    critical path (the `checkout_health.detect_behind_origin` offline/online seam). Best-effort like boot's other
    GitHub reads; a miss just re-offers."""
    if not repo or not token:
        return None
    try:
        import telemetry
        gh = telemetry.GitHubIssues(repo, token)
        status, pulls = gh._transport("GET", f"/repos/{repo}/pulls?state=open&per_page=50", None)
        if status >= 400 or not isinstance(pulls, list):
            return None
        return any((p.get("title") or "") == REMOVAL_PR_TITLE for p in pulls)
    except Exception:  # noqa: BLE001 — any read failure degrades this one signal quietly
        return None


# ---- in-tool demo: a self-checking falsification (issue #471) --------------------------------

def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _fixture(tmp: str, name: str, *, license_text: str | None, construction: bool = False) -> str:
    """A throwaway committed git checkout: optional root LICENSE, optional construction CLAUDE.md, one commit."""
    root = os.path.join(tmp, name)
    os.makedirs(root, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    if license_text is not None:
        with open(os.path.join(root, "LICENSE"), "w", encoding="utf-8") as fh:
            fh.write(license_text)
    with open(os.path.join(root, "CLAUDE.md"), "w", encoding="utf-8") as fh:
        fh.write("# construction governance\n" if construction else "# a product\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    return root


def _demo() -> int:
    import tempfile
    seed = license_seeds.CURRENT_SEED
    print("What this proves: the detector fires ONLY on the engine's own template LICENSE left in a product repo,")
    print("and preserves an adopter's own license, an absent license, and the engine's own template repo.\n")
    with tempfile.TemporaryDirectory() as tmp:
        leftover = _fixture(tmp, "leftover", license_text=seed)
        renamed = _fixture(tmp, "renamed", license_text=seed.replace("StarshipSuperjam", "Acme Corp"))
        absent = _fixture(tmp, "absent", license_text=None)
        template = _fixture(tmp, "template", license_text=seed, construction=True)

        d_leftover = detect_foreign_license(cwd=leftover)
        d_renamed = detect_foreign_license(cwd=renamed)
        d_absent = detect_foreign_license(cwd=absent)
        d_template = detect_foreign_license(cwd=template)

        print(f"1) A generated repo still carrying the template LICENSE -> FIRES: {d_leftover is not None} "
              f"(fingerprint {d_leftover and d_leftover['fingerprint']})")
        print(f"2) An adopter who kept the text but renamed the Licensor -> preserved (None): {d_renamed is None}")
        print(f"3) A repo with no LICENSE                               -> preserved (None): {d_absent is None}")
        print(f"4) The engine's OWN template/construction repo          -> no-op (None):     {d_template is None}")

        print("\n5) The plain-language line the operator sees (an OFFER, ranked below the safety alarms):\n")
        import boot  # lazy: boot is fully loaded by demo time
        signals = boot.gather_signals()
        signals["foreign_license"] = {"present": True, "main": "/your/project/folder",
                                      "fingerprint": d_leftover["fingerprint"], "pr_open": False}
        print(boot.render_dashboard(signals))

        # Self-check: the detector separates the four shapes, and boot renders a non-empty offer line for the fire.
        rendered = boot.render_dashboard(signals)
        ok = (d_leftover is not None and d_leftover.get("fingerprint")
              and d_renamed is None and d_absent is None and d_template is None
              and "license" in rendered.lower())
        if not ok:
            print("\nDEMO UNEXPECTED: detection or the boot offer line did not behave as expected.", file=sys.stderr)
            return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    if argv and argv[0] == "check":
        # Read-only detection over THIS repo's real checkout, summarized (never a git verb on the operator surface).
        d = detect_foreign_license()
        if d is None:
            print("No leftover template LICENSE (or this is the engine's own template repo).")
        else:
            print(f"A leftover template LICENSE is present at {d['main']} (fingerprint {d['fingerprint']}).")
        return 0
    print("usage: license_health.py [demo|check]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
