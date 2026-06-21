"""erasure_observer.py — the cross-session Layer-2 erasure OBSERVER (memory-substrate, slice 4e PR ii).

Slice 4e is the memory substrate's single irreversible act: physically erasing a remembered note. Slice (i) built
the enactment core — a content-free `operator-adjudicated-erasure` marker, a gated removal inside `compact()`, and
`compact.enact_erasure(target_id, merge_sha)` (the SOLE minter, append-only) — and shipped it INERT (no live caller).
THIS module is the live caller: at SessionStart it turns a *merged single-purpose erasure pull request* into that
marker, so the next compaction removes the named note.

The consent gate is the locked law (memory/README): erasure happens ONLY because the operator merged a single-purpose
erasure PR — *the merge event only*; a merely-closed (declined / auto-resolved) Issue or PR NEVER erases. So the
observer:

  - **discovers** candidate erasure PRs by the dedicated `engine-erasure` label (D-210's "by label/search"), among
    CLOSED items only;
  - **confirms a genuine merge** — `merged_at` non-null AND a real `merge_commit_sha` — never acting on a close;
  - **binds the target to the IMMUTABLE merge tree** — it reads the content-free target id from a committed proposal
    file at `?ref=merge_commit_sha` (the merge commit's tree is frozen; the PR *body* is post-merge-mutable and is
    NEVER read). It validates the id is exactly the content-free record-id shape and reads NOTHING but that id (no
    gitignored ledger content, no operator-facing cost — D-007);
  - **dedups on the target id alone** — the retained marker (slice-i tombstone) is the cross-session dedup ledger, so
    a re-merged PR never re-mints or re-fires the one-time heads-up;
  - **enacts** via `compact.enact_erasure`, and on a NEW enactment relays ONE plain-language heads-up.

Posture: **fail-SAFE on consent, fail-OPEN on host.** Any doubt — no token, unreachable GitHub, unmerged, an
unreadable/malformed proposal, a bad id shape — yields no erasure and a silent proceed (retry next session). The
SessionStart hook can never block or slow the session past one bounded, swallowed read.

Why this is not an AI-reachable note-shredder (the hazard slice i dropped the `erase` CLI to avoid): the merge SHA
comes from a GENUINE merge to protected `main`, never from argv. An AI in-session cannot merge to protected `main`,
cannot fabricate `merged_at`/`merge_commit_sha`, and cannot forge the committed proposal at the merge tree — so there
is deliberately NO real-ledger arbitrary-mint verb here either. The label only *discovers*; the binding is the
immutable proposal@sha, so a mislabelled random merged PR (no proposal at its tree) is a no-op.

Leaf discipline: stdlib + the cycle-free `memory` set (`compact` / `ledger` / `records`) + the sibling `hooks`; the
GitHub reader (`boot` resolvers + `telemetry`'s 2-tuple transport) is lazy-imported inside the network path so the
cold-start load stays light. Tests/demo replace ONLY the injected `_transport` (`gh._transport(method, path, body) ->
(status, json)`) — no live GitHub. Run the demo:
    uv run --directory .engine --frozen -- python tools/memory/erasure_observer.py demo
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import hooks  # noqa: E402 — .engine/tools/hooks.py: the SessionStart fail-open harness
from memory import compact, ledger, records  # noqa: E402

ERASURE_LABEL = "engine-erasure"   # the dedicated label a single-purpose erasure PR carries (the (ii)↔(iii) contract)

# The committed proposal file the observer reads at the PR's merge tree. A COMMITTED path (NOT under the gitignored
# `.engine/memory/` ledger dir, which could not be committed). Slice (iii) MUST emit `{"target": <id>, "cost": <plain
# paraphrase>}` here and OWNS this file's main-tree lifecycle; the observer commits nothing here and reads ONLY `target`.
_PROPOSAL_PATH = ".engine/erasures/proposal.json"

# The content-free record-id shape: `records.new_record_id()` mints a uuid4 hex = exactly 32 lowercase hex chars.
# Validating against this exact shape (not a loose hex match) makes a malformed/foreign `target` fail-SAFE.
_RECORD_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")


# --- the GitHub reads (every read fail-OPEN: returns None / [] on any doubt, NEVER raises) -----------------

def _get(gh, path: str):
    """One GET through the injected transport. Returns parsed JSON, or None on ANY failure — an HTTP error
    (status >= 400), a null body, or a transport exception (e.g. GitHub unreachable). Fail-OPEN: the observer
    must never raise into a session start; a read failure simply means "skip" upstream."""
    try:
        status, data = gh._transport("GET", path, None)
    except Exception:  # noqa: BLE001 — a transport fault (unreachable host, etc.) must degrade to "skip", never raise
        return None
    if not isinstance(status, int) or status >= 400 or data is None:
        return None
    return data


def discover_erasure_pr_numbers(gh) -> list:
    """Candidate erasure-PR numbers: CLOSED items carrying the `engine-erasure` label that are PRs (the issues
    endpoint lists PRs too — a PR carries a `pull_request` key). Returns a list of ints (possibly empty); never
    raises. Bounded to the few real erasure PRs by the label filter (D-210's "by label/search")."""
    data = _get(gh, f"/repos/{gh.repo}/issues?state=closed&labels={ERASURE_LABEL}&per_page=100")
    if not isinstance(data, list):
        return []
    return [item["number"] for item in data
            if isinstance(item, dict) and "pull_request" in item and isinstance(item.get("number"), int)]


def _is_genuinely_merged(pr) -> bool:
    """True iff this PR object shows a GENUINE merge: a non-null `merged_at` AND a non-empty `merge_commit_sha`.
    A merely-closed (declined / auto-resolved) PR has `merged_at` null — the locked "merge event only" gate; such
    a PR never erases."""
    if not isinstance(pr, dict):
        return False
    merge_sha = pr.get("merge_commit_sha")
    return bool(pr.get("merged_at")) and isinstance(merge_sha, str) and bool(merge_sha)


def _is_record_id(value) -> bool:
    """True iff `value` is exactly the content-free record-id shape (`uuid4().hex` — 32 lowercase hex chars)."""
    return isinstance(value, str) and _RECORD_ID_RE.match(value) is not None


def _read_target(gh, merge_sha: str):
    """The content-free target id, read from the committed proposal file at the IMMUTABLE merge tree
    (`?ref=merge_sha`). Returns a validated record id, or None on ANY doubt (fail-SAFE): 404/absent, a directory
    (a list body), a non-`base64` encoding (the `"none"` GitHub returns for >1 MB blobs), a base64/JSON decode
    error, a non-object body, or a missing/malformed `target`. Reads ONLY `target` — never the operator-facing
    `cost`, never any ledger content."""
    data = _get(gh, f"/repos/{gh.repo}/contents/{_PROPOSAL_PATH}?ref={merge_sha}")
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        return None
    raw = data.get("content")
    if not isinstance(raw, str):
        return None
    try:
        obj = json.loads(base64.b64decode(raw).decode("utf-8"))  # base64 may carry embedded newlines — decode tolerates
    except Exception:  # noqa: BLE001 — a malformed/corrupt proposal is a doubt -> skip (fail-SAFE)
        return None
    target = obj.get("target") if isinstance(obj, dict) else None
    return target if _is_record_id(target) else None


# --- dedup + enactment ------------------------------------------------------------------------------------

def _erased_targets(path: "str | None" = None) -> set:
    """The set of target ids the ledger ALREADY holds an erasure marker for — the cross-session dedup ledger. The
    marker is retained across compaction (slice-i tombstone), so this set persists even after the note is gone."""
    return {r.get(records.TARGET_KEY) for r in ledger.iter_records(path=path)
            if isinstance(r, dict) and r.get("kind") == records.ERASURE_KIND}


def _already_enacted(target: str, *, path: "str | None" = None) -> bool:
    """True iff an erasure marker already targets `target` — dedup on the TARGET ID ALONE (a content-free id is
    unique; once any merge authorised erasing it, a re-merged PR must not re-mint or re-fire the heads-up)."""
    return target in _erased_targets(path)


def enact_from_merged_prs(gh, *, path: "str | None" = None) -> list:
    """Discover merged `engine-erasure` PRs, enact each not-yet-enacted target, and return the PR numbers NEWLY
    enacted THIS run (for the one-time heads-up). Pure orchestration over the fail-open reads + the slice-i minter;
    reads the ledger's existing targets ONCE and dedups in-memory (so two PRs naming one target, or a re-run, never
    double-mint). Never raises."""
    seen = _erased_targets(path)
    enacted: list = []
    for number in discover_erasure_pr_numbers(gh):
        pr = _get(gh, f"/repos/{gh.repo}/pulls/{number}")
        if not _is_genuinely_merged(pr):
            continue
        merge_sha = pr["merge_commit_sha"]
        target = _read_target(gh, merge_sha)
        if target is None or target in seen:
            continue
        if compact.enact_erasure(target, merge_sha, path=path) is not None:
            seen.add(target)
            enacted.append(number)
    return enacted


# --- the SessionStart hook (fail-open; one-time plain-language heads-up) -----------------------------------

def _reader():
    """A GitHub reader over the operator's `gh` token, or None when the repo/token can't be resolved (a degraded
    host — proceed silently). Reuses boot's resolvers + `telemetry`'s 2-tuple transport; lazy-imported so the
    cold-start load path stays light until the observer actually reads."""
    import boot       # noqa: E402 — lazy: keep boot's heavy import graph off the module-load path
    import telemetry  # noqa: E402 — its GitHubIssues exposes the injectable 2-tuple `_transport` (= `_http`)
    repo = boot.repo_slug()
    token = boot.gh_token()
    if not repo or not token:
        return None
    return telemetry.GitHubIssues(repo, token)


def _heads_up(pr_numbers: list) -> str:
    """The model-facing relay for a NEW enactment — plain language, the operator's chosen one-time heads-up. Names
    the PR(s) the operator merged (operator-recognisable, immutable) and NEVER the note's content."""
    refs = ", ".join(f"#{n}" for n in sorted(set(pr_numbers)))
    count = len(set(pr_numbers))
    note = "one remembered note is" if count == 1 else f"{count} remembered notes are"
    return (
        f"INFORM THE USER, in plain language (they asked to be told once): because they merged a single-purpose "
        f"erasure pull request ({refs}), {note} now scheduled to be permanently erased at the next memory tidy — "
        f"the one action on their memory that cannot be undone, happening only because they merged it.")


def _session_start_handler(payload) -> dict:
    """Memory's cross-session erasure OBSERVER at SessionStart. Fail-open throughout: resolve a reader, enact any
    merged erasure PR not yet acted on, and on a NEW enactment relay ONE plain-language heads-up; otherwise proceed
    silently. Any doubt or fault -> a silent proceed (the session is never blocked; the observer retries next
    session). `payload` is unused (the observer reads GitHub, not the event)."""
    try:
        gh = _reader()
        if gh is None:
            return hooks.proceed()                 # no repo/token -> degraded host, silent
        enacted = enact_from_merged_prs(gh)
        if enacted:
            return hooks.inject(_heads_up(enacted))
    except Exception:  # noqa: BLE001 — fail-open: a fault here must never strand the session start
        return hooks.proceed()
    return hooks.proceed()


# --- operator demonstration (REAL observer logic; only the GitHub transport is stubbed) --------------------
# A walkthrough on a THROWAWAY practice cabinet. It runs the REAL discovery -> genuine-merge -> read-target@tree ->
# dedup -> enact -> compact path; only the GitHub network is a stub at the injectable seam. It proves: the engine
# acts on the PR you MERGED (not one merely closed) and on what you COMMITTED (not the editable PR body), erases only
# that note, and never repeats. Vary which note the merged PR authorises (by its word) at the top and re-run:
#     uv run --directory .engine --frozen -- python tools/memory/erasure_observer.py demo
_DEMO_SESSION = "session-observer"
_DEMO_KEEP_TEXT = "Decided the harbor festival keeps its Saturday fireworks. KEEP-THIS-NOTE."
_DEMO_KEEP_WORD = "fireworks"
_DEMO_GONE_TEXT = "Withdrawn idea: move the depot onto the floodplain. ERASE-THIS-NOTE."
_DEMO_GONE_WORD = "floodplain"
_DEMO_AUTHORISED = "gone"   # which note the merged PR authorises erasing: "gone" (default) or "keep" — VARY and re-run
_DEMO_MERGED_PR = 7         # the merged single-purpose erasure PR
_DEMO_CLOSED_PR = 8         # a control: CLOSED but NOT merged -> the observer must leave it alone
_DEMO_MERGE_SHA = "a1b2c3d4e5f600000000000000000000deadbeef"


class _FakeGH:
    """A stand-in GitHub reader for the demo/tests: a fixed repo + an injected transport. Lets the REAL observer
    logic run fully offline ([[demo-must-exercise-real-logic]]) — only the network is faked."""

    def __init__(self, transport, *, repo="your-org/your-project"):
        self.repo = repo
        self._transport = transport


def _stub_transport(*, target_id: str, body_id: str, merged_sha: str = _DEMO_MERGE_SHA):
    """Answer the three GETs the observer makes: the by-label discovery (a merged PR + a closed-unmerged control,
    both labelled), each `/pulls/{n}`, and the proposal read at the merged PR's tree. The merged PR's BODY names
    `body_id` (the OTHER note) while the committed proposal names `target_id` — so the demo can prove the observer
    binds to the merge tree, not the editable body."""
    proposal = base64.b64encode(
        json.dumps({"target": target_id, "cost": "a withdrawn idea you decided to drop"}).encode("utf-8")
    ).decode("ascii")

    def transport(method, path, body):
        if "/issues?" in path:                                   # the by-label discovery (closed items)
            return 200, [{"number": _DEMO_MERGED_PR, "pull_request": {}},
                         {"number": _DEMO_CLOSED_PR, "pull_request": {}}]
        m = re.search(r"/pulls/(\d+)", path)
        if m and int(m.group(1)) == _DEMO_MERGED_PR:
            return 200, {"number": _DEMO_MERGED_PR, "merged_at": "2026-06-21T00:00:00Z",
                         "merge_commit_sha": merged_sha,
                         "body": f"Erase the withdrawn note. (an editable body that names {body_id})"}
        if m and int(m.group(1)) == _DEMO_CLOSED_PR:
            return 200, {"number": _DEMO_CLOSED_PR, "merged_at": None, "merge_commit_sha": None,
                         "body": "a different change that was closed without merging"}
        if "/contents/" in path and f"ref={merged_sha}" in path:
            return 200, {"content": proposal, "encoding": "base64"}
        return 404, None

    return transport


def _plant(text: str) -> str:
    """Plant one real, always-live note through the live factory; return its content-free id."""
    from memory import consolidate
    rec = consolidate._make_episodic(_DEMO_SESSION, {"role": "decision", "text": text}, "demo-batch")
    rec.pop(records.BATCH_KEY, None)               # always-live (not a crashed-pass orphan)
    ledger.append(rec)
    return rec[records.RECORD_ID_KEY]


def _found(word: str) -> int:
    from memory import index
    return len(index.query(word).records)


def _rebuild() -> None:
    from memory import index
    index.rebuild()


def _slips() -> int:
    return sum(1 for r in ledger.iter_records() if isinstance(r, dict) and r.get("kind") == records.ERASURE_KIND)


def _has_slip_for(target_id: str) -> bool:
    return any(isinstance(r, dict) and r.get("kind") == records.ERASURE_KIND
               and r.get(records.TARGET_KEY) == target_id for r in ledger.iter_records())


def _present(record_id: str) -> bool:
    return any(isinstance(r, dict) and r.get(records.RECORD_ID_KEY) == record_id
               for r in ledger.iter_records())


def _snippet(text, width: int = 66) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _demo() -> int:
    import tempfile

    print("=" * 92)
    print("MEMORY — the engine acts on the erasure pull request YOU merged: turning your consent into the act (practice)")
    print("=" * 92)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ENGINE_MEMORY_DIR"] = tmp          # the throwaway cabinet
        try:
            ok = _demo_body()
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)

    print("\n" + "-" * 92)
    print("What this just proved: when you MERGE a single-purpose erasure pull request, the engine schedules exactly")
    print("the one note you authorised for permanent erasure — and it acts on the pull request you MERGED (not one")
    print("merely closed) and on what you COMMITTED (not the pull-request text, which can be edited after the fact).")
    print("It tells you once, then never again. This is the ONE thing the engine can do to your memory that cannot be")
    print("undone, and it happens ONLY because you merged that pull request. Nothing in the engine yet CREATES such a")
    print("pull request (that is the next step), so on its own it still erases nothing. If GitHub is ever unreachable,")
    print("the engine simply carries on and tries again next session — nothing breaks. That was a PRACTICE cabinet,")
    print("thrown away. Vary it: at the top switch which note the merged pull request authorises (by its word) and")
    print("re-run — the erase follows what was committed; the note you did not authorise always survives.")
    return 0 if ok else 1


