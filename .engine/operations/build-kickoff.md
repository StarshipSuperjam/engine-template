---
title: Build kickoff — claim the Build and bind the sealed plan
---
## Purpose

Read in `planning`: open the draft PR, bind the sealed plan with the operator's recorded decision,
and record its approved depth. This phase ends before implementation changes the tree.
The surrounding flow is [Build orchestration](build-orchestration.md).

## Steps

### 1. Plan and open the claim

Open one draft pull request for the Build and keep it draft throughout construction. Title it `Kind: what
changed`, using the kinds in `.github/pull_request_template.md`. A Build is one PR-shaped change; it need not
be one session. An Issue is never created merely because a Build exists — not even to track the work; an Issue is
intake, and a Build's work is carried by its draft PR. This kickoff opens a NEW Build in a fresh worktree cut
from main, consulting no prior binding and cleaning up none; a Build that must RESUME instead keeps its worktree and recovers and re-verifies its plan from the local plan library (see [Build continuity](build-continuity.md)).

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

### Fresh entry and overlapping work

Before the first bind, fetch the verified default target in the isolated worktree and bring the branch
up to date. For an unbound branch with work already on it, use `git rebase origin/<target>` and push its
current head to the draft PR. Resolve conflicts there; never reset, stash or switch the operator checkout.
A branch created from freshly fetched main already satisfies ancestry. `plan bind` fetches again and refuses
if the target moved, the checkout is dirty or mid-operation, or the PR repository/ref/head/base does not match.
A failed freshness check creates no accepted Build. Retry after resolving the named condition.

The shared preflight derives issues from the sealed plan, the authorizing Issue and the draft PR's structured
closing references. It checks nonterminal local claims across worktrees, every page of open draft/ready PRs,
and recognized `claude/` and `codex/` issue branches. The exact verified candidate PR/head is excluded even
on first admission; competing work is not. Missing remote results are an incomplete observation, never an
empty one. A direct Build with no issue identity records overlap as not applicable.

A collision or incomplete lookup prints its evidence and an observation digest. Continue only on the
operator's explicit acceptance of that observation, adding
`--overlap-override <reported-digest> --overlap-reason "<accepted reason>"` to the same bind command.
If the material observation changes, obtain a new decision for the new observation. An override cannot
bypass freshness or another Build's ownership. Remote observers can still race: this is a bounded preflight,
not a distributed issue lock. Mechanic entry uses the same check with
`mechanic_build.py worktree <name> --issue <number>` and the same override/reason flags, recording its private
receipt before worktree creation; the eventual bind still performs fresh admission.

Bind the plan once:

```text
build_coordinator.py plan bind --plan <plan-id> \
  --repository <owner/repo> --pr <number>
```

`--plan` names a SEALED plan in the local library, and nothing else enters a Build: an unsealed plan is
refused at the door with its remaining lifecycle steps named, as is one whose content moved after its seal.
Add `--operator-decided` only after the operator's go; the bind refuses without it and records gate and moment,
never words. For unattended work add `--issue <number>` — that Issue AUTHORIZES the work; it is never its plan.

Keep bind's `ownership.build_id` and `ownership.generation`; every mutation supplies them as
`--expect-build-id` and `--expect-generation`, plus current `--expect-revision`, before the verb.
Binding freezes the verified admission material before recording ownership and activation; matching retries
preserve the original observation, identity and consent. Interrupted preparation must still match its frozen
head, target tip, issue set and scoped override. Changed inputs refuse without replacing the preparing claim
or snapshot; restore matching inputs or explicitly retire that preparation using the continuity procedure.
An already-active matching bind is continuation, with no new admission certificate or consent event. A legacy
active Build stays honestly unverified at entry; an ambiguous legacy preparation cannot silently activate. Full snapshots
live at `builds/<build-id>/snapshot.json`; external `--state` files are private locators. For interrupted
preparations, verified continuation or explicit legacy migration, follow [Build continuity](build-continuity.md).

**The seal hands back before Build**: an offer, not a gate; [Plan orchestration](plan-orchestration.md) describes it.
Binding records the operator's agreement to begin; it does not mechanically verify that hand-back or
record what the session runs on.

### Where the plan lives

The plan lives in the local library, never GitHub; see [Plan orchestration](plan-orchestration.md).
Never reconstruct approval from summaries, transcript fragments or implementation. An Issue authorizes
work, not plan authority. Plan lifecycle events stay local. Fresh Build admission needs live repository/PR
verification; the operator-typed start command's local stance change remains network-independent. Native plan
acceptance still imports an Explore draft and does not start or authorize a Build.

### 2. Assess risk and approve the Build gate

**Risk and depth are settled on the plan side, before the seal**, and
[Plan orchestration](plan-orchestration.md) runs that stop — the plan's context and open questions first, the
depth a separate, led step reached once the operator has closed every open question. Run the knowledge impact check, offer only the depths
worth offering for this repository's installed reviewers (only Quick when no reviewers, StarshipSuperjam/engine-template#763),
fill `.engine/templates/risk-assessment.md` — now carrying a one-line care recommendation — in plain language, and record the operator's approval.
No installed reviewer is a disclosed no-extra-review result, never a false green.

**That one choice covers both gates**: it names the lenses the seal will require, and it is the depth this
Build's deliverable review runs at, so consent is given once and given there. On the Build side,
`approve --plan <plan.json> --depth …` records the same depth against the
bound payload; changing approved depth clears review coverage, and progress prose does not.

The `trivial` profile is the one-entry fast path: its reduced plan needs raw intent, objective, one success obligation, one reversible work item, and no-spec disclosure—none of the normal profile's evidence, assumption, risk, scope, interpretation, or review-strategy fields. Same-session, quick depth, no cold lenses, and one commit keep one headline plus plan/depth approval as its only operator ceremony; validation and merge remain. A guarded-enforcement change, guardrail weakening, second item or commit, settled referent, or cold continuation requires revision to `normal` and renewed approval.

### 3. The plan review already happened

The plan-side seal requires one cold plan review covering every lens at the approved depth.
Build has no extra plan-review gate or waiver. Approve, seal and bind each require an operator decision
and verify the previous gate's record; it attests that the operator was asked, not that the choice was sound.

## Done when

The draft pull request is open and stays draft; the sealed plan is bound with the operator's recorded decision;
the approved depth is recorded against the bound payload; and `status` reports `implementation`, naming
[Build implementation](build-implementation.md) as the runbook to read next.
