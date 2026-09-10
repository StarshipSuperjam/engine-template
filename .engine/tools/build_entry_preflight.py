"""Observed admission facts for a new Build, separate from continuation and local Build stance.

Network observations happen before ownership locks. The store freezes material identity and the
original observation time; a retry may verify it again but cannot replace it with a different check.
"""
from __future__ import annotations

from pathlib import Path
import os
import re
import subprocess
import json
import time

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


def issue_numbers(plan, explicit_issue, pr, repository):
    """Only structured issue authority selects the Build's issue set; prose is not plan authority."""
    selected = {n for n in (explicit_issue, (plan.get("intent_source") or {}).get("issue"))
                if isinstance(n, int) and not isinstance(n, bool) and n > 0}
    references = pr.get("closingIssuesReferences")
    if not isinstance(references, list):
        raise core.CoordinatorError("the draft PR's structured closing issues could not be observed; refresh its metadata before admission")
    for item in references:
        if not isinstance(item, dict) or not isinstance(item.get('url'), str):
            raise core.CoordinatorError("the draft PR's closing issue metadata is incomplete")
        url = item.get("url", "")
        match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/issues/([1-9][0-9]*)", url)
        if not match or (item.get('number') is not None and item['number'] != int(match[2])):
            raise core.CoordinatorError("the draft PR's closing issue identity is incomplete or contradictory")
        if repo_identity.slug_eq(match[1], repository):
            selected.add(int(match[2]))
    return sorted(selected)


