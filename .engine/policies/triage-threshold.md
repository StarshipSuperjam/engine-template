---
title: Triage threshold
status: accepted
date: 2026-06-03
values:
  persistence: 3
  auto_resolve: 2
  triage_pressure: 10
---

## Rule

This policy is the home of three tuning values the engine's background monitoring reads. They live in this file's settings block — the `values` at the very top — in plain sight and editable, rather than buried in code. To retune the engine, change the number there; that block is the one place the engine actually reads.

- Persistence threshold (`persistence`): how many start-ups a recurring low-impact signal must persist across before it is promoted to a tracked issue.
- Auto-resolve observation count (`auto_resolve`): how many start-ups a now-quiet tracked signal goes unseen before it is closed automatically.
- Triage-pressure threshold (`triage_pressure`): the number of open low-priority engine issues above which the next start-up shows a short standing-backlog reminder — a reminder only, it never opens or closes anything itself.

## Scope

These values govern only the engine's own background monitoring: how patient it is before it flags a recurring signal, when it treats a signal as resolved, and when it reminds you about an accumulating backlog. They take effect once that monitoring ships in a later slice; until then they stand as a documented starting point.

## Rationale

These are the dials that decide how patient the engine is before it bothers you. Set them too eager and you get pestered about things that would have sorted themselves out; set them too relaxed and a real, recurring problem takes too long to reach you. They live in this file's settings block, precisely so they are easy to find and change — raise a number to be interrupted less often, lower it to be told sooner. Nothing here is urgent or alarming; it is ordinary tuning you are free to revisit once you have seen how the engine behaves in practice.

## Enforcement-tier

**Posture.** These values are simply read by the engine's background monitoring; this policy does not itself check or block anything. Its whole force is the expectation that the values stay here — legible and tunable — rather than being hidden as fixed constants in code.
