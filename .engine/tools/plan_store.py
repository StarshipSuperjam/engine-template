#!/usr/bin/env python3
"""The durable local plan library: where plans live, and what it takes to trust what is there.

Why durable, and why local. Build coordinator state lives in OS temp on purpose — one Build's
current facts, expected to die with the machine. Planning state is the opposite: an operator's
intent, the deliberation behind it, and the revision chain that got from one to the other, all of
which took real thought and none of which can be reconstructed. That state was observed to vanish
across a reboot. So this store is durable. And it is local — gitignored, never published — because a
plan under discussion is the operator's own working thought rather than a reviewable project
artifact, and it may name things that must not become public. The honest cost of that pairing is
that recovery is workstation-only; that is stated plainly rather than engineered around.

WHERE the library lives is the subtlest thing in this module. A planning session usually runs in the
engine's own checkout while the Build it plans runs in a worktree of a DIFFERENT repository. Resolve
the root naively and the plan lands where the Build can never find it. The precedence is therefore
explicit and it refuses rather than guesses:

  1. ENGINE_PLAN_DIR, when set — the escape hatch, and what the tests drive.
  2. The recorded product checkout, when this deployment has one and its local path is known. An
     owned product's plans belong in the product's own canonical checkout.
  3. Otherwise the engine's own common checkout — the durable clone root, which is shared by every
     linked worktree of that clone, so a session in a worktree and a session in the main checkout
     reach the SAME library.

A mechanic whose product target is recorded but whose local path is unset is AMBIGUOUS, and the
store refuses. Falling back to the engine root there would silently create a second library that
looks fine and is invisible to every Build.

Two things this module deliberately does not do. It does not import `mechanic_build`: that module
sits at the killswitch tier, and a plan library is not a reason to widen what depends on it. And it
does not call `resolve_build_target`, whose checkout-health leg refuses on a dirty tree — correct for
entering a Build, catastrophic for reading a plan, since it would make an operator's plans
unreadable exactly when a messy working tree makes them most needed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import subprocess

import build_coordinator_core as core
import checkout_health
import moment
import plan_contract

PlanStoreError = core.CoordinatorError

ROOT = Path(__file__).resolve().parents[2]
RECORD_SCHEMA = ROOT / ".engine" / "schemas" / "plan-record.v1.json"

# Fields a record on disk may carry that plan-record.v1 no longer declares. The record schema forbids
# unknown properties and every read and write validates, so a retired field has to be dropped at the
# raw read — the one door every reader and every mutator comes through — or the record becomes
# unreadable the moment the removal lands. `delivered_efforts` and `effort_shortfall_accepted` left
# `plan_review` when review depth became the lens roster alone.
_RETIRED_REVIEW_FIELDS = ("delivered_efforts", "effort_shortfall_accepted")


def forward_migrate_record(record: dict) -> dict:
    """Drop retired `plan_review` fields from a record read off disk; returns the record itself when
    there is nothing to drop, and a copy (the file is never edited underneath its caller) when there is."""
    review = record.get("plan_review") if isinstance(record, dict) else None
    if not isinstance(review, dict) or not any(k in review for k in _RETIRED_REVIEW_FIELDS):
        return record
    out = dict(record)
    out["plan_review"] = {k: v for k, v in review.items() if k not in _RETIRED_REVIEW_FIELDS}
    return out

ENV_DIR = "ENGINE_PLAN_DIR"
LIBRARY_SUBDIR = os.path.join(".engine", "plans")
RECORD_FILENAME = "record.json"
REVISIONS_DIRNAME = "revisions"

# Every directory the store creates is owner-only and every file it writes is owner-read/write.
# Applied explicitly rather than left to the process umask: the content is operator intent, and
# "private unless the umask says otherwise" is not a confidentiality property.
DIR_MODE = 0o700
FILE_MODE = 0o600

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*--[0-9a-f]{6}$")
_PLAN_ID_RE = re.compile(r"^pln_[0-9a-f]{12}$")

# Path fragments that mean a cloud-sync client is managing the bytes underneath us. A sync client
# can resurrect a deleted file, revert a rename, or present a stale copy — every one of which
# defeats a compare-and-swap that assumes the filesystem is the only writer.
_SYNCED_MARKERS = (
    "/library/mobile documents",      # iCloud Drive
    "/dropbox/", "/google drive/", "/googledrive/", "/onedrive", "/box sync/", "/pcloud",
    "/sync.com/", "/nextcloud/", "/owncloud/", "/mega/", "/creative cloud files/",
)
# Filesystem types that are not local disks. Advisory locks over these are unreliable between hosts.
_NETWORK_FSTYPES = ("nfs", "smbfs", "cifs", "afpfs", "webdav", "fuse.sshfs", "ftp", "9p")


# The wire timestamp shape has one home in the engine (moment), so a store cannot drift into its own
# format or its own idea of "now".
_now = moment.utc_now


def contain(candidate: Path, boundary: Path, what: str) -> Path:
    """Return `candidate` resolved, or refuse if it escapes `boundary`. THE containment chokepoint.

    Every path this engine builds from data it did not mint itself passes through here. The hazard is
    not exotic: `Path("/library") / "/etc/passwd"` is `/etc/passwd` — an absolute component silently
    discards everything to its left — and `..` walks out just as easily. A store that joins an
    imported record's own strings onto its root without this is an arbitrary-file-write primitive
    wearing the costume of a plan importer.

    Resolves both sides before comparing, so a symlink cannot point out of the library and back in
    under a name that looks contained.
    """
    resolved = Path(candidate).resolve()
    limit = Path(boundary).resolve()
    if resolved != limit and limit not in resolved.parents:
        raise PlanStoreError(
            f"refused: {what} would land at {resolved}, outside the plan library at {limit}. A plan's "
            "own record cannot choose where it is written; this is what an imported bundle would need "
            "to do to overwrite a file elsewhere on the machine.")
    return resolved


def ensure_dir(path: Path, *, within: Path | None = None) -> None:
    """Create a library directory owner-only, and every library directory on the way to it.

    Two traps this closes, both of which leave a world-readable directory beside 0700 ones.
    `mkdir(exist_ok=True)` ignores its `mode` for a directory that already exists, and the process
    umask can clip the mode for one that does not — hence the explicit chmod. And `mkdir(parents=True)`
    applies `mode` ONLY to the leaf, creating intermediates at the default 0777-minus-umask: that is
    how the library root itself ends up 0755 while every plan folder inside it is 0700, leaving slug
    names — which carry plan titles — readable by every account on the machine.

    `within` REFUSES: a target that is not the boundary itself or inside it raises before anything is
    created, and the permission walk then stops at the boundary so it never touches a parent that
    belongs to the operator or the system. The refusal is the load-bearing half. An earlier version
    bounded only the walk and created the directory unconditionally first, which made `within` read
    like containment while providing none — a trap for any caller that passed a path derived from
    outside input believing this helper made it safe.
    """
    if within is not None:
        contain(path, Path(within), "a directory")
    boundary = Path(within).resolve() if within is not None else path.resolve()
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    current = path.resolve()
    while True:
        try:
            os.chmod(current, DIR_MODE)
        except OSError:
            pass
        if current == boundary or current == current.parent or boundary not in current.parents:
            break
        current = current.parent


# --- Where the library lives -------------------------------------------------

def library_root(cwd: str | None = None) -> Path:
    """The absolute path to this instance's plan library, or a refusal naming why it is ambiguous."""
    override = os.environ.get(ENV_DIR)
    if override and override.strip():
        return Path(os.path.expanduser(override.strip())).resolve()

    product_path, product_state = checkout_health.resolve_product_checkout(cwd)
    if product_state == "path-unset":
        raise PlanStoreError(
            "this deployment builds an owned product, but this machine's checkout path for it is not set, "
            "so there is no unambiguous home for its plans. Set the product checkout path (or "
            f"{ENV_DIR}) before using the plan library — writing to the engine's own folder instead would "
            "create a second library that no Build can see.")
    if product_path:
        return Path(product_path).resolve() / LIBRARY_SUBDIR

    common = checkout_health.engine_common_checkout(cwd)
    if not common:
        raise PlanStoreError(
            "could not resolve this engine's canonical checkout, so there is no unambiguous home for the "
            f"plan library. Set {ENV_DIR} to say where plans belong — creating one relative to the current "
            "folder would produce a worktree-local library that vanishes with the worktree.")
    return Path(common).resolve() / LIBRARY_SUBDIR


