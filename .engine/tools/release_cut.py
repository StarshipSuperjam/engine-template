#!/usr/bin/env python3
"""Release-cut classifier + writer — the produce side of the engine's version line
(wbs/release-process.md; the complement to the consume side the module manager owns).

The engine and every module carry a version (`.engine/engine.json` `engine_release` + the
`packages` map; each `.engine/modules/<id>/manifest.json` `version`). The module manager
*consumes* a published release (fetch + overlay + migrate); nothing yet *produces* one — this tool
is that missing half: it decides the next version from what changed since the last release, and it
records the chosen versions into the manifests. It does NOT tag, open a PR, or publish a Release —
that GitHub-facing plumbing is later slices; this is the version-decision core they drive.

Two subcommands, split so consent attaches to a proposal the writer cannot silently drift from:

  propose  — read-only. Resolve the last release baseline from the engine's HOME repo (the #369
             `home_repository` coordinate — the same source the updater fetches from, so producer
             and consumer agree on what "a release" is), diff since it, and author:
               * the mechanical bump FLOOR (release-process §3): a module ADDED => engine >= minor;
                 a module REMOVED => engine >= major; a new `migrations` entry in a package => that
                 package >= minor; the engine version = the MAX implied bump;
               * a plain-language CHANGE INVENTORY (what changed since the last release), so the
                 maintainer can catch a wrong floor or a missing signal;
               * where a contract/seam/interface/wiring surface changed, an AI-authored plain-language
                 IMPACT statement, with the break/no-break behavioral demonstration marked present
                 (a correlate exists) or "no correlate — release consciously sub-bar, named" (the
                 §6/§7/D-152 legible gate path; the acceptance-benchmark instrument is not built yet,
                 and its absence is stated, never faked).
             It writes nothing.

  apply    — the writer. Records the chosen engine + per-package versions into the manifests, with:
               * RAISE-ONLY enforcement (release-process §3): every target is compared against the
                 current on-disk version and a value not strictly greater is REFUSED loudly (the dev
                 sentinel `0.0.0-dev` sorts below any real release); nothing is ever silently lowered;
               * an ATOMIC staged write: every touched file is written to a temp sibling and
                 schema-re-validated (plus a packages<->manifest equality check) BEFORE any swap, then
                 all swapped together; a validation failure changes nothing, and a write error mid-swap
                 rolls back the files already written and reports loudly (no split-brain — the §6
                 "atomic-or-loudly-incomplete" invariant; the reviewed-PR merge is the real
                 all-or-nothing unit, this bounds the on-disk window);
               * shape preservation: manifests are loaded, mutated in place, and rewritten with the
                 house 2-space+newline writer, so only version VALUES change — the `home_repository`
                 line stays byte-identical and the tightened weakening_guard (D-281/D-282) is not
                 tripped by a version-only cut.

Read-only discovery + the release-ref/fetch/manifest-write helpers are reused from module_coherence
and module_manager (one present-set reader, one release-ref resolver — no drift).

A third subcommand renders the maintainer's evidence:

  pr-body  — read-only. Render the release pull request's body from a `propose` JSON + an `apply` result
             JSON: the change inventory, the versions actually recorded, a legible §6 gate-path line
             (passed / consciously-sub-bar / errored — the three read as distinct), and the confirm/raise/
             reject guidance that makes the PR review the §3 consent act. Authored HERE, never in workflow
             bash, so the gate-path legibility has one home.

CLI:
  python tools/release_cut.py propose [--json] [--baseline-tree DIR]
  python tools/release_cut.py apply --engine VER [--all VER] [--package id=ver ...] \
                                    [--proposal FILE] [--dry-run] [--json]
  python tools/release_cut.py pr-body --proposal FILE --applied FILE [--gate-state STATE]
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import sys
import tempfile

import jsonschema

import validate
import module_coherence
import module_manager

SENTINEL = "0.0.0-dev"
ENGINE_SCHEMA = os.path.join(validate.SCHEMAS_DIR, "engine.v1.json")
MODULE_SCHEMA = os.path.join(validate.SCHEMAS_DIR, "module.v1.json")


# --------------------------------------------------------------------------- version ordering
# Strict MAJOR.MINOR.PATCH with an optional pre-release suffix — the SAME grammar the module.v1 schema
# now enforces on the manifest `version` field (#402 U07a), so the writer here and the schema gate at CI
# cannot bless different shapes. Kept in sync deliberately: the schema is the harder gate, and this writer
# check catches a nonsense version before it ever reaches a release manifest.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")


def _valid_version(v: str) -> bool:
    """A MAJOR.MINOR.PATCH version, optionally with a pre-release suffix (1.2.0, 1.0.0-rc1, 0.0.0-dev).
    The manifest schema requires this exact shape (module.v1 version pattern), and it is enforced HERE at
    the writer too so a nonsense version (a typo, a shell fragment, a 1- or 2-component number) never
    reaches a release manifest and never fools the digit-only ordering."""
    return bool(_VERSION_RE.match(v or ""))


def _is_prerelease(v: str) -> bool:
    """A version carrying a pre-release suffix (a '-', e.g. the `0.0.0-dev` sentinel or `1.0.0-rc1`)."""
    return "-" in (v or "")


def _release_tuple(v: str) -> tuple:
    """The numeric release identity of a version, the pre-release suffix REMOVED before tupling —
    otherwise `validate._ver_tuple` folds `-rc1`'s digits into the tuple and a pre-release sorts
    ABOVE its own release (1.0.0-rc1 -> (1,0,0,1) > (1,0,0))."""
    return validate._ver_tuple((v or "").split("-", 1)[0])


def _strictly_greater(new: str, cur: str) -> bool:
    """True iff `new` is a strictly higher RELEASE than `cur`. Compared on the release numbers with the
    pre-release stripped; on equal numbers a real release outranks a pre-release of the same numbers
    (so `0.1.0` > `0.0.0-dev` and `1.0.0` > `1.0.0-rc1`), and a pre-release is never taken as greater
    than another version of the same numbers (conservative — a pre-release progression like rc1 -> rc2
    is refused rather than risk a silent mis-order; raise-only never lowers)."""
    nt, ct = _release_tuple(new), _release_tuple(cur)
    if nt != ct:
        return nt > ct
    return _is_prerelease(cur) and not _is_prerelease(new)


# --------------------------------------------------------------------------- baseline resolution
class Baseline:
    """The last-release baseline for the diff. `ref` is None in FIRST-CUT mode (the home has no
    published release yet — the current reality, and the state the v1/beta cut is made from)."""
    def __init__(self, ref, first_cut: bool, note: str):
        self.ref = ref
        self.first_cut = first_cut
        self.note = note


def resolve_baseline() -> Baseline:
    """The last released tag from the engine's HOME repo (#369 `home_repository`), or a first-cut
    baseline when the home has no release yet. A TRANSPORT failure (offline/DNS) is not a first cut —
    it is unknowable, and we say so rather than guess an empty baseline."""
    home = module_manager._home_repository()
    if not home:
        return Baseline(None, True, "no home repository is recorded, so there is no prior release to "
                                    "diff against — treating this as the first cut.")
    try:
        ref = module_manager._resolve_release_ref(None, repo=home)
        return Baseline(ref, False, f"diffing since the last release {ref} of {home}.")
    except Exception as exc:  # _resolve_release_ref raises RuntimeError subclasses (Exception), never BaseException
        if module_manager._release_is_missing(exc):
            return Baseline(None, True, f"{home} has no published release yet — this is the first cut.")
        raise


def _baseline_tree_for(baseline: Baseline, injected: str | None) -> tuple:
    """The baseline release tree to diff against, and a temp dir to clean up (or None). An INJECTED local
    tree always wins (tests and an explicit `--baseline-tree` pass one, so `propose` never reaches the
    network in a test). Otherwise, in diff mode, the tree is fetched from the home's release tarball at the
    resolved ref via the module_manager network boundary — a TESTED Python caller (like the other release
    helpers), never a private symbol reached from workflow bash. First-cut mode diffs nothing, so no tree."""
    if injected:
        return injected, None
    if baseline.first_cut:
        return None, None
    home = module_manager._home_repository()
    tmp = tempfile.mkdtemp(prefix="release-baseline-")
    try:
        tree = module_manager._fetch_release_tree(baseline.ref, tmp, repo=home)
    except BaseException:
        # the fetch can raise (transport failure, non-200, a malformed tarball) BEFORE the temp dir is
        # returned to the caller's finally — clean it up here so a failed fetch never strands a temp dir
        # (the caller only removes what it receives back).
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tree, tmp


# --------------------------------------------------------------------------- present / baseline sets
def _present_modules() -> dict:
    """id -> manifest for every present module (the live tree)."""
    out = {}
    for _rel, man in module_coherence.discover_manifests():
        mid = man.get("id")
        if mid:
            out[mid] = man
    return out


def _modules_in_tree(tree_root: str) -> dict:
    """id -> manifest for every module manifest under a fetched/injected release TREE root (the
    baseline side of the diff — `discover_manifests` only reads the live tree, so the baseline set
    is read from the release tree here)."""
    import glob as _glob
    out = {}
    for path in sorted(_glob.glob(os.path.join(tree_root, ".engine", "modules", "*", "manifest.json"))):
        man = validate.load_json(path)
        mid = man.get("id")
        if mid:
            out[mid] = man
    return out


# --------------------------------------------------------------------------- floor classification
def _bump_at_least(current: str, level: str) -> str:
    """The version `current` bumped to at least the given `level` (major|minor). Used to express the
    mechanical FLOOR as a concrete next version for the change inventory; the maintainer may raise it."""
    parts = list(validate._ver_tuple(current))
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[0], parts[1], parts[2]
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _max_level(a: str, b: str) -> str:
    order = {"none": 0, "patch": 1, "minor": 2, "major": 3}
    return a if order[a] >= order[b] else b


def classify(baseline: Baseline, baseline_tree: str | None) -> dict:
    """The proposal: the floor per package + engine, the change inventory, and the impact statements.
    In first-cut mode there is no baseline to diff, so no delta/floor is derived — the initial version
    is the maintainer's explicit choice (release-process §7)."""
    present = _present_modules()
    engine = module_coherence.load_engine_manifest() or {}
    inventory: list[str] = []
    impacts: list[dict] = []
    package_floor: dict[str, str] = {}
    engine_level = "none"

    if baseline.first_cut:
        inventory.append(
            f"First release: establishes the baseline version for the engine and all "
            f"{len(present)} installed packages. No prior release exists to diff against, so the "
            f"initial version is chosen, not derived.")
        return {
            "mode": "first-cut",
            "baseline": None,
            "baseline_note": baseline.note,
            "current_engine": engine.get("engine_release"),
            "engine_floor_level": "none",
            "engine_floor_version": None,   # first cut: no prior release, so no mechanical floor to meet
            "package_floor": {},
            "change_inventory": inventory,
            "impacts": impacts,
        }

    # diff mode — compare the present set against the baseline release tree
    if not baseline_tree:
        raise RuntimeError(
            "a prior release exists but no baseline tree was provided to diff against; the release "
            "workflow fetches it (module_manager._fetch_release_tree), and tests inject a local tree.")
    was = _modules_in_tree(baseline_tree)
    added = sorted(set(present) - set(was))
    removed = sorted(set(was) - set(present))

    for mid in added:
        inventory.append(f"Added the '{mid}' capability.")
        engine_level = _max_level(engine_level, "minor")
    for mid in removed:
        inventory.append(f"Removed the '{mid}' capability.")
        engine_level = _max_level(engine_level, "major")

    for mid, man in present.items():
        old = was.get(mid)
        if not old:
            continue
        new_migs = set((man.get("migrations") or {}).keys())
        old_migs = set((old.get("migrations") or {}).keys())
        if new_migs - old_migs:
            keys = ", ".join(sorted(new_migs - old_migs))
            inventory.append(f"'{mid}' gained a data/config migration ({keys}).")
            package_floor[mid] = _bump_at_least(man.get("version", "0.0.0"), "minor")

    # contract / seam / interface / wiring changes carry an AI-authored impact statement
    impacts = _impact_statements(baseline_tree)
    if impacts:
        for im in impacts:
            engine_level = _max_level(engine_level, im["floor_level"])

    if not inventory and not impacts:
        inventory.append("No module added or removed and no new migration since the last release — "
                         "so at most a patch. A behaviour change with no structural signal would not "
                         "show here; cross-check against what you actually shipped.")

    # The concrete mechanical floor version: the minimum next engine version a minor/major signal forces
    # (None when nothing structural fired — a patch is discretionary, so raise-only alone bounds it). This is
    # what `apply` enforces the chosen version against and what the PR body shows the maintainer to check.
    current_engine = engine.get("engine_release", SENTINEL)
    engine_floor_version = (_bump_at_least(current_engine, engine_level)
                            if engine_level in ("minor", "major") else None)

    return {
        "mode": "diff",
        "baseline": baseline.ref,
        "baseline_note": baseline.note,
        "current_engine": current_engine,
        "engine_floor_level": engine_level,
        "engine_floor_version": engine_floor_version,
        "package_floor": package_floor,
        "change_inventory": inventory,
        "impacts": impacts,
    }


