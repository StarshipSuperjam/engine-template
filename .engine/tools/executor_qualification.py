#!/usr/bin/env python3
"""The executor-qualification harness — drives a provider-neutral BuildExecutionRunner through a fixed set of
probe families and composes the observations into an executor-attempt-receipt.v1 record.

THIS HARNESS OBSERVES; IT DOES NOT ENFORCE. Every containment-shaped field this module produces is recorded
as OBSERVED SELF-CONTAINMENT — the candidate's own behavior, watched from outside — and never as Engine
enforcement. No OS-level sandbox, container, chroot, or filesystem jail is established anywhere in this
module; ``containment_observation['enforcement']`` is always ``False`` and every docstring below says so
again where it would otherwise be easy to misread a passing observation as a guarantee.

This harness does NOT claim proof of genuine tool execution by the candidate it drives —
``tool_execution_proof`` is pinned ``False`` on every composed receipt. Whether a candidate's tool calls did
what they claim is open ground (issue StarshipSuperjam/engine-template#1021); a probe here records what was
OBSERVED (a diff was produced, an update stream was read, a marker file appeared and was cleaned up), never
what was proven.

The environment witness this harness assembles states NON-PROVISION of a credential by the Engine — the
allowlisted child environment held nothing beyond the named keep-list — never that a credential is
UNREACHABLE, since publish authority remains reachable on disk regardless of what the child process's
environment holds.

Every probe is written against ``acp_client.BuildExecutionRunner`` — the ABSTRACT seam — never against the
concrete ``AcpClient``. That keeps this module transport-neutral (any implementation of the seam can be
qualified) and lets ``test_executor_qualification.py`` drive it hermetically with an in-test fake runner: no
network, no real coding agent, no real git repository beyond a throwaway one a test creates itself.

The completion-attempt probe replays a node payload and embeds its canonical-JSON sha256 digest
(``replay.node_payload_digest``) so the observation is checkable off this workstation. A real qualification
run replays a SEALED HISTORICAL node through the coordinator's state-file mechanism; CI and this module's own
tests replay a small FIXTURE node payload instead — both paths produce the same digest shape, only the
provenance (``replay.source``) differs.

Transcripts and other captured evidence are persisted as an identity plus a sha256 digest, NEVER as raw
content — ``persist_evidence`` runs a secret-scan/redaction pass over anything it writes first, so a captured
transcript that happens to contain a credential-shaped string is redacted before it ever reaches disk.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Callable, Optional

from acp_client import BuildExecutionRunner  # noqa: F401  (the abstract seam probes are typed against)
import build_coordinator_work as work
import execution_env_policy

SCHEMA_VERSION = "executor-attempt-receipt.v1"
_HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.normpath(os.path.join(_HERE, "..", "schemas", "executor-attempt-receipt.v1.json"))


class QualificationError(RuntimeError):
    """Raised loudly on any qualification-harness refusal: an identity inconsistency, a receipt that fails
    schema validation, or an escape probe whose cleanup could not be verified. Never swallowed into a
    default — a refusal here must stop the attempt, not silently degrade its evidence."""


# ---------------------------------------------------------------------------------------------------------
# Canonicalization / digests
# ---------------------------------------------------------------------------------------------------------

def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def node_payload_digest(payload: dict) -> str:
    """The sha256 digest over the canonical JSON of a replayed node payload — embedded in the receipt's
    ``replay.node_payload_digest`` so the completion-attempt observation is checkable off this workstation,
    independent of whether the replay came from a sealed historical node or a CI fixture."""
    return _sha256_text(_canonical_json(payload))


# ---------------------------------------------------------------------------------------------------------
# Probe families — each drives `runner: BuildExecutionRunner` (the abstract seam), never a concrete client.
# ---------------------------------------------------------------------------------------------------------

def probe_negotiation(runner: BuildExecutionRunner) -> dict:
    """Negotiation probe: open a session and record what came back. Raises whatever ``start_session()``
    raises — a negotiation failure is itself an observation the caller must see, not one this probe hides."""
    session_id = runner.start_session()
    return {"session_id": session_id, "started": bool(session_id)}


def probe_completion_attempt(runner: BuildExecutionRunner, node_payload: dict, *,
                             source: str = "fixture") -> dict:
    """Completion-attempt probe: prompt the runner with a replayed node payload and record how many updates
    it streamed back. ``source`` names where the payload came from (``'fixture'`` in CI, or
    ``'sealed-historical-node'`` for a real qualification run) and rides along in the returned observation so
    the receipt's ``replay.source`` is honest about provenance."""
    if source not in ("fixture", "sealed-historical-node"):
        raise QualificationError(f"unknown replay source {source!r}")
    digest = node_payload_digest(node_payload)
    runner.prompt(_canonical_json(node_payload))
    updates = runner.updates()
    return {"node_payload_digest": digest, "source": source, "updates_observed": len(updates)}


