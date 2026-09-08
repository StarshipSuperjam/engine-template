#!/usr/bin/env python3
"""The durable Build snapshot: where a Build's evidence lives so a restart cannot take it.

WHY this exists. The Build coordinator's snapshot used to live in the OS temporary directory, on
purpose: one Build's current facts, expected to die with the machine, carrying no authority. The
purpose was sound and the consequence was not. A Build was killed mid-flight and every piece of
coordinator evidence it held — approval, receipts, findings, dispositions, progress — went with it,
and was reconstructed by hand. Evidence an operator cannot rely on surviving a restart is not
evidence; it is a note. So the snapshot becomes durable.

WHERE it lives, and why there. The plan is already durable, already local, already private, and
already addressed: it sits in the plan library, one folder per plan. A Build enters only through a
sealed plan, so the plan binding is the one name that identifies a Build without inventing a second
registry to hold it. The snapshot therefore lives beside the plan it executes, at
`<library>/<slug>/builds/snapshot.json`. No new store, no new address space, no new thing to garbage
collect: retire the plan and its Build evidence goes with it.

ONE snapshot per plan, and a second Build supersedes EXPLICITLY. A plan is per-Build by design — one
plan, one seal, one pull request — so a second Build of the same plan means something went wrong
with the first, and the operator is the one who knows which. `create` refuses when a snapshot is
already there; `supersede` is the verb that says "yes, replace it", and it keeps the displaced
snapshot beside the new one rather than deleting evidence on the operator's behalf. Two sessions
racing for the same plan meet the same exclusive lock and the same compare-and-swap the store has
always carried, so a lost update stays impossible rather than unlikely.

WHAT IT IS NOT. This store is durable; it is still not authoritative. The plan is the authority. A
snapshot that disagrees with the sealed plan loses, exactly as before.

RETENTION, stated before the evidence became durable rather than after. The snapshot holds a
reviewer's `--private-reference` notes, which are local-only by contract: never published to the
pull-request body, never read back by any verb. Making the snapshot durable changes how long they
persist, so the posture is stated plainly here. They are written owner-only (0600) inside an
owner-only folder (0700), inside a gitignored library that no path publishes; they live exactly as
long as the plan folder does and are deleted with it; and they are covered by the same
workstation-only trust model the plan library itself rests on. An operator who wants one
gone sooner deletes the plan folder — there is no separate place to hunt.

WHAT THIS MODULE DOES NOT REIMPLEMENT. The lock, the compare-and-swap, the atomic durable write, and
the schema validation have exactly one home in `build_coordinator_core`; the containment
chokepoint, the owner-only directory walk, and the unreliable-volume warnings have exactly one home
in `plan_store`. This module composes them. It does not restate them, because a re-expressed
guarantee is a guarantee with two versions, and the second one is always the weaker.
"""
from __future__ import annotations

import contextlib
import copy
import json
import uuid
from pathlib import Path

import build_coordinator_core as core
import moment
import plan_store

BuildStateError = core.CoordinatorError

BUILDS_DIRNAME = "builds"
SNAPSHOT_FILENAME = "snapshot.json"


def _durable_json(path: Path, value: dict) -> None:
    core.atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + '\n',
                      durable=True, mode=plan_store.FILE_MODE, require_directory_flush=True)


def _flush_directory(path: Path) -> None:
    if not core.fsync_dir(path):
        raise BuildStateError(f'could not flush {path}; retry the recorded Build transaction')


def _legacy_slot(library, slug) -> Path:
    return plan_store.contain(builds_dir(library, slug) / SNAPSHOT_FILENAME,
                              library.root, 'the legacy Build slot')


def _legacy_lock(library, slug) -> Path:
    return _legacy_slot(library, slug).with_name(SNAPSHOT_FILENAME + '.lock')


def claim_identity(claim: dict) -> dict:
    return {key: claim[key] for key in ('build_id', 'generation')}


