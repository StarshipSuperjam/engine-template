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
`<library>/<slug>/builds/<build-id>/snapshot.json`. The old `builds/snapshot.json` slot becomes a
permanent directory barrier at migration. Retirement retains evidence in the same private Build folder.

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
import os
import stat
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


def _locator_value(record, claim):
    return {'schema_version': 'build-locator.v1', 'plan_id': record['plan_id'],
            'ownership': claim_identity(claim), 'snapshot': claim['snapshot']}


def _private_locator(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BuildStateError(f'{path} must be an owner-only regular locator file')
    value = core.json_file(path)
    if set(value) != {'schema_version', 'plan_id', 'ownership', 'snapshot'} or value.get('schema_version') != 'build-locator.v1':
        raise BuildStateError(f'{path} is not a minimal Build locator; explicitly migrate any full legacy evidence')
    return value


def _check_locator_locked(record, locator, claim=None):
    if not locator:
        return
    path = Path(locator)
    if path.exists() or path.is_symlink():
        value = _private_locator(path)
        allowed = ([claim] if claim else []) + (record.get('build_lease') or {}).get('history', [])
        if value['plan_id'] != record['plan_id'] or not any(
                value == _locator_value(record, previous) for previous in allowed):
            raise BuildStateError('locator belongs to another Build; choose an unused path or recover its owner')


def _claim_path(library, slug, claim) -> Path:
    expected = builds_dir(library, slug) / claim['build_id'] / SNAPSHOT_FILENAME
    path = plan_store.contain(Path(claim['snapshot']), library.root, 'a claimed Build snapshot')
    if path != expected:
        raise BuildStateError('the recorded Build address is not its plan-owned address; repair the claim')
    return path


@contextlib.contextmanager
def ownership_lock(library, slug, *, snapshots=(), legacy_sources=()):
    """Program callers enter first; then plan locks, then snapshot locks in path order.

    Include the permanent legacy lock and every current/caller-held snapshot address. A stale
    caller can lock its former address but still fails identity comparison before any write.
    Transfers use the same order across both plans without recursively acquiring these locks.
    """
    with plan_store.exclusive_lock_for(library, slug):
        plan_store.ensure_dir(builds_dir(library, slug), within=library.root)
        record = library.read_record(slug)
        claim = (record.get('build_lease') or {}).get('current')
        paths = set(Path(p) for p in snapshots)
        if claim:
            paths.add(_claim_path(library, slug, claim))
        locks = {_legacy_lock(library, slug)}
        for path in paths:
            plan_store.ensure_dir(path.parent, within=library.root)
            locks.add(path.with_name(path.name + '.lock'))
        sources = set(Path(p).resolve() for p in legacy_sources)
        if claim and claim['state'] == 'preparing' and claim.get('legacy_source'):
            sources.add(Path(claim['legacy_source']))
        for source in sources:
            if not source.parent.is_dir():
                raise BuildStateError(f'legacy source folder is missing: {source.parent}; restore its evidence before retrying')
            locks.add(source.with_name(source.name + '.lock'))
        with contextlib.ExitStack() as held:
            for path in sorted(locks):
                held.enter_context(core.exclusive_lock(path))
            yield


def _assert_claim(record, identity, *, states=('active',)) -> dict:
    claim = (record.get('build_lease') or {}).get('current')
    if not identity or not claim or claim_identity(claim) != identity:
        raise BuildStateError('stale or missing Build identity; use the identity returned by bind or verified continuation')
    if claim['generation'] != record['build_lease']['generation']:
        raise BuildStateError('claim generation disagrees with its ledger; repair ownership before writing')
    if claim['state'] not in states:
        raise BuildStateError(f"Build is {claim['state']}; retry its recorded transition before writing")
    return claim


def _assert_snapshot_claim(record, claim, state):
    if (state.get('ownership') != claim_identity(claim)
            or state['plan']['plan_id'] != record['plan_id']
            or state['plan']['sealed_digest'] != claim['sealed_digest']
            or state['build']['repository'] != claim['repository']
            or state['build']['pr'] != claim['pull_request']
            or str(Path(state['build'].get('worktree', '')).resolve()) != claim['worktree']):
        raise BuildStateError('snapshot and plan claim disagree about Build ownership; preserve evidence and recover the transaction')


def _binding_projection(claim) -> dict:
    return {key: claim[key] for key in ('sealed_digest', 'build_plan_digest', 'at',
                                       'repository', 'pull_request')}


def _check_seal(record, state, *, legacy_revision=False) -> None:
    seal = record.get('seal') or {}
    if record.get('closure') or not seal:
        raise BuildStateError('a closed or unsealed plan cannot reserve a Build; preserve legacy evidence '
                              'and finish it on its original Engine, or select an open sealed successor')
    executed = seal.get('build_plan_digest')
    if legacy_revision and state['plan'].get('diverged_from_seal'):
        # The binding names the historical seal; only a connected chain of recorded
        # operator revisions can account for a different executed payload. Earlier adoption
        # entries may precede that seal, so begin at the current sealed payload.
        for change in state.get('plan_change_escalations', []):
            if change['reviewed_plan_digest'] == executed and change['operator_change'].strip():
                executed = change['plan_digest']
    if (record['plan_id'] != state['plan']['plan_id'] or
            seal.get('sealed_digest') != state['plan']['sealed_digest'] or
            executed != state['plan']['digest'] or
            record['current']['plan_digest'] != seal.get('sealed_digest')):
        raise BuildStateError('the Build does not match the current sealed plan')


def reserve_build(library, slug, state, *, consent=None, locator=None,
                  legacy_source=None, legacy_clients_stopped=False) -> dict:
    """Reserve before any snapshot write. A retry receives the same identity and consent.

    No timeout or rollback can steal this reservation. A caller changing any request field
    must explicitly retire it first. Legacy migration names and fingerprints its source;
    missing or ambiguous legacy evidence is never interpreted as an unused plan.
    """
    if legacy_source and not legacy_clients_stopped:
        raise BuildStateError('legacy migration requires --legacy-clients-stopped: pause affected sessions, '
                             'wait for old Engine commands to exit, update their worktrees, then migrate and resume')
    if locator:
        if Path(locator).is_symlink():
            raise BuildStateError('a Build locator must not be a symlink')
        locator = str(Path(locator).resolve())
        if Path(locator).is_relative_to(library.root):
            raise BuildStateError('omit --state for canonical evidence; an external locator must be outside the library')
    with ownership_lock(library, slug, legacy_sources=(legacy_source,) if legacy_source else ()):
        record = library.read_record(slug)
        _check_seal(record, state, legacy_revision=bool(legacy_source))
        wanted = {'repository': state['build']['repository'], 'pull_request': state['build']['pr'],
                  'sealed_digest': state['plan']['sealed_digest'],
                  'build_plan_digest': state['plan']['digest'],
                  'worktree': str(Path(state['build']['worktree']).resolve()), 'locator': locator}
        lease = record.get('build_lease')
        if lease and lease['current']:
            claim = lease['current']
            if claim['state'] == 'preparing' and claim.get('legacy_source') and not legacy_source:
                raise BuildStateError('legacy migration is preparing; retry state migrate with --legacy-clients-stopped')
            if any(claim[k] != v for k, v in wanted.items()) or claim['state'] not in ('preparing', 'active'):
                raise BuildStateError('this plan already has a different Build claim; resume it or explicitly supersede it')
            if legacy_source and claim.get('legacy_source') != str(Path(legacy_source).resolve()):
                raise BuildStateError('this migration is reserved for a different source')
            # Reflush the journal after a prior uncertain directory flush, before trusting it.
            library.write_build_record_locked(slug, record)
            return copy.deepcopy(claim)
        _check_locator_locked(record, locator)
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
            if (any(legacy.get(k) != wanted[k] for k in ('repository', 'pull_request', 'sealed_digest'))
                    or legacy.get('build_plan_digest') != record['seal']['build_plan_digest']):
                raise BuildStateError('legacy snapshot and plan binding disagree; repair ownership first')
            on_disk = core.json_file(source)
            if core.digest(core.forward_migrate(on_disk)) != core.digest(core.forward_migrate(state)):
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
        if claim['state'] == 'preparing' and claim.get('transfer'):
            raise BuildStateError('this preparation belongs to successor adoption; retry the recorded adoption')
        _check_seal(record, state, legacy_revision=bool(claim.get('legacy_source')))
        path = _claim_path(library, slug, claim)
        if claim['state'] == 'preparing' and claim.get('legacy_source'):
            _legacy_evidence_locked(library, slug, claim)
        plan_store.ensure_dir(path.parent, within=library.root)
        if path.exists():
            saved = core.json_file(path)
            core.validate(saved, schema(saved) if callable(schema) else schema)
            if saved.get('ownership') != identity:
                raise BuildStateError('reserved snapshot holds another Build; preserve it and repair the claim')
            _check_seal(record, saved, legacy_revision=bool(claim.get('legacy_source')))
        else:
            if claim['state'] == 'active':
                raise BuildStateError('active snapshot is missing; restore its evidence, never recreate it from bind input')
            saved = core.forward_migrate(state)
            saved['ownership'] = identity
            core.validate(saved, schema(saved) if callable(schema) else schema)
        _assert_snapshot_claim(record, claim, saved)
        _durable_json(path, saved)
        _cutover_locked(library, slug, claim)
        if claim['locator']:
            locator = Path(claim['locator'])
            with core.exclusive_lock(locator.with_name(locator.name + '.lock')):
                _check_locator_locked(record, claim['locator'], claim)
                _durable_json(locator, _locator_value(record, claim))
        claim['state'] = 'active'
        library.write_build_record_locked(slug, record)
        return saved


def _legacy_evidence_locked(library, slug, claim):
    """Recheck the reserved source under its sibling lock, including a resumed rename."""
    source = Path(claim['legacy_source'])
    evidence = source
    if source == _legacy_slot(library, slug) and not source.is_file():
        evidence = _claim_path(library, slug, claim).parent / 'legacy-original.json'
    if not evidence.is_file() or core.digest(core.json_file(evidence)) != claim['legacy_digest']:
        raise BuildStateError(f'legacy evidence changed or disappeared at {source}; preserve it and '
                             f'{claim["snapshot"]}. Restore the original matching reserved digest '
                             f'{claim["legacy_digest"]} at {evidence} from retained evidence or backup, then retry the same migration')
    return core.json_file(evidence)


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
        with ownership_lock(self.library, self.slug, snapshots=(self.path,)):
            yield

    def _check_write(self, state, *, creating):
        if creating:
            raise BuildStateError('production snapshots are created only by the binding transaction')
        claim = _assert_claim(self.library.read_record(self.slug), self.identity)
        if claim['snapshot'] != str(self.path) or state.get('ownership') != self.identity:
            raise BuildStateError('snapshot identity or address moved; this caller cannot write it')
        _assert_snapshot_claim(self.library.read_record(self.slug), claim, state)

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
    # Old fixtures and unupgraded records keep their old address. A retired upgraded plan has
    # no active snapshot: its old address is a directory barrier, never an available file slot.
    record = library._read_record_unchecked(slug)
    claim = (record.get('build_lease') or {}).get('current')
    return _claim_path(library, slug, claim) if claim else _legacy_slot(library, slug)


def store_for_plan(selector: str, schema, expected_revision: int | None = None,
                   *, library: plan_store.PlanLibrary | None = None, identity=None):
    """The durable store for one plan, selected the same way every other plan verb selects a plan."""
    library = library or plan_store.PlanLibrary()
    slug = library.resolve(selector)
    if library.read_record(slug).get('build_lease'):
        return ClaimedBuildStore(library, slug, schema, expected_revision, identity=identity)
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
        record = library._read_record_unchecked(slug)
        lease = record.get('build_lease')
        if lease:
            claim = lease['current']
            if claim and Path(claim['worktree']).resolve() == target:
                found.append((slug, _claim_path(library, slug, claim)))
            continue
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
                         *, library: plan_store.PlanLibrary | None = None,
                         identity=None) -> core.RevisionedStore:
    """The durable store for the Build running in `worktree`, or a refusal naming what was found.

    NOTHING auto-selects. Zero matches and two matches are different problems with different fixes,
    and picking one of two is how a session writes a second Build's evidence into the first Build's
    record.
    """
    library = library or plan_store.PlanLibrary()
    found = bound_snapshots(worktree, library=library)
    if len(found) == 1:
        if library._read_record_unchecked(found[0][0]).get('build_lease'):
            return ClaimedBuildStore(library, found[0][0], schema, expected_revision, identity=identity)
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


