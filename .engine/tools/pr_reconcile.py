#!/usr/bin/env python3
"""Reconcile a pull request stranded on the engine's derived-committed index files (StarshipSuperjam/engine-template#136).

When two pieces of work are in flight at once they can both rewrite the engine's two internal index files —
the knowledge graph (`.engine/knowledge/graph.json`) and the self-map (`.engine/self-map.md`) — and a sibling
pull request merging first leaves THIS pull request in a GitHub `CONFLICTING` state a non-engineer cannot
clear. Those two files are *derived-committed*: their content is a pure function of the source tree, so
a conflict on them is **spurious** — resolved by regenerating from the reconciled tree, never a hand-merge,
never a side-pick, and **never handed to the operator**.

This mirrors `checkout_health`'s detect → assess → execute shape, **lossless-or-it-does-not-run**:
  - `detect_conflict(gh)` — READ-ONLY, boot-relayed: is the current branch's open PR in a GitHub conflicting
    merge state? Returns an offer dict on a confirmed conflict, else None. GitHub computes `mergeable`
    asynchronously, so an *unknown* state degrades QUIETLY to None (caught next boot) — never a false
    "all clear". The authoritative file-level classifier is `assess`, not GitHub's async field.
  - `assess()` — READ-ONLY classification. A working-tree-free `git merge-tree` against the freshly-fetched
    default branch decides whether the conflict is confined to the two derived-committed members (`fixable`, lossless) or
    touches authored files (`needs-manual` — a real conflict for human decision, never auto-resolved). It
    refuses on a tree that carries no engine files (an external-contribution / fork-main branch is never
    regenerated onto).
  - `reconcile(apply=True)` — the executor: an **append-only merge** of the default branch (no history
    rewrite, NO force-push), regenerate the two members from the reconciled tree, re-verify, then a plain
    push. ANY surprise → `git reset --hard` to the captured pre-state and a plain-language refusal.

boot OFFERS the fix; the assistant runs `reconcile(apply=True)` on the operator's consent (the
`checkout_health.unstrand` model; `boot-session-start.md`). The operator is offered the fix, never handed the
conflict.

CLI:  python tools/pr_reconcile.py             # classify THIS branch's PR (offer line or "no conflict")
      python tools/pr_reconcile.py reconcile   # dry-run: what the fix WOULD do (no mutation)
      python tools/pr_reconcile.py reconcile --apply   # reconcile THIS PR (only if fixable)
      python tools/pr_reconcile.py prepare     # dry-run: proactively make THIS branch an integration candidate
      python tools/pr_reconcile.py prepare --apply     # bring up to date + regenerate + push (never merges)
      python tools/pr_reconcile.py demo        # a lossless-recovery + safe-refuse walkthrough on throwaway repos

`prepare` generalizes the reactive stranded-PR reconcile into the proactive integration-preparation primitive
the serialized integration coordinator (`integration_queue.py`) calls: it brings a reviewed candidate up to
date against the current protected head and regenerates its derived state BEFORE merge, refusing any authored
conflict. It proves the candidate MERGEABLE; the coordinator's `prove_ready` proves the checks green on the
pushed head (freshness, StarshipSuperjam/engine-template#915). It never merges the protected branch.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import repo_identity     # noqa: E402  (resolve_default_branch — the shared default-branch resolver)
import derived_state     # noqa: E402  (the derived-committed set + regeneration — single owner, StarshipSuperjam/engine-template#925)

# The registered reconcile set — derived-committed members whose conflicts are spurious (a pure function of
# source, so regenerate-to-resolve, never hand-merge). Single-sourced from the derived-state substrate
# (StarshipSuperjam/engine-template#925). The RUNTIME set that assess/_regen_members act on is present-AND-generator-resolvable
# (`_reconcile_members`): a member whose OPTIONAL generator is absent stays OUT of the spurious set, so its
# conflict classifies authored (needs-manual) and refuses — never append-merged and then discovered to be
# un-regenerable. `_CORE_MEMBERS` is the always-present pair (self-map + graph) that marks a tree as an engine
# tree at all — the fork-main / external-contribution guard.
MEMBERS = derived_state.paths(reconcile=True)
_CORE_MEMBERS = tuple(m.path for m in derived_state.members(reconcile=True) if m.optional_module is None)


def _reconcile_members(root: str) -> set:
    """The present-and-regenerable reconcile members for THIS tree — F-risk-3: gate on generator-resolvability,
    not mere file presence, so a present-file / absent-generator artifact stays OUT of the spurious set."""
    return set(derived_state.paths(reconcile=True, present_root=root))

# An inline identity so a merge/commit never fails for lack of a configured git user on the operator's machine.
_IDENT = ["-c", "user.email=engine@local", "-c", "user.name=engine"]

# Bounded retry through a transient missing-origin / shared-config blip (StarshipSuperjam/engine-template#704): under heavy parallel-worktree
# use, a concurrent write to the one shared .git/config makes an arbitrary git command fail for a moment, then
# self-heal. A few fast retries ride out that window. This inline retry is copied — not shared — across the
# five tools that carry it (scope_profile, close_linkage_preflight, pr_reconcile, module_manager, tune),
# matching the codebase's per-module retry convention (e.g. memory/capture.py's lock retry); keep the copies
# identical. Applied ONLY to the read-side fetch in `assess` (via `_fetch_with_retry`), NEVER inside `_ok` —
# the reconcile executor relies on `_ok`'s single-shot result to read a non-fast-forward push rejection as a
# terminal "someone advanced the branch" signal, which a retry would mask.
_ORIGIN_RETRY_ATTEMPTS = 3
_ORIGIN_RETRY_DELAY = 0.3      # seconds between attempts
_FETCH_FAST_FAIL = 5.0        # a fetch that FAILS in under this many seconds is a transient blip worth
                              # retrying; a slower failure is a genuine remote hang, so it is NOT retried and
                              # assess degrades at ~one timeout, never attempts×timeout (StarshipSuperjam/engine-template#704).


# ---- the git boundary (best-effort; a mutation reports success, never raises) ----------------

def _run(args: list, root: str, timeout: int = 30) -> str | None:
    """Run a local git command under `root` and return stripped stdout, or None on any non-zero / failure.
    Never raises — every read is best-effort."""
    try:
        out = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                             timeout=timeout, check=False)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — a missing binary / timeout / OS error degrades to "unavailable"
        return None


def _ok(args: list, root: str, timeout: int = 120) -> bool:
    """Run a git MUTATION under `root` and report success (return code 0). Never raises."""
    try:
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                              timeout=timeout, check=False).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _fetch_with_retry(default: str, root: str) -> bool:
    """`git fetch origin <default>` with a bounded retry through a transient missing-origin / shared-config
    blip (StarshipSuperjam/engine-template#704). Retries ONLY a FAST failure (the blip); a slower failure is a genuine remote hang, so it is
    not retried and `assess` degrades at ~one timeout rather than attempts×timeout. Calls `_ok` — leaving that
    helper (the executor's own mutation primitive) byte-unchanged — so the retry is confined to the read-side
    fetch and never touches the executor's push-rejection signal. The common (first-try) path makes exactly
    one call and never sleeps."""
    for attempt in range(_ORIGIN_RETRY_ATTEMPTS):
        started = time.monotonic()
        if _ok(["fetch", "origin", default], root):
            return True
        if time.monotonic() - started >= _FETCH_FAST_FAIL:
            break                          # a slow failure is a genuine hang/outage, not a transient blip
        if attempt < _ORIGIN_RETRY_ATTEMPTS - 1:
            time.sleep(_ORIGIN_RETRY_DELAY)
    return False


def _current_branch(root: str) -> str | None:
    """The current branch name, or None on a detached HEAD (which `rev-parse --abbrev-ref` reports as 'HEAD')."""
    b = _run(["rev-parse", "--abbrev-ref", "HEAD"], root)
    return b if b and b != "HEAD" else None


def _default_branch(root: str) -> str:
    """The repo's default branch name via the shared resolver (PROTECTED_BRANCH env -> recorded manifest ->
    origin/HEAD -> 'main'), so the reconcile base can never diverge from the branch the safety gate protects."""
    return repo_identity.resolve_default_branch(root)


def _dirty(root: str) -> bool:
    return bool((_run(["status", "--porcelain"], root) or "").strip())


def _unmerged(root: str) -> list[str]:
    """The paths left in a conflicted (unmerged) state by an in-progress merge."""
    out = _run(["diff", "--name-only", "--diff-filter=U"], root) or ""
    return [line for line in out.splitlines() if line.strip()]


def _merge_tree(base: str, root: str, head: str = "HEAD") -> tuple[str, list[str]] | None:
    """`git merge-tree --write-tree --name-only <base> <head>` — compute the merge WITHOUT touching the working
    tree. Returns ("clean", []) on a clean merge (exit 0), ("conflict", [paths]) on conflicts (exit 1), or None
    when merge-tree is unavailable / errored / unparseable (the caller treats None as 'cannot classify safely
    → needs-manual', never 'fixable'). Conflicted paths are the first stdout section after the tree-OID line,
    keyed off the exit code (NOT a substring grep of the trailing message block)."""
    try:
        p = subprocess.run(["git", "-C", root, "merge-tree", "--write-tree", "--name-only", base, head],
                          capture_output=True, text=True, timeout=120, check=False)
    except Exception:  # noqa: BLE001
        return None
    if p.returncode == 0:
        return ("clean", [])
    if p.returncode != 1:
        return None                       # 128 = old git / bad args / not a repo → cannot classify
    lines = p.stdout.splitlines()
    if not lines:
        return None
    paths: list[str] = []
    for line in lines[1:]:                # line 0 is the written tree's OID
        if line == "":                    # the name-only section ends at the first blank line
            break
        paths.append(line)
    return ("conflict", paths) if paths else None   # exit 1 but no parseable paths → cannot classify safely


def _members_present(root: str) -> bool:
    """The derived-committed members exist in the tree — the external-contribution / fork-main guard (a product/upstream
    contribution branch carries no engine files and is NEVER regenerated onto; locked build-orchestration)."""
    return all(os.path.isfile(os.path.join(root, m)) for m in _CORE_MEMBERS)


# ---- detect (READ-ONLY, boot-relayed) --------------------------------------------------------

def detect_conflict(gh, *, root: str | None = None) -> dict | None:
    """Is the current branch's open PR in a GitHub conflicting merge state? READ-ONLY. Returns
    {"pr": <n>, "title": <str>} on a confirmed conflict; None on clean / no-PR / no-GitHub / an UNKNOWN
    (async-uncomputed) merge state. A `mergeable == null` / `mergeable_state == "unknown"` NEVER reads as a
    confident "all clear" — it degrades quietly to None and is caught at the next boot (the authoritative
    file-level classifier is `assess`). `gh` is a neutral `github_client.reader` (`.repo` + `.transport`)."""
    if gh is None:
        return None
    root = root or validate.ROOT
    branch = _current_branch(root)
    if not branch:
        return None
    try:
        owner = gh.repo.split("/")[0]
        status, pulls = gh.transport(
            "GET", f"/repos/{gh.repo}/pulls?state=open&head={owner}:{branch}&per_page=10", None)
        if status >= 400 or not isinstance(pulls, list) or not pulls:
            return None
        number = pulls[0].get("number")
        if not number:
            return None
        status, pr = gh.transport("GET", f"/repos/{gh.repo}/pulls/{number}", None)
        if status >= 400 or not isinstance(pr, dict):
            return None
        mergeable = pr.get("mergeable")
        mstate = (pr.get("mergeable_state") or "").lower()
        if mergeable is False or mstate == "dirty":
            return {"pr": number, "title": pr.get("title") or ""}
        # mergeable is True → cleanly mergeable; None / "unknown" → GitHub hasn't computed it yet. Either way we
        # surface nothing this boot — an uncomputed state is caught at the next boot (boot never blocks polling),
        # and the authoritative file-level classifier is assess(). Never a false "all clear".
        return None
    except Exception:  # noqa: BLE001 — any read failure degrades this one signal quietly
        return None


# ---- assess (READ-ONLY classification; the authoritative file-level check) --------------------

def assess(*, root: str | None = None, default: str | None = None, fetch: bool = True) -> dict:
    """Classify the current branch's mergeability against the default branch, OFFLINE of GitHub's async field.
    status ∈ healthy | fixable | needs-manual. `fixable` iff a non-empty conflict set is confined to the two
    derived-committed members (lossless regenerate-to-resolve); any authored conflict, an unclassifiable merge, or a tree
    carrying no engine members → `needs-manual` (never `fixable`)."""
    root = root or validate.ROOT
    default = default or _default_branch(root)
    if not _members_present(root):
        return {"status": "needs-manual", "reason": "no-engine-members", "base": None, "conflicted": []}
    if fetch and not _fetch_with_retry(default, root):
        return {"status": "needs-manual", "reason": "fetch-failed", "base": None, "conflicted": []}
    base = _run(["rev-parse", f"origin/{default}"], root) or _run(["rev-parse", default], root)
    if not base:
        return {"status": "needs-manual", "reason": "no-base", "base": None, "conflicted": []}
    mt = _merge_tree(base, root)
    if mt is None:
        return {"status": "needs-manual", "reason": "cannot-classify", "base": base, "conflicted": []}
    kind, paths = mt
    if kind == "clean":
        return {"status": "healthy", "base": base, "conflicted": []}
    member_set = _reconcile_members(root)   # present + generator-resolvable only (F-risk-3)
    authored = [p for p in paths if p not in member_set]
    if authored:
        return {"status": "needs-manual", "reason": "authored-conflict", "base": base, "conflicted": paths}
    return {"status": "fixable", "base": base, "conflicted": paths}    # ⊆ members, non-empty → lossless


# ---- reconcile (the executor; lossless-or-refuse; NO force-push) ------------------------------

def _regen_members(root: str) -> bool:
    """Regenerate the present reconcile members FROM the reconciled tree by running the tree's OWN generators
    (subprocess dispatch — a throwaway copy regenerates itself, exactly as an integrate session does, the
    demo-fidelity rule). Single-sourced through the derived-state substrate (StarshipSuperjam/engine-template#925); regenerates exactly
    the present-and-resolvable set assess() classified as spurious. All must succeed."""
    results = derived_state.regenerate(
        derived_state.paths(reconcile=True, present_root=root), root=root, dispatch="subprocess")
    return bool(results) and all(r.status in ("regenerated", "unchanged") for r in results)


def reconcile(*, apply: bool = False, root: str | None = None, default: str | None = None) -> dict:
    """Reconcile the current branch's PR against the default branch, regenerating the two derived-committed members from the
    reconciled tree. Dry-run (apply=False) returns the assessment without mutating. apply=True executes an
    APPEND-ONLY merge (no history rewrite, NO force-push), resolves a member-only conflict by regeneration,
    re-verifies, and pushes. On ANY surprise it `git reset --hard`es to the captured pre-state and REFUSES —
    it never loses work, never side-picks, never hand-merges, and never claims a success it didn't earn."""
    root = root or validate.ROOT
    default = default or _default_branch(root)
    a = assess(root=root, default=default)
    if a["status"] != "fixable" or not apply:
        return {**a, "applied": False}
    return _execute_bring_up_to_date(root, a["base"], final_status="reconciled")


def _is_ancestor(ancestor: str, rev: str, root: str) -> bool:
    """True iff `ancestor` is an ancestor of `rev` — i.e. `rev` already contains it. Used to tell an
    already-up-to-date integration candidate (base ⊆ HEAD) from one that must be brought forward."""
    return _ok(["merge-base", "--is-ancestor", ancestor, rev], root)


def _execute_bring_up_to_date(root: str, base: str, *, final_status: str) -> dict:
    """The shared lossless executor (reconcile + prepare): an APPEND-ONLY merge of `base` (NO history
    rewrite, NO force-push), regenerate the present derived members from the reconciled tree, re-verify the
    branch merges cleanly, then a plain push. ANY surprise → `git reset --hard` to the captured pre-state and
    a needs-manual refusal. It NEVER merges the protected branch itself and NEVER force-pushes. On success it
    returns `final_status` ('reconciled' for a stranded-PR fix, 'prepared' for a proactive integration
    candidate)."""
    branch = _current_branch(root)
    if not branch:
        return {"status": "needs-manual", "reason": "detached-head", "applied": False}
    if _dirty(root):
        return {"status": "needs-manual", "reason": "dirty-tree", "applied": False}
    pre = _run(["rev-parse", "HEAD"], root)
    if not pre:
        return {"status": "needs-manual", "reason": "no-head", "applied": False}
    # The RUNTIME reconcile set for this tree — present AND generator-resolvable (F-risk-3). The executor
    # stages/verifies exactly these, never the static declared set: `git add`ing a declared-but-absent member
    # (e.g. the product-spec-matrix on a deployment without the product-design module) would fail the add.
    members_here = _reconcile_members(root)

    def _restore() -> None:
        _ok(["merge", "--abort"], root)            # convenience while a merge is in progress
        _ok(["reset", "--hard", pre], root)        # the UNIVERSAL restore — valid after an auto-completed merge

    def _refuse(reason: str) -> dict:
        _restore()
        return {"status": "needs-manual", "reason": reason, "applied": False}

    merged_clean = _ok([*_IDENT, "merge", "--no-ff", "--no-edit", base], root)
    if not merged_clean:
        conflicted = set(_unmerged(root))
        if not conflicted or (conflicted - members_here):     # an authored / unexpected conflict appeared
            return _refuse("unexpected-conflict")
        if not _regen_members(root):
            return _refuse("regen-failed")
        if not _ok(["add", *sorted(members_here)], root) or not _ok([*_IDENT, "commit", "--no-edit"], root):
            return _refuse("commit-failed")
    else:
        # The merge auto-completed (the members textually auto-merged). Regenerate anyway so the committed
        # members are the canonical regeneration of the merged sources, then record any change.
        if not _regen_members(root):
            return _refuse("regen-failed")
        if _dirty(root):
            if not _ok(["add", *sorted(members_here)], root) or not _ok(
                    [*_IDENT, "commit", "-m", "Regenerate engine index files from the reconciled tree"], root):
                return _refuse("commit-failed")

    # Re-verify locally BEFORE pushing: the branch must now merge into the default branch with no conflict
    # (the load-bearing reconcile-before-merge guarantee — the server-side merge button cannot run a local fix).
    if _merge_tree(base, root) != ("clean", []):
        return _refuse("verify-failed")
    # Plain push (NON-force). A non-fast-forward rejection means someone advanced the branch → refuse.
    if not _ok(["push", "origin", branch], root):
        return _refuse("push-rejected")
    return {"status": final_status, "branch": branch, "base": base, "applied": True}


def prepare(*, apply: bool = False, root: str | None = None, default: str | None = None) -> dict:
    """Proactively make THIS branch an integration candidate against the freshly-fetched protected head — the
    primitive the serialized integration coordinator calls before a candidate is surfaced ready.

    Classify BEFORE mutation (via `assess`). If ANY authored/source conflict exists → STOP: needs-manual,
    nothing mutated, both branches intact (authored overlap is never guessed away). If the head already
    contains the current base it is already an integration candidate → `healthy`, nothing to do. Otherwise
    (behind, whether cleanly or with a derived-member-only conflict) bring it up to date through the shared
    lossless executor: append-only merge + regenerate the present derived members + re-verify mergeability +
    non-force push. `apply=False` is a dry-run that classifies without mutating.

    It brings the candidate up to date and proves it MERGEABLE; it does NOT itself assert the required checks
    are green on the new head — that is the coordinator's `prove_ready`, which reads GitHub's checks on the
    exact pushed head (freshness bound through protection_guard, StarshipSuperjam/engine-template#915). And it never merges the
    protected branch: bringing a candidate up to date is not merging it."""
    root = root or validate.ROOT
    default = default or _default_branch(root)
    a = assess(root=root, default=default)
    if a["status"] == "needs-manual":
        return {**a, "applied": False}
    base, branch = a["base"], _current_branch(root)
    up_to_date = bool(base) and _is_ancestor(base, "HEAD", root)
    if not apply:
        return {"status": "healthy" if up_to_date else "prepared", "base": base, "branch": branch,
                "conflicted": a.get("conflicted", []), "up_to_date": up_to_date, "applied": False}
    if up_to_date:
        return {"status": "healthy", "base": base, "branch": branch, "up_to_date": True, "applied": False}
    return _execute_bring_up_to_date(root, base, final_status="prepared")


# ---- operator-facing CLI copy (plain words; no git verbs reach the operator surface) ----------

def _plain_reconcile(apply: bool) -> int:
    r = reconcile(apply=apply)
    status, reason = r["status"], r.get("reason")
    if status == "healthy":
        print("No pull request is stuck — nothing to reconcile.")
    elif status == "reconciled":
        print("Done — I reconciled your pull request against the latest main and pushed the result. Both "
              "pieces of work are still there; nothing was lost. If another change lands before you merge, "
              "I'll offer to do this again.")
    elif status == "needs-manual" and reason == "authored-conflict":
        print("I stopped and left everything exactly as it was — nothing changed, no work lost. This one I "
              "can't safely fix on my own: the two pieces of work changed the same actual content (not just "
              "the engine's index files), and choosing between them is a real decision. Tell me which "
              "direction you want, or ask me to walk you through the two versions in plain English — I'll do "
              "the rest once you've chosen.")
    elif status == "fixable" and not apply:
        print("This pull request is stuck on the engine's internal index files. I can fix it safely and keep "
              "both pieces of work (I reconcile it against the latest main and rebuild those files). Re-run "
              "with --apply to do it.")
    else:
        print("I stopped and left your work untouched — I couldn't safely finish this here. Nothing changed. "
              "Try again in a moment, or ask me what happened and I'll explain it in plain words.")
    return 0


# ---- the operator-runnable demo (throwaway git repos; the REAL reconcile) ---------------------

def _demo() -> int:
    import demo_pr_reconcile  # the walkthrough lives in its own demo_* file (the demo convention)
    return demo_pr_reconcile.main([])


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    if argv and argv[0] == "reconcile":
        return _plain_reconcile(apply="--apply" in argv)
    if argv and argv[0] == "prepare":
        r = prepare(apply="--apply" in argv)
        status = r["status"]
        if status == "healthy":
            print("This branch is already up to date with the latest main — nothing to prepare.")
        elif status == "prepared":
            print("Prepared: I brought this branch up to date with the latest main and regenerated its "
                  "derived files. It now merges cleanly." if r.get("applied")
                  else "This branch is behind the latest main; I can bring it up to date and regenerate its "
                       "derived files (run with --apply).")
        elif status == "needs-manual" and r.get("reason") == "authored-conflict":
            print("This branch conflicts with the latest main in your own edited files — that's a real "
                  "decision for you, so I've left both untouched.")
        else:
            print(f"Could not prepare this branch ({r.get('reason', status)}); nothing was changed.")
        return 0 if status in ("healthy", "prepared") else 1
    # Default: classify THIS branch (build a GitHub reader the way boot does, lazily to avoid an import cycle).
    import boot
    repo, token = boot.repo_slug(), boot.gh_token()
    import github_client
    import telemetry
    gh = github_client.reader(repo, token, user_agent=telemetry.USER_AGENT) if repo and token else None
    hit = detect_conflict(gh)
    print(hit if hit else "no conflicting pull request detected for the current branch")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