# --------------------------------------------------------------------------- impact statements (§3 semantic)
_CONTRACT_GLOBS = (
    os.path.join(".engine", "contracts"),        # eADR contracts
    os.path.join(".engine", "interfaces"),        # interface surfaces
)


def _impact_statements(baseline_tree: str) -> list[dict]:
    """For each changed/added/removed contract or interface surface between the baseline tree and the
    live tree, an AI-authored plain-language impact statement (what changed · a note that consumers
    depend on it · why that reads breaking-or-additive), plus the behavioral-correlate marking. The
    break/no-break demonstration runs "where a behavioral correlate exists" (release-process §3); with
    no acceptance-benchmark instrument built, none is available, so the marking is honest, not faked."""
    out: list[dict] = []
    for sub in _CONTRACT_GLOBS:
        live_dir = os.path.join(validate.ROOT, sub)
        base_dir = os.path.join(baseline_tree, sub)
        live = _dir_bytes(live_dir)
        base = _dir_bytes(base_dir)
        for name in sorted(set(live) | set(base)):
            lb, bb = live.get(name), base.get(name)
            if lb == bb:
                continue
            if bb is None:
                what, level = f"a new contract surface '{name}' was added", "minor"
                why = "new surfaces are additive — nothing existing depended on it yet."
            elif lb is None:
                what, level = f"the contract surface '{name}' was removed", "major"
                why = "removing a surface other parts may depend on is a breaking change."
            else:
                what, level = f"the contract surface '{name}' changed", "minor"
                why = ("a changed contract can be additive or breaking depending on which consumers "
                       "depend on it — read the change against them before confirming.")
            out.append({
                "surface": os.path.join(sub, name),
                "what": what,
                "why": why,
                "floor_level": level,
                "behavioral_demo": "none — no behavioral correlate is available (the acceptance-benchmark "
                                   "instrument is not built), so this rests on the impact statement and your "
                                   "confirmation; the release is consciously sub-bar on this signal, named here.",
            })
    return out


