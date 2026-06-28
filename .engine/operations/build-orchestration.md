---
title: Build orchestration — how Build work happens, from draft pull request to submitted pull request
---

## Purpose

How a Build session turns intent into a merged change. An orchestrating session opens a **draft pull
request** (the claim), plans the work as an ordered commit sequence, has the plan and then the result
reviewed by cold-context review passes at a depth the operator approves, integrates the work as the
**single writer of the final commits**, and **submits the pull request for the operator's merge** — the
only unbypassable gate. The draft pull request is the claim; the submitted pull request is the close; the
forward plan, when one is written, lives in a build Issue. Enter this runbook to understand or explain how
a build is planned, reviewed, assembled, and submitted, and why each gate exists.

## Steps

The workflow is a fixed shape an orchestrating session follows; the review passes that run at each gate are
**derived** from the installed review packs — none installed means a **disclosed no-op pass**, never a
silent green. The one mechanical hook is the pull-request **Review** section's presence-gate (the
completeness check over `.github/pull_request_template.md`); everything else is a deliberate-effort nudge
whose only wall is the protected-branch merge.

1. **Plan — open the claim and propose coverage.** Open a **draft pull request**, and keep it a draft for
   its whole working life. The checks run on a draft exactly as on a ready one (so there is never a reason to
   open it ready just to make CI run), and a draft **cannot be merged** — which is what keeps an in-progress
   change from being merged before its review gate finishes. Plan the change as an ordered commit sequence; when the work will be distributed across unattended sessions, record that
   sequence as the build Issue's checklist (the format below). Then **relay the risk assessment to the
   operator in chat** (it reaches them only as what the assistant types — no hook channel renders to their
   screen) — the plan-gate consent surface, filled from the `risk-assessment` template (`.engine/templates/risk-assessment.md`):
   the plain-language headline, what the change touches, **what will run** (the review passes this depth
   will run, and what is missing — never a time or a cost figure, which the engine has no method to know),
   the how-careful depth choice, and — only when the change weakens one of the engine's own guardrails — the
   plain-language warning naming which protection weakens and what the AI could then do unwatched. The
   operator iterates the plan to solid and approves the plan and the depth **before any work starts**. To
   see what a change touches — what depends on the parts it changes, and what checks or governs them — run
   the impact check in `.engine/operations/knowledge-impact-check.md` before settling the plan.
   *Always runs*, even with zero review packs.
2. **Plan-review — cold review before building.** The installed plan-review passes run cold-context at the
   approved depth, before any implementation; each finding takes one disposition per the finding-disposition
   policy (`.engine/policies/finding-disposition.md`) — fix in line, log a tracked Issue, or escalate. No
   packs installed means a disclosed no-op the operator is told about plainly, never a green "passed".
3. **Implement — one of three strategies, chosen at Plan.** *Orchestrator-inline* for tiny or
   tightly-coupled work; *parallel workers*, each in its own isolated worktree returning mechanical work
   product (not commits), when the work is loosely-coupled and decomposable and holding the whole result
   while generating it would lose grounding; *time-distributed routine* for large decomposable bulk work
   (the distributed shape in Notes). Delegation buys cohesion under context pressure, not speed.
4. **Integrate — the orchestrator is the single writer.** The orchestrator reviews each work product for
   correctness and fit, revises what does not cohere, and authors the final commit(s) with the whole result
   in view. A worker that failed leaves a missing planned commit the orchestrator re-dispatches or
   completes — there is no phantom-slot class, because the plan plus git state are the record.
5. **Pre-submission review — gated behind green validation.** Confirm the mechanical validation suite
   (`.engine/suites.json`) is green first — cold review is not spent on code that does not pass its checks;
   then the installed pre-submission passes run cold-context and findings are dispositioned. Validation
   reruns on every change including post-audit fixes; the cold review runs once at the agreed depth and does
   **not** rerun on those fixes unless the operator asks — the Review record states that delta.
6. **Submit — mark the draft ready and hand to the human gate.** Confirm validation is green and fill the
   pull-request contract including the **Review** section (below), then **mark the pull request ready**
   (`gh pr ready`) — the act that submits it. Author that contract by **reading
   `.github/pull_request_template.md` in full, never by grepping it for headers** — each section is a bold
   summary line, then bullets, then an italic `*Impact:*` line, none of which a header scan reveals. Mark it ready **only once** validation is green, the
   pre-submission review is clean (no unresolved `blocking` or `serious` finding), and every post-review fix
   is pushed; until then it stays a **draft**, which cannot be merged. Marking it ready is the operator-facing
   signal that the change is ready to merge. A build session is **done when the pull request is submitted**
   (marked ready); merge-and-walk leaves nothing dangling.

**Regenerating the engine's internal index files is part of integrate.** As the single writer, the
orchestrator reconciles the pull request's base against the default branch, then regenerates the engine's
internal index files — the knowledge graph and the self-map — last, from the reconciled tree, so the pull
request is current before review. A textual conflict on one of these files is **spurious** — both sides are
only regenerations of the same sources — so the resolution is to clear it and regenerate unconditionally,
never a side-pick and never a hand-merge. The load-bearing guarantee is **reconcile-before-merge**: the
eventual merge must already be clean, because the server-side merge button cannot run a local fix. A quiet
line in the Review record states how many internal index files were regenerated and that no work was lost —
the operator meets the disclosure, never the conflict. **One case is not yet self-healing:** a stranded
conflict is still possible — a sibling pull request can merge mid-flight — though only its *resolution* is
taken off the operator's hands. When that happens, the engine **surfaces the stranded pull request at the
next session's start and offers a one-step fix the assistant runs on the operator's say-so** — it reconciles
the pull request against the latest default branch, regenerates the two internal index files from the
reconciled tree, and is lossless-or-it-does-not-run; if anything but those two files clashed, it changes
nothing and routes the operator to a plain-language decision. Never the operator.

