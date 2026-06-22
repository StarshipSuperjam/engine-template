"""restore_vault.py — memory's backup vault, the RESTORE path (memory-substrate, slice 6b).

The EXPORT half (`backup_vault.py`) copies the gitignored ledger + a 4-key snapshot manifest to a PRIVATE GitHub
repo — by default the shared `engine-memory-vault`, this project in its own minted-id folder (D-237); restore binds
on that same folder id. That made memory durable off-machine — but nothing brought it BACK. This module is
the RESTORE half, which fully closes Risk R2 (memory loss / portability for a non-engineer). Locked design:
engine-planning memory README §"Backup and portability" — "restore = replace the ledger and rebuild the derived
index (routed through `migrations` if the record shape changed)", guarded by the ledger-generation stamp so an older
backup landing over newer state is SURFACED, never silently resurrected. Two operator floors live here:
  Floor 3 — the auto-restore-offer: a fresh instance whose local memory is empty but whose committed pointer is
    configured surfaces a plain-language offer at session start (boot relays `detect_restore_offer`, the strand /
    pr_conflict "boot offers, the assistant executes on consent" model). New-laptop recovery never depends on CLI or
    path knowledge — bounded honestly: it needs the project repo present (the committed pointer is what it reads).
  Floor 4 — degrade-and-disclose: every failure names a consequence + ONE recovery action, never a git/HTTP error.

Posture (the backup_vault precedent): pure GitHub API over the same 10s-bounded transport; the restore ACT is a
FOREGROUND, consent-gated command (it OVERWRITES local memory, so it must never run unattended). The detector is
LOCAL-ONLY (no network) so it adds nothing to session-start cost. The swap is crash-safe and serialized behind the
single-writer lock; the canonical ledger is untouched until the atomic rename.

CLI: restore | status | demo [--live]. Run the demo (fully offline):
    uv run --directory .engine --frozen -- python tools/memory/restore_vault.py demo
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import backup_vault as bv  # noqa: E402 — the shared vault surface (pointer/transport/manifest/helpers)
from memory import ledger              # noqa: E402 — the canonical store + its restore primitive + generation stamp

_UNSET = object()

# A restore temp written in the SAME dir as the ledger (replace_ledger requires a sibling for an atomic rename).
_RESTORE_TMP = "ledger.ndjson.restore-tmp"


# ============================================================================================================
# Fetch — the GET/read side the export tool lacks (Git Data: ref -> commit -> tree -> blob).
# ============================================================================================================

def _fetch_blob(gh, owner: str, repo: str, sha) -> "bytes | None":
    """GET one blob and decode it, VERIFYING the bytes against the tree's object id (git's content-addressing) and
    the API's `size` — so a truncated/corrupt download is rejected before it can ever overwrite memory. None on any
    doubt."""
    if not (isinstance(sha, str) and sha):
        return None
    obj = bv._get(gh, f"/repos/{owner}/{repo}/git/blobs/{sha}")
    if not isinstance(obj, dict) or obj.get("encoding") != "base64" or not isinstance(obj.get("content"), str):
        return None
    try:
        raw = base64.b64decode(obj["content"])          # b64decode tolerates the API's 60-char line wraps
    except Exception:  # noqa: BLE001 — undecodable -> reject (never a partial write)
        return None
    if bv._git_blob_sha1(raw) != sha:                    # Merkle integrity: the bytes ARE what the tree points at
        return None
    size = obj.get("size")
    if isinstance(size, int) and not isinstance(size, bool) and size != len(raw):
        return None
    return raw


def fetch_snapshot(*, transport=None) -> dict:
    """Fetch the backed-up ledger bytes + manifest from the configured vault. Pure GitHub API over the bounded
    transport; cheap-probe-first (a repo GET bounds a dead host). Never raises. Returns {ok, error, ledger_bytes,
    manifest, ...}; error in {not-configured, no-token, unreachable, no-backup-data, namespace-missing, corrupt}."""
    pointer = bv.read_pointer()
    if pointer is None:
        return {"ok": False, "error": "not-configured"}
    gh = bv._gh(transport)
    if gh is None:
        return {"ok": False, "error": "no-token"}
    owner, repo, branch, namespace = pointer["owner"], pointer["repo"], pointer["branch"], pointer["namespace"]
    try:
        if bv._get(gh, f"/repos/{owner}/{repo}") is None:               # cheap probe: reachability
            return {"ok": False, "error": "unreachable"}
        ref = bv._get(gh, f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
        base_sha = (ref or {}).get("object", {}).get("sha")
        if not (isinstance(base_sha, str) and base_sha):
            return {"ok": False, "error": "no-backup-data"}
        commit = bv._get(gh, f"/repos/{owner}/{repo}/git/commits/{base_sha}")
        tree_sha = (commit or {}).get("tree", {}).get("sha")
        if not (isinstance(tree_sha, str) and tree_sha):
            return {"ok": False, "error": "corrupt"}
        tree = bv._get(gh, f"/repos/{owner}/{repo}/git/trees/{tree_sha}?recursive=1")
        if not isinstance(tree, dict) or tree.get("truncated") is True:  # a truncated tree could hide the ledger entry
            return {"ok": False, "error": "corrupt"}
        entries = {e.get("path"): e for e in tree.get("tree", []) if isinstance(e, dict)}
        led_entry = entries.get(f"{namespace}/ledger.ndjson")
        man_entry = entries.get(f"{namespace}/manifest.json")
        if not isinstance(led_entry, dict) or not isinstance(man_entry, dict):
            # A now-MISSING namespace (the vault is populated — it holds OTHER projects' folders — but mine is gone,
            # i.e. my folder was removed by hand) is a DISTINCT finding the operator must see (floor 2), never the
            # silent "no backup yet" no-restore. A truly fresh vault (no other folders) stays `no-backup-data`.
            mine = f"{namespace}/"
            others = any(e.get("type") == "blob" and "/" in (e.get("path") or "")
                         and not (e.get("path") or "").startswith(mine)
                         for e in entries.values() if isinstance(e, dict))
            return {"ok": False, "error": "namespace-missing" if others else "no-backup-data"}
        ledger_bytes = _fetch_blob(gh, owner, repo, led_entry.get("sha"))
        manifest_raw = _fetch_blob(gh, owner, repo, man_entry.get("sha"))
        if ledger_bytes is None or manifest_raw is None:
            return {"ok": False, "error": "corrupt"}
        try:
            manifest = json.loads(manifest_raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "corrupt"}
        if not isinstance(manifest, dict):
            return {"ok": False, "error": "corrupt"}
        return {"ok": True, "error": None, "ledger_bytes": ledger_bytes, "manifest": manifest,
                "owner": owner, "repo": repo, "namespace": namespace}
    except Exception:  # noqa: BLE001 — any transport fault degrades to a clean failure, never a raise
        return {"ok": False, "error": "unreachable"}


# ============================================================================================================
# Local state reads (for the resurrection guard + the consent count + the offer).
# ============================================================================================================

def _local_structurally_empty() -> bool:
    """True iff the local ledger file is missing or zero bytes — the fresh-machine case. Deliberately NOT '0
    parseable records': a corrupt non-empty ledger must never read as empty (else the offer would invite overwriting
    it)."""
    try:
        return os.path.getsize(ledger.ledger_path()) == 0
    except OSError:
        return True


def _local_record_count() -> int:
    try:
        return len(ledger.read().records)
    except Exception:  # noqa: BLE001
        return 0


def _generation_known() -> bool:
    """Whether the local generation is KNOWN — the meta sidecar exists and holds a valid int. A non-empty local
    ledger whose sidecar is missing/unreadable reads as generation 0, which could MASK a real higher generation
    (and so a real resurrection); the restore guard treats that 'unknown' as a possible resurrection."""
    try:
        with open(ledger.meta_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return False
    val = data.get("generation") if isinstance(data, dict) else None
    return isinstance(val, int) and not isinstance(val, bool) and val >= 0


def _count_lines(raw: bytes) -> int:
    return raw.count(b"\n") if raw else 0


# ============================================================================================================
# Resurrection-surfacing — a restore that would land an OLDER generation is surfaced, never silently applied.
# (Mirrors module_manager.surface_stamp_mismatch's body for generations; routes through boot's open-findings.)
# ============================================================================================================

def surface_resurrection(local_gen: int, backup_gen, *, now: "int | None" = None, github=_UNSET) -> "int | None":
    """Surface a declined resurrecting restore as ONE tracked engine finding via telemetry.promote_finding (which
    boot renders through its read-only open-findings path — no boot change). Content-free: names only the condition
    + the one recovery action, never ledger content / repo / git error. Returns the Issue number, or None offline
    (the in-session disclose + the decline still stand). `github` is injectable for tests/demo (None => offline)."""
    f = _resurrection_finding()
    import close       # noqa: E402 — lazy: this rare path keeps the common import graph lean
    import telemetry   # noqa: E402
    gh = (close._github() if github is _UNSET else github)
    if gh is None:                                       # offline -> surfaced-in-session-not-tracked; consent is the wall
        return None
    now_iso = bv._iso_utc(int(time.time()) if now is None else now)
    digest = hashlib.sha1(b"memory/restore-resurrection").hexdigest()[:12]
    record = {"source_id": f"memory/restore-resurrection/{digest}", "severity": telemetry.TRUST_CRITICAL,
              "message": f.get("message"), "location": f.get("location"),
              "first_seen": now_iso, "last_seen": now_iso}
    try:
        return telemetry.promote_finding(gh, record, now_iso)
    except Exception:  # noqa: BLE001 — surfacing must never raise into the restore path
        return None


def _resurrection_finding() -> dict:
    import validate  # noqa: E402 — lazy: the finding-record shape
    return validate.finding(
        "hard",
        "The engine declined a memory restore: the backup is older than the memory on this computer, so restoring "
        "it would bring back notes that were deliberately removed since the backup was taken. It was surfaced here "
        "rather than applied. If you intend to recover the older copy, run the restore again and confirm explicitly.",
        location="memory backup/restore")


# ============================================================================================================
# Floor-4 wording — plain language, non-engineer; consequence + ONE recovery action, never a git/HTTP error.
# ============================================================================================================

_MSG_NOT_CONFIGURED = ("No backup is set up yet for this project, so there's nothing to restore. Ask me to set one "
                       "up to keep an off-site copy of this project's AI memory.")
_MSG_UNREACHABLE = ("I couldn't reach your backup just now, so I didn't restore anything. Your memory on this "
                    "computer is unchanged. Check your internet connection and ask me to try the restore again.")
_MSG_NO_BACKUP_DATA = ("The backup is set up, but I couldn't find a saved memory in it to restore yet (it may not "
                       "have finished its first backup). Your memory on this computer is unchanged.")
_MSG_NAMESPACE_MISSING = ("Your project's saved-memory folder is no longer in the backup — it looks like it was "
                          "removed from the backup by hand. Nothing on this computer changed. If your memory is still "
                          "here, ask me to set up the backup again and I'll rebuild it from this computer. If this "
                          "computer is empty too and the memory isn't saved on another machine, that backed-up copy "
                          "is gone for good.")
_MSG_CORRUPT = ("I couldn't read a complete copy of your memory from the backup, so I did NOT change anything on "
                "this computer — better to keep what you have than risk a half copy. Try the restore again in a "
                "little while.")
_MSG_VERSION_MISMATCH = ("This backup was made by a different version of the engine, and bringing it back safely "
                         "needs an update step that isn't built yet. I left your memory on this computer unchanged.")
_MSG_BAD_MANIFEST = ("I couldn't make sense of this backup's details, so I left your memory on this computer "
                     "unchanged rather than risk restoring something wrong.")
_MSG_RESURRECTION = ("Your memory on this computer is MORE RECENT than this backup. Restoring it would undo edits "
                     "and removals you've made since the backup was taken — so I did NOT restore it. If you truly "
                     "want the older copy, tell me explicitly and I'll restore it.")
_MSG_BUSY = ("Memory is busy right now, so I didn't restore anything — nothing was changed. Ask me to try the "
             "restore again in a moment.")
_MSG_APPLY_FAILED = ("Something went wrong part-way through restoring, so I stopped. Your memory on this computer is "
                     "unchanged. Ask me to try the restore again.")
_MSG_DECLINED = "No restore was done. Your memory on this computer is unchanged."


def _floor4_fetch(error: "str | None") -> str:
    return {"not-configured": _MSG_NOT_CONFIGURED, "no-token": _MSG_UNREACHABLE, "unreachable": _MSG_UNREACHABLE,
            "no-backup-data": _MSG_NO_BACKUP_DATA, "namespace-missing": _MSG_NAMESPACE_MISSING,
            "corrupt": _MSG_CORRUPT}.get(error or "", _MSG_UNREACHABLE)


def _restore_consent_prompt(local_count: int, backup_count: int) -> str:
    """Floor 1 ethos — the single most dangerous string: restore OVERWRITES local memory. Name the loss flatly."""
    return (f"This will replace the notes saved on this computer ({local_count}) with the backup copy "
            f"({backup_count}). The current notes will be gone. Continue? [y/N]: ")


def _restored_msg(count: int) -> str:
    return (f"Restored your project's AI memory from the backup — {count} note(s) are back on this computer, ready "
            "to use.")


# ============================================================================================================
# The restore act (foreground, consent-gated, crash-safe + serialized).
# ============================================================================================================

def _ask_restore_consent(local_count: int, backup_count: int) -> str:
    try:
        return input(_restore_consent_prompt(local_count, backup_count))
    except EOFError:
        return "n"


def restore_now(*, transport=None, consent: "str | None" = None, override: bool = False,
                now: "int | None" = None, github=_UNSET) -> dict:
    """Restore the local ledger + index from the configured backup. Fetch -> format guard -> resurrection guard ->
    consent -> apply (under the writer lock). OVERWRITES local memory, so it is foreground + consent-gated. Fail-SAFE:
    the canonical ledger is untouched until the atomic rename, and every failure is a plain Floor-4 message, never a
    raise. `consent` ('y'/'n') bypasses the prompt for tests/demo; `override` proceeds past the resurrection guard;
    `github` is forwarded to surfacing (None => offline). Result: {ok, error, restored, message}."""
    when = int(time.time()) if now is None else int(now)
    fetch = fetch_snapshot(transport=transport)
    if not fetch.get("ok"):
        return {"ok": False, "error": fetch.get("error"), "restored": False, "message": _floor4_fetch(fetch.get("error"))}
    manifest, ledger_bytes = fetch["manifest"], fetch["ledger_bytes"]

    if manifest.get("ledger-version") != ledger.LEDGER_FORMAT_VERSION:
        return {"ok": False, "error": "version-mismatch", "restored": False, "message": _MSG_VERSION_MISMATCH}
    backup_gen = manifest.get("ledger-generation")
    if not (isinstance(backup_gen, int) and not isinstance(backup_gen, bool) and backup_gen >= 0):
        return {"ok": False, "error": "bad-manifest", "restored": False, "message": _MSG_BAD_MANIFEST}

    # Resurrection guard — only relevant when the local ledger is NOT structurally empty (the fresh-machine case
    # has nothing to lose). A non-empty local ledger is protected when the backup is provably older OR its
    # generation is UNKNOWN (sidecar missing -> local_gen reads 0, which could mask a real higher generation).
    if not _local_structurally_empty():
        local_gen = ledger.generation()
        if (not _generation_known()) or (backup_gen < local_gen):
            if not override:
                surface_resurrection(local_gen, backup_gen, now=when, github=github)
                return {"ok": False, "error": "resurrection", "restored": False, "message": _MSG_RESURRECTION}

    local_count = _local_record_count()
    backup_count = _count_lines(ledger_bytes)
    answer = consent if consent is not None else _ask_restore_consent(local_count, backup_count)
    if str(answer).strip().lower() not in ("y", "yes"):
        return {"ok": False, "declined": True, "restored": False, "message": _MSG_DECLINED}

    return _apply_restore(ledger_bytes, backup_gen, backup_count)


def _apply_restore(ledger_bytes: bytes, backup_gen: int, backup_count: int) -> dict:
    """The crash-safe, serialized swap. Under the single-writer lock: write a validated sibling temp -> remove the
    existing index (so once it is gone, any concurrent query scans the live ledger and cannot trust a stale index
    over the swapped one, for any generation relationship) -> atomic replace -> stamp the backup's generation ->
    rebuild the index."""
    from memory import capture, index   # noqa: E402 — lazy: keep capture/index off the module-load path
    data_dir = ledger.ledger_dir()
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError:
        pass
    lock_fd = capture._acquire_lock(os.path.join(data_dir, capture.LOCK_FILENAME))
    if lock_fd is None:                                  # a live capture / compaction holds it — restore can't retry
        return {"ok": False, "error": "busy", "restored": False, "message": _MSG_BUSY}
    tmp = os.path.join(data_dir, _RESTORE_TMP)
    try:
        with open(tmp, "wb") as fh:
            fh.write(ledger_bytes)
        chk = ledger.read(path=tmp)                      # completeness: a complete, parseable ledger only
        if chk.torn_trailing or chk.malformed or (ledger_bytes and not chk.records):
            _quiet_remove(tmp)
            return {"ok": False, "error": "corrupt", "restored": False, "message": _MSG_CORRUPT}
        _quiet_remove(index.index_path())                # drop the stale index for the swap window
        ledger.replace_ledger(tmp, path=ledger.ledger_path())   # fsync temp -> atomic rename -> fsync dir
        ledger.set_generation(backup_gen)                # the restored content carries the backup's TRUE generation
        index.rebuild()                                  # rebuild from the restored ledger, re-stamp the generation
        return {"ok": True, "error": None, "restored": True, "message": _restored_msg(backup_count)}
    except Exception:  # noqa: BLE001 — any fault leaves the canonical ledger as it was before the rename
        _quiet_remove(tmp)
        return {"ok": False, "error": "apply-failed", "restored": False, "message": _MSG_APPLY_FAILED}
    finally:
        capture._release_lock(lock_fd)


def _quiet_remove(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


# ============================================================================================================
# Floor 3 — the local-only auto-restore-offer detector (boot relays it; boot owns the wording).
# ============================================================================================================

def detect_restore_offer() -> "dict | None":
    """LOCAL-ONLY (no network): an offer signal iff a backup is configured (the committed pointer) AND the local
    memory is structurally empty — the new-laptop recovery case. None otherwise (a configured-but-populated machine,
    or no backup). The fetch happens only when the operator accepts; boot renders the plain-language offer."""
    try:
        if not bv._setup_done() or not _local_structurally_empty():
            return None
        return {"configured": True}
    except Exception:  # noqa: BLE001 — a detector fault degrades to no-offer, never breaks the boot pack
        return None


# ============================================================================================================
# CLI verbs.
# ============================================================================================================

def status(*, now: "int | None" = None) -> int:
    """Read-only, plain voice (never 'generation'/'ledger'/'index'): is a backup set up, and is local memory empty
    (restore-eligible) or populated."""
    pointer = bv.read_pointer()
    if pointer is None:
        print(_MSG_NOT_CONFIGURED)
        return 0
    where = f"{pointer['owner']}/{pointer['repo']}"
    print(f"A backup is set up for this project (your private repository \"{where}\").")
    if _local_structurally_empty():
        print("Your memory on this computer is empty — say \"restore my memory\" and I'll try to bring it back from "
              "the backup.")
    else:
        print(f"Your memory on this computer has {_local_record_count()} note(s); restoring would replace them with "
              "the backup copy.")
    return 0


def _restore_cli() -> int:
    print(restore_now()["message"])
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "restore":
        return _restore_cli()
    if cmd == "status":
        return status()
    if cmd == "demo":
        return _demo_live() if "--live" in argv[1:] else _demo()
    print(f"usage: restore_vault.py [restore|status|demo [--live]]\nunknown command {cmd!r}", file=sys.stderr)
    return 2


# ============================================================================================================
# Operator demonstration — REAL restore logic against the in-memory _FakeVault; only the network is stubbed.
# ============================================================================================================
# A fully offline round-trip on a throwaway memory cabinet + repo root (the real ledger and pointer are never
# touched): back up some memory, WIPE the local copy, RESTORE it, and prove it came back identical and searchable;
# then prove the resurrection guard refuses an older backup, the consent text on a populated machine, and the
# Floor-4 degrade on an unreachable backup. Vary it: change the notes, the generations, force the fetch to fail.

def _demo() -> int:
    import tempfile
    print("=" * 96)
    print("MEMORY — the engine restores your AI memory from its private backup (practice run)")
    print("=" * 96)
    with tempfile.TemporaryDirectory() as cabinet, tempfile.TemporaryDirectory() as root:
        import validate
        old_root = validate.ROOT
        os.environ["ENGINE_MEMORY_DIR"] = cabinet
        validate.ROOT = root
        os.makedirs(os.path.join(root, ".engine"), exist_ok=True)
        with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            json.dump({"engine_release": "0.0.0-dev"}, fh)
        try:
            ok = _demo_body()
        finally:
            validate.ROOT = old_root
            os.environ.pop("ENGINE_MEMORY_DIR", None)
    print("\n" + "-" * 96)
    print("What this just proved: after a backup, the engine can WIPE the local memory and bring it back identical")
    print("and searchable — so a dead disk or a new laptop no longer loses 'how did I get here'. It will NOT silently")
    print("overwrite newer memory with an older backup (it surfaces that and refuses), it tells you plainly what will")
    print("be replaced before it does anything, and a failed fetch changes nothing. That was a PRACTICE run, thrown")
    print("away. To prove it end-to-end on your REAL GitHub — a throwaway private repo created, backed up to, restored")
    print("from, and deleted — run this command with --live.")
    return 0 if ok else 1


def _demo_body() -> bool:
    from memory import index

    def query_hits(text: str) -> int:
        return len(index.query(text).records)

    # --- PART 1 — back up, WIPE, then RESTORE: the memory comes back identical and searchable -----------------
    print("\nPART 1 — back up your memory, lose it, and restore it (the round trip)")
    print("-" * 96)
    bv._demo_plant("Decided the launch banner ships in the spring release.")
    bv._demo_plant("Lesson: never deploy on a Friday — RUMBLEDETHUMPS.")
    fake = bv._FakeVault()
    bv.setup(transport=fake.transport, consent="y")                 # creates the vault + pushes this memory
    original = _read_bytes(ledger.ledger_path())
    before_hits = query_hits("rumbledethumps")
    os.remove(ledger.ledger_path())                                 # simulate disk loss
    _quiet_remove(ledger.meta_path())
    lost = _local_structurally_empty()
    restored = restore_now(transport=fake.transport, consent="y", github=None)
    came_back = _read_bytes(ledger.ledger_path())
    after_hits = query_hits("rumbledethumps")
    print(f"  backed up 2 notes, then wiped the local copy (empty now: {lost})")
    print(f"  restore says: {restored['message']}")
    print(f"  the restored memory is byte-identical to the original: {came_back == original}")
    print(f"  and it is searchable again (a known note found before={before_hits}, after={after_hits})")
    part1 = (restored.get("ok") is True and restored.get("restored") is True and came_back == original
             and lost is True and before_hits == 1 and after_hits == 1)
    print(f"  => {'memory survived a total local loss.' if part1 else '!!! the round trip failed'}")

    # --- PART 2 — the resurrection guard: an OLDER backup is surfaced, not silently applied -------------------
    print("\nPART 2 — it refuses to silently bring back notes you've removed since (resurrection guard)")
    print("-" * 96)
    ledger.set_generation(9)                                        # pretend this machine has compacted/erased since
    guarded = restore_now(transport=fake.transport, consent="y", github=None)   # backup generation is 0 < 9
    still_there = _read_bytes(ledger.ledger_path()) == came_back
    print(f"  the engine's plain-language message: \"{guarded['message']}\"")
    part2 = guarded.get("ok") is False and guarded.get("error") == "resurrection" and still_there
    print(f"  => {'an older backup is surfaced and refused — your memory is untouched.' if part2 else '!!! the guard failed'}")

    # --- PART 3 — the same restore, but the operator EXPLICITLY overrides ------------------------------------
    print("\nPART 3 — if you truly want the older copy, an explicit override restores it")
    print("-" * 96)
    forced = restore_now(transport=fake.transport, consent="y", override=True, github=None)
    part3 = forced.get("ok") is True and forced.get("restored") is True
    print(f"  with an explicit override: {forced['message']}")
    print(f"  => {'the operator stays in control.' if part3 else '!!! the override failed'}")

    # --- PART 4 — the auto-restore offer fires only on an EMPTY machine with a backup -------------------------
    print("\nPART 4 — on a fresh machine the engine OFFERS to restore (Floor 3), and stays quiet otherwise")
    print("-" * 96)
    populated_offer = detect_restore_offer()                       # memory is present now -> no offer
    os.remove(ledger.ledger_path()); _quiet_remove(ledger.meta_path())
    empty_offer = detect_restore_offer()                           # empty + configured pointer -> offer
    print(f"  with memory present: {'offers' if populated_offer else 'stays quiet'}")
    print(f"  with memory empty + a backup configured: {'offers to restore' if empty_offer else 'stays quiet'}")
    part4 = populated_offer is None and bool(empty_offer)
    print(f"  => {'the offer appears exactly when recovery is wanted.' if part4 else '!!! the offer mis-fired'}")

    # --- PART 5 — the consent text shown before any overwrite (a populated machine) ---------------------------
    print("\nPART 5 — the exact words you see before anything is replaced (a populated machine)")
    print("-" * 96)
    for line in _restore_consent_prompt(42, 40).rstrip().splitlines():
        print(f"    | {line}")
    part5 = "will be gone" in _restore_consent_prompt(42, 40)
    print(f"  => {'the overwrite is named plainly, before it happens.' if part5 else '!!! the consent text is unclear'}")

    # --- PART 6 — degrade-and-disclose: an unreachable backup changes nothing (Floor 4) ----------------------
    print("\nPART 6 — if the backup can't be reached, nothing on this computer changes (Floor 4)")
    print("-" * 96)
    bv._demo_plant("A note that must survive a failed restore.")
    guard_bytes = _read_bytes(ledger.ledger_path())
    def _dead_transport(method, path, body=None):
        return None, None
    failed = restore_now(transport=_dead_transport, consent="y", github=None)
    unchanged = _read_bytes(ledger.ledger_path()) == guard_bytes
    print(f"  the engine's plain-language message: \"{failed['message']}\"")
    part6 = failed.get("ok") is False and unchanged and "http" not in failed["message"].lower()
    print(f"  => {'a failure names a consequence and one action, and changes nothing.' if part6 else '!!! a failed fetch was mishandled'}")

    ok = part1 and part2 and part3 and part4 and part5 and part6
    if not ok:
        print("\nDEMO UNEXPECTED: a restore guarantee did not hold (the round trip, the resurrection guard, the "
              "override, the offer, the consent text, or degrade-and-disclose).", file=sys.stderr)
    return bool(ok)


def _demo_live() -> int:
    """The LIVE end-to-end test the operator runs: create a throwaway PRIVATE repo, back a tiny FAKE memory up into
    it, RESTORE from it into a throwaway cabinet and prove it round-trips, then DELETE the repo. The real ledger and
    pointer are never touched; the DELETE is name-guarded (backup_vault._safe_demo_delete) to the disposable repo."""
    import tempfile
    print("=" * 96)
    print("LIVE TEST — creates a REAL, throwaway PRIVATE repo on your GitHub, backs a tiny fake memory up into it,")
    print("            restores it back and checks it matches, then DELETES the repo. Real memory is never touched.")
    print("=" * 96)
    project = bv._project_slug()
    if not project or "/" not in project:
        print(f"\n  {bv._MSG_NO_PROJECT}")
        return 0
    gh = bv._gh()
    if gh is None:
        print(f"\n  {bv._MSG_NO_TOKEN}")
        return 0
    import secrets
    project_name = project.split("/")[-1]
    demo_name = f"{project_name}{bv._DEMO_MARKER}{secrets.token_hex(4)}"
    status_code, repo_obj = bv._send(gh, "POST", "/user/repos",
                                     {"name": demo_name, "private": True, "auto_init": True,
                                      "description": "Throwaway engine memory-restore live test — safe to delete."})
    if status_code == 403:
        print(f"\n  {bv._MSG_NO_SCOPE}")
        return 0
    if status_code not in (200, 201) or not isinstance(repo_obj, dict):
        print("\n  I couldn't create the throwaway test repository just now. Nothing was created; try again later.")
        return 0
    owner = (repo_obj.get("owner") or {}).get("login")
    repo = repo_obj.get("name")
    branch = repo_obj.get("default_branch") or "main"
    print(f"\n  Created a throwaway private repo: https://github.com/{owner}/{repo}")
    with tempfile.TemporaryDirectory() as cabinet, tempfile.TemporaryDirectory() as root:
        import validate
        old_root = validate.ROOT
        os.environ["ENGINE_MEMORY_DIR"] = cabinet
        validate.ROOT = root
        os.makedirs(os.path.join(root, ".engine"), exist_ok=True)
        with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            json.dump({"engine_release": "0.0.0-dev"}, fh)
        try:
            bv._demo_plant("A throwaway note for the live restore test.")
            original = _read_bytes(ledger.ledger_path())
            files = {"livetest/ledger.ndjson": original,
                     "livetest/manifest.json": (json.dumps(bv.build_manifest(ledger_path=ledger.ledger_path())) + "\n").encode()}
            pushed = bv._push_files(gh, owner, repo, branch, files)
            bv.write_pointer(owner, repo, branch, "livetest")
            os.remove(ledger.ledger_path())
            restored = restore_now(consent="y", github=None)
            came_back = _read_bytes(ledger.ledger_path()) if os.path.exists(ledger.ledger_path()) else b""
            print(f"  Backed up a tiny fake memory: {'yes' if pushed else 'no'}")
            print(f"  Restored it from the real repo and it matched: {came_back == original and restored.get('ok')}")
        finally:
            validate.ROOT = old_root
            os.environ.pop("ENGINE_MEMORY_DIR", None)
    if bv._safe_demo_delete(repo, project_name):
        del_status, _ = bv._send(gh, "DELETE", f"/repos/{owner}/{repo}")
        if del_status in (200, 204):
            print("  Deleted the throwaway repo. Nothing is left behind.")
        else:
            print(f"  I couldn't auto-delete it (that repo is PRIVATE and harmless). Remove it yourself with:\n"
                  f"      gh repo delete {owner}/{repo} --yes")
    else:
        print(f"  Safety: the repo name didn't look disposable, so I did NOT delete it:\n"
              f"      gh repo delete {owner}/{repo} --yes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
