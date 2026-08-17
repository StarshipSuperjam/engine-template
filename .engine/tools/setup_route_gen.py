#!/usr/bin/env python3
"""Generator for the per-module setup routes (ADR 0336).

Each offerable module gets one automatic `engine-setup-<module-id>` route — a Claude `model-only` skill that
recognizes intent to set the add-on up. The routes are DERIVED-COMMITTED, core-owned provisioning surfaces:
generated from the offerable manifests' `presentation` (the `setup_trigger` becomes the route's description,
and a `setup_operation`, when present, becomes its canonical target), committed and shipped, and regenerated
rather than hand-edited. They are CORE-owned on purpose — declining or removing the module deletes the
module's own files, but never its offer route, so an operator can always ask to add it back.

A generated route never installs, removes, or grants authority because its trigger matched: its body checks
installation state, offers the add-on (with consent) when absent, and enters the module's setup operation
(or reports the active capability) when present. `derive()` is pure over the discovered manifests; `check()`
compares the committed routes to the derived set for the drift gate; `generate()` writes them.
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

SKILLS_DIR = os.path.join(validate.ROOT, ".claude", "skills")
# The offerable statuses — the opt-out-able set that carries a `presentation`. Mirrors module_catalog._OFFERABLE
# (a required or internal module is never offered and carries no presentation).
_OFFERABLE = ("optional", "default-on", "experimental")
_NAME_PREFIX = "engine-setup-"


def _route_name(module_id: str) -> str:
    return _NAME_PREFIX + module_id


def _body(module_id: str, setup_operation: str | None) -> str:
    """The generated thin-recognizer body: check install state, offer on absence (with consent), enter the
    setup operation (or report the capability) on presence."""
    if setup_operation:
        installed_step = (f"If it is installed, enter its setup procedure in `{setup_operation}`.")
    else:
        installed_step = ("If it is installed, report the active capability and route the operator's request "
                          "to the add-on's own workflow.")
    return "\n".join([
        "## Steps",
        "",
        f"1. Check whether the `{module_id}` add-on is installed in this project.",
        "2. If it is not installed, explain in plain language what it does and offer to add it through the "
        "normal setup step — never install it because this route matched; adding it is the operator's decision.",
        f"3. {installed_step}",
        "",
    ])


def _render(module_id: str, presentation: dict) -> str:
    """One setup route's SKILL.md text. The description is the manifest's concise `setup_trigger`; the
    engine-target, when the module declares a `setup_operation`, is that operation, module-conditional on the
    module itself (absent on a deployment that declined it)."""
    trigger = presentation.get("setup_trigger") or ""
    if not trigger:
        raise ValueError(f"module '{module_id}' presentation has no setup_trigger")
    if len(trigger) > 120:
        raise ValueError(f"module '{module_id}' setup_trigger is {len(trigger)} > 120 chars")
    setup_operation = presentation.get("setup_operation")
    lines = [
        "---",
        f"name: {_route_name(module_id)}",
        f"description: {trigger}",
        "invocation: model-only",
        "user-invocable: false",
    ]
    if setup_operation:
        lines += [
            "engine-targets:",
            "  - kind: operation",
            f"    ref: {setup_operation}",
            "    availability: module-conditional",
            f"    owner: {module_id}",
        ]
    lines += ["---", "", _body(module_id, setup_operation)]
    return "\n".join(lines)


def derive() -> dict:
    """{relative SKILL.md path: rendered text} for every offerable module carrying a presentation, keyed by the
    route's committed path. Pure over the discovered manifests — the single source is each manifest's
    `presentation`, never a hand-authored route."""
    import module_coherence  # lazy: imports validate; keep out of import time
    out: dict = {}
    for _rel, manifest in module_coherence.discover_manifests():
        if not isinstance(manifest, dict):
            continue
        mid = manifest.get("id")
        pres = manifest.get("presentation")
        if not mid or manifest.get("status") not in _OFFERABLE:
            continue
        if not isinstance(pres, dict) or not pres.get("setup_trigger"):
            continue
        rel = os.path.join(".claude", "skills", _route_name(mid), "SKILL.md")
        out[rel] = _render(mid, pres)
    return out


def check(tier: str = "hard") -> list:
    """Findings when a committed setup route is missing or diverges from its derived text, or a stray
    `engine-setup-*` route exists with no offerable module behind it. The drift gate for the derived-committed
    routes (mirrors codex_gen's render-equality contract)."""
    findings = []
    expected = derive()
    for rel, text in sorted(expected.items()):
        path = os.path.join(validate.ROOT, rel)
        if not os.path.isfile(path):
            findings.append(validate.finding(tier, f"The setup route {rel} is missing; regenerate the setup "
                            f"routes with `setup_route_gen.py generate`."))
            continue
        with open(path, encoding="utf-8") as fh:
            if fh.read() != text:
                findings.append(validate.finding(tier, f"The setup route {rel} is out of date — it no longer "
                                f"matches its module's presentation. Regenerate with "
                                f"`setup_route_gen.py generate`."))
    # A stray engine-setup-<id> route whose module is not an offerable present module is orphan generation.
    expected_names = {os.path.basename(os.path.dirname(rel)) for rel in expected}
    if os.path.isdir(SKILLS_DIR):
        for name in sorted(os.listdir(SKILLS_DIR)):
            if name.startswith(_NAME_PREFIX) and name not in expected_names:
                findings.append(validate.finding(tier, f"The setup route '{name}' has no offerable module "
                                f"behind it — a stale generated route. Remove it or restore its module."))
    return findings


def generate() -> list:
    """Write every derived setup route; return the relative paths written."""
    written = []
    for rel, text in sorted(derive().items()):
        path = os.path.join(validate.ROOT, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        written.append(rel)
    return written


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        result = generate()
        print(f"wrote {len(result)} setup route(s):")
        for rel in result:
            print(f"  {rel}")
    elif len(sys.argv) > 1 and sys.argv[1] == "check":
        found = check()
        print(f"{len(found)} finding(s)")
        for f in found:
            print("  ", f["message"][:140])
    else:
        print("usage: setup_route_gen.py {generate|check}", file=sys.stderr)
        sys.exit(2)
