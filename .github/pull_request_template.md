<!-- BEFORE OPENING THIS PR — does this change COMPLETE a GitHub issue, including the final slice of a multi-PR effort? If yes, add one `Closes #N` line per issue directly below this comment. Only the `Closes #N` keyword auto-closes the issue on merge — describing the resolution in prose (e.g. "resolves / satisfies / finishes #N") does NOT close it, and the issue silently lingers open. One keyword per issue: "Closes #1, #2" closes only #1 — #2 is left open. If this PR is a slice that does not yet complete the issue, add no Closes line and instead write a `Part of #N` line in the Scope or Out-of-scope section below. That `Part of #N` phrase is what lets the engine tell an accidental stray closing keyword from an intended close and offer to fix it before you merge; without it, the engine can't tell the two apart, so it neither flags nor fixes a stray keyword — your backstop is then your own read of the "will close" list on the PR page. Delete this comment if the PR closes no issue. -->

> *A green mechanical check below shows this change conforms to the engine's rules — not that it is correct. What covers correctness is the behavioural steps in **Review** and your own read of the change; a green check is never a substitute for that. **Your merge is the binding gate.***
>
> *About those checks: only the one that runs when the change is proposed for merge can stop a risky merge — a check that ran while the change was still being written is early advice. Each check is itself proven against a deliberately broken example it must catch, so a passing check can't be one that quietly did nothing — but that proves the check works, not that this change is right. And a check that could not run leaves its area unverified.*

## Purpose

**<one-line summary of why this change exists>**

- <supporting detail; add bullets as needed>

*Impact: <what this enables or unblocks>*

## Scope

**<one-line summary of what is included>**

- <the specific items, as bullets>

*Impact: <what this change delivers>*

## Out of scope

**<one-line summary of what is deliberately excluded>**

- <the specific exclusions, as bullets>

*Impact: <why these are out, not gaps>*

## Risk

**<one-line summary of what could break; call out any guardrail-weakening plainly>**

- <the specific risks, as bullets>

*Impact: <the consequence and how it is bounded>*

## Validation

**<one-line summary of how this was checked>**

- <the mechanical-check results, as bullets>

*Impact: <what an approver can rely on>*

## Review

**<one line, plain language: how careful the review was and what it found — or "no extra review ran" when no review packs are installed; never name a review pass>**

- <plain bullets: the depth that ran; the review passes that ran, written as plain checks (never their internal names); that each step completed; each finding's outcome (fixed / tracked as an Issue / escalated); and — if anything was fixed after the review — that those fixes were validated but not re-reviewed, so the reviewed version and the merged version differ. A trivial change fills this with one honest line, e.g. "I made this small, reversible change myself; no extra review.">

- <Paste here, unedited, the output of `.engine/tools/spec_referent.py review-steps` — the steps the operator can run themselves to watch this change work, in two plain groups ("things you can confirm yourself" and "things I checked for you"), copied not authored or graded. When the tool finds nothing operator-runnable it prints one plain line saying why (a behavior-preserving / internal / doc-only change; operator-runnable checks that cannot run in this environment; no settled description; or a trivial change). An unrun step is a promise, not proof — never stacked beside a green check; an offer for when the change matters, not a duty on every merge.>

*Impact: <the engine's own account of the review — the approver's merge is the binding gate>*

## Files of interest

**<one-line summary of where to look first>**

- <the key paths, as bullets>

*Impact: <what these most determine>*

## Claude involvement

**<one-line summary: design decisions vs mechanical edits>**

- <the specifics, with references to the decision/contract surface>

*Impact: <where AI judgment is load-bearing vs mechanical>*