def _claim_path(library, slug, claim) -> Path:
    expected = builds_dir(library, slug) / claim['build_id'] / SNAPSHOT_FILENAME
    path = plan_store.contain(Path(claim['snapshot']), library.root, 'a claimed Build snapshot')
    if path != expected:
        raise BuildStateError('the recorded Build address is not its plan-owned address; repair the claim')
    return path


@contextlib.contextmanager
def ownership_lock(library, slug):
    """Plan, old permanent lock, then per-Build snapshot: one order, never recursive.

    Program callers enter with their program lock held. Transfers acquire both plan locks in
    stable plan-id order before entering either snapshot lock. The old lock is retained even
    after cutover so a writer waiting on its inode cannot bypass the transition.
    """
    with plan_store.exclusive_lock_for(library, slug):
        plan_store.ensure_dir(builds_dir(library, slug), within=library.root)
        with core.exclusive_lock(_legacy_lock(library, slug)):
            yield


def _assert_claim(record, identity, *, states=('active',)) -> dict:
    claim = (record.get('build_lease') or {}).get('current')
    if not identity or not claim or claim_identity(claim) != identity:
        raise BuildStateError('stale or missing Build identity; use the identity returned by bind or verified continuation')
    if claim['state'] not in states:
        raise BuildStateError(f"Build is {claim['state']}; retry its recorded transition before writing")
    return claim


def _binding_projection(claim) -> dict:
    return {key: claim[key] for key in ('sealed_digest', 'build_plan_digest', 'at',
                                       'repository', 'pull_request')}


def _check_seal(record, state) -> None:
    seal = record.get('seal') or {}
    if record.get('closure') or not seal:
        raise BuildStateError('a closed or unsealed plan cannot reserve a Build')
    if (record['plan_id'] != state['plan']['plan_id'] or
            seal.get('sealed_digest') != state['plan']['sealed_digest'] or
            seal.get('build_plan_digest') != state['plan']['digest'] or
            record['current']['plan_digest'] != seal.get('sealed_digest')):
        raise BuildStateError('the Build does not match the current sealed plan')


def reserve_build(library, slug, state, *, consent=None, locator=None,
                  legacy_source=None) -> dict:
    """Reserve before any snapshot write. A retry receives the same identity and consent.

    No timeout or rollback can steal this reservation. A caller changing any request field
    must explicitly retire it first. Legacy migration names and fingerprints its source;
    missing or ambiguous legacy evidence is never interpreted as an unused plan.
    """
    locator = str(Path(locator).resolve()) if locator else None
    with ownership_lock(library, slug):
        record = library.read_record(slug)
        _check_seal(record, state)
        wanted = {'repository': state['build']['repository'], 'pull_request': state['build']['pr'],
                  'sealed_digest': state['plan']['sealed_digest'],
                  'build_plan_digest': state['plan']['digest'],
                  'worktree': str(Path(state['build']['worktree']).resolve()), 'locator': locator}
        lease = record.get('build_lease')
        if lease and lease['current']:
            claim = lease['current']
            if any(claim[k] != v for k, v in wanted.items()) or claim['state'] not in ('preparing', 'active'):
                raise BuildStateError('this plan already has a different Build claim; resume it or explicitly supersede it')
            if legacy_source and claim.get('legacy_source') != str(Path(legacy_source).resolve()):
                raise BuildStateError('this migration is reserved for a different source')
            # Reflush the journal after a prior uncertain directory flush, before trusting it.
            library.write_build_record_locked(slug, record)
            return copy.deepcopy(claim)
        old = _legacy_slot(library, slug)
        legacy = record.get('build_binding')
        if not lease and (legacy or old.is_file()) and not legacy_source:
            raise BuildStateError('legacy Build evidence requires explicit state migrate; nothing was reserved')
        source_digest = None
        if legacy_source:
            source = Path(legacy_source).resolve()
            if not source.is_file() or not legacy:
                raise BuildStateError('legacy binding or source is missing; preserve the evidence and repair ownership first')
            if old.is_file() and old.resolve() != source:
                raise BuildStateError('both canonical and external legacy snapshots exist; reconcile ambiguous evidence first')
            if any(legacy.get(k) != wanted[k] for k in ('repository', 'pull_request', 'sealed_digest', 'build_plan_digest')):
                raise BuildStateError('legacy snapshot and plan binding disagree; repair ownership first')
            on_disk = core.json_file(source)
            if core.digest(on_disk) != core.digest(state):
                raise BuildStateError('legacy source changed; reread it before migration')
            source_digest = core.digest(on_disk)
        generation = lease['generation'] + 1 if lease else 1
        claim = dict(wanted, build_id='bld_' + uuid.uuid4().hex, generation=generation,
                     state='preparing', at=moment.utc_now())
        claim['snapshot'] = str(builds_dir(library, slug) / claim['build_id'] / SNAPSHOT_FILENAME)
        if legacy_source:
            claim.update(legacy_source=str(Path(legacy_source).resolve()), legacy_digest=source_digest)
        record['build_lease'] = {'version': 1, 'generation': generation, 'current': claim,
                                 'history': lease['history'] if lease else []}
        record['build_binding'] = _binding_projection(claim)
        if consent:
            import plan_lifecycle
            prior = plan_lifecycle.missing_prior_consent(record, consent['gate'])
            if prior:
                raise BuildStateError(prior)
            record.setdefault('consent', []).append(consent)
        library.write_build_record_locked(slug, record)
        return copy.deepcopy(claim)


