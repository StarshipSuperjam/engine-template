---
title: Build orchestration — how Build work happens, from draft pull request to submitted pull request
---

## Purpose

How a Build session turns intent into a merged change. An orchestrating session opens a **draft pull
request** (the claim), plans the work as an ordered commit sequence, has the plan and then the result
reviewed by cold-context passes at a depth the operator approves, integrates as the **single writer of the
final commits**, and **submits the pull request for the operator's merge** — the only unbypassable gate.
The submitted pull request is the close; the forward plan, when written, lives in a build Issue. Enter
this runbook to understand or explain how a build is planned, reviewed, assembled, and submitted, and why
each gate exists.

## Steps

The review passes that run at each gate are **derived** from the installed review packs — none installed
means a **disclosed no-op pass**, never a silent green. The one mechanical hook is the pull-request
**Review** section's presence-gate (the completeness check over `.github/pull_request_template.md`);
everything else is a deliberate-effort nudge whose only wall is the protected-branch merge.

1. **Plan — open the claim and propose coverage.** Open a **draft pull request** and keep it a draft for
   its whole working life: the checks run on a draft exactly as on a ready one (so never open it ready just
   to make CI run), and a draft **cannot be merged**, which stops an in-progress change from merging before
   its review gate finishes. Plan the change as an ordered commit sequence; when the work is distributed
   across unattended sessions, record that sequence as the build Issue's checklist (format in Notes). Run
   the impact check (`.engine/operations/knowledge-impact-check.md`) — what depends on the parts you change,
   and what checks or governs them — before settling the plan.
2. **Relay the risk assessment — the plan-gate consent surface.** Relay it to the operator **in chat**
   (it reaches them only as assistant text — no hook renders to their screen), filled from the
   `risk-assessment` template (`.engine/templates/risk-assessment.md`): the plain-language headline, what
   the change touches, **what will run** (the passes this depth runs, and what is missing — never a time or
   cost figure, which the engine cannot know; a made-up number is the false confidence the trust model
   refuses), the how-careful depth choice, and — only when the change weakens an engine guardrail — the
   plain-language warning naming which protection weakens and what the AI could then do unwatched. The
   operator iterates the plan to solid and approves the plan and the depth **before any work starts**. This
   plan gate (steps 1–2) *always runs*, even with zero review packs.
3. **Plan-review — cold review before building.** The installed plan-review passes run cold-context at the
   approved depth, before any implementation; each finding takes one disposition per the finding-disposition
   policy (`.engine/policies/finding-disposition.md`) — fix in line, log a tracked Issue, or escalate. After
   the audit the orchestrator synthesizes the findings into one recommended call plus the trade, re-engaging
   the operator on a material finding and **always** on an unresolved `blocking`-severity finding — never
   self-judging a blocking finding into a silent "logged and proceed". No packs installed means a disclosed
   no-op told plainly, never a green "passed".
4. **Implement — one of three strategies, chosen at Plan.** *Orchestrator-inline* for tiny or
   tightly-coupled work; *parallel workers*, each in its own isolated worktree returning mechanical work
   product (not commits), when the work is loosely-coupled and decomposable and holding the whole result
   while generating it would lose grounding; *time-distributed routine* for large decomposable bulk work
   (Notes). Delegation buys cohesion under context pressure, not speed.
5. **Integrate — the orchestrator is the single writer.** Review each work product for correctness and fit,
   revise what does not cohere, and author the final commit(s) with the whole result in view. A failed
   worker leaves a missing planned commit the orchestrator re-dispatches or completes — no phantom-slot
   class, because the plan plus git state are the record. Then **reconcile the pull request's base against
   the default branch and regenerate the engine's internal index files — the knowledge graph and the
   self-map — last, from the reconciled tree**. A textual conflict on those is **spurious** (both sides
   regenerate the same sources): clear it and regenerate unconditionally, never a side-pick or hand-merge.
   The load-bearing guarantee is **reconcile-before-merge** — the eventual merge must already be clean,
   because the server-side merge button cannot run a local fix. A quiet Review-record line states how many
   index files were regenerated and that no work was lost — the operator meets the disclosure, never the
   conflict.
6. **Pre-submission review — gated behind green validation.** Confirm the validation suite
   (`.engine/suites.json`) is green first — run `uv run --directory .engine --frozen -- python
   tools/validate.py --suite CI` and the self-tests `uv run --directory .engine --frozen -- python -m unittest
   discover -s tools -p 'test_*.py' -b` (the same commands CI runs) — cold review is not spent on code that
   fails its checks. The `--frozen` keeps a test run from quietly rewriting the locked `uv.lock`, and the `-b`
   keeps the `Ran N … OK` summary visible: it buffers each test's stdout so the walkthrough output the
   `test_*.py` self-tests emit while exercising their demos does not bury the tail. Then
   the installed pre-submission passes run cold-context and findings are dispositioned. Validation reruns on
   every change including post-audit fixes; the cold review runs once at the agreed depth and does **not**
   rerun on those fixes unless the operator asks — the Review record states that delta.
   **Re-derive every not-applicable carve-out the negative-fixture meta-check lists.** When that meta-check
   (`engine/check/hard-check-bite`) reports a hard check as *not applicable* (its loud soft note — a check
   exempted from a negative fixture), the gate does not take the disclosure's word: for each one it re-derives
   the bound — confirming the check's *aimed* failure cannot be triggered by any committed input in CI (so the
   only seedable path is the fail-closed one, which would be a false witness), not merely that the disclosure
   carries the right property string. Anything that no longer holds becomes a finding; the per-carve-out
   re-derivation is recorded in the Review section. This is the standing control behind the meta-check's printed
   "re-derived at the review gate"; the meta-check checks the disclosure, the gate checks the world.
