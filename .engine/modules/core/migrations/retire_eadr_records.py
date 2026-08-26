"""Retire the former Engine decision-record surface from an installed repository.

This is deliberately a version-bounded compatibility payload, not a live feature.  Preflight discovers the
exact historical tracked record set, refuses every unsafe or ambiguous tree, and checks that no actionable
Project Manager head still asks executable work to use the removed surface.  Apply receives only that sealed
set.  It mutates through held directory descriptors, captures every file under a private transaction-owned
name before deletion or replacement, and returns one exact receipt entry per target.

The generic upgrade transaction owns crash recovery.  Every private capture name is part of each target's
recovery scope, so a killed process is restored from the transaction's pre-update commit without an archive,
tombstone, or migration-specific recovery branch.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess


_INSTANCE = ".engine/contracts/instance"
_OVERRIDES = ".engine/operator-overrides.json"
_OVERRIDE_KEY = "contract-threshold"
_RECORD_RE = re.compile(
    r"^(?:[a-z0-9]+(?:-[a-z0-9]+)*-)?eADR-[0-9]{4}(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?\.md$")
_RETIRED_PLAN_TOKEN_RE = re.compile(
    r"(?:\beADR(?:-[0-9]{4})?\b|\.engine/contracts(?:/instance)?\b|contract\.v1\b|"
    r"contract-threshold\b|contract-(?:frontmatter|shape)\b|DEPLOYMENT_CONTRACTS\b)")
_Q_PREFIX = ".engine-upgrade-retirement-quarantine-"
_OVERRIDE_Q = ".engine-upgrade-retirement-quarantine-overrides"
_OVERRIDE_NEXT = ".engine-upgrade-retirement-next-overrides"


class RetirementRefused(RuntimeError):
    def __init__(self, code: str, path: str, reason: str, remediation: str):
        super().__init__(reason)
        self.refusal = {"code": code, "path": path, "reason": reason, "remediation": remediation}


def _refusal(code: str, path: str, reason: str, remediation: str) -> dict:
    return {"code": code, "path": path, "reason": reason, "remediation": remediation}


def _required_primitives() -> str | None:
    required_dir_fd = (os.open, os.stat, os.rename, os.unlink, os.mkdir, os.rmdir)
    missing = [fn.__name__ for fn in required_dir_fd if fn not in os.supports_dir_fd]
    if os.listdir not in os.supports_fd:
        missing.append("listdir(fd)")
    for name in ("O_DIRECTORY", "O_NOFOLLOW"):
        if not hasattr(os, name):
            missing.append(name)
    return ", ".join(missing) if missing else None


def _run_git(root: str, *args: str, text: bool = False):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=text,
                          timeout=30, check=False)


def _object_format(root: str) -> str:
    proc = _run_git(root, "rev-parse", "--show-object-format", text=True)
    value = proc.stdout.strip() if proc.returncode == 0 else ""
    if value not in {"sha1", "sha256"}:
        raise RetirementRefused(
            "git-object-format-unavailable", _INSTANCE,
            "Git could not identify the repository's object format.",
            "Repair the repository's Git metadata, then run the update again.")
    return value


def _blob_identity(data: bytes, object_format: str) -> str:
    digest = hashlib.new(object_format)
    digest.update(f"blob {len(data)}\0".encode("ascii"))
    digest.update(data)
    return "git-blob:" + digest.hexdigest()


def _read_fd(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _open_dir(name: str, parent_fd: int | None = None) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    return os.open(name, flags, dir_fd=parent_fd)


def _open_regular(name: str, parent_fd: int) -> tuple[int, os.stat_result]:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    meta = os.fstat(fd)
    if not stat.S_ISREG(meta.st_mode):
        os.close(fd)
        raise OSError(f"{name} is not a regular file")
    return fd, meta


def _same_entry(name: str, parent_fd: int, meta: os.stat_result) -> bool:
    now = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    return stat.S_ISREG(now.st_mode) and (now.st_dev, now.st_ino) == (meta.st_dev, meta.st_ino)


def _same_dir(name: str, parent_fd: int, directory_fd: int) -> bool:
    now, held = os.stat(name, dir_fd=parent_fd, follow_symlinks=False), os.fstat(directory_fd)
    return stat.S_ISDIR(now.st_mode) and (now.st_dev, now.st_ino) == (held.st_dev, held.st_ino)


def _tracked_inventory(root: str) -> dict[str, tuple[str, str]]:
    proc = _run_git(root, "ls-files", "-s", "-z", "--", _INSTANCE)
    if proc.returncode != 0:
        raise RetirementRefused(
            "tracked-inventory-unavailable", _INSTANCE,
            "Git could not enumerate the former record directory safely.",
            "Repair the repository's Git metadata, then run the update again.")
    prefix = (_INSTANCE + "/").encode()
    found: dict[str, tuple[str, str]] = {}
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            header, path_b = raw.split(b"\t", 1)
            mode_b, oid_b, stage_b = header.split(b" ", 2)
            path = os.fsdecode(path_b)
        except (ValueError, UnicodeError):
            raise RetirementRefused(
                "tracked-entry-malformed", _INSTANCE,
                "Git returned an undecodable former-record path.",
                "Rename the path to a supported historical record name, then run the update again.")
        if not path_b.startswith(prefix):
            continue
        tail = path[len(_INSTANCE) + 1:]
        if "/" in tail:
            raise RetirementRefused(
                "nested-entry", path,
                "The former record directory contains a nested tracked entry.",
                "Move or remove the nested entry deliberately, then run the update again.")
        mode, oid, stage = mode_b.decode("ascii", "replace"), oid_b.decode("ascii", "replace"), stage_b
        if stage != b"0" or mode != "100644":
            raise RetirementRefused(
                "special-mode", path,
                "A former record is unmerged, executable, linked, or otherwise not an ordinary tracked file.",
                "Resolve the index entry and make it an ordinary tracked Markdown file, then run the update again.")
        if tail in found:
            raise RetirementRefused(
                "duplicate-entry", path, "Git returned the same former-record path more than once.",
                "Resolve the duplicate index state, then run the update again.")
        found[tail] = (mode, oid)
    return found


def _q_name(name: str) -> str:
    return _Q_PREFIX + hashlib.sha256(os.fsencode(name)).hexdigest()[:20]


def _path_for_instance_name(name: str) -> str:
    return _INSTANCE + "/" + name


def _plan_refusals(root: str) -> list[dict]:
    """Refuse only actionable current heads. Historical revisions and closed plans are never scanned."""
    try:
        import plan_store
        library = plan_store.PlanLibrary()
    except Exception as exc:  # noqa: BLE001 - an unprovable executable-head set fails closed
        return [_refusal(
            "plan-library-unavailable", ".engine/plans",
            f"The update could not prove that current Project Manager heads avoid the removed surface ({exc}).",
            "Repair or explicitly locate the plan library, then run the update again.")]
    if not library.root.exists():
        return []
    refusals = []
    for slug in library.slugs():
        try:
            record = library.read_record(slug)
            status = plan_store.derived_status(record)
            if status in {"complete", "retired", "abandoned"}:
                continue
            document = library.head(slug)
            # Only the executable Build payload can strand a future update. Raw intent, revision notes,
            # deliberation, and other immutable planning history may name the retired surface as context;
            # treating that history as an instruction would force a history rewrite the retirement explicitly
            # forbids. Native/legacy plan fixtures without an engine-plan wrapper remain directly scannable.
            executable = document.get("build_plan", document) if isinstance(document, dict) else document
            rendered = json.dumps(executable, sort_keys=True, ensure_ascii=False)
            tokens = sorted(set(_RETIRED_PLAN_TOKEN_RE.findall(rendered)))
            if not tokens:
                continue
            snapshot = record.get("current", {}).get("snapshot") or "current-head"
            path = f".engine/plans/{slug}/{snapshot}"
            plan_id = record.get("plan_id") or slug
            refusals.append(_refusal(
                "actionable-plan-incompatible", path,
                f"Actionable plan {plan_id} ({status}) still names removed decision-record machinery: "
                + ", ".join(tokens) + ".",
                f"Revise or close plan {plan_id}, then run the update again. Historical non-head revisions "
                "and completed plans do not need rewriting."))
        except Exception as exc:  # noqa: BLE001 - name the exact plan rather than silently skipping it
            refusals.append(_refusal(
                "plan-head-unreadable", f".engine/plans/{slug}/record.json",
                f"The current head for plan {slug} could not be checked safely ({exc}).",
                f"Repair or close plan {slug}, then run the update again."))
    return refusals


def _discover(context: dict) -> dict:
    root = context["root"]
    missing = _required_primitives()
    if missing:
        return {"status": "refused", "refusals": [_refusal(
            "dirfd-primitives-unavailable", _INSTANCE,
            f"This platform lacks required no-follow directory operations: {missing}.",
            "Run the update on a supported local Python and filesystem; no record was changed.")]}
    plan_refs = _plan_refusals(root)
    if plan_refs:
        return {"status": "refused", "refusals": plan_refs}
    try:
        object_format = _object_format(root)
        tracked = _tracked_inventory(root)
        root_fd = _open_dir(root)
        try:
            engine_fd = _open_dir(".engine", root_fd)
            try:
                contracts_fd = _open_dir("contracts", engine_fd)
                try:
                    instance_fd = _open_dir("instance", contracts_fd)
                    try:
                        names = sorted(os.listdir(instance_fd))
                        if set(names) != set(tracked):
                            extra = sorted(set(names) - set(tracked))
                            missing_names = sorted(set(tracked) - set(names))
                            bad = extra[0] if extra else missing_names[0]
                            path = _path_for_instance_name(bad)
                            reason = ("The former record directory contains an untracked or unexpected entry."
                                      if extra else "A Git-tracked former record is missing from the working tree.")
                            raise RetirementRefused(
                                "unsafe-tree" if extra else "missing-target", path, reason,
                                "Restore, commit, move, or remove the named entry deliberately, then run the update again.")
                        if "README.md" not in names:
                            raise RetirementRefused(
                                "missing-guide", _INSTANCE + "/README.md",
                                "The shipped guide for the former record directory is missing.",
                                "Restore the tracked guide, then run the update again.")
                        targets = []
                        for name in names:
                            path = _path_for_instance_name(name)
                            if name != "README.md" and not _RECORD_RE.fullmatch(name):
                                raise RetirementRefused(
                                    "unexpected-name", path,
                                    "An entry in the former record directory is not one of the shipped historical filename forms.",
                                    "Rename it to its exact historical form or move it out deliberately, then run the update again.")
                            try:
                                fd, meta = _open_regular(name, instance_fd)
                            except OSError:
                                raise RetirementRefused(
                                    "unsafe-entry", path,
                                    "A former record changed into a link, directory, or special file.",
                                    "Restore it as an ordinary tracked file, then run the update again.")
                            try:
                                identity = _blob_identity(_read_fd(fd), object_format)
                                if not _same_entry(name, instance_fd, meta) or identity.removeprefix("git-blob:") != tracked[name][1]:
                                    raise RetirementRefused(
                                        "entry-raced", path,
                                        "A former record changed while the update was inspecting it.",
                                        "Stop the concurrent writer, restore the tracked bytes, then run the update again.")
                            finally:
                                os.close(fd)
                            qpath = _path_for_instance_name(_q_name(name))
                            try:
                                os.stat(_q_name(name), dir_fd=instance_fd, follow_symlinks=False)
                            except FileNotFoundError:
                                pass
                            else:
                                raise RetirementRefused(
                                    "quarantine-collision", qpath,
                                    "A private name reserved for safe retirement already exists.",
                                    "Recover or remove the earlier update residue, then run the update again.")
                            targets.append({"path": path, "operation": "delete", "before_identity": identity,
                                            "recovery_scope": [path, qpath]})
                    finally:
                        os.close(instance_fd)
                finally:
                    os.close(contracts_fd)

                try:
                    override_fd, override_meta = _open_regular("operator-overrides.json", engine_fd)
                except FileNotFoundError:
                    override_fd = None
                except OSError:
                    raise RetirementRefused(
                        "unsafe-override", _OVERRIDES,
                        "The saved settings file is a link, directory, or special file.",
                        "Restore it as an ordinary tracked JSON file, then run the update again.")
                if override_fd is not None:
                    try:
                        data_b = _read_fd(override_fd)
                        try:
                            data = json.loads(data_b)
                        except (UnicodeDecodeError, ValueError):
                            raise RetirementRefused(
                                "override-unreadable", _OVERRIDES,
                                "The saved settings file is not readable JSON.",
                                "Repair the JSON without discarding unrelated settings, then run the update again.")
                        if not isinstance(data, dict):
                            raise RetirementRefused(
                                "override-malformed", _OVERRIDES,
                                "The saved settings file is not a top-level object.",
                                "Repair the JSON object without discarding unrelated settings, then run the update again.")
                        if _OVERRIDE_KEY in data:
                            identity = _blob_identity(data_b, object_format)
                            if not _same_entry("operator-overrides.json", engine_fd, override_meta):
                                raise RetirementRefused(
                                    "override-raced", _OVERRIDES,
                                    "The saved settings file changed while the update was inspecting it.",
                                    "Stop the concurrent writer, settle the settings, then run the update again.")
                            tracked_override = _run_git(root, "ls-files", "--error-unmatch", "--", _OVERRIDES)
                            if tracked_override.returncode != 0:
                                raise RetirementRefused(
                                    "override-untracked", _OVERRIDES,
                                    "The saved setting to retire is not Git-tracked and cannot be recovered safely.",
                                    "Commit the settings file or remove that one obsolete key deliberately, then run the update again.")
                            qpath = ".engine/" + _OVERRIDE_Q
                            next_path = ".engine/" + _OVERRIDE_NEXT
                            for leaf, rel in ((_OVERRIDE_Q, qpath), (_OVERRIDE_NEXT, next_path)):
                                try:
                                    os.stat(leaf, dir_fd=engine_fd, follow_symlinks=False)
                                except FileNotFoundError:
                                    pass
                                else:
                                    raise RetirementRefused(
                                        "quarantine-collision", rel,
                                        "A private name reserved for safe settings retirement already exists.",
                                        "Recover or remove the earlier update residue, then run the update again.")
                            targets.append({"path": _OVERRIDES, "operation": "replace",
                                            "before_identity": identity,
                                            "recovery_scope": [_OVERRIDES, qpath, next_path]})
                    finally:
                        os.close(override_fd)
            finally:
                os.close(engine_fd)
        finally:
            os.close(root_fd)
        return {"status": "ready", "targets": targets}
    except RetirementRefused as exc:
        return {"status": "refused", "refusals": [exc.refusal]}
    except OSError as exc:
        return {"status": "refused", "refusals": [_refusal(
            "unsafe-tree", _INSTANCE,
            f"The former record tree could not be opened through verified no-follow directories ({exc}).",
            "Repair the named directory tree and stop concurrent changes, then run the update again.")]}


def preflight(context: dict) -> dict:
    if context.get("kind") != "tracked-content":
        return {"status": "refused", "refusals": [_refusal(
            "wrong-protocol", _INSTANCE, "This retirement requires the tracked-content protocol.",
            "Run it through the Engine upgrade command.")]}
    return _discover(context)


def _killpoint(label: str) -> None:
    """Test-only crash seam. Unset in production; a child-process matrix drives exact durable boundaries."""
    requested = os.environ.get("ENGINE_RETIREMENT_KILL_AT")
    if requested in {label, label.split(":", 1)[0]}:
        os.kill(os.getpid(), 9)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _sealed_targets(sealed_plan: dict) -> dict[str, dict]:
    """Validate the pre-overlay target plan without consulting Git's now-candidate index."""
    if not isinstance(sealed_plan, dict) or sealed_plan.get("schema_version") != "tracked-content-plan.v1" \
            or sealed_plan.get("migration_id") != "core@0.7.0" \
            or sealed_plan.get("module_id") != "core" or sealed_plan.get("version") != "0.7.0" \
            or sealed_plan.get("run") != "migrations/retire_eadr_records.py" \
            or not isinstance(sealed_plan.get("targets"), list):
        raise RuntimeError("the sealed retirement plan is malformed")
    targets = {}
    for target in sealed_plan["targets"]:
        if not isinstance(target, dict) or not isinstance(target.get("path"), str) \
                or not isinstance(target.get("before_identity"), str) \
                or not isinstance(target.get("recovery_scope"), list):
            raise RuntimeError("the sealed retirement plan contains a malformed target")
        path = target["path"]
        if path in targets:
            raise RuntimeError(f"the sealed retirement plan repeats a target: {path}")
        if path.startswith(_INSTANCE + "/"):
            name = path[len(_INSTANCE) + 1:]
            if name != "README.md" and not _RECORD_RE.fullmatch(name):
                raise RuntimeError(f"the sealed retirement plan names an unsupported record: {path}")
            expected_scope = sorted([path, _path_for_instance_name(_q_name(name))])
            expected_operation = "delete"
        elif path == _OVERRIDES:
            expected_scope = sorted([_OVERRIDES, ".engine/" + _OVERRIDE_Q, ".engine/" + _OVERRIDE_NEXT])
            expected_operation = "replace"
        else:
            raise RuntimeError(f"the sealed retirement plan names an out-of-scope target: {path}")
        if target.get("operation") != expected_operation or target["recovery_scope"] != expected_scope:
            raise RuntimeError(f"the sealed retirement plan has an invalid operation or recovery scope: {path}")
        targets[path] = target
    if _INSTANCE + "/README.md" not in targets:
        raise RuntimeError("the sealed retirement plan does not include the former record guide")
    return targets


