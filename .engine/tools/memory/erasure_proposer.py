"""erasure_proposer.py — the Layer-2 erasure EMITTER (memory-substrate, slice 4e PR iii).

Slice 4e is the memory substrate's single irreversible act: physically erasing a remembered note. Slice (i) built
the enactment core (`compact.enact_erasure`, the sole append-only minter, shipped inert); slice (ii) built the
cross-session OBSERVER (`erasure_observer`), which turns a *merged single-purpose erasure pull request* into the
gated marker. THIS module is the PRODUCER that closes the loop: a deterministic probe over the engine's
already-logically-retired notes selects one that has **earned erasure**, writes the content-free proposal at the
observer's fixed path, and AUTO-OPENS a single-purpose pull request labelled `engine-erasure` for the operator to
merge. After this slice: a local self-review -> an auto-opened erasure PR -> the operator merges -> a later session's
observer enacts -> the next compaction erases. The merge is the one irreversible-consent point; nothing auto-merges.

The producer NEVER mints the marker itself — it writes a file and opens a PR; the merge (the operator) + the observer
(a later session) do the rest. So this module deliberately reaches NO ledger-write / minter path (a build-conformance
invariant `test_forget.py` pins by source scan: the only sanctioned callers of the slice-i minter are `compact` and
`erasure_observer`, never this file).

D-007 (content-free binding): the committed proposal carries `{"targets": [<stable content-free record id>, …],
"costs": [<plain-language paraphrase>, …]}` — parallel arrays, `costs[i]` describing `targets[i]`, so one merged
pull request can clear a whole batch of earned notes in a single consent act (the operator directive: one merge
clears the backlog). Each `target` is a note's uuid-hex id (reveals nothing about the gitignored content); each
`cost` is built ONLY from content-free metadata (the note's role/kind rendered as plain words + a coarse age bucket)
— never the note's text, never its session id or tags. A test scans the whole serialized proposal for every note's
distinctive words and flips red if any leaks. The committed grammar is what the operator reads (the pull-request
body enumerates every cost line) and what a later session erases by (the observer reads `targets`), bound to the
one immutable merge tree — so what was consented and what is erased are provably the same committed artifact. A
legacy single-target proposal `{"target": id, …}` is still read as a one-note batch (back-compat).

Posture: **deterministic detection, injected emission.** The probe is pure mechanism over the ledger (same ledger +
same clock -> same selection). The PR-opener is INJECTED (the `module_manager._open_upgrade_pr` discipline) so
tests/demo run the real logic fully offline; NO hook/workflow/cron invokes `propose`, so the real open is never
reached AUTOMATICALLY (a deliberate `propose` run is the operator's own choice, the same class as `tune` /
`module_manager` — and even then it only OPENS a pull request, never erases). Auto-open is
de-duplicated: a target already covered by ANY `engine-erasure` pull request (open or merged) or already carrying an
erasure marker is skipped, and on any host doubt the producer DECLINES to open (fail-SAFE — a missed open just retries;
declining loses nothing). Memory is local + gitignored, so detection runs in a LOCAL session where the ledger exists,
never in the empty-store CI checkout. Run the demo:
    uv run --directory .engine --frozen -- python tools/memory/erasure_proposer.py demo
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import erasure_observer as observer  # noqa: E402 — the (ii)<->(iii) contract: reuse its path/label/predicates
from memory import forget, ledger, records       # noqa: E402

# Build-spec leaf (uncalibrated, recorded; the operator set ~1 month this session — mirrors the
# `compact._COMPACT_WASTE_THRESHOLD` leaf convention). How long an already-logically-retired note must have sat before
# it has EARNED physical erasure. Failure direction is "nothing lost": too high merely defers reclaiming a little disk
# (the note stays resident + fully recoverable); too low only proposes more readily, and a human still merge-gates each.
EARNED_ERASURE_MIN_AGE_DAYS = 30

# Build-spec leaf (uncalibrated, recorded; the operator chose ~7 days this session). How often the throttled local
# trigger may CHECK for an earned note — a FIXED cadence (not a self-tuning controller, which the design refuses). It
# is a politeness/cost guard only: the SAFETY guards are the age window above + the human merge. Failure direction is
# benign in both senses — too short merely checks more often (each open still merge-gated, deduped, one-at-a-time);
# too long merely defers reclaiming a little disk.
EARNED_ERASURE_CHECK_INTERVAL_DAYS = 7

_DAY = 86400

# A gitignored runtime sidecar under .engine/memory/ (sibling of capture-state.json / ledger-meta.json) holding the
# last-check timestamp for the throttle. Never committed; resolved via ledger.ledger_dir() so it lands in the throwaway
# cabinet under tests/demo and the real store in production.
_STATE_FILENAME = "erasure-proposer-state.json"

# Plain words for the closed, engine-shipped role vocabulary (consolidate.ROLE_VOCABULARY) — so the operator-facing
# `cost` never surfaces a raw engine token. An unknown/absent role degrades to the neutral "a note".
_ROLE_PHRASE = {
    "decision": "a decision",
    "rationale/pushback": "a note about why a choice was made",
    "lesson": "a lesson",
    "dead-end": "a note about an approach that was set aside",
    "preference": "a preference",
    "intent": "a plan",
    "observation": "an observation",
}


# --- the earned-erasure probe (deterministic; pure over the ledger) ----------------------------------------

def earned_targets(path: "str | None" = None, *, now: "int | None" = None) -> list:
    """The already-logically-retired notes that have EARNED physical erasure, oldest first. Deterministic over the
    ledger (`forget.duplicates` — the crash-duplicate orphans recall already drops): a note earns erasure iff it is
    logically retired AND its birth-age `now - ts` exceeds `EARNED_ERASURE_MIN_AGE_DAYS` (the only durable temporal
    field — retirement itself is unstamped) AND it carries zero reinforcement markers ("never recalled" — structurally
    true for an orphan, kept as a load-bearing safety floor). Returns the records (each carrying its content-free id);
    ordered oldest `ts` first, then by id (a total, content-free tie-break). Mutates nothing."""
    src = ledger.ledger_path() if path is None else path
    now = int(time.time()) if now is None else now
    cutoff = now - EARNED_ERASURE_MIN_AGE_DAYS * _DAY
    access = forget._access_index(src)
    earned: list = []
    for _sid, recs in forget.duplicates(path).items():
        for r in recs:
            rid = r.get(records.RECORD_ID_KEY)
            ts = r.get("ts")
            if not observer._is_record_id(rid):
                continue
            if not (isinstance(ts, int) and not isinstance(ts, bool)) or ts > cutoff:
                continue                       # too fresh (or no usable birth time) -> not yet earned
            if access.get(rid):
                continue                       # ever recalled -> the safety floor refuses to propose erasing it
            earned.append(r)
    earned.sort(key=lambda r: (r["ts"], r[records.RECORD_ID_KEY]))
    return earned


# --- the proposal (content-free; D-007) --------------------------------------------------------------------

def _role_phrase(role) -> str:
    return _ROLE_PHRASE.get(role, "a note")


def _age_phrase(seconds: int) -> str:
    """A coarse, content-free age bucket (no raw timestamp). Grammatical across the range the operator may vary into."""
    days = max(0, int(seconds // _DAY))
    if days < 14:
        return "in the last couple of weeks"
    if days < 31:
        return "a few weeks ago"
    if days < 75:
        return "about a month ago"
    if days < 320:
        return f"about {round(days / 30)} months ago"
    if days < 550:
        return "about a year ago"
    return "over a year ago"


def _cost_for(record: dict, now: int) -> str:
    """The content-free plain-language cost line for ONE earned note — built ONLY from the note's role (a closed
    engine vocabulary, rendered to plain words) + a coarse age bucket, never the note's `text`/`session_id`/`tags`
    (D-007). Shared by `build_proposal` (the committed grammar) and `_print_candidates` (the local list)."""
    ts = record.get("ts")
    age = now - ts if isinstance(ts, int) and not isinstance(ts, bool) else 0
    return (f"{_role_phrase(record.get('role'))} the engine set aside as a duplicate of a save that didn't finish — "
            f"{_age_phrase(age)}; already hidden from recall and still fully recoverable until erased.")


def build_proposal(records_in: list, *, now: "int | None" = None) -> dict:
    """The committed batch proposal `{"targets": [id, …], "costs": [line, …]}` for one or more earned notes —
    EXACTLY those two keys, both content-free, and `costs[i]` describes `targets[i]` (parallel, one-to-one). Each
    `target` is validated to the observer's record-id shape; each `cost` is plain language from the note's role + a
    coarse age bucket (D-007 — never the note's text/session/tags). Raises on an empty list or any record without a
    valid content-free id (so an invalid target can never enter the grammar)."""
    if not records_in:
        raise ValueError("refusing to build a proposal with no targets")
    now = int(time.time()) if now is None else now
    targets: list = []
    costs: list = []
    for record in records_in:
        target = record.get(records.RECORD_ID_KEY)
        if not observer._is_record_id(target):
            raise ValueError("refusing to build a proposal for a record without a content-free id")
        targets.append(target)
        costs.append(_cost_for(record, now))
    return {"targets": targets, "costs": costs}


def write_proposal(proposal: dict, *, root: "str | None" = None) -> str:
    """Write the batch proposal to the observer's fixed committed path under `root` (the repo root by default; a
    throwaway root in tests/demo). Refuses to write a proposal whose `targets` are not ALL content-free record ids,
    that is empty, or whose `costs` do not correspond one-to-one to its targets (so an invalid or mismatched batch
    can never land — the operator must read a cost line for exactly the notes that will be erased). Overwrites in
    place — there is only ever one canonical proposal. Returns the path written."""
    targets = proposal.get("targets")
    costs = proposal.get("costs")
    if not isinstance(targets, list) or not targets or not all(observer._is_record_id(t) for t in targets):
        raise ValueError("refusing to write a proposal whose targets are not all content-free record ids")
    if not isinstance(costs, list) or len(costs) != len(targets):
        raise ValueError("refusing to write a proposal whose costs do not correspond one-to-one to its targets")
    if root is None:
        import validate  # lazy: only the real write needs the repo root
        root = validate.ROOT
    dest = os.path.join(root, observer._PROPOSAL_PATH)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump({"targets": targets, "costs": [str(c) for c in costs]}, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return dest


# --- the GitHub reader + the cross-PR dedup ----------------------------------------------------------------

def _reader(transport=None):
    """A GitHub boundary over the operator's `gh` token (label = `engine-erasure`), or None when the repo/token can't
    be resolved (a degraded host). Tests/demo inject a `transport` (the same 2-tuple seam the observer uses), so the
    real logic runs fully offline. Lazy-imported so the cold-start load stays light."""
    import telemetry  # noqa: E402 — its GitHubIssues exposes the injectable 2-tuple transport + ensure_label
    if transport is not None:
        return telemetry.GitHubIssues("local/practice", "practice-token",
                                      label=observer.ERASURE_LABEL, transport=transport)
    import boot  # noqa: E402 — lazy: keep boot's heavy import graph off the module-load path
    repo = boot.repo_slug()
    token = boot.gh_token()
    if not repo or not token:
        return None
    return telemetry.GitHubIssues(repo, token, label=observer.ERASURE_LABEL)


def _proposed_targets(gh):
    """The set of target ids already covered by an `engine-erasure` pull request in ANY state (open / merged / closed)
    — the cross-PR dedup, so auto-open opens at most ONE pull request per target, ever, and respects a decline. Reads
    each candidate's proposal at its merge tree (merged) or head (open). Returns the set, or None if the pull-request
    LIST itself could not be read (host doubt -> the caller DECLINES to open, fail-SAFE). Never raises."""
    raw = observer._get(gh, f"/repos/{gh.repo}/issues?state=all&labels={observer.ERASURE_LABEL}&per_page=100")
    if raw is None:
        return None                                   # could not read the list -> cannot dedup -> decline upstream
    if not isinstance(raw, list):
        return set()
    out: set = set()
    for item in raw:
        if not (isinstance(item, dict) and "pull_request" in item and isinstance(item.get("number"), int)):
            continue
        pr = observer._get(gh, f"/repos/{gh.repo}/pulls/{item['number']}")
        if not isinstance(pr, dict):
            continue
        ref = pr.get("merge_commit_sha") or (pr.get("head") or {}).get("sha")
        if not isinstance(ref, str) or not ref:
            continue
        for target in observer._read_targets(gh, ref):  # reads ONLY the content-free ids from the committed proposal
            out.add(target)
    return out


def _open_erasure_pr_numbers(gh):
    """The numbers of the `engine-erasure` pull requests that are currently OPEN — the one-in-flight serializer's read,
    so auto-open holds the next proposal until the operator has resolved (merged or closed) the current one. Returns the
    list, or None if the open-list could not be read (host doubt -> the caller DECLINES to open, fail-SAFE — never read
    an unreadable list as 'none open -> go ahead'). Never raises."""
    raw = observer._get(gh, f"/repos/{gh.repo}/issues?state=open&labels={observer.ERASURE_LABEL}&per_page=100")
    if raw is None:
        return None                                   # could not read -> cannot confirm none open -> decline upstream
    if not isinstance(raw, list):
        return []
    return [item["number"] for item in raw
            if isinstance(item, dict) and "pull_request" in item and isinstance(item.get("number"), int)]


def _apply_label(gh, number: int) -> bool:
    """Ensure the `engine-erasure` label exists and apply it to the just-opened pull request (a PR is an issue for
    labelling). The observer discovers ONLY by this label, so this is load-bearing. Fail-OPEN: a label failure leaves
    the PR un-discovered (safe — no erasure) rather than raising; returns True iff the label was applied."""
    try:
        gh.ensure_label()
        gh._transport("POST", f"/repos/{gh.repo}/issues/{number}/labels", {"labels": [observer.ERASURE_LABEL]})
        return True
    except Exception:  # noqa: BLE001 — a degraded host must not strand the caller; the un-labelled PR simply won't fire
        return False


# --- the real PR opener (INJECTED in tests/demo; NEVER runs in the construction repo) ----------------------

def _open_erasure_pr(gh, branch: str, title: str, body: str, content: str):
    """THE GIT+PR BOUNDARY — HOOK-SAFE: build the branch, commit the single proposal file, and open the single-purpose
    pull request ENTIRELY via the GitHub API over the bounded `gh` transport (create-ref -> put-contents -> open-pull).
    There is NO local git and NO working-tree mutation, and every call is timeout-bounded — so the background
    SessionStart trigger can never switch the operator's branch out from under a live session, nor hang on a stalled
    `git push`. The PUT commits EXACTLY the one proposal file, so the merge tree the observer reads carries exactly that
    one change (single-purpose). Fail-SAFE throughout: any non-success status, unreadable body, or transport fault ->
    return None (the caller reports a retry, never a raise from a hook); a pre-existing branch ref (a 422 from a prior
    partial open) is treated as already-in-flight -> None, so a deterministic branch name can never wedge or duplicate.
    Returns the new pull-request number, or None."""
    import boot  # noqa: E402 — lazy: only for the protected-branch name
    base = getattr(boot, "PROTECTED_BRANCH", "main")
    try:
        head = observer._get(gh, f"/repos/{gh.repo}/git/ref/heads/{base}")
        base_sha = (head or {}).get("object", {}).get("sha")
        if not isinstance(base_sha, str) or not base_sha:
            return None
        status, _ = gh._transport("POST", f"/repos/{gh.repo}/git/refs",
                                  {"ref": f"refs/heads/{branch}", "sha": base_sha})
        if status not in (200, 201):                 # 422 (ref already exists) or any error -> decline, never wedge
            return None
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        put = {"message": title, "content": encoded, "branch": branch}
        # proposal.json ships as a committed placeholder, so it already exists on the branch — the Contents API needs
        # the existing blob sha to UPDATE it (omitting it 422s). A fresh tree without the file -> no sha -> create.
        existing = observer._get(gh, f"/repos/{gh.repo}/contents/{observer._PROPOSAL_PATH}?ref={base}")
        file_sha = (existing or {}).get("sha")
        if isinstance(file_sha, str) and file_sha:
            put["sha"] = file_sha
        status, _ = gh._transport("PUT", f"/repos/{gh.repo}/contents/{observer._PROPOSAL_PATH}", put)
        if status not in (200, 201):
            return None
        status, pr = gh._transport("POST", f"/repos/{gh.repo}/pulls",
                                   {"title": title, "head": branch, "base": base, "body": body})
        if status not in (200, 201) or not isinstance(pr, dict):
            return None
        number = pr.get("number")
        return number if isinstance(number, int) else None
    except Exception:  # noqa: BLE001 — fail-SAFE: a degraded host yields no open, never a raise from a SessionStart hook
        return None


def _branch_for(targets: list) -> str:
    """A deterministic branch name over the whole target SET (order-independent), so a byte-identical retried batch
    reuses the same branch (the `_open_erasure_pr` 422-already-exists guard then declines rather than duplicating).
    Note the real one-at-a-time protection is the in-flight serializer (`_open_erasure_pr_numbers`), not this name."""
    digest = hashlib.sha1("\n".join(sorted(targets)).encode("utf-8")).hexdigest()
    return f"erasure-{digest[:12]}"


def _pr_title(n: int) -> str:
    return f"Erase {n} remembered note{'' if n == 1 else 's'} (single-purpose)"


def _pr_body(proposal: dict) -> str:
    """The operator-facing pull-request body — plain language, one cost line PER note, no engine jargon, no note
    content. This is the consent surface: merging erases every note listed here (a later session carries it out;
    nothing merges on its own), so the body ENUMERATES each note's plain-language cost — the operator sees the full
    list they are consenting to erase, never a bare count. For a batch it is all-or-nothing: closing keeps them all.
    Renders from `costs` (which `write_proposal` pins one-to-one with the committed `targets`), so the list read is
    exactly the list erased."""
    costs = proposal.get("costs") or []
    n = len(costs)
    if n == 1:
        return (
            "This pull request proposes to **permanently erase one remembered note** from the engine's memory.\n\n"
            f"**What it is:** {costs[0]}\n\n"
            "Merging this pull request is your consent to erase that one note — the single thing the engine can do to "
            "its memory that cannot be undone. Nothing is erased the moment you merge: a later session carries out "
            "the erasure, and nothing merges on its own. If you would rather keep the note, just **close** this pull "
            "request — declining loses nothing (the note stays exactly where it is, still hidden from recall and "
            "fully recoverable).\n")
    listed = "\n".join(f"- {c}" for c in costs)
    return (
        f"This pull request proposes to **permanently erase {n} remembered notes** from the engine's memory, in one "
        f"batch.\n\n"
        f"**What each one is** (merging consents to erasing all {n}):\n\n{listed}\n\n"
        f"Merging this pull request is your consent to erase all {n} notes — the single thing the engine can do to "
        f"its memory that cannot be undone. Nothing is erased the moment you merge: a later session carries out the "
        f"erasure, and nothing merges on its own. This is all-or-nothing: merging erases every note above. If you "
        f"would rather keep any of them, just **close** this pull request — declining loses nothing (every note stays "
        f"exactly where it is, still hidden from recall and fully recoverable), and the engine will not raise these "
        f"same notes again.\n")


# --- the auto-open orchestrator ----------------------------------------------------------------------------

def propose(path: "str | None" = None, *, opener=None, transport=None, root: "str | None" = None,
            now: "int | None" = None) -> dict:
    """Detect the oldest note that has earned erasure and, if it is not already covered, AUTO-OPEN one single-purpose
    erasure pull request for it. The consent posture the operator chose: the engine opens the pull request itself; the
    operator merge-gates it; nothing auto-merges. Fail-SAFE throughout — any host doubt, or an existing pull request /
    marker for the target, yields no open. Returns a plain-language result dict (`opened`: the PR numbers opened).

    Injection: tests/demo pass `opener` (a stub that never touches git/network) and/or `transport` (the GitHub seam).
    An un-injected call reaches the REAL opener (the deployed auto-open path); an injected `transport` with no `opener`
    is a practice run that writes the proposal but opens nothing (the footgun guard, mirroring module_manager)."""
    earned = earned_targets(path=path, now=now)
    if not earned:
        return {"opened": [], "targets": [], "message": "No notes have earned erasure — nothing to propose."}
    enacted = observer._erased_targets(path)           # ids already scheduled (a retained marker) — a local read
    gh = _reader(transport)
    if gh is None:
        return {"opened": [], "targets": [],
                "message": "Could not reach GitHub; will try again at the next review."}
    proposed = _proposed_targets(gh)
    if proposed is None:                               # host doubt on the dedup list -> DECLINE (fail-SAFE), never
        return {"opened": [], "targets": [],           # coalesce to empty (that would fail-OPEN into a duplicate open)
                "message": "Could not check existing erasure pull requests; declined to open (no duplicate risk)."}
    covered = enacted | proposed
    candidates = [r for r in earned if r[records.RECORD_ID_KEY] not in covered]  # earned, oldest-first, order kept
    if not candidates:
        return {"opened": [], "targets": [],
                "message": "Every earned note is already scheduled or proposed for erasure."}
    open_already = _open_erasure_pr_numbers(gh)        # the one-in-flight serializer: one erasure PR (one consent
    if open_already is None:                           # act) in flight at a time — for a batch it holds the WHOLE
        return {"opened": [], "targets": [],           # next batch until the current one is merged or closed
                "message": "Could not check existing erasure pull requests; declined to open (no duplicate risk)."}
    if open_already:
        return {"opened": [], "targets": [],
                "message": "An erasure pull request is already open for your review — holding the next one until it is resolved."}
    proposal = build_proposal(candidates, now=now)
    targets = proposal["targets"]
    n = len(targets)
    if opener is not None:
        # Injected stub (tests/demo): keep the file-backed callable seam — write the proposal where the stub reads it.
        write_proposal(proposal, root=root)
        number = opener(_branch_for(targets), _pr_title(n), _pr_body(proposal), [observer._PROPOSAL_PATH],
                        repo=None, token=None)
    else:
        # The real, hook-safe path: commit the proposal via the API over `gh` (real or injected transport) — no local
        # write, no working-tree mutation.
        content = json.dumps(proposal, ensure_ascii=False, indent=2) + "\n"
        number = _open_erasure_pr(gh, _branch_for(targets), _pr_title(n), _pr_body(proposal), content)
    if number is None:
        return {"opened": [], "targets": targets,
                "message": "Could not open the pull request; will try again at the next review."}
    labelled = _apply_label(gh, number)
    return {"opened": [number], "targets": targets, "proposal": proposal, "labelled": labelled,
            "message": f"Opened a single-purpose erasure pull request (#{number}) clearing "
                       f"{n} note{'' if n == 1 else 's'} for your review."}


# --- the throttled local trigger (a SessionStart hook; fail-open; mirrors erasure_observer) ----------------

def _state_path() -> str:
    return os.path.join(ledger.ledger_dir(), _STATE_FILENAME)


def _last_check() -> "int | None":
    """The last-check timestamp from the gitignored sidecar, or None if it is missing or unreadable (-> 'check now')."""
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            return int(json.load(fh).get("last_check_ts"))
    except Exception:  # noqa: BLE001 — a missing/corrupt sidecar must never STOP the loop; treat as 'check now'
        return None


def _should_check(now: int) -> bool:
    """Throttle gate: check at most once per EARNED_ERASURE_CHECK_INTERVAL_DAYS. A missing/corrupt OR a FUTURE timestamp
    (clock skew / tampering) -> check now, so the loop can never silently stick OFF; otherwise wait out the interval."""
    last = _last_check()
    if last is None or last > now:
        return True
    return (now - last) >= EARNED_ERASURE_CHECK_INTERVAL_DAYS * _DAY


def _record_check(now: int) -> None:
    """Stamp the sidecar with this check time. Best-effort: a write failure just means we check again next session."""
    try:
        os.makedirs(ledger.ledger_dir(), exist_ok=True)
        with open(_state_path(), "w", encoding="utf-8") as fh:
            json.dump({"last_check_ts": int(now)}, fh)
    except Exception:  # noqa: BLE001 — never strand the session on a sidecar write
        pass


def _heads_up(pr_numbers: list, note_count: "int | None" = None) -> str:
    """The one-time, model-facing relay for a NEWLY-OPENED erasure pull request — plain language, no ids/jargon beyond
    the operator-recognisable PR number, never the note's content. `note_count` (how many notes the batch clears) is
    stated plainly so the operator knows the scale; the per-note detail is in the pull-request body. Mirrors
    erasure_observer._heads_up's prefix."""
    refs = ", ".join(f"#{n}" for n in sorted(set(pr_numbers)))
    if note_count == 1 or note_count is None:
        scope = "one old note it had already hidden as a duplicate"
        keep = "the note stays exactly where it is"
        again = "this same note"
    else:
        scope = f"{note_count} old notes it had already hidden as duplicates (each described in the pull request)"
        keep = "every note stays exactly where it is"
        again = "these same notes"
    return (
        f"INFORM THE USER, in plain language (they asked to be told once): the engine has opened a single-purpose "
        f"pull request ({refs}) proposing to permanently erase {scope}. "
        f"Nothing is erased yet, and nothing erases on its own — it is erased only if they merge that pull request, "
        f"and even then a later session carries it out. If they would rather keep them, they can just close the "
        f"pull request: closing loses nothing — {keep}, hidden from recall and still fully "
        f"recoverable. The engine will not raise {again} again.")


