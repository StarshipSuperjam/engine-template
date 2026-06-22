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

D-007 (content-free binding): the committed proposal carries `{"target": <stable content-free record id>, "cost":
<plain-language paraphrase>}`. `target` is the note's uuid-hex id (reveals nothing about the gitignored content);
`cost` is built ONLY from content-free metadata (the note's role/kind rendered as plain words + a coarse age bucket) —
never the note's text, never its session id or tags. A test scans the whole serialized proposal for the note's
distinctive words and flips red if any leaks.

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

_DAY = 86400

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


def build_proposal(record: dict, *, now: "int | None" = None) -> dict:
    """The committed proposal `{"target", "cost"}` for one earned note — EXACTLY those two keys, both content-free.
    `target` is the note's stable id (validated to the observer's record-id shape). `cost` is plain language built
    ONLY from the note's role (a closed engine vocabulary, rendered to plain words) + a coarse age bucket — never the
    note's `text`, never its `session_id`/`tags` (D-007). Raises if the record has no valid content-free id."""
    target = record.get(records.RECORD_ID_KEY)
    if not observer._is_record_id(target):
        raise ValueError("refusing to build a proposal for a record without a content-free id")
    ts = record.get("ts")
    now = int(time.time()) if now is None else now
    age = now - ts if isinstance(ts, int) and not isinstance(ts, bool) else 0
    cost = (f"{_role_phrase(record.get('role'))} the engine set aside as a duplicate of a save that didn't finish — "
            f"{_age_phrase(age)}; already hidden from recall and still fully recoverable until erased.")
    return {"target": target, "cost": cost}


def write_proposal(proposal: dict, *, root: "str | None" = None) -> str:
    """Write the proposal to the observer's fixed committed path under `root` (the repo root by default; a throwaway
    root in tests/demo). Refuses to write a proposal whose `target` is not a content-free record id (so an invalid
    target can never land). Overwrites in place — there is only ever one canonical proposal. Returns the path written."""
    target = proposal.get("target")
    if not observer._is_record_id(target):
        raise ValueError("refusing to write a proposal whose target is not a content-free record id")
    if root is None:
        import validate  # lazy: only the real write needs the repo root
        root = validate.ROOT
    dest = os.path.join(root, observer._PROPOSAL_PATH)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump({"target": target, "cost": proposal.get("cost", "")}, fh, indent=2, ensure_ascii=False)
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
        target = observer._read_target(gh, ref)       # reads ONLY the content-free id from the committed proposal
        if target:
            out.add(target)
    return out


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

def _open_erasure_pr(branch: str, title: str, body: str, paths: list, *, repo=None, token=None):
    """THE GIT+PR BOUNDARY: stage ONLY the proposal file on a new branch, commit, push, and open the single-purpose
    pull request (POST /pulls). Mirrors `module_manager._open_upgrade_pr`, but stages ONLY `paths` (never `-A`) so the
    merge tree the observer reads carries exactly the one proposal change. INJECTED for tests + the demo (propose's
    `opener=...`) and reached AUTOMATICALLY by nothing (no hook/cron calls `propose`), so this real path runs only on
    a deliberate operator `propose` — never in tests/demo, never on its own. Returns the new PR number."""
    import subprocess
    import urllib.request
    import json as _json
    import boot
    import validate
    slug = repo or boot.repo_slug()
    tok = token if token is not None else boot.gh_token()
    if not slug or not tok:
        raise RuntimeError("could not determine the repository / credentials to open the erasure pull request.")
    base = getattr(boot, "PROTECTED_BRANCH", "main")
    for args in (["git", "checkout", "-b", branch], ["git", "add", *paths],
                 ["git", "commit", "-m", title], ["git", "push", "-u", "origin", branch]):
        subprocess.run(args, cwd=validate.ROOT, check=True, capture_output=True)
    url = f"https://api.github.com/repos/{slug}/pulls"
    payload = _json.dumps({"title": title, "head": branch, "base": base, "body": body}).encode("utf-8")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "engine-erasure-proposer", "Authorization": f"Bearer {tok}",
               "Content-Type": "application/json"}
    with urllib.request.urlopen(urllib.request.Request(url, data=payload, headers=headers), timeout=60) as resp:
        pr = _json.loads(resp.read())
    return pr.get("number")


def _branch_for(target: str) -> str:
    return f"erasure-{target[:12]}"


_PR_TITLE = "Erase one remembered note (single-purpose)"


