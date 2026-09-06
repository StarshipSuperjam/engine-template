---
title: Engine development — how a session that develops the Engine itself runs
---

## Purpose

The Engine is developed in exactly one place — its own home repository — but a session can reach that work from
more than one starting point. A deployed project treats the Engine as fixed machinery arriving as released updates
("changing the Engine's own machinery isn't this project's job", its root floor says); in the engine's home, that
machinery **is** the work. This runbook governs every session that develops the Engine.

It rests on the **trust model — informed consent on evidence, never code review.** The maintainer is a
**non-engineer and the sole gate-holder, with no outside engineer;** he directs the work and approves every
merge but **cannot read code**, so no step may rest on code-reading or an engineer's review. The merge gate is
**informed consent over an evidence bundle**: mechanical green (deterministic), independent cold-context
cross-checks (worth = independence + adversarial pressure), **behavioral demonstration the maintainer runs and
varies himself** (the one class that routes around AI judgment), and an honest self-report that names its own
tier. Confidence is bounded by how much of a change has a non-AI correlate — a bound named, never dressed up.
The protected-branch merge is the one unbypassable gate; the operations, checks, demonstrations, and review
receipts below are the live owners of the evidence presented there.

**Which sessions this governs.** The routing decision itself is authored in the `engine-develop-engine` route,
present in every deployment and read before a lane is chosen; this runbook holds the reasoning and the
discipline that follows. Where the two differ, the route decides the lane and this runbook the discipline. The
lanes turn on **which repository holds the files the change edits**:

- **Home lane.** The checkout is the engine's own home — git origin equals the recorded `home_repository`
  (`repo_identity.is_home_repo`, which fails toward home). Work here, under the steps below.
- **Mechanic lane.** The edited files live in the engine's home repository and this checkout is the mechanic that
  builds it. Enter `.engine/operations/owned-product-build.md` (it cuts the isolated worktree and governs
  isolation, delivery, and cleanup). Ground in this runbook at the worktree's **resolved base commit** — never its
  working tree or index — because in-flight edits must never govern the build that makes them; record that base
  commit with the evidence. All seven steps below govern, and step 4's Build is the owned-product Build already in
  progress: continue it, never cut a second worktree. Follow the steps below in order — the development discipline
  — taking each of `owned-product-build.md`'s delivery mechanics where it applies; run neither list twice.
- **Work whose files live in the mechanic's own repository** (its spec corpus, its runbooks) is an ordinary
  Build there, not Engine development. An ask spanning both repositories is two sequenced Builds, one per
  repository; never one worktree, and never a refusal.
- **Anything else refuses.** A deployed project consumes the Engine as released updates and is not authorized
  to develop it; when the lane cannot be confidently established, refuse.

Two things this arrangement does not give you: nothing at session start names this runbook in the mechanic lane
(the owned-product build step reaches it), and the trust model above describes the engine's own maintainer, not
another operator's clone that arms a build target. Boot surfaces this runbook only in the engine's own home (a
deployed project retires it at first-run); a worktree cut from the home repository is home source, hence the
mechanic lane's base-copy grounding.

## Steps

1. Confirm your lane (see Purpose); in the mechanic lane every step below runs inside the emitted product
   worktree. Then **Ground in this runbook** — the trust model above, and the **development invariants** and
   **frozen check names** in Notes: the durable disciplines every step below assumes.
2. **Set the session stance** — read `.engine/operations/operating-modes.md` for the Explore/Build write-gate
   (the local gate is backstopped by the protected-branch merge wall, never dressed as the wall itself), and
   `.engine/self-map.md` with `.engine/operations/knowledge-impact-check.md` for where the engine's parts are
   and what each one touches, depends on, checks, and governs.
3. **Read the live owners for the current work** — code, schemas, checks, policies, operations, tests, and the
   wiring map that connects them. **Never invent structure:** where a value is not yet fixed, decide it with the
   maintainer and record it in the pull request and the natural owner; where a needed rule doesn't exist, or two
   owners contradict, stop and raise it. Cold reviewers judge the Codex adapter surfaces (`AGENTS.md`, `.agents/`,
   `.codex/`, the provider seam) directly against their generators, policies, schemas, and checks.