def _cutover_locked(library, slug, claim) -> None:
    """Replace the old file slot with a directory that old file operations cannot replace.

    Canonical legacy evidence is renamed, never unlinked, after its copy is durable. Both names
    are on the same filesystem. A retry distinguishes our rename from someone deleting the
    old source by the preserved original; it never guesses that an absent source is success.
    """
    old = _legacy_slot(library, slug)
    target = _claim_path(library, slug, claim)
    preserved = target.parent / 'legacy-original.json'
    if old.is_symlink():
        raise BuildStateError('legacy slot is a symlink; repair it before cutover')
    if old.is_file():
        if claim.get('legacy_source') != str(old) or core.digest(core.json_file(old)) != claim.get('legacy_digest'):
            raise BuildStateError('unexpected legacy evidence at cutover; preserve it and reconcile ownership')
        if preserved.exists():
            raise BuildStateError('legacy source reappeared after cutover; preserve both copies and reconcile')
        old.rename(preserved)
        _flush_directory(target.parent)
        _flush_directory(old.parent)
    elif (claim.get('legacy_source') == str(old) and not old.is_dir()
          and not preserved.is_file()):
        raise BuildStateError('legacy source disappeared before cutover; recover the original before retrying')
    old.mkdir(mode=plan_store.DIR_MODE, exist_ok=True)
    _flush_directory(old.parent)


def finish_binding(library, slug, identity, state, schema) -> dict:
    """Converge preparing -> durable snapshot -> compatibility barrier -> active.

    A snapshot already written by this transaction wins over retry input, preserving all
    evidence. A different identity or corrupt snapshot refuses without replacing anything.
    """
    with ownership_lock(library, slug):
        record = library.read_record(slug)
        claim = _assert_claim(record, identity, states=('preparing', 'active'))
        _check_seal(record, state)
        path = _claim_path(library, slug, claim)
        plan_store.ensure_dir(path.parent, within=library.root)
        with core.exclusive_lock(path.with_name(path.name + '.lock')):
            if path.exists():
                saved = core.json_file(path)
                core.validate(saved, schema(saved) if callable(schema) else schema)
                if saved.get('ownership') != identity:
                    raise BuildStateError('reserved snapshot holds another Build; preserve it and repair the claim')
                _check_seal(record, saved)
            else:
                if claim['state'] == 'active':
                    raise BuildStateError('active snapshot is missing; restore its evidence, never recreate it from bind input')
                saved = copy.deepcopy(state)
                saved['ownership'] = identity
                core.validate(saved, schema(saved) if callable(schema) else schema)
            _durable_json(path, saved)
            _cutover_locked(library, slug, claim)
            claim['state'] = 'active'
            library.write_build_record_locked(slug, record)
            return saved


