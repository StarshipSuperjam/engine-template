# What this engine is made of

> **Generated file — do not edit by hand.** This map is derived from the engine's surface
> catalog and module manifests, so it always matches them. To update it, change those and
> regenerate with `uv run --directory .engine -- python tools/self_map.py generate`, then commit the result.

> **What this shows — and what it does not.** This map shows your engine's structural makeup:
> the kinds of file it governs and the packages it is built from, derived to match those sources.
> It does not show whether each part *works* or is well designed — that is your review and each
> module's own checks, never something this map attests.

Engine release `0.6.3` · identity `solo`

## Surfaces

Every kind of file the engine governs — its home and authority, and the schema and template that govern it (13 surfaces).

| surface | purpose | home | authority | lifecycle | class | governing schema | template |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `agent` | Personas the engine runs for a trigger (review, worker, and audit roles), routed by role, lens, model tier, permissions, and output contract. | `.claude/agents/` | mechanics-and-guidance | artifact | prose | `agent.v1.json` | `../templates/agent.md` |
| `check` | Declarative validation rules the validator dispatches (target, kind, params, tier, suites, message) — authored as data, never as validator code. | `.engine/check/` | mechanics-and-guidance | artifact | structured | `check.v1.json` | (none) |
| `codex-agent` | The Codex-native render of an engine review persona (a TOML agent Codex spawns; generated from the canonical Claude persona, never hand-authored). | `.codex/agents/` | mechanics-and-guidance | artifact | structured | `codex-agent.v1.json` | (none) |
| `codex-skill` | The Codex-native render of an engine typed command (a SKILL.md Codex discovers; generated from the canonical Claude skill, never hand-authored). | `.agents/skills/` | mechanics-and-guidance | artifact | prose | `codex-skill.v1.json` | `../templates/codex-skill.md` |
| `conduct` | Codes of conduct — the operator's standing behavioral stance for how the AI engages (plain language, provenance, push-back, and the like); tier-3 guidance, pure posture, never an enforcement gate. Two committed layers (engine defaults plus operator override) composed by rule id, loaded at the grounding floor. | `.engine/conduct/` | mechanics-and-guidance | artifact | prose | `conduct.v1.json` | `../templates/conduct.md` |
| `doc` | Operator-facing, hand-authored plain-language explanations of the engine — written for the human, not the AI. | `.engine/docs/` | mechanics-and-guidance | artifact | prose | `doc.v1.json` | `../templates/doc.md` |
| `executors` | Executor-qualification records — the Engine's versioned, observed account of an external build-executor's three qualification gates (protocol, containment, capability) and fail-closed dispatch witnesses; distinct from the runtime-environment store at .engine/state/execution.json. | `.engine/executors/` | mechanics-and-guidance | artifact | structured | `executor-qualification.v1.json` | (none) |
| `interface` | Protocol contracts — a stable callable boundary a swappable implementation satisfies; implementations bind by presence, resolve single-active, and name a fallback. | `.engine/interfaces/` | mechanics-and-guidance | artifact | structured | `interface.v1.json` | (none) |
| `operation` | The authoritative steps of a multi-step engine procedure performed by reading-and-following; one procedure, one home, referenced by its invokers rather than restated. | `.engine/operations/` | mechanics-and-guidance | artifact | prose | `operation.v1.json` | `../templates/operation.md` |
| `policy` | Standing rules — ongoing directives that govern behavior across sessions; the highest declared authority tier. | `.engine/policies/` | standing-rules | decision | prose | `policy.v1.json` | `../templates/policy.md` |
| `schema` | Structural contracts — JSON Schema (2020-12) declaring the shape of structured files and of prose frontmatter. | `.engine/schemas/` | mechanics-and-guidance | artifact | structured | `https://json-schema.org/draft/2020-12/schema` | (none) |
| `skill` | In-session procedures (Claude Code SKILL.md, progressive disclosure), engine-prefixed; invoked per the model-auto / operator-typed / model-only axis. | `.claude/skills/` | mechanics-and-guidance | artifact | prose | `skill.v1.json` | `../templates/skill.md` |
| `tool` | The engine's executable machinery — the validator, hooks, MCP servers, the wiring library, and interface implementations. | `.engine/tools/` | mechanics-and-guidance | artifact | code | (none) | (none) |

