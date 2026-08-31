#!/usr/bin/env python3
"""Executor bridge acquisition — pin enforcement, out-of-band digest verification, and identity
separation for acquiring an external ACP bridge package (and the agent it vendors) BEFORE it is ever
qualified or run.

This module is GENERIC: it is driven by a spec (an exact ``name@version``) and a cache directory, not
hardcoded to any one bridge. Its shape serves both candidate BRIDGE packages the Engine expects to
acquire later — ``@agentclientprotocol/claude-agent-acp`` (which vendors its agent inside its
``@anthropic-ai/claude-agent-sdk`` dependency) and ``@agentclientprotocol/codex-acp`` (which spawns a
vendored ``@openai/codex`` binary from its dependency tree) — but this module never names either one.

This tool builds ACQUISITION only. It does not qualify, run, or grant eligibility to anything it
acquires — that is a separate, later, attended step. Its pure logic (pinning, digesting, identity
separation, credential-non-provision) is unit-testable entirely against local fixtures: the only code
path that would ever touch a network is the default ``runner``, and every test injects a fake one.

Registry / package-metadata note: wherever this module reads ``package.json`` or other registry-adjacent
metadata, that read is DISCOVERY and LAUNCH metadata ONLY — it identifies what was acquired. It is never,
by itself, a qualification or certification of the acquired package; qualification is a separate,
evidenced decision made elsewhere (see executor_records.py).

No credential handling: this module never reads, copies, opens, or passes through any credential store
(no ``~/.npmrc`` auth token, no ``~/.claude``, no OS keychain, no ``NPM_TOKEN`` / ``ANTHROPIC_API_KEY`` /
similar environment variable). ``credential_non_provision_witness`` inspects a caller-supplied
environment/argument mapping and witnesses that the Engine did not SUPPLY a credential through this
tool's own call — it does not and cannot claim credentials are unreachable elsewhere on the machine.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess

REGISTRY_PRESENCE_IS_DISCOVERY_ONLY = (
    "Registry/package-metadata presence (e.g. that a package.json exists, or what a registry reports "
    "about a spec) is DISCOVERY and LAUNCH metadata ONLY. It identifies what would be acquired or was "
    "acquired. It is never, by itself, a qualification or certification of the package's behavior."
)

# Credential-bearing environment keys this tool refuses to read, copy, or pass through. Not exhaustive
# of every possible secret name on every machine -- it is the caller-controlled allowlist boundary this
# tool checks against, kept short and legible rather than an attempt at universal secret detection.
_CREDENTIAL_ENV_KEYS = frozenset({
    "NPM_TOKEN",
    "npm_config__auth",
    "npm_config__authtoken",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "NPM_AUTH_TOKEN",
    "NODE_AUTH_TOKEN",
})

# A pinned spec's version segment: an exact concrete semver, optionally with a prerelease/build suffix,
# but no range operators, wildcards, or dist-tags.
_SEMVER_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


class AcquisitionError(ValueError):
    """Acquisition refused: an unpinned spec, a digest mismatch, or a malformed fixture/tree. Always
    raised loudly -- never swallowed into a partial or best-effort acquisition."""


def is_pinned(spec: str) -> bool:
    """True only for an exact ``name@version`` spec with a concrete semver version.

    Scoped names (a leading ``@``, exactly one ``/``) are valid; the version is the segment after the
    LAST ``@`` in the spec (so a scoped name's own leading ``@`` is not mistaken for the version
    separator). Refuses: no version, a range (``^1.0.0``, ``~1.2``, ``>=1``, ``1.x``), a dist-tag
    (``latest``, ``next``, ``beta``), or a URL/git spec (contains ``://``, starts with ``git+``, or
    otherwise is not a plain name@version pair).
    """
    if not isinstance(spec, str) or not spec:
        return False
    if "://" in spec or spec.startswith("git+") or spec.startswith("git:"):
        return False
    last_at = spec.rfind("@")
    if last_at <= 0:
        # No '@' at all, or the only '@' is a scope marker at index 0 -- no version given.
        return False
    name = spec[:last_at]
    version = spec[last_at + 1:]
    if not name or not version:
        return False
    if name.startswith("@"):
        if name.count("/") != 1:
            return False
    elif "@" in name:
        return False
    if "/" in version:
        return False
    return bool(_SEMVER_RE.match(version))


def require_pinned(spec: str) -> None:
    """Raise ``AcquisitionError`` unless ``spec`` is an exact pinned ``name@version``."""
    if not is_pinned(spec):
        raise AcquisitionError(
            f"refusing unpinned executor spec {spec!r}: acquisition requires an exact name@version "
            "with a concrete semver -- no range, dist-tag, or URL/git spec is accepted"
        )


def tree_digest(path: str) -> str:
    """Compute a reproducible ``sha256:<hex>`` digest over a directory tree.

    Walks every file under ``path``, sorted by relpath for determinism, and folds each file's relpath
    plus its bytes into one sha256 hash. Two byte-identical trees produce the same digest regardless of
    walk order or the host OS; changing any file's bytes, adding a file, or removing one changes the
    digest.
    """
    if not os.path.isdir(path):
        raise AcquisitionError(f"cannot digest {path!r}: not a directory")
    hasher = hashlib.sha256()
    relpaths = []
    for root, _dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            relpaths.append(os.path.relpath(full, path))
    for rel in sorted(relpaths):
        # Normalize separators so the digest is stable across platforms.
        norm_rel = rel.replace(os.sep, "/")
        with open(os.path.join(path, rel), "rb") as fh:
            data = fh.read()
        hasher.update(norm_rel.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(hashlib.sha256(data).digest())
    return "sha256:" + hasher.hexdigest()


def verify_digest(path: str, expected: str) -> None:
    """Raise ``AcquisitionError`` unless ``tree_digest(path) == expected``.

    ``expected`` must come from out-of-band record-keeping fixed BEFORE acquisition -- never derived
    from the tree this call is about to verify, which would verify nothing.
    """
    actual = tree_digest(path)
    if actual != expected:
        raise AcquisitionError(
            f"digest mismatch at {path!r}: expected {expected}, got {actual} -- refusing to treat "
            "this acquisition as usable"
        )


def _default_runner(spec: str, cache_dir: str, *, env: dict) -> None:
    """The ONLY code path in this module that would touch the network or invoke a real package
    manager. Installs ``spec`` into ``cache_dir`` with npm lifecycle scripts disabled. Every test in
    this module injects a fake ``runner`` instead of calling this function, so no test can reach real
    npm."""
    os.makedirs(cache_dir, exist_ok=True)
    # --ignore-scripts disables every lifecycle hook (the supply-chain teeth); a plain install writes
    # package-lock.json into the prefix, which is the lockfile the acquisition record wants. The env is the
    # caller's allowlist (never os.environ), so no credential is smuggled in.
    subprocess.run(
        ["npm", "install", spec, "--prefix", cache_dir,
         "--ignore-scripts", "--no-audit", "--no-fund"],
        check=True,
        env=env,
        cwd=cache_dir,
    )


def credential_non_provision_witness(env: dict) -> dict:
    """Inspect a caller-supplied environment/argument mapping and witness that no known
    credential-bearing key is present in it.

    Honesty rule: this witnesses NON-PROVISION -- that the Engine did not hand a credential to this
    tool's own call through ``env``. It does NOT and cannot claim credentials are unreachable on the
    machine (a real ``~/.npmrc`` or OS keychain entry may still exist outside this call's view); this
    tool simply never reads, copies, or passes one through.
    """
    if not isinstance(env, dict):
        raise AcquisitionError("credential_non_provision_witness requires a mapping")
    present = sorted(k for k in env if k in _CREDENTIAL_ENV_KEYS)
    return {"credential_keys_present": present, "non_provision": len(present) == 0}


def _read_package_json(pkg_dir: str) -> dict:
    """DISCOVERY ONLY -- see REGISTRY_PRESENCE_IS_DISCOVERY_ONLY. Reads name/version from a
    package.json if present; returns {} if absent so callers can degrade gracefully rather than crash
    on a fixture missing metadata."""
    pkg_json = os.path.join(pkg_dir, "package.json")
    if not os.path.isfile(pkg_json):
        return {}
    with open(pkg_json, encoding="utf-8") as fh:
        data = json.load(fh)
    return {"name": data.get("name"), "version": data.get("version")}


def _package_identity(pkg_dir: str) -> dict:
    meta = _read_package_json(pkg_dir)
    return {
        "name": meta.get("name"),
        "version": meta.get("version"),
        "digest": tree_digest(pkg_dir),
    }


def identify_vendored_agent(bridge_root: str, *, vendor_subpath: str) -> dict:
    """Locate the vendored agent inside the bridge's dependency tree at
    ``bridge_root/vendor_subpath`` (e.g. ``node_modules/@anthropic-ai/claude-agent-sdk``) and return its
    OWN identity ``{name, version, digest}`` -- a SEPARATE identity from the bridge package's own
    identity, each with its own sha256 digest, so a bridge-backed run never attributes the vendored
    agent's behavior to the bridge package or vice versa.
    """
    vendor_dir = os.path.join(bridge_root, *vendor_subpath.split("/"))
    if not os.path.isdir(vendor_dir):
        raise AcquisitionError(
            f"no vendored agent found at {vendor_subpath!r} under {bridge_root!r}"
        )
    return _package_identity(vendor_dir)


def describe(bridge_root: str, *, vendor_subpath: str) -> dict:
    """Return both identities distinctly: the bridge package's own identity and the vendored agent's
    identity, each independently digested. See ``identify_vendored_agent`` for the separation
    rationale."""
    return {
        "bridge_identity": _package_identity(bridge_root),
        "vendored_agent_identity": identify_vendored_agent(bridge_root, vendor_subpath=vendor_subpath),
    }


def acquire(
    spec: str,
    *,
    cache_dir: str,
    expected_digest: str,
    runner=_default_runner,
    env: dict | None = None,
) -> dict:
    """Acquire a pinned bridge spec into an out-of-repo ``cache_dir``, scripts disabled, then verify it
    against an out-of-band ``expected_digest`` fixed before this call.

    ``cache_dir`` is expected to live OUTSIDE the repository -- this tool never creates anything under
    the repo tree; callers (and tests) must pass a tempdir or another out-of-repo path.

    ``runner`` is the injectable seam: it defaults to a real npm install with lifecycle scripts
    disabled (the only code in this module that would touch the network), but every test injects a fake
    runner that populates a local fixture tree instead, so no test in this module can reach real npm.

    Refuses (raises ``AcquisitionError``) and leaves nothing usable behind when: ``spec`` is not pinned,
    or the acquired tree's digest does not match ``expected_digest``.
    """
    require_pinned(spec)
    call_env = dict(env) if env is not None else {}
    runner(spec, cache_dir, env=call_env)
    verify_digest(cache_dir, expected_digest)
    return {
        "spec": spec,
        "cache_dir": cache_dir,
        "digest": expected_digest,
        "scripts_disabled": True,
    }
