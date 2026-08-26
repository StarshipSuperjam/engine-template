---
id: eADR-0045
title: Compaction is survived by verification, not prevented; the plan-to-build boundary is a refusal
status: accepted
date: 2026-08-26
---

## Decision

The engine does not initiate, schedule, or configure context compaction, and does not estimate how full a context is. It treats compaction as something that happens TO a Build and guarantees recovery from it: every Build-mutating coordinator verb verifies the running session against the durable snapshot before it writes, unconditionally, and refuses by name on a mismatch. A post-compaction hook injects a re-grounding pointer built from a closed allowlist, and is advisory. Separately, the boundary between sealing a plan and building it becomes a mechanical refusal: Build entry declines to start in the same session that sealed the plan with no compaction since, unless the operator explicitly overrides, and that override is recorded and published. Everything else about that boundary — settling durable facts, compacting, choosing the model and effort for the build phase — is ceremony, and is described as ceremony wherever it appears.

## Significance

This settles which half of the context problem the engine owns. It owns RECOVERY, and it owns it with a mechanism that cannot be weakened by the unreliability of any signal: verification does not ask whether a compaction occurred, because the only thing that could answer is a fail-open hook. It does not own PREVENTION — no engine-written auto-compact setting in any scope, no engine-initiated compaction, no token estimation — and the operator's own threshold stays the operator's, because a project-scope value would silently outrank a user-scope one they set themselves. The consequence a later change must preserve: any new coordinator verb is verified by default, since the exempt set is a small enumerated list of read-only verbs and everything absent from it is checked. The phase barrier's honesty is equally load-bearing. It is a refusal an operator can pass in one flag, and its value is that passing it is a recorded act rather than a silent one; a future change that made it unbypassable would be claiming an authority over the operator's own session that this record does not grant.

## Rationale

Three facts about the platform, verified rather than assumed, force this shape. No mechanism lets a model initiate compaction in an interactive session, so an engine that promised to compact on your behalf would ship dead code. The auto-compact threshold is operator-configurable at user scope and the harness caps it at the model's window, so the engine has nothing useful to add and something real to break. And `SessionStart` with the `compact` matcher is the one point at which anything can reach a freshly compacted session, which is why re-grounding lives there and not on the pre-compaction event, which cannot inject at all.

The unconditional scope of verification is the load-bearing choice, and it was made against the cheaper one. Verifying only when a compaction was observed would have been a smaller change and would have read as equivalent. It is not equivalent: the observation comes from a hook that fails open by law, so the guarantee would have been the hook's reliability wearing a guarantee's name. Running always costs three cheap reads and makes the post-compaction path and the ordinary path the same code, which is also the only way the recovery path gets exercised by every Build rather than by the rare one that happens to compact. Verification reads and never writes, because a write would bump the snapshot revision under a compare-and-swap guard the verb is about to hold — the check protecting the Build would have been what wedged it. For the same reason observations live in an append-only sidecar rather than in the snapshot: a hook and the coordinator must not contend for one document.

The barrier is a refusal rather than a prompt because a prompt at the seal is exactly what the old ceremony already implied and nobody took. What made it skippable was that nothing downstream noticed. A refusal at bind is noticed, and it is proportionate: it costs an operator who genuinely means to continue one recorded flag, and it costs an operator who forgot the thing they wanted.

## Anti-choice

The strongest rejected alternative was an engine-managed auto-compact threshold rendered into project settings, which a four-lens panel refuted from three directions at once: the settings-wiring seam is closed and has no scalar-key path, a project-scope value would silently preempt the operator's own user-scope setting, the value would sit outside the operator tuning lane, and it was required by neither the barrier nor the recovery conditional. A one-time user-level recommendation replaces it and the engine writes nothing.

Also rejected: an MCP control bridge letting the engine initiate compaction, refuted by the absence of any such platform mechanism; prepared boundary receipts every N integrations, refuted because an always-current snapshot makes a receipt prepared at integration N useless to a compaction landing mid-node later, and the snapshot is the recovery authority; an attended pause at a utilization threshold, rejected because native compact-and-continue before the drift zone is strictly better than stopping and keeps unattended runs alive; and a second consent ceremony at the barrier beside the existing bind gate, rejected because that gate already refuses without a recorded operator decision and a second ceremony would drift from the first.

One alternative was rejected during implementation rather than design, and is recorded because its appeal survives the refutation: proving the session boundary from the live-session marker boot writes. That marker is keyed to the repository root of the checkout the reading code runs from, and Build entry runs in the build worktree, which is never the root boot stamped — so it reads as absent every time. A barrier resting on it would refuse every bind, and a gate that always refuses is a gate everyone learns to override.

## Status

accepted
