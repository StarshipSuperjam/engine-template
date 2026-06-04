# What this engine is made of

> **Generated file — do not edit by hand.** This map is derived from the engine's surface
> catalog and module manifests, so it always matches them. To update it, change those and
> regenerate with `uv run --directory .engine -- python tools/self_map.py generate`, then commit the result.

Engine release `0.0.0-dev` · identity `solo`

## Surfaces

Every kind of file the engine governs — its home and authority, and the schema and template that govern it (10 surfaces).

| surface | purpose | home | authority | lifecycle | class | governing schema | template |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `agent` | Personas the engine runs for a trigger (review, worker, and audit roles), routed by role, lens, model tier, permissions, and output contract. | `.claude/agents/` | mechanics-and-guidance | artifact | prose | (none) | (none) |
| `check` | Declarative validation rules the validator dispatches (target, kind, params, tier, suites, message) — authored as data, never as validator code. | `.engine/check/` | mechanics-and-guidance | artifact | structured | `check.v1.json` | (none) |
| `contract` | Architecturally significant decision records — one decision each, with rationale and the rejected alternative; the top authority tier, file-per-decision, append-only. | `.engine/contracts/` | decisions | decision | prose | `contract.v1.json` | `../templates/contract.md` |
| `doc` | Operator-facing, hand-authored plain-language explanations of the engine — written for the human, not the AI. | `.engine/docs/` | mechanics-and-guidance | artifact | prose | (none) | (none) |
| `interface` | Protocol contracts — a stable callable boundary a swappable implementation satisfies; implementations bind by presence, resolve single-active, and name a fallback. | `.engine/interfaces/` | mechanics-and-guidance | artifact | structured | `interface.v1.json` | (none) |
| `operation` | The authoritative steps of a multi-step engine procedure performed by reading-and-following; one procedure, one home, referenced by its invokers rather than restated. | `.engine/operations/` | mechanics-and-guidance | artifact | prose | (none) | (none) |
| `policy` | Standing rules — ongoing directives that govern behavior across sessions; the second authority tier. | `.engine/policies/` | standing-rules | decision | prose | `policy.v1.json` | `../templates/policy.md` |
| `schema` | Structural contracts — JSON Schema (2020-12) declaring the shape of structured files and of prose frontmatter. | `.engine/schemas/` | mechanics-and-guidance | artifact | structured | `https://json-schema.org/draft/2020-12/schema` | (none) |
| `skill` | In-session procedures (Claude Code SKILL.md, progressive disclosure), engine-prefixed; invoked per the model-auto / operator-typed / model-only axis. | `.claude/skills/` | mechanics-and-guidance | artifact | prose | (none) | (none) |
| `tool` | The engine's executable machinery — the validator, hooks, MCP servers, the wiring library, and interface implementations. | `.engine/tools/` | mechanics-and-guidance | artifact | code | (none) | (none) |

## Modules

The packages your engine is assembled from, and how they wire together (2 installed).

The dependency graph — each module is listed after the ones it builds on (`→` means "depends on"):

- `core` (no dependencies)
- `validators-core` → `core`

### `core` — version `0.0.0-dev` (required)

- depends on: nothing
- provides:
  - agent: `.claude/agents/.gitkeep`
  - check: `.engine/check/guardrail-weakening.json`, `.engine/check/protection.json`
  - contract: `.engine/contracts/*.md`, `.engine/contracts/.gitkeep`
  - doc: `.engine/docs/.gitkeep`
  - foundation: `.engine/self-map.md`, `.engine/suites.json`
  - interface: `.engine/interfaces/*.json`
  - knowledge: `.engine/knowledge/*.json`
  - operation: `.engine/operations/.gitkeep`
  - policy: `.engine/policies/*.md`
  - schema: `.engine/schemas/*.json`
  - skill: `.claude/skills/.gitkeep`
  - state: `.engine/state/*.json`
  - template: `.engine/templates/*.md`
  - tool: `.engine/tools/*.py`
- wires: gitignore, mcp

### `validators-core` — version `0.0.0-dev` (required)

- depends on: `core`
- provides:
  - check: `.engine/check/catalog-coverage.json`, `.engine/check/contract-frontmatter.json`, `.engine/check/contract-shape.json`, `.engine/check/contract-threshold.json`, `.engine/check/engine-manifest.json`, `.engine/check/interface-declaration.json`, `.engine/check/knowledge-coverage.json`, `.engine/check/link-integrity.json`, `.engine/check/module-manifest.json`, `.engine/check/policy-frontmatter.json`, `.engine/check/policy-shape.json`, `.engine/check/pr-body-completeness.json`, `.engine/check/self-map-drift.json`, `.engine/check/state-cursor.json`
- wires: none (this module adds no shared-state edits)
