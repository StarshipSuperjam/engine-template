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
workstation-only trust model the plan library itself rests on (eADR-0044). An operator who wants one
gone sooner deletes the plan folder — there is no separate place to hunt.

WHAT THIS MODULE DOES NOT REIMPLEMENT. The lock, the compare-and-swap, the atomic durable write, and
the schema validation have exactly one home in `build_coordinator_core`; the containment
chokepoint, the owner-only directory walk, and the unreliable-volume warnings have exactly one home
in `plan_store`. This module composes them. It does not restate them, because a re-expressed
guarantee is a guarantee with two versions, and the second one is always the weaker.
"""
from __future__ import annotations

import json
from pathlib import Path

import build_coordinator_core as core
import moment
import plan_store

BuildStateError = core.CoordinatorError

BUILDS_DIRNAME = "builds"
SNAPSHOT_FILENAME = "snapshot.json"


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
    """Set the current snapshot aside so a second Build of the same plan may start. Never silent.

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