## Modules

The packages your engine is assembled from, and how they wire together (13 installed).

The dependency graph — each module is listed after the ones it builds on (`→` means "depends on"):

- `core` (no dependencies)
- `dependency-discipline` → `core`
- `design-review` → `core`
- `external-contribution` → `core`
- `github-projects-sync` → `core`
- `memory-substrate-sqlite-fts5` → `core`
- `memory-semantic-recall` → `core`, `memory-substrate-sqlite-fts5`
- `migration-discipline` → `core`
- `product-design` → `core`
- `qa-review` → `core`
- `routine-mode` → `core`
- `validators-core` → `core`
- `audit-library` → `validators-core`

### `core` — version `0.6.2` (required)

- depends on: nothing
- provides:
  - agent: `.claude/agents/engine-grounding-scout.md`, `.claude/agents/engine-validation-runner.md`, `.claude/agents/engine-worker-bounded.md`, `.claude/agents/engine-worker-builder.md`
  - check: `.engine/check/guardrail-weakening.json`, `.engine/check/protection.json`
  - codex-agent: `.codex/agents/engine-worker-bounded.toml`, `.codex/agents/engine-worker-builder.toml`
  - codex-skill: `.agents/skills/engine-change-conduct/SKILL.md`, `.agents/skills/engine-change-conduct/agents/openai.yaml`, `.agents/skills/engine-check-impact/SKILL.md`, `.agents/skills/engine-check-impact/agents/openai.yaml`, `.agents/skills/engine-configure-codex/SKILL.md`, `.agents/skills/engine-configure-codex/agents/openai.yaml`, `.agents/skills/engine-configure-memory-backup/SKILL.md`, `.agents/skills/engine-configure-memory-backup/agents/openai.yaml`, `.agents/skills/engine-coordinate-build/SKILL.md`, `.agents/skills/engine-coordinate-build/agents/openai.yaml`, `.agents/skills/engine-design-product/SKILL.md`, `.agents/skills/engine-design-product/agents/openai.yaml`, `.agents/skills/engine-develop-engine/SKILL.md`, `.agents/skills/engine-develop-engine/agents/openai.yaml`, `.agents/skills/engine-drop-operator-pin/SKILL.md`, `.agents/skills/engine-drop-operator-pin/agents/openai.yaml`, `.agents/skills/engine-enable-protection/SKILL.md`, `.agents/skills/engine-enable-protection/agents/openai.yaml`, `.agents/skills/engine-file-engine-issue/SKILL.md`, `.agents/skills/engine-file-engine-issue/agents/openai.yaml`, `.agents/skills/engine-file-upstream-issue/SKILL.md`, `.agents/skills/engine-file-upstream-issue/agents/openai.yaml`, `.agents/skills/engine-help/SKILL.md`, `.agents/skills/engine-help/agents/openai.yaml`, `.agents/skills/engine-install-engine/SKILL.md`, `.agents/skills/engine-install-engine/agents/openai.yaml`, `.agents/skills/engine-manage-addons/SKILL.md`, `.agents/skills/engine-manage-addons/agents/openai.yaml`, `.agents/skills/engine-manage-plans/SKILL.md`, `.agents/skills/engine-manage-plans/agents/openai.yaml`, `.agents/skills/engine-manage-programs/SKILL.md`, `.agents/skills/engine-manage-programs/agents/openai.yaml`, `.agents/skills/engine-manage-setup/SKILL.md`, `.agents/skills/engine-manage-setup/agents/openai.yaml`, `.agents/skills/engine-onboard-project/SKILL.md`, `.agents/skills/engine-onboard-project/agents/openai.yaml`, `.agents/skills/engine-parts/SKILL.md`, `.agents/skills/engine-parts/agents/openai.yaml`, `.agents/skills/engine-prepare-routine/SKILL.md`, `.agents/skills/engine-prepare-routine/agents/openai.yaml`, `.agents/skills/engine-recall/SKILL.md`, `.agents/skills/engine-recall/agents/openai.yaml`, `.agents/skills/engine-release-project/SKILL.md`, `.agents/skills/engine-release-project/agents/openai.yaml`, `.agents/skills/engine-release/SKILL.md`, `.agents/skills/engine-release/agents/openai.yaml`, `.agents/skills/engine-remove-engine/SKILL.md`, `.agents/skills/engine-remove-engine/agents/openai.yaml`, `.agents/skills/engine-restore-operator-pin/SKILL.md`, `.agents/skills/engine-restore-operator-pin/agents/openai.yaml`, `.agents/skills/engine-save-operator-pin/SKILL.md`, `.agents/skills/engine-save-operator-pin/agents/openai.yaml`, `.agents/skills/engine-setup-dependency-discipline/SKILL.md`, `.agents/skills/engine-setup-dependency-discipline/agents/openai.yaml`, `.agents/skills/engine-setup-design-review/SKILL.md`, `.agents/skills/engine-setup-design-review/agents/openai.yaml`, `.agents/skills/engine-setup-external-contribution/SKILL.md`, `.agents/skills/engine-setup-external-contribution/agents/openai.yaml`, `.agents/skills/engine-setup-github-projects-sync/SKILL.md`, `.agents/skills/engine-setup-github-projects-sync/agents/openai.yaml`, `.agents/skills/engine-setup-memory-semantic-recall/SKILL.md`, `.agents/skills/engine-setup-memory-semantic-recall/agents/openai.yaml`, `.agents/skills/engine-setup-migration-discipline/SKILL.md`, `.agents/skills/engine-setup-migration-discipline/agents/openai.yaml`, `.agents/skills/engine-setup-product-design/SKILL.md`, `.agents/skills/engine-setup-product-design/agents/openai.yaml`, `.agents/skills/engine-setup-qa-review/SKILL.md`, `.agents/skills/engine-setup-qa-review/agents/openai.yaml`, `.agents/skills/engine-setup/SKILL.md`, `.agents/skills/engine-setup/agents/openai.yaml`, `.agents/skills/engine-show-help/SKILL.md`, `.agents/skills/engine-show-help/agents/openai.yaml`, `.agents/skills/engine-show-parts/SKILL.md`, `.agents/skills/engine-show-parts/agents/openai.yaml`, `.agents/skills/engine-show-status/SKILL.md`, `.agents/skills/engine-show-status/agents/openai.yaml`, `.agents/skills/engine-start/SKILL.md`, `.agents/skills/engine-start/agents/openai.yaml`, `.agents/skills/engine-status/SKILL.md`, `.agents/skills/engine-status/agents/openai.yaml`, `.agents/skills/engine-submit-upstream-contribution/SKILL.md`, `.agents/skills/engine-submit-upstream-contribution/agents/openai.yaml`, `.agents/skills/engine-switch-reviewers/SKILL.md`, `.agents/skills/engine-switch-reviewers/agents/openai.yaml`, `.agents/skills/engine-tune-settings/SKILL.md`, `.agents/skills/engine-tune-settings/agents/openai.yaml`, `.agents/skills/engine-upgrade-engine/SKILL.md`, `.agents/skills/engine-upgrade-engine/agents/openai.yaml`, `.agents/skills/engine-upgrade/SKILL.md`, `.agents/skills/engine-upgrade/agents/openai.yaml`, `.agents/skills/engine-validate-codex/SKILL.md`, `.agents/skills/engine-validate-codex/agents/openai.yaml`
  - conduct: `.engine/conduct/defaults.md`
  - doc: `.engine/docs/accepted-hook-qualification.md`, `.engine/docs/ci-assurance.md`, `.engine/docs/getting-started.md`
  - executors: `.engine/executors/*.json`
  - foundation: `.engine/build-orchestration-obligations.json`, `.engine/build-protocol.json`, `.engine/check-classification.json`, `.engine/self-map.md`, `.engine/suites.json`
  - interface: `.engine/interfaces/*.json`
  - knowledge: `.engine/knowledge/*.json`
  - migration: `.engine/modules/core/migrations/*.py`
  - operation: `.engine/operations/boot-session-start.md`, `.engine/operations/build-execution.md`, `.engine/operations/build-orchestration.md`, `.engine/operations/build-product-grounding.md`, `.engine/operations/build-submission-evidence.md`, `.engine/operations/build-work-dispatch.md`, `.engine/operations/checkout-auto-update.md`, `.engine/operations/close-turn.md`, `.engine/operations/codex-incident-replay.md`, `.engine/operations/codex-settings.md`, `.engine/operations/codex-validation.md`, `.engine/operations/conduct-author.md`, `.engine/operations/control-plane-bootstrap.md`, `.engine/operations/engine-arrival.md`, `.engine/operations/engine-development.md`, `.engine/operations/engine-release.md`, `.engine/operations/engine-remove.md`, `.engine/operations/engine-team-switch.md`, `.engine/operations/engine-upgrade.md`, `.engine/operations/first-run.md`, `.engine/operations/knowledge-impact-check.md`, `.engine/operations/memory-migration-trial.md`, `.engine/operations/memory-recall.md`, `.engine/operations/module-add.md`, `.engine/operations/module-remove.md`, `.engine/operations/onboarding-read.md`, `.engine/operations/operating-modes.md`, `.engine/operations/owned-product-build.md`, `.engine/operations/plan-orchestration.md`, `.engine/operations/program-orchestration.md`, `.engine/operations/serialized-integration.md`, `.engine/operations/tune-policy.md`
  - policy: `.engine/policies/attention.md`, `.engine/policies/briefing-budget.md`, `.engine/policies/escalation.md`, `.engine/policies/finding-disposition.md`, `.engine/policies/model-bindings.json`, `.engine/policies/model-routing-postures.json`, `.engine/policies/model-routing.md`, `.engine/policies/session-economy.md`, `.engine/policies/supported-upgrade-matrix.md`, `.engine/policies/triage-threshold.md`
  - provisioning: `.engine/provisioning/first-run-assets.json`, `.engine/provisioning/module-catalog.json`, `.engine/provisioning/module-surfaces.json`
  - schema: `.engine/schemas/*.json`
  - skill: `.claude/skills/engine-change-conduct/SKILL.md`, `.claude/skills/engine-check-impact/SKILL.md`, `.claude/skills/engine-configure-codex/SKILL.md`, `.claude/skills/engine-configure-memory-backup/SKILL.md`, `.claude/skills/engine-coordinate-build/SKILL.md`, `.claude/skills/engine-design-product/SKILL.md`, `.claude/skills/engine-develop-engine/SKILL.md`, `.claude/skills/engine-drop-operator-pin/SKILL.md`, `.claude/skills/engine-enable-protection/SKILL.md`, `.claude/skills/engine-file-engine-issue/SKILL.md`, `.claude/skills/engine-file-upstream-issue/SKILL.md`, `.claude/skills/engine-help/SKILL.md`, `.claude/skills/engine-install-engine/SKILL.md`, `.claude/skills/engine-manage-addons/SKILL.md`, `.claude/skills/engine-manage-plans/SKILL.md`, `.claude/skills/engine-manage-programs/SKILL.md`, `.claude/skills/engine-manage-setup/SKILL.md`, `.claude/skills/engine-onboard-project/SKILL.md`, `.claude/skills/engine-parts/SKILL.md`, `.claude/skills/engine-prepare-routine/SKILL.md`, `.claude/skills/engine-recall/SKILL.md`, `.claude/skills/engine-release-project/SKILL.md`, `.claude/skills/engine-release/SKILL.md`, `.claude/skills/engine-remove-engine/SKILL.md`, `.claude/skills/engine-restore-operator-pin/SKILL.md`, `.claude/skills/engine-save-operator-pin/SKILL.md`, `.claude/skills/engine-setup-dependency-discipline/SKILL.md`, `.claude/skills/engine-setup-design-review/SKILL.md`, `.claude/skills/engine-setup-external-contribution/SKILL.md`, `.claude/skills/engine-setup-github-projects-sync/SKILL.md`, `.claude/skills/engine-setup-memory-semantic-recall/SKILL.md`, `.claude/skills/engine-setup-migration-discipline/SKILL.md`, `.claude/skills/engine-setup-product-design/SKILL.md`, `.claude/skills/engine-setup-qa-review/SKILL.md`, `.claude/skills/engine-setup/SKILL.md`, `.claude/skills/engine-show-help/SKILL.md`, `.claude/skills/engine-show-parts/SKILL.md`, `.claude/skills/engine-show-status/SKILL.md`, `.claude/skills/engine-start/SKILL.md`, `.claude/skills/engine-status/SKILL.md`, `.claude/skills/engine-submit-upstream-contribution/SKILL.md`, `.claude/skills/engine-switch-reviewers/SKILL.md`, `.claude/skills/engine-tune-settings/SKILL.md`, `.claude/skills/engine-upgrade-engine/SKILL.md`, `.claude/skills/engine-upgrade/SKILL.md`, `.claude/skills/engine-validate-codex/SKILL.md`
  - state: `.engine/state/*.json`
  - template: `.engine/templates/*.md`
  - tool: `.engine/tools/*.py`, `.engine/tools/*.sh`
