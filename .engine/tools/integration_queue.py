"""Provider-independent serialized cross-PR integration (StarshipSuperjam/engine-template#925).

PR StarshipSuperjam/engine-template#969 made the concurrency law true INSIDE one Build (the execution DAG). This lifts it to BETWEEN pull
requests and sessions: reviewed candidates enter an ordered path, ONE integration candidate is admitted at a
time, it is brought up to date and proven fresh against the current protected head, and it is surfaced ready
for the operator's merge. It orders and proves; it NEVER merges — there is no `gh pr merge`, no merge API,
no MCP merge here, exactly as `release.yml` opens a PR and stops. That guarantee rests on this module carrying
no merge path (asserted by test) and on the protected-branch ruleset, NOT on the session merge-action hook
(which cannot see a subprocess's internals). This is NOT the intra-Build build_coordinator: that serializes
work-item nodes onto one PR branch against a temp-only StateStore; this serializes whole reviewed PRs against
one another, and its durable store is GitHub itself (open PRs + labels), never an engine-private ledger
(eADR-0025).

Durable facts, all read live from GitHub:
  - candidate set    = open PRs targeting the protected branch.
  - reviewed         = the `engine-integrate-ready` label, plus — in TEAM identity — an approval surviving
                       the last push (GitHub's require_code_owner_review is the binding code-owner gate at
                       merge, not this recognition); in SOLO identity, the PR being ready (not draft). Solo has no
                       distinct reviewer, so "reviewed" here is OPERATOR-ACKNOWLEDGED readiness, never a claim
                       that an independent review gate passed (eADR-0021, eADR-0042).
  - admission        = a singleton `engine-integrating` label (the backend's advisory CAS).

`prove_ready` is an ADVISORY pre-flight: the binding stale-green blocker is the StarshipSuperjam/engine-template#915 strict ruleset enforced
by GitHub at the merge click, not this check.

CLI:  python tools/integration_queue.py status     # ordered reviewed candidates + who is admitted
      python tools/integration_queue.py next        # which PR is next, and why
      python tools/integration_queue.py prepare      # admit next + bring THIS branch up to date + prove ready (never merges)
      python tools/integration_queue.py advance      # release admission (operator merged, or abandon)
      python tools/integration_queue.py backend      # disclose the active backend (native vs serialized)
      python tools/integration_queue.py demo         # a fixture walkthrough on a fake GitHub
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import protection_guard          # noqa: E402  (missing_floor freshness, resolve_tier, REQUIRED_CHECKS)
import integration_queue_backend as backend  # noqa: E402

READY_LABEL = backend.READY_LABEL          # single home: integration_queue_backend (provisioned there)
PRIORITY_LABEL = backend.PRIORITY_LABEL


@dataclass(frozen=True)
class Candidate:
    pr: int
    head_sha: str
    base_sha: str
    reviewed: bool
    order_key: tuple
    title: str


# ---- reading the candidate set from GitHub ---------------------------------------------------------

def _open_pulls(transport: Callable, repo: str, base: str) -> list:
    status, pulls = transport("GET", f"/repos/{repo}/pulls?state=open&base={base}&per_page=100", None)
    return pulls if status < 400 and isinstance(pulls, list) else []


def _labels(pr: dict) -> set:
    return {lab.get("name") for lab in pr.get("labels", []) if isinstance(lab, dict)}


def _approval_survives_last_push(transport: Callable, repo: str, pr_number: int, head_sha: str) -> bool:
    """TEAM readiness signal: an APPROVED review whose commit is the current head — an approval that survived
    the last push. GitHub's reviews API does NOT expose whether the reviewer is a CODEOWNERS-designated owner,
    so this recognizes any surviving approval; the binding CODE-OWNER requirement is GitHub's own
    require_code_owner_review at the merge gate (protection_guard / eADR-0021), never this advisory pre-flight."""
    status, reviews = transport("GET", f"/repos/{repo}/pulls/{pr_number}/reviews", None)
    if status >= 400 or not isinstance(reviews, list):
        return False
    return any(r.get("state") == "APPROVED" and r.get("commit_id") == head_sha for r in reviews)


def reviewed_candidates(transport: Callable, repo: str, base: str, *, tier: str) -> list[Candidate]:
    """The ordered list of admissible candidates. Order = (priority-rank, pr-number): PR number ascending is
    the FIFO default; the `engine-integrate-priority` label promotes. Computed fresh from live GitHub every
    call — never cached durably."""
    out: list[Candidate] = []
    for pr in _open_pulls(transport, repo, base):
        labels = _labels(pr)
        if READY_LABEL not in labels:
            continue
        number = pr.get("number")
        head_sha = (pr.get("head") or {}).get("sha", "")
        base_sha = (pr.get("base") or {}).get("sha", "")
        if tier == "team":
            reviewed = _approval_survives_last_push(transport, repo, number, head_sha)
        else:
            reviewed = not pr.get("draft", False)     # solo: operator-acknowledged ready (not draft) + label
        if not reviewed:
            continue
        rank = 0 if PRIORITY_LABEL in labels else 1
        out.append(Candidate(number, head_sha, base_sha, True, (rank, number), pr.get("title", "")))
    out.sort(key=lambda c: c.order_key)
    return out


# ---- proving a candidate ready (advisory; the ruleset is the binding gate) --------------------------

def _protected_head(transport: Callable, repo: str, base: str) -> Optional[str]:
    status, ref = transport("GET", f"/repos/{repo}/git/ref/heads/{base}", None)
    if status < 400 and isinstance(ref, dict):
        return (ref.get("object") or {}).get("sha")
    return None


def _checks_green(transport: Callable, repo: str, head_sha: str, required: list) -> bool:
    status, data = transport("GET", f"/repos/{repo}/commits/{head_sha}/check-runs", None)
    if status >= 400 or not isinstance(data, dict):
        return False
    runs = {r.get("name"): r.get("conclusion") for r in data.get("check_runs", [])}
    return all(runs.get(name) == "success" for name in required)


def _branch_rules(transport: Callable, repo: str, base: str) -> list:
    status, rules = transport("GET", f"/repos/{repo}/rules/branches/{base}", None)
    return rules if status < 400 and isinstance(rules, list) else []


def prove_ready(transport: Callable, repo: str, candidate: Candidate, base: str, *, tier: str,
                required: Optional[list] = None) -> dict:
    """Advisory pre-flight: is this candidate a fresh integration candidate right now? Composes THREE live
    reads — the freshness floor is armed (protection_guard.missing_floor over the real tier), the candidate's
    base is the current protected head (up to date), and the required checks are green on the candidate's
    exact head. Returns {ready, reasons}. This is a pre-flight, not the enforcement: the strict ruleset (StarshipSuperjam/engine-template#915)
    is what actually refuses a stale-green merge at the operator's click."""
    required = list(required if required is not None else protection_guard.REQUIRED_CHECKS)
    reasons: list[str] = []
    if protection_guard.missing_floor(_branch_rules(transport, repo, base), required, tier=tier):
        reasons.append("the protected-branch floor is not fully in force")
    head = _protected_head(transport, repo, base)
    if head is None:                       # read FAILED — fail closed (like the floor + checks reads), never
        reasons.append("could not read the current main to confirm the candidate is up to date")
    elif candidate.base_sha and candidate.base_sha != head:
        reasons.append("the candidate is behind the current main — it needs bringing up to date")
    if not _checks_green(transport, repo, candidate.head_sha, required):
        reasons.append("the required checks are not green on the candidate's current head")
    return {"ready": not reasons, "reasons": reasons}


# ---- orchestration ---------------------------------------------------------------------------------

def status(transport: Callable, repo: str, base: str, *, tier: str, be) -> dict:
    """The read-only picture: the ordered reviewed candidates, which PR currently holds admission (and, if
    stranded, its visibility), and the active backend."""
    candidates = reviewed_candidates(transport, repo, base, tier=tier)
    admitted = be.admitted() if be.name == "serialized" else None
    return {"backend": be.name, "admitted": admitted,
            "candidates": [{"pr": c.pr, "title": c.title, "order_key": list(c.order_key)} for c in candidates]}


def surface_next(transport: Callable, repo: str, base: str, *, tier: str, be, this_pr: Optional[int],
                 prepare_fn: Optional[Callable] = None) -> dict:
    """Admit and prepare the next reviewed candidate, then surface its readiness — NEVER merge.

    Picks the next candidate by order; if another PR already holds admission it reports that and stops (one at
    a time). Otherwise it acquires admission. When the admitted candidate is THIS session's PR, it brings the
    branch up to date via `prepare_fn` (pr_reconcile.prepare) and proves readiness; the operator merges. A
    prepare that hits an authored conflict releases admission and surfaces the blocked candidate rather than
    silently advancing to the next."""
    candidates = reviewed_candidates(transport, repo, base, tier=tier)
    if not candidates:
        return {"status": "empty",
                "detail": ("No reviewed candidate is waiting to integrate. A pull request joins the queue once "
                           f"it carries the `{READY_LABEL}` label and is out of draft.")}
    holder = be.admitted()
    if holder is not None and holder != this_pr:
        return {"status": "busy", "admitted": holder,
                "detail": f"PR #{holder} is currently integrating — one candidate at a time."}
    nxt = candidates[0]
    adm = be.admit(nxt.pr)
    if not adm.acquired:
        return {"status": "not-admitted", "detail": adm.disclosure, "next": nxt.pr}
    # Whose PR is this candidate? Only bring it up to date when it is THIS session's own branch; otherwise we
    # can only report on it (a peer session or the operator owns its preparation).
    mine = this_pr is not None and nxt.pr == this_pr
    if mine and prepare_fn is not None:
        prep = prepare_fn(apply=True)
        if prep.get("status") not in ("prepared", "healthy"):
            be.release(nxt.pr)
            reason = prep.get("reason", prep.get("status"))
            if reason == "authored-conflict":
                detail = (f"PR #{nxt.pr} conflicts with the latest main in files a human edited — a real "
                          "decision, so I left both branches exactly as they were and released the integration "
                          "slot for the next candidate. Resolve it on the branch, then it can re-enter the queue.")
            else:
                detail = (f"PR #{nxt.pr} couldn't be brought up to date ({reason}); I changed nothing and "
                          "released the integration slot. It needs a look before it can integrate.")
            return {"status": "blocked", "next": nxt.pr, "reason": reason, "detail": detail}
    proof = prove_ready(transport, repo, nxt, base, tier=tier)
    whose = "" if mine else f" (PR #{nxt.pr} is the next in the queue, not this session's own branch)"
    return {"status": "ready" if proof["ready"] else "not-ready", "next": nxt.pr, "title": nxt.title,
            "admitted": nxt.pr, "reasons": proof["reasons"], "mine": mine,
            "detail": (f"PR #{nxt.pr} is the next ready candidate — it reconciles cleanly and its checks are "
                       f"green against current main. Merge it when the evidence convinces you.{whose}"
                       if proof["ready"] else
                       f"PR #{nxt.pr} is admitted but not yet ready: {'; '.join(proof['reasons'])}.{whose}")}


# ---- CLI -------------------------------------------------------------------------------------------

def _live_context():
    import boot
    import telemetry
    import repo_identity
    repo, token = boot.repo_slug(), boot.gh_token()
    base = repo_identity.resolve_default_branch() if hasattr(repo_identity, "resolve_default_branch") else "main"
    tier = protection_guard.resolve_tier()
    transport = None
    if repo and token:
        import github_client
        gh = github_client.reader(repo, token, user_agent=telemetry.USER_AGENT)
        transport = gh.transport
    be, why = backend.select_backend(repo or "", token or "", base, tier=tier, transport=transport)
    return transport, repo, base, tier, be, why


def _coordinate(fn) -> None:
    """Run a best-effort advisory coordination emit (StarshipSuperjam/engine-template#939), swallowing everything. Coordination is
    advisory (eADR-0043) and must NEVER affect the queue action it rides — the emitters are themselves
    no-harm, and this lazy-import-plus-swallow is belt and braces. `fn` receives the coordination_emitters
    module; it is imported lazily so the queue never loads coordination at module time."""
    try:
        import coordination_emitters
        fn(coordination_emitters)
        # Surface any live-poke line to STDERR (never stdout) so this session's agent can relay it via the
        # doorbell skill — a pointer to the durable notice, never authority (StarshipSuperjam/engine-template#939, eADR-0043).
        for _line in coordination_emitters.drain_pokes():
            print(_line, file=sys.stderr)
    except Exception:  # noqa: BLE001 — an advisory emit never propagates into the queue
        pass


def _coordination_sync(transport, repo: str, pr: int) -> None:
    """A bounded coordination read point (StarshipSuperjam/engine-template#939): read the pull request's advisory board from
    GitHub, refresh the local ledger snapshot (so boot can relay the unread count with no network), print any
    unread notices, and mark them seen. Best-effort — never raises, never affects the queue command."""
    try:
        import coordination_board
        import coordination_ledger
        client = coordination_board._Comments(repo, "", transport=transport)
        notices = coordination_board.read_board(client, pr)
        unseen = coordination_ledger.sync_board(pr, notices)
        if unseen:
            kinds = ", ".join(sorted({n["kind"] for n in unseen}))
            print(f"Coordination: {len(unseen)} unread advisory notice(s) on PR #{pr} ({kinds}) — "
                  "re-verify the canonical state each names before acting.")
            coordination_ledger.mark_seen(pr, [n["notice_id"] for n in unseen])
    except Exception:  # noqa: BLE001 — advisory read, never breaks the queue
        pass


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    if argv and argv[0] == "backend":
        _t, _repo, base, tier, be, why = _live_context()
        print(f"active backend: {be.name}\n{why}")
        return 0
    transport, repo, base, tier, be, why = _live_context()
    if transport is None:
        print("No GitHub context (repo/token) — cannot read the integration queue.")
        return 1
    if argv and argv[0] == "next":
        cands = reviewed_candidates(transport, repo, base, tier=tier)
        print(f"next: PR #{cands[0].pr} ({cands[0].title})" if cands else "no reviewed candidate waiting")
        return 0
    if argv and argv[0] == "advance":
        this = _current_pr(transport, repo, base)
        if this is None:
            print("No open pull request for the current branch to advance.")
            return 0
        held = be.admitted()
        be.release(this)
        # NOTE: the merge-reaction coordination fan-out (next-in-queue, base-advanced revalidation, and the
        # dependency-merged scan) does NOT ride this verb. `advance` is a human afterthought to a merge —
        # relying on someone running it would make those signals non-deterministic, and by the documented
        # "merge, then advance" flow the merged pull request is already closed here, so a merge could never be
        # observed from this branch. Those signals fire deterministically from the post-merge workflow
        # (engine-coordination-postmerge.yml -> coordination_postmerge.py) on the merge event itself
        # (StarshipSuperjam/engine-template#939, eADR-0043). `advance` keeps only its slot-release duty.
        if held == this:
            print(f"Released the integration slot held by PR #{this}; the next candidate can be admitted.")
            # Advisory (StarshipSuperjam/engine-template#939): releasing the slot is a handoff — tell the next reviewed candidate the
            # slot opened. This rides the deliberate release ACTION (unlike the merge-reactions above): the
            # merge path is covered by the deterministic post-merge next-in-queue notice, and this is its
            # session-action twin for a voluntary/abandon release. Never a lock — the receiver re-checks the
            # queue. Best-effort; any poke is surfaced by _coordinate. EXCLUDE `this`: releasing the slot does
            # not close/unlabel this PR, so it can still be the earliest ready candidate — without this filter
            # the notice would address the releaser itself and the real waiting peer would never hear.
            _nextc = [c for c in reviewed_candidates(transport, repo, base, tier=tier) if c.pr != this]
            if _nextc:
                _coordinate(lambda ce: ce.emit_handoff(transport, repo, _nextc[0].pr, "slot-released"))
        else:
            where = f"PR #{held} holds it" if held else "no pull request holds it right now"
            print(f"PR #{this} wasn't holding the integration slot ({where}); nothing changed. To free a slot "
                  "stuck on another pull request, close that pull request (it drops out of the queue) or run "
                  "advance from its branch.")
        return 0
    if argv and argv[0] == "prepare":
        import pr_reconcile
        this = _current_pr(transport, repo, base)
        result = surface_next(transport, repo, base, tier=tier, be=be, this_pr=this,
                              prepare_fn=lambda **kw: pr_reconcile.prepare(**kw))
        # Advisory: surface the admission / block to the candidate's owner (best-effort, never gates).
        if result.get("status") == "blocked" and result.get("next"):
            _coordinate(lambda ce: ce.emit_integration_blocked(transport, repo, result["next"]))
        elif result.get("admitted"):
            _coordinate(lambda ce: ce.emit_integration_admitted(transport, repo, result["admitted"]))
        print(result["detail"])
        if this is not None:
            _coordination_sync(transport, repo, this)
        return 0 if result["status"] in ("ready", "empty", "busy") else 1
    # default: status
    st = status(transport, repo, base, tier=tier, be=be)
    print(f"backend: {st['backend']}; admitted: {st['admitted']}")
    for c in st["candidates"]:
        print(f"  candidate PR #{c['pr']}: {c['title']}")
    try:  # the coordination read is advisory — a GitHub hiccup must never crash `status`
        _this = _current_pr(transport, repo, base)
        if _this is not None:
            _coordination_sync(transport, repo, _this)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _current_pr(transport: Callable, repo: str, base: str) -> Optional[int]:
    import pr_reconcile
    branch = pr_reconcile._current_branch(None)
    if not branch:
        return None
    owner = repo.split("/")[0]
    status_code, pulls = transport("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}", None)
    if status_code < 400 and isinstance(pulls, list) and pulls:
        return pulls[0].get("number")
    return None


def _demo() -> int:
    """A fixture walkthrough on a fake GitHub: two reviewed candidates, one-at-a-time admission, a not-ready
    candidate surfaced honestly, and proof the coordinator carries no merge call."""
    import demo_integration_queue
    return demo_integration_queue.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