def _session_start_handler(payload, *, now: "int | None" = None) -> dict:
    """Memory's earned-erasure PROPOSER at SessionStart — the throttled local trigger. Fail-open throughout (a fault
    here must NEVER block or slow session start): if the throttle interval has elapsed, run the auto-open (which is
    itself fail-SAFE and, via the API opener, touches no working tree and cannot hang), then stamp the check; on a NEW
    open relay ONE plain-language heads-up; otherwise proceed silently. `payload` is unused (we read the ledger + GitHub,
    not the event). The check itself is the only side effect on a throttled session: none."""
    import hooks  # noqa: E402 — lazy: keep the module-load path light
    try:
        when = int(time.time()) if now is None else int(now)
        if not _should_check(when):
            return hooks.proceed()                    # within the cooldown -> a cheap, silent no-op
        result = propose()                            # the real, un-injected, hook-safe auto-open path
        _record_check(when)
        opened = result.get("opened") or []
        if opened:
            return hooks.inject(_heads_up(opened, len(result.get("targets") or [])))
    except Exception:  # noqa: BLE001 — fail-open: a fault must never strand the session start
        return hooks.proceed()
    return hooks.proceed()


# --- CLI -------------------------------------------------------------------------------------------------

def _print_candidates(path: "str | None" = None) -> int:
    """The `candidates` verb: an operator-legible, LOCAL list of what has earned erasure — each note's plain-language
    cost (exactly what a pull request would carry) plus a snippet of its own text shown ONLY here on the operator's
    machine (never committed — D-007 governs the committed tree, not local display). Opens nothing."""
    earned = earned_targets(path=path)
    if not earned:
        print("No notes have earned erasure — there is nothing to propose removing.")
        return 0
    print(f"{len(earned)} hidden duplicate note(s) have earned erasure (set aside long enough to be safe to remove).")
    print("Each is STILL SAVED and fully recoverable until you merge a pull request to erase it:\n")
    now = int(time.time())
    for r in earned:
        print(f"  - {_cost_for(r, now)}")
        print(f"      (its own words, shown only here on your machine: {forget._snippet(r.get('text'))})")
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "session-start":
        import hooks  # noqa: E402 — lazy
        return hooks.run_hook("SessionStart", _session_start_handler)
    if cmd == "candidates":
        return _print_candidates()
    if cmd == "propose":
        print(propose()["message"])
        return 0
    if cmd == "demo":
        return _demo_live() if "--live" in argv[1:] else _demo()
    print(f"usage: erasure_proposer.py [session-start|candidates|propose|demo [--live]]\nunknown command {cmd!r}",
          file=sys.stderr)
    return 2