- wires: codex-hook, codex-mcp, gitignore, hook, mcp

### `dependency-discipline` — version `0.1.0` (optional)

- depends on: `core`
- provides:
  - check: `.engine/check/dependency-pinning.json`, `.engine/check/dependency-review.json`
  - policy: `.engine/policies/dependency-discipline.md`
  - tool: `.engine/tools/dependency_discipline/*.py`
- wires: none (this module adds no shared-state edits)

### `design-review` — version `0.2.0` (optional)

- depends on: `core`
- provides:
  - agent: `.claude/agents/engine-design-review-architecture.md`, `.claude/agents/engine-design-review-feasibility.md`, `.claude/agents/engine-design-review-product-intent.md`, `.claude/agents/engine-design-review-risk-governance.md`
  - codex-agent: `.codex/agents/engine-design-review-architecture.toml`, `.codex/agents/engine-design-review-feasibility.toml`, `.codex/agents/engine-design-review-product-intent.toml`, `.codex/agents/engine-design-review-risk-governance.toml`
- wires: none (this module adds no shared-state edits)

### `external-contribution` — version `0.1.0` (optional)

- depends on: `core`
- provides:
  - check: `.engine/check/upstream-clean.json`
  - operation: `.engine/operations/external-contribution-issue.md`, `.engine/operations/external-contribution-submit.md`
  - policy: `.engine/policies/external-contribution.md`
  - tool: `.engine/tools/external_contribution/*.py`