def _pr_body(proposal: dict) -> str:
    """The operator-facing pull-request body — plain language, the cost paraphrase, no engine jargon, no note content.
    Draws the consent picture explicitly: merging THIS pull request consents to erasing THIS one note (it does not
    erase now; a later session does), and declining loses nothing."""
    return (
        "This pull request proposes to **permanently erase one remembered note** from the engine's memory.\n\n"
        f"**What it is:** {proposal['cost']}\n\n"
        "Merging this pull request is your consent to erase that one note — the single thing the engine can do to its "
        "memory that cannot be undone. Nothing is erased the moment you merge: a later session carries out the "
        "erasure, and nothing merges on its own. If you would rather keep the note, just **close** this pull request — "
        "declining loses nothing (the note stays exactly where it is, still hidden from recall and fully recoverable).\n")


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
        return {"opened": [], "target": None, "message": "No notes have earned erasure — nothing to propose."}
    record = earned[0]
    target = record[records.RECORD_ID_KEY]
    if observer._already_enacted(target, path=path):
        return {"opened": [], "target": target,
                "message": "The oldest earned note is already scheduled for erasure."}
    gh = _reader(transport)
    if gh is None:
        return {"opened": [], "target": target,
                "message": "Could not reach GitHub; will try again at the next review."}
    proposed = _proposed_targets(gh)
    if proposed is None:
        return {"opened": [], "target": target,
                "message": "Could not check existing erasure pull requests; declined to open (no duplicate risk)."}
    if target in proposed:
        return {"opened": [], "target": target,
                "message": "An erasure pull request already covers this note."}
    proposal = build_proposal(record, now=now)
    dest = write_proposal(proposal, root=root)
    injected = opener is not None or transport is not None
    open_fn = opener or (None if injected else _open_erasure_pr)
    if open_fn is None:
        return {"opened": [], "target": target, "proposal": proposal, "wrote": dest,
                "message": "Wrote the proposal (practice run — no pull request opened)."}
    number = open_fn(_branch_for(target), _PR_TITLE, _pr_body(proposal), [observer._PROPOSAL_PATH],
                     repo=None, token=None)
    if number is None:
        return {"opened": [], "target": target,
                "message": "Could not open the pull request; will try again at the next review."}
    labelled = _apply_label(gh, number)
    return {"opened": [number], "target": target, "proposal": proposal, "labelled": labelled,
            "message": f"Opened a single-purpose erasure pull request (#{number}) for your review."}


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
        print(f"  - {build_proposal(r, now=now)['cost']}")
        print(f"      (its own words, shown only here on your machine: {forget._snippet(r.get('text'))})")
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "candidates":
        return _print_candidates()
    if cmd == "propose":
        print(propose()["message"])
        return 0
    if cmd == "demo":
        return _demo()
    print(f"usage: erasure_proposer.py [candidates|propose|demo]\nunknown command {cmd!r}", file=sys.stderr)
    return 2


# --- operator demonstration (REAL probe + proposal + the REAL observer read; only the network/opener are stubbed) ---
# A walkthrough on a THROWAWAY practice cabinet + a throwaway working tree. It runs the REAL earned-erasure probe, the
# REAL proposal builder, the REAL observer's read of the written file, and the REAL auto-open + cross-PR dedup against
# an in-memory GitHub — only the network and the git/PR open are faked. It proves: the engine picks the OLD hidden
# duplicate (not the fresh one), describes it in plain words carrying NONE of its text, writes a proposal the live
# observer accepts, opens ONE labelled pull request, and on a re-run opens NOTHING (it never re-pesters). Vary the ages
# / the window near the top and re-run:
#     uv run --directory .engine --frozen -- python tools/memory/erasure_proposer.py demo
_DEMO_SESSION = "session-proposer"
_DEMO_OLD_TEXT = "Lesson: never deploy on a Friday — the rollback ate the whole weekend. RUMBLEDETHUMPS."
_DEMO_OLD_WORD = "rumbledethumps"       # a distinctive word that must NEVER appear in the committed proposal
_DEMO_OLD_ROLE = "lesson"
_DEMO_OLD_AGE_DAYS = 40                  # older than the ~1-month window -> EARNED        (VARY this)
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
    print("What this just proved: the engine picks an OLD hidden duplicate (not a recent one), describes it in plain")
    print("words that carry NONE of the note's text, and opens ONE single-purpose pull request — which it labels so a")
    print("later session can find it. The note is PERMANENTLY erased only because YOU merge that pull request; nothing")
    print("is erased now, and nothing merges on its own. Run again and it opens nothing — it never re-pesters you about")
    print("a note already in front of you. On a fresh project there are no old duplicates, so it proposes nothing; if")
    print("GitHub is unreachable it simply tries again next time — nothing breaks. That was a PRACTICE cabinet, thrown")
    print(f"away. Vary it: change the note ages or EARNED_ERASURE_MIN_AGE_DAYS (now {EARNED_ERASURE_MIN_AGE_DAYS}) near")
    print("the top and re-run — watch the OLD note become earned and the FRESH one stay safe.")
    return 0 if ok else 1