def resolve_explicit(path, schema, expected_revision=None, *, library=None, identity=None):
    """Resolve an external locator or an exact canonical file; full external evidence is legacy."""
    library = library or plan_store.PlanLibrary()
    path = Path(path)
    if path.is_symlink():
        raise BuildStateError('a Build locator must not be a symlink')
    path = path.resolve()
    if not path.is_file():
        raise BuildStateError(f'no snapshot or locator at {path}; retry bind or recover the recorded claim')
    value = core.json_file(path)
    if value.get('schema_version') == 'build-locator.v1':
        value = _private_locator(path)
        slug = library.resolve(value['plan_id'])
        record = library.read_record(slug)
        claim = _assert_claim(record, value['ownership'], states=('preparing', 'active'))
        if value != _locator_value(record, claim) or claim['locator'] != str(path):
            raise BuildStateError('stale locator address; recover the exact Build you intended')
        return ClaimedBuildStore(library, slug, schema, expected_revision, identity=identity)
    plan_id = (value.get('plan') or {}).get('plan_id')
    if not plan_id:
        raise BuildStateError('explicit state is neither a Build nor a locator')
    slug = library.resolve(plan_id)
    record = library.read_record(slug)
    if record.get('build_lease'):
        claim = _assert_claim(record, value.get('ownership'), states=('preparing', 'active'))
        if _claim_path(library, slug, claim) != path:
            raise BuildStateError('full evidence outside its canonical address must be explicitly migrated')
        return ClaimedBuildStore(library, slug, schema, expected_revision, identity=identity)
    return DurableBuildStore(path, schema, expected_revision)