- wires: none (this module adds no shared-state edits)

### `github-projects-sync` — version `0.3.0` (optional)

- depends on: `core`
- provides:
  - codex-skill: (none)
  - operation: `.engine/operations/projects-release-advance.md`, `.engine/operations/projects-sync-setup.md`
  - skill: (none)
  - tool: `.engine/tools/projects_sync/*.py`
- wires: codex-hook, gitignore, hook

### `memory-substrate-sqlite-fts5` — version `0.2.0` (required)

- depends on: `core`
- provides:
  - backup: `.engine/memory-backup/pointer.json`
  - erasures: `.engine/erasures/proposal.json`
  - tool: `.engine/tools/memory/*.py`
- wires: codex-hook, codex-mcp, gitignore, hook, mcp

### `memory-semantic-recall` — version `0.1.0` (default-on)

- depends on: `core`, `memory-substrate-sqlite-fts5`
- provides:
  - asset: `.engine/tools/memory/semantic/NOTICE.txt`, `.engine/tools/memory/semantic/checksums.json`, `.engine/tools/memory/semantic/potion-retrieval-32m-int8.npz`, `.engine/tools/memory/semantic/vocab.txt`
  - tool: `.engine/tools/memory/semantic/*.py`
- wires: none (this module adds no shared-state edits)

