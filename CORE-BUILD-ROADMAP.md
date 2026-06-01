# Core build roadmap — the Builder-A `core` decomposition

> **Maintainer-layer construction scaffold (Builder-A genesis).** This is not an engine surface and not
> a deployed artifact; it is the approved map for building the locked `core` module PR by PR, the same
> tier as the construction `CLAUDE.md`. It carries no `stub|designed|locked` status. It is **retired —
> deleted — at M1** as the core build's final task (see **Retirement** below); it must never ship in v1
> or travel to a generated repo. Because the template is not generated-from until v1, and this file is
> gone before then, it never reaches an adopter.

## Context

The `engine-template` stage-0 seed is built and merged (PR #1): the construction `CLAUDE.md`, the uv
tool-runtime, the seed validator + the two guards + self-tests, the seven-field check rules, the
8-section PR template, and the two frozen CI checks (`engine-ci`, `engine-guard`). The protected-`main`
ruleset is applied as a setting.

The milestone is **M1** — the point where the half-built engine becomes a better builder of itself than
the hand-governed genesis, and Builder B takes over (`../engine-planning/wbs/module-order.md` §5,
`../engine-planning/wbs/stage-0-harness.md` §8). M1 needs five things: `repository-topology` → `core` →
`validators-core` + `memory-substrate-sqlite-fts5` + the control-plane bootstrap.

This roadmap (1) records `repository-topology` as **satisfied-by-seed**, and (2) decomposes the locked
`core` module into an ordered sequence of small, individually-mergeable PRs, pinning/flagging the
build-spec leaves `core` defers. Each slice is built in its own later session under the two gates (plan
gate before, deliverable gate before merge). The whole shape was approved first so the maintainer never
merges into `core` blind, slice by slice.

**Why a roadmap and not invention:** the WBS deliberately treats `core` "at module granularity" and does
**not** decompose it — "its internal construction follows the stage-0 supersession ratchet"
(`module-order.md` §3). So the *slice boundaries and ordering* below are the deferred build-spec
decision. Every slice maps to an already-locked design component; **no slice invents structure** beyond
the locked design. Where `core` defers a concrete value, this roadmap **flags** it for explicit
maintainer decision at the slice — it never silently picks one.

**Status:** plan-gate cold-audited (≥4 independent lenses) and approved; the findings and dispositions
are in the **Plan-gate record** section below, and the accepted fixes are folded into the slices. Now
committed as the durable build map future Builder-A sessions resume from.

---

## Decision recorded: `repository-topology` is satisfied by the seed (no separate topology PR)

`repository-topology` (locked, `../engine-planning/systems/infrastructure/repository-topology/README.md`)
fixes **"laws, not leaves"** — the filesystem partition and placement laws, not a directory tree to stamp
out. Against the merged seed:

- **The partition it fixes is already laid** — `.engine/`, the engine-owned `.github/` files, the root
  `CLAUDE.md`, `.gitignore`. The pieces not yet present (`.claude/`, `.mcp.json`, `.engine/boot/`,
  per-surface `.engine/<surface>/` dirs) are explicitly **Tier-2**: each attaches additively with the
  system that owns it (they arrive *with* `core` slices), never from a standalone topology step
  (topology README "Two tiers: laws now, leaves later").
- **CODEOWNERS is deliberately absent here.** `stage-0-harness.md` §6 (the "engine == product
  degeneracy"): in the construction repo the engine *is* the product, the wall is vacuous, and CODEOWNERS
  is "neither a hand-built seed piece nor a superseded one — it is simply absent." The CODEOWNERS-rendering
  *machinery* ships later inside `core`/provisioning and travels to *generated* repos; it is never
  instantiated here.

**Conclusion (plan-gate confirmed sound):** there is **no separate `repository-topology` PR**. It is a
precondition the seed met; its only un-laid pieces are Tier-2 subtrees that arrive with their owners.

---

## The `core` decomposition (ordered)

Per-slice fields: **Delivers** · **Deps** (hard predecessors) · **Supersedes** (seed-ratchet row, if
any) · **Demo** (operator-runnable fail-then-pass) · **Consent** (⚖️ = checker-of-checkers / weighty
consent surface) · **Leaves** (build-spec leaves to flag, not decide). Within a phase, order is free
unless a Dep says otherwise; the only hard rule is no slice precedes a slice it depends on.

**Catalog amend-first handling (applies to every surface-introducing slice):** per the ontology
amend-first rule, a surface is named in the catalog *before* any instance is authored. Slice 1 enumerates
the **full v1 surface set** — including the surfaces that never get a dedicated slice (`tool`,
`operations`, `check`, `schema`, `template`) — OR each surface-introducing slice carries an explicit
amend-the-catalog-first sub-step. Decide which at slice 1; either way no instance lands without its
catalog record.

### Phase 0 — Preconditions (already satisfied)
- `repository-topology` — satisfied-by-seed (above).
- uv tool-runtime — a genesis precondition materialized identically before/after M1; **not a core
  slice** (`module-order.md` §2).
- protected-`main` ruleset — a seed *setting* (the M1 "control-plane bootstrap"); the re-runnable
  bootstrap *operation* is slice 25.

### Phase 1 — Grammar root (the dependency floor)
1. **Ontology meta-contract + surface catalog + coverage.** *Delivers:* the surface meta-contract record,
   the single schema-governed catalog (one record per surface; self-referential core = `contract`,
   `policy`, `schema`; **the full v1 surface set enumerated**, incl. `tool`/`operations`/`check`/
   `template`), the amend-first rule, coverage-staleness notion. *Deps:* none — **the first core slice;
   the root everything dereferences.** *Supersedes:* —. *Demo:* hand-edit a surface out of the catalog /
   add an `.engine/<surface>/` with no record → coverage drift surfaces (full teeth once slice 4 lands);
   restore → clean. *Consent:* ⚖️-light — **contagion note:** the catalog root propagates to every
   surface in every generated project; carry a named-residual consent line even though it has no runtime
   behavior. *Leaves:* catalog exact fields; each surface's engine-prefixed identifier scheme; **each
   prose surface's `location` directory name** (authored, not derivable — see slice 13).
2. **`schema` surface + the `finding.v1` base.** *Delivers:* the `schema` surface home + the canonical
   `finding.v1` base `{severity, message, location}` (D-113/D-115/D-118) every checker/finding rides.
   *Deps:* 1. *Supersedes:* —. *Demo:* a finding omitting a required base field fails schema validation;
   add it → passes. *Consent:* normal. *Leaves:* per-finding JSON Schema fields under the base.
3. **`template` surface machinery.** *Delivers:* template = prose skeleton + structured shape-spec
   governed by a schema; the `catalog → template → shape-rules → instance` path; sections as the control,
   length as a `soft-warn` budget. *Deps:* 1, 2. *Supersedes:* —. *Demo:* author a prose surface from a
   template, drop a required section → shape check fires; restore → passes; over-length → soft note only.
   *Consent:* normal. *Leaves:* —.

### Phase 2 — Validation engine (early keystone) ⚖️
4. **Dispatcher + `schema`/`presence`/`shape` kinds + suites + triggers.** *Delivers:* the thin dispatcher
   (loads rule data, routes to kind callable, reports by tier, fails closed on dangling kind in CI), the
   kind-callable result contract on `finding.v1`, three of five closed kinds, suite declarations,
   trigger set (`pre-commit`/`pre-close`/`CI`/`audit-prep`), dangling-kind→finding. *Deps:* 1, 2.
   *Supersedes:* the **seed PR-body completeness check → the validation `presence` kind** (a ratchet row);
   the dispatcher supersedes the seed validator's *machinery* — **core ships the engine + zero/seed rules
   only; the engine-self-validation rule corpus rides `validators-core` at L2 (D-089), so the ratchet row
   is not "completed" here.** *Demo:* run the dispatcher over the existing seed rule data — empty PR
   section → red via the `presence` kind; fill → green; point a `schema` rule at a malformed file → red.
   *Consent:* ⚖️ (the validator itself). *Leaves:* the seven-field check-rule JSON Schema; concrete
   trigger-set values; **whether the `schema`/`shape` kinds need a non-stdlib library (e.g. a JSON-Schema
   2020-12 validator) — if so, the CI uv-group seam applies (cross-cutting flag below).**
5. **`coverage` + `coherence` + `custom/script` kinds; re-home the two guards.** *Delivers:* the
   remaining closed kinds (coverage = catalog-coverage + link-integrity; coherence = directly-callable
   library entry); the `custom/script` escape-hatch kind; **re-home the protection-detection guard and
   the guardrail-weakening guard as frozen-named `custom/script` rules** (validation README — "a
   frozen-named `custom/script` check whose check wiring this foundation owns"). *Deps:* 4, 1. *Supersedes:*
   the seed validator's machinery folds into core's dispatcher (corpus still arrives at `validators-core`).
   *Demo:* rule naming an unregistered kind → fails closed in CI; correct → green. Push a
   guardrail-weakening diff → still blocks → `guardrail-ack` clears; **plus the security sub-demo: a PR
   that edits the guard's own rule code does NOT get to run its edited version** (proves the guard still
   evaluates from trusted base). *Consent:* ⚖️ **maximal; litigation-adjacent.** Two hard invariants the
   slice plan gate must verify: (a) **frozen names `engine-ci`/`engine-guard`/`guardrail-ack` are
   preserved** (a rename is itself guardrail-weakening); (b) **the weakening guard's *execution* stays on
   the `pull_request_target` workflow that checks out base only and reads the diff only, never head** —
   the `custom/script` *check-kind* may live in the dispatcher, but moving the guard's execution into the
   `pull_request`/`engine-ci` context would silently regress the D-051 "a PR cannot disarm its own guard"
   property. *Leaves:* the module-provided check-kind discovery directory + filename↔kind convention (not
   a core-lock blocker).

### Phase 3 — Module composition
6. **Module-system manifest grammar + coherence consumer + engine-manifest hand-seeding.** *Delivers:* the
   manifest grammar at `.engine/modules/<id>/manifest.json` (governed by a `schema`-kind check, not
   catalogued), installed-set-is-present model, the dependency presence/acyclicity/range **coherence
   kind** invoked directly by the module manager (not a rule); the hand-seeded **engine manifest** gains
   its first consumer. *Deps:* 4, 5, 1. *Supersedes:* — (the engine manifest is a **handoff**, not a
   ratchet supersession: hand-seeded here, inherited by the module manager at M1, slice 25). *Demo:* an
   engine file claimed by no `provides` → orphan finding; claim it → green; declare a `depends` on an
   absent module → unsatisfied-dependency finding. *Consent:* moderate. *Leaves:* manifest JSON Schema;
   **engine-manifest genesis values — the in-development version sentinel (illustratively `0.0.0-dev`,
   exact token a leaf), the append-as-built list, and the `solo` identity tier.** Surface the `solo` tier
   as a **plain-language consent point**: per §15, `solo` means weakening cannot happen *silently* (the
   maintainer is the only second check), not that it cannot happen.
7. **The wiring library (closed seam vocabulary) + the comment-fenced-block helper.** *Delivers:* the
   permanent `.engine/tools/` library — paired applier+reverser per closed seam directive
   (`hook`/`mcp`/`ontology-entry`/`permission`/`gitignore`), engine-namespaced identity, idempotent,
   insert-iff-absent / remove-only-engine-identified — **plus the shared comment-fenced-block helper that
   the foundation `.gitignore` block and the (generated-repo) CODEOWNERS renderer both reuse (a library
   helper, NOT a module `wires` seam directive — `module-system` §Coherence, `provisioning`
   §Tool-runtime).** *Deps:* soft on 6 (the library is a manifest-agnostic primitive; manifest `wires`
   consumption is bound by provisioning at slice 25 — the library can be built and demoed standalone).
   *Supersedes:* — (net-new R5 machinery). *Demo:* apply a `gitignore` directive twice → second is a
   no-op; add an operator's identical-looking line, reverse the engine directive → only the engine-fenced
   line is removed. (The `hook`/`permission` appliers are demoed at/after slice 20, once the committed
   `.claude/settings.json` exists — see cross-cutting flag.) *Consent:* ⚖️ (the R5 firewall; add an
   adversarial security lens). *Leaves:* the concrete permission strings core's kernel ops need; the
   gitignore comment-fence marker; the `${CLAUDE_PROJECT_DIR:-.}` MCP entry form.
8. **Self-map (surface-level + wiring-graph).** *Delivers:* the generated, committed, fingerprint-gated
   self-map — surface-level portion (derivable after slice 1) and the wiring-graph portion (derived from
   present manifests). *Deps:* 1, 6. *Supersedes:* —. *Demo:* regenerate after a surface/manifest change →
   map updates; hand-edit drift → fingerprint check red; regenerate → green. *Consent:* normal. *Leaves:*
   the operator-reachable "what is my engine made of" access path (a provisioning/operations UX leaf).

### Phase 4 — Cognitive floors
9. **State cursor + schema.** *Delivers:* the one schema-validated committed machine-state file (pointers
   + counts only), its schema, halt-on-malformed posture (refuse + surface, never crash). *Deps:* 1, 2, 4.
   *Supersedes:* —. *Demo:* corrupt the state file to schema-invalid → refusal notice fires (not a crash);
   restore → clean. (A schema-valid-but-wrong cursor is known-unbounded — flag; the catch is the operator's
   diff review.) *Consent:* normal. *Leaves:* the exact field set + JSON Schema.
10. **Knowledge entities + schema + generator + fingerprint coverage gate.** *Delivers:* committed
    per-surface knowledge entities + schema, the derived generator (walks every catalog surface), the
    fingerprint coverage check (a `coverage`-kind rule; the unbypassable CI backstop forcing regen).
    *Deps:* 1, 4, 5, 2. *Supersedes:* —. *Demo:* change a surface without regenerating → coverage check
    red at CI; regenerate + commit → green. *Consent:* moderate (hard CI backstop). *Leaves:* entity/edge
    schema, generator, per-surface coverage (the v1 surface set is settled, D-042 — not a leaf).
11. **Knowledge derived index + graph-query MCP + interface protocol declarations.** *Delivers:* the
    gitignored derived index + boot slice (regenerable), the engine-prefixed graph-query MCP server (the
    **core-shipped fallback floor** realizing the knowledge-retrieval op-set, D-116), and the `interface`
    surface + the **knowledge-retrieval** and **`search`** protocol contracts (op-set pinned by D-116:
    `get-entity`/`find`/`neighbors`/`relate`; core holds the protocol; implementations bind by presence).
    **Note the asymmetry:** knowledge-retrieval's fallback impl (the graph-query MCP) ships in core here;
    **`search` is protocol-only in core — its FTS5 implementation + MCP are `memory-substrate`'s (L2),** so
    the interfaces "fallback is a shipped foundation tool" law is satisfied for `search` only when
    memory-substrate lands. *(Consider splitting: 11a = knowledge-retrieval interface + the graph-query
    MCP fallback; 11b = the `search` protocol-only declaration. Decide at the slice.)* *Deps:* 10, 7, 1.
    *Supersedes:* —. *Demo:* **first perform the one-time operator MCP approval** (the platform security
    prompt — the engine writes the `.mcp.json` definition but must NOT auto-write the approval flag,
    `module-system` §MCP registration); then query the MCP for neighbors → adjacency from committed
    entities; **delete the gitignored index / leave the server unapproved → committed entities still
    answer, loudly surfaced (degrade-to-git-native);** two non-default conforming impls → single-active
    coherence finding. *Consent:* normal. *Leaves:* per-op JSON Schemas; selection/precedence;
    fallback-ack mechanism; discovery handle; derived-index format; **the operator-approval seam is
    operator-owned, never engine-written.**
12. **Attention policy + ranking tool.** *Delivers:* the attention policy (budget, ranking weights, trim
    order, debt-blocking rule, scent threshold) + the ranking function realizing the ordered-partition +
    weighted-intra-partition form (D-117) — deterministic, reference-time an explicit input, no ML/decay,
    degrades over partial inputs. *Deps:* 9, 11 (structural adjacency via the `neighbors` path). *Supersedes:*
    —. *Demo:* over a **constructed fixture candidate-set** (not live debt/knowledge — telemetry's debt
    register, slice 18, need not exist yet), a blocking-debt candidate orders ahead **structurally
    regardless of weights** (remove the partition precedence → the feature floats up → restore); drop a
    substrate input → ranks over the rest. *Consent:* normal **with a named residual on the merge consent
    surface (§17):** the structural demo proves partition assignment, but ranking *quality* (does it
    surface the right things first?) is unproven until the values are calibrated — name this, do not let
    it read as demonstrated. *Leaves:* **all concrete values** — budget splits, weights, partition
    precedence, trim order, thresholds, calibration inputs (the single largest leaf-cluster; the *form* is
    pinned, the values are not).

### Phase 5 — Decision / governance surfaces
13. **Contracts surface + template + `eADR-####` scheme + eADR stream.** *Delivers:* the `contract`
    grammar (one file per decision, `decision` lifecycle `proposed→accepted→superseded`, template
    sections Decision/Significance/Rationale/Anti-choice/Status, supersede-on-replacement), the
    `eADR-####` identifier scheme, and the append-only eADR stream home. *Deps:* 1, 3, 2. *Supersedes:* —.
    *Demo:* author a contract missing Anti-choice → shape/presence check flags it; add substantive
    anti-choice → passes; supersede one → index lists both, superseded retained. *Consent:* normal.
    *Leaves:* the **`contract` surface's catalog `location` directory name** (authored, not derivable —
    cf. the locked `interface`→`.engine/interfaces/` precedent; do not assume `.engine/contracts/`);
    contract template skeleton + length budget; `eADR-####` padding/slug; frontmatter schema + date
    format; whether any eADR instances are backfilled or the stream ships empty.
14. **Policies surface + the four v1-core policies.** *Delivers:* the `policy` grammar (template:
    Rule/Scope/Rationale/Enforcement-tier) + the four instances: **contract-threshold**,
    **finding-disposition**, **escalation**, **triage-threshold**. (Consider splitting grammar from the
    four instances if large.) *Deps:* 1, 3, 13. *Supersedes:* —. *Demo:* the contract-threshold
    `hard-fail` presence pairs with slice 4; the operator opening a policy file is "informed, not alarmed"
    (plain-language readout). *Consent:* partial (contract-threshold + finding-disposition are
    trust-model; their *enforcement* slices carry the weight). *Leaves:* policy template + budget; the
    `policy` surface `location` directory name; **all numeric threshold values** (triage persistence/
    auto-resolve/pressure; contract-threshold rate anomaly) — maintainer-set, never silently picked.
15. **Control-plane PR-body contract → core ownership.** *Delivers:* binds the live 8-section PR contract
    + the Review section to **core** ownership/catalog/coherence; pins the Review layout (depth/lenses/
    dispositions/post-audit-fix delta) and its in-block honesty caveat. *Deps:* 13. *Supersedes:* — (the
    PR template + ruleset are stage-0 **no-proto native artifacts**, not a ratchet row; this slice binds
    their *ownership* to core, confirming not rebuilding — `stage-0 §6`). *Demo:* empty Review →
    `engine-ci` red; fill → green (the seed proof re-pointed at the core-owned contract); render a
    post-audit-fix delta in plain language. *Consent:* ⚖️. **LITIGATION WATCH:** never drop/rename
    `Review` or trim a section — that is a change to a locked system; raise the alarm, do not quietly trim.
    *Leaves:* the `provides`-vs-foundation-infra ownership boundary of the PR template file.
16. **Agent persona-template grammar.** *Delivers:* the persona template grammar + roster-assembly:
    routing fields `role` (closed) / `lens` (open, review-roles only) / `model-tier` (closed) /
    `permissions` / `output-contract` (findings on `finding.v1`); coherence rules (closed `role`;
    lens-on-non-review-role is a finding; 0..N agents per lens). Ships grammar only — **no persona
    instance** (D-066). *Deps:* 1, 2, 5. *Supersedes:* —. *Demo:* an agent with a `role` outside the
    closed set → coherence finding; a `worker` declaring a `lens` → symmetric finding. *Consent:* normal.
    *Leaves:* routing-field schema field names; `model`/`effort` passthrough convention; per-consumer
    `severity` enums. **Do not rename `model-tier`** (D-100 defers it).

### Phase 6 — Lifecycle spine + guardrail machinery
17. **Hook-script contract + block-budget law.** *Delivers:* the engine hook-script invocation contract
    (`${CLAUDE_PROJECT_DIR}/.engine/.venv/<bin>/python …`, per-OS, never bare python / never `uv run` on a
    hot path), the stdin-event-JSON / exit-code-2-blocks / `additionalContext` contract, the block-budget
    law (starts empty; fail-open-and-flag). *Deps:* tool-runtime (seed). *Supersedes:* —. *Demo:* a hook
    exiting 2 blocks; any other non-zero is non-blocking (fail-open); a missing `.venv` → non-blocking.
    *Consent:* ⚖️ (the fail-open safety floor under every local gate). *Leaves:* the block-cap default
    (**verify the current Claude Code platform default at the slice plan gate; do not hardcode a remembered
    number**) and whether the engine overrides it; per-OS interpreter path form.
18. **Telemetry detect→surface machinery.** *Delivers:* the consume/dedup/promote/auto-resolve loop over
    the `finding.v1`-extended record (`source-id` + first/last-seen); triage = the single autonomous write
    (open/update an engine-labeled Issue), source-keyed dedup; severity-class promotion latency;
    auto-resolve; the render-only triage-pressure stream. *Deps:* 2, 14 (triage-threshold policy), state
    (9), control-plane label scheme. *Supersedes:* —. *Demo:* a trust-critical signal opens an Issue
    immediately; re-fire → same Issue updates (dedup, not a duplicate); remove cause → auto-resolves.
    *Consent:* partial (autonomous-write / never-self-heal is load-bearing). *Leaves:* the telemetry
    finding-record + **its own ambient-capture record (the `PostToolUse` check-fire record, telemetry-owned
    per D-118)** schemas + cache paths; stream-cache layout. (Threshold *values* live in slice 14's policy,
    not here. **Distinct from** memory's `Stop` turn-delta capture [slice 22, memory L2] and close's
    ephemeral per-turn findings-record [slice 22] — three separate records, do not conflate.)
19. **Deployed-floor root `CLAUDE.md` + `doc` surface + operator orientation doc.** *Delivers:* the thin,
    always-loaded **deployed-floor** `CLAUDE.md` for *generated* repos (orientation pointer,
    memory-authority routing, engine/product wall, the named-marker double-fault) shipped as a **new
    traveling artifact**; the `doc` surface grammar; the one named v1 operator orientation doc (what the
    engine is, how to discover commands self-sufficiently, that audit retirement-proposals are normal).
    *Deps:* 1, 3, 9. *Supersedes:* the **construction `CLAUDE.md` → core grammar + boot floor** ratchet
    row (the boot-floor half; the grammar half is Phases 1–5). *Demo:* a fixture repo boots (hook
    disabled) → the floor's pointer resolves and the named-marker instruction renders; remove the
    orientation doc → broken pointer caught. *Consent:* ⚖️ (a §17 trust-floor artifact + plain-language
    leak-guard). **LITIGATION WATCH:** the deployed-floor `CLAUDE.md` ships **alongside**, never overwrites,
    the construction `CLAUDE.md` — stage-0 §6 says the two bodies "stay distinct." *Leaves:* the root
    `CLAUDE.md` copy; the doc template + the orientation prose (maintainer reads for plainness); the
    `doc` surface `location` name; the project-status-card title/token (co-pinned with slice 20).
20. **Boot SessionStart pack.** *Delivers:* the `SessionStart` boot pack under `.engine/boot/`, the
    orientation-event model, assembly that reads committed state and pulls knowledge/memory when up,
    alarms in pinned priority (governance-critical first + distinct, then attention-ranked debt, then
    digests), the "recently shipped" digest, the substrate-deferred surfacings, the loud-degrade notice.
    Boot **relays** (core "integrates, owners detect"): it **surfaces and nags** an unprotected `main` and
    other substrate states, binding to channels rather than rosters. **Wires `SessionStart`.** *Deps:* 17,
    19, 7, 9, 10, 11, 12, 18. *Supersedes:* the boot-pack half of the construction-`CLAUDE.md` ratchet.
    *Demo:* boot → the project-status card renders; make `main` unprotected → governance-critical alarm
    **surfaces first + distinct** (the *one-click fix* is demoed at slice 25, where the bootstrap operation
    lives — boot only nags here); take a substrate down → loud-degrade line names it; corrupt the state
    cursor → boot says "I couldn't read where the project stands" and falls to the floor (never a
    SessionStart halt — the platform cannot block on SessionStart). *Consent:* ⚖️ (densest integrator;
    orientation trust floor). **→ M1 boot-orient.** *Leaves:* boot-pack format; surfaced-item
    ordering/rendering within the alarms-pinned law; project-status-card title/token (= slice 19).
21. **Modes Explore write-gate + stance signal.** *Delivers:* the three-stance model (Explore default /
    Build / Routine); the session-scoped, non-committed stance signal cleared at every `SessionStart`
    (ambiguity → Explore); the `PreToolUse` write-gate active only in Explore, denying the enumerated
    building set and allowing everything else (no default-deny on unclassifiable); the operator-legible
    announcement + denied-action sentence. **Registers a block-budget member; wires `PreToolUse`.** *Deps:*
    17, 7, 20 (the SessionStart clear must agree). *Supersedes:* the **ad-hoc maintainer write-discipline →
    modes Explore write-gate** ratchet row. *Demo (honest §6-nudge tier):* in Explore, ask for a **git/gh
    build verb** and a file-mutating tool the gate classifies → gate denies with the plain "we're
    exploring; tell me to build" sentence; a read-only test → allowed; resume a "Build" session → boots
    back in Explore. **Name the limitation:** the gate is a local §6 nudge — the platform may not block
    every edit-tool path (modes README: "the platform ignores a `PreToolUse` deny for some edit paths,
    D-051 verified"; **confirm the current platform behavior at the slice plan gate**), and the protected-
    branch merge wall is the only guarantee. *Consent:* ⚖️ (block-budget member; self-election firewall).
    **→ M1 write-gate.** *Leaves:* the stance-signal representation; the exact denied-action match list
    (file-mutating tools, GitHub-MCP PR-creation tool, `git`/`gh` building-verb patterns); the
    announcement/denied copy.
22. **Close Stop disposition gate + ambient-capture trigger.** *Delivers:* the `Stop` hook doing two
    things — triggering memory's ambient capture (triggers, never gates) and the finding-disposition gate
    (hard-blocks the turn while an undispositioned finding sits in the **ephemeral, session-scoped,
    off-repo findings-record** [a record distinct from telemetry's and memory's]; degrades to *logged* at
    the cap; fails open with a notice; satisfiable non-interactively for routine via log-it). **Registers
    the second block-budget member; wires `Stop`.** *Deps:* 17, 7, 14 (finding-disposition policy), 18.
    *Supersedes:* —. *Demo:* a turn that raises a concern won't end until dispositioned, then shows "1
    fixed, 1 saved"; drive blocks to the cap → the finding is logged and the turn force-ends (never a
    deadlock). *Consent:* ⚖️ (trust spine; second block-budget member). *Leaves:* the ephemeral
    findings-record representation; the disposition-summary + loop/cap/fail-open copy. **Seam:** ambient
    capture is *memory's* turn-delta mechanism — the trigger relays; its consumer lands functional with
    `memory-substrate-sqlite-fts5` (L2, after core).
23. **Knowledge commit-boundary regen hook + attention scent hook.** *Delivers:* the `PreToolUse`
    git-intercept that triggers knowledge's commit-boundary regen, and the `UserPromptSubmit` attention
    scent (the cheap lexical push of attributed, unverified pointers; selective, de-duplicated, fail-open,
    rate-limited; injection never blocks). *Deps:* 17, 7, 11. *Supersedes:* —. *Demo:* a `git commit`
    fires the regen on the boundary; a prompt mentioning a known topic injects an attributed pointer, never
    woven recall; crash the scent → injects nothing, never stalls, failure de-duplicated. *Consent:* normal
    (neither is block-eligible). *Leaves:* scent rendering/attribution + rate-limit; regen git-command
    match patterns (co-pin with slice 21). **Seam:** the scent reads the **memory-substrate FTS5 index**
    (L2) — sequence so this does not land before that index exists (may span the M1 boundary).

### Phase 7 — Orchestration, provisioning, operator verbs
24. **Build-orchestration workflow.** *Delivers:* the orchestrating-session workflow — draft-PR-as-claim;
    the fixed gate skeleton (Plan / plan-review / Implement / Integrate / pre-submission review / Submit)
    with derived lenses + the empty-lens no-op (D-066); the plan gate's two beats (risk-assessment consent
    surface before the spend with consequence-named depth + cost/time + the §15 weakening headline; one
    synthesized call after); the green-validation-baseline precondition; the three implement strategies;
    single-writer integrate; filling the Review section at submit. *Deps:* 15, 21, 20, 3 (risk template),
    16 (roster), 4/5 (validation suite). *Supersedes:* realizes "plan-first, one step at a time" in
    machinery. *Demo:* Build on a one-line change → fast path (one glance, validation, merge); a
    guardrail-weakening change → the plan-gate headline names *which* protection weakens; submit without
    Review → completeness red; fill → green. *Consent:* ⚖️ (quality spine; §15 plan-gate consent). **→ M1
    drive-build-orchestration.** *Leaves:* the depth-level grammar; risk-assessment template wording;
    Review layout (= slice 15); build-Issue checklist/scope-lock format; routine non-interactive posture;
    worker per-persona `model`+`effort` (D-100).