def _match_completion(claim, completion):
    expected = {'build_id': claim['build_id'], 'generation': claim['generation'],
                'snapshot': claim['snapshot'], 'repository': claim['repository'],
                'pull_request': claim['pull_request'], 'sealed_digest': claim['sealed_digest']}
    if not isinstance(completion, dict) or completion.get('merged') is not True or any(
            completion.get(k) != v for k, v in expected.items()):
        raise BuildStateError('completion requires merged evidence matching the exact Build, address, generation, seal, repository and PR')


def retire_build(library, slug, identity, schema, *, reason, expected_revision,
                 terminal_state='superseded', completion=None, close_state=None):
    """Journal, archive, retire, then release this exact generation. Every cut is retryable."""
    if not reason or not reason.strip() or terminal_state not in ('superseded', 'abandoned', 'complete'):
        raise BuildStateError('retirement requires a reason and a supported terminal state')
    if close_state and close_state not in ('abandoned', 'retired', 'complete'):
        raise BuildStateError('unsupported plan closure')
    with ownership_lock(library, slug):
        record = library.read_record(slug)
        lease = record.get('build_lease')
        if not lease:
            raise BuildStateError('legacy retirement requires explicit state migrate first')
        if not lease['current']:
            matches = [c for c in lease['history'] if claim_identity(c) == identity]
            if not matches or matches[-1]['state'] != terminal_state or matches[-1]['reason'] != reason:
                raise BuildStateError('no matching retirement to retry; this identity cannot release another generation')
            if close_state and (record.get('closure') or {}).get('state') != close_state:
                raise BuildStateError('retirement retry must name the recorded plan closure')
            if terminal_state == 'complete':
                _match_completion(matches[-1], completion)
            archive = Path(matches[-1]['archive'])
            if not archive.is_file():
                raise BuildStateError('retired evidence is missing; recover the archive before relying on this retirement')
            archived = core.json_file(archive)
            revision = 0 if archived.get('unwritten_preparation') else archived.get('revision')
            if expected_revision is None or expected_revision != revision:
                raise BuildStateError('retirement retry must name the original snapshot revision')
            if revision:
                core.validate(archived, schema(archived) if callable(schema) else schema)
                _assert_snapshot_claim(record, matches[-1], archived)
            elif archived != {'ownership': identity, 'unwritten_preparation': True,
                              'reason': reason, 'terminal_state': terminal_state}:
                raise BuildStateError('preparation archive differs from its retirement record')
            library.write_build_record_locked(slug, record)
            return archive
        claim = _assert_claim(record, identity, states=('preparing', 'active', 'retiring'))
        if claim['state'] == 'preparing' and claim.get('transfer'):
            raise BuildStateError('successor adoption is preparing; retry the recorded adoption before retirement')
        path = _claim_path(library, slug, claim)
        if terminal_state == 'complete':
            if claim['state'] == 'preparing':
                raise BuildStateError('a preparing Build must finish binding before completion can be recorded')
            _match_completion(claim, completion)
        if claim['state'] == 'retiring':
            if claim['reason'] != reason or claim['terminal_state'] != terminal_state:
                raise BuildStateError('retirement is already preparing a different decision; retry its recorded reason and state')
            if 'close_state' in claim and claim['close_state'] != close_state:
                raise BuildStateError('retry the recorded plan closure action')
            archive = Path(claim['archive'])
        else:
            # The revision supplied by the caller fixes the archive name before any irreversible
            # step. The store compares it under its lock before preparing the journal.
            if expected_revision is None:
                raise BuildStateError('retirement requires the expected snapshot revision (0 for an unwritten preparation)')
            archive = path.with_name(f'retired-g{claim["generation"]:06d}-r{expected_revision:06d}.json')
        plan_store.ensure_dir(path.parent, within=library.root)
        archive = plan_store.contain(archive, library.root, 'a retirement archive')
        _flush_directory(path.parent.parent)
        if archive.parent != path.parent:
            raise BuildStateError('retirement archive moved outside its Build folder; recover the recorded address')
        if claim['state'] == 'preparing' and claim.get('legacy_source'):
            original = _legacy_evidence_locked(library, slug, claim)
            if not path.exists():
                saved = core.forward_migrate(original)
                saved['ownership'] = identity
                core.validate(saved, schema(saved) if callable(schema) else schema)
                _assert_snapshot_claim(record, claim, saved)
                _durable_json(path, saved)
            _cutover_locked(library, slug, claim)
        was_unwritten = claim['state'] == 'preparing' and not path.exists()

        def prepare(state):
            claim.update(state='retiring', terminal_state=terminal_state, reason=reason,
                         archive=str(archive), terminal_at=claim.get('terminal_at') or moment.utc_now(),
                         retirement_revision=expected_revision, close_state=close_state)
            library.write_build_record_locked(slug, record)

        if was_unwritten or (claim['state'] == 'retiring' and expected_revision == 0):
            if expected_revision != 0 or terminal_state == 'complete':
                raise BuildStateError('an unwritten preparation retires at expected revision 0 and cannot be completed')
            # No evidence is invented: this archive explicitly records that creation never landed.
            if path.exists():
                raise BuildStateError('snapshot appeared during preparation recovery; reread it before retirement')
            prepare(None)
            journal = {'ownership': identity, 'unwritten_preparation': True,
                       'reason': reason, 'terminal_state': terminal_state}
            if archive.exists() and core.json_file(archive) != journal:
                raise BuildStateError('preparation archive conflicts; preserve it and recover the transaction')
            _durable_json(archive, journal)
        else:
            store = DurableBuildStore(path, schema, expected_revision, library_root=library.root)
            store.retire_locked(archive, validate_owner=lambda state: _assert_snapshot_claim(record, claim, state),
                         prepare=prepare)
        claim['state'] = terminal_state
        lease['history'].append(copy.deepcopy(claim))
        lease['current'] = None
        record['build_binding'] = None
        if close_state:
            record['closure'] = {'state': close_state, 'at': claim['terminal_at'], 'reason': reason}
        library.write_build_record_locked(slug, record)
        return archive