### `migration-discipline` — version `0.1.0` (optional)

- depends on: `core`
- provides:
  - check: `.engine/check/migration-rollback.json`
  - policy: `.engine/policies/migration-discipline.md`
  - tool: `.engine/tools/migration_discipline/*.py`
- wires: none (this module adds no shared-state edits)

### `product-design` — version `0.2.0` (optional)

- depends on: `core`
- provides:
  - check: `.engine/check/product-adr-form.json`, `.engine/check/product-design-form.json`, `.engine/check/product-lock-integrity.json`, `.engine/check/product-spec-coverage.json`, `.engine/check/product-spec-form.json`, `.engine/check/product-spec-matrix.json`
  - codex-skill: `.agents/skills/engine-design/SKILL.md`, `.agents/skills/engine-design/agents/openai.yaml`
  - doc: `.engine/docs/product-design.md`
  - foundation: `.engine/product-spec-matrix.json`
  - operation: `.engine/operations/product-intake.md`
  - policy: `.engine/policies/spec-structure-integrity.md`
  - scaffold: `.engine/modules/product-design/scaffold/*.md`
  - skill: `.claude/skills/engine-design/SKILL.md`
  - tool: `.engine/tools/product_design/*.py`
- wires: codex-hook, hook

### `qa-review` — version `0.2.0` (optional)