def _dir_bytes(d: str) -> dict:
    """name -> raw bytes for every file directly under `d` (empty when the dir is absent)."""
    out = {}
    if not os.path.isdir(d):
        return out
    for name in os.listdir(d):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                out[name] = fh.read()
    return out


# --------------------------------------------------------------------------- apply (the writer)
def _target_versions(engine_ver: str, all_ver: str | None, packages: dict, present: dict) -> dict:
    """The concrete version each package is written to: `--all` sets every present package, an explicit
    `--package id=ver` overrides, and any package left unspecified keeps its current version."""
    out = {}
    for mid, man in present.items():
        if mid in packages:
            out[mid] = packages[mid]
        elif all_ver is not None:
            out[mid] = all_ver
        else:
            out[mid] = man.get("version", SENTINEL)
    return out


def _raise_only_violations(engine_ver: str, targets: dict, engine_cur: str, present: dict) -> list[str]:
    """Every target that is NOT strictly greater than its current on-disk version — the raise-only
    guard (release-process §3). A returned non-empty list means the write must be refused."""
    bad = []
    if not _strictly_greater(engine_ver, engine_cur):
        bad.append(f"engine version {engine_ver} is not higher than the current {engine_cur}")
    for mid, ver in targets.items():
        cur = present[mid].get("version", SENTINEL)
        if not _strictly_greater(ver, cur):
            bad.append(f"package '{mid}' version {ver} is not higher than the current {cur}")
    return bad