# --- migration ----------------------------------------------------------------

# The one snapshot version a durable store accepts. Migration lands snapshots HERE and nowhere else,
# so a snapshot migrated today cannot be orphaned by the v1 schema deletion that follows it.
CURRENT_SCHEMA_VERSION = "build-state.v2"


def migrate(source: Path | str, selector: str, schema, *,
            library: plan_store.PlanLibrary | None = None, worktree: Path | str | None = None) -> Path:
    """Legacy fixture seam; production migration uses reserve_build and finish_binding.

    This retains the old copy-before-publication behavior for isolated compatibility tests and
    refuses claimed storage. Nothing touches the fixture snapshot until the migrated document
    has been built, validated against the schema it will be stored
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
    if core.json_file(library.plan_dir(slug) / 'record.json').get('build_lease'):
        raise BuildStateError('claimed storage requires the transactional state migrate command')
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



def supersede(library, slug, *, reason, identity=None, expected_revision=None, schema=None):
    """Explicitly retire exactly the caller's claim, preserving its evidence and every lock."""
    if schema is None:
        schema = Path(__file__).resolve().parent.parent / 'schemas' / 'build-state.v2.json'
    return retire_build(library, slug, identity, schema, reason=reason,
                        expected_revision=expected_revision, terminal_state='superseded')