- depends on: `core`
- provides:
  - agent: `.claude/agents/engine-qa-review-divergence-hunter.md`, `.claude/agents/engine-qa-review-security-governance.md`, `.claude/agents/engine-qa-review-spec-conformance.md`, `.claude/agents/engine-qa-review-technical-integrity.md`, `.claude/agents/engine-qa-review-usability.md`
  - codex-agent: `.codex/agents/engine-qa-review-divergence-hunter.toml`, `.codex/agents/engine-qa-review-security-governance.toml`, `.codex/agents/engine-qa-review-spec-conformance.toml`, `.codex/agents/engine-qa-review-technical-integrity.toml`, `.codex/agents/engine-qa-review-usability.toml`
- wires: none (this module adds no shared-state edits)

### `routine-mode` — version `0.2.0` (required)

- depends on: `core`
- provides:
  - codex-skill: `.agents/skills/engine-routine/SKILL.md`, `.agents/skills/engine-routine/agents/openai.yaml`
  - operation: `.engine/operations/routine-entry.md`
  - skill: `.claude/skills/engine-routine/SKILL.md`
- wires: none (this module adds no shared-state edits)

### `validators-core` — version `0.3.0` (required)

- depends on: `core`
- provides:
  - check: `.engine/check/agent-coherence.json`, `.engine/check/agent-frontmatter.json`, `.engine/check/agent-shape.json`, `.engine/check/audit-concern-list.json`, `.engine/check/audit-digest-fingerprint.json`, `.engine/check/audit-digest-staleness.json`, `.engine/check/block-coherence.json`, `.engine/check/build-protocol.json`, `.engine/check/catalog-completeness.json`, `.engine/check/catalog-coverage.json`, `.engine/check/census-completeness.json`, `.engine/check/ci-assurance-drift.json`, `.engine/check/codex-agent-coherence.json`, `.engine/check/codex-agent-schema.json`, `.engine/check/codex-hooks-schema.json`, `.engine/check/codex-provider-parity.json`, `.engine/check/codex-skill-coherence.json`, `.engine/check/codex-skill-frontmatter.json`, `.engine/check/codex-skill-shape.json`, `.engine/check/conduct-frontmatter.json`, `.engine/check/conduct-shape.json`, `.engine/check/conduct-weakening-guard.json`, `.engine/check/doc-frontmatter.json`, `.engine/check/doc-shape.json`, `.engine/check/engine-manifest.json`, `.engine/check/engine-todo-form.json`, `.engine/check/execution-state.json`, `.engine/check/executor-record.json`, `.engine/check/first-run-assets.json`, `.engine/check/first-run-reference-closure.json`, `.engine/check/hard-check-bite.json`, `.engine/check/in-tool-demo-failure-path.json`, `.engine/check/interface-coherence.json`, `.engine/check/interface-declaration.json`, `.engine/check/knowledge-coverage.json`, `.engine/check/knowledge-vocabulary.json`, `.engine/check/lane-removed.json`, `.engine/check/lens-consumption.json`, `.engine/check/link-integrity.json`, `.engine/check/manifest-write-funnel.json`, `.engine/check/memory-pointer-public-safety.json`, `.engine/check/model-bindings-schema.json`, `.engine/check/model-routing.json`, `.engine/check/module-catalog-drift.json`, `.engine/check/module-manifest.json`, `.engine/check/module-surfaces-drift.json`, `.engine/check/operation-frontmatter.json`, `.engine/check/operation-shape.json`, `.engine/check/operator-guarded-paths.json`, `.engine/check/operator-local-references.json`, `.engine/check/policy-frontmatter.json`, `.engine/check/policy-override-stale.json`, `.engine/check/policy-shape.json`, `.engine/check/pr-behaviors-declared.json`, `.engine/check/pr-body-completeness.json`, `.engine/check/pr-release-impact.json`, `.engine/check/provider-exceptions-schema.json`, `.engine/check/provider-vocabulary-confinement.json`, `.engine/check/provisioning-catalog.json`, `.engine/check/release-integrity.json`, `.engine/check/route-budget.json`, `.engine/check/route-target-existence.json`, `.engine/check/self-map-drift.json`, `.engine/check/setup-route-drift.json`, `.engine/check/shipped-issue-references.json`, `.engine/check/shipped-local-references.json`, `.engine/check/skill-coherence.json`, `.engine/check/skill-frontmatter.json`, `.engine/check/skill-shape.json`, `.engine/check/state-cursor.json`, `.engine/check/template-shape-spec.json`, `.engine/check/untracked-surface.json`, `.engine/check/uv-group-drift.json`
  - policy: `.engine/policies/provider-exceptions.json`