4. **Plan the one next step and run the PLAN GATE** — enter `.engine/operations/build-orchestration.md`, plan the
   change, and use the coordinator's installed roster at the operator-approved depth; cold reviewers share no
   session context and challenge whether the plan is sound and buildable without invention, and Build's
   approved-depth protocol owns coverage, so no second reviewer-count rule lives here.
5. **Build the step to its full capability** — one step at a time, each finished and re-grounded from merged
   disk before the next. A partial or deferred build is a divergence, not a smaller change.
6. **Run the DELIVERABLE GATE** — use Build's installed roster at the approved depth to cold-context audit the
   built PR before merge against what was asked, including plan-derived conformance and the adversarial divergence
   sweep that roster requires. Tag findings **blocking / serious / nit**; resolve or explicitly reject every
   blocking and serious one with logged rationale before proceeding. Non-delegable orchestrator disciplines:
   **ground-truth every concrete finding against the source before recording**, and **re-adjudicate a high-confirm
   lens** — adjudication raises confidence, never confers it.
7. **Assemble the evidence bundle and submit** the PR for the maintainer's reviewed merge.

## Done when

The change reached `main` **only through the maintainer's reviewed merge** — the one unbypassable gate — after
passing the plan gate and the deliverable gate, validator-green, with any enforcement-file change disclosed in
plain language at the merge and a killswitch-tier weakening cleared solely by the deliberate `guardrail-ack`; it
leaves a merged pull request and its logged decisions behind, nothing dangling.

## Notes

**Development invariants.**

- **Full capability every PR.** Each PR drives the slice it touches to its full agreed capability; a deferral is
  an explicitly recorded decision (a tracked issue or a logged carve-out), never a quiet stub — measured by the
  capability delivered, not by effort or count.
- **A deferral is written where the work is.** Work owed to the code is recorded at the site with the engine's
  marker (`engine_todo.py` owns the grammar and lists every outstanding one); a decision *not* to build is a
  carve-out in the pull-request body.
- **A deliverable-gate cold review on every non-trivial PR**, plus an **operator-runnable behavioral demo** for
  any observable behavior — the per-PR catch for a semantic divergence, never reserved for "foundational" steps.
- **Tests are wired through the review** — a green test name is never trusted alone; the cold lens attests
  name↔assertion fidelity, and load-bearing tests get a behavioral demo.
- **One home for a moment in time.** Every read, format, or parse of a moment goes through
  `.engine/tools/moment.py`, whose docstring carries the two binding laws; no tool hand-rolls the trailing-Z shape
  or a local-clock calendar day.
- **Guardrail-weakening is always surfaced at the merge** — a plain disclosure for ordinary enforcement-file
  edits, and a hard block clearing only via the deliberate `guardrail-ack` at the killswitch tier.
- **Consequential PRs carry a visibly weightier consent surface** so they are not rubber-stamped across many small
  green PRs.
- The merge-gate **reviewer is a non-engineer at every layer** — what grows is the machinery that fills the
  evidence bundle, not the gate-holder's ability to read code.
- **Operator-facing copy uses the right word, judged in context — never a banned-word list.** No forbidden-word
  list is kept or created; jargon is judged by the `audit` prose probe and the per-PR review, not a filter.
- **A behavioral demo is a falsification that can fail, and it has a declared fate — it does not accumulate.**
  Every committed `demo_*.py` must exercise the real surface and be able to fail, and each resolves to one of:
  covered by a permanent regression test, kept as construction evidence walled from travel (the first-run
  retirement set), or promoted by an explicit logged decision to a standing operator capability (the only state
  in which a demo travels).

**Frozen check names** — a rename of any is a guardrail-weakening change.

- `engine-ci` — the validator (PR-body completeness + link/file integrity) plus the protection-detection guard.
- `engine-guard` — the guardrail-weakening classifier (runs on `pull_request_target`, reads the diff only,
  never checks out head code).
- `guardrail-ack` — the label the maintainer applies to deliberately acknowledge a change the engine flags and
  holds the merge on: a killswitch-tier guardrail weakening (`engine-guard`), or — once the optional
  product-design module is installed — a change to a settled product description. It downgrades the finding
  to a record; it never erases it.