def _demo_body(tree: str) -> bool:
    old_id = _plant_retired(_DEMO_OLD_TEXT, _DEMO_OLD_ROLE, _DEMO_OLD_AGE_DAYS, "batch-old")
    _plant_retired(_DEMO_FRESH_TEXT, _DEMO_FRESH_ROLE, _DEMO_FRESH_AGE_DAYS, "batch-fresh")

    # --- PART 1 ------------------------------------------------------------------------------------------
    print(f"\nPART 1 — two hidden duplicates: one ~{_DEMO_OLD_AGE_DAYS} days old, one ~{_DEMO_FRESH_AGE_DAYS} days old")
    print("-" * 96)
    earned = earned_targets()
    picked = [r[records.RECORD_ID_KEY] for r in earned]
    print(f"  the engine considers {len(earned)} note(s) old enough to have earned erasure")
    print(f'  the OLD note ("...{_DEMO_OLD_WORD}..."): {"selected" if old_id in picked else "NOT selected"}')
    print(f'  the FRESH note ("...{_DEMO_FRESH_WORD}..."): {"selected" if any(p != old_id for p in picked) else "left alone (too recent)"}')
    part1 = picked == [old_id]
    print(f"  => {'only the old hidden duplicate earned erasure.' if part1 else '!!! the wrong set was selected'}")

    # --- PART 2 ------------------------------------------------------------------------------------------
    print("\nPART 2 — the engine describes it in plain words that carry NONE of the note's text")
    print("-" * 96)
    proposal = build_proposal(earned[0])
    serialized = json.dumps(proposal, ensure_ascii=False)
    leaked = _DEMO_OLD_WORD.lower() in serialized.lower()
    print(f'  what the pull request will say: "{proposal["cost"]}"')
    print(f'  does anything committed contain the note\'s distinctive word "{_DEMO_OLD_WORD}"? {"YES" if leaked else "no"}')
    part2 = (not leaked) and proposal["target"] == old_id
    print(f"  => {'the proposal names the note by an opaque tag and a plain description — its words never leak.' if part2 else '!!! the note content leaked, or the wrong note was named'}")

    # --- PART 3 ------------------------------------------------------------------------------------------
    print("\nPART 3 — the engine writes the proposal, and the REAL later-session reader accepts exactly it")
    print("-" * 96)
    dest = write_proposal(proposal, root=tree)
    with open(dest, "rb") as fh:
        raw = fh.read()

    def _serve(method, path, body):
        if "/contents/" in path:
            return 200, {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}
        return 404, None

    resolved = observer._read_target(observer._FakeGH(_serve), "demo-merge-sha")
    print(f"  wrote the proposal to the committed path: {observer._PROPOSAL_PATH}")
    print(f"  the later session reads back the SAME note: {'yes' if resolved == old_id else 'NO'}")
    part3 = resolved == old_id and resolved == proposal["target"]
    print(f"  => {'the file the engine writes is exactly the file the later session erases by.' if part3 else '!!! the reader did not accept what was written'}")

    # --- PART 4 ------------------------------------------------------------------------------------------
    print("\nPART 4 — the engine opens ONE single-purpose pull request and labels it")
    print("-" * 96)
    hub = _DemoHub(tree)
    result = propose(opener=hub.open, transport=hub.transport, root=tree)
    print(f"  {result['message']}")
    part4 = result["opened"] == [4242] and observer.ERASURE_LABEL in hub._labels and result.get("labelled") is True
    print(f"  => {'one labelled pull request, opened for your review (it does not merge itself).' if part4 else '!!! it opened the wrong number of pull requests, or did not label it'}")

    # --- PART 5 ------------------------------------------------------------------------------------------
    print("\nPART 5 — run the review again: it opens NOTHING (it never re-pesters you about the same note)")
    print("-" * 96)
    again = propose(opener=hub.open, transport=hub.transport, root=tree)
    print(f"  {again['message']}")
    part5 = again["opened"] == []
    print(f"  => {'a note already in front of you is left alone — no duplicate pull request.' if part5 else '!!! it opened a duplicate pull request'}")

    return part1 and part2 and part3 and part4 and part5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