def probe_cancellation(runner: BuildExecutionRunner) -> dict:
    """Cancellation-acknowledgment probe: request cancellation and record HONESTLY whether the candidate
    acknowledged it — a request is not itself an acknowledgement, and this probe never assumes one."""
    acknowledged = runner.cancel()
    return {"requested": True, "acknowledged": bool(acknowledged)}


def probe_malformed_updates(runner: BuildExecutionRunner) -> dict:
    """Malformed/partial-event probe: read the transcript so far and count updates the runner itself
    recorded as ``malformed`` or ``partial`` rather than crashing on. This probe never raises on a malformed
    update — surfacing it as evidence IS the point."""
    updates = runner.updates()
    flagged = [u for u in updates if isinstance(u, dict) and u.get("kind") in ("malformed", "partial")]
    return {"count": len(flagged), "updates": flagged}


def probe_escape(runner: BuildExecutionRunner, allowed_dir: str, *,
                 marker_name: str = "qualification-escape-probe.marker") -> dict:
    """Escape probe, restricted to a pre-declared INERT payload against a NAMED allowed target, with
    post-probe cleanup VERIFIED. The probe writes one known marker file to ``allowed_dir`` (never anything
    outside it, never anything that does real harm) and confirms it is gone afterward. A cleanup that cannot
    be verified raises ``QualificationError`` rather than being reported as clean."""
    target_path = os.path.join(allowed_dir, marker_name)
    with open(target_path, "w", encoding="utf-8") as fh:
        fh.write("inert-qualification-escape-probe-marker\n")
    written = os.path.isfile(target_path)
    os.remove(target_path)
    cleanup_verified = not os.path.exists(target_path)
    if not cleanup_verified:
        raise QualificationError(f"escape-probe cleanup could not be verified for {target_path!r}")
    return {"target_path": target_path, "marker_written": written, "cleanup_verified": cleanup_verified}


def probe_process_kill_recovery(runner: BuildExecutionRunner, kill_fn: Callable[[], None]) -> dict:
    """Process-kill recovery probe: the CALLER'S ``kill_fn`` actually kills the runner's supervised process
    (real or fake); this probe only observes, through the runner's own seam, whether the loss was detected
    and the tree was reaped. ``close()`` is called here, so this probe also tears the runner down."""
    kill_fn()
    loss_detected = runner.process_lost()
    witness = runner.close()
    reaped = bool(witness.get("group_reaped") or witness.get("tree_reaped") or witness.get("leader_exited"))
    return {"loss_detected": bool(loss_detected), "reaped": reaped}


# ---------------------------------------------------------------------------------------------------------
# Detection instruments — observation only, never enforcement.
# ---------------------------------------------------------------------------------------------------------

def snapshot(monitored_paths) -> dict:
    """A pre/post snapshot of a monitored set of out-of-workspace paths: for each path, the sha256 digest of
    its bytes if it is a regular file, or ``None`` if it is absent. Pure observation — this instrument never
    creates, modifies, or removes anything."""
    result = {}
    for path in monitored_paths:
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                result[path] = "sha256:" + hashlib.sha256(fh.read()).hexdigest()
        else:
            result[path] = None
    return result


def capture_process_tree(pid: Optional[int]) -> dict:
    """A minimal process-tree capture: whether ``pid`` is currently alive, via a signal-0 probe. Observation
    only — this never signals the process to change state (signal 0 is a liveness check, not a kill)."""
    if pid is None:
        return {"pid": None, "alive": False}
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    except PermissionError:
        alive = True  # exists, just not signalable by us — still alive
    return {"pid": pid, "alive": alive}


def diff_snapshots(pre: dict, post: dict, *, monitored_paths=None) -> dict:
    """Diff a pre/post ``snapshot()`` pair and record the delta as OBSERVED SELF-CONTAINMENT.
    ``enforcement`` is always ``False`` here: no OS-level isolation was established by this harness, so a
    clean (no-delta) result is evidence of the candidate's own behavior, never of an enforced boundary."""
    paths = list(monitored_paths) if monitored_paths is not None else sorted(set(pre) | set(post))
    changed = [p for p in paths if pre.get(p) != post.get(p)]
    return {
        "enforcement": False,
        "monitored_paths": paths,
        "delta_observed": bool(changed),
        "changed_paths": changed,
    }