def _schema_ok(instance, schema_path: str) -> list[str]:
    schema = validate.load_json(schema_path)
    v = jsonschema.Draft202012Validator(schema)
    return [e.message for e in v.iter_errors(instance)]


def apply(engine_ver: str, all_ver: str | None, packages: dict, proposal: dict | None,
          dry_run: bool) -> dict:
    """Record the chosen versions atomically. Returns a result dict (applied/refused + the proposed-vs-
    applied record for traceability). Writes nothing on a raise-only violation or a validation failure."""
    present = _present_modules()
    engine = module_coherence.load_engine_manifest()
    if engine is None:
        raise RuntimeError("the engine manifest (.engine/engine.json) is missing; cannot cut a release.")
    engine_cur = engine.get("engine_release", SENTINEL)
    targets = _target_versions(engine_ver, all_ver, packages, present)

    # version grammar: refuse a non-version string at the door (a typo must not reach a manifest)
    bad_fmt = []
    if not _valid_version(engine_ver):
        bad_fmt.append(f"engine version '{engine_ver}' is not a valid version (expected like 1.2.0 or 1.0.0-rc1)")
    for mid, ver in targets.items():
        if not _valid_version(ver):
            bad_fmt.append(f"package '{mid}' version '{ver}' is not a valid version (expected like 1.2.0)")
    if bad_fmt:
        return {"applied": False, "reason": "invalid-version", "violations": bad_fmt,
                "recovery": "use dotted-number versions, optionally with a -prerelease suffix (1.2.0, 1.0.0-rc1)."}

    # raise-only: refuse loudly, never silently lower (release-process §3)
    violations = _raise_only_violations(engine_ver, targets, engine_cur, present)
    if violations:
        return {"applied": False, "reason": "raise-only", "violations": violations,
                "recovery": "choose versions strictly higher than the current ones, then re-run."}

    # not-below-the-confirmed-floor: when a proposal is supplied, a target must MEET OR RAISE its
    # confirmed floor — compared against the floor value, not the current version (raise-only already
    # covered current). A target strictly below the floor is refused.
    floor_notes = []
    if proposal:
        # the ENGINE floor: a minor/major bump forced by what changed since the last release (a module added
        # or removed, an interface changed) must be MET, not just be higher than the current version. Without
        # this, a removed-module major floor could be undercut by a patch bump — the §3 "catch a wrong floor"
        # backstop. None when nothing structural fired (a patch is discretionary; raise-only bounds it).
        engine_floor = proposal.get("engine_floor_version")
        if engine_floor and _strictly_greater(engine_floor, engine_ver):
            floor_notes.append(f"engine version {engine_ver} is below the mechanical floor {engine_floor} "
                               f"that what changed since the last release requires")
        pf = proposal.get("package_floor", {})
        for mid, floor in pf.items():
            if mid in targets and _strictly_greater(floor, targets[mid]):
                floor_notes.append(f"'{mid}' version {targets[mid]} is below its confirmed floor {floor}")
        if floor_notes:
            return {"applied": False, "reason": "below-confirmed-floor", "violations": floor_notes,
                    "recovery": "raise the engine and any flagged packages to at least their mechanical floor."}

    # stage every touched file, validate ALL before any swap, then swap together (rollback on failure)
    staged: list[tuple[str, str]] = []  # (target_path, temp_path)
    errors: list[str] = []
    try:
        # engine.json — mutate in place so home_repository/identity/order are byte-preserved
        new_engine = dict(engine)
        new_engine["engine_release"] = engine_ver
        pkgs = dict(new_engine.get("packages", {}))
        for mid, ver in targets.items():
            if mid in pkgs:
                pkgs[mid] = ver
        new_engine["packages"] = pkgs
        errors += [f"engine.json: {m}" for m in _schema_ok(new_engine, ENGINE_SCHEMA)]

        # each module manifest — mutate version only
        module_new: dict[str, dict] = {}
        for _rel, man in module_coherence.discover_manifests():
            mid = man.get("id")
            if mid in targets:
                nm = dict(man)
                nm["version"] = targets[mid]
                module_new[_rel] = nm
                errors += [f"{_rel}: {m}" for m in _schema_ok(nm, MODULE_SCHEMA)]

        # split-brain guard: engine.json packages[mid] must equal each module manifest version
        for _rel, nm in module_new.items():
            mid = nm.get("id")
            if new_engine["packages"].get(mid) != nm.get("version"):
                errors.append(f"split-brain: engine.json packages['{mid}']="
                              f"{new_engine['packages'].get(mid)} != {_rel} version={nm.get('version')}")

        if errors:
            return {"applied": False, "reason": "validation", "violations": errors,
                    "recovery": "the computed manifests did not validate; nothing was written."}

        if dry_run:
            return {"applied": False, "reason": "dry-run", "targets": targets, "engine": engine_ver,
                    "from_engine": engine_cur}

        # write temps
        def _stage(path, data):
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
            staged.append((path, tmp))

        _stage(module_manager._engine_manifest_path(), new_engine)
        for _rel, nm in module_new.items():
            _stage(os.path.join(validate.ROOT, _rel), nm)

        # swap together; a write error mid-swap rolls back the files already swapped, so the tree is
        # never left half-written (best-effort atomic — the reviewed-PR merge is the real all-or-
        # nothing unit, and the release-integrity check catches any residual split-brain at merge).
        def _read_bytes(p):
            with open(p, "rb") as fh:
                return fh.read()

        originals = {path: _read_bytes(path) for path, _tmp in staged}
        swapped = []
        try:
            for path, tmp in staged:
                os.replace(tmp, path)
                swapped.append(path)
            staged = []
        except OSError as exc:
            for path in swapped:
                with open(path, "wb") as fh:
                    fh.write(originals[path])
            raise RuntimeError(f"a write error interrupted the cut ({exc}); the files already written were "
                               f"restored, so no versions changed and nothing was left half-written.")
    finally:
        for _path, tmp in staged:  # any un-swapped temp on an error path
            try:
                os.unlink(tmp)
            except OSError:
                pass

    return {"applied": True, "engine": engine_ver, "from_engine": engine_cur, "targets": targets,
            "proposed_floor": (proposal or {}).get("package_floor", {})}