def mentioned_issues(text, repository):
    """Read local, qualified and linked references without treating a foreign issue as local."""
    found = set()
    for repo, number in re.findall(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/([1-9][0-9]*)", text):
        if repo_identity.slug_eq(repo, repository):
            found.add(int(number))
    # A Markdown label belongs to its link target, not to the surrounding repository.
    text = re.sub(r"\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"https?://\S+", "", text)
    for repo, number in re.findall(r"(?<![\w/])([\w.-]+/[\w.-]+)#([1-9][0-9]*)", text):
        if repo_identity.slug_eq(repo, repository):
            found.add(int(number))
    found.update(int(n) for n in re.findall(r"(?<![\w/])#([1-9][0-9]*)\b", text))
    return found


def _open_prs(root, repository):
    try:
        deadline = time.monotonic() + 45
        result = subprocess.run(["gh", "api", "--paginate", "--slurp",
            f"repos/{repository}/pulls?state=open&per_page=100"], cwd=root, text=True,
            capture_output=True, timeout=45, env=dict(os.environ, GH_PROMPT_DISABLED="1"))
        if result.returncode:
            raise ValueError("request failed")
        pages = json.loads(result.stdout)
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise ValueError("unexpected pagination envelope")
        rows = [row for page in pages for row in page]
        if any(not isinstance(row, dict) or not isinstance(row.get("number"), int)
               or not isinstance(row.get("head"), dict) or 'body' not in row or 'title' not in row for row in rows):
            raise ValueError("incomplete PR observation")
        owner, name = repository.split("/", 1)
        query = """query($owner:String!,$name:String!,$number:Int!,$endCursor:String) {
          repository(owner:$owner,name:$name) { pullRequest(number:$number) {
            closingIssuesReferences(first:100,after:$endCursor) {
              nodes { number url } pageInfo { hasNextPage endCursor }
            }
          } }
        }"""
        for row in rows:
            # REST's PR list omits sidebar-linked issues. Page the structured connection too,
            # within one total observation budget; missing pages are never an empty issue set.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("PR issue observation timed out")
            linked = subprocess.run(["gh", "api", "graphql", "--paginate", "--slurp",
                "-f", "query=" + query, "-f", "owner=" + owner, "-f", "name=" + name,
                "-F", "number=" + str(row["number"])], cwd=root, text=True,
                capture_output=True, timeout=remaining, env=dict(os.environ, GH_PROMPT_DISABLED="1"))
            if linked.returncode:
                raise ValueError("PR issue observation failed")
            connections = json.loads(linked.stdout)
            if not isinstance(connections, list) or not connections:
                raise ValueError("missing PR issue pages")
            refs = []
            cursors = set()
            for index, page in enumerate(connections):
                if not isinstance(page, dict) or page.get("errors"):
                    raise ValueError("failed PR issue page")
                connection = page["data"]["repository"]["pullRequest"]["closingIssuesReferences"]
                info = connection["pageInfo"]
                more = info["hasNextPage"]
                if not isinstance(more, bool) or more != (index < len(connections) - 1):
                    raise ValueError("incomplete PR issue pagination")
                if more:
                    cursor = info.get("endCursor")
                    if not isinstance(cursor, str) or not cursor or cursor in cursors:
                        raise ValueError("invalid PR issue cursor")
                    cursors.add(cursor)
                nodes = connection["nodes"]
                if not isinstance(nodes, list):
                    raise ValueError("invalid PR issue nodes")
                refs.extend(nodes)
            row["closingIssuesReferences"] = refs
            issue_numbers({}, None, row, repository)  # Validate every identity before claiming coverage.
        return rows
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, core.CoordinatorError) as exc:
        raise core.CoordinatorError("open pull requests could not be completely observed") from exc


def local_overlap(library, repository, issues, *, identity=None, candidate=None, worktree=None):
    """Nonterminal leases count as occupied even before a snapshot exists."""
    matches, errors = [], []
    try:
        slugs = library.slugs()
    except Exception:
        return [], ["local plan claims could not be enumerated"]
    for slug in slugs:
        try:
            record = library.read_record(slug)
            claim = (record.get("build_lease") or {}).get("current")
            if not claim:
                old = record.get("build_binding")
                if old and not record.get("closure") and repo_identity.slug_eq(old.get("repository"), repository):
                    errors.append(f"legacy ownership cannot be completely observed: {slug}")
                continue
            if claim["state"] not in ("preparing", "active", "transferring", "retiring"):
                continue
            if identity and {k: claim[k] for k in ("build_id", "generation")} == identity:
                continue
            if not repo_identity.slug_eq(claim.get("repository"), repository):
                continue
            if ((candidate and claim["pull_request"] == candidate["pr"])
                    or (worktree and Path(claim["worktree"]).resolve() == Path(worktree).resolve())):
                # A decision about overlapping issues is never permission to share ownership.
                raise _OwnershipConflict(f"another Build claim owns this PR or worktree: {claim['build_id']}; resume or retire its owner")
            if not issues:
                continue
            material = (claim.get("admission") or {}).get("material", {})
            claimed = set(material.get("issues", []))
            if claim.get("authorizing_issue"):
                claimed.add(claim["authorizing_issue"])
            source = ((library.head(slug).get("build_plan") or {}).get("intent_source") or {})
            if source.get("kind") == "issue":
                claimed.add(source["issue"])
            for issue in sorted(set(issues) & claimed):
                matches.append(f"local:{repository.lower()}#{issue}:{claim['build_id']}:g{claim['generation']}")
        except _OwnershipConflict:
            raise
        except Exception:
            errors.append(f"local claim could not be completely observed: {slug}")
    return sorted(set(matches)), sorted(set(errors))


class _OwnershipConflict(core.CoordinatorError):
    pass


def overlap_observation(root, library, repository, issues, *, identity=None, candidate=None, worktree=None):
    local, errors = local_overlap(library, repository, issues, identity=identity,
                                 candidate=candidate, worktree=worktree)
    if not issues:
        errors = []  # There is no issue-overlap question; known ownership conflicts already refused.
    local_digest = core.digest({"matches": local, "errors": errors})
    matches = list(local)
    if issues:
        try:
            for pr in _open_prs(root, repository):
                head = pr["head"]
                if (candidate and pr["number"] == candidate["pr"]
                        and head.get("ref") == candidate["head_ref"]
                        and head.get("sha") == candidate["head"]
                        and repo_identity.slug_eq((head.get("repo") or {}).get("full_name"), candidate["head_repository"])):
                    continue
                referenced = set(issue_numbers({}, None, pr, repository))
                referenced.update(mentioned_issues((pr.get("title") or "") + "\n" + (pr.get("body") or ""), repository))
                for issue in sorted(set(issues) & referenced):
                    matches.append(f"pr:{repository.lower()}#{pr['number']}:issue#{issue}")
        except core.CoordinatorError as exc:
            errors.append(str(exc))
        try:
            for line in _git(root, "ls-remote", "--heads", "origin").splitlines():
                fields = line.split()
                if len(fields) != 2 or not fields[1].startswith("refs/heads/"):
                    raise core.CoordinatorError("remote branch observation was incomplete")
                branch = fields[1][len("refs/heads/"):]
                if (candidate and branch == candidate["head_ref"] and fields[0] == candidate["head"]
                        and repo_identity.slug_eq(candidate["head_repository"], repository)):
                    continue
                match = re.match(r"^(?:claude|codex)/(?:issue[-_/])?([1-9][0-9]*)(?:[-_/]|$)", branch)
                if match and int(match[1]) in issues:
                    matches.append(f"branch:{repository.lower()}:{branch}:issue#{match[1]}")
        except core.CoordinatorError as exc:
            errors.append(str(exc))
    return {"coverage": "incomplete" if errors else "complete" if issues else "not-applicable",
            "matches": sorted(set(matches)), "errors": sorted(set(errors)), "local_digest": local_digest}


def accept_overlap(repository, issues, observed, *, override=None, reason=None):
    scope = core.digest({"repository": repository.lower(), "issues": issues, "observation": observed})
    blocked = observed["matches"] or observed["errors"]
    if blocked and (override != scope or not reason or not reason.strip()):
        detail = "; ".join(observed["matches"] + observed["errors"])
        raise core.CoordinatorError(f"overlapping issue work or incomplete coverage: {detail}. Review these observations; an explicit decision may retry with --overlap-override {scope} --overlap-reason <reason>. Freshness and ownership cannot be overridden.")
    if override and (not blocked or override != scope or not reason or not reason.strip()):
        raise core.CoordinatorError("overlap override is stale or does not match current observations; review the new preflight")
    return {"observation_digest": scope, "reason": reason.strip()} if override else None


def verify_local_overlap(library, material, *, identity=None, worktree=None):
    matches, errors = local_overlap(library, material["repository"], material["issues"],
        identity=identity, candidate=material, worktree=worktree)
    if not material['issues']:
        errors = []
    if core.digest({"matches": matches, "errors": errors}) != material["overlap"]["local_digest"]:
        raise core.CoordinatorError("local issue claims changed during admission; preserve the preparation and repeat the preflight")