def restore_handoff(library, slug, value, restored, schema, *, worktree, projection, locator=None):
    """Verify a cold export against the surviving canonical evidence; never create ownership.

    A bounded export cannot prove it is the newest copy after the canonical evidence is lost.
    That case requires recovery of the private snapshot, or explicit retirement, rather than
    silently recreating possibly stale evidence from a portable handoff.
    """
    identity = value.get('ownership')
    if not identity or not value.get('snapshot') or not value.get('snapshot_revision'):
        raise BuildStateError('legacy handoff lacks ownership and revision evidence; migrate and re-export from its owning Build')
    store = ClaimedBuildStore(library, slug, schema, value['snapshot_revision'], identity=identity)
    def restore(current):
        record = library.read_record(slug)
        claim = _assert_claim(record, identity)
        if value['snapshot'] != claim['snapshot']:
            raise BuildStateError('handoff names a former or different canonical address')
        if str(Path(worktree).resolve()) != claim['worktree']:
            raise BuildStateError('continue this Build in its recorded worktree; a handoff cannot silently move ownership')
        if locator and str(Path(locator).resolve()) not in (claim['snapshot'], claim['locator']):
            raise BuildStateError('handoff restore uses the existing canonical snapshot or its registered locator')
        bounded = {k: v for k, v in value.items() if k != 'snapshot'}
        if core.digest(projection(current)) != core.digest(bounded):
            raise BuildStateError('handoff evidence is stale or altered; re-export the current canonical Build')
        # Preserve private notes from the canonical snapshot. Only continuation evidence changes.
        for node_id, node in restored['work'].items():
            original = current['work'].get(node_id, {})
            if node.get('claim'):
                original['claim'] = dict(original['claim'], restored=node['claim'].get('restored', False))
            if node.get('integration'):
                original['integration'].update({key: node['integration'][key]
                    for key in ('restored', 'receipt') if key in node['integration']})
        current['validation'] = restored['validation']
        current['checkout_snapshot'] = None
        return current
    return store.mutate(restore)