# --------------------------------------------------------------------------- rendering
def _render_proposal(p: dict) -> str:
    lines = ["Release proposal", "================", "", p["baseline_note"], ""]
    lines.append("What changed since the last release:")
    for c in p["change_inventory"]:
        lines.append(f"  - {c}")
    if p["impacts"]:
        lines.append("")
        lines.append("Contract / interface changes (read before confirming):")
        for im in p["impacts"]:
            lines.append(f"  - {im['what']}: {im['why']}")
            lines.append(f"    behavioral check: {im['behavioral_demo']}")
    lines.append("")
    if p["mode"] == "first-cut":
        lines.append("This is the first cut — choose the initial version explicitly, e.g.:")
        lines.append("  release_cut.py apply --engine <ver> --all <ver>")
    else:
        floor = p["engine_floor_level"]
        if floor == "none":
            lines.append(f"No structural change forces a bump — a patch at most (current "
                         f"{p['current_engine']}). You may still raise it if you shipped a behaviour "
                         f"change with no structural signal; you can never lower it.")
        else:
            lines.append(f"Mechanical engine floor: at least a {floor} bump "
                         f"(current {p['current_engine']}). You may raise it, never lower it.")
        if p["package_floor"]:
            lines.append("Per-package floors:")
            for mid, ver in p["package_floor"].items():
                lines.append(f"  - {mid}: at least {ver}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- release-PR body (§6 legibility)
def _gate_path_line(state: str) -> str:
    """The §6 legible gate-path line: the three release-readiness states must read as VISIBLY DISTINCT, never
    alike. Only `sub-bar` is reachable today — no acceptance-benchmark instrument is built, so nothing measures
    a release — but `passed`/`errored` are rendered here structurally so a future benchmark reads legibly
    rather than as a retrofit (the standing §6 invariant, not a one-of-three accident)."""
    if state == "passed":
        return ("**Release readiness — passed.** The engine was exercised against its readiness check and met "
                "the bar for this release.")
    if state == "errored":
        return ("**Release readiness — could not be checked (it errored).** The readiness check did not run to "
                "completion, so readiness is unproven — treat this release as unverified until it runs clean.")
    return ("**Release readiness — no automated check ran (this is on purpose).** There is no automated "
            "readiness check built yet, so this release was not measured against one. It rests on the summary "
            "below and your own read — not a machine check. This is a deliberate, recorded choice, not a "
            "passed check.")


def _version_lines(applied: dict) -> list:
    """Plain-language 'what versions this sets' — collapsed to one line when every capability moves to the
    engine's own new version (the uniform first-cut case), else itemised so a per-capability difference shows."""
    engine = applied.get("engine")
    from_engine = applied.get("from_engine")
    targets = applied.get("targets") or {}
    # the first cut moves from the construction sentinel `0.0.0-dev`, which is internal and means nothing to the
    # maintainer — say "no earlier version" instead of surfacing it.
    from_shown = "no earlier version" if from_engine == SENTINEL else from_engine
    lines = [f"- Engine: {from_shown} → {engine}"]
    if targets and all(v == engine for v in targets.values()):
        lines.append(f"- Every capability ({len(targets)}): → {engine}")
    else:
        for mid in sorted(targets):
            lines.append(f"- {mid}: → {targets[mid]}")
    return lines


def _pr_section(header: str, summary: str, body_lines: list, impact: str) -> list:
    """One pull-request-body section in the repo template's shape — a **bold one-line summary**, its bullets,
    then the italic `*Impact:*` line — so the release body matches the form every engine pull request's body
    uses, not merely the required headers (a header-only body clears the completeness gate but is not a
    template-conforming body)."""
    return [f"## {header}", "", f"**{summary}**", "", *body_lines, "", f"*Impact: {impact}*", ""]


def render_pr_body(proposal: dict, applied: dict, gate_state: str = "sub-bar") -> str:
    """The release pull request's body — the maintainer's whole evidence bundle, authored HERE (never
    composed in workflow bash) so the §6 gate-path legibility has one home. It takes both the `propose` JSON
    (the change inventory + interface impacts) and the `apply` result JSON (the versions actually recorded),
    and closes with the confirm/raise/reject guidance that makes the PR review the §3 consent act: the merge
    is the go-ahead, and a wrong or missing signal is caught by closing and re-running with the right version.
    Maintainer-facing register (§8): one engine version moving vX→vY — no 'release-cut'/'bump'/'version
    production' vocabulary. Every section follows the repo pull-request template's form (bold summary →
    bullets → `*Impact:*`), not just its headers — a real template-conforming body, whose section names also
    clear the pull-request-completeness gate."""
    engine = applied.get("engine")
    from_engine = applied.get("from_engine")
    # this body IS the maintainer's consent surface, so it must never author a "None → None" release: a refused
    # or malformed apply result carries no versions and cannot be rendered as a release.
    if not engine:
        raise RuntimeError("cannot render a release summary: the apply result recorded no engine version "
                           "(the release was refused or the result is malformed).")
    # the construction sentinel `0.0.0-dev` is internal — never surface it to the maintainer (see _version_lines)
    from_shown = "no earlier version" if from_engine == SENTINEL else from_engine

    out = [f"# A new engine version: {from_shown} → {engine}", ""]

    out += _pr_section(
        "Purpose",
        f"This records a new version of your engine — {from_shown} → {engine} — for you to review and publish.",
        [f"- Merging this is your go-ahead to release {engine}; closing it releases nothing and changes none of "
         "your own settings or content.",
         "- A release only ever moves the version up, never down."],
        f"merging publishes {engine} for your instances to upgrade to; nothing is published until then.")

    # Scope — the versions recorded + the change inventory that set them (the itemised version lines and the
    # least-version floor line stay verbatim; they are what a reviewer checks the release against).
    scope = ["The versions this release sets:", *_version_lines(applied)]
    floor_v = proposal.get("engine_floor_version")
    if floor_v:
        scope.append(f"- The least this release could be is **{floor_v}** — that is what the changes below "
                     f"require; a higher version is fine, a lower one is not.")
    scope += ["", "What changed since the last release:"]
    scope += [f"- {c}" for c in proposal.get("change_inventory", [])]
    out += _pr_section(
        "Scope",
        "The engine and capability versions this records, and the changes that set them.",
        scope,
        "these are the exact versions written into the manifests and the maps that mirror them.")

    out += _pr_section(
        "Out of scope",
        "What merging does not do.",
        ["- It does not change how your engine behaves beyond the version stamp.",
         "- It does not migrate any of your data.",
         "- It does not touch your own settings or content."],
        "the only thing this pull request changes is the recorded version and the generated maps that mirror it.")

    # Risk — the §6 gate-path line is the (already bold-led) section summary; the breaking-change warning and
    # the interface-impact list are its bullets, so a reviewer scanning "Risk" sees the weight here, not only
    # as a neutral line up in Scope.
    risk = []
    if proposal.get("engine_floor_level") == "major":
        risk.append("- **This release makes a breaking change.** Something an earlier version provided was "
                    "removed, or changed in a way that is not backward-compatible — so anything that relied on "
                    "it will need attention. What changed is listed under Scope above.")
    impacts = proposal.get("impacts") or []
    if impacts:
        if risk:             # a breaking-change bullet precedes this intro — a blank line keeps the intro from
            risk.append("")  # being absorbed into that bullet as a lazy markdown continuation (the two would
                             # otherwise fuse, hiding the interface-changes signpost on the highest-stakes release).
        risk.append("Interface changes to read before you merge:")
        risk += [f"- {im.get('what', '')}: {im.get('why', '')}" for im in impacts]
    else:
        risk.append("- No changes to interface contract files were detected — this does not cover a removed "
                    "capability or a data migration, which would be listed under Scope. The summary can only "
                    "show changes it detects mechanically, so your own knowledge of what you shipped is the "
                    "backstop (see Review).")
    out += ["## Risk", "", _gate_path_line(gate_state), "", *risk, "",
            "*Impact: a wrong version, or a change the summary could not detect mechanically, is caught by "
            "closing and re-running with the right version — nothing publishes until you merge.*", ""]

    out += _pr_section(
        "Validation",
        "The engine's own tooling produced this and `engine-ci` checks it — the mechanical floor.",
        ["- A green check shows the versions agree across all the files that record them, the generated maps "
         "are in sync, and this summary is complete.",
         f"- It does **not** judge whether {engine} is the right version to release — that judgment is yours."],
        f"green means the release conforms to the engine's rules, not that {engine} is the right call.")

    out += _pr_section(
        "Review",
        "How to act on this — go ahead, raise the version, or stop.",
        [f"- **Go ahead** — if the summary above matches what you built, merge this; that merge is your consent "
         f"to release {engine}.",
         "- **Want a higher version** — close this and run the release again with a higher version number (a "
         "release can only ever go up, never down).",
         "- **Something's missing** — if you know you changed something that is not listed above (for example "
         "you removed a capability but do not see it here), close this and run the release again with the "
         "version you know it should be; the summary shows only what it can detect mechanically, so your own "
         "knowledge of what you shipped is the backstop."],
        f"your merge is the binding consent to publish {engine} — the engine never merges this for you.")

    out += _pr_section(
        "Files of interest",
        "Where to look — the recorded versions and the maps that mirror them.",
        ["- `.engine/engine.json` and each installed capability's `.engine/modules/<id>/manifest.json` — the "
         "recorded versions.",
         "- `.engine/knowledge/graph.json` and `.engine/self-map.md` — the generated maps, refreshed to match."],
        "these are the only files this pull request changes.")

    out += _pr_section(
        "Claude involvement",
        "The engine's release workflow prepared this; the version choice and the decision to publish are yours.",
        ["- It computed the version, recorded it into the manifests, regenerated the derived maps, and opened "
         "this for your review.",
         "- The version follows the engine's release process; nothing is published until you merge."],
        f"the mechanical steps are the engine's; the decision to publish {engine} is yours.")

    out += ["_Closing this pull request leaves behind the `release/…` branch it was opened from. That branch is "
            "not a release — nothing is released until you merge — and it is safe to delete._"]
    return "\n".join(out)


# --------------------------------------------------------------------------- CLI
def _cmd_propose(args) -> int:
    baseline = resolve_baseline()
    tree, cleanup = _baseline_tree_for(baseline, args.baseline_tree)
    try:
        proposal = classify(baseline, tree)
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
    if args.json:
        print(json.dumps(proposal, indent=2))
    else:
        print(_render_proposal(proposal))
    return 0


def _cmd_pr_body(args) -> int:
    proposal = validate.load_json(args.proposal)
    applied = validate.load_json(args.applied)
    print(render_pr_body(proposal, applied, args.gate_state))
    return 0


def _cmd_apply(args) -> int:
    packages = {}
    for spec in args.package or []:
        if "=" not in spec:
            print(f"CONFIG ERROR: --package expects id=version, got '{spec}'.", file=sys.stderr)
            return 2
        mid, ver = spec.split("=", 1)
        packages[mid.strip()] = ver.strip()
    proposal = None
    if args.proposal:
        if not os.path.isfile(args.proposal):
            print(f"CONFIG ERROR: the proposal file '{args.proposal}' does not exist. Pass the path to a "
                  f"proposal written by `propose --json`.", file=sys.stderr)
            return 2
        proposal = validate.load_json(args.proposal)
    result = apply(args.engine, getattr(args, "all"), packages, proposal, args.dry_run)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result.get("applied") or result.get("reason") == "dry-run" else 1
    if result.get("applied"):
        print(f"Applied: engine {result['from_engine']} -> {result['engine']}; "
              f"{len(result['targets'])} package version(s) recorded.")
        return 0
    if result.get("reason") == "dry-run":
        print(f"Dry run: engine {result['from_engine']} -> {result['engine']} across "
              f"{len(result['targets'])} package(s); nothing written.")
        return 0
    print(f"Refused ({result['reason']}):", file=sys.stderr)
    for v in result.get("violations", []):
        print(f"  - {v}", file=sys.stderr)
    if result.get("recovery"):
        print(f"To fix: {result['recovery']}", file=sys.stderr)
    return 1


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="release_cut.py", description="Decide and record the next engine version.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("propose", help="read-only: the proposed bump floor + change inventory")
    pp.add_argument("--json", action="store_true")
    pp.add_argument("--baseline-tree", help="a local release tree to diff against (tests/workflow inject this)")
    pa = sub.add_parser("apply", help="record the chosen versions into the manifests (atomic, raise-only)")
    pa.add_argument("--engine", required=True, help="the new engine version")
    pa.add_argument("--all", help="set every present package to this version (the first-cut / uniform case)")
    pa.add_argument("--package", action="append", help="id=version override for one package (repeatable)")
    pa.add_argument("--proposal", help="a proposal JSON from `propose` to enforce the confirmed floor against")
    pa.add_argument("--dry-run", action="store_true", help="compute + validate but write nothing")
    pa.add_argument("--json", action="store_true")
    pb = sub.add_parser("pr-body", help="render the release pull-request body from a proposal + apply-result")
    pb.add_argument("--proposal", required=True, help="the proposal JSON written by `propose --json`")
    pb.add_argument("--applied", required=True, help="the result JSON written by `apply --json`")
    pb.add_argument("--gate-state", default="sub-bar", choices=["passed", "sub-bar", "errored"],
                    help="the acceptance-benchmark outcome to render (only 'sub-bar' is reachable until the "
                         "benchmark is built)")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "propose":
            return _cmd_propose(args)
        if args.cmd == "pr-body":
            return _cmd_pr_body(args)
        return _cmd_apply(args)
    except Exception as exc:  # plain-language failure, never a traceback (release-process §6)
        print(f"\nRELEASE-CUT ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