- wires: none (this module adds no shared-state edits)

### `audit-library` — version `0.3.0` (required)

- depends on: `validators-core`
- provides:
  - agent: `.claude/agents/engine-audit.md`
  - audits: `.engine/audits/audit-digest.md`, `.engine/audits/concern-list.json`, `.engine/audits/self-review-setup.md`
  - codex-agent: `.codex/agents/engine-audit.toml`
- wires: none (this module adds no shared-state edits)

## Commands and routes

How you reach your engine: the commands a person types, and the automatic routes the assistant may follow on your behalf (10 operator commands, 39 automatic routes).

### Operator commands

| command | what it does | module |
| --- | --- | --- |
| `engine-design` | Describe what you want to build, in plain words — I'll help you write it down clearly, check it holds together, and settle it as the description to build from. | `product-design` |
| `engine-help` | List the Engine's commands — what you can type and what each one does. | `core` |
| `engine-parts` | Show what your engine is made of — its version, the kinds of files it governs, and the modules installed and how they depend on each other. | `core` |
| `engine-recall` | Look up what this project already decided, tried, or learned in an earlier session, from its saved memory. | `core` |
| `engine-release` | Cut and publish a new release of this project — your product's version in a deployed repo, the engine's own in its home repo. | `core` |
| `engine-routine` | Set up unattended work — let me advance a planned build on a schedule while you're away, adding each change to a pull request for your approval. | `routine-mode` |
| `engine-setup` | Set up the Engine in a new project, and afterwards manage add-ons, conduct, reviewers, protection, backup, and settings. | `core` |
| `engine-start` | Start building — switch from looking around to making changes, which I'll put up for your approval. | `core` |
| `engine-status` | Show where your project stands — what's next, what recently shipped, and anything that needs your attention. | `core` |
| `engine-upgrade` | Check for, apply, or undo an engine update — see exactly what an update would change, apply it as a pull request you review, or safely undo a half-finished or unwanted one. | `core` |

### Automatic routes

Routes the assistant may follow on its own to reach an engine workflow, with what each points at and whether that target is always present, module-conditional, or home-only.

A note on runtimes: on Claude these routes are hidden from the operator's typed menu (they are model-only, so `engine-help` never lists them); on Codex, which has no hidden-route selector, the same routes are also explicitly visible and typeable. That is the one deliberate provider asymmetry, and this map is where it is disclosed.

