#!/usr/bin/env python3
"""Bounded non-sensitive health for accepted-hook qualification and recovery.

This record deliberately lives beside accepted-hook activation in the shared Git common directory, never in
the refused memory target.  A candidate/stale automatic hook cannot therefore turn a qualification failure
into a canonical ledger write.  The shared launcher calls this helper only after it has classified the
accepted dispatcher's fixed refusal prefix, or after an accepted invocation succeeds.

The helper is a diagnostic boundary, not a same-user security boundary.  Its destination and vocabulary are
closed, its free-form inputs are digested, and its JSON is atomically replaced under the accepted metadata
lock primitive.  It never stores hook stderr, ledger content, filesystem paths, credentials, or vault data.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import time

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import moment  # noqa: E402 — the trailing-Z time seam; pure stdlib leaf


SCHEMA_VERSION = "accepted-hook-qualification-health.v1"
RECEIPT_VERSION = "accepted-hook-qualification-receipt.v1"
HEALTH_REL = os.path.join("engine", "accepted-hooks", "qualification-health.json")
LOCK_REL = os.path.join("engine", "accepted-hooks", "qualification-health.lock")
NOTICE_INTERVAL_SECONDS = 6 * 60 * 60
MAX_COUNT = 2**31 - 1
LEGACY_GUIDANCE = (
    "Automatic memory work is being skipped because its accepted code or state binding did not qualify. "
    "Retire or recreate legacy worktrees, restore the accepted activation, then run a qualified hook again."
)
GUIDANCE_BY_REASON = {
    "accepted-dispatch-refused": (
        "Automatic memory work was skipped because this session is not qualified to write canonical memory. "
        "Nothing was changed, and reading and recall are unaffected. Qualification converges on its own at a "
        "session start that can reach GitHub, so this usually clears itself; if it does not, run `uv run "
        "--directory .engine --frozen -- python tools/accepted_hook_dispatch.py ensure --root .. --ambient` "
        "to see exactly what is holding it back."
    ),
    "accepted-dispatcher-absent": (
        "Automatic memory work was skipped because this worktree has no accepted-code dispatcher. Restore the "
        "Engine-managed `.engine/tools/accepted_hook_dispatch.py` file or recreate the worktree, then retry."
    ),
    "accepted-runtime-unavailable": (
        "Automatic memory work was skipped because the Engine's private Python runtime could not start. Repair "
        "the `.engine/.venv` environment for this worktree, then retry the hook."
    ),
}
RECOVERY_GUIDANCE = "The latest automatic memory hook qualified; no repair is currently required."
GUIDANCE = LEGACY_GUIDANCE  # compatibility name for older callers and records
# `engine_status` refuses a guidance sentence longer than this, so a record carrying one is malformed
# rather than merely stale.
MAX_GUIDANCE_CHARS = 900
_OPERATIONS = {
    ".engine/tools/boot.py": "automatic-boot-operation",
    ".engine/tools/close.py": "automatic-close-operation",
    ".engine/tools/memory/compact.py": "automatic-compaction",
    ".engine/tools/memory/erasure_observer.py": "automatic-erasure-observer",
    ".engine/tools/memory/backup_vault.py": "automatic-backup",
}
_REASON_CODES = frozenset({
    "accepted-dispatch-refused", "accepted-dispatcher-absent", "accepted-runtime-unavailable",
})
_HEALTH_KEYS = frozenset({
    "schema_version", "status", "skipped_effect_count", "first_failure_at", "last_failure_at",
    "last_recovery_at", "updated_at", "last_notice_at", "suppressed_notice_count", "guidance",
    "last_failure", "last_qualified", "last_receipt",
})
_RECEIPT_KEYS = frozenset({
    "schema_version", "outcome", "accepted", "target", "effect", "provider",
    "run_identity_digest", "occurred_at", "previous_status", "skipped_effect_count", "receipt_digest",
})
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)


class QualificationHealthError(RuntimeError):
    """The diagnostic record itself could not be safely resolved or updated."""


def _digest(value) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()


def _moment(now: float | None = None) -> str:
    return moment.utc_now() if now is None else moment.to_z(now)


def _parse_moment(value: str | None) -> float | None:
    return moment.epoch(value)


def _git(root: str, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", root, *args], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualificationHealthError("Git identity is unavailable") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise QualificationHealthError("Git identity is unavailable")
    return result.stdout.strip()


def _roots(root: str) -> tuple[str, str]:
    if not isinstance(root, str) or not os.path.isabs(root):
        raise QualificationHealthError("project root must be absolute")
    top = os.path.realpath(_git(root, "rev-parse", "--show-toplevel"))
    if top != os.path.realpath(root):
        raise QualificationHealthError("project root is not its Git top level")
    raw = _git(top, "rev-parse", "--git-common-dir")
    common = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(top, raw))
    if not os.path.isdir(common) or os.path.islink(common):
        raise QualificationHealthError("Git common directory is unsafe")
    return top, common


def health_path(root: str) -> str:
    _top, common = _roots(root)
    return os.path.join(common, HEALTH_REL)


def _valid_digest(value) -> bool:
    return isinstance(value, str) and bool(_DIGEST.fullmatch(value))


def _valid_activation(value) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {"repository", "commit", "tree", "engine_release", "epoch"}:
        return False
    return all(isinstance(value[key], str) and 0 < len(value[key]) <= 160
               for key in ("repository", "commit", "tree", "engine_release")) and (
                   isinstance(value["epoch"], int) and not isinstance(value["epoch"], bool)
                   and 0 <= value["epoch"] <= MAX_COUNT)


def _validate_receipt(value, expected_outcome: str | None = None) -> None:
    if not isinstance(value, dict) or set(value) != _RECEIPT_KEYS:
        raise QualificationHealthError("qualification health record has an invalid receipt")
    outcome = value.get("outcome")
    script = (value.get("effect") or {}).get("script") if isinstance(value.get("effect"), dict) else None
    target = value.get("target")
    effect = value.get("effect")
    supplied_digest = value.get("receipt_digest")
    digest_input = {**value, "receipt_digest": None}
    if (value.get("schema_version") != RECEIPT_VERSION
            or outcome not in {"qualified", "skipped"}
            or (expected_outcome is not None and outcome != expected_outcome)
            or not _valid_activation(value.get("accepted"))
            or not isinstance(target, dict) or set(target) != {"kind", "identity_digest"}
            or target.get("kind") != "canonical" or not _valid_digest(target.get("identity_digest"))
            or not isinstance(effect, dict) or set(effect) != {"operation_id", "script", "cardinality"}
            or script not in _OPERATIONS or effect.get("operation_id") != _OPERATIONS.get(script)
            or effect.get("cardinality") != 1
            or value.get("provider") not in {"claude", "codex"}
            or (value.get("run_identity_digest") is not None
                and not _valid_digest(value.get("run_identity_digest")))
            or _parse_moment(value.get("occurred_at")) is None
            or value.get("previous_status") not in {"unknown", "healthy", "degraded"}
            or not isinstance(value.get("skipped_effect_count"), int)
            or isinstance(value.get("skipped_effect_count"), bool)
            or not 0 <= value["skipped_effect_count"] <= MAX_COUNT
            or not _valid_digest(supplied_digest) or _digest(digest_input) != supplied_digest):
        raise QualificationHealthError("qualification health record has an invalid receipt")


def _validate_failure(value) -> None:
    keys = {"reason_code", "effect", "provider", "run_identity_digest", "accepted", "target"}
    effect = value.get("effect") if isinstance(value, dict) else None
    target = value.get("target") if isinstance(value, dict) else None
    script = effect.get("script") if isinstance(effect, dict) else None
    if (not isinstance(value, dict) or set(value) != keys or value.get("reason_code") not in _REASON_CODES
            or value.get("provider") not in {"claude", "codex"}
            or (value.get("run_identity_digest") is not None
                and not _valid_digest(value.get("run_identity_digest")))
            or not _valid_activation(value.get("accepted"))
            or not isinstance(target, dict) or set(target) != {"kind", "identity_digest"}
            or target.get("kind") != "canonical" or not _valid_digest(target.get("identity_digest"))
            or not isinstance(effect, dict) or set(effect) != {"operation_id", "script", "cardinality"}
            or script not in _OPERATIONS or effect.get("operation_id") != _OPERATIONS.get(script)
            or effect.get("cardinality") != 1):
        raise QualificationHealthError("qualification health record has an invalid failure summary")


def _derived_guidance(value: dict) -> str:
    """The operator-facing sentence for an already-validated record. The single source for both paths."""
    if value.get("status") != "degraded":
        return RECOVERY_GUIDANCE
    failure = value.get("last_failure")
    reason = failure.get("reason_code") if isinstance(failure, dict) else None
    return GUIDANCE_BY_REASON.get(reason, LEGACY_GUIDANCE)


def _read_path(path: str) -> dict | None:
    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
            raise QualificationHealthError("qualification health record is unsafe")
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return None
    except QualificationHealthError:
        raise
    except (OSError, ValueError) as exc:
        raise QualificationHealthError("qualification health record is unreadable") from exc
    if (not isinstance(value, dict) or set(value) != _HEALTH_KEYS
            or value.get("schema_version") != SCHEMA_VERSION):
        raise QualificationHealthError("qualification health record has an unsupported schema")
    if value.get("status") not in {"healthy", "degraded"}:
        raise QualificationHealthError("qualification health record has an invalid status")
    for key in ("skipped_effect_count", "suppressed_notice_count"):
        item = value.get(key)
        if not isinstance(item, int) or isinstance(item, bool) or not 0 <= item <= MAX_COUNT:
            raise QualificationHealthError("qualification health record has an invalid bounded count")
    for key in ("first_failure_at", "last_failure_at", "last_recovery_at", "updated_at", "last_notice_at"):
        item = value.get(key)
        if item is not None and (_parse_moment(item) is None or len(item) > 32):
            raise QualificationHealthError("qualification health record has an invalid timestamp")
    guidance = value.get("guidance")
    if not isinstance(guidance, str) or len(guidance) > MAX_GUIDANCE_CHARS:
        raise QualificationHealthError("qualification health record has an invalid guidance value")
    receipt = value.get("last_receipt")
    _validate_receipt(receipt, "skipped" if value["status"] == "degraded" else "qualified")
    if value.get("last_qualified") is not None:
        _validate_receipt(value["last_qualified"], "qualified")
    if value.get("last_failure") is not None:
        _validate_failure(value["last_failure"])
    if (receipt["skipped_effect_count"] != value["skipped_effect_count"]
            or receipt["occurred_at"] != value["updated_at"]
            or (value["status"] == "degraded"
                and (value.get("last_failure") is None or value.get("last_failure_at") != value["updated_at"]))):
        raise QualificationHealthError("qualification health record has inconsistent bounded state")
    # The stored sentence is derived, never authoritative: recompute it from the closed sets this
    # function has already validated (`status`, and `last_failure.reason_code` against _REASON_CODES).
    # Comparing it against the current wording instead is what wedged a real machine — RL3 reworded one
    # entry mid-build and every record written before that became permanently unreadable, which silences
    # the very channel that reports skipped memory work. Deriving also means no text a damaged or hostile
    # record carries can reach the operator, which exact-match comparison allowed as an equal outcome.
    value["guidance"] = _derived_guidance(value)
    return value


def read(root: str) -> dict | None:
    return _read_path(health_path(root))


def _load_dispatcher(root: str):
    path = os.path.join(root, ".engine", "tools", "accepted_hook_dispatch.py")
    try:
        spec = importlib.util.spec_from_file_location("_engine_qualification_health_atomic", path)
        if spec is None or spec.loader is None:
            raise QualificationHealthError("accepted metadata primitive is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    except QualificationHealthError:
        raise
    except Exception as exc:  # the automatic path emits one bounded diagnostic, never an import traceback
        raise QualificationHealthError("accepted metadata primitive is unavailable") from exc
    if os.path.realpath(getattr(module, "__file__", "")) != os.path.realpath(path):
        raise QualificationHealthError("accepted metadata primitive escaped the project")
    return module


def _activation(common: str) -> dict | None:
    path = os.path.join(common, "engine", "accepted-hooks", "activation.json")
    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 32 * 1024:
            return None
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ("repository", "commit", "tree", "engine_release", "epoch"):
        item = value.get(key)
        if not isinstance(item, (str, int)) or isinstance(item, bool) or len(str(item)) > 160:
            return None
        result[key] = item
    return result if _valid_activation(result) else None


def _receipt(*, outcome: str, script: str, provider: str, run_id: str | None, common: str,
             occurred_at: str, previous_status: str, skipped_effect_count: int) -> dict:
    operation = _OPERATIONS.get(script)
    if operation is None:
        raise QualificationHealthError("unregistered automatic script")
    if provider not in {"claude", "codex"}:
        raise QualificationHealthError("unknown provider")
    receipt = {
        "schema_version": RECEIPT_VERSION,
        "outcome": outcome,
        "accepted": _activation(common),
        "target": {"kind": "canonical", "identity_digest": _digest(common + ":canonical-memory")},
        "effect": {"operation_id": operation, "script": script, "cardinality": 1},
        "provider": provider,
        "run_identity_digest": _digest(run_id) if isinstance(run_id, str) and run_id else None,
        "occurred_at": occurred_at,
        "previous_status": previous_status,
        "skipped_effect_count": skipped_effect_count,
        "receipt_digest": None,
    }
    receipt["receipt_digest"] = _digest(receipt)
    return receipt


def update(root: str, *, outcome: str, script: str, provider: str, run_id: str | None = None,
           reason_code: str | None = None, now: float | None = None) -> dict:
    """Atomically record one qualified or skipped automatic effect and return its non-sensitive receipt."""
    if outcome not in {"qualified", "skipped"}:
        raise QualificationHealthError("qualification outcome is invalid")
    if outcome == "skipped" and reason_code not in _REASON_CODES:
        raise QualificationHealthError("qualification reason code is invalid")
    top, common = _roots(root)
    path = os.path.join(common, HEALTH_REL)
    lock = os.path.join(common, LOCK_REL)
    dispatcher = _load_dispatcher(top)
    occurred_at = _moment(now)
    try:
        with dispatcher._exclusive_lock(lock):
            previous = _read_path(path)
            previous_status = previous.get("status", "unknown") if previous else "unknown"
            count = previous.get("skipped_effect_count", 0) if previous else 0
            count = min(MAX_COUNT, count + (1 if outcome == "skipped" else 0))
            receipt = _receipt(
                outcome=outcome, script=script, provider=provider, run_id=run_id, common=common,
                occurred_at=occurred_at, previous_status=previous_status, skipped_effect_count=count,
            )
            record = {
                "schema_version": SCHEMA_VERSION,
                "status": "degraded" if outcome == "skipped" else "healthy",
                "skipped_effect_count": count,
                "first_failure_at": previous.get("first_failure_at") if previous else None,
                "last_failure_at": previous.get("last_failure_at") if previous else None,
                "last_recovery_at": previous.get("last_recovery_at") if previous else None,
                "updated_at": occurred_at,
                "last_notice_at": previous.get("last_notice_at") if previous else None,
                "suppressed_notice_count": previous.get("suppressed_notice_count", 0) if previous else 0,
                # Placeholder: `_derived_guidance` sets the real sentence below, once `last_failure`
                # exists, so the write and read paths cannot drift apart.
                "guidance": RECOVERY_GUIDANCE,
                "last_failure": previous.get("last_failure") if previous else None,
                "last_qualified": previous.get("last_qualified") if previous else None,
                "last_receipt": receipt,
            }
            emit_notice = False
            if outcome == "skipped":
                record["first_failure_at"] = record["first_failure_at"] or occurred_at
                record["last_failure_at"] = occurred_at
                record["last_failure"] = {
                    "reason_code": reason_code, "effect": receipt["effect"], "provider": provider,
                    "run_identity_digest": receipt["run_identity_digest"], "accepted": receipt["accepted"],
                    "target": receipt["target"],
                }
                prior_notice = _parse_moment(record["last_notice_at"])
                current = time.time() if now is None else now
                emit_notice = prior_notice is None or current - prior_notice >= NOTICE_INTERVAL_SECONDS
                if emit_notice:
                    record["last_notice_at"] = occurred_at
                else:
                    record["suppressed_notice_count"] = min(
                        MAX_COUNT, record["suppressed_notice_count"] + 1)
            else:
                record["last_qualified"] = receipt
                if previous_status == "degraded":
                    record["last_recovery_at"] = occurred_at
            record["guidance"] = _derived_guidance(record)
            dispatcher._atomic_json(path, record)
    except QualificationHealthError:
        raise
    except Exception as exc:
        raise QualificationHealthError("qualification health record could not be atomically updated") from exc
    return {"record": record, "receipt": receipt, "emit_notice": emit_notice}


def _provider_and_session():
    tools = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import providers
    return providers.detect(), providers.resolve_session()


def _fixture_self_test() -> dict:
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory(prefix="engine-qualification-health-") as temporary:
        root = pathlib.Path(temporary) / "project with spaces"
        root.mkdir()
        for command in (
            ("git", "init", "-b", "main", str(root)),
            ("git", "-C", str(root), "config", "user.name", "Health Fixture"),
            ("git", "-C", str(root), "config", "user.email", "fixture@example.invalid"),
        ):
            subprocess.run(command, check=True, capture_output=True)
        tools = root / ".engine/tools"
        tools.mkdir(parents=True)
        source = pathlib.Path(__file__).resolve().parents[1] / "accepted_hook_dispatch.py"
        (tools / "accepted_hook_dispatch.py").write_bytes(source.read_bytes())
        common = pathlib.Path(_roots(str(root))[1])
        activation = common / "engine/accepted-hooks/activation.json"
        activation.parent.mkdir(parents=True)
        activation.write_text(json.dumps({
            "repository": "owner/repo", "commit": "a" * 40, "tree": "b" * 40,
            "engine_release": "9.9.9", "epoch": 1,
        }), encoding="utf-8")
        first = update(
            str(root), outcome="skipped", script=".engine/tools/memory/compact.py", provider="codex",
            run_id="secret-session", reason_code="accepted-dispatch-refused", now=1_800_000_000)
        second = update(
            str(root), outcome="skipped", script=".engine/tools/memory/compact.py", provider="codex",
            run_id="secret-session", reason_code="accepted-dispatch-refused", now=1_800_000_001)
        recovered = update(
            str(root), outcome="qualified", script=".engine/tools/memory/compact.py", provider="codex",
            run_id="secret-session", now=1_800_000_002)
        value = read(str(root))
        if not (first["emit_notice"] and not second["emit_notice"] and value == recovered["record"]
                and value["status"] == "healthy" and value["skipped_effect_count"] == 2
                and value["suppressed_notice_count"] == 1 and value["last_recovery_at"]
                and "secret-session" not in json.dumps(value)):
            raise AssertionError("qualification-health transition or privacy contract failed")
        return {
            "first_notice": first["emit_notice"], "recurring_notice_suppressed": True,
            "skipped_effect_count": value["skipped_effect_count"], "recovered": True,
            "session_identity_hashed": True, "bounded_bytes": os.path.getsize(health_path(str(root))),
        }


def main(argv: list[str]) -> int:
    if argv == ["self-test"]:
        print(json.dumps(_fixture_self_test(), sort_keys=True))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outcome", choices=("qualified", "skipped"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--script", required=True, choices=sorted(_OPERATIONS))
    parser.add_argument("--reason-code", choices=sorted(_REASON_CODES))
    parser.add_argument("--receipt", action="store_true")
    args = parser.parse_args(argv)
    try:
        provider, run_id = _provider_and_session()
        result = update(
            args.root, outcome=args.outcome, script=args.script, provider=provider, run_id=run_id,
            reason_code=args.reason_code,
        )
    except QualificationHealthError as exc:
        print(f"Engine qualification health could not be updated: {exc}.", file=sys.stderr)
        return 1
    except Exception:
        print("Engine qualification health could not be updated: provider context is unavailable.",
              file=sys.stderr)
        return 1
    if result["emit_notice"]:
        print(result["record"]["guidance"], file=sys.stderr)
    if args.receipt:
        print(json.dumps(result["receipt"], sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