7. **Submit — mark the draft ready and hand to the human gate.** Fill the pull-request contract including
   the **Review** section by **reading `.github/pull_request_template.md` in full, never grepping it for
   headers** — each section is a bold summary line, then bullets, then an italic `*Impact:*` line, none of
   which a header scan reveals. **Run the close-linkage pre-flight** (`close_linkage_preflight.py check`) and
   fold its lines into Review, applying any disclosed defang it emits (see Notes). **Mark the pull request
   ready** (`gh pr ready`) — the act that submits it —
   **only once** validation is green, the pre-submission review is clean (no unresolved `blocking` or
   `serious` finding), and every post-review fix is pushed; until then it stays a **draft**, which cannot be
   merged. A build session is **done when the pull request is submitted**; merge-and-walk leaves nothing
   dangling.

## Done when

A draft pull request was opened as the claim; the plan and result were reviewed to the approved depth (or
disclosed un-reviewed where no pack is installed); the orchestrator authored the cohesive final commits;
validation is green; the pull-request contract, Review section included, is filled in plain language; and
the pull request is marked ready (`gh pr ready`) and so submitted for the operator's merge. The build
Issue, where one was written, closes as its commits land.

## Notes

**The skeleton is posture, named at its honest tier.** Nothing mechanically forces a session to run the
review passes, run them at the approved depth, or halt on a finding before merge — the same honest limit
the `operating-modes` write-gate and the `close-turn` disposition gate carry. The one mechanical hook is
the Review presence-gate; its *truthfulness*, like every section's, stays posture. The only unbypassable
wall is the protected-branch merge.

**The Review record** states, in plain language a non-engineer reads at the merge: the depth that ran, the
review passes that ran (as plain checks, never their internal names), that each gate completed, **whether a
review ran the operator's code in a throwaway copy to judge it** (said plainly, never left silent, since
running their code can have effects they would not expect), the findings' dispositions, and — when
post-audit fixes were made — that they were validated but not re-reviewed (so the reviewed and merged
versions differ). With no review packs installed it says so plainly
— "no extra review ran", never a green pass — and carries the standing caveat that it is the engine's own
account and the operator's merge is the real gate. A trivial fast-path build fills it with a truthful one.

**The consumed-review-lenses record.** The fenced block below records which build stage runs which installed
review; the `lens-consumption` check reads it and goes red if a review is installed that no stage runs.
product-design's spec-lock ceremony is the plan-review four's **second consumer** (it runs the same four on
a description, when installed). Machine-read — the tokens are lens names, **never operator-facing wording**.

```text
consumed-review-lenses:
  plan-review gate: product-intent, architecture, feasibility, risk-governance
  product-design spec-lock ceremony: product-intent, architecture, feasibility, risk-governance
  pre-submission gate: spec-conformance, usability, technical-integrity, security-governance
```

**The stranded-conflict case is not yet self-healing.** A sibling pull request can merge mid-flight after
integrate's reconcile, stranding a conflict; only its *resolution* leaves the operator's hands. The engine
**surfaces the stranded pull request at the next session's start and offers a one-step fix the assistant
runs on the operator's say-so** — reconcile against the latest default branch, regenerate the two index
files from the reconciled tree, lossless-or-it-does-not-run; if anything but those two files clashed, it
changes nothing and routes the operator to a plain-language decision. Never the operator resolving it by
hand.

**Routine is the same workflow, time-distributed.** For large, cleanly-decomposable bulk work the implement
phase is spread across unattended sessions: an interactive Plan records the commit sequence and scope-lock
in the build Issue, unattended sessions add commits within that scope and report progress from git and the
checklist, and an interactive Finalize integrates, reviews for cohesion, validates, and submits. Its
cohesion guarantee is planned-up-front-plus-checked-at-Finalize, weaker than interactive Build's continuous
assembly and acceptable only for decomposable work.

**The build-Issue body + checklist + scope-lock format.** The build Issue is engine-authored, so its body
realizes the control-plane engine-authored-issue body contract through the shared issue-authoring helper
(`.engine/tools/issue_author.py`), never a human web issue template. Its parts are filled from the build:
*what this is* (the build it tracks and why) and *what happens next* — the ordered commit sequence as a
machine-readable checklist ("N of M done"; the next unchecked item is the next chunk), with the permitted
write-scope alongside it as the union of the planned chunks' declared paths. Both live in the build Issue,
authored at Plan, GitHub-native and cold-readable, carrying the engine-domain label.