# --- operator demonstration (REAL probe + proposal + the REAL observer read; only the network/opener are stubbed) ---
# A walkthrough on a THROWAWAY practice cabinet + a throwaway working tree. It runs the REAL earned-erasure probe, the
# REAL proposal builder, the REAL observer's read of the written file, and the REAL auto-open + cross-PR dedup against
# an in-memory GitHub — only the network and the git/PR open are faked. It proves: the engine picks the OLD hidden
# duplicates (not the fresh one), describes EACH in plain words carrying NONE of its text, writes ONE batch proposal the
# live observer reads back in full, opens ONE labelled pull request clearing them all, and on a re-run opens NOTHING (it
# never re-pesters). Two notes here because the demo plants two; in real use the batch size tracks the backlog, and one
# merge consents to the whole batch. Vary the ages / the window near the top and re-run:
#     uv run --directory .engine --frozen -- python tools/memory/erasure_proposer.py demo
_DEMO_SESSION = "session-proposer"
_DEMO_OLD_TEXT = "Lesson: never deploy on a Friday — the rollback ate the whole weekend. RUMBLEDETHUMPS."
_DEMO_OLD_WORD = "rumbledethumps"       # a distinctive word that must NEVER appear in the committed proposal
_DEMO_OLD_ROLE = "lesson"
_DEMO_OLD_AGE_DAYS = 40                  # older than the ~1-month window -> EARNED        (VARY this)
_DEMO_OLD2_TEXT = "Decision: the archived logs move to cold storage after ninety days. CLAPSHOT."
_DEMO_OLD2_WORD = "clapshot"            # a second distinctive word that must NEVER appear in the committed proposal
_DEMO_OLD2_ROLE = "decision"
_DEMO_OLD2_AGE_DAYS = 35                 # also earned, slightly younger -> the batch holds BOTH (VARY this)
_DEMO_FRESH_TEXT = "Decided the new launch banner ships in the spring release."
_DEMO_FRESH_WORD = "banner"
_DEMO_FRESH_ROLE = "decision"
_DEMO_FRESH_AGE_DAYS = 3                 # too fresh -> NOT earned                          (VARY this)


