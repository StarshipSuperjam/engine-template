---
title: Finding disposition
status: accepted
date: 2026-06-03
---

## Rule

Every concern the AI raises while working must reach exactly one durable outcome — never a "maybe later" left floating in the conversation.

For an ordinary concern discovered during work:

- If it is an engineering blocker inside the approved design and scope → solve it as part of the work.
- If resolving it would change design, law, authority, the agreed capability boundary, require a guardrail
  acknowledgement, or require another operator-only choice → surface that boundary for a decision.
- If it is real but outside the current work → open a tracked issue and move on without asking permission to
  absorb it into this Build.

For a cold-review finding, the orchestrator first judges whether the concern is correct and whether the
reviewer's suggested remedy fits. Reviewer severity is advice, not an automatic response. Record one of:

- **Accepted and fixed** — the concern is correct and the in-scope repair landed.
- **Accepted and tracked** — the concern is correct but is deliberately outside this PR, with a durable Issue.
- **Partially accepted** — the underlying concern is real, but a bounded remedy fits better than the proposal.
- **Rejected** — the concern or proposed consequence does not hold, with grounded rationale.
- **Escalated** — a genuine design, law, authority, capability-boundary, guardrail-acknowledgement, or
  operator-only decision remains; record which boundary is implicated.

Record separately whether the finding still blocks this PR. A `blocking` or `serious` reviewer label never
sets that field by itself, and accepting a concern never means accepting its proposed remedy.

This supersedes the older automatic rule that every unresolved reviewer-labelled `blocking` finding returns
to the operator. When the orchestrator judges a reviewer-labelled blocking concern does not block this PR,
the disagreement is mandatory merge-surface evidence: the PR Review record names the finding and gives a
safe operator-facing summary of the concern and adjudication. Sensitive details stay in local evidence; the
public line may instead name a bounded private security reference. This preserves operator visibility without
making reviewer severity or a proposed remedy authoritative.

A "not urgent, we'll get to it" aside with no record created is a violation of this rule.

## Scope

Applies to anything the AI surfaces during a working session. The "fix it in line" outcome is deliberately narrow: it is allowed only when the fix is both small and directly related to the work in hand. Anything larger, or unrelated, becomes a tracked issue instead — work is never quietly expanded to absorb it.

## Rationale

The point is simple: no concern should disappear into a transcript, and no reviewer should silently become the
engineer of record. Ordinary concerns still resolve to fix, track, or escalate. Review findings preserve the
orchestrator's adjudication so the operator can see which concern was accepted, bounded, rejected, or raised
for a real decision. Tracked issues are not a hidden backlog either — they return through normal Engine
attention rather than waiting to be hunted down.

## Enforcement-tier

- **Posture** — the disposition habit itself is an expectation the AI is trusted to follow on every concern it raises.
- **Hard-fail (the close gate's, not this policy's)** — the end-of-session ritual pushes back until every concern raised has been given a disposition, and hands the operator a plain-language summary instead of leaving them to scour the transcript. That ritual is built as the turn-close `Stop` hook; this policy doc itself stays posture, while the gate enforces a strong local block over the findings that were recorded.
- The protected-branch merge is the durable gate — but what it enforces is the operator's **consent on evidence**: the mechanical checks and the behavioural demonstrations they can run (eADR-0013), not a reading of the diff for semantic defects. So it covers what was **recorded** — a written-down concern travels into the change set and its checks. A concern noticed but never recorded has **no** merge-time detector, and the operator is not expected to reconstruct it from the code; its capture rests on the disposition habit above, not on human review catching it at merge.