class ClaimedBuildStore(core.RevisionedStore):
    """Production mutations require a caller-held identity under the plan and snapshot locks."""

    durable = True
    require_directory_flush = True
    file_mode = plan_store.FILE_MODE
    what = 'claimed Build snapshot'

    def __init__(self, library, slug, schema, expected_revision=None, *, identity=None):
        self.library, self.slug, self.identity = library, slug, identity
        claim = (library.read_record(slug).get('build_lease') or {}).get('current')
        if not claim:
            raise BuildStateError('no current Build claim; bind the sealed plan first')
        super().__init__(str(_claim_path(library, slug, claim)), schema, expected_revision)

    @contextlib.contextmanager
    def _locked(self):
        with ownership_lock(self.library, self.slug):
            with core.exclusive_lock(self.lock):
                yield

    def _check_write(self, state, *, creating):
        if creating:
            raise BuildStateError('production snapshots are created only by the binding transaction')
        claim = _assert_claim(self.library.read_record(self.slug), self.identity)
        if claim['snapshot'] != str(self.path) or state.get('ownership') != self.identity:
            raise BuildStateError('snapshot identity or address moved; this caller cannot write it')

class DurableBuildStore(core.RevisionedStore):
    """A Build snapshot that survives a forced restart. A PEER of `core.StateStore`, not a subclass.

    Peer rather than subclass on purpose. The two stores differ in the only things that matter here —
    where the file lives, how durably it is written, and who may read it — and a subclass that
    overrode all three would be inheriting nothing but a name, while inviting a later change to one
    store to silently become a change to both. What they genuinely share is the revisioned-store
    discipline, and that is inherited from where it is single-homed.
    """

    durable = True
    file_mode = plan_store.FILE_MODE
    what = "durable Build snapshot"
    missing_remedy = "use 'plan bind' first"
    stale_remedy = "reload status"

    def __init__(self, path: Path | str, schema, expected_revision: int | None = None,
                 *, library_root: Path | None = None):
        super().__init__(str(path), schema, expected_revision)
        self.library_root = Path(library_root).resolve() if library_root is not None else None
        if self.library_root is not None:
            plan_store.contain(self.path, self.library_root, "a Build snapshot")

    def create(self, state: dict) -> None:
        """Create the snapshot, owner-only all the way down.

        The directory walk comes first and is not optional: `atomic_write` creates a missing parent
        with the process umask, which is how a 0700 plan folder ends up with a 0755 `builds/`
        directory inside it and the operator's evidence readable by every account on the machine.
        """
        if self.library_root is not None:
            plan_store.ensure_dir(self.path.parent, within=self.library_root)
        else:
            plan_store.ensure_dir(self.path.parent)
        super().create(state)


# --- addressing ---------------------------------------------------------------

def builds_dir(library: plan_store.PlanLibrary, slug: str) -> Path:
    return plan_store.contain(library.plan_dir(slug) / BUILDS_DIRNAME, library.root, "a Build folder")


def snapshot_path(library: plan_store.PlanLibrary, slug: str) -> Path:
    """The one durable snapshot address for one plan.

    Routed through the containment chokepoint because `slug` reaches here from a record the store did
    not mint — and `Path("/library") / "/etc/passwd"` is `/etc/passwd`, an absolute component
    silently discarding everything to its left.
    """
    return plan_store.contain(builds_dir(library, slug) / SNAPSHOT_FILENAME, library.root,
                              "a Build snapshot")


def store_for_plan(selector: str, schema, expected_revision: int | None = None,
                   *, library: plan_store.PlanLibrary | None = None) -> DurableBuildStore:
    """The durable store for one plan, selected the same way every other plan verb selects a plan."""
    library = library or plan_store.PlanLibrary()
    slug = library.resolve(selector)
    return DurableBuildStore(snapshot_path(library, slug), schema, expected_revision,
                             library_root=library.root)