def volume_warning(path: Path | None = None) -> str | None:
    """A plain-language warning when the library sits somewhere its guarantees do not hold, else None.

    Both cases break the same assumption: that this store and the operating system are the only
    things writing these files. A sync client can resurrect, revert, or stale a file underneath a
    compare-and-swap; a network filesystem makes the advisory lock unreliable across hosts. Neither
    is refused — an operator may have good reasons, and refusing would strand them — but neither is
    passed over in silence.

    Three outcomes, not two, and the third is the honest one: None means NO PROBLEM FOUND, and a
    caller that needs to know whether the check actually ran must ask `volume_determined()`. An
    earlier version claimed "unknown never reads as local" while returning None for both, which is
    the reassurance-by-omission it was written to avoid.
    """
    target = Path(path) if path is not None else library_root()
    lowered = str(target).lower()
    for marker in _SYNCED_MARKERS:
        if marker in lowered + "/":
            return (f"The plan library at {target} appears to sit inside a cloud-synced folder. A sync client "
                    "can restore a deleted file, undo a rename, or hand back a stale copy, any of which "
                    "defeats the store's lock and its compare-and-swap. Plans are local and private by "
                    "design; syncing them also copies raw operator intent to a third party. Move the library "
                    f"off the synced folder, or set {ENV_DIR} to somewhere local.")
    fstype = _filesystem_type(target)
    if fstype and any(fstype.startswith(net) for net in _NETWORK_FSTYPES):
        return (f"The plan library at {target} sits on a {fstype} network filesystem. The store's advisory "
                "file lock is not reliable across hosts there, so two machines could write the same plan "
                f"without either being refused. Move the library to a local disk, or set {ENV_DIR}.")
    return None


