# engine-template — construction governance (read first, every build session)

This file governs the **construction of the engine-template repository** — the work still being carried PR by PR,
from a complete and locked design, to v1. The engine's own machinery now runs that construction: the SessionStart
**boot** briefing, the **modes** Explore/Build write-gate, the **validation** suite, and the **build-orchestration**
runbook are all live and govern every build session. This file is the maintainer-construction layer over that
machinery — the durable trust model, the two cold-context audit gates, and the harness invariants the machinery
does not by itself enforce — and it **retires at v1**, when the build locus leaves this repo. It is the
maintainer-construction-governance file, distinct from the thin deployed-floor `CLAUDE.deployed.md` — the
`CLAUDE.md` a generated repo is meant to carry in its place. The two bodies stay separate and must not be
conflated.

## What this repo is

`engine-template` is a GitHub repository template that, via "Use this template," stands up an AI-driven Engine —
the externalized state, memory, knowledge, attention, guardrails, and control plane a non-engineer needs to direct
cold-booting AI sessions on any project. **M1 — the point at which the nascent engine took over building itself —
is crossed:** the engine's own boot, validation, modes write-gate, and build-orchestration machinery now govern
construction, and the in-repo engine builds the remaining v1 work under the same protected-`main` merge gate. What
remains is the rest of the v1 module set and the design-rationale transplant (moving the design's *why* into the
Engine before the build locus leaves); this file governs that work and is superseded piece by piece (see the
supersession section) as each hand-built rung gives way to the module that prefigures it.

## Source authority — the design is canonical and reference-only

The complete design lives in the **sibling workspace `../engine-planning/`** (canonical and build-ready). It is
the single source of truth. This build **reads** it and **never edits** it. Start from `../engine-planning/CLAUDE.md`,
then `principles.md`, `constraints.md`, `goals-and-quality.md`, and the relevant `systems/**` and `modules/**`
docs governing the current step; `wbs/build-conformance.md` carries the deliverable-gate protocol (active until v1).

**Never invent structure.** Where the design defers a concrete value (a "build-spec leaf"), decide it **explicitly
with the maintainer and record it**, never silently. Where the design genuinely lacks needed grammar or
contradicts itself, **stop and raise it** — do not paper over it. A change that would edit a `locked` design doc
stops for the litigation alarm in `../engine-planning/CLAUDE.md`.

## The trust model — informed consent on evidence, never code review

The maintainer (Shane) is a **non-engineer and the sole gate-holder, with no outside engineer.** He directs the
build and approves every merge but **cannot read code.** So no construction step may rest on code-reading or an
engineer's review. The merge gate is **informed consent over an evidence bundle** (`principles.md` §17): mechanical
green (deterministic), independent cold-context cross-checks (worth = independence + adversarial pressure),
**behavioral demonstration the maintainer runs and varies himself** (the one class that routes around AI
judgment), and an honest self-report that names its own tier. Confidence is bounded by how much of a change has a
non-AI correlate — that bound is named, never dressed up.

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

## Harness invariants (hold through v1)

- Every change is a **pull request against protected `main`**; **validator-green before merge** (the
  mechanical floor — the seed validator, now `validators-core`).
- **Plan-first, one step at a time**; each step finished and re-grounded from merged disk before the next.
- **A deliverable-gate cold review on every non-trivial PR**, plus an **operator-runnable behavioral
  demonstration** for any observable behavior — the per-PR catch for a semantic divergence, never reserved
  for "foundational" steps.
- **Tests are wired through the review** — a green test name is never trusted alone; the cold lens attests
  name↔assertion fidelity, and load-bearing tests get a behavioral demo.
- **Guardrail-weakening is always surfaced at the merge** and clears only via the distinct, deliberate
  acknowledgment (the `guardrail-ack` label).
- **Consequential PRs carry a visibly weightier consent surface** (the checker-of-checkers: the validator and
  the guards) so they are not rubber-stamped across many small green PRs.
- The merge-gate **reviewer is a non-engineer at every layer** — what grows is the machinery that fills the
  evidence bundle, not the gate-holder's ability to read code.
