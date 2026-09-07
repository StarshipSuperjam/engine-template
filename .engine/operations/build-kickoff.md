---
title: Build kickoff — claim the Build and bind the sealed plan
---
## Purpose

The first phase of a Build, read when the coordinator reports `planning`: open the draft pull request that is
the Build's claim, bind the sealed plan with the operator's recorded decision, and record the approved depth
against the bound payload. It ends with a bound, approved Build and nothing yet changed in the tree.
The surrounding flow is [Build orchestration](build-orchestration.md).

## Steps

### 1. Plan and open the claim

Open one draft pull request for the Build and keep it draft throughout construction. Title it `Kind: what
changed`, using the kinds in `.github/pull_request_template.md`. A Build is one PR-shaped change; it need not
be one session. An Issue is never created merely because a Build exists — not even to track the work; an
Issue is intake, and a Build's work is carried by its draft PR — the seal hand-back and the bind kickoff both
say so. A Build that must continue cold recovers its plan from the local plan library (see [Build continuity](build-continuity.md)).

The plan is not authored here. It is authored, reviewed and SEALED through the Project Manager first, and
[Plan orchestration](plan-orchestration.md) is that half: the deliberation, the operator's stops, and
everything the lifecycle demands on the way to a seal. A Build binds the sealed plan's `build-plan.v2`
payload and presents a readable projection generated from that exact document — a view, not a second
authority, never edited or translated back into JSON. Where no settled spec exists the plan discloses it, and
its success obligations still govern conformance review. The coordinator can prove only that the plan's
reasoning is present and that later work uses the same plan; it cannot prove that reasoning is sound.

Follow [Build product grounding](build-product-grounding.md): retain advisory milestone and readiness
evidence, resolve settled descriptions without degrading failed reads to no-spec, map every selected
canonical criterion, and derive its review steps. Build consumes settled intent; missing product description
work returns through product intake instead of being improvised here.

Bind the plan once:

```text
build_coordinator.py plan bind --plan <plan-id> \
  --repository <owner/repo> --pr <number>
```

`--plan` names a SEALED plan in the local library, and nothing else enters a Build: an unsealed plan is
refused at the door with its remaining lifecycle steps named, as is one whose content moved after its seal.
Add `--operator-decided` only after the operator's go; the bind refuses without it and records gate and moment,
never words. For unattended work add `--issue <number>` — that Issue AUTHORIZES the work; it is never its plan.

**The seal hands back before the Build starts**, and [Plan orchestration](plan-orchestration.md) carries what
that pause asks for. It is an offer, not a gate: the bind's `--operator-decided` consent is the operator's
agreement to begin; nothing mechanical checks the hand-back's steps, and the engine neither reads nor records
what the session runs on.

### Where the plan lives

In the local plan library, on this workstation, and never on GitHub — see
[Plan orchestration](plan-orchestration.md). Never reconstruct an approved plan from a summary, transcript
fragments, or implementation. An Issue may AUTHORIZE a Build, which is what `--issue` records, but
authorization and plan authority are two artifacts and neither stands in for the other; no lifecycle event is
a GitHub comment, and GitHub or network loss does not stop same-session local work.

### 2. Assess risk and approve the Build gate

**Risk and depth are settled on the plan side, before the seal**, and
[Plan orchestration](plan-orchestration.md) runs that stop: run the knowledge impact check, offer only the
depths worth offering for this repository's installed reviewers (only Quick when no reviewers,
StarshipSuperjam/engine-template#763), fill `.engine/templates/risk-assessment.md` in plain language, and
record the operator's approval at the gate. No installed reviewer is a disclosed no-extra-review result,
never a false green.

**That one choice covers both gates**: it names the lenses the seal will require, and it is the depth this
Build's deliverable review runs at, so consent is given once and given there. On the Build side,
`approve --plan <plan.json> --depth …` records the same depth against the
bound payload; changing approved depth clears review coverage, and progress prose does not.

The `trivial` profile is the one-entry fast path: its reduced plan needs raw intent, objective, one success obligation, one reversible work item, and no-spec disclosure—none of the normal profile's evidence, assumption, risk, scope, interpretation, or review-strategy fields. Same-session, quick depth, no cold lenses, and one commit keep one headline plus plan/depth approval as its only operator ceremony; validation and merge remain. A guarded-enforcement change, guardrail weakening, second item or commit, settled referent, or cold continuation requires revision to `normal` and renewed approval.

### 3. The plan review already happened

There is no plan review on this side: exactly one cold plan review runs per plan, on the plan side against the
approved revision before the seal, and the seal refuses while the recorded lenses do not cover the approved
depth's roster. A bound plan is a reviewed plan by construction, so this side has no plan-review gate and no
waiver for one. Approve, seal and bind each refuse without a recorded operator decision at that gate, and each
checks that the gate before it left its record — a record that the operator was asked, not proof.

## Done when

The draft pull request is open and stays draft; the sealed plan is bound with the operator's recorded decision;
the approved depth is recorded against the bound payload; and `status` reports `implementation`, naming
[Build implementation](build-implementation.md) as the runbook to read next.