def _demo_body() -> bool:
    keep_id = _plant(_DEMO_KEEP_TEXT)
    gone_id = _plant(_DEMO_GONE_TEXT)
    _rebuild()
    if _DEMO_AUTHORISED == "keep":
        target_id, target_word, other_id, other_word, other_text = (
            keep_id, _DEMO_KEEP_WORD, gone_id, _DEMO_GONE_WORD, _DEMO_GONE_TEXT)
    else:
        target_id, target_word, other_id, other_word, other_text = (
            gone_id, _DEMO_GONE_WORD, keep_id, _DEMO_KEEP_WORD, _DEMO_KEEP_TEXT)
    # The merged PR's committed proposal names the AUTHORISED note; its editable BODY names the OTHER note.
    gh = _FakeGH(_stub_transport(target_id=target_id, body_id=other_id))

    # --- PART 1 ------------------------------------------------------------------------------------------
    print("\nPART 1 — two real notes are on file, both findable")
    print("-" * 92)
    found_target = _found(target_word)
    found_other = _found(other_word)
    print(f'  the note the merged pull request authorises erasing: search "{target_word}" -> found {found_target}')
    print(f'  the other note (you did NOT authorise it): search "{other_word}" -> found {found_other}')
    part1 = found_target == 1 and found_other == 1
    print(f"  => {'both notes are on file and findable.' if part1 else '!!! a note is missing at the start'}")

    # --- PART 2 ------------------------------------------------------------------------------------------
    print(f"\nPART 2 — the engine reads GitHub: it acts on the MERGED pull request (#{_DEMO_MERGED_PR}), not the one")
    print(f"         merely CLOSED (#{_DEMO_CLOSED_PR})")
    print("-" * 92)
    enacted = enact_from_merged_prs(gh)              # the REAL observer, against the stubbed GitHub
    print(f"  pull requests the engine acted on this session: {enacted}   (it ignored the closed-not-merged one)")
    print(f"  permanent-erase authorisations now on file: {_slips()}")
    print("  the one-time heads-up the engine would show you:")
    if enacted:
        print(f"    \"{_heads_up(enacted)[len('INFORM THE USER, in plain language (they asked to be told once): '):]}\"")
    part2 = enacted == [_DEMO_MERGED_PR] and _slips() == 1
    print(f"  => {'it acted only on the pull request you merged.' if part2 else '!!! it acted on the wrong pull request, or both, or none'}")

    # --- PART 3 ------------------------------------------------------------------------------------------
    print("\nPART 3 — it acted on what you COMMITTED, not on the pull-request text (which can be edited later)")
    print("-" * 92)
    scheduled_target = _has_slip_for(target_id)
    scheduled_other = _has_slip_for(other_id)
    print(f'  the merged pull request\'s editable text named the OTHER note ("{_snippet(other_text)}")')
    print(f"  the note actually scheduled is the one named in the committed file: {'the authorised note' if scheduled_target else 'NO'}")
    print(f"  the note named only in the editable text was left alone: {'yes' if not scheduled_other else 'NO (scheduled it!)'}")
    part3 = scheduled_target and not scheduled_other
    print(f"  => {'it bound to what you committed, not to text that can be edited after the fact.' if part3 else '!!! it followed the editable text instead of the committed file'}")

    # --- PART 4 ------------------------------------------------------------------------------------------
    print("\nPART 4 — the tidy erases it (once); the other note is untouched; re-checking GitHub changes nothing")
    print("-" * 92)
    report = compact.compact()
    target_gone = not _present(target_id)
    found_other_after = _found(other_word)
    enacted_again = enact_from_merged_prs(gh)        # the SAME merged PR, a later session -> dedup, no re-fire
    print(f'  the authorised note: search "{target_word}" -> found {_found(target_word)}   (physically gone: {"yes" if target_gone else "NO"})')
    print(f'  the other note: search "{other_word}" -> found {found_other_after}   (untouched)')
    print(f"  re-checking GitHub next session acted on: {enacted_again}   (none — already done; you are NOT told again)")
    part4 = (target_gone and report.get("erased") == 1 and found_other_after == 1
             and enacted_again == [] and _slips() == 1)
    print(f"  => {'erased once and only once; the other note survived; no second heads-up.' if part4 else '!!! the wrong note changed, it repeated, or it re-fired the notice'}")

    return part1 and part2 and part3 and part4


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "session-start":
        return hooks.run_hook("SessionStart", _session_start_handler)
    if cmd == "demo":
        return _demo()
    print(f"usage: erasure_observer.py [session-start|demo]\nunknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
