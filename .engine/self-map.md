# What this engine is made of

> **Generated file — do not edit by hand.** This map is derived from the engine's surface
> catalog and module manifests, so it always matches them. To update it, change those and
> regenerate with `uv run --directory .engine -- python tools/self_map.py generate`, then commit the result.

Engine release `0.0.0-dev` · identity `solo`

## Surfaces

Every kind of file the engine governs — its home and authority, and the schema and template that govern it (11 surfaces).

| surface | purpose | home | authority | lifecycle | class | governing schema | template |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `agent` | Personas the engine runs for a trigger (review, worker, and audit roles), routed by role, lens, model tier, permissions, and output contract. | `.claude/agents/` | mechanics-and-guidance | artifact | prose | `agent.v1.json` | `../templates/agent.md` |
| `check` | Declarative validation rules the validator dispatches (target, kind, params, tier, suites, message) — authored as data, never as validator code. | `.engine/check/` | mechanics-and-guidance | artifact | structured | `check.v1.json` | (none) |
| `conduct` | Codes of conduct — the operator's standing behavioral stance for how the AI engages (plain language, provenance, push-back, and the like); tier-3 guidance, pure posture, never an enforcement gate. Two committed layers (engine defaults plus operator override) composed by rule id, loaded at the grounding floor. | `.engine/conduct/` | mechanics-and-guidance | artifact | prose | `conduct.v1.json` | `../templates/conduct.md` |
| `contract` | Architecturally significant decision records — one decision each, with rationale and the rejected alternative; the top authority tier, file-per-decision, append-only. | `.engine/contracts/` | decisions | decision | prose | `contract.v1.json` | `../templates/contract.md` |
| `doc` | Operator-facing, hand-authored plain-language explanations of the engine — written for the human, not the AI. | `.engine/docs/` | mechanics-and-guidance | artifact | prose | `doc.v1.json` | `../templates/doc.md` |
| `interface` | Protocol contracts — a stable callable boundary a swappable implementation satisfies; implementations bind by presence, resolve single-active, and name a fallback. | `.engine/interfaces/` | mechanics-and-guidance | artifact | structured | `interface.v1.json` | (none) |
| `operation` | The authoritative steps of a multi-step engine procedure performed by reading-and-following; one procedure, one home, referenced by its invokers rather than restated. | `.engine/operations/` | mechanics-and-guidance | artifact | prose | `operation.v1.json` | `../templates/operation.md` |
| `policy` | Standing rules — ongoing directives that govern behavior across sessions; the second authority tier. | `.engine/policies/` | standing-rules | decision | prose | `policy.v1.json` | `../templates/policy.md` |
| `schema` | Structural contracts — JSON Schema (2020-12) declaring the shape of structured files and of prose frontmatter. | `.engine/schemas/` | mechanics-and-guidance | artifact | structured | `https://json-schema.org/draft/2020-12/schema` | (none) |
| `skill` | In-session procedures (Claude Code SKILL.md, progressive disclosure), engine-prefixed; invoked per the model-auto / operator-typed / model-only axis. | `.claude/skills/` | mechanics-and-guidance | artifact | prose | `skill.v1.json` | `../templates/skill.md` |
| `tool` | The engine's executable machinery — the validator, hooks, MCP servers, the wiring library, and interface implementations. | `.engine/tools/` | mechanics-and-guidance | artifact | code | (none) | (none) |

## Modules

The packages your engine is assembled from, and how they wire together (11 installed).

The dependency graph — each module is listed after the ones it builds on (`→` means "depends on"):

- `core` (no dependencies)
- `dependency-discipline` → `core`
- `design-review` → `core`
- `external-contribution` → `core`
- `github-projects-sync` → `core`
- `memory-substrate-sqlite-fts5` → `core`
- `migration-discipline` → `core`
- `qa-review` → `core`
- `routine-mode` → `core`
- `validators-core` → `core`
- `audit-library` → `core`, `validators-core`

### `core` — version `0.0.0-dev` (required)

- depends on: nothing
- provides:
  - agent: `.claude/agents/.gitkeep`
  - check: `.engine/check/guardrail-weakening.json`, `.engine/check/protection.json`
  - conduct: `.engine/conduct/defaults.md`
  - contract: `.engine/contracts/*.md`, `.engine/contracts/.gitkeep`
  - doc: `.engine/docs/*.md`
  - foundation: `.engine/self-map.md`, `.engine/suites.json`
  - interface: `.engine/interfaces/*.json`
  - knowledge: `.engine/knowledge/*.json`
  - operation: `.engine/operations/boot-session-start.md`, `.engine/operations/build-orchestration.md`, `.engine/operations/close-turn.md`, `.engine/operations/conduct-author.md`, `.engine/operations/control-plane-bootstrap.md`, `.engine/operations/engine-remove.md`, `.engine/operations/engine-upgrade.md`, `.engine/operations/first-run.md`, `.engine/operations/knowledge-impact-check.md`, `.engine/operations/module-add.md`, `.engine/operations/module-remove.md`, `.engine/operations/operating-modes.md`, `.engine/operations/tune-policy.md`
  - policy: `.engine/policies/attention.md`, `.engine/policies/contract-threshold.md`, `.engine/policies/escalation.md`, `.engine/policies/finding-disposition.md`, `.engine/policies/triage-threshold.md`
  - provisioning: `.engine/provisioning/first-run-assets.json`, `.engine/provisioning/module-catalog.json`
  - schema: `.engine/schemas/*.json`
  - skill: `.claude/skills/.gitkeep`, `.claude/skills/engine-conduct/SKILL.md`, `.claude/skills/engine-help/SKILL.md`, `.claude/skills/engine-setup/SKILL.md`, `.claude/skills/engine-start/SKILL.md`, `.claude/skills/engine-status/SKILL.md`, `.claude/skills/engine-tune/SKILL.md`
  - state: `.engine/state/*.json`
  - template: `.engine/templates/*.md`
  - tool: `.engine/tools/*.py`, `.engine/tools/*.sh`