| route | reachable as | points at | module |
| --- | --- | --- | --- |
| `engine-change-conduct` | model-only | operation `.engine/operations/conduct-author.md` (active) | `core` |
| `engine-check-impact` | model-only | operation `.engine/operations/knowledge-impact-check.md` (active) | `core` |
| `engine-configure-codex` | model-only | operation `.engine/operations/codex-settings.md` (active) | `core` |
| `engine-configure-memory-backup` | model-only | tool `.engine/tools/memory/backup_vault.py` (active) | `core` |
| `engine-coordinate-build` | model-only | operation `.engine/operations/build-orchestration.md` (active) | `core` |
| `engine-design-product` | model-only | operation `.engine/operations/product-intake.md` (module-conditional) | `core` |
| `engine-develop-engine` | model-only | operation `.engine/operations/engine-development.md` (home-only); operation `.engine/operations/owned-product-build.md` (active) | `core` |
| `engine-drop-operator-pin` | model-only | tool `.engine/tools/memory/pins.py` (active) | `core` |
| `engine-enable-protection` | model-only | operation `.engine/operations/control-plane-bootstrap.md` (active) | `core` |
| `engine-file-engine-issue` | model-only | tool `.engine/tools/issue_author.py` (active) | `core` |
| `engine-file-upstream-issue` | model-only | operation `.engine/operations/external-contribution-issue.md` (module-conditional) | `core` |
| `engine-install-engine` | model-only | operation `.engine/operations/engine-arrival.md` (active) | `core` |
| `engine-manage-addons` | model-only | operation `.engine/operations/module-add.md` (active); operation `.engine/operations/module-remove.md` (active) | `core` |
| `engine-manage-plans` | model-only | operation `.engine/operations/plan-orchestration.md` (active) | `core` |
| `engine-manage-programs` | model-only | operation `.engine/operations/program-orchestration.md` (active) | `core` |
| `engine-manage-setup` | model-only | skill `engine-setup` (active) | `core` |
| `engine-onboard-project` | model-only | operation `.engine/operations/onboarding-read.md` (active) | `core` |
| `engine-prepare-routine` | model-only | skill `engine-routine` (active) | `core` |
| `engine-recall` | model-auto | operation `.engine/operations/memory-recall.md` (active); tool `.engine/tools/memory/recall.py` (active) | `core` |
| `engine-release-project` | model-only | operation `.engine/operations/engine-release.md` (active); operation `.engine/operations/projects-release-advance.md` (module-conditional) | `core` |
| `engine-remove-engine` | model-only | operation `.engine/operations/engine-remove.md` (active) | `core` |
| `engine-restore-operator-pin` | model-only | tool `.engine/tools/memory/mcp_server.py` (active) | `core` |
| `engine-save-operator-pin` | model-only | tool `.engine/tools/memory/pins.py` (active) | `core` |
| `engine-setup-dependency-discipline` | model-only | skill `engine-setup` (active) | `core` |
| `engine-setup-design-review` | model-only | skill `engine-setup` (active) | `core` |
| `engine-setup-external-contribution` | model-only | skill `engine-setup` (active) | `core` |
| `engine-setup-github-projects-sync` | model-only | skill `engine-setup` (active); operation `.engine/operations/projects-sync-setup.md` (module-conditional) | `core` |
| `engine-setup-memory-semantic-recall` | model-only | skill `engine-setup` (active) | `core` |
| `engine-setup-migration-discipline` | model-only | skill `engine-setup` (active) | `core` |
| `engine-setup-product-design` | model-only | skill `engine-setup` (active) | `core` |
| `engine-setup-qa-review` | model-only | skill `engine-setup` (active) | `core` |
| `engine-show-help` | model-only | tool `.engine/tools/engine_help.py` (active) | `core` |
| `engine-show-parts` | model-only | tool `.engine/tools/self_map.py` (active) | `core` |
| `engine-show-status` | model-only | tool `.engine/tools/engine_status.py` (active) | `core` |
| `engine-submit-upstream-contribution` | model-only | operation `.engine/operations/external-contribution-submit.md` (module-conditional) | `core` |
| `engine-switch-reviewers` | model-only | operation `.engine/operations/engine-team-switch.md` (active) | `core` |
| `engine-tune-settings` | model-only | operation `.engine/operations/tune-policy.md` (active) | `core` |
| `engine-upgrade-engine` | model-only | operation `.engine/operations/engine-upgrade.md` (active) | `core` |
| `engine-validate-codex` | model-only | operation `.engine/operations/codex-validation.md` (active) | `core` |
