# engine-template — construction governance (read first, every build session)

This file governs the **construction of the engine-template repository** during the Builder-A genesis
stretch — before the engine's own machinery (boot, validation, build-orchestration) comes online to govern
building. It is the maintainer-construction-governance file, distinct from the thin deployed-floor
`CLAUDE.md` that the `core`/boot system will define for a generated repo. The two bodies stay separate.

## What this repo is

`engine-template` is a GitHub repository template that, via "Use this template," stands up an AI-driven
Engine — the externalized state, memory, knowledge, attention, guardrails, and control plane a non-engineer
needs to direct cold-booting AI sessions on any project. The repo is being built **PR by PR from a complete,
locked design** toward **M1**, the point at which the nascent engine (Builder B) takes over building itself
and this construction-governance file is superseded by the `core` grammar + boot floor.

## Source authority — the design is canonical and reference-only

The complete design lives in the **sibling workspace `../engine-planning/`** (canonical, build-ready as of
its decision-log D-159). It is the single source of truth. This build **reads** it and **never edits** it.
Start from `../engine-planning/CLAUDE.md`, then `principles.md`, `constraints.md`, `goals-and-quality.md`,
the relevant `systems/**` and `modules/**` docs, and the build plan in `wbs/` (`stage-0-harness.md`,
`module-order.md`, `build-conformance.md`).

**Never invent structure.** Where the design defers a concrete value (a "build-spec leaf"), decide it
**explicitly with the maintainer and record it**, never silently. Where the design genuinely lacks needed
grammar or contradicts itself, **stop and raise it** — do not paper over it. A change that would edit a
`locked` design doc stops for the litigation alarm in `../engine-planning/CLAUDE.md`.

## The trust model — informed consent on evidence, never code review

The maintainer (Shane) is a **non-engineer and the sole gate-holder from the first commit, with no outside
engineer.** He directs the build and approves every merge but **cannot read code.** So no construction step
may rest on code-reading or an engineer's review. The merge gate is **informed consent over an evidence
bundle** (`principles.md` §17): mechanical green (deterministic), independent cold-context cross-checks
(worth = independence + adversarial pressure), **behavioral demonstration the maintainer runs and varies
himself** (the one class that routes around AI judgment), and an honest self-report that names its own tier.
Confidence is bounded by how much of a change has a non-AI correlate — that bound is named, never dressed up.

## The two cold-context audit gates (HARD — every build session)

1. **Plan gate** — every session plan is cold-context audited **before** executing it (is it sound, and
   buildable from the spec without inventing?).
2. **Deliverable gate** — every built PR is cold-context audited **again before merge** (does what got built
   match the spec?), the per-PR `build-conformance` review.

Each gate launches **≥4 independent agents sharing no session context** — distinct lenses (adversarial,
technical-feasibility, non-engineer-operator, architect; the deliverable gate adds the conformance reviewer +
adversarial divergence-hunter, which defaults to divergent under doubt). Findings are tagged
**blocking / serious / nit**; every blocking and serious finding is **resolved or explicitly rejected with
logged rationale before proceeding.** Orchestrator disciplines (non-delegable): **ground-truth every concrete
finding against the source before recording**, and **re-adjudicate a high-confirm lens** (over-tagging is a
smell to recheck, never a verdict to relay). My adjudication raises confidence; it never confers it.

## Harness invariants (hold from the seed through v1)

- Every change is a **pull request against protected `main`**; **validator-green before merge** (the
  mechanical floor — the seed validator, then `validators-core`).
- **Plan-first, one step at a time**; each step finished and re-grounded from merged disk before the next.
- **A deliverable-gate cold review on every non-trivial PR**, plus an **operator-runnable behavioral
  demonstration** for any observable behavior — the per-PR catch for a semantic divergence, never reserved
  for "foundational" steps.
- **Tests are wired through the review** — a green test name is never trusted alone; the cold lens attests
  name↔assertion fidelity, and load-bearing tests get a behavioral demo.
- **Guardrail-weakening is always surfaced at the merge** and clears only via the distinct, deliberate
  acknowledgment (the `guardrail-ack` label).
- **Consequential PRs carry a visibly weightier consent surface** (the checker-of-checkers: the seed
  validator and the guards) so they are not rubber-stamped across many small green PRs.
- The merge-gate **reviewer is a non-engineer at every layer** — what grows toward M1 is the machinery that
  fills the evidence bundle, not the gate-holder's ability to read code.
- **Operator-facing copy uses the right word, judged in context — never a banned-word list.** Clarity over
  engineer-shorthand is a writing-and-review *judgment*, and keeping internal machinery out of operator
  narration is a *relevance* judgment (engine-planning §12) — neither is a mechanical word-substring filter
  (which would grade prose, against §7) and **no forbidden-word list is kept or created** (a list invites
  list-growth and teaches that word-banning is a writing function — D-225). Whether a render leans on jargon
  is judged by the `audit` prose probe and the per-PR build-conformance review, not a filter.

## The seed (stage 0) and its frozen names

The stage-0 seed is the irreducibly-ungated trust root (`../engine-planning/wbs/stage-0-harness.md` §2). It
ships: this `CLAUDE.md`; the uv tool-runtime (`.engine/pyproject.toml` + `.engine/uv.lock`, with
`.engine/.venv/` gitignored); the seed validator (`.engine/tools/validate.py`) as a thin dispatcher over
seven-field rule data (`.engine/check/`); the PR template (`.github/pull_request_template.md`, the eight
required sections); two CI workflows; and the protected-`main` ruleset (a setting the maintainer applies).

**Frozen names (a rename of any is a guardrail-weakening change):**
- `engine-ci` — required check #1: the validator (PR-body completeness + link/file integrity) plus the
  protection-detection guard.
- `engine-guard` — required check #2: the guardrail-weakening classifier (runs on `pull_request_target`,
  reads the diff only, never checks out head code).
- `guardrail-ack` — the label the maintainer applies to deliberately acknowledge a guardrail-weakening change.

## Supersession ratchet → M1

Each hand-built seed piece is **superseded** by the engine module that prefigures it: the seed validator →
`validators-core`; the PR-body completeness rule → the validation `presence` kind; this `CLAUDE.md` → the
`core` grammar + boot floor; ad-hoc write-discipline → the modes Explore write-gate. The protected-branch
human merge gate is the one rung that never retires. The `core`-decomposition scaffold lived in the
`engine-planning` workspace (`core-build-roadmap.md`) — **not** this repo, which must not carry its own
build rules — and covered `core`'s PR-slice sequence. It was **retired at core completion** (its final
slice, Slice CD, merged) and **archived in `engine-planning`, not deleted**, with its resume-order pointer
removed here; it never shipped in v1. The path
to **M1**: `repository-topology` → `core` →
`validators-core` + `memory-substrate-sqlite-fts5` + the control-plane bootstrap, after which Builder B
builds the rest of v1 in-repo under the same merge gate.

## Resume order for a build session

1. This file.
2. `../engine-planning/CLAUDE.md`, then `principles.md` + `constraints.md` + `goals-and-quality.md`.
3. `../engine-planning/wbs/stage-0-harness.md` and `module-order.md` for where the build is.
4. The `systems/**` / `modules/**` docs governing the current step, and `wbs/build-conformance.md` for the
   deliverable-gate protocol.
5. Plan the one next step, run the plan gate, build, run the deliverable gate, assemble the evidence bundle.