def _plant_retired(text: str, role: str, age_days: int, batch: str) -> str:
    """Plant one real, back-dated, logically-retired note (an episodic with an OPEN batch — a crash-duplicate orphan
    recall already hides); return its content-free id."""
    from memory import consolidate
    rec = consolidate._make_episodic(_DEMO_SESSION, {"role": role, "text": text}, batch)
    rec["ts"] = int(time.time()) - age_days * _DAY
    ledger.append(rec)                                  # appended with no closing marker -> retired
    return rec[records.RECORD_ID_KEY]


class _DemoHub:
    """A tiny in-memory GitHub for the demo: the auto-open registers a pull request here, and the re-run's dedup reads
    it back through the REAL observer — so the demo exercises the real open + dedup offline."""

    def __init__(self, root: str):
        self._root = root
        self._prs: dict = {}
        self._contents: dict = {}
        self._labels: set = set()
        self._next = 4242

    def transport(self, method, path, body):
        if path.endswith(f"/labels/{observer.ERASURE_LABEL}") and method == "GET":
            return (200, {"name": observer.ERASURE_LABEL}) if observer.ERASURE_LABEL in self._labels else (404, None)
        if "/issues/" in path and path.endswith("/labels") and method == "POST":
            return 200, []
        if path.endswith("/labels") and method == "POST":
            self._labels.add((body or {}).get("name"))
            return 201, {}
        if "/issues?" in path:
            return 200, [{"number": n, "pull_request": {}} for n in sorted(self._prs)]
        m = re.search(r"/pulls/(\d+)", path)
        if m:
            pr = self._prs.get(int(m.group(1)))
            return (200, pr) if pr else (404, None)
        m = re.search(r"/contents/.*\?ref=([0-9A-Za-z]+)", path)
        if m:
            content = self._contents.get(m.group(1))
            return (200, {"content": content, "encoding": "base64"}) if content else (404, None)
        return 404, None

    def open(self, branch, title, body, paths, *, repo=None, token=None):
        with open(os.path.join(self._root, observer._PROPOSAL_PATH), encoding="utf-8") as fh:
            proposal = json.load(fh)
        number = self._next
        self._next += 1
        sha = f"head{number}cafe"
        self._prs[number] = {"number": number, "merged_at": None, "merge_commit_sha": None, "head": {"sha": sha}}
        self._contents[sha] = base64.b64encode(json.dumps(proposal).encode("utf-8")).decode("ascii")
        return number