**The plan gate runs in two beats.** *Before the spend*, the risk assessment is the consent-and-coverage
surface (step 1). *After the audit*, the orchestrator synthesizes the findings into one recommended call
plus the trade, re-engaging the operator on a material finding and **always** on an unresolved
`blocking`-severity finding — it may never self-judge a blocking finding into a silent "logged and proceed".

**The Review record** states, in plain language a non-engineer reads at the merge: the depth that ran, the
review passes that ran (as plain checks, never their internal names), that each gate completed, the
findings' dispositions, and — when post-audit fixes were made — that they were validated but not
re-reviewed (so the reviewed version and the merged version differ). When no review packs are installed it
says so plainly — "no extra review ran" — never a green pass; and it carries the standing caveat that it is
the engine's own account and the operator's merge is the real gate. A trivial fast-path build fills it with
a truthful minimal line.

**The build-Issue body + checklist + scope-lock format**: the build Issue is engine-authored, so — like
every engine-authored Issue — its body realizes the control-plane engine-authored-issue body contract,
assembled through the shared issue-authoring helper (`.engine/tools/issue_author.py`), never a human web
issue template (those populate only the "New issue" form, which the engine's programmatic creation path
bypasses). The contract's parts are filled from the build: *what this is* (the build this Issue tracks and
why) and *what happens next* (the checklist + scope-lock below), with any backstage references rendered as
plain links. The ordered commit sequence is a machine-readable checklist ("N of M done"; the next unchecked
item is the next chunk); the permitted write-scope is recorded alongside it as the union of the planned
chunks' declared paths. Both live in the build Issue, authored at Plan, GitHub-native and cold-readable,
carrying the engine-domain label.

**Grouping product work into phases.** When a build realizes product work and the project carries a committed
build order (`docs/spec/build-plan.md`, the [product-design](../modules/product-design/manifest.json) module's
artifact), group the work under native GitHub phases at Plan — the Milestone *is* the plan. Run
`.engine/tools/milestone_emit.py emit`: it reads the build order and creates one phase per entry, never
duplicating one on a re-run, then assign each open work Issue to its phase (`gh issue edit <n> --milestone
<phase>`). The phase names are the build order's own, shown to the operator in plain language — never engine or
review vocabulary. **Absent a build order there is nothing to consume and the build plans its phase itself.** The
build order is a consumed input, authored by the module, never here.

## Done when

A draft pull request was opened as the claim; the plan and result were reviewed to the operator-approved
depth (or disclosed as un-reviewed where no pack is installed); the orchestrator authored the cohesive
final commits; validation is green; the pull-request contract, Review section included, is filled in plain
language; and the pull request — a draft until its gate came back clean — is marked ready (`gh pr ready`)
and so submitted for the operator's merge. The forward plan, where one was
written, is the build Issue, closing as its commits land.

## Notes

**The skeleton is posture, named at its honest tier.** Nothing mechanically forces a session to run the
review passes, run them at the approved depth, or halt on a finding before the merge — the same honest
limit the `operating-modes` write-gate and the `close-turn` disposition gate carry. The one mechanical hook
is the Review section's presence-gate; its *truthfulness*, like every contract section's, stays posture.
The only unbypassable wall is the protected-branch merge.

**Never fabricate an estimate.** The consent surface states what will run; it never asserts how long the
work will take or names a cost — the engine has no method to know either, and a made-up number is exactly
the false confidence the trust model exists to refuse.

**Routine is the same workflow, time-distributed.** For large, cleanly-decomposable bulk work, the implement
phase is spread across unattended sessions: an interactive Plan records the commit sequence and scope-lock
in the build Issue, unattended sessions add commits within that scope and report progress from git and the
checklist, and an interactive Finalize integrates, reviews for cohesion, validates, and submits. Its
cohesion guarantee is planned-up-front-plus-checked-at-Finalize, honestly weaker than interactive Build's
continuous assembly and acceptable only for decomposable work.

**Some pieces are owned elsewhere and are not authored here.** The **routine entry** — the `/engine-routine`
command and its routine-entry procedure (the scope-lock read at boot and per commit, the first-fire echo,
the misfire-as-Issue) — is the routine-mode package's, which preserves the unattended drift-firewall. The
**non-interactive permission posture** that makes an unattended run genuinely unable to ask is settled where
routine is actually exercised, not authored in this runbook. The **engine-authored-issue body contract**
the build Issue fills and the **issue-authoring helper** that assembles it are core/control-plane's (not
invented here); the step that ensures the engine-domain label exists is provisioning's; and the *human* web
issue templates are a separate control-plane artifact a person files through, never the path the engine's
own build Issue takes. This runbook fixes only the distributed-implement *workflow shape* and the
build-Issue checklist/scope-lock *format*.

**A recognized automation's pull request carries a disclosed not-applicable check — relay both
decisions plainly.** Walking the operator to merge a dependency-update pull request from a recognized
external automation (Dependabot), the `engine-ci` green includes a **disclosed not-applicable pass**
for the PR-body completeness check: it does not bind for the automation's own pull requests, so it was
**not verified** — green means *not applicable*, never *checked and passed*. Keep that distinct from
the separate decision the operator actively makes — applying the **`guardrail-ack`** label, which still
gates the locked-dependency change (flagged because changing pinned dependencies is exactly what a
person should consciously approve). Every other check still runs; the merge stays the operator's.
