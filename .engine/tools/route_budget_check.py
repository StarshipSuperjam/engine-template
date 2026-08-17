#!/usr/bin/env python3
"""Route budget — the custom/script entry for engine/check/route-budget (ADR 0336).

The generated automatic-route surface is what an AI runtime scans to decide which engine workflow a plain
request maps to. It must stay compact and legible, and it must reach BOTH runtimes. This check projects every
MODEL-REACHABLE route — a skill whose `invocation` the model may reach on its own (invocation in
codex_gen._MODEL_REACHABLE = {model-auto, model-only}) — as the tuple (slug, description, repo-relative
SKILL.md path), and enforces three legs:

  A. DESCRIPTION LENGTH — each model-reachable route carries a non-empty description of at most 120
     characters. The description IS the routing text the model matches a request against; a bloated one reads
     poorly and crowds the surface.
  B. TOTAL BUDGET — the whole projection (the sum of slug + description + path lengths across every
     model-reachable route) fits under a hard 6000-character ceiling, so the full route surface stays small
     enough to sit in a model's working context.
  C. NO OMISSION — every model-reachable Claude route has a committed, model-startable Codex twin
     (.agents/skills/<slug>/agents/openai.yaml carrying `allow_implicit_invocation: true`), so a route the
     model can reach on Claude is never silently stranded off the Codex runtime.

Reachability is imported from codex_gen._MODEL_REACHABLE — the SINGLE home for what "the model may start this"
means — so this check and the Codex renderer can never disagree about which routes count. Reads local
committed files only. Emits finding.v1 JSON on stdout, exit 0 on a successful evaluation; a crash exits
non-zero (fail-closed).
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import skill_discovery  # noqa: E402  (the shared skill-discovery helper — one glob + slug-identity path)
import codex_gen  # noqa: E402  (the single home of _MODEL_REACHABLE — reachability lives there, not here)

# The two hard ceilings the route surface must fit under.
_DESC_CEILING = 120       # characters per route description
_TOTAL_CEILING = 6000     # characters across the whole projection (slug + description + path, all routes)


def _twin_is_model_startable(base: str, slug: str) -> bool:
    """True iff the route's committed Codex twin exists and declares `allow_implicit_invocation: true` — the
    render codex_gen emits for a model-reachable route. A missing or typed twin strands the route on one runtime."""
    twin = os.path.join(base, ".agents", "skills", slug, "agents", "openai.yaml")
    try:
        with open(twin, "r", encoding="utf-8") as fh:
            return "allow_implicit_invocation: true" in fh.read()
    except OSError:
        return False


def findings(tier: str, root: str | None = None) -> list:
    base = root or validate.ROOT
    out = []
    # strict=True is the GUARD posture: a skill whose frontmatter cannot be parsed RAISES here (crash → the
    # custom/script runner turns it into a hard finding) rather than being silently dropped from the projection.
    # Reachability is the platform-truth question (an omitted invocation is model-auto = reachable), so a route
    # that skips the invocation line the schema invites is still held to the budget, never silently exempt.
    reachable = [r for r in skill_discovery.records("claude", root=base, strict=True)
                 if codex_gen.is_platform_reachable(r["frontmatter"].get("invocation"))]

    total = 0
    for rec in reachable:
        slug = rec["slug"]
        desc = rec["frontmatter"].get("description")
        rel = os.path.relpath(rec["path"], base)
        # Leg A — description present and within the per-route ceiling.
        if not isinstance(desc, str) or not desc.strip():
            out.append(validate.finding(tier,
                       f"The model-route '{slug}' has no description — the description is the routing text the "
                       f"model matches a request against, so a route without one is unreachable. Give it a "
                       f"single-sentence description of at most {_DESC_CEILING} characters.", {"file": rel, "line": None}))
            desc = desc or ""
        elif len(desc) > _DESC_CEILING:
            out.append(validate.finding(tier,
                       f"The model-route '{slug}' has a {len(desc)}-character description — over the "
                       f"{_DESC_CEILING}-character ceiling. Tighten it to one crisp sentence so the automatic "
                       f"route surface stays legible.", {"file": rel, "line": None}))
        total += len(slug) + len(desc) + len(rel)
        # Leg C — the route reaches the Codex runtime too.
        if not _twin_is_model_startable(base, slug):
            out.append(validate.finding(tier,
                       f"The model-route '{slug}' has no model-startable Codex twin "
                       f"(.agents/skills/{slug}/agents/openai.yaml with allow_implicit_invocation: true), so it "
                       f"is reachable on Claude but stranded on Codex. Regenerate the Codex renders.", {"file": rel, "line": None}))

    # Leg B — the whole projection fits under the total ceiling.
    if total > _TOTAL_CEILING:
        out.append(validate.finding(tier,
                   f"The automatic-route surface projects to {total} characters across {len(reachable)} "
                   f"model-reachable routes — over the {_TOTAL_CEILING}-character budget. Tighten route "
                   f"descriptions (each already under {_DESC_CEILING}) so the whole surface stays compact "
                   f"enough to sit in a model's context.", None))
    return out


def main(argv: list) -> int:
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    # ROUTE_BUDGET_FIXTURE_ROOT (unset in production) points the projection at a seeded fixture repo root so
    # the negative-fixture meta-check witnesses the guard biting a real over-budget route.
    fixture = validate.env_override_path("ROUTE_BUDGET_FIXTURE_ROOT")
    print(json.dumps(findings(tier, root=fixture)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