def bound_snapshots(worktree: Path | str, *, library: plan_store.PlanLibrary | None = None) -> list[tuple[str, Path]]:
    """Every durable snapshot in the library whose Build runs in `worktree`, as (slug, path) pairs.

    A Build occupies one worktree, and the worktree is what a resuming session actually has in its
    hands — it is standing in it. So that, rather than a pointer file the restart could have been
    holding when it died, is what addresses the snapshot on the way back.
    """
    library = library or plan_store.PlanLibrary()
    target = Path(worktree).resolve()
    found: list[tuple[str, Path]] = []
    for slug in library.slugs():
        path = snapshot_path(library, slug)
        if not path.is_file():
            continue
        try:
            state = core.json_file(path)
        except core.CoordinatorError:
            # A snapshot too damaged to parse must not make every other Build unresolvable. It is
            # skipped here and reported by name where a reader can act on it, never silently healed.
            continue
        recorded = (state.get("build") or {}).get("worktree")
        # Resolved on both sides. A macOS temp path, a symlinked home, and a `/private` prefix are
        # the same worktree spelled three ways, and a string comparison would report the Build as
        # missing while it sits right there.
        if recorded and Path(recorded).resolve() == target:
            found.append((slug, path))
    return sorted(found)


def resolve_for_worktree(worktree: Path | str, schema, expected_revision: int | None = None,
                         *, library: plan_store.PlanLibrary | None = None) -> DurableBuildStore:
    """The durable store for the Build running in `worktree`, or a refusal naming what was found.

    NOTHING auto-selects. Zero matches and two matches are different problems with different fixes,
    and picking one of two is how a session writes a second Build's evidence into the first Build's
    record.
    """
    library = library or plan_store.PlanLibrary()
    found = bound_snapshots(worktree, library=library)
    if len(found) == 1:
        return DurableBuildStore(found[0][1], schema, expected_revision, library_root=library.root)
    if not found:
        raise BuildStateError(
            f"no Build snapshot is bound to this worktree ({Path(worktree).resolve()}). Start the "
            "Build with 'plan bind', or name an explicit snapshot with --state. If a Build did run "
            "here, its snapshot lives with its plan — 'project_manager.py list' shows the library.")
    raise BuildStateError(
        f"{len(found)} Build snapshots name this worktree ({Path(worktree).resolve()}): "
        + ", ".join(slug for slug, _ in found)
        + ". Two Builds cannot share a worktree, so one of these is stale. Name the one you mean "
          "with --state, and supersede or retire the other.")


# --- migration ----------------------------------------------------------------

# The one snapshot version a durable store accepts. Migration lands snapshots HERE and nowhere else,
# so a snapshot migrated today cannot be orphaned by the v1 schema deletion that follows it.
CURRENT_SCHEMA_VERSION = "build-state.v2"


