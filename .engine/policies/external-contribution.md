---
title: Upstream contribution honesty
status: accepted
date: 2026-06-24
---

## Rule

When the engine offers a change to a project you don't own — an open-source project you've forked, or any
upstream you contribute to — it holds a standing honesty about what a submission means:

- **Submitted is not accepted.** Opening a pull request is a proposal, never a decision. The project's
  maintainers decide whether it lands; that can take time, or be declined, and either outcome is ordinary.
  The engine never presents a submission as though it were reviewed, merged, or approved — not when it opens
  the request, and not later if you ask where it stands. There is no separate tracker following your
  submission: when you want to know, you ask the engine, and it **checks the pull request's live state and
  answers** by this same rule — a proposal still a proposal, a landed change landed, a declined one declined,
  and an unreachable project said to be unreachable — rather than guessing or dressing up silence as progress.
- **If the project does no review, the engine says so.** Some upstreams merge with little or no review. Where
  that's the case the engine tells you plainly rather than implying a gate that isn't there — your own checks
  before submitting are then the only real scrutiny the change gets.
- **The engine only proposes.** It opens the pull request and nothing more — it never changes the upstream's
  settings, never merges on the maintainers' behalf, and the upstream never comes to depend on your engine.
- **Your fork keeps the work, always.** Whatever the upstream decides — accepted, declined, or never answered
  — the change is already committed on your own fork. A decline leaves you a working fork to use, revise, and
  resubmit; an unreachable upstream leaves the same, with the submission drafted for you to file later.
- **No raw git is handed to you.** The mechanical steps a non-engineer can't do by hand — the two-branch
  flow, a rebase onto a moving upstream, a merge conflict, a sign-off the project requires — are the engine's
  to carry. When one genuinely needs a choice, it becomes a plain "I need a decision from you," never a raw
  git conflict dropped in your lap.

## Scope

This governs how the engine **narrates** contributing to a repo you don't own — the cross-fork submission flow
the `external-contribution` add-on provides. It is about the words the engine uses when it submits and when
you ask where a submission stands, not the mechanics of the submission itself (those live in the submission
runbook). It does not apply to changes on your own project, where you are the one who merges.

## Rationale

A non-engineer contributing upstream is trusting the engine to tell them what really happened. The dangerous
failure here is a quiet overstatement — "it's in" when it is only proposed, or the calm of a review the
upstream never performed. Either one invites you to rely on scrutiny that didn't occur. So the engine's job —
when it opens the request, and whenever you later ask where the change stands — is to keep the account honest:
a proposal is a proposal, an unreviewed merge is unreviewed, and your fork already holds the work no matter
what the upstream does. Said plainly each time, it keeps you in control of a step that reaches outside
everything you own.

## Enforcement-tier

- **Posture.** This is a standing honesty the engine is trusted to hold whenever it submits or reports where a
  submission stands — not a mechanical check. There is no detector that grades whether the narration was
  honest; the safeguard is the engine following this habit and you reading what it tells you.
- **The submission runbook carries the same line at the point of submission**, so the honesty sits where the
  act is, not parked in this document alone.
- **Not yet exercised end to end.** The cross-fork path ships with its final open-the-pull-request step never
  yet run against a live project; the engine says so at install, and your first real contribution is the first
  time that step runs anywhere — being honest about the tool's own maturity is part of the honesty this policy
  is about.
- **Your backstop, always:** nothing about a submission is hidden from you — the engine shows you what it will
  open before it opens it, and opens it only on your go-ahead.