def apply(context: dict, sealed_plan: dict) -> dict:
    # Preflight ran against the baseline index. The generic updater then overlays and stages the candidate
    # before apply, so Git's live index intentionally no longer contains these retired paths. Re-discovering
    # through that candidate index makes every valid real upgrade disagree with its own sealed plan. The plan
    # is the authority in phase two; live filesystem bytes, membership, plan heads, and races are rechecked
    # below through held no-follow descriptors before anything is changed.
    refusals = _plan_refusals(context["root"])
    if refusals:
        first = refusals[0]
        raise RuntimeError(f"{first.get('code')}: {first.get('reason')} {first.get('remediation')}")
    targets = _sealed_targets(sealed_plan)
    object_format = _object_format(context["root"])
    changes = []
    root_fd = _open_dir(context["root"])
    try:
        engine_fd = _open_dir(".engine", root_fd)
        try:
            contracts_fd = _open_dir("contracts", engine_fd)
            try:
                instance_fd = _open_dir("instance", contracts_fd)
                try:
                    instance_paths = sorted(p for p in targets if p.startswith(_INSTANCE + "/"))
                    expected_names = [path[len(_INSTANCE) + 1:] for path in instance_paths]
                    if sorted(os.listdir(instance_fd)) != expected_names:
                        raise RuntimeError("the live retirement set no longer matches the sealed preflight plan")
                    # Verify the WHOLE sealed set before the first rename. A concurrent add/remove or byte
                    # change therefore refuses without partial mutation; the per-file checks in the mutation
                    # loop remain as the second race belt.
                    for path in instance_paths:
                        name = path[len(_INSTANCE) + 1:]
                        fd, meta = _open_regular(name, instance_fd)
                        try:
                            if _blob_identity(_read_fd(fd), object_format) != targets[path]["before_identity"] \
                                    or not _same_entry(name, instance_fd, meta):
                                raise RuntimeError(f"sealed record changed before apply: {path}")
                        finally:
                            os.close(fd)
                    if not _same_dir("instance", contracts_fd, instance_fd):
                        raise RuntimeError("the former record directory was swapped before apply")
                    for path in instance_paths:
                        name = path[len(_INSTANCE) + 1:]
                        qname = _q_name(name)
                        target = targets[path]
                        fd, meta = _open_regular(name, instance_fd)
                        try:
                            if _blob_identity(_read_fd(fd), object_format) != target["before_identity"] \
                                    or not _same_entry(name, instance_fd, meta):
                                raise RuntimeError(f"sealed record changed before capture: {path}")
                        finally:
                            os.close(fd)
                        os.rename(name, qname, src_dir_fd=instance_fd, dst_dir_fd=instance_fd)
                        _killpoint("record-capture:" + name)
                        captured_fd, captured_meta = _open_regular(qname, instance_fd)
                        try:
                            if _blob_identity(_read_fd(captured_fd), object_format) != target["before_identity"] \
                                    or not _same_entry(qname, instance_fd, captured_meta):
                                raise RuntimeError(f"captured record identity mismatch: {path}")
                            os.fchmod(captured_fd, 0o600)
                            os.fsync(captured_fd)
                        finally:
                            os.close(captured_fd)
                        _killpoint("record-verified:" + name)
                        os.unlink(qname, dir_fd=instance_fd)
                        os.fsync(instance_fd)
                        _killpoint("record-delete:" + name)
                        changes.append({"path": path, "operation": "delete",
                                        "before_identity": target["before_identity"],
                                        "after_identity": "absent", "changed_paths": [path]})
                    if os.listdir(instance_fd):
                        raise RuntimeError("the former record directory gained an entry during retirement")
                    if not _same_dir("instance", contracts_fd, instance_fd):
                        raise RuntimeError("the former record directory was swapped during retirement")
                finally:
                    os.close(instance_fd)
                os.rmdir("instance", dir_fd=contracts_fd)
                os.fsync(contracts_fd)
                _killpoint("instance-delete")
            finally:
                os.close(contracts_fd)

            if _OVERRIDES in targets:
                target = targets[_OVERRIDES]
                fd, meta = _open_regular("operator-overrides.json", engine_fd)
                try:
                    original = _read_fd(fd)
                    if _blob_identity(original, object_format) != target["before_identity"] \
                            or not _same_entry("operator-overrides.json", engine_fd, meta):
                        raise RuntimeError("saved settings changed before capture")
                finally:
                    os.close(fd)
                os.rename("operator-overrides.json", _OVERRIDE_Q,
                          src_dir_fd=engine_fd, dst_dir_fd=engine_fd)
                _killpoint("override-capture")
                captured_fd, captured_meta = _open_regular(_OVERRIDE_Q, engine_fd)
                try:
                    captured = _read_fd(captured_fd)
                    if _blob_identity(captured, object_format) != target["before_identity"] \
                            or not _same_entry(_OVERRIDE_Q, engine_fd, captured_meta):
                        raise RuntimeError("captured settings identity mismatch")
                    os.fchmod(captured_fd, 0o600)
                    data = json.loads(captured)
                    if _OVERRIDE_KEY not in data:
                        raise RuntimeError("the sealed top-level setting is no longer present")
                    del data[_OVERRIDE_KEY]
                    replacement = (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
                finally:
                    os.close(captured_fd)
                next_fd = os.open(_OVERRIDE_NEXT, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                  0o600, dir_fd=engine_fd)
                try:
                    _write_all(next_fd, replacement)
                    os.fsync(next_fd)
                finally:
                    os.close(next_fd)
                _killpoint("override-rewrite")
                os.rename(_OVERRIDE_NEXT, "operator-overrides.json",
                          src_dir_fd=engine_fd, dst_dir_fd=engine_fd)
                os.fsync(engine_fd)
                _killpoint("override-replace")
                os.unlink(_OVERRIDE_Q, dir_fd=engine_fd)
                os.fsync(engine_fd)
                _killpoint("override-delete")
                changes.append({"path": _OVERRIDES, "operation": "replace",
                                "before_identity": target["before_identity"],
                                "after_identity": _blob_identity(replacement, object_format),
                                "changed_paths": [_OVERRIDES]})
        finally:
            os.close(engine_fd)
    finally:
        os.close(root_fd)
    return {"status": "applied", "changes": changes}
