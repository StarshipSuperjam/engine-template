#!/usr/bin/env python3
"""Session-start automatic catch-up and its reviewed operator preference.

Only boot calls :func:`automatic_catch_up`.  It is intentionally narrower than
``checkout_health.catch_up``: it may fast-forward one clean operator checkout already on the
freshly verified remote default branch, and it never switches a branch, rescues work, reconciles
dirty-but-subsumed files, repairs a strand, or resolves divergence.  All of those remain consented
manual recovery paths in ``checkout_health``.

The committed, deployment-owned preference is ``.engine/operator-checkout.json``.  Its absence
means the shipped default (enabled); the sole accepted explicit form is
``{"automatic_catch_up": false}`` or ``{"automatic_catch_up": true}``.  Any other present,
unreadable, or malformed file fails closed and lets boot give the operator a repair notice.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checkout_health  # noqa: E402
import repo_identity  # noqa: E402  (the shared default-branch reader for the temporary review worktree)
import tune  # noqa: E402  (reviewed configuration PR transport)


CONFIG_REL = os.path.join(".engine", "operator-checkout.json")
_KEY = "automatic_catch_up"


def preference_path(cwd: str | None = None) -> str | None:
    """The operator main checkout's preference path, never this session worktree's copy."""
    resolved = checkout_health._main_checkout(cwd)
    return os.path.join(resolved[0], CONFIG_REL) if resolved else None


def load_preference(cwd: str | None = None, *, path: str | None = None) -> dict:
    """Read the strict three-state preference without treating invalid data as the default."""
    path = path or preference_path(cwd)
    if not path:
        return {"state": "invalid", "reason": "checkout-unresolved", "path": None}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {"state": "enabled", "source": "default", "path": path}
    except json.JSONDecodeError:
        return {"state": "invalid", "reason": "invalid-json", "path": path}
    except OSError:
        return {"state": "invalid", "reason": "unreadable", "path": path}
    if not isinstance(raw, dict):
        return {"state": "invalid", "reason": "not-an-object", "path": path}
    if set(raw) != {_KEY}:
        return {"state": "invalid", "reason": "unexpected-shape", "path": path}
    value = raw.get(_KEY)
    if type(value) is not bool:  # bool is intentionally exact; truthy JSON values never opt in/out.
        return {"state": "invalid", "reason": "not-a-boolean", "path": path}
    return {"state": "enabled" if value else "disabled", "source": "configured", "path": path}


def _atomic_write(path: str, enabled: bool) -> None:
    """Replace the small preference atomically; a failed write never leaves truncated JSON behind."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".operator-checkout-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({_KEY: enabled}, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _pr_body(enabled: bool) -> str:
    choice = "enabled" if enabled else "disabled"
    return (
        "You used `/engine-setup` to change automatic project-folder updates. This pull request saves your "
        f"choice: automatic catch-up is **{choice}**.\n\n"
        "Merging this is what makes the choice take effect. The preference is preserved when the Engine is "
        "upgraded, and it only controls the local project folder; it never pushes, merges, or changes GitHub.\n")


def _staging_worktree(root: str) -> tuple[str | None, str | None]:
    """Create a detached default-branch worktree for a preference PR, never changing the live checkout."""
    branch = repo_identity.resolve_default_branch(root)
    base = (checkout_health._run(["git", "-C", root, "rev-parse", "--verify", f"refs/heads/{branch}"])
            or checkout_health._run(["git", "-C", root, "rev-parse", "--verify",
                                     f"refs/remotes/origin/{branch}"]))
    if not base:
        return None, "the verified default branch is not available locally"
    worktree = tempfile.mkdtemp(prefix="engine-checkout-preference-")
    try:
        os.rmdir(worktree)  # Git worktree add requires a path it can create itself.
        result = subprocess.run(["git", "-C", root, "worktree", "add", "--detach", worktree, base.strip()],
                                capture_output=True, text=True, check=False)
        if result.returncode:
            return None, result.stderr.strip() or "Git could not create the review worktree"
        return worktree, None
    except OSError as exc:
        return None, str(exc)


def _remove_staging_worktree(root: str, worktree: str) -> None:
    """Remove the disposable, clean review worktree; a cleanup failure never changes live preference state."""
    try:
        subprocess.run(["git", "-C", root, "worktree", "remove", worktree],
                       capture_output=True, text=True, check=False)
    except OSError:
        pass


def set_preference(enabled: bool, cwd: str | None = None, *, path: str | None = None,
                   opener=tune._open_tune_pr, open_pr: bool = True) -> dict:
    """Propose an explicit choice from a disposable worktree, so only a merged reviewed PR activates it."""
    path = path or preference_path(cwd)
    if not path:
        return {"ok": False, "message": "I couldn't find this project's main folder, so nothing was changed.",
                "pr": None}
    if not open_pr or opener is None:
        return {"ok": True, "message": "Practice run only — no preference was saved without a reviewed pull request.",
                "pr": None}
    root = os.path.dirname(os.path.dirname(path))
    relpath = CONFIG_REL.replace(os.sep, "/")
    review_root, reason = _staging_worktree(root)
    if not review_root:
        return {"ok": False, "message": f"I couldn't prepare the reviewed preference change ({reason}). Nothing changed.",
                "pr": None}
    try:
        _atomic_write(os.path.join(review_root, CONFIG_REL), enabled)
        pr = opener(branch="engine-checkout-auto-update", title="Maintenance: set automatic checkout updates",
                    body=_pr_body(enabled), paths=[relpath], cwd=review_root)
    except Exception as exc:  # noqa: BLE001 — only the disposable review worktree was written.
        return {"ok": False, "message": f"The preference pull request could not be opened: {exc}. Nothing changed.",
                "pr": None}
    finally:
        _remove_staging_worktree(root, review_root)
    return {"ok": True,
            "message": ("I've prepared your choice as a pull request — merge it to make it take effect. "
                        "Nothing changes until you do."), "pr": pr}


def _current_snapshot(snapshot: dict) -> dict:
    """The exact assessed target has just been materialized, so boot need not fetch a second time."""
    return {**snapshot, "state": "current", "presentation": "current", "current": snapshot["branch"],
            "on_default": True, "head_oid": snapshot["target_oid"],
            "default_oid": snapshot["target_oid"], "behind_commits": 0, "missing_merges": 0}


def _normalise_peer_winner(cwd: str | None, result: dict) -> dict:
    """A sibling boot may win the named-ref CAS; a fresh current reread is a benign result, not a failure."""
    if result.get("reason") not in {"checkout-changed", "clash"}:
        return result
    observed = checkout_health.checkout_snapshot(cwd)
    if observed.get("state") == "current":
        return {"status": "current", "snapshot": observed, "peer_updated": True}
    return result


def automatic_catch_up(cwd: str | None = None) -> dict:
    """One boot-only automatic attempt.  Every refusal returns a structured outcome and leaves recovery manual."""
    preference = load_preference(cwd)
    if preference["state"] == "invalid":
        return {"status": "invalid-config", "preference": preference}
    if preference["state"] == "disabled":
        # Boot still takes its ordinary fresh health snapshot afterwards, so drift and the consented action remain.
        return {"status": "disabled", "preference": preference}

    snapshot = checkout_health.checkout_snapshot(cwd)
    if snapshot.get("state") == "unavailable":
        return {"status": "unavailable", "snapshot": snapshot}
    if snapshot.get("state") == "current":
        return {"status": "current", "snapshot": snapshot}
    if not snapshot.get("on_default"):
        return {"status": "blocked", "reason": "off-main", "snapshot": snapshot}
    if not checkout_health._succeeds(["git", "-C", snapshot["main"], "merge-base", "--is-ancestor",
                                      snapshot["head_oid"], snapshot["target_oid"]]):
        return {"status": "blocked", "reason": "diverged", "snapshot": snapshot}
    safe, reasons = checkout_health._is_lossless(snapshot["main"])
    if not safe:
        return {"status": "blocked", "reason": "local-work", "reasons": reasons, "snapshot": snapshot}

    applied = checkout_health._advance_clean_default_snapshot(snapshot, protect_head=True)
    if applied.get("status") == "fixed":
        return {"status": "updated", "update": applied, "snapshot": _current_snapshot(snapshot)}
    normal = _normalise_peer_winner(cwd, {"status": "blocked", "reason": applied.get("reason"),
                                          "snapshot": snapshot, "update": applied})
    return normal


def _show(preference: dict) -> str:
    if preference["state"] == "enabled":
        return ("Automatic project-folder updates are on. At session start, the Engine can fast-forward only a "
                "clean folder already on the verified remote default branch.")
    if preference["state"] == "disabled":
        return ("Automatic project-folder updates are off. The Engine still tells you when shared work is "
                "available and offers the usual **bring it up to date** action.")
    path = preference.get("path") or CONFIG_REL
    return (f"Automatic project-folder updates are paused because `{path}` could not be read safely "
            f"({preference.get('reason')}). Use `/engine-setup` to save a new on/off choice; nothing updates "
            "automatically until then.")


def _demo_git(args: list[str], *, cwd: str | None = None) -> str:
    """Run the self-contained demonstration's Git setup, failing loudly if the fixture cannot be made."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git failure"
        raise RuntimeError(f"demo setup failed: git {' '.join(args)}: {detail}")
    return result.stdout.strip()


def _demo() -> int:
    """Prove the automatic path on a disposable local origin and clone, never an operator checkout."""
    print("Automatic checkout catch-up — disposable local repository demonstration\n")
    with tempfile.TemporaryDirectory(prefix="engine-auto-catch-up-") as temporary:
        origin = os.path.join(temporary, "origin.git")
        author = os.path.join(temporary, "author")
        operator = os.path.join(temporary, "operator")
        _demo_git(["init", "--bare", "--initial-branch=main", origin])
        _demo_git(["init", "--initial-branch=main", author])
        _demo_git(["config", "user.email", "demo@example.invalid"], cwd=author)
        _demo_git(["config", "user.name", "Engine demonstration"], cwd=author)
        os.makedirs(os.path.join(author, ".claude"))
        os.makedirs(os.path.join(author, ".engine"))
        with open(os.path.join(author, ".claude", "settings.json"), "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        with open(os.path.join(author, ".engine", "marker"), "w", encoding="utf-8") as fh:
            fh.write("fixture\n")
        with open(os.path.join(author, "shared.txt"), "w", encoding="utf-8") as fh:
            fh.write("first shared version\n")
        _demo_git(["add", "."], cwd=author)
        _demo_git(["commit", "-m", "initial Engine checkout"], cwd=author)
        _demo_git(["remote", "add", "origin", origin], cwd=author)
        _demo_git(["push", "-u", "origin", "main"], cwd=author)
        _demo_git(["--git-dir", origin, "symbolic-ref", "HEAD", "refs/heads/main"])
        _demo_git(["clone", origin, operator])

        # Land a shared commit after the operator clone is made: it is clean, on the verified default, and behind.
        with open(os.path.join(author, "shared.txt"), "w", encoding="utf-8") as fh:
            fh.write("latest shared version\n")
        _demo_git(["add", "shared.txt"], cwd=author)
        _demo_git(["commit", "-m", "shared update"], cwd=author)
        target = _demo_git(["rev-parse", "HEAD"], cwd=author)
        _demo_git(["push", "origin", "main"], cwd=author)
        before = _demo_git(["rev-parse", "HEAD"], cwd=operator)
        result = automatic_catch_up(cwd=operator)
        after = _demo_git(["rev-parse", "HEAD"], cwd=operator)
        clean = _demo_git(["status", "--porcelain"], cwd=operator) == ""

        print(f"  Before: {before}")
        print(f"  Assessed remote default target: {target}")
        print(f"  Controller outcome: {result.get('status')}")
        print(f"  After: {after}")
        print(f"  Proof — exact target materialized and working tree clean: {after == target and clean}")
        return 0 if result.get("status") == "updated" and after == target and clean else 1


def main(argv: list[str]) -> int:
    if not argv or argv[0] == "show":
        print(_show(load_preference()))
        return 0
    if argv[0] in {"enable", "disable"}:
        result = set_preference(argv[0] == "enable")
        print(result["message"])
        return 0 if result["ok"] else 1
    if argv[0] == "demo":
        return _demo()
    print("usage: checkout_auto_update.py [show | enable | disable | demo]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