def migrate(source: Path | str, selector: str, schema, *,
            library: plan_store.PlanLibrary | None = None, worktree: Path | str | None = None) -> Path:
    """Move one OS-temp snapshot into the durable library, or refuse with a remedy.

    PROVEN ON A COPY FIRST, and that ordering is the whole safety argument. This function is the one
    place in the engine that can destroy live Build evidence, so nothing touches the real snapshot
    until the migrated document has been built, validated against the schema it will be stored
    under, and written to a scratch file inside the destination folder. Only then does the atomic
    replace happen, and only then is the source left behind — left, never deleted, because a
    migration that removes its own source has no way back if the operator disagrees with the result.

    A build-state.v1 snapshot is REFUSED rather than converted, and the refusal names why. v1 is a
    linear Build with no work ledger, and the current schema derives completion from integration
    evidence that a v1 snapshot never recorded. Fabricating an empty ledger would produce a document
    that validates and then wedges: every completed item would read as completed without the
    evidence that earns it. So an in-flight v1 Build finishes on the engine it started on.
    """
    library = library or plan_store.PlanLibrary()
    source_path = Path(source).resolve()
    if not source_path.is_file():
        raise BuildStateError(f"no snapshot to migrate at {source_path}")
    state = core.json_file(source_path)
    version = state.get("schema_version")
    if version != CURRENT_SCHEMA_VERSION:
        raise BuildStateError(
            f"{source_path} is a {version or 'versionless'} Build snapshot, and the durable store "
            f"holds {CURRENT_SCHEMA_VERSION} only. It is not converted, because a {version} snapshot "
            "carries no work ledger and the current schema derives completion from one — an invented "
            "ledger would validate and then wedge the Build. Finish this Build on the engine it "
            "started on, or abandon it and re-bind its sealed plan for a fresh Build. The file is "
            "untouched.")
    slug = library.resolve(selector)
    destination = snapshot_path(library, slug)
    if destination.exists():
        raise BuildStateError(
            f"{slug} already holds a durable Build snapshot at {destination}. Migrating over it would "
            "destroy the evidence already there; supersede it explicitly if that is what you mean.")
    if worktree is not None:
        state.setdefault("build", {})["worktree"] = str(Path(worktree).resolve())
    # Through the same forward migration the stores apply on load. This verb exists to move a document
    # written by an older engine forward, so it is the last place that should refuse one for carrying a
    # field that engine declared and this one retired.
    state = core.forward_migrate(state)
    core.validate(state, schema(state) if callable(schema) else schema)
    plan_store.ensure_dir(destination.parent, within=library.root)
    # The rehearsal: the exact bytes, written to a scratch name in the destination folder, so a full
    # disk or a refused durable flush fails HERE, with the source still the only copy that matters.
    rehearsal = destination.with_name(destination.name + ".migrating")
    core.atomic_write(rehearsal, json.dumps(state, indent=2, sort_keys=True) + "\n",
                      durable=True, mode=plan_store.FILE_MODE)
    rehearsal.replace(destination)
    return destination



def supersede(library: plan_store.PlanLibrary, slug: str, *, reason: str) -> Path | None:
    """Clear a confirmed-stale binding: set the current snapshot aside so a fresh Build of the same
    plan may start. Never silent.

    This is deliberately NOT the resume path — a genuine continuation keeps its worktree and
    re-verifies the binding in place, and never comes here. Nor is it needed to start a Build of some
    OTHER plan: each plan gets its own snapshot, so a different plan just binds fresh. But this plan
    cannot bind fresh in a different worktree — snapshots are keyed by plan, not worktree, so while this
    snapshot exists a re-bind of the same plan is refused. Superseding clears that one snapshot so its
    slot is free again. Once cleared, the plan no longer answers `bound_snapshots` (the live snapshot is
    gone), so a resuming session sees no live work for it. Superseding neither completes the plan nor
    touches the PR.

    The displaced snapshot is MOVED, not removed: it becomes `superseded-<revision>.json` beside the
    new one, byte-for-byte as it stood, with the reason recorded in a sibling `.reason.json`. An
    operator superseding a Build usually does so because something went wrong, which is precisely
    when the evidence of what went wrong is worth keeping — and keeping the snapshot itself
    unaltered is what lets it still be read as the schema-valid document it is.
    """
    current = snapshot_path(library, slug)
    if not current.is_file():
        return None
    state = core.json_file(current)
    revision = state.get("revision", 0)
    retired = current.with_name(f"superseded-{revision:06d}.json")
    if retired.exists():
        raise BuildStateError(
            f"{retired} already exists, so superseding again would overwrite a snapshot already set "
            "aside. Move or delete it first — this store does not silently destroy evidence.")
    core.atomic_write(retired, json.dumps(state, indent=2, sort_keys=True) + "\n",
                      durable=True, mode=plan_store.FILE_MODE)
    core.atomic_write(retired.with_suffix(".reason.json"),
                      json.dumps({"at": moment.utc_now(), "reason": reason,
                                  "superseded_revision": revision}, indent=2, sort_keys=True) + "\n",
                      durable=True, mode=plan_store.FILE_MODE)
    current.unlink()
    lock = current.with_name(current.name + ".lock")
    if lock.exists():
        lock.unlink()
    return retired