def volume_determined(path: Path | None = None) -> bool:
    """Could the network-filesystem check actually run here?

    A path-marker match (iCloud, Dropbox and friends) is decided from the path alone and always
    counts as determined. Otherwise it depends on whether the platform probe answered. `doctor`
    reports an undetermined volume as a stated gap rather than folding it into "no problems found" —
    a check that did not run and a check that passed are different facts, and only one of them is
    reassuring.
    """
    target = Path(path) if path is not None else library_root()
    lowered = str(target).lower() + "/"
    if any(marker in lowered for marker in _SYNCED_MARKERS):
        return True
    return _filesystem_type(target) is not None


def _filesystem_type(path: Path) -> str | None:
    """The filesystem type at `path`, or None when it cannot be determined.

    Python exposes no portable API for this, so each platform gets its own probe: `df`/`stat` on
    Darwin, `/proc/mounts` on Linux, nothing anywhere else. Both degrade quietly to None, and
    `volume_determined` is what tells a caller that None meant "could not tell" rather than "fine".

    `path` is resolved up to its nearest EXISTING ancestor first, because the usual caller asks about
    a library that has not been created yet. That is the normal case here, not an edge one.
    """
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        if os.uname().sysname == "Darwin":
            out = subprocess.run(["df", "-T", "nfs,smbfs,afpfs,webdav", str(probe)],
                                 capture_output=True, text=True, timeout=5)
            lines = [ln for ln in out.stdout.splitlines()[1:] if ln.strip()]
            if lines:
                out2 = subprocess.run(["stat", "-f", "%T", str(probe)],
                                      capture_output=True, text=True, timeout=5)
                return (out2.stdout.strip() or "network").lower()
            return None
        with open("/proc/mounts", encoding="utf-8") as handle:
            best, best_type = "", None
            for line in handle:
                parts = line.split()
                if len(parts) >= 3 and str(probe).startswith(parts[1]) and len(parts[1]) >= len(best):
                    best, best_type = parts[1], parts[2]
            return best_type.lower() if best_type else None
    except (OSError, subprocess.SubprocessError, AttributeError):
        return None


# --- Identity ----------------------------------------------------------------

def mint_plan_id() -> str:
    return "pln_" + secrets.token_hex(6)


def slug_for(title: str, plan_id: str) -> str:
    """A readable folder name plus the id's last six hex characters, so two plans that happen to share
    a title never share a folder. Minted once; a later retitle changes `title` and leaves this alone."""
    stem = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48].strip("-") or "plan"
    if not stem[0].isalnum():
        stem = "plan-" + stem
    return f"{stem}--{plan_id[-6:]}"


# --- The library -------------------------------------------------------------