def _demo() -> int:
    import tempfile

    print("=" * 96)
    print("MEMORY — the engine proposes erasing an OLD hidden duplicate: it opens the pull request, YOU consent by")
    print("         merging it (practice run)")
    print("=" * 96)
    with tempfile.TemporaryDirectory() as cabinet, tempfile.TemporaryDirectory() as tree:
        os.environ["ENGINE_MEMORY_DIR"] = cabinet           # the throwaway memory cabinet
        try:
            ok = _demo_body(tree)
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)

    print("\n" + "-" * 96)
    print("What this just proved: the engine picks the OLD hidden duplicates (not a recent one), describes EACH in plain")
    print("words that carry NONE of the note's text, and opens ONE single-purpose pull request clearing them all — which")
    print("it labels so a later session can find it. The pull-request body lists one plain line per note, so you see the")
    print("whole batch you are consenting to; merging is all-or-nothing. They are PERMANENTLY erased only because YOU")
    print("merge that pull request; nothing is erased now, and nothing merges on its own. There are two notes here only")
    print("because the demo planted two — in real use the batch tracks your session backlog, and one merge clears it.")
    print("Run again and it opens nothing — it never re-pesters you about notes already in front of you. On a fresh")
    print("project there are no old duplicates, so it proposes nothing; if GitHub is unreachable it simply tries again")
    print(f"next time — nothing breaks. That was a PRACTICE cabinet, thrown away. Vary it: change the note ages or")
    print(f"EARNED_ERASURE_MIN_AGE_DAYS (now {EARNED_ERASURE_MIN_AGE_DAYS}) near the top and re-run — watch the OLD notes")
    print("become earned and the FRESH one stay safe.")
    return 0 if ok else 1


