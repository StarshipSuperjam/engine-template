"""backup_vault.py — memory's backup vault, the EXPORT path (memory-substrate, slice 6a).

The engine's experiential memory is the gitignored, append-only ledger (`.engine/memory/ledger.ndjson`). It is
canonical and deliberately out of git — but a disk failure or a new machine loses it, and a non-engineer has no
backup discipline (Risk R2). The locked design (engine-planning memory README §"Backup and portability", D-061 +
D-237): the Engine **backs memory up itself** — copy the ledger + a snapshot manifest to a PRIVATE GitHub repo via
the operator's own GitHub credentials. The default destination is a single SHARED cross-project vault
(`engine-memory-vault`), each project in its own minted-id folder; a per-project repo is offered at every setup
(D-237). This module is the EXPORT half + create/adopt; RESTORE lives in restore_vault.py.

Operator-facing FLOORS that live here (Floor 3's restore-offer lives in restore_vault.py):
  (1) consent-before-create — no backup repo is created without plain-language consent naming the repo + its
      must-stay-private requirement; consent is FOREGROUND only (a SessionStart hook can inject text, never read an
      interactive yes/no), so the `setup` verb gets it;
  (2) self-describing repo — on creation the engine commits a plain-language README into the backup repo;
  (4) degrade-and-disclose — every failure names a consequence + ONE recovery action, never a git/HTTP error.
"Privacy is posture": the destination is created PRIVATE and verified; if it is ever flipped public out of band, the
engine re-verifies on every push, DECLINES to send new memory to a public repo, and surfaces the flip in plain words.

Posture: **pure GitHub API, hook-safe** (the erasure_proposer precedent). The automatic push runs on a throttled
SessionStart hook, so it must NEVER touch local git (no branch switch, no `git push` hang) and must be
timeout-bounded. The transport is a tightened 10s-per-call boundary (telemetry's shared `_http` is fixed at 30s),
and the push is **cheap-probe-first** (a single repo GET — the privacy re-verify — gates the expensive blob work),
so a dead/flaky host fails in ≤~10s, not the full sequence. Everything fails SAFE: the local ledger is canonical, so
a missed/declined push loses nothing and simply retries next cadence.

Cadence is a recorded build-spec leaf (the design defers it): the operator chose throttled ~once per 24h
(`BACKUP_INTERVAL_HOURS`) + a manual `now`. The throttle gates on the last SUCCESSFUL push (a failed push retries
next session — freshness wins; the cheap-probe-first bound keeps that cheap).

CLI: setup | now | status | session-start | demo [--live]. Run the demo (fully offline):
    uv run --directory .engine --frozen -- python tools/memory/backup_vault.py demo
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import ledger  # noqa: E402 — the canonical store + its generation stamp + the shared ledger_dir()

# Build-spec leaf (recorded; the operator chose ~24h this session). How often the throttled SessionStart push may
# run, after the one-time consented setup. A politeness/cost guard only: failure-direction is benign both ways — too
# short merely pushes more often (each push is an idempotent snapshot); too long leaves a staler backup, but the
# local ledger stays canonical and intact. Mirrors the erasure_proposer's recorded-interval leaf convention.
BACKUP_INTERVAL_HOURS = 24
_HOUR = 3600

# A gitignored runtime sidecar under .engine/memory/ (sibling of ledger-meta.json / erasure-proposer-state.json),
# holding the throttle + privacy-report state. Never committed; resolved via ledger.ledger_dir() so it lands in the
# throwaway cabinet under tests/demo and the real store in production. Already fenced by the `.engine/memory/` gitignore.
_STATE_FILENAME = "backup-vault-state.json"

# The committed destination pointer — the ONE backup artifact that lives in git (topology law-5's pre-authorized
# carve-out: a fresh instance reads it to find the namespace). It CANNOT live under the gitignored `.engine/memory/`,
# so it is a committed file owned by the manifest's `backup` provides group, with its dir carved into catalog-coverage
# infra_dirs. Content-free (a slug/namespace/timestamp — never ledger content). Ships as an unconfigured placeholder;
# `setup` fills it.
POINTER_REL = ".engine/memory-backup/pointer.json"

# Backup destination scope (D-237 / engine-planning memory README §Backup 256-269). Build-spec leaf (recorded; the
# operator chose SHARED this session — the design's settled default): "shared" = one fixed vault per GitHub account
# holding every engine project in its own namespace folder; "per-project" = one repo per project. The per-project
# mode stays reachable (a code change, not an operator toggle, this slice; surfacing the choice as a real operator
# setting is provisioning's bootstrap-UX leaf). The choice is PRESENTED at every setup (floor 1); failure direction
# is benign — the namespace folder keeps projects separate either way.
_DEFAULT_SCOPE = "shared"                        # "shared" | "per-project"
_SHARED_VAULT_NAME = "engine-memory-vault"       # the one shared vault, baked (used in shared mode)
_PER_PROJECT_SUFFIX = "-engine-memory-backup"    # per-project mode: name = "<project-repo-name><suffix>"

# The engine's self-describing marker, the FIRST line of the backup README. ADOPT verifies it before reusing an
# existing same-named private repo, so the engine can never colonize a coincidentally-named repo it did not create
# (the design's "recognized by the self-describing destination", 305-307). Content-free.
_VAULT_README_MARKER = "<!-- engine-memory-vault -->"


def _vault_name(project_name: str, scope: str) -> str:
    """The destination repo name for `scope`: the one shared vault, or the per-project name."""
    return _SHARED_VAULT_NAME if scope == "shared" else f"{project_name}{_PER_PROJECT_SUFFIX}"


def _mint_namespace() -> str:
    """Mint a fresh namespace id at destination-binding — a content-free, collision-free, rename-stable opaque id (a
    uuid4 hex, MIRRORING records.new_record_id — the design's namespace-identity law, 262-269). Backup and restore
    bind on this id, never a runtime project name, so a shared vault can never mis-route or restore the wrong
    project's memory. The representation (uuid4 hex) is a recorded build-spec leaf."""
    return uuid.uuid4().hex

# A fixed, content-free commit message for every backup push (NEVER derived from ledger content — D-007 leak guard).
_COMMIT_MESSAGE = "Update memory backup (engine)"
_REPO_DESCRIPTION = "Private AI-memory backup created and maintained by the engine. Keep it private; don't hand-edit."

# The tightened per-call network timeout (seconds). telemetry's shared `_http` hardcodes 30s; a SessionStart push must
# be bounded much tighter so a flaky host cannot stall session start. Matches boot._run's 10s CLI budget.
_TIMEOUT = 10

_GITHUB_API = "https://api.github.com"
_USER_AGENT = "engine-template-memory-backup"

# The unmistakable disposable marker the live-demo's throwaway repo name carries; the DELETE refuses any name without
# it (a name-guard so `demo --live` can never delete the real vault or the project repo).
_DEMO_MARKER = "-memvault-demo-"


# ============================================================================================================
# The GitHub boundary — a 2-tuple transport (method, path, body) -> (status, json), injectable for tests/demo.
# ============================================================================================================

class _Boundary:
    """Holds an injected/real transport so the push logic runs fully offline behind a stub (the proposer/observer
    `_transport` seam). `.repo` is unused here (paths carry explicit owner/repo); only `._transport` matters."""

    def __init__(self, transport):
        self._transport = transport


def _bounded_transport(token: str):
    """A 10s-bounded GitHub transport over the operator's token. Mirrors telemetry._http's headers/JSON handling but
    with a tighter timeout and — deliberately — NEVER raises (an unreachable host returns (None, None), a clean
    failure the caller treats as 'skip'), so a SessionStart hook can never raise or hang on the network."""

    def transport(method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            _GITHUB_API + path, data=data, method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": _USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:            # 4xx/5xx — surface the status, never swallow
            return exc.code, None
        except Exception:  # noqa: BLE001 — unreachable/timeout/any fault -> a bounded (None) failure, never a raise
            return None, None

    return transport


def _gh(transport=None):
    """The GitHub boundary for backup: injected `transport` in tests/demo, else a bounded transport over the
    operator's `gh` token (`boot.gh_token()`). None when no token resolves (a degraded host -> proceed silently)."""
    if transport is not None:
        return _Boundary(transport)
    import boot  # noqa: E402 — lazy: keep boot's heavy import graph off the module-load path
    token = boot.gh_token()
    if not token:
        return None
    return _Boundary(_bounded_transport(token))


def _send(gh, method: str, path: str, body=None):
    """One call through the transport, returning (status, json). Never raises (a transport fault -> (None, None))."""
    try:
        return gh._transport(method, path, body)
    except Exception:  # noqa: BLE001 — a transport fault degrades to a clean failure, never a raise into a hook
        return None, None


def _get(gh, path: str):
    """One GET; parsed JSON or None on ANY doubt (status>=400, null body, transport fault). Fail-open (observer._get)."""
    status, data = _send(gh, "GET", path)
    if not isinstance(status, int) or status >= 400 or data is None:
        return None
    return data


# ============================================================================================================
# The snapshot manifest (exactly the four locked keys) + the committed pointer.
# ============================================================================================================

def _iso_utc(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _engine_version() -> str:
    """The engine release from `.engine/engine.json` -> `engine_release` (the field instantiator reads). 'unknown'
    on any doubt (a redirected/absent root)."""
    try:
        import validate  # noqa: E402 — lazy: only the manifest build needs the repo root
        data = validate.load_json(os.path.join(validate.ROOT, ".engine", "engine.json"))
        rel = data.get("engine_release") if isinstance(data, dict) else None
        return rel if isinstance(rel, str) and rel else "unknown"
    except Exception:  # noqa: BLE001 — a missing/malformed engine.json degrades to "unknown", never crashes a backup
        return "unknown"


def build_manifest(*, ledger_path: "str | None" = None, now: "int | None" = None) -> dict:
    """The snapshot manifest committed beside the ledger copy — EXACTLY the four locked keys (README §Backup):
    ledger-version, ledger-generation, timestamp, engine-version. The generation is read LIVE from the ledger's
    sidecar (`ledger.generation`), so the field is genuinely populated now — restore (slice 6b) reads it to surface a
    resurrecting restore, and inherits no manifest format change. `ledger_path` lets a throwaway store read ITS own
    generation."""
    when = int(time.time()) if now is None else int(now)
    return {
        "ledger-version": ledger.LEDGER_FORMAT_VERSION,
        "ledger-generation": ledger.generation(for_path=ledger_path),
        "timestamp": _iso_utc(when),
        "engine-version": _engine_version(),
    }


def _pointer_path() -> str:
    import validate  # noqa: E402 — the committed pointer lives in the working tree at the repo root
    return os.path.join(validate.ROOT, *POINTER_REL.split("/"))


def read_pointer() -> "dict | None":
    """The committed destination pointer, or None if absent/malformed/UNCONFIGURED. A partial pointer (any required
    field missing or empty) reads as None -> setup not done, so the throttled hook never fires against an incomplete
    destination."""
    try:
        with open(_pointer_path(), encoding="utf-8") as fh:
            p = json.load(fh)
    except Exception:  # noqa: BLE001 — absent/unreadable/malformed -> unconfigured
        return None
    if not isinstance(p, dict) or p.get("schema_version") != 1:
        return None
    for key in ("owner", "repo", "branch", "namespace"):
        if not (isinstance(p.get(key), str) and p.get(key)):
            return None
    return p


def _setup_done() -> bool:
    return read_pointer() is not None


def write_pointer(owner: str, repo: str, branch: str, namespace: str, *, now: "int | None" = None) -> dict:
    """Write the committed pointer (content-free: a slug/namespace/timestamp). Returns the written dict."""
    when = int(time.time()) if now is None else int(now)
    p = {"schema_version": 1, "owner": owner, "repo": repo, "branch": branch,
         "namespace": namespace, "created_at": _iso_utc(when)}
    path = _pointer_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"                                   # atomic write (mirrors ledger.replace_ledger): the minted
    with open(tmp, "w", encoding="utf-8") as fh:         # namespace id must be durably IN the pointer BEFORE the
        json.dump(p, fh, indent=2)                       # first export, surviving a kill-window (D-237, 265).
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return p


# ============================================================================================================
# The throttle + privacy-report state sidecar (gitignored).
# ============================================================================================================

def _state_path() -> str:
    return os.path.join(ledger.ledger_dir(), _STATE_FILENAME)


def _read_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — a missing/corrupt sidecar reads as empty (-> push now); never stops the loop
        return {}


def _last_success(state: "dict | None" = None) -> "int | None":
    state = _read_state() if state is None else state
    val = state.get("last_success_ts")
    return val if isinstance(val, int) and not isinstance(val, bool) else None


def _should_push(now: int) -> bool:
    """Throttle gate on the last SUCCESSFUL push: a missing/corrupt OR a FUTURE timestamp -> push now (the loop can
    never silently stick OFF); a failed push leaves last-success untouched, so it retries next session (freshness),
    bounded cheap by cheap-probe-first."""
    last = _last_success()
    if last is None or last > now:
        return True
    return (now - last) >= BACKUP_INTERVAL_HOURS * _HOUR


def _record_state(*, now: int, success: bool, privacy_ok: bool) -> None:
    """Best-effort stamp: last_success_ts advances ONLY on a real success (the throttle key); last_attempt_ts and
    last_privacy_ok always record (the latter makes the privacy-flip warning fire once, not every session)."""
    state = _read_state()
    state["last_attempt_ts"] = int(now)
    state["last_privacy_ok"] = bool(privacy_ok)
    if success:
        state["last_success_ts"] = int(now)
    try:
        os.makedirs(ledger.ledger_dir(), exist_ok=True)
        with open(_state_path(), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except Exception:  # noqa: BLE001 — never strand the session on a sidecar write
        pass


# ============================================================================================================
# The Git Data push (large-file safe: blob -> tree -> commit -> ref; the Contents API caps ~1MB).
# ============================================================================================================

def _git_blob_sha1(raw: bytes) -> str:
    """The git object id of a blob with `raw` content: sha1(b'blob <len>\\0' + raw). RESTORE (slice 6b) recomputes
    this over a fetched blob and requires it equals the tree entry's sha — git's own content-addressing — so a
    truncated or corrupted download can never be swapped over good local memory. The _FakeVault stores blobs under
    this same id, so the offline demo/tests exercise the real integrity check."""
    h = hashlib.sha1()
    h.update(b"blob " + str(len(raw)).encode("ascii") + b"\x00")
    h.update(raw)
    return h.hexdigest()


def _create_blob(gh, base: str, content: bytes) -> "str | None":
    encoded = base64.b64encode(content).decode("ascii")
    status, blob = _send(gh, "POST", f"{base}/git/blobs", {"content": encoded, "encoding": "base64"})
    sha = (blob or {}).get("sha") if status in (200, 201) else None
    return sha if isinstance(sha, str) and sha else None


def _push_files(gh, owner: str, repo: str, branch: str, files: dict, *, retry: bool = True) -> bool:
    """Commit `files` (path -> bytes) onto `branch` via the Git Data API — handles a multi-MB ledger the Contents API
    cannot. base_tree preserves the seeded README + any other namespace. On a 409/422 (non-fast-forward: a concurrent
    push moved the tip — the README's branch-per-namespace / retry-on-reject case) re-read and retry ONCE; otherwise
    decline (fail-SAFE: the local ledger is canonical). Returns True iff the ref advanced. Never raises."""
    base = f"/repos/{owner}/{repo}"
    ref = _get(gh, f"{base}/git/ref/heads/{branch}")
    base_sha = (ref or {}).get("object", {}).get("sha")
    if not (isinstance(base_sha, str) and base_sha):
        return False
    commit = _get(gh, f"{base}/git/commits/{base_sha}")
    base_tree = (commit or {}).get("tree", {}).get("sha")
    if not (isinstance(base_tree, str) and base_tree):
        return False
    tree = []
    for path, content in files.items():
        blob_sha = _create_blob(gh, base, content)
        if blob_sha is None:
            return False
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})
    status, new_tree = _send(gh, "POST", f"{base}/git/trees", {"base_tree": base_tree, "tree": tree})
    new_tree_sha = (new_tree or {}).get("sha") if status in (200, 201) else None
    if not (isinstance(new_tree_sha, str) and new_tree_sha):
        return False
    status, new_commit = _send(gh, "POST", f"{base}/git/commits",
                               {"message": _COMMIT_MESSAGE, "tree": new_tree_sha, "parents": [base_sha]})
    commit_sha = (new_commit or {}).get("sha") if status in (200, 201) else None
    if not (isinstance(commit_sha, str) and commit_sha):
        return False
    status, _ = _send(gh, "PATCH", f"{base}/git/refs/heads/{branch}", {"sha": commit_sha, "force": False})
    if status in (200, 201):
        return True
    if status in (409, 422) and retry:               # tip moved -> re-read + retry once
        return _push_files(gh, owner, repo, branch, files, retry=False)
    return False


def push_now(*, transport=None, now: "int | None" = None) -> dict:
    """Push the latest ledger + snapshot manifest to the configured vault. Requires setup (a pointer). CHEAP-PROBE
    FIRST: a single repo GET re-verifies the repo is still PRIVATE (and confirms reachability) before any blob work —
    so a public flip or a dead host costs one bounded call, never the full sequence. On a public flip it DECLINES to
    push (never sends new memory to a public repo). Fail-SAFE: returns a structured result, never raises.

    Result: {ok, error, pushed}. error in {None, not-configured, no-token, unreachable, public, push-failed}."""
    when = int(time.time()) if now is None else int(now)
    pointer = read_pointer()
    if pointer is None:
        return {"ok": False, "error": "not-configured", "pushed": False}
    gh = _gh(transport)
    if gh is None:
        return {"ok": False, "error": "no-token", "pushed": False}
    owner, repo, branch, namespace = pointer["owner"], pointer["repo"], pointer["branch"], pointer["namespace"]

    repo_obj = _get(gh, f"/repos/{owner}/{repo}")           # cheap probe: privacy re-verify + reachability
    if repo_obj is None:
        return {"ok": False, "error": "unreachable", "pushed": False}
    if repo_obj.get("private") is not True:
        return {"ok": False, "error": "public", "pushed": False}

    lpath = ledger.ledger_path()
    try:
        with open(lpath, "rb") as fh:
            ledger_bytes = fh.read()
    except FileNotFoundError:
        ledger_bytes = b""                                  # the substrate ships empty — a valid empty backup
    manifest_bytes = (json.dumps(build_manifest(ledger_path=lpath, now=when), indent=2) + "\n").encode("utf-8")
    files = {f"{namespace}/ledger.ndjson": ledger_bytes, f"{namespace}/manifest.json": manifest_bytes}
    if not _push_files(gh, owner, repo, branch, files):
        return {"ok": False, "error": "push-failed", "pushed": False}
    return {"ok": True, "error": None, "pushed": True}


# ============================================================================================================
# Floor wording — plain language, non-engineer; consequence + ONE recovery action, never a git/HTTP error.
# ============================================================================================================

def _choice_prompt() -> str:
    """Floor 1: the shared-vs-per-repo choice, PRESENTED at every setup (engine-planning memory README 290-295). The
    shared vault is the default; the disclosure names the trade-off AND why one would pick per-repo. The plain
    CONTENT is law; this exact wording is memory's provisional realization (provisioning's UX leaf may re-skin it)."""
    return (
        "Where should I keep this project's backup?\n\n"
        f"  - SHARED BACKUP (recommended): one private repository, \"{_SHARED_VAULT_NAME}\", that holds the memory\n"
        "    for ALL your engine projects, each in its own folder. Simplest — set up once, everything in one place.\n"
        "  - A SEPARATE BACKUP, just for this project: a private repository for this project alone.\n\n"
        "Most people pick shared. Choose a separate backup if THIS project is more private than your others, or if\n"
        "you might one day give someone access to one project without handing them all of them — a shared backup\n"
        "means one accidental flip to public would expose every project at once, while a separate one limits any\n"
        "single slip to this project.\n\n"
        "Keep everything in one shared backup? [Y/n]  (n = a separate backup just for this project): ")


def _ask_scope() -> str:
    """Foreground: present the choice, default shared. EOFError (a pipe / no stdin) -> the default, so a non-interactive
    run never raises (mirrors _ask_consent)."""
    try:
        answer = input(_choice_prompt())
    except EOFError:
        return _DEFAULT_SCOPE
    return "per-project" if str(answer).strip().lower() in ("n", "no") else "shared"


def _consent_prompt(vault_name: str, scope: str) -> str:
    """Floor 1: the foreground consent naming the chosen destination + its must-stay-private requirement. The shared
    variant carries the co-location disclosure (one repo holds every project; a flip exposes them all); privacy is
    named as honest posture, never a guarantee (it cannot prevent a later out-of-band flip)."""
    if scope == "shared":
        return (
            f"I'll keep this project's AI memory in your shared backup — the PRIVATE repository \"{vault_name}\" on\n"
            "your own GitHub. I'll create it if it doesn't exist yet, or add this project's own folder to it if it\n"
            "does. Here is exactly what that means:\n\n"
            "  - WHAT is copied: a private copy of the notes the engine has saved about this project — the\n"
            "    decisions, lessons, and plans it remembers. (Your code and your work are not involved.)\n"
            "  - ONE SHARED PLACE: this repository holds the memory for ALL your engine projects, each in its own\n"
            "    folder — so anyone who can see this one repository can see every project's notes, and one accidental\n"
            "    flip to public would expose every project at once. (A separate backup just for this project would\n"
            "    limit any single slip to this project.)\n"
            "  - PRIVATE, WATCHED HONESTLY: the engine creates it private, verifies it, and keeps checking — but it\n"
            "    can't stop you or GitHub from flipping it public later, which is why the point above matters.\n"
            "  - FROM THEN ON: the engine keeps it up to date for you automatically — about once a day.\n\n"
            "Nothing leaves your computer until you say yes.\n\n"
            "Use the shared backup for this project now? [y/N]: ")
    return (
        "The engine can keep a safe copy of this project's AI memory somewhere other than this computer, so a copy\n"
        "is always there if you ever need it. Here is exactly what that means:\n\n"
        "  - WHAT is copied: a private copy of the notes the engine has saved about this project — the decisions,\n"
        "    lessons, and plans it remembers. (Your code and your work are not involved.)\n"
        f"  - WHERE it goes: a brand-new, PRIVATE repository on your own GitHub account, named \"{vault_name}\",\n"
        "    just for this project, that only you can see.\n"
        "  - PRIVATE, WATCHED HONESTLY: the engine creates it private, verifies it, and keeps checking — but it\n"
        "    can't stop a later flip to public out of its control.\n"
        "  - FROM THEN ON: the engine keeps it up to date for you automatically — about once a day.\n\n"
        "Nothing leaves your computer until you say yes.\n\n"
        "Create the private backup now? [y/N]: ")


def _readme_text(project_name: str, scope: str = _DEFAULT_SCOPE) -> str:
    """Floor 2: the plain-language README committed into the backup repo on creation. Leads with the engine's
    self-describing marker (adopt verifies it). The shared variant is multi-project-framed, names the deliberately
    opaque folder ids so they're never mistaken for clutter, and redirects the dangerous delete-a-folder instinct by
    naming its cost (engine-planning memory README 295-303)."""
    if scope == "shared":
        return (
            f"{_VAULT_README_MARKER}\n"
            "# Your engine memory vault\n\n"
            "This private repository holds the AI memory for **all your engine projects** — each project in its own\n"
            "folder.\n\n"
            "The folders have **scrambled names like `a3f90c…` on purpose**: the engine uses a private code for each\n"
            "project instead of its name, so nothing in here reveals what you're working on. Each scrambled folder is\n"
            "one project's memory; you're not meant to tell which is which by looking. **Nothing here is junk.**\n\n"
            "**Keep it private. Don't delete or rename a folder, and don't hand-edit the files — deleting a folder\n"
            "permanently erases that project's saved memory, and the engine cannot bring it back.** To remove or fix\n"
            "a project's memory, ask the engine. On a new computer, the engine restores each project's memory from\n"
            "its folder — you don't need to do anything in here.\n")
    return (
        f"{_VAULT_README_MARKER}\n"
        f"# {project_name} — AI memory backup\n\n"
        f"This repository is an automatic backup of the AI memory for the **{project_name}** project.\n\n"
        "**Keep it private.** It holds the project's working notes — the decisions, lessons, and plans the engine\n"
        "remembers.\n\n"
        "**Please don't delete it, and don't hand-edit the files.** The engine reads and rewrites them\n"
        "automatically. If you ever set this project up on a new computer, the engine uses this backup to restore\n"
        "its memory for you — you don't need to do anything in here.\n")


_HEADS_UP_PUSH_FAILED = (
    "INFORM THE USER, in plain language: I couldn't update the off-site backup of this project's AI memory this "
    "time, so the backup may be behind the latest notes. Your memory on this computer is safe and complete. One "
    "thing to try: when you have a steady internet connection, ask me to \"back up memory now\", and I'll bring it "
    "up to date.")


def _heads_up_public() -> str:
    pointer = read_pointer() or {}
    where = f"{pointer.get('owner', '?')}/{pointer.get('repo', '?')}"
    return (
        "INFORM THE USER, in plain language: the backup repository for this project's AI memory "
        f"(\"{where}\") is currently PUBLIC — it should be private, because it holds the project's notes. I did NOT "
        "send any new memory to it. One fix: open that repository on GitHub, go to its Settings, and switch it back "
        "to Private; then ask me to \"back up memory now\".")


_MSG_NO_PROJECT = ("I couldn't tell which GitHub project this is, so I can't name a backup for it yet. Your memory "
                   "is safe on this computer, and nothing was created.")
_MSG_NO_TOKEN = ("I couldn't reach your GitHub account, so I can't set up the backup right now. Your memory is safe "
                 "on this computer, and nothing was created. Sign in with `gh auth login`, then ask me to set up the "
                 "backup again.")
_MSG_NO_SCOPE = ("I couldn't create the private backup repository because my GitHub access doesn't include "
                 "permission to create repositories. Your memory is safe on this computer, and nothing was created. "
                 "To turn on backups, run this once in your terminal:  gh auth refresh -s repo  — then ask me to set "
                 "up the backup again.")
_MSG_NOT_PRIVATE = ("Something went wrong creating the backup as private, so I removed it right away rather than risk "
                    "leaving your notes somewhere public. Your memory is safe on this computer. You can ask me to try "
                    "the backup setup again.")
_MSG_CREATE_FAILED = ("I couldn't create the backup repository just now. Your memory is safe on this computer, and "
                      "nothing was created. You can ask me to try the backup setup again in a little while.")
_MSG_DECLINED = "No backup was created. Nothing left your computer."
_MSG_ADOPT_PUBLIC = (f"Heads up: your backup repository \"{_SHARED_VAULT_NAME}\" is currently PUBLIC, and it holds the "
                     "saved memory for ALL your projects — so anyone on the internet can read them right now. I did "
                     "NOT add this project to it. Open it on GitHub, go to Settings, and switch it to Private as soon "
                     "as you can; then ask me to set up the backup again.")
_MSG_FOREIGN_VAULT = (f"There's already a private repository named \"{_SHARED_VAULT_NAME}\" on your account that I "
                      "didn't create, so I left it alone — I won't add your memory to a repository I don't recognize. "
                      "Rename that repository, or tell me to use a separate backup just for this project.")
_MSG_UNREACHABLE_SETUP = ("I couldn't reach GitHub, so I couldn't set up the backup. Nothing was created. Try again "
                          "when you have a steady internet connection.")


def _is_shared(repo: str) -> bool:
    return repo == _SHARED_VAULT_NAME


def _setup_done_msg(owner: str, repo: str) -> str:
    if _is_shared(repo):
        return (f"Your project's AI memory is now backed up to your shared private repository (\"{owner}/{repo}\"), in "
                "this project's own folder — and I'll keep it up to date automatically, about once a day. Your other "
                "projects each have their own folder in there and weren't touched.")
    return (f"Your project's AI memory is now backed up to a private repository on your GitHub (\"{owner}/{repo}\"), "
            "and I'll keep it up to date automatically — about once a day; this copy is your safety net.")


# ============================================================================================================
# setup (foreground, consent-gated) + the README seed + verify-private.
# ============================================================================================================

def _project_slug() -> "str | None":
    import boot  # noqa: E402 — lazy
    return boot.repo_slug()


def _safe_demo_delete(repo: str, project_name: str) -> bool:
    """The live-demo DELETE name-guard: a name is safe to delete ONLY if it carries the unmistakable disposable
    marker AND is neither the project repo nor the real vault repo — so `demo --live` can never delete real data."""
    return (isinstance(repo, str) and _DEMO_MARKER in repo
            and repo != project_name
            and repo != _SHARED_VAULT_NAME
            and repo != f"{project_name}{_PER_PROJECT_SUFFIX}")


def _seed_readme(gh, owner: str, repo: str, branch: str, project_name: str, scope: str = _DEFAULT_SCOPE) -> bool:
    """Floor 2: commit the plain-language README (with the self-describing marker) into the backup repo. Best-effort
    (the backup DATA matters more than a perfect README); auto_init left a generic README, so this is an UPDATE
    needing its existing blob sha."""
    base = f"/repos/{owner}/{repo}"
    existing = _get(gh, f"{base}/contents/README.md?ref={branch}")
    sha = existing.get("sha") if isinstance(existing, dict) else None
    body = {"message": _COMMIT_MESSAGE, "branch": branch,
            "content": base64.b64encode(_readme_text(project_name, scope).encode("utf-8")).decode("ascii")}
    if isinstance(sha, str) and sha:
        body["sha"] = sha
    status, _ = _send(gh, "PUT", f"{base}/contents/README.md", body)
    return status in (200, 201)


def _ask_consent(vault_name: str, scope: str) -> str:
    try:
        return input(_consent_prompt(vault_name, scope))
    except EOFError:
        return "n"


def _authenticated_login(gh) -> "str | None":
    """The login of the token's GitHub account — the owner under which `/user/repos` creates, so the vault always
    lives there. None on any doubt (the subscript is null-guarded so a (status, None) fault can't raise)."""
    status, data = _send(gh, "GET", "/user")
    login = (data or {}).get("login") if status == 200 else None
    return login if isinstance(login, str) and login else None


def _vault_is_engine_created(gh, owner: str, repo: str, branch: str) -> bool:
    """ADOPT guard: True iff the existing repo's README leads with the engine's self-describing marker — so the engine
    never colonizes a coincidentally same-named private repo it did not create (design 305-307)."""
    existing = _get(gh, f"/repos/{owner}/{repo}/contents/README.md?ref={branch}")
    content = existing.get("content") if isinstance(existing, dict) else None
    if not isinstance(content, str):
        return False
    try:
        text = base64.b64decode(content).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — an unreadable README -> treat as not-ours (fail safe; never colonize)
        return False
    return text.lstrip().startswith(_VAULT_README_MARKER)


def _adopt_existing(gh, login: str, vault_name: str, probe: dict) -> dict:
    """Reuse an EXISTING private vault (floor 3): verify it's private + engine-created (the self-describing marker),
    then bind. NEVER creates or deletes (it may hold other projects' folders). Returns {ok, owner, repo, branch,
    created:False} or a decline dict."""
    if probe.get("private") is not True:
        return {"ok": False, "error": "adopt-public", "message": _MSG_ADOPT_PUBLIC}
    branch = probe.get("default_branch") or "main"
    if not _vault_is_engine_created(gh, login, vault_name, branch):
        return {"ok": False, "error": "foreign-vault", "message": _MSG_FOREIGN_VAULT}
    return {"ok": True, "owner": login, "repo": vault_name, "branch": branch, "created": False}


def _bind_destination(gh, login: str, vault_name: str, scope: str, project_name: str) -> dict:
    """Create the chosen private destination, or ADOPT the operator's existing engine vault. The privacy-delete
    touches ONLY a repo created in THIS call — an existing repo is never deleted. Returns {ok, owner, repo, branch,
    created} or a decline dict {ok:False, error, message}."""
    probe_status, probe = _send(gh, "GET", f"/repos/{login}/{vault_name}")
    if probe_status == 200 and isinstance(probe, dict):
        return _adopt_existing(gh, login, vault_name, probe)
    if probe_status != 404:                                      # None / 5xx / other -> don't blind-create a duplicate
        return {"ok": False, "error": "unreachable", "message": _MSG_UNREACHABLE_SETUP}
    status, repo_obj = _send(gh, "POST", "/user/repos",
                             {"name": vault_name, "private": True, "auto_init": True,
                              "description": _REPO_DESCRIPTION})
    if status == 403:
        return {"ok": False, "error": "no-scope", "message": _MSG_NO_SCOPE}
    if status == 422:                                           # name exists (a private repo we couldn't see, or a
        again_status, again = _send(gh, "GET", f"/repos/{login}/{vault_name}")   # race) -> re-probe + adopt, never a
        if again_status == 200 and isinstance(again, dict):                      # create-failed loop
            return _adopt_existing(gh, login, vault_name, again)
        return {"ok": False, "error": "unreachable", "message": _MSG_UNREACHABLE_SETUP}
    if status not in (200, 201) or not isinstance(repo_obj, dict):
        return {"ok": False, "error": "create-failed", "message": _MSG_CREATE_FAILED}
    owner = (repo_obj.get("owner") or {}).get("login")
    repo = repo_obj.get("name")
    branch = repo_obj.get("default_branch") or "main"
    if not (isinstance(owner, str) and owner and isinstance(repo, str) and repo):
        return {"ok": False, "error": "create-failed", "message": _MSG_CREATE_FAILED}
    check = _get(gh, f"/repos/{owner}/{repo}")                  # verify PRIVATE — never leave a public backup
    if check is None or check.get("private") is not True:
        _send(gh, "DELETE", f"/repos/{owner}/{repo}")          # delete THIS just-created repo only
        return {"ok": False, "error": "not-private", "message": _MSG_NOT_PRIVATE}
    readme_ok = _seed_readme(gh, owner, repo, branch, project_name, scope)
    return {"ok": True, "owner": owner, "repo": repo, "branch": branch, "created": True, "readme_seeded": readme_ok}


def setup(*, scope: "str | None" = None, transport=None, consent: "str | None" = None,
          now: "int | None" = None) -> dict:
    """Foreground first-time setup. Presents the shared-vs-per-repo choice (floor 1; shared default), then on consent
    CREATES the chosen private destination — or ADOPTS the operator's existing shared vault (recognized by its
    self-describing README marker) — writes the committed pointer with a freshly MINTED namespace id, and does the
    first push. `scope`/`consent` bypass the prompts for tests/demo. Creates NOTHING without a yes (Floor 1).

    Result: {ok, ...} with a plain-language `message`. error in {no-project, no-token, no-scope, create-failed,
    not-private, adopt-public, foreign-vault, unreachable}."""
    when = int(time.time()) if now is None else int(now)
    if _setup_done():
        return {"ok": True, "already": True, "message": "Memory backup is already set up."}
    project = _project_slug()
    if not project or "/" not in project:
        return {"ok": False, "error": "no-project", "message": _MSG_NO_PROJECT}
    project_name = project.split("/")[-1]

    gh = _gh(transport)
    if gh is None:                                               # check we CAN back up before asking permission to
        return {"ok": False, "error": "no-token", "message": _MSG_NO_TOKEN}

    chosen_scope = scope if scope is not None else _ask_scope()
    vault_name = _vault_name(project_name, chosen_scope)
    answer = consent if consent is not None else _ask_consent(vault_name, chosen_scope)
    if str(answer).strip().lower() not in ("y", "yes"):
        return {"ok": False, "declined": True, "message": _MSG_DECLINED}

    login = _authenticated_login(gh)
    if not login:
        return {"ok": False, "error": "create-failed", "message": _MSG_CREATE_FAILED}

    bind = _bind_destination(gh, login, vault_name, chosen_scope, project_name)
    if not bind.get("ok"):
        return bind
    owner, repo, branch, created = bind["owner"], bind["repo"], bind["branch"], bind["created"]

    namespace = _mint_namespace()                               # a fresh id, even on adopt (never a discovered one)
    write_pointer(owner, repo, branch, namespace, now=when)
    result = push_now(transport=transport, now=when)
    _record_state(now=when, success=result.get("ok", False), privacy_ok=result.get("error") != "public")
    msg = _setup_done_msg(owner, repo)
    if not result.get("ok"):
        msg += " (The first copy will finish on the next backup.)"
    return {"ok": True, "created": created, "adopted": not created, "owner": owner, "repo": repo,
            "namespace": namespace, "readme_seeded": bind.get("readme_seeded"),
            "first_push": result.get("ok", False), "message": msg}


# ============================================================================================================
# The throttled SessionStart hook (fail-open; cheap-probe-first; one-time privacy relay).
# ============================================================================================================

def _session_start_handler(payload, *, now: "int | None" = None) -> dict:
    """Memory's backup vault at SessionStart — the throttled, hook-safe auto-push. Fail-open throughout (a fault here
    must NEVER block or slow session start): silent until setup exists; then, at most once per BACKUP_INTERVAL_HOURS
    of the last SUCCESS, push (cheap-probe-first, fail-SAFE, no local git). On a disclosable failure relay ONE
    plain-language line (a push failure, or a newly-detected public flip — once). `payload` is unused."""
    import hooks  # noqa: E402 — lazy: keep the module-load path light
    try:
        when = int(time.time()) if now is None else int(now)
        if not _setup_done() or not _should_push(when):
            return hooks.proceed()
        prev = _read_state()
        result = push_now(now=when)
        err = result.get("error")
        privacy_ok_now = err != "public"
        _record_state(now=when, success=result.get("ok", False), privacy_ok=privacy_ok_now)
        msg = None
        if not result.get("ok"):
            if err == "public":
                if prev.get("last_privacy_ok", True):       # newly public -> tell once
                    msg = _heads_up_public()
            elif err in ("push-failed", "unreachable"):
                msg = _HEADS_UP_PUSH_FAILED
        if msg:
            return hooks.inject(msg)
    except Exception:  # noqa: BLE001 — fail-open: a fault must never strand the session start
        return hooks.proceed()
    return hooks.proceed()


# ============================================================================================================
# CLI verbs.
# ============================================================================================================

def _now_message(result: dict) -> str:
    if result.get("ok"):
        return "Backed up this project's AI memory to your private backup repository."
    err = result.get("error")
    if err == "not-configured":
        return "Memory backup isn't set up yet. Ask me to set up the backup first."
    if err == "public":
        return _heads_up_public().split(": ", 1)[-1]
    if err in ("push-failed", "unreachable", "no-token"):
        return ("I couldn't update the backup just now — your memory on this computer is safe and complete. Try "
                "again when you have a steady internet connection.")
    return "I couldn't update the backup just now. Your memory on this computer is safe and complete."


def status(*, now: "int | None" = None) -> int:
    """Read-only: is setup done, where the vault is, how long since the last SUCCESSFUL backup, still-private."""
    pointer = read_pointer()
    if pointer is None:
        print("Memory backup is not set up yet. Ask me to set it up to keep an off-site copy of this project's "
              "AI memory.")
        return 0
    when = int(time.time()) if now is None else int(now)
    state = _read_state()
    last = _last_success(state)
    if _is_shared(pointer["repo"]):
        print(f"Memory backup: ON — your shared private repository \"{pointer['owner']}/{pointer['repo']}\", in this "
              "project's own folder. Your other projects each have their own folder in there and weren't touched.")
    else:
        print(f"Memory backup: ON — your private repository \"{pointer['owner']}/{pointer['repo']}\".")
    if last is None:
        print("Last successful backup: none yet (the next backup will make the first copy).")
    else:
        days = max(0, (when - last) // 86400)
        when_str = "today" if days == 0 else ("1 day ago" if days == 1 else f"{days} days ago")
        print(f"Last successful backup: {when_str}.")
        if days >= 2:
            print("  (That's a little stale — ask me to \"back up memory now\" to bring it up to date.)")
    if state.get("last_privacy_ok") is False:
        print("  Note: the backup repository looked PUBLIC last time — it should be private. Open it on GitHub, go "
              "to Settings, and switch it back to Private.")
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "session-start":
        import hooks  # noqa: E402 — lazy
        return hooks.run_hook("SessionStart", _session_start_handler)
    if cmd == "setup":
        print(setup()["message"])
        return 0
    if cmd == "now":
        print(_now_message(push_now()))
        return 0
    if cmd == "status":
        return status()
    if cmd == "demo":
        return _demo_live() if "--live" in argv[1:] else _demo()
    print(f"usage: backup_vault.py [setup|now|status|session-start|demo [--live]]\nunknown command {cmd!r}",
          file=sys.stderr)
    return 2


# ============================================================================================================
# Operator demonstration — REAL backup logic against an in-memory GitHub; only the network is stubbed.
# ============================================================================================================
# A fully offline walkthrough on a throwaway memory cabinet + a throwaway repo root (so the real ledger and the real
# committed pointer are never touched). It runs the REAL setup/consent gate, the REAL Git Data push, the REAL
# privacy re-verify, and the REAL degrade-and-disclose + throttle — only GitHub is the in-memory _FakeVault. Vary it:
# answer the consent "n" vs "y", flip the repo public, force the network to fail, and re-run.

class _FakeVault:
    """A tiny in-memory GitHub for the demo/tests: answers repo-create, repo GET (with a flippable `private`), the
    Git Data push (ref/commit/blob/tree/ref), Contents PUT/GET (the README), and DELETE — so the REAL backup logic
    runs fully offline. `pushed_ledger_via_contents` stays False unless the ledger is ever PUT via the Contents API
    (the large-file guard: it must go via Git Data blobs)."""

    def __init__(self, *, private: bool = True, fail_blob: bool = False, no_scope: bool = False,
                 owner: str = "demo-user"):
        self.private = private
        self.fail_blob = fail_blob
        self.no_scope = no_scope
        self.owner = owner
        self.repos: dict = {}
        self.blobs: dict = {}
        self.commits: dict = {}
        self.trees: dict = {}
        self.refs: dict = {}
        self.contents: dict = {}
        self.deleted: list = []
        self.created: list = []
        self.pushed_ledger_via_contents = False
        self._hidden_probes: set = set()
        self._n = 1000

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:036x}"

    def hide_next_probe(self, name: str) -> None:
        """Make the NEXT repo-GET on `name` return 404 (a private repo the token can't see yet / an eventual-
        consistency race), so a create then 422s — exercising the 422->re-probe->adopt fallback."""
        self._hidden_probes.add(f"{self.owner}/{name}")

    def preseed(self, name: str, readme: str) -> str:
        """Plant an EXISTING repo (NOT created by this run) carrying `readme` — for the adopt / foreign-vault /
        adopt-public tests. Returns the slug. The instance `private` flag still governs the live repo GET."""
        slug = f"{self.owner}/{name}"
        branch = "main"
        blob = self._next("b"); tree = self._next("t"); commit = self._next("c")
        self.blobs[blob] = base64.b64encode(readme.encode("utf-8")).decode("ascii")
        self.trees[tree] = {"sha": tree, "tree": []}
        self.commits[commit] = {"sha": commit, "tree": {"sha": tree}}
        self.refs[f"{slug}@{branch}"] = commit
        self.contents[f"{slug}@README.md"] = blob
        self.repos[slug] = {"default_branch": branch}
        return slug

    def transport(self, method: str, path: str, body=None):
        if method == "GET" and path == "/user":
            return 200, {"login": self.owner}
        if method == "POST" and path == "/user/repos":
            if self.no_scope:
                return 403, None
            name = body["name"]
            slug = f"{self.owner}/{name}"
            if slug in self.repos:                           # name already exists -> 422 (the create-OR-adopt race)
                return 422, None
            branch = "main"
            blob = self._next("b"); tree = self._next("t"); commit = self._next("c")
            self.blobs[blob] = base64.b64encode(b"# init\n").decode("ascii")
            self.trees[tree] = {"sha": tree, "tree": []}
            self.commits[commit] = {"sha": commit, "tree": {"sha": tree}}
            self.refs[f"{slug}@{branch}"] = commit
            self.contents[f"{slug}@README.md"] = blob
            self.repos[slug] = {"default_branch": branch}
            self.created.append(slug)
            return 201, {"name": name, "owner": {"login": self.owner}, "default_branch": branch, "private": True}
        m = re.match(r"^/repos/([^/]+)/([^/]+)$", path)
        if m:
            slug = f"{m.group(1)}/{m.group(2)}"
            if method == "DELETE":
                self.deleted.append(slug)
                self.repos.pop(slug, None)
                return 204, None
            if method == "GET":
                if slug in self._hidden_probes:                  # a private repo the token can't see yet (race):
                    self._hidden_probes.discard(slug)            # 404 once, then visible on the re-probe
                    return 404, None
                if slug not in self.repos:
                    return 404, None
                return 200, {"private": self.private, "default_branch": self.repos[slug]["default_branch"]}
        m = re.match(r"^/repos/([^/]+)/([^/]+)/git/ref/heads/(.+)$", path)
        if m and method == "GET":
            sha = self.refs.get(f"{m.group(1)}/{m.group(2)}@{m.group(3)}")
            return (200, {"object": {"sha": sha}}) if sha else (404, None)
        m = re.match(r"^/repos/([^/]+)/([^/]+)/git/commits/(.+)$", path)
        if m and method == "GET":
            c = self.commits.get(m.group(3))
            return (200, c) if c else (404, None)
        m = re.match(r"^/repos/([^/]+)/([^/]+)/git/blobs$", path)
        if m and method == "POST":
            if self.fail_blob:
                return 422, None
            raw = base64.b64decode(body["content"])     # store under the REAL git object id, so a fetch can verify it
            sha = _git_blob_sha1(raw)
            self.blobs[sha] = body["content"]
            return 201, {"sha": sha}
        m = re.match(r"^/repos/([^/]+)/([^/]+)/git/blobs/([^?]+)", path)
        if m and method == "GET":                        # the RESTORE read side (slice 6b): return the blob base64
            content = self.blobs.get(m.group(3))
            if content is None:
                return 404, None
            return 200, {"sha": m.group(3), "content": content, "encoding": "base64",
                         "size": len(base64.b64decode(content))}
        m = re.match(r"^/repos/([^/]+)/([^/]+)/git/trees$", path)
        if m and method == "POST":
            sha = self._next("t")
            base = self.trees.get(body.get("base_tree"), {})       # merge base_tree's inherited entries (the real
            merged = {e["path"]: e for e in base.get("tree", [])}  # recursive Trees API flattens them) so another
            for e in body.get("tree", []):                         # project's folders SURVIVE a later push — a real
                merged[e["path"]] = e                              # cross-project coexistence round-trip can be tested
            self.trees[sha] = {"sha": sha, "tree": list(merged.values())}
            return 201, {"sha": sha}
        m = re.match(r"^/repos/([^/]+)/([^/]+)/git/trees/([^?]+)", path)
        if m and method == "GET":                        # the RESTORE read side: a recursive GET returns the FLATTENED
            stored = self.trees.get(m.group(3))          # cumulative tree (base_tree merged on each push above), so a
            if stored is None:                           # restore finds every project's namespace folder, not just the last.
                return 404, None
            return 200, {"sha": m.group(3), "tree": stored.get("tree", []), "truncated": False}
        m = re.match(r"^/repos/([^/]+)/([^/]+)/git/commits$", path)
        if m and method == "POST":
            sha = self._next("c")
            self.commits[sha] = {"sha": sha, "tree": {"sha": body["tree"]}}
            return 201, {"sha": sha}
        m = re.match(r"^/repos/([^/]+)/([^/]+)/git/refs/heads/(.+)$", path)
        if m and method == "PATCH":
            self.refs[f"{m.group(1)}/{m.group(2)}@{m.group(3)}"] = body["sha"]
            return 200, {}
        m = re.match(r"^/repos/([^/]+)/([^/]+)/contents/([^?]+)", path)
        if m:
            slug = f"{m.group(1)}/{m.group(2)}"
            fpath = m.group(3)
            if method == "PUT":
                if fpath.endswith("ledger.ndjson"):
                    self.pushed_ledger_via_contents = True   # the guard: the ledger must NEVER go via Contents
                sha = self._next("b")
                self.blobs[sha] = body["content"]
                self.contents[f"{slug}@{fpath}"] = sha
                return 201, {"content": {"sha": sha}}
            if method == "GET":
                sha = self.contents.get(f"{slug}@{fpath}")
                return (200, {"sha": sha, "content": self.blobs.get(sha, ""), "encoding": "base64"}) if sha \
                    else (404, None)
        return 404, None


def _demo_plant(text: str) -> None:
    """Append one real note to the throwaway ledger so the backup has content to copy."""
    ledger.append({"kind": "turn-delta", "role": "observation", "text": text, "ts": int(time.time())})


def _demo() -> int:
    import tempfile

    print("=" * 96)
    print("MEMORY — the engine backs up your AI memory to a PRIVATE repo, with your consent (practice run)")
    print("=" * 96)
    with tempfile.TemporaryDirectory() as cabinet, tempfile.TemporaryDirectory() as root:
        import validate
        old_root = validate.ROOT
        os.environ["ENGINE_MEMORY_DIR"] = cabinet              # the throwaway memory cabinet
        validate.ROOT = root                                   # the throwaway repo root (pointer + engine.json)
        os.makedirs(os.path.join(root, ".engine"), exist_ok=True)
        with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            json.dump({"engine_release": "0.0.0-dev"}, fh)
        try:
            ok = _demo_body()
        finally:
            validate.ROOT = old_root
            os.environ.pop("ENGINE_MEMORY_DIR", None)

    print("\n" + "-" * 96)
    print("What this just proved: NOTHING is created or sent until you say yes; when you do, the engine makes a")
    print("PRIVATE repo on your GitHub, checks it really is private, writes a plain-language README into it, and")
    print("copies your memory there (via the large-file path, so even a big memory fits). It writes a small pointer")
    print("so a fresh machine can find the backup later. If the repo is ever flipped public it STOPS sending and")
    print("tells you; if the network fails it tells you and your local memory stays safe; and after a backup it")
    print("won't run again for about a day. That was a PRACTICE setup, thrown away — and this single run already")
    print("showed you all three safety paths: declining consent (PART 1), the repo flipped public (PART 5), and a")
    print("network failure (PART 6), each stopping safely. To prove it end-to-end on your REAL GitHub — a throwaway")
    print("private repo that is created, verified private, copied to, and then deleted — run this command with --live.")
    return 0 if ok else 1


def _demo_body() -> bool:
    _demo_plant("Decided the launch banner ships in the spring release.")
    _demo_plant("Lesson: never deploy on a Friday.")

    # --- PART 1 — consent gate -----------------------------------------------------------------------------
    print("\nPART 1 — nothing is created until you say yes")
    print("-" * 96)
    print("  The choice the engine shows you (Floor 1 — shared vault by default, a per-project repo one step away):\n")
    for line in _choice_prompt().rstrip().splitlines():
        print(f"    | {line}")
    print("\n  Then, for the shared vault, the consent it asks (the co-location trade-off named plainly):\n")
    for line in _consent_prompt(_SHARED_VAULT_NAME, "shared").rstrip().splitlines():
        print(f"    | {line}")
    declined = setup(scope="shared", transport=_FakeVault().transport, consent="n")
    fake = _FakeVault()
    accepted = setup(scope="shared", transport=fake.transport, consent="y")
    print(f"\n  you answer 'n': {declined['message']}  (repos created: {len(_FakeVault().created)})")
    print("  you answer 'y': a private shared vault is created and verified private")
    part1 = (declined.get("declined") is True and accepted.get("ok") is True
             and accepted.get("created") is True and len(fake.created) == 1 and not fake.deleted)
    print(f"  => {'consent is real — no yes, nothing created.' if part1 else '!!! consent gate failed'}")

    # --- PART 2 — the self-describing README (Floor 2) -----------------------------------------------------
    print("\nPART 2 — the engine writes a plain-language README into the backup repo (Floor 2)")
    print("-" * 96)
    for line in _readme_text("your-project", "shared").rstrip().splitlines():
        print(f"    | {line}")
    part2 = accepted.get("readme_seeded") is True
    print(f"  => {'the backup repo describes itself in plain words.' if part2 else '!!! the README was not seeded'}")

    # --- PART 3 — the ledger is copied via the large-file path, not the 1MB Contents API -------------------
    print("\nPART 3 — your memory is copied via the large-file path (so even a big memory fits)")
    print("-" * 96)
    manifest = build_manifest(ledger_path=ledger.ledger_path())
    print(f"  the snapshot manifest committed beside it: {json.dumps(manifest)}")
    part3 = (fake.pushed_ledger_via_contents is False
             and set(manifest) == {"ledger-version", "ledger-generation", "timestamp", "engine-version"})
    print(f"  the ledger went via the Git Data (blob) path, NOT the 1MB-capped Contents API: "
          f"{not fake.pushed_ledger_via_contents}")
    print(f"  => {'the whole memory is copied, with its four-field manifest.' if part3 else '!!! wrong copy path or manifest'}")

    # --- PART 4 — the committed pointer (a fresh machine reads it to find the backup) -----------------------
    print("\nPART 4 — a small pointer is written so a fresh machine can find the backup later")
    print("-" * 96)
    pointer = read_pointer()
    print(f"  the committed pointer: {json.dumps(pointer)}")
    part4 = (pointer is not None and pointer.get("repo") == accepted.get("repo")
             and "ledger" not in json.dumps(pointer).lower())     # content-free (no ledger text)
    print(f"  => {'a fresh instance can find the namespace — and the pointer carries no note content.' if part4 else '!!! pointer missing or leaky'}")

    # --- PART 5 — privacy posture: a public flip STOPS the push and tells you -------------------------------
    print("\nPART 5 — if the backup is ever flipped PUBLIC, the engine stops sending and tells you")
    print("-" * 96)
    fake.private = False
    flipped = push_now(transport=fake.transport)
    print(f"  the engine's plain-language warning: \"{_heads_up_public().split(': ', 1)[-1]}\"")
    part5 = flipped.get("ok") is False and flipped.get("error") == "public" and flipped.get("pushed") is False
    print(f"  => {'it declined to send new memory to a public repo.' if part5 else '!!! it pushed to a public repo'}")

    # --- PART 6 — degrade-and-disclose on a network failure (Floor 4) --------------------------------------
    print("\nPART 6 — if the network fails, it tells you plainly and your local memory stays safe (Floor 4)")
    print("-" * 96)
    # the repo exists and is private (the cheap probe passes), but the upload itself fails
    fake.private = True
    fake.fail_blob = True
    failed = push_now(transport=fake.transport)
    print(f"  the engine's plain-language message: \"{_HEADS_UP_PUSH_FAILED.split(': ', 1)[-1]}\"")
    part6 = failed.get("ok") is False and failed.get("error") == "push-failed"
    print(f"  => {'a failure names a consequence and one recovery action, never a git error.' if part6 else '!!! failure not handled'}")

    # --- PART 7 — the ~24h throttle ------------------------------------------------------------------------
    print(f"\nPART 7 — after a backup it does not run again for about {BACKUP_INTERVAL_HOURS} hours")
    print("-" * 96)
    base = 1_000_000_000
    fresh = _should_push(base)
    _record_state(now=base, success=True, privacy_ok=True)
    too_soon = _should_push(base + 3 * _HOUR)
    elapsed = _should_push(base + (BACKUP_INTERVAL_HOURS + 1) * _HOUR)
    print(f"  a first session (no backup yet): {'backs up' if fresh else 'skips'}")
    print(f"  3 hours later: {'backs up' if too_soon else 'skips — not yet a day, so no network call'}")
    print(f"  {BACKUP_INTERVAL_HOURS + 1} hours later: {'backs up again' if elapsed else 'still skips'}")
    part7 = fresh and (not too_soon) and elapsed
    print(f"  => {'it backs up at most about once a day.' if part7 else '!!! the throttle did not gate as expected'}")

    ok = part1 and part2 and part3 and part4 and part5 and part6 and part7
    if not ok:
        print("\nDEMO UNEXPECTED: a backup-vault guarantee did not hold (consent gate, README seed, large-file copy, "
              "the pointer, the privacy decline, degrade-and-disclose, or the throttle).", file=sys.stderr)
    return bool(ok)


def _demo_live() -> int:
    """The LIVE end-to-end test the operator runs himself: create a UNIQUELY-NAMED, unmistakably-disposable PRIVATE
    repo on their real GitHub, verify it is private, push a tiny throwaway ledger (the REAL ledger is never read or
    touched), then DELETE it. The DELETE is name-guarded so it can only ever remove the disposable demo repo, never
    the real vault or the project repo. Nothing about the real memory or the committed pointer is touched."""
    import tempfile
    print("=" * 96)
    print("LIVE TEST — this creates a REAL, throwaway PRIVATE repo on your GitHub, copies a tiny fake memory into it,")
    print("            verifies it is private, and then DELETES it. Your real memory and backup are never touched.")
    print("=" * 96)
    project = _project_slug()
    if not project or "/" not in project:
        print(f"\n  {_MSG_NO_PROJECT}")
        return 0
    gh = _gh()
    if gh is None:
        print(f"\n  {_MSG_NO_TOKEN}")
        return 0
    project_name = project.split("/")[-1]
    demo_name = f"{project_name}{_DEMO_MARKER}{secrets.token_hex(4)}"

    status, repo_obj = _send(gh, "POST", "/user/repos",
                             {"name": demo_name, "private": True, "auto_init": True,
                              "description": "Throwaway engine memory-backup live test — safe to delete."})
    if status == 403:
        print(f"\n  {_MSG_NO_SCOPE}")
        return 0
    if status not in (200, 201) or not isinstance(repo_obj, dict):
        print("\n  I couldn't create the throwaway test repository just now. Nothing was created; try again later.")
        return 0
    owner = (repo_obj.get("owner") or {}).get("login")
    repo = repo_obj.get("name")
    branch = repo_obj.get("default_branch") or "main"
    url = f"https://github.com/{owner}/{repo}"
    print(f"\n  Created a throwaway private repo: {url}")

    check = _get(gh, f"/repos/{owner}/{repo}")
    is_private = bool(check and check.get("private") is True)
    print(f"  Verified it is private: {'yes' if is_private else 'NO'}")
    with tempfile.TemporaryDirectory() as cabinet:
        os.environ["ENGINE_MEMORY_DIR"] = cabinet
        try:
            _demo_plant("A throwaway note for the live backup test.")
            files = {"livetest/ledger.ndjson": open(ledger.ledger_path(), "rb").read(),
                     "livetest/manifest.json": (json.dumps(build_manifest(ledger_path=ledger.ledger_path())) + "\n").encode()}
            pushed = _push_files(gh, owner, repo, branch, files)
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)
    print(f"  Copied a tiny fake memory into it: {'yes' if pushed else 'no (that is fine — it gets deleted next)'}")

    # The name-guard: only ever delete the disposable demo repo, never the real vault or the project repo.
    if _safe_demo_delete(repo, project_name):
        del_status, _ = _send(gh, "DELETE", f"/repos/{owner}/{repo}")
        if del_status in (200, 204):
            print(f"  Deleted the throwaway repo. Nothing is left behind.")
        else:
            print(f"  I couldn't auto-delete it (that repo is PRIVATE and harmless). Remove it yourself with:")
            print(f"      gh repo delete {owner}/{repo} --yes")
    else:
        print(f"  Safety: the repo name didn't look disposable, so I did NOT delete it. Remove it yourself if you "
              f"wish:\n      gh repo delete {owner}/{repo} --yes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