**Grouping product work into phases.** When a build realizes product work and the project carries a committed
build order (`docs/spec/build-plan.md`, the [product-design](../modules/product-design/manifest.json) module's
artifact), group the work under native GitHub phases at Plan — the Milestone *is* the plan. Run
`.engine/tools/milestone_emit.py emit`: it reads the build order and creates one phase per entry, never
duplicating one on a re-run, then assign each open work Issue to its phase (`gh issue edit <n> --milestone
<phase>`). The phase names are the build order's own, shown to the operator in plain language — never engine or
review vocabulary. **Absent a build order there is nothing to consume and the build plans its phase itself.** The
build order is a consumed input, authored by the module, never here.

**Checking against the settled description.** When a build realizes a product-design work item, resolve the
**settled description** at Plan — `.engine/tools/spec_referent.py resolve` on the build Issue's `Builds to:` work
item (it follows work item → its `docs/spec/` document → that document's acceptance criteria, gated on a settled
description). Hand those criteria **verbatim** (never a summary or a built-vs-spec judgment of your own) to the
plan-review and pre-submission passes as the description they check against; when none resolves the pass
discloses that plainly — never a silent pass. The **same one resolution** (consumed, not re-resolved) fills the
**Review** record's operator-runnable acceptance steps (`spec_referent.py review-steps`): the steps the operator
can run themselves, copied verbatim into two plain groups — "things you can confirm yourself" and "things I
checked for you" — or a plain reason-named line when nothing is operator-runnable (an in-tool demo and a CLI-only
check go on the engine's account). It is an offer for when the change matters, not a duty, and an unrun step is a
promise, not proof — never beside a green check. The resolution holds with or without the optional product-design
module; a read failure is surfaced loudly, never read as "no description".

**The close-linkage pre-flight.** At submit, before marking ready, the orchestrator compares what the pull
request **will** close — GitHub's computed linkage (`gh pr view --json closingIssuesReferences`, `gh api
graphql` beneath it) **plus** the closing keywords in the integrated commit messages, which that field does
not reflect — against what the pull request **declares**: a deliberate `Closes #N` line versus a `Part of #N`
dependency in its own Scope/Out-of-scope. Two contradictions are decidable without guessing intent: an issue
the change will close while declaring itself only *part of* it, and the comma-trap (`Closes #1, #2` links only
`#1`). **Detect-and-surface, never silent-and-unilateral:** the default is a plain Review line the operator
reads at the merge; only an **unambiguously-accidental, body-sourced** keyword (declared *part of*, no
deliberate close line, uniquely locatable) is **neutralized** — a minimal keyword-only edit of the engine's
own PR body, never a narrative rewrite, never product scope — and the removal is **disclosed** in Review. A
commit-sourced or cross-repo close is surfaced, never defanged; an unreadable will-close set fails closed to
the could-not-read line, never a false "nothing will close". It is **not a gate** — the comparison is
mechanical but rides the AI-authored Review record at its posture tier, bounded by the operator's own GitHub
"will close" view. `close_linkage_preflight.py check --pr N --base REF` emits the lines and any defanged body.

**Some pieces are owned elsewhere, not authored here.** The **routine entry** (`/engine-routine` and its
procedure — the scope-lock read at boot and per commit, the first-fire echo, the misfire-as-Issue) is the
routine-mode package's, and the **non-interactive permission posture** that makes an unattended run unable
to ask is settled where routine is exercised. The engine-authored-issue body contract and its helper are
core/control-plane's; the step that ensures the engine-domain label exists is provisioning's; and the
*human* web issue templates are a separate control-plane artifact a person files through. This runbook
fixes only the distributed-implement *workflow shape* and the build-Issue *format*.

**A recognized automation's pull request carries a disclosed not-applicable check — relay both decisions
plainly.** Walking the operator to merge a dependency-update pull request from a recognized automation
(Dependabot), the `engine-ci` green includes a **disclosed not-applicable pass** for the PR-body
completeness check: it does not bind for the automation's own pull requests, so it was **not verified** —
green means *not applicable*, never *checked and passed*. Keep that distinct from the **`guardrail-ack`**
label the operator actively applies, which still gates the locked-dependency change (changing pinned
dependencies is exactly what a person should consciously approve). Every other check still runs; the merge
stays the operator's.

**A fail-open finding is surfaced in the Validation section.** When filling the pull request's **Validation**
section, surface any open **fail-open finding** the engine is carrying — a safety gate that could *not run*
(a crashed hook or an unhealthy tool-runtime), promoted to a tracked engine finding and carried at boot — as
a **named line, distinct from an ordinary pass or fail**: "*a safety check could not run on this change:
what it would have checked; this work was not verified for X*." It is **non-blocking** and only informs the
operator's consent at the merge — never a new gate. If none is open, say nothing; this is a surfacing duty,
not a section to always fill (`systems/infrastructure/hooks/README.md` §Fail-open-and-flag).
