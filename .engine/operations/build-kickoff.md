---
title: Build kickoff — claim the Build and bind the sealed plan
---
## Purpose
Open the draft PR, bind the sealed plan on the operator's decision, and record its approved depth before implementation.
Follow [Build orchestration](build-orchestration.md); resume an existing Build through [Build continuity](build-continuity.md).

## Steps

### 1. Open the claim from settled intent
Open one draft PR in a fresh isolated worktree from main. Keep it draft throughout construction; title it `Kind: what changed` using the kinds in `.github/pull_request_template.md`.
A Build is one PR-shaped change, possibly spanning sessions. Never create an Issue merely to track a Build or clean up another Build's binding at kickoff.

The Project Manager authors, reviews and seals the plan first: [Plan orchestration](plan-orchestration.md) owns that deliberation and its operator decisions.
Bind the exact sealed `build-plan.v2` payload. Its readable projection is a view, never a second authority to edit or translate back into JSON.
Follow [Build product grounding](build-product-grounding.md): retain milestone/readiness evidence, resolve settled descriptions, map selected canonical criteria and derive review steps.
Failed reads must not become no-spec. When no settled spec exists, disclose it; the plan's success obligations still govern conformance. Missing product description work returns through product intake.

### 2. Verify fresh entry and overlapping work
Fetch the verified default target in the isolated worktree before first bind. A branch created from freshly fetched main already satisfies ancestry.
For an unbound branch carrying work, rebase onto `origin/<target>`, resolve conflicts there and push its head to the draft PR. Never reset, stash or switch the operator checkout.
Bind fetches again; dirty/mid-operation checkouts, stale target ancestry, failed fetches and mismatched PR repository/ref/head/base refuse before accepted work.
Fix the named condition and retry. Active-branch catch-up and intentional history rewrites use the continuity procedure instead of this fresh-entry remedy.

Issues come from the sealed plan, authorizing Issue and PR's structured closing references. The shared preflight checks nonterminal local claims across worktrees, all open draft/ready PR pages, and recognized `claude/`/`codex/` issue branches.
The exact verified candidate PR/head is excluded even on first admission; competing work is not. No issue identity records overlap as not applicable. Missing results are incomplete, never an empty scan.
A collision or incomplete lookup names its evidence and digest. Only on the operator's explicit acceptance, add the following to the same bind:
```text
--overlap-override <reported-digest> --overlap-reason "<accepted reason>"
```
A changed material observation needs a new decision. An override cannot bypass freshness or another Build's ownership. Remote observers can race: this preflight is not a distributed issue lock.
Mechanic entry uses the same check via `mechanic_build.py worktree <name> --issue <number>` and the same override/reason flags, saving a private receipt before creation. Eventual bind still verifies admission.

### 3. Bind the sealed plan
```text
build_coordinator.py plan bind --plan <plan-id> --repository <owner/repo> --pr <number> --operator-decided
```
Use `--operator-decided` only after the operator's go. Bind records the gate and moment, never their words; for unattended work add `--issue <number>` to name authorization, not plan authority.
An unsealed or changed seal refuses with its remaining lifecycle steps. Never reconstruct approval from summaries, transcripts or implementation.
Keep `ownership.build_id` and `ownership.generation`; every mutation supplies `--expect-build-id`, `--expect-generation` and current `--expect-revision` before its verb.

Binding freezes admission material before activation. Matching interrupted retries retain original observation, identity and consent; changed head, target, issues or scoped override cannot replace the preparation or snapshot.
Restore matching inputs or explicitly retire that preparation through continuity. A matching active bind is continuation, not a new certificate or consent event. Legacy active entry stays honestly unverified; ambiguous legacy preparation refuses.
Full snapshots live at `builds/<build-id>/snapshot.json` in the local plan library. External `--state` files are private locators. Continuity owns adoption, migration and retirement.

The seal hands back before Build as described in plan orchestration. Bind records agreement to begin, not proof that the hand-back occurred or the plan is sound.
Plan lifecycle events stay local. Fresh admission needs live verification; the operator-typed start command's local stance step remains network-independent.
Native plan acceptance still imports an Explore draft and grants no Build authority. An Issue never replaces the local sealed plan.

### 4. Carry the approved depth into Build
Risk and depth were settled before sealing. Run the knowledge impact check through plan orchestration, which owns `.engine/templates/risk-assessment.md`, care recommendation and operator approval.
Offer only installed depths (only Quick without reviewers). No installed reviewer is a disclosed no-extra-review result, never a false green.
That one choice covers both design and deliverable gates. `approve --plan <payload.json> --depth <approved-depth>` records it against the bound payload; changing depth clears review coverage, and progress prose does not.
The seal already required the installed cold plan lenses. Build adds no extra plan-review gate or waiver; lifecycle decisions attest that the operator was asked, not that the choice was sound.

The trivial profile requires raw intent, objective, one success obligation, one reversible item and no-spec disclosure. Same-session quick depth, no cold lenses and one commit preserve its reduced ceremony; validation and human merge remain.
Guarded enforcement, guardrail weakening, another item/commit, settled referent or cold continuation require normal-profile revision and renewed approval.

## Done when
The draft PR claims this Build, the exact seal is bound on recorded authority, and approved depth is recorded against its payload.
Status reports implementation and names [Build implementation](build-implementation.md).