- wires: gitignore, hook, mcp

### `dependency-discipline` — version `0.0.0-dev` (optional)

- depends on: `core`
- provides:
  - check: `.engine/check/dependency-pinning.json`, `.engine/check/dependency-review.json`
  - policy: `.engine/policies/dependency-discipline.md`
  - tool: `.engine/tools/dependency_discipline/*.py`
- wires: none (this module adds no shared-state edits)

### `design-review` — version `0.0.0-dev` (optional)

- depends on: `core`
- provides:
  - agent: `.claude/agents/design-review-architecture.md`, `.claude/agents/design-review-feasibility.md`, `.claude/agents/design-review-product-intent.md`, `.claude/agents/design-review-risk-governance.md`
- wires: none (this module adds no shared-state edits)

### `external-contribution` — version `0.0.0-dev` (optional)

- depends on: `core`
- provides:
  - check: `.engine/check/upstream-clean.json`
  - operation: `.engine/operations/external-contribution-submit.md`
  - policy: `.engine/policies/external-contribution.md`
  - tool: `.engine/tools/external_contribution/*.py`
- wires: none (this module adds no shared-state edits)

### `github-projects-sync` — version `0.0.0-dev` (optional)

- depends on: `core`
- provides:
  - operation: `.engine/operations/projects-sync-setup.md`
  - skill: `.claude/skills/engine-board-setup/SKILL.md`
  - tool: `.engine/tools/projects_sync/*.py`
- wires: gitignore, hook

### `memory-substrate-sqlite-fts5` — version `0.0.0-dev` (required)

- depends on: `core`
- provides:
  - backup: `.engine/memory-backup/pointer.json`
  - erasures: `.engine/erasures/proposal.json`
  - tool: `.engine/tools/memory/*.py`
- wires: gitignore, hook, mcp

### `migration-discipline` — version `0.0.0-dev` (optional)

- depends on: `core`
- provides:
  - policy: `.engine/policies/migration-discipline.md`
- wires: none (this module adds no shared-state edits)

### `qa-review` — version `0.0.0-dev` (optional)

- depends on: `core`
- provides:
  - agent: `.claude/agents/qa-review-security-governance.md`, `.claude/agents/qa-review-spec-conformance.md`, `.claude/agents/qa-review-technical-integrity.md`, `.claude/agents/qa-review-usability.md`
- wires: none (this module adds no shared-state edits)

### `routine-mode` — version `0.0.0-dev` (required)

- depends on: `core`
- provides:
  - operation: `.engine/operations/routine-entry.md`
  - skill: `.claude/skills/engine-routine/SKILL.md`
- wires: none (this module adds no shared-state edits)

### `validators-core` — version `0.0.0-dev` (required)

- depends on: `core`
- provides:
  - check: `.engine/check/agent-frontmatter.json`, `.engine/check/agent-shape.json`, `.engine/check/audit-concern-list.json`, `.engine/check/audit-digest-fingerprint.json`, `.engine/check/audit-digest-staleness.json`, `.engine/check/catalog-coverage.json`, `.engine/check/conduct-frontmatter.json`, `.engine/check/conduct-shape.json`, `.engine/check/conduct-weakening-guard.json`, `.engine/check/contract-frontmatter.json`, `.engine/check/contract-shape.json`, `.engine/check/contract-threshold.json`, `.engine/check/doc-frontmatter.json`, `.engine/check/doc-shape.json`, `.engine/check/engine-manifest.json`, `.engine/check/first-run-reference-closure.json`, `.engine/check/in-tool-demo-failure-path.json`, `.engine/check/interface-declaration.json`, `.engine/check/knowledge-coverage.json`, `.engine/check/knowledge-vocabulary.json`, `.engine/check/link-integrity.json`, `.engine/check/memory-pointer-public-safety.json`, `.engine/check/module-manifest.json`, `.engine/check/operation-frontmatter.json`, `.engine/check/operation-shape.json`, `.engine/check/policy-frontmatter.json`, `.engine/check/policy-override-stale.json`, `.engine/check/policy-shape.json`, `.engine/check/pr-body-completeness.json`, `.engine/check/provisioning-catalog.json`, `.engine/check/self-map-drift.json`, `.engine/check/skill-coherence.json`, `.engine/check/skill-frontmatter.json`, `.engine/check/skill-shape.json`, `.engine/check/state-cursor.json`, `.engine/check/uv-group-drift.json`
- wires: none (this module adds no shared-state edits)

### `audit-library` — version `0.0.0-dev` (required)

- depends on: `core`, `validators-core`
- provides:
  - agent: `.claude/agents/audit.md`
  - audits: `.engine/audits/audit-digest.md`, `.engine/audits/concern-list.json`, `.engine/audits/self-review-setup.md`
- wires: none (this module adds no shared-state edits)