- **Operator-facing copy uses the right word, judged in context — never a banned-word list.** Clarity over
  engineer-shorthand is a writing-and-review *judgment*, and keeping internal machinery out of operator
  narration is a *relevance* judgment (engine-planning §12) — neither is a mechanical word-substring filter
  (which would grade prose, against §7) and **no forbidden-word list is kept or created** (a list invites
  list-growth and teaches that word-banning is a writing function — D-225). Whether a render leans on jargon
  is judged by the `audit` prose probe and the per-PR build-conformance review, not a filter.
- **A behavioral demo is a falsification that can fail, and it has a declared fate — it does not accumulate.**
  Every committed `demo_*.py` must exercise the real surface and be able to fail (a parallel reimplementation
  or a happy-path showcase is the alarm), and each must resolve to one of: covered by a permanent regression
  test, kept as construction evidence walled from travel (the first-run retirement set, so it does not ship
  into a generated repo), or **promoted by an explicit logged decision** to a standing operator capability —
  the only state in which a demo travels. The whole construction set retires with the build-conformance
  harness at v1. This is a reminder; the durable rule's canonical home is upstream — the engine-planning
  glossary "Behavioral attestation" (referenced by `wbs/build-conformance.md` §6/§10, which itself retires at
  v1 — D-228) — not duplicated here.

## The seed (stage 0) and its frozen names

The stage-0 seed was the irreducibly-ungated trust root (`../engine-planning/wbs/stage-0-harness.md` §2): this
`CLAUDE.md`; the uv tool-runtime (`.engine/pyproject.toml` + `.engine/uv.lock`, with `.engine/.venv/` gitignored);
the seed validator (`.engine/tools/validate.py`) as a thin dispatcher over rule data (`.engine/check/`); the PR
template (`.github/pull_request_template.md`, the eight required sections); two CI workflows; and the
protected-`main` ruleset (a setting the maintainer applies). Its hand-built pieces are now superseded by the engine
modules that grew from them (see the supersession section), but its **check names remain frozen and load-bearing.**

**Frozen names (a rename of any is a guardrail-weakening change):**
- `engine-ci` — required check #1: the validator (PR-body completeness + link/file integrity) plus the
  protection-detection guard.
- `engine-guard` — required check #2: the guardrail-weakening classifier (runs on `pull_request_target`,
  reads the diff only, never checks out head code).
- `guardrail-ack` — the label the maintainer applies to deliberately acknowledge a change the engine flags for
  review and holds the merge on: a guardrail-weakening change (`engine-guard`), or — once the optional
  product-design module is installed — a change to a settled product description (its lock-integrity
  re-acceptance check). The name and mechanism are unchanged; the set of flagged changes it clears is what grew.

## Supersession — each hand-built rung gives way to its module; the merge gate never does

Each hand-built seed piece is **superseded** by the engine module that prefigures it: the seed validator →
`validators-core`; the PR-body completeness rule → the validation `presence` kind; ad-hoc write-discipline → the
modes Explore write-gate; and **this `CLAUDE.md` → the `core` grammar + the boot floor.** That last supersession is
what **retires this file at v1**, when the build locus leaves the repo and the deployed floor (`CLAUDE.deployed.md`)
becomes the only `CLAUDE.md` a generated repo carries. **The protected-branch human merge gate is the one rung
that never retires** — every other rung is superseded by machinery; the gate-holder's merge is forever.

## Resume order for a build session

1. This file — the trust model, the two cold-context audit gates, and the harness invariants above.
2. `../engine-planning/CLAUDE.md`, then `principles.md` + `constraints.md` + `goals-and-quality.md` — the design
   canon, still the single source of truth.
3. `.engine/operations/operating-modes.md` for the session stance (Explore/Build write-gate), and
   `.engine/self-map.md` with `.engine/operations/knowledge-impact-check.md` for where the engine's parts are and
   what each one touches, depends on, checks, and governs.
4. The `systems/**` / `modules/**` design docs governing the current step, and `wbs/build-conformance.md` for the
   deliverable-gate protocol.
5. `.engine/operations/build-orchestration.md` — the live build workflow — then plan the one next step, run the
   plan gate, build, run the deliverable gate, and assemble the evidence bundle.