class PlanLibrary:
    """The one door to the plan library. Every write goes through the lock; every read of a head
    checks that the bytes on disk still hash to what the record says they do."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root).resolve() if root is not None else library_root()

    # -- paths --
    def plan_dir(self, slug: str) -> Path:
        return self.root / slug

    def _record_path(self, slug: str) -> Path:
        return self.plan_dir(slug) / RECORD_FILENAME

    def _lock_path(self, slug: str) -> Path:
        return self.plan_dir(slug) / (RECORD_FILENAME + ".lock")

    def _mkdir(self, path: Path) -> None:
        ensure_dir(path, within=self.root)

    def _write_json(self, path: Path, value: dict) -> None:
        core.atomic_write(path, json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                          durable=True, mode=FILE_MODE)

    # -- listing and selection --
    def slugs(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir()
                      if p.is_dir() and _SLUG_RE.match(p.name) and (p / RECORD_FILENAME).is_file())

    def resolve(self, selector: str) -> str:
        """Select a plan by full id, unique id prefix, or slug. NOTHING auto-selects: not the newest
        plan, not the only plan. An operator who typed an ambiguous prefix is told which plans it
        matched, because silently picking one of them is how the wrong plan gets sealed."""
        selector = (selector or "").strip()
        if not selector:
            raise PlanStoreError("name a plan by id, unique id prefix, or slug; nothing is selected by default")
        available = self.slugs()
        if selector in available:
            return selector
        records = {slug: self._read_record_unchecked(slug) for slug in available}
        exact = [slug for slug, rec in records.items() if rec.get("plan_id") == selector]
        if exact:
            return exact[0]
        prefix = [slug for slug, rec in records.items()
                  if str(rec.get("plan_id", "")).startswith(selector)] if selector.startswith("pln") else []
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise PlanStoreError(
                f"{selector!r} matches {len(prefix)} plans: "
                + ", ".join(f"{records[s]['plan_id']} ({s})" for s in sorted(prefix))
                + ". Name one exactly.")
        raise PlanStoreError(
            f"no plan matches {selector!r}"
            + (". The library is empty." if not available else "; the library holds: " + ", ".join(available)))

    # -- reading --
    def _read_record_unchecked(self, slug: str) -> dict:
        path = self._record_path(slug)
        if not path.is_file():
            raise PlanStoreError(f"no plan record at {path}; the folder is not a usable plan")
        return forward_migrate_record(core.json_file(path))

    def read_record(self, slug: str) -> dict:
        record = self._read_record_unchecked(slug)
        core.validate(record, RECORD_SCHEMA)
        return record

    def read_revision(self, slug: str, revision: int) -> dict:
        """Read one revision and prove it is the revision the record says it is.

        The read-time chain-integrity rule, stated once and applied everywhere: a revision file is
        trusted only when its canonical digest equals the digest the record recorded for it. A file
        that does not hash to its entry is CORRUPT, not merely different, and is refused rather than
        returned — a plan is a document people act on, and handing back altered bytes under the name
        of a digest that no longer describes them is the one failure this store must not have.
        """
        record = self.read_record(slug)
        entry = next((e for e in record["ledger"] if e["revision"] == revision), None)
        if entry is None:
            raise PlanStoreError(f"plan {slug} has no revision {revision}")
        if "redacted" in entry:
            raise PlanStoreError(
                f"revision {revision} of {slug} was redacted on {entry['redacted']['at']} "
                f"({entry['redacted']['reason']}); its body is gone by intent and cannot be read")
        path = self.plan_dir(slug) / entry["snapshot"]
        if not path.is_file():
            raise PlanStoreError(
                f"revision {revision} of {slug} is missing from disk ({entry['snapshot']}). It was not "
                "redacted, so this is loss rather than intent; the chain is broken at that point and the "
                "revisions on either side of it are unaffected.")
        document = core.json_file(path)
        actual = core.digest(document)
        if actual != entry["plan_digest"]:
            raise PlanStoreError(
                f"revision {revision} of {slug} does not match its recorded digest (recorded "
                f"{entry['plan_digest']}, found {actual}); the file was changed outside the store and is "
                "not trustworthy")
        return document

    def head(self, slug: str) -> dict:
        """The current revision, verified. See `recover_head` for what to do when this refuses."""
        record = self.read_record(slug)
        return self.read_revision(slug, record["current"]["revision"])

    def verify_chain(self, slug: str) -> list[str]:
        """Every integrity problem in this plan's chain, in operator-facing language; empty when sound.

        Reported as a list rather than raised one at a time so an operator sees the whole picture:
        knowing that revisions 3 and 7 are damaged is a different situation from knowing only about 3.
        """
        problems: list[str] = []
        try:
            record = self.read_record(slug)
        except PlanStoreError as exc:
            return [str(exc)]
        # Collected first: a revision with an interrupted redaction gets ONE truthful line below,
        # not that line plus a "loss rather than intent" that contradicts it.
        interrupted = set(self.interrupted_redactions(slug))
        expected = 1
        seen: set = set()
        for entry in record["ledger"]:
            duplicate = entry["revision"] in seen
            seen.add(entry["revision"])
            if duplicate:
                problems.append(
                    f"revision {entry['revision']} appears more than once in the ledger; each revision "
                    "is minted once and its entry is never rewritten, so a duplicate means the record "
                    "was edited outside the store")
            elif entry["revision"] < expected:
                problems.append(
                    f"revision {entry['revision']} appears after revision {expected - 1}; the ledger is "
                    "append-only and oldest-first, so an out-of-order entry means the record was "
                    "reordered outside the store")
            elif entry["revision"] != expected:
                problems.append(
                    f"the revision chain jumps from {expected - 1} to {entry['revision']}; revisions must be "
                    "contiguous from 1, and a gap means an entry was removed from the record itself")
            if entry["revision"] != expected:
                expected = entry["revision"]
            expected += 1
            if "redacted" in entry or entry["revision"] in interrupted:
                continue
            try:
                self.read_revision(slug, entry["revision"])
            except PlanStoreError as exc:
                problems.append(str(exc))
        head_rev = record["current"]["revision"]
        if not any(e["revision"] == head_rev for e in record["ledger"]):
            problems.append(f"the record's head points at revision {head_rev}, which is not in the ledger")
        marked = {e["revision"] for e in record["ledger"] if "redacted" in e}
        for revision in self.interrupted_redactions(slug):
            entry = next((e for e in record["ledger"] if e["revision"] == revision), None)
            body_present = bool(entry) and (self.plan_dir(slug) / entry["snapshot"]).exists()
            if revision in marked and not body_present:
                # Marked AND the body is gone: the redaction finished and only the marker's own
                # removal did not. Reporting that as unfinished would send an operator to rotate a
                # credential that was in fact excised — over-cautious, but this claims every window
                # reads truthfully, and a warning nobody can act on erodes the ones they should.
                continue
            problems.append(
                f"the redaction of revision {revision} began and did not finish, so its body may still "
                f"be on disk even if the record says otherwise. Re-run `redact` on revision {revision} "
                "to complete it. If what was redacted was a credential, rotate it regardless.")
        return problems

    def recover_head(self, slug: str) -> tuple[int, list[str]]:
        """Find the newest revision that is actually intact, and say what was passed over to get there.

        Recovery, not repair: the record is left exactly as it is. A corrupt head is a fact an
        operator needs to decide about — resume from the intact ancestor, or restore the damaged file
        from a backup — and quietly rewriting the record would take that decision away and destroy
        the evidence of what happened.
        """
        record = self.read_record(slug)
        skipped: list[str] = []
        for entry in sorted(record["ledger"], key=lambda e: e["revision"], reverse=True):
            try:
                self.read_revision(slug, entry["revision"])
                return entry["revision"], skipped
            except PlanStoreError as exc:
                skipped.append(str(exc))
        raise PlanStoreError(
            f"no intact revision of {slug} remains; every entry in the ledger is missing, redacted or "
            "corrupt: " + "; ".join(skipped))

    # -- writing --
    def create(self, document: dict, *, intake: dict | None = None) -> str:
        """Mint a new plan from a validated revision-1 document. Returns its slug."""
        plan_contract.validate_document(document)
        if document["revision"] != 1:
            raise PlanStoreError(f"a new plan starts at revision 1, not {document['revision']}")
        plan_id = document["plan_id"]
        if not _PLAN_ID_RE.match(plan_id):
            raise PlanStoreError(f"malformed plan id {plan_id!r}")
        slug = slug_for(document["title"], plan_id)
        plan_dir = self.plan_dir(slug)
        self._mkdir(plan_dir)
        with core.exclusive_lock(self._lock_path(slug)):
            # INSIDE the lock. Checking before acquiring it would let two concurrent creates both
            # pass and the second silently overwrite the first — the exact shape the compare-and-swap
            # exists to prevent everywhere else in this class.
            if (plan_dir / RECORD_FILENAME).exists():
                raise PlanStoreError(f"a plan already exists at {plan_dir}")
            # The remaining directories are created after the existence check, so a create that
            # refuses (or that fails validation below) does not leave an orphan skeleton behind.
            self._mkdir(plan_dir / REVISIONS_DIRNAME)
            for extra in ("reviews", "seals", "builds"):
                self._mkdir(plan_dir / extra)
            digest_value = core.digest(document)
            snapshot = self._snapshot_name(1, digest_value)
            self._write_json(plan_dir / snapshot, document)
            record = {
                "schema_version": "plan-record.v1",
                "plan_id": plan_id,
                "slug": slug,
                "title": document["title"],
                "created_at": document["created_at"],
                "current": {
                    "revision": 1,
                    "plan_digest": digest_value,
                    "build_plan_digest": plan_contract.build_plan_digest(document),
                    "snapshot": snapshot,
                },
                "ledger": [{"revision": 1, "plan_digest": digest_value, "snapshot": snapshot,
                            "revised_at": document["revised_at"],
                            **({"note": document["revision_note"]} if document.get("revision_note") else {})}],
                "approval": None, "plan_review": None, "seal": None, "build_binding": None,
            }
            if intake:
                record["intake"] = intake
            core.validate(record, RECORD_SCHEMA)
            self._write_json(self._record_path(slug), record)
        return slug

    @staticmethod
    def _snapshot_name(revision: int, digest_value: str) -> str:
        return f"{REVISIONS_DIRNAME}/{revision:06d}--{digest_value.split(':')[1][:6]}.json"

    def append_revision(self, slug: str, document: dict, *, expected_revision: int) -> dict:
        """Mint the next revision. `expected_revision` is the head the caller believes it is building
        on; a writer holding a stale head is refused and NOTHING it holds is written."""
        plan_contract.validate_document(document)
        with core.exclusive_lock(self._lock_path(slug)):
            record = self.read_record(slug)
            core.assert_revision(record["current"]["revision"], expected_revision, "plan",
                                 "another session revised this plan; re-read it and re-apply your change")
            if document["plan_id"] != record["plan_id"]:
                raise PlanStoreError(
                    f"this revision belongs to plan {document['plan_id']}, not {record['plan_id']}")
            next_revision = record["current"]["revision"] + 1
            if document["revision"] != next_revision:
                raise PlanStoreError(
                    f"the revision is numbered {document['revision']} but the next revision here is "
                    f"{next_revision}")
            digest_value = core.digest(document)
            snapshot = self._snapshot_name(next_revision, digest_value)
            self._write_json(self.plan_dir(slug) / snapshot, document)
            record["ledger"].append(
                {"revision": next_revision, "plan_digest": digest_value, "snapshot": snapshot,
                 "revised_at": document["revised_at"],
                 **({"note": document["revision_note"]} if document.get("revision_note") else {})})
            record["current"] = {
                "revision": next_revision,
                "plan_digest": digest_value,
                "build_plan_digest": plan_contract.build_plan_digest(document),
                "snapshot": snapshot,
            }
            record["title"] = document["title"]
            # The approval and the review are NOT cleared here, and that is the whole cadence.
            #
            # The order is approve -> one cold review -> fold the fixes in as revisions -> one
            # proportional judgment of the delta -> seal. Folding a fix is a revision, so clearing the
            # approval on revision would make the agreed sequence impossible: by the time the plan was
            # sealable there would be no record it had ever been approved, and the only way out would
            # be re-approving and re-reviewing after every fix — the death spiral this cadence exists
            # to avoid.
            #
            # Both gates record the revision and digest they were granted against, so a stale approval
            # is DERIVED (approved, never reviewed, and the head has moved since) rather than erased.
            # Deriving it keeps the evidence: an operator can still see what was approved and when,
            # which is exactly what they need in order to decide whether re-approving is warranted.
            core.validate(record, RECORD_SCHEMA)
            self._write_json(self._record_path(slug), record)
            return record

    def update_record(self, slug: str, change, *, expected_revision: int | None = None) -> dict:
        """Apply `change` to the record under the lock, with the same compare-and-swap. For the gate
        evidence — approval, review, seal, binding — which changes the record without minting a
        revision. The revisions themselves are never touched here; they are immutable.

        `change` receives the record as re-read INSIDE the lock, and that is the only copy it may
        judge. A caller that decides "no review is recorded yet" from a copy read before the lock and
        then writes unconditionally has a check-then-act race: the compare-and-swap on
        `current.revision` will not catch it, because recording a review does not mint a revision. So
        every single-minted gate re-asserts its own precondition inside `change` and raises there.
        """
        with core.exclusive_lock(self._lock_path(slug)):
            record = self.read_record(slug)
            core.assert_revision(record["current"]["revision"], expected_revision, "plan",
                                 "another session revised this plan; re-read it and re-apply your change")
            change(record)
            core.validate(record, RECORD_SCHEMA)
            self._write_json(self._record_path(slug), record)
            return record

    def redact_revision(self, slug: str, revision: int, *, reason: str) -> dict:
        """Excise one revision's BODY, leaving the chain honest and the excision visible.

        Plans hold whatever the operator said, and sometimes that must be removed — a credential
        pasted into raw intent, a name that should never have been written down. The body is deleted;
        the ledger entry, its digest, and its place in the sequence stay. So the record still shows
        that a revision existed here and what it hashed to, and `verify_chain` still passes: a
        redaction reads as a deliberate act, never as a hole that looks like corruption.

        The head cannot be redacted. Redacting the document the plan currently IS would leave the plan
        with no readable current state; revise first, then redact the old revision.
        """
        if not reason or not reason.strip():
            raise PlanStoreError("a redaction needs a stated reason; an unexplained hole in the record is "
                                 "indistinguishable from damage")
        with core.exclusive_lock(self._lock_path(slug)):
            record = self.read_record(slug)
            if revision == record["current"]["revision"]:
                raise PlanStoreError(
                    f"revision {revision} is the current head of {slug} and cannot be redacted; revise the "
                    "plan first so there is a readable current revision, then redact this one")
            entry = next((e for e in record["ledger"] if e["revision"] == revision), None)
            if entry is None:
                raise PlanStoreError(f"plan {slug} has no revision {revision}")
            path = self.plan_dir(slug) / entry["snapshot"]
            if "redacted" in entry:
                # Already marked. Finish the job rather than reporting success over a half-done
                # redaction, and take the operator's corrected reason if they supplied a new one —
                # a retry usually happens because something about the first attempt was wrong.
                self._unlink_body(path)
                if reason.strip() != entry["redacted"]["reason"]:
                    entry["redacted"]["reason"] = reason.strip()
                    core.validate(record, RECORD_SCHEMA)
                    self._write_json(self._record_path(slug), record)
                self._clear_intent(slug, entry)
                return record

            # THREE STEPS, and the order of all three is load-bearing. Neither of the two obvious
            # orderings is safe, because each leaves a crash window that LIES about what happened:
            #
            #   unlink then mark  -> a crash leaves a body genuinely gone with no marker, which
            #                        verify_chain correctly reports as loss. A deliberate redaction
            #                        reads as corruption.
            #   mark then unlink  -> a crash leaves the record saying "cleanly redacted" while the
            #                        body — the credential this was run to remove — is still on disk
            #                        and readable. The store vouches for a confidentiality it does
            #                        not have, which is the worse of the two by a distance.
            #
            # So a durable INTENT marker is written first, before anything is destroyed. Every crash
            # window then has a truthful reading: intent present and body present means "interrupted,
            # re-run"; intent present and body gone means "interrupted after deletion, re-run"; no
            # intent and a marked entry means done. The record is marked only AFTER the body is
            # actually gone, so the store can never report a redaction it has not completed.
            self._write_intent(slug, entry, reason.strip())
            self._unlink_body(path)
            entry["redacted"] = {"at": _now(), "reason": reason.strip()}
            core.validate(record, RECORD_SCHEMA)
            self._write_json(self._record_path(slug), record)
            self._clear_intent(slug, entry)
            return record

    def _intent_path(self, slug: str, entry: dict) -> Path:
        return self.plan_dir(slug) / REVISIONS_DIRNAME / f".redacting-{entry['revision']:06d}"

    def _write_intent(self, slug: str, entry: dict, reason: str) -> None:
        core.atomic_write(self._intent_path(slug, entry), reason + "\n",
                          durable=True, mode=FILE_MODE)

    def _clear_intent(self, slug: str, entry: dict) -> None:
        path = self._intent_path(slug, entry)
        if path.exists():
            path.unlink()
            core.fsync_dir(path.parent)

    def interrupted_redactions(self, slug: str) -> list:
        """Revisions whose redaction began and did not finish. A crash cannot hide one: the intent
        marker outlives it, so `verify_chain` can say 'interrupted, re-run redact' instead of either
        reporting corruption or quietly vouching that a secret is gone.

        A marker whose name does not parse is IGNORED rather than raising. The glob can catch things
        this store never wrote — a cloud-sync client's "conflicted copy" of a marker being the
        obvious one, on exactly the volumes `volume_warning` exists to complain about. An integrity
        check that crashes on a stray file takes the whole plan's diagnostics down with it, which is
        strictly worse than overlooking a file that is not a marker.
        """
        directory = self.plan_dir(slug) / REVISIONS_DIRNAME
        if not directory.is_dir():
            return []
        found = []
        for path in directory.glob(".redacting-*"):
            suffix = path.name[len(".redacting-"):]
            if suffix.isdigit():
                found.append(int(suffix))
        return sorted(found)

    @staticmethod
    def _unlink_body(path: Path) -> None:
        """Remove a redacted revision's body, durably. This is a LOGICAL deletion: the directory entry
        goes and the store can no longer reach the content, but the underlying blocks are not
        overwritten, and any backup or filesystem snapshot taken before now still holds it. If what
        was redacted was a credential, redacting is not the remedy — rotating it is."""
        if path.exists():
            path.unlink()
            core.fsync_dir(path.parent)


# The nine statuses a plan can be in. There is no `phase` field anywhere in the record: each of these
# is computed from evidence at the moment it is asked for, so a status can never disagree with the
# thing it claims to summarize. The list is here, next to the function, because it is the enumeration
# every reader means when it says "status".
STATUSES = ("draft", "awaiting-approval", "awaiting-review", "review-recorded",
            "sealed", "active", "complete", "retired", "abandoned")


def approval_is_stale(record: dict) -> bool:
    """Was this plan approved, never reviewed, and then changed?

    That combination means the review would run against something the operator never approved, so the
    approval no longer speaks for the head. Revising AFTER the review is a different thing entirely —
    that is folding fixes in, which the seal's proportional delta judgment covers — so a review at the
    approved revision keeps the approval live no matter how many fix revisions follow.
    """
    approval = record.get("approval")
    if not approval:
        return False
    if record.get("plan_review"):
        return False
    return record["current"]["plan_digest"] != approval["plan_digest"]


def exclusive_lock_for(library: "PlanLibrary", slug: str):
    """The plan's own lock, for a writer that lives outside PlanLibrary (today: `import`).

    Exposed rather than letting a caller assemble the lock path itself, so the lock a writer takes is
    provably the same one every other writer takes. A second, subtly-different lock path would look
    exactly like locking and serialise nothing.
    """
    return core.exclusive_lock(library._lock_path(slug))


def derived_status(record: dict, *, head_blockers: list | None = None) -> str:
    """The plan's lifecycle status, derived from evidence every time and stored nowhere.

    Ordered most-decided first, because the evidence accumulates: a sealed plan still has its
    approval and its review, so asking about the seal first is what makes the answer the LATEST true
    thing rather than the earliest.

    `head_blockers` is `plan_contract.seal_blockers` for the current revision, when the caller has
    already computed it. It separates the two draft states honestly: a plan whose head still carries
    unresolved decisions is a `draft`, while one with nothing left outstanding is `awaiting-approval`
    — waiting on the operator, not on itself. Omit it and the answer stays `draft`, which understates
    rather than overstates readiness.
    """
    closure = record.get("closure")
    if closure:
        return closure["state"]
    if record.get("build_binding"):
        return "active"
    if record.get("seal"):
        return "sealed"
    if record.get("plan_review"):
        return "review-recorded"
    if record.get("approval") and not approval_is_stale(record):
        return "awaiting-review"
    if head_blockers is not None and not head_blockers:
        return "awaiting-approval"
    return "draft"