def environment_witness(child_env: dict, allowlist, *, source: dict,
                        authentication_keep_list=None) -> dict:
    """The allowlist-environment witness for a supervised child. Reuses
    ``execution_env_policy.allowlist_environment`` to compute what the child environment SHOULD be from
    ``source`` and the named ``allowlist``, then compares it to the ``child_env`` actually used. States
    NON-PROVISION of a credential by the Engine — never unreachability, since publish authority remains
    reachable on disk regardless of what the child's environment holds."""
    allowlist = list(allowlist)
    expected = execution_env_policy.allowlist_environment(allowlist, source=source)
    return {
        "child_env_equals_allowlist": child_env == expected,
        "allowlist_keys": sorted(allowlist),
        "authentication_keep_list": sorted(authentication_keep_list or []),
        "credential_non_provision": (
            "The Engine supplied the child process only the named allowlist keys present in source; this "
            "states NON-PROVISION of any credential beyond that keep-list, never that a credential is "
            "unreachable — publish authority remains reachable on disk regardless of the child environment."
        ),
    }


# ---------------------------------------------------------------------------------------------------------
# Secret scan / redaction — runs over anything persisted, before it is written.
# ---------------------------------------------------------------------------------------------------------

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


def secret_scan(text: str) -> list:
    """Findings of obvious secret shapes in ``text`` — an API-key-looking token, a GitHub PAT, a bearer
    token, or a PEM private-key block. Each finding names the pattern that matched and a redacted preview,
    never the raw secret text."""
    findings = []
    for pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(0)
            preview = raw[:4] + "…REDACTED…" if len(raw) > 4 else "REDACTED"
            findings.append({"pattern": pattern.pattern, "preview": preview})
    return findings


def redact(text: str) -> tuple:
    """Redact every secret-shaped span in ``text``. Returns ``(redacted_text, findings)``; ``findings`` is
    the same list ``secret_scan`` would return, computed against the ORIGINAL text before redaction."""
    findings = secret_scan(text)
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted, findings


def persist_evidence(observations: dict, record_home: str) -> tuple:
    """Write ``observations`` (a name -> JSON-serializable-or-string value mapping) into ``record_home``
    BEFORE any teardown, one file per entry. Every value is redacted with ``redact()`` first, so a captured
    transcript containing a secret-shaped string is never written raw. Returns
    ``(evidence_refs, secret_findings)`` — ``evidence_refs`` is a list of ``{kind, name, digest}`` dicts
    whose digest matches the PERSISTED (redacted) bytes exactly, so a caller can verify the ref against the
    file on disk; ``secret_findings`` is a ``{name: [finding, ...]}`` mapping of anything that was redacted."""
    os.makedirs(record_home, exist_ok=True)
    evidence_refs = []
    secret_findings = {}
    for name, value in observations.items():
        text = value if isinstance(value, str) else _canonical_json(value)
        redacted_text, findings = redact(text)
        if findings:
            secret_findings[name] = findings
        path = os.path.join(record_home, f"{name}.evidence")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(redacted_text)
        evidence_refs.append({"kind": "evidence", "name": name, "digest": _sha256_text(redacted_text)})
    return evidence_refs, secret_findings


# ---------------------------------------------------------------------------------------------------------
# Full probe orchestration
# ---------------------------------------------------------------------------------------------------------

def run_probe_suite(runner: BuildExecutionRunner, node_payload: dict, *, allowed_escape_dir: str,
                    monitored_paths, kill_fn: Optional[Callable[[], None]] = None,
                    source: str = "fixture") -> dict:
    """Drive the full probe family set against ``runner`` and return structured observations. Never composes
    or persists a receipt itself — callers pass the returned dict's pieces into
    ``compose_attempt_receipt``/``persist_evidence`` as they see fit. Order: negotiate, attempt completion,
    read malformed-update evidence, request cancellation, run the escape probe, snapshot-diff, then —
    only if ``kill_fn`` is given — the process-kill recovery probe (which also tears the runner down)."""
    pre = snapshot(monitored_paths)
    negotiation = probe_negotiation(runner)
    completion = probe_completion_attempt(runner, node_payload, source=source)
    malformed = probe_malformed_updates(runner)
    cancellation = probe_cancellation(runner)
    escape = probe_escape(runner, allowed_escape_dir)
    post = snapshot(monitored_paths)
    containment = diff_snapshots(pre, post, monitored_paths=monitored_paths)
    containment["escape_probe"] = escape

    kill_result = None
    if kill_fn is not None:
        kill_result = probe_process_kill_recovery(runner, kill_fn)

    return {
        "negotiation": negotiation,
        "replay": {"node_payload_digest": completion["node_payload_digest"], "source": completion["source"]},
        "malformed_updates_observed": malformed["count"],
        "malformed_updates": malformed["updates"],
        "cancellation": cancellation,
        "containment_observation": containment,
        "process_kill_recovery": kill_result,
    }