def adoption_source(library, identity, schema):
    """Locate the caller's retained generation, including a half-finished adoption's archive."""
    if not identity:
        raise BuildStateError('adoption requires the caller-held Build ID and generation')
    matches = []
    for slug in library.slugs():
        record = library.read_record(slug)
        lease = record.get('build_lease') or {}
        claims = ([lease['current']] if lease.get('current') else []) + lease.get('history', [])
        for claim in claims:
            if claim_identity(claim) == identity:
                path = _claim_path(library, slug, claim)
                if not path.is_file() and claim.get('transfer') and claim.get('archive'):
                    path = plan_store.contain(Path(claim['archive']), library.root, 'an adoption archive')
                matches.append((slug, path))
    if len(matches) != 1:
        raise BuildStateError('the requested adoption generation is missing or ambiguous; recover its ownership record')
    slug, path = matches[0]
    return slug, DurableBuildStore(path, schema, library_root=library.root)


def adopt_build(library, predecessor_slug, successor_slug, identity, expected_revision,
                schema, *, change, consent):
    """Recoverable transfer across two plan records and one canonical successor snapshot.

    The predecessor journals the exact successor claim first, so every retry knows the same
    destination and generation. Both plan locks are held in plan-id order, then all permanent
    snapshot locks in path order. No network or recursively locked store call occurs here.
    """
    if predecessor_slug == successor_slug or expected_revision is None:
        raise BuildStateError('adoption requires a different successor and an expected source revision')
    slugs = sorted((predecessor_slug, successor_slug), key=lambda s: library.read_record(s)['plan_id'])
    with contextlib.ExitStack() as locks:
        for slug in slugs:
            locks.enter_context(plan_store.exclusive_lock_for(library, slug))
            plan_store.ensure_dir(builds_dir(library, slug), within=library.root)
        old_record, new_record = library.read_record(predecessor_slug), library.read_record(successor_slug)
        if old_record['plan_id'] not in ' '.join((new_record.get('intake') or {}).get('predecessors', [])):
            raise BuildStateError('the successor does not name this predecessor')
        old_lease = old_record.get('build_lease') or {}
        old_claim = old_lease.get('current')
        completed_source = not old_claim
        if completed_source:
            old_claim = next((c for c in old_lease.get('history', []) if claim_identity(c) == identity), None)
        if not old_claim or claim_identity(old_claim) != identity:
            raise BuildStateError('stale adoption identity; another generation owns the predecessor')
        if old_claim['state'] not in (('superseded',) if completed_source else ('active', 'transferring')):
            raise BuildStateError('the predecessor is not available for this adoption')
        if not completed_source:
            _assert_claim(old_record, identity, states=('active', 'transferring'))
        transfer = old_claim.get('transfer')
        if transfer and 'successor_plan_id' not in transfer:
            transfer = None  # receipt of an earlier incoming transfer, not an outgoing journal
        if completed_source and not transfer:
            raise BuildStateError('this generation retired without an adoption to resume')
        old_path = _claim_path(library, predecessor_slug, old_claim)
        target_lease = new_record.get('build_lease')
        if transfer:
            if transfer['successor_plan_id'] != new_record['plan_id'] or transfer['source_revision'] != expected_revision:
                raise BuildStateError('retry the exact successor and source revision recorded by the adoption')
            new_claim = copy.deepcopy(transfer['successor_claim'])
        else:
            if (target_lease and target_lease['current']) or new_record.get('build_binding'):
                raise BuildStateError('the successor already owns another Build; no transfer began')
            if _legacy_slot(library, successor_slug).is_file():
                raise BuildStateError('successor has legacy evidence; migrate or retire it before adoption')
            generation = max(old_claim['generation'], (target_lease or {}).get('generation', 0)) + 1
            seal = new_record.get('seal') or {}
            if new_record.get('closure') or not seal or seal['sealed_digest'] != new_record['current']['plan_digest']:
                raise BuildStateError('the successor is closed, unsealed or changed')
            new_claim = {k: old_claim[k] for k in ('build_id', 'repository', 'pull_request', 'worktree', 'locator')}
            new_claim.update(generation=generation, state='preparing', at=moment.utc_now(),
                sealed_digest=seal['sealed_digest'], build_plan_digest=seal['build_plan_digest'],
                snapshot=str(builds_dir(library, successor_slug) / old_claim['build_id'] / SNAPSHOT_FILENAME))
            new_claim['transfer'] = {'predecessor_plan_id': old_record['plan_id'],
                                     'predecessor_identity': identity, 'source_revision': expected_revision}
        new_path = _claim_path(library, successor_slug, new_claim)
        for path in (old_path, new_path):
            plan_store.ensure_dir(path.parent, within=library.root)
        lock_paths = {_legacy_lock(library, s) for s in slugs}
        lock_paths.update(p.with_name(p.name + '.lock') for p in (old_path, new_path))
        for path in sorted(lock_paths): locks.enter_context(core.exclusive_lock(path))
        existing_target = (new_record.get('build_lease') or {}).get('current')
        unavailable = (new_record.get('closure') or
            (existing_target and claim_identity(existing_target) != claim_identity(new_claim)) or
            (not existing_target and (target_lease or {}).get('generation', 0) >= new_claim['generation']))
        if unavailable:
            # A crash after the predecessor journal but before successor reservation can allow
            # another legitimate bind to win the successor. Restore the still-present source,
            # while preserving its exact evidence; never erase the competing claim.
            if transfer and not completed_source and old_path.is_file() and not new_path.exists():
                for key in ('transfer', 'reason', 'archive', 'terminal_at', 'terminal_state'):
                    old_claim.pop(key, None)
                old_claim['state'] = 'active'
                library.write_build_record_locked(predecessor_slug, old_record)
            raise BuildStateError('successor is no longer available; recover the recorded transfer or choose an available successor')
        if existing_target and existing_target['state'] not in ('preparing', 'active'):
            raise BuildStateError('successor is retiring; finish its recorded retirement, not an earlier adoption')
        if existing_target and any(existing_target.get(k) != v for k, v in new_claim.items() if k != 'state'):
            raise BuildStateError('successor reservation differs from the predecessor transfer journal')
        if completed_source and existing_target and existing_target['state'] == 'active':
            if not Path(old_claim['archive']).is_file():
                raise BuildStateError('predecessor archive is missing; recover its evidence before continuing')
            saved = core.json_file(new_path)
            core.validate(saved, schema(saved) if callable(schema) else schema)
            _assert_snapshot_claim(new_record, existing_target, saved)
            _check_seal(new_record, saved)
            library.write_build_record_locked(successor_slug, new_record)
            return saved
        source = old_path if old_path.is_file() else Path(old_claim.get('archive', ''))
        saved_old = core.json_file(source)
        core.validate(saved_old, schema(saved_old) if callable(schema) else schema)
        _assert_snapshot_claim(old_record, old_claim, saved_old)
        if saved_old['revision'] != expected_revision:
            raise BuildStateError('adoption source revision moved; reread the current Build before adopting')
        desired = copy.deepcopy(saved_old)
        change(desired)
        desired['ownership'] = claim_identity(new_claim)
        desired['revision'] += 1
        _check_seal(new_record, desired)
        _assert_snapshot_claim(new_record, new_claim, desired)
        core.validate(desired, schema(desired) if callable(schema) else schema)
        import plan_lifecycle
        prior = plan_lifecycle.missing_prior_consent(new_record, 'adopt')
        if prior: raise BuildStateError(prior)
        if not transfer:
            old_claim.update(state='transferring', terminal_state='superseded',
                reason=f"adopted sealed successor {new_record['plan_id']}", terminal_at=moment.utc_now(),
                archive=str(old_path.with_name(f'adopted-g{old_claim["generation"]:06d}-r{expected_revision:06d}.json')),
                transfer={'successor_plan_id': new_record['plan_id'], 'source_revision': expected_revision,
                          'successor_claim': copy.deepcopy(new_claim), 'consent': consent})
            transfer = old_claim['transfer']
        # Re-flush visible journal writes on retry before relying on their durability.
        library.write_build_record_locked(predecessor_slug, old_record)
        if not existing_target:
            new_record['build_lease'] = {'version': 1, 'generation': new_claim['generation'],
                'current': new_claim, 'history': (target_lease or {}).get('history', [])}
            new_record['build_binding'] = _binding_projection(new_claim)
            new_record.setdefault('consent', []).append(transfer['consent'])
            library.write_build_record_locked(successor_slug, new_record)
        else:
            new_claim = new_record['build_lease']['current']
        if new_path.exists():
            desired = core.json_file(new_path)
            core.validate(desired, schema(desired) if callable(schema) else schema)
            _assert_snapshot_claim(new_record, new_claim, desired)
            _check_seal(new_record, desired)
        _durable_json(new_path, desired)
        _cutover_locked(library, successor_slug, new_claim)
        if not completed_source:
            DurableBuildStore(old_path, schema, expected_revision, library_root=library.root).retire_locked(
                Path(old_claim['archive']), validate_owner=lambda s: _assert_snapshot_claim(old_record, old_claim, s),
                prepare=lambda s: None)
            old_claim['state'] = 'superseded'
            old_lease['history'].append(copy.deepcopy(old_claim)); old_lease['current'] = None
            old_record['build_binding'] = None
            old_record['closure'] = {'state': 'retired', 'at': old_claim['terminal_at'], 'reason': old_claim['reason']}
            library.write_build_record_locked(predecessor_slug, old_record)
        if new_claim['locator']:
            locator = Path(new_claim['locator'])
            with core.exclusive_lock(locator.with_name(locator.name + '.lock')):
                if locator.exists() or locator.is_symlink():
                    current_locator = _private_locator(locator)
                    allowed = (_locator_value(old_record, old_claim), _locator_value(new_record, new_claim))
                    if current_locator not in allowed:
                        raise BuildStateError('adoption locator was replaced by another owner; recover its registered address')
                _durable_json(locator, _locator_value(new_record, new_claim))
        new_claim['state'] = 'active'
        library.write_build_record_locked(successor_slug, new_record)
        return desired
