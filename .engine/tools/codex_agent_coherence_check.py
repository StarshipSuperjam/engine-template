#!/usr/bin/env python3
"""Codex-render integrity guard — the custom/script entry for engine/check/codex-agent-coherence.

Two legs over the Codex renders (eADR-0034):
  1. REVIEWER REQUESTED DEFAULT: every engine Codex persona (`.codex/agents/*.toml` rendered from a
     canonical Claude persona) keeps `sandbox_mode = "read-only"` and pins NO `model`. This is the
     standalone default, not a mechanical child boundary: Codex can reapply the parent task's live
     permission override (provider-exceptions.json). The check prevents weaker committed defaults and
     rotting model ids without overstating runtime isolation.
  2. RENDER SYNC: every committed render (personas AND the `.agents/skills/` twins) matches what the
     render tool would produce from its canonical `.claude/` source, and no engine-prefixed render
     exists without a source — a hand-edited, stale, or orphaned render goes red (the drift gate
     that makes the generated-render doctrine enforceable).

Reads local committed files only. Emits finding.v1 JSON on stdout, exit 0 on a successful
evaluation; a crash exits non-zero (the custom/script kind fails closed).
"""
from __future__ import annotations
import glob
import json
import os
import sys
import tomllib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate    # noqa: E402
import codex_gen   # noqa: E402


def _canonical_frontmatter(slug: str) -> dict | None:
    """The parsed frontmatter of a render's canonical .claude source, or None when it is absent.

    Role is read from this canonical source, NEVER the hand-editable .codex render: a render's role is
    only as trustworthy as the source the render-sync leg pins it to. A render with no canonical source
    is held to the reviewer floor (fail toward the stricter side) and separately flagged as an orphan
    by the render-sync leg.
    """
    src = os.path.join(validate.ROOT, ".claude", "agents", f"{slug}.md")
    if not os.path.isfile(src):
        return None
    return validate.frontmatter(src)


def _floor_findings(tier: str, agents_dir: str) -> list:
    out = []
    for path in sorted(glob.glob(os.path.join(agents_dir, "*.toml"))):
        rel = os.path.relpath(path, validate.ROOT)
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except Exception as exc:  # noqa: BLE001 — unreadable render = a finding, never a crash
            out.append(validate.finding(tier, f"'{rel}' could not be read as TOML ({exc}); a "
                       f"persona the platform cannot parse silently vanishes. "
                       f"Regenerate it (codex_gen.py generate).", validate.loc(path)))
            continue
        slug = os.path.splitext(os.path.basename(path))[0]
        source_fm = _canonical_frontmatter(slug)
        if source_fm is not None and source_fm.get("role") == "worker":
            # A worker render legitimately carries a write sandbox and an explicit per-provider model;
            # the reviewer no-model floor does not apply. Instead it must NOT be read-only, and its model
            # must match the single-sourced implementation_classes binding (drift, not a pinned-id rot).
            if data.get("sandbox_mode") == "read-only":
                out.append(validate.finding(tier, f"'{rel}' is a worker render with a read-only sandbox; a "
                           f"dispatched worker must be able to write its own node. Regenerate it "
                           f"(codex_gen.py generate).", validate.loc(path)))
            try:
                want = codex_gen._impl_binding(source_fm.get("implementation-class"), None)
            except Exception as exc:  # noqa: BLE001 — a missing binding is a finding, never a crash
                out.append(validate.finding(tier, f"'{rel}' worker render could not resolve its model "
                           f"binding ({exc}). Fix implementation_classes and regenerate.", validate.loc(path)))
                continue
            if data.get("model") != want["model"]:
                out.append(validate.finding(tier, f"'{rel}' worker render pins model {data.get('model')!r}, "
                           f"which does not match its implementation_classes binding {want['model']!r}. The "
                           f"worker model is single-sourced; regenerate (codex_gen.py generate).", validate.loc(path)))
            continue
        # A review/audit render (or a render whose canonical role cannot be placed): the reviewer floor.
        if data.get("sandbox_mode") != "read-only":
            out.append(validate.finding(tier, f"'{rel}' is a review persona whose sandbox is not read-only "
                       f"as its requested default — a reviewer must report findings, never edit the work. Restore "
                       f"sandbox_mode = \"read-only\" (edit the Claude source and regenerate).",
                       validate.loc(path)))
        if "model" in data:
            out.append(validate.finding(tier, f"'{rel}' pins a model id, which rots and silently "
                       f"changes who reviews. A review persona never pins a model; remove it from the "
                       f"canonical source and regenerate.", validate.loc(path)))
    return out


def findings(tier: str, agents_dir: str | None = None) -> list:
    if agents_dir is not None:
        return _floor_findings(tier, agents_dir)   # fixture seam: the floor leg over a seeded dir
    out = _floor_findings(tier, os.path.join(validate.ROOT, ".codex", "agents"))
    for problem in codex_gen.check():
        out.append(validate.finding(tier, f"{problem} A render out of sync with its canonical "
                   f"Claude source means the two runtimes review with different instructions.",
                   None))
    return out


def main(argv: list) -> int:
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    # ENGINE_CODEX_AGENT_FIXTURE_DIR (unset in production) points the floor leg at a seeded fixture
    # dir so the negative-fixture meta-check witnesses the guard biting a real bad input.
    fixture = validate.env_override_path("ENGINE_CODEX_AGENT_FIXTURE_DIR")
    print(json.dumps(findings(tier, agents_dir=fixture)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