25. **Provisioning permanent primitives.** *Delivers:* the **permanent** half of provisioning — the
    re-runnable control-plane bootstrap operation (`admin:repo_ruleset` check → `gh auth refresh` → apply
    augment-never-weaken ruleset → verify → loud-degrade) and the permanent module manager/updater
    (add/remove/upgrade, reverse-dependency-aware, group-scoped `uv sync` by module `id`, frozen-check-name
    invariant, engine-updater overlay). At M1 the module manager **inherits the hand-seeded engine
    manifest**; the CODEOWNERS renderer (for generated repos) consumes slice 7's comment-fenced-block
    helper. *Deps:* 7, 5 (coherence kind), 6 (manifest schema). *Supersedes:* — (the manifest handoff +
    the local-uv→auto-bootstrap handoff are **handoffs, not ratchet rows**). *Demo:* on a fixture repo with
    `main` unprotected, run bootstrap → approve the `gh` screen → protection lands + the boot alarm clears
    (**this is where boot's one-click-fix offer is demoed end-to-end**); re-run → idempotent; `add` a
    fixture module → coherence green; `remove` a depended-on module → refused in plain language. *Consent:*
    ⚖️ (the #1 trust dependency, R1; the uv consent gate). **→ M1 control-plane bootstrap mechanism +
    manifest handoff** (the *setting* already exists in the seed). *Leaves:* the pre-bootstrap explanation
    copy; the tool-runtime consent + degraded copy; the standing degraded-state banner; the concrete
    ruleset payload + API mechanics; the kernel-op permission strings (= slice 7).
26. **Operator verbs: Build-entry skill + `/engine-help`.** *Delivers:* the operator-typed **Build-entry**
    verb (`disable-model-invocation`; the only thing that flips the stance to Build; the model cannot
    self-invoke) and the **`/engine-help`** degradation-proof verb index (installed verbs parsed from
    committed `.claude/skills/*/SKILL.md` + legacy commands by real YAML parsing; available-if-installed
    verbs read from provisioning's committed catalog; closes with a pointer to the orientation doc; derives
    from committed sources, never an MCP substrate). *Deps:* skill surface (Phase 1 catalog), 19, 21
    (Build flips the stance), 24, 25 (catalog). *Supersedes:* the operator half of the write-discipline
    ratchet (the Build entry). *Demo:* type Build → stance flips (announced); confirm the model cannot
    invoke it; type `/engine-help` → verb list + descriptions + the doc pointer; kill the knowledge MCP +
    re-run → unchanged (degradation-proof). *Consent:* partial (Build-entry operator-only-invocability is a
    self-election safety property). *Leaves:* Build-entry verb name + wording; `/engine-help` rendering
    strings / listing order / the catalog's exact fields; orientation pointer copy.
27. **Provisioning instantiator (ships-to-travel, NOT exercised in the construction repo).** *Delivers:*
    the thin self-deleting first-run instantiator (gather → confirm → apply → verify → retire; the
    module-selection walkthrough, token derivation, manifest checkpoint/resume, brownfield collision
    check, self-deletion). *Deps:* 25 (composes its permanent primitives), 6 (full catalog + manifest).
    *Supersedes:* —. *Demo:* **runs against a throwaway generated/fixture repo, never the construction
    repo,** shipped with its own **plain-language non-engineer runbook** (held to the stage-0 §2
    operator-runnable-checklist bar) — generate a fixture, run the instantiator, confirm deselection,
    CODEOWNERS render, runtime materialize behind consent, bootstrap attempt, self-deletion; re-enter an
    interrupted run to show resume. *Consent:* ⚖️ (operates ungated until its apply phase installs the
    write-gate). **DEGENERACY — the loudest line on this slice's consent surface:** the instantiator never
    runs in the construction repo (engine == product; no product tenant; no CODEOWNERS rendered; manifest
    hand-seeded), so its only evidence is a demonstration on an **AI-built stand-in fixture**, never the
    repo being merged into. Clicking merge accepts the inductive step "works on the fixture ⇒ works for a
    real adopter," which the fixture cannot discharge — name this plainly, do not bury it. *Leaves:* all
    selection/consent/walkthrough copy.

---

## Build-spec leaves register (decided explicitly with the maintainer at each slice, never silently)

Grouped; each is **flagged, not pre-decided**. The slice that pins it is in brackets.

- **Schemas/grammar:** catalog exact fields + per-surface identifier schemes [1]; **each prose surface's
  `location` directory name — authored, not derivable** (`contract` [13], `policy` [14], `doc` [19]; cf.
  the locked `interface`→`.engine/interfaces/` precedent); `finding.v1` per-finding fields [2]; the
  seven-field check-rule schema [4]; the manifest schema + genesis values (in-dev sentinel token,
  append-as-built, **solo** tier) [6]; the state field set + schema [9]; knowledge entity/edge schema [10];
  per-op interface JSON Schemas + discovery handle [11]; contract/policy/doc templates + frontmatter
  schemas + `eADR-####` scheme [13/14/19]; agent routing-field schema [16].
- **Values requiring maintainer judgment:** **all attention values** (budget, weights, partition
  precedence, trim order, thresholds, calibration) [12]; **all policy threshold numbers** (triage
  persistence/auto-resolve/pressure; contract-threshold rate) [14]; the block-cap default (verify at slice)
  [17].
- **Discovery/wiring:** the module-provided check-kind discovery directory + convention [5]; the kernel-op
  permission strings + gitignore fence + MCP entry form [7]; the operator MCP-approval seam (operator-owned)
  [11]; the project-status-card title/token [19/20].
- **Genesis build seams (from the plan gate):** whether the `schema`/`shape` kinds need a non-stdlib
  JSON-Schema validator, and the **CI `uv sync` group-selection policy** (`--all-groups` vs per-id
  `--group`) hand-maintained until slice 25's module manager derives it [4 → 25]; whether routine
  `guardrail-ack` on construction PRs is accepted as-is or the classifier is refined at slice 5 [5].
- **All operator-facing copy (plain-language, leak-guard):** boot-pack format + surfaced ordering [20];
  write-gate announcement/denied copy [21]; close summary/notices [22]; depth-level grammar +
  risk-assessment wording + Review layout [24/15]; bootstrap/consent/degraded copy [25]; Build-entry
  wording + `/engine-help` rendering/order/catalog fields + orientation prose [26/19]; instantiator
  walkthrough/consent copy + the non-engineer fixture runbook [27].

## Cross-cutting flags (carried through every relevant slice)

- **`guardrail-ack` fires routinely during core construction (anti-habituation watch — plan-gate B1).**
  The live `engine-guard` is a coarse path-based classifier: any *modification/rename/removal* of a file
  under `.engine/check/`, `.engine/tools/`, `.github/workflows/`, or of `.engine/uv.lock` /
  `.engine/pyproject.toml` / `.github/CODEOWNERS` trips it (pure *additions* do not). So it correctly
  fires on the genuinely-weighty ⚖️ slices that modify the checker-of-checkers (4, 5, 15), **and** it fires
  on routine dependency bumps (any slice that re-resolves `uv.lock`). Slice 5 re-homing the guard
  **modifies the guard itself**, so its first clearing is hand-`guardrail-ack`'d (a known recursion).
  Decide explicitly (build-spec leaf): accept routine acks on construction PRs, or refine the classifier at
  slice 5 to distinguish add/strengthen from weaken — so the signal does not become habituated noise.
- **CI `uv sync` group selection is hand-maintained from the first dep-carrying slice until slice 25.** The
  live `engine-ci`/`engine-guard` run `uv sync --frozen` with no `--group`; a module's PEP 735
  `[dependency-groups]` entry (named by module id) is not installed without `--group`/`--all-groups`. The
  deriver (module manager) is slice 25, so each dep-carrying slice before then updates the workflow's
  `uv sync` invocation and re-resolves `uv.lock` (which trips the guard, above). Confirm at slice 4 whether
  a third-party library is actually needed.
- **Committed `.claude/settings.json` is born at the first hook-wiring slice (20).** The seed has only a
  local/gitignored `.claude/settings.local.json`. Slice 7's `hook`/`permission` appliers therefore can be
  *built* before 20 but are *behaviorally demoed* at/after 20; slice 7's standalone demo uses the
  `gitignore` directive (the file exists).
- **Frozen names** `engine-ci` / `engine-guard` / `guardrail-ack` must survive the validator/guard
  re-homing [5] and the module manager's CI binding [25] — a rename is itself guardrail-weakening.
- **Weakening-guard trust property [5, litigation-adjacent]:** the guard's execution stays on
  `pull_request_target`, base-checkout-only, diff-only, never head — moving it into the dispatcher's
  `pull_request`/`engine-ci` context would silently regress D-051. Behavioral sub-demo: a PR cannot run its
  own edited guard.
- **Litigation watch — two `CLAUDE.md` bodies stay distinct** [19]: the deployed-floor `CLAUDE.md` ships
  alongside, never overwrites, the construction `CLAUDE.md` (stage-0 §6). Resolve the reading before
  building slice 19.
- **Litigation watch — the locked 8-section PR contract** [15]: never drop/rename `Review` or trim a
  section; raise the alarm rather than quietly reconcile.
- **CODEOWNERS is simply absent** in the construction repo; the machinery ships in [25]/[27] (consuming
  slice 7's comment-fenced-block helper) and renders only against *generated* repos. Its derivation is
  exercised on a fixture repo, not in-repo.
- **Handoffs, not supersessions:** the engine manifest (hand-seeded [6] → inherited by the module manager
  [25]) and the local-uv-install → auto-bootstrap transition; name them as handoffs, not ratchet rows.
- **Three distinct "capture" records — do not conflate:** telemetry's `PostToolUse` check-fire/ambient
  record [18, D-118]; memory's `Stop` turn-delta [memory-substrate, L2, relayed by close 22]; close's
  ephemeral per-turn findings-record [22].
- **Memory-substrate (L2) seams:** close's ambient capture [22] and the attention scent [23] relay into
  the memory substrate, a separate required module built **after** core. Sequence the scent so it does not
  land before that FTS5 index exists.
- **`validators-core` (L2) completes the seed-validator ratchet row:** core ships the dispatcher + five
  kinds + zero/seed rules [4/5]; the engine-self-validation **rule corpus** rides `validators-core`, after
  core (D-089).

## Where the M1 self-construction line falls

M1 = a session can **boot-orient + write-gate itself + run the validation suite + drive
build-orchestration** (stage-0 §8). Within core that is delivered by: the validation dispatcher [4/5],
the boot floor + pack [19/20], the modes write-gate [21], and build-orchestration [24], plus the module
manager / bootstrap mechanism [25]. **But full M1 also requires** `validators-core` (the corpus) and
`memory-substrate-sqlite-fts5` (the memory floor + the `search` implementation behind slice 11's
protocol) — both separate L2 modules **after** core — and the control-plane bootstrap *setting* (already
in the seed). So the M1 crossover lands **after core's Phase 7 and after those two L2 modules.** All 27
core slices are built by **Builder A** (genesis) before the crossover. *(Plan-gate confirmed: placement
correct; all four M1 capabilities map to real slices.)*

## How each slice is built and verified (the per-slice recipe)

Every slice is one PR against protected `main`, and runs the full harness (stage-0 §7):

1. **Plan gate (before building):** the slice's detailed plan is cold-context audited by ≥4 independent
   agents (adversarial, technical-feasibility, non-engineer-operator, architect) reading the slice plan +
   the governing locked docs fresh; blocking/serious findings resolved or rejected-with-rationale first.
   Litigation-adjacent slices (5, 15, 19) get the litigation alarm ground-truthed against the locked
   source at their plan gate.
2. **Build** the slice; keep the seed validator (then the core dispatcher) green; keep the CI `uv sync`
   group selection current (cross-cutting flag).
3. **Deliverable gate (before merge):** the per-PR `build-conformance` review (conformance reviewer +
   adversarial divergence-hunter, hunter defaults divergent under doubt); orchestrator ground-truths every
   concrete finding against source and re-adjudicates a high-confirm lens.
4. **Behavioral demonstration:** the slice's fail-then-pass **Demo** above, run and varied by the
   maintainer himself — the one evidence class that routes around AI judgment. (Slices 25/27 demo on a
   fixture/generated repo with a non-engineer runbook, not in-repo.)
5. **Evidence bundle + consent surface:** mechanical green + cold conformance attestation +
   tests-through-review + the behavioral demo + the honest Review record. ⚖️ slices carry a visibly
   weightier consent surface; any guardrail-weakening clears only via the `guardrail-ack` label; the
   no-behavioral-correlate residuals (12, 27) are named on the consent surface, not papered over.

## Plan-gate record (this roadmap)

Cold-context audited by four independent lenses (adversarial, technical-feasibility,
non-engineer-operator, architect), no shared session context. **The spine held under all four** —
7-phase ordering, carve-outs, M1-line placement, and the topology-satisfied conclusion each independently
confirmed sound; no finding overturned the decomposition. Findings ground-truthed against the locked
source and adjudicated:

**Blocking (2) — accepted, folded in:**
- *Guard fires routinely during core construction* → new cross-cutting flag + ⚖️/leaf decision at slice 5
  (above).
- *boot↔provisioning bootstrap edge* → boot [20] rescoped to surface+nag-only (relays to the channel); the
  one-click-fix demo moved to slice 25 where the operation lives.

**Serious — accepted, folded in:** D-089 "core ships zero rules / corpus at validators-core" (slices 4/5
wording corrected); the `.gitignore`/CODEOWNERS comment-fenced-block helper given a home in slice 7;
`.engine/contracts/` demoted to a flagged `location` leaf [13] and the sweep generalized to all prose
surfaces; slice 15 supersedes-label corrected to "no-proto native, not ratchet row" and firmed to ⚖️;
slice 11 MCP demo gains the operator-approval step + degrade-to-git-native, and the `search`
protocol-only-in-core / possible 11a-11b split clarified; slice 12 demo marked fixture-based and its
ranking-quality residual elevated to the consent surface, dep corrected to 11; slice 6 `solo` tier surfaced
as a §15 consent point and "coherence kind not a rule" corrected; slice 17 block-cap "8" demoted to
verify-at-slice; slice 21 write-gate demo reworded to the honest §6-nudge tier (some edit paths slip the
deny; merge wall is the guarantee); the three distinct capture records [18/22] disambiguated; slice 27
fixture-only inductive gap made the loudest consent line + a non-engineer runbook required; slice 1
amend-first/full-surface-set handling and contagion consent note added; CI `uv sync` group seam flagged.

**Rejected / no-change:** none outright rejected; the `0.0.0-dev` token was already correctly framed as
illustrative-confirm-at-slice (no change needed).

**Routed to slice plan gates (litigation-adjacent, verify at the slice against locked source):** the
weakening-guard `pull_request_target` trust property [5]; the two-`CLAUDE.md` distinction [19]; the
8-section PR-contract integrity [15]; the exact platform behavior of `PreToolUse` deny on edit paths [21]
and the Stop block-cap [17].

## Retirement (the "do not let it linger" clause)

This roadmap is a **Builder-A genesis scaffold**, not a deployed artifact. Its job ends when `core` is
built and Builder B takes over. Its **final task at M1 is its own deletion** — the same crossover where
the construction `CLAUDE.md` is superseded by the `core` grammar + boot floor. Concretely:

- The PR that lands the last `core` slice (or the first Builder-B/in-repo PR after M1) **deletes
  `CORE-BUILD-ROADMAP.md`** and removes its pointer from the construction `CLAUDE.md` resume order.
- This obligation is recorded in the construction `CLAUDE.md` **Supersession ratchet → M1** so it cannot
  be silently dropped.
- It **must not ship in v1** and must never travel to a generated repo. (It cannot, in practice: the
  template is not generated-from until v1, and this file is deleted at M1, well before then.)
- Until retired, it is freely revisable — superseded slices may be struck through or annotated as each
  lands, so the file always shows where the build stands.

## Next action

Build **slice 1 — the ontology meta-contract + surface catalog** — in its own session: plan it in
detail, run the slice plan gate, build, run the deliverable gate, assemble the evidence bundle and the
operator-runnable demo.