def _demo_body(tree: str) -> bool:
    old_id = _plant_retired(_DEMO_OLD_TEXT, _DEMO_OLD_ROLE, _DEMO_OLD_AGE_DAYS, "batch-old")
    old2_id = _plant_retired(_DEMO_OLD2_TEXT, _DEMO_OLD2_ROLE, _DEMO_OLD2_AGE_DAYS, "batch-old2")
    _plant_retired(_DEMO_FRESH_TEXT, _DEMO_FRESH_ROLE, _DEMO_FRESH_AGE_DAYS, "batch-fresh")
    old_ids = {old_id, old2_id}

    # --- PART 1 ------------------------------------------------------------------------------------------
    print(f"\nPART 1 — three hidden duplicates: two old enough to have earned erasure, one ~{_DEMO_FRESH_AGE_DAYS} days old")
    print("-" * 96)
    earned = earned_targets()
    picked = [r[records.RECORD_ID_KEY] for r in earned]
    print(f"  the engine considers {len(earned)} note(s) old enough to have earned erasure")
    print(f'  the two OLD notes ("...{_DEMO_OLD_WORD}...", "...{_DEMO_OLD2_WORD}..."): {"both selected" if old_ids <= set(picked) else "NOT both selected"}')
    print(f'  the FRESH note ("...{_DEMO_FRESH_WORD}..."): {"left alone (too recent)" if not any(p not in old_ids for p in picked) else "selected"}')
    part1 = set(picked) == old_ids
    print(f"  => {'only the two old hidden duplicates earned erasure.' if part1 else '!!! the wrong set was selected'}")

    # --- PART 2 ------------------------------------------------------------------------------------------
    print("\nPART 2 — the engine describes EACH in plain words that carry NONE of the notes' text (one line per note)")
    print("-" * 96)
    proposal = build_proposal(earned)
    serialized = json.dumps(proposal, ensure_ascii=False).lower()
    leaked = any(w.lower() in serialized for w in (_DEMO_OLD_WORD, _DEMO_OLD2_WORD))
    print("  what the pull request will say, one line per note:")
    for c in proposal["costs"]:
        print(f'    - "{c}"')
    print(f'  does anything committed contain either note\'s distinctive word? {"YES" if leaked else "no"}')
    part2 = (not leaked) and set(proposal["targets"]) == old_ids and len(proposal["costs"]) == len(proposal["targets"])
    print(f"  => {'the proposal names each note by an opaque tag and a plain description — no words leak, one cost per target.' if part2 else '!!! the note content leaked, or the wrong set was named'}")

    # --- PART 3 ------------------------------------------------------------------------------------------
    print("\nPART 3 — the engine writes ONE batch proposal, and the REAL later-session reader reads back BOTH notes")
    print("-" * 96)
    dest = write_proposal(proposal, root=tree)
    with open(dest, "rb") as fh:
        raw = fh.read()

    def _serve(method, path, body):
        if "/contents/" in path:
            return 200, {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}
        return 404, None

    resolved = observer._read_targets(observer._FakeGH(_serve), "demo-merge-sha")
    print(f"  wrote the batch proposal to the committed path: {observer._PROPOSAL_PATH}")
    print(f"  the later session reads back the SAME notes: {'yes' if set(resolved) == old_ids else 'NO'}")
    part3 = set(resolved) == old_ids and set(resolved) == set(proposal["targets"])
    print(f"  => {'the file the engine writes is exactly the file the later session erases by.' if part3 else '!!! the reader did not accept what was written'}")

    # --- PART 4 ------------------------------------------------------------------------------------------
    print("\nPART 4 — the engine opens ONE single-purpose pull request clearing BOTH notes, and labels it")
    print("-" * 96)
    hub = _DemoHub(tree)
    result = propose(opener=hub.open, transport=hub.transport, root=tree)
    print(f"  {result['message']}")
    part4 = (result["opened"] == [4242] and set(result["targets"]) == old_ids
             and observer.ERASURE_LABEL in hub._labels and result.get("labelled") is True)
    print(f"  => {'one labelled pull request, clearing the whole batch, opened for your review (it does not merge itself).' if part4 else '!!! it opened the wrong number of pull requests, cleared the wrong set, or did not label it'}")

    # --- PART 5 ------------------------------------------------------------------------------------------
    print("\nPART 5 — run the review again: it opens NOTHING (it never re-pesters you about notes already in front of you)")
    print("-" * 96)
    again = propose(opener=hub.open, transport=hub.transport, root=tree)
    print(f"  {again['message']}")
    part5 = again["opened"] == []
    print(f"  => {'notes already in front of you are left alone — no duplicate pull request.' if part5 else '!!! it opened a duplicate pull request'}")

    # --- PART 6 ------------------------------------------------------------------------------------------
    print(f"\nPART 6 — the weekly throttle: after a check it does not even LOOK again until ~{EARNED_ERASURE_CHECK_INTERVAL_DAYS} days pass")
    print("-" * 96)
    base = 1_000_000_000
    fresh_look = _should_check(base)                       # no record yet -> look now
    _record_check(base)
    too_soon = _should_check(base + 2 * _DAY)              # 2 days later -> within the cooldown, do not even look
    elapsed = _should_check(base + (EARNED_ERASURE_CHECK_INTERVAL_DAYS + 1) * _DAY)   # interval passed -> look again
    print(f"  a first session (no record yet): {'looks now' if fresh_look else 'skips'}")
    print(f"  2 days later: {'looks' if too_soon else 'skips — not yet a week, so no GitHub call and nothing opens'}")
    print(f"  {EARNED_ERASURE_CHECK_INTERVAL_DAYS + 1} days later: {'looks again' if elapsed else 'still skips'}")
    part6 = fresh_look and (not too_soon) and elapsed
    print(f"  => {'it checks at most once a week. Three distinct reasons it stays quiet: nothing earned, a request already open, or simply not yet time.' if part6 else '!!! the throttle did not gate as expected'}")

    return part1 and part2 and part3 and part4 and part5 and part6


