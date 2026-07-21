---
title: Engine development — how a development session runs in the engine's own home repo
---

## Purpose

This repo is the engine's own home — the one place the Engine itself is developed. A deployed project treats
the Engine as fixed machinery that arrives as released updates (its root floor says so: "changing the Engine's
own machinery isn't this project's job"); here, that machinery **is** the work. Enter this runbook at the start
of any development session in this repo.

It rests on the **trust model — informed consent on evidence, never code review.** The maintainer is a
**non-engineer and the sole gate-holder, with no outside engineer;** he directs the work and approves every
merge but **cannot read code.** So no step may rest on code-reading or an engineer's review. The merge gate is
**informed consent over an evidence bundle**: mechanical green (deterministic), independent cold-context
cross-checks (worth = independence + adversarial pressure), **behavioral demonstration the maintainer runs and
varies himself** (the one class that routes around AI judgment), and an honest self-report that names its own
tier. Confidence is bounded by how much of a change has a non-AI correlate — that bound is named, never dressed
up. The full record is in the contracts: **eADR-0013** (consent on evidence, never code review) states the
gate's nature; **eADR-0005** places the one unbypassable gate at the protected-branch merge.

Boot surfaces this runbook when the checkout is the engine's own home — git origin equals the recorded
`home_repository` (`repo_identity.is_home_repo`). It is retired from a generated copy at first-run, so a
deployed project never carries it. The codes of conduct load every session through the root floor.

## Steps

1. **Ground in this runbook** — the trust model above, the **development invariants**, and the **frozen check
   names** (both in Notes). These are the durable disciplines every step below assumes.
2. **Set the session stance** — read `.engine/operations/operating-modes.md` for the Explore/Build write-gate,
   and `.engine/self-map.md` with `.engine/operations/knowledge-impact-check.md` for where the engine's parts
   are and what each one touches, depends on, checks, and governs.
3. **Read the governing design record** — the eADRs under `.engine/contracts/` for the current work. That
   in-repo decision set plus the operations runbooks are the single source of truth. **Never invent structure:**
   where a concrete value is not yet fixed, decide it explicitly with the maintainer and record it — never
   silently; where a needed rule or grammar genuinely doesn't exist, or the record contradicts itself, stop and
   raise it with the maintainer and record the decision as an eADR. The Codex adapter surfaces (`AGENTS.md`,
   `.agents/`, `.codex/`, the provider seam) are governed by eADR-0034; cold reviewers judge Codex work there.
4. **Plan the one next step and run the PLAN GATE** — enter `.engine/operations/build-orchestration.md`, plan
   the change, then cold-context audit that plan **before** executing it: is it sound, and buildable from the
   record without inventing? Launch **≥4 independent agents sharing no session context** (adversarial,
   technical-feasibility, non-engineer-operator, architect).
5. **Build the step to its full capability** — one step at a time, each finished and re-grounded from merged
   disk before the next. A partial or deferred build is a divergence, not a smaller change.
6. **Run the DELIVERABLE GATE** — cold-context audit the built PR **again before merge**: does what got built
   match what was asked? ≥4 independent lenses plus the conformance reviewer and the adversarial
   divergence-hunter (which defaults to divergent under doubt). Tag findings **blocking / serious / nit**;
   resolve or explicitly reject every blocking and serious one with logged rationale before proceeding.
   Orchestrator disciplines (non-delegable): **ground-truth every concrete finding against the source before
   recording**, and **re-adjudicate a high-confirm lens** — adjudication raises confidence, never confers it.
7. **Assemble the evidence bundle and submit** the PR for the maintainer's reviewed merge.

## Done when

The change reached `main` **only through the maintainer's reviewed merge** — the one unbypassable gate — after
passing the plan gate and the deliverable gate, validator-green, with any guardrail-weakening surfaced at the
merge and cleared solely by the deliberate `guardrail-ack`. It leaves a merged pull request and its logged
decisions behind; nothing is left dangling.

## Notes

**Development invariants.**

- **Full capability every PR.** Each PR drives the slice it touches to its full agreed capability; a deferral is
  an explicitly recorded decision (a tracked issue or a logged carve-out), never a quiet stub — measured by the
  capability delivered, not by effort or count.
- Every change is a **pull request against protected `main`**; **validator-green before merge** (`validators-core`).
- **Plan-first, one step at a time**; each step re-grounded from merged disk before the next.
- **A deliverable-gate cold review on every non-trivial PR**, plus an **operator-runnable behavioral demo** for
  any observable behavior — the per-PR catch for a semantic divergence, never reserved for "foundational" steps.
- **Tests are wired through the review** — a green test name is never trusted alone; the cold lens attests
  name↔assertion fidelity, and load-bearing tests get a behavioral demo.
- **Guardrail-weakening is always surfaced at the merge** and clears only via the deliberate `guardrail-ack`.
- **Consequential PRs carry a visibly weightier consent surface** so they are not rubber-stamped across many
  small green PRs.
- The merge-gate **reviewer is a non-engineer at every layer** — what grows is the machinery that fills the
  evidence bundle, not the gate-holder's ability to read code.
- **Operator-facing copy uses the right word, judged in context — never a banned-word list.** No forbidden-word
  list is kept or created; whether a render leans on jargon is judged by the `audit` prose probe and the per-PR
  review, not a substring filter.
- **A behavioral demo is a falsification that can fail, and it has a declared fate — it does not accumulate.**
  Every committed `demo_*.py` must exercise the real surface and be able to fail, and each resolves to one of:
  covered by a permanent regression test, kept as construction evidence walled from travel (the first-run
  retirement set), or promoted by an explicit logged decision to a standing operator capability — the only
  state in which a demo travels.

**Frozen check names** — a rename of any is a guardrail-weakening change.

- `engine-ci` — the validator (PR-body completeness + link/file integrity) plus the protection-detection guard.
- `engine-guard` — the guardrail-weakening classifier (runs on `pull_request_target`, reads the diff only,
  never checks out head code).
- `guardrail-ack` — the label the maintainer applies to deliberately acknowledge a change the engine flags and
  holds the merge on: a guardrail-weakening change (`engine-guard`), or — once the optional product-design
  module is installed — a change to a settled product description.