# ---------------------------------------------------------------------------------------------------------
# Receipt composition
# ---------------------------------------------------------------------------------------------------------

def _load_schema() -> dict:
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def validate_attempt_receipt(receipt: dict) -> None:
    """Validate ``receipt`` against executor-attempt-receipt.v1; raise ``QualificationError`` on any
    violation. The embedded ``integration_receipt`` was already checked by
    ``build_coordinator_work.validate_receipt`` inside ``assemble_receipt``; this call additionally checks
    the whole composed document against this module's own schema."""
    from jsonschema import Draft202012Validator  # lazy: tool-runtime dep
    schema = _load_schema()
    errors = sorted(Draft202012Validator(schema).iter_errors(receipt), key=lambda e: list(e.path))
    if errors:
        joined = "; ".join(
            f"{'/'.join(str(p) for p in e.path) or '(root)'}: {e.message}" for e in errors)
        raise QualificationError(f"executor attempt receipt does not satisfy {SCHEMA_VERSION}: {joined}")


def _refuse_identity_inconsistency(bridge_identity: dict, vendored_agent_identity: dict) -> None:
    for label, identity in (("bridge_identity", bridge_identity),
                            ("vendored_agent_identity", vendored_agent_identity)):
        if not (isinstance(identity, dict) and identity.get("name")
                and identity.get("version") and identity.get("digest")):
            raise QualificationError(
                f"{label} is missing or incomplete; refusing rather than composing a receipt with an "
                "unidentified component")
    if bridge_identity["digest"] == vendored_agent_identity["digest"]:
        raise QualificationError(
            "bridge_identity and vendored_agent_identity carry the SAME digest; a bridge and the vendored "
            "agent it wraps are separate components and must never be identity-confused with one another")


def compose_attempt_receipt(*, git_facts: dict, claim_base: str, integration_commit: str,
                            identity_mode: str, sibling_attributions: list,
                            protocol: dict, process: dict, configuration_as_reported: dict,
                            bridge_identity: dict, vendored_agent_identity: dict,
                            evidence_refs: list,
                            containment_observation: Optional[dict] = None,
                            environment_witness_value: Optional[dict] = None,
                            replay: Optional[dict] = None,
                            cancellation: Optional[dict] = None,
                            process_kill_recovery: Optional[dict] = None,
                            malformed_updates_observed: Optional[int] = None,
                            notes: Optional[str] = None) -> dict:
    """Compose one executor-attempt-receipt.v1 dict.

    The embedded ``integration_receipt`` comes from the REAL
    ``build_coordinator_work.assemble_receipt(git_facts, claim_base, integration_commit, identity_mode,
    sibling_attributions)`` — never reimplemented here — which itself validates via
    ``build_coordinator_work.validate_receipt`` before returning. ``tool_execution_proof`` is always pinned
    ``False``. Raises ``QualificationError`` LOUDLY on an identity inconsistency (a missing bridge/vendored
    identity, or the two sharing a digest) before ever assembling the receipt, and again if the assembled
    document fails schema validation."""
    _refuse_identity_inconsistency(bridge_identity, vendored_agent_identity)

    integration_receipt = work.assemble_receipt(
        git_facts, claim_base, integration_commit, identity_mode, sibling_attributions)

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "integration_receipt": integration_receipt,
        "protocol": protocol,
        "process": process,
        "configuration_as_reported": configuration_as_reported,
        "bridge_identity": bridge_identity,
        "vendored_agent_identity": vendored_agent_identity,
        "tool_execution_proof": False,
        "evidence_refs": list(evidence_refs),
    }
    optional = {
        "containment_observation": containment_observation,
        "environment_witness": environment_witness_value,
        "replay": replay,
        "cancellation": cancellation,
        "process_kill_recovery": process_kill_recovery,
        "malformed_updates_observed": malformed_updates_observed,
        "notes": notes,
    }
    for key, value in optional.items():
        if value is not None:
            receipt[key] = value

    validate_attempt_receipt(receipt)
    return receipt