def _demo_live() -> int:
    """The LIVE end-to-end test the operator runs himself: plant a fake earned note in a THROWAWAY memory store (the
    real ledger is never read or touched) and run the REAL armed trigger so it opens a REAL `engine-erasure` pull
    request you can see in GitHub. The API opener changes no local branch or file, so cleanup is just closing the PR.
    Nothing is erased — the pull request only PROPOSES."""
    import tempfile
    print("=" * 96)
    print("LIVE TEST — this opens a REAL pull request on your GitHub repo, on purpose. It NEVER touches your real")
    print("            memory (the test note lives in a throwaway store) and changes NO local branch or file (the pull")
    print("            request is built entirely via the GitHub API).")
    print("=" * 96)
    with tempfile.TemporaryDirectory() as cabinet:
        os.environ["ENGINE_MEMORY_DIR"] = cabinet           # the throwaway store — the real ledger is never used
        try:
            _plant_retired(_DEMO_OLD_TEXT, _DEMO_OLD_ROLE, _DEMO_OLD_AGE_DAYS, "batch-live")
            print(f"\nPlanted one fake ~{_DEMO_OLD_AGE_DAYS}-day-old hidden duplicate in the throwaway store; opening a real pull request...\n")
            result = propose()                              # un-injected -> the real gh + the real API opener
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)
    print(f"  {result['message']}")
    opened = result.get("opened") or []
    if opened:
        print("\n  Open it in GitHub and read the plain-language body — nothing is erased; it only PROPOSES erasing one")
        print("  note. When you are done (your real memory was never touched), clean it up with one command:")
        print(f"      gh pr close {opened[0]} --delete-branch")
    else:
        print("\n  No pull request opened (see the message above — e.g. GitHub unreachable, an erasure request already")
        print("  open, or no credentials resolved). Your real memory was never read or touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
