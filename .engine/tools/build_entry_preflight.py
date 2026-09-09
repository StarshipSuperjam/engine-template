"""Observed admission facts for a new Build, separate from continuation and local Build stance.

Network observations happen before ownership locks. The store freezes material identity and the
original observation time; a retry may verify it again but cannot replace it with a different check.
"""
from __future__ import annotations

from pathlib import Path
import os
import re
import subprocess

import build_coordinator_core as core
import moment
import repo_identity


def _git(root, *args):
    try:
        result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True,
                                timeout=45, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise core.CoordinatorError("Build admission git observation failed or timed out; retry the preflight") from exc
    if result.returncode:
        raise core.CoordinatorError("Build admission could not verify git " + args[0] + "; retry the preflight")
    return result.stdout.strip()


def _clean(root):
    if _git(root, "status", "--porcelain"):
        raise core.CoordinatorError("Build admission requires a clean isolated worktree; commit or resolve its changes before binding")
    for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
        if (Path(root) / _git(root, "rev-parse", "--git-path", marker)).exists():
            raise core.CoordinatorError("finish or abort the current git operation before binding a Build")


def verify_frozen(root, observation):
    """Recheck local material immediately before reservation/activation; never fetch under a lock."""
    facts = observation["material"]
    _clean(root)
    if (not repo_identity.slug_eq(repo_identity.origin_slug(str(root)), facts["repository"])
            or _git(root, "rev-parse", "HEAD") != facts["head"]
            or _git(root, "symbolic-ref", "--short", "HEAD") != facts["head_ref"]
            or _git(root, "rev-parse", f"refs/remotes/origin/{facts['target_ref']}") != facts["target_tip"]):
        raise core.CoordinatorError("Build admission inputs changed during preflight; retry without replacing a preparing claim")


def observe_fresh(root, repository, number, pr):
    """Fetch the exact PR target from verified origin and refuse stale work, never rebase it."""
    _clean(root)
    if not repo_identity.slug_eq(repo_identity.origin_slug(str(root)), repository):
        raise core.CoordinatorError("Build admission repository does not match verified origin")
    head, branch = _git(root, "rev-parse", "HEAD"), _git(root, "symbolic-ref", "--short", "HEAD")
    target = pr.get("baseRefName")
    head_repo = (pr.get("headRepository") or {}).get("nameWithOwner")
    if not head_repo:
        owner = (pr.get("headRepositoryOwner") or {}).get("login")
        name = (pr.get("headRepository") or {}).get("name")
        head_repo = f"{owner}/{name}" if owner and name else None
    if (pr.get("number") != number or pr.get("state") != "OPEN" or pr.get("isDraft") is not True
            or pr.get("headRefOid") != head or pr.get("headRefName") != branch
            or not head_repo or not repo_identity.slug_eq(head_repo, repository)
            or not isinstance(target, str) or not target or target.startswith("-")):
        raise core.CoordinatorError("Build admission requires a verified draft PR, head repository/ref and target ref matching this worktree")
    _git(root, "check-ref-format", "refs/heads/" + target)
    remote_head = _git(root, "ls-remote", "--symref", "origin", "HEAD")
    if f"ref: refs/heads/{target}\tHEAD" not in remote_head.splitlines():
        raise core.CoordinatorError("the PR target is not origin's verified default branch; correct the draft PR base before binding")
    _git(root, "fetch", "--no-tags", "origin", f"+refs/heads/{target}:refs/remotes/origin/{target}")
    tip = _git(root, "rev-parse", "--verify", f"refs/remotes/origin/{target}^{{commit}}")
    if not re.fullmatch(r"[0-9a-f]{40}", tip) or pr.get("baseRefOid") != tip:
        raise core.CoordinatorError("the fetched target and PR base differ; refresh the PR observation and retry admission")
    if core.run(["git", "merge-base", "--is-ancestor", tip, head], root=Path(root)).returncode:
        raise core.CoordinatorError(f"fresh origin/{target} is not contained in this branch; rebase the isolated unbound branch onto origin/{target}, push it, then retry plan bind")
    observed = {"observed_at": moment.utc_now(), "material": {
        "repository": repository, "pr": number, "head_repository": head_repo, "head_ref": branch,
        "head": head, "target_repository": repository, "target_ref": target, "target_tip": tip}}
    verify_frozen(root, observed)
    return observed


def material_digest(observation):
    return core.digest(observation["material"])
