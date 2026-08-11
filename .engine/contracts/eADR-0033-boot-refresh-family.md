---
id: eADR-0033
title: Boot is a read-only refresh family that honours deferrals
status: accepted
date: 2026-06-29
---

## Decision

Orientation is a family of read-only cognition-refresh moments, not a single startup ritual: a heavy cold-start pack at session start, a near-zero per-prompt scent every turn, and post-compaction re-orientation riding the next scent. Boot owns only the event model — which moments fire, on which hook, at what cadence and cost tier — and is the integration point that renders, in plain language, the operator-facing readouts its neighbours hand it; it never regenerates derived or committed state, and its sole local write is a gitignored presentation marker recording what was already shown.

Boot also owns the **mechanical fit** of the assembled cold-start pack to the platform's per-value output size limit — distinct from the cognitive priority above. Because the platform silently replaces an over-limit value with a truncated preview, boot measures the rendered pack before injecting it and sets aside whole lower-value components, in a fixed order, rather than truncating one mid-content. This fit is governed by the `briefing-budget` policy — a character budget and a set-aside rank per growing component. The **governance and consent content is never set aside**; the **status dashboard is the last thing set aside** — kept in every ordinary session, yielded only under extreme pressure (after every other component, when a heavy load of governance alarms — themselves never set aside — leaves no room). A margin canary holds a stated character margin under the size limit in the clean case, so ordinary structural growth of the never-shed content is caught before it starts eroding the dashboard's room; it is not an absolute promise the dashboard never sheds. The set-aside order, first to last: the build-sprawl note (mechanic only) → work neighbourhood → where-we-left-off → the pin index → the status dashboard; a mechanic's mandatory build-safety grounding is never set aside, alongside governance and consent. A set-aside is disclosed in plain words — a pin set aside, or older pins folded behind the index cap, raises a distinct, always-shown alert so the operator prunes rather than silently loses it.

## Significance

This locks orientation as plural, read-only, and unconditional-with-a-floor: refresh fires on its own, never as a step the operator must invoke, and never as a regeneration of canonical state. It fixes that boot is a renderer of other systems' contracts, not an originator — it surfaces a refused state cursor, reversible forgetting, an unprotected branch, and degraded substrates in plain words, but the detection and the fix belong to the systems that own them. Later work must respect this seam: a neighbour may refine its own internals and its own gate, but boot fixes only the disclosure, and any new operator-facing alarm must arrive as a deferral boot renders, ranked behind the governance-critical ones, never as logic boot invents.

It also locks the byte-fit as **governed, not emergency**: every component that grows carries a named budget and a set-aside rank, so trimming becomes a stated policy rather than a silent truncation, and a margin canary keeps the never-shed content (governance + consent) plus the routine dashboard within a stated headroom in the clean case, so structural growth is caught before it starts eroding the dashboard's room. The growth-vector table (dials in the `briefing-budget` policy):

| Component | Grows with | Dial | Set-aside rank |
|---|---|---|---|
| Build-sprawl note (mechanic) | count of stale stray workspaces | counts-only one-liner; operator detail rides the dashboard | 1 (first set aside) |
| Work neighbourhood | graph relationships near the work | `neighborhood_groups_max` | 2 |
| Where we left off | quoted length of past-session lines | `excerpt_chars` | 3 |
| Pin index | number of operator pins | `pin_index_title_chars`, `pin_index_count_max`, `pins_block_chars_max` | 4 |
| Status dashboard (routine body) | project state, counts | `dashboard_chars_max` (growth alarm) | 5 (last set aside; a heavy alarm load can still shed it, disclosed) |
| Mechanic build grounding | mandatory build-safety text (mechanic only) | `mechanic_grounding_chars_max` (growth alarm, code-floored) | never set aside |
| Governance + consent | — | — | never set aside |
| Clean-case headroom | never-shed content + routine dashboard | `margin_floor_chars` (hard code min) | — |

Three amendments (StarshipSuperjam/engine-template#950), each recorded here so the built code and this contract cannot drift:

- **The build-sprawl cleanup note leaves never-shed Tier 0.** It was appended to the mechanic grounding (and so inherited never-shed); it is now a separate, counts-only, first-to-shed one-liner, with its operator-facing detail (paths, idle days, remove/prune steps) on the last-shed dashboard and `/engine-status`. A low-value housekeeping nudge should not consume the never-shed room that safety grounding and consent content need. Its detector is now **activity-aware** — a stray workspace with recent git activity is a possibly-live session's and is not surfaced — so the nudge stops firing on the operator's other open sessions.
- **The pin index gains a bounded, LOUD fold.** "Every pin always visible" is amended: the index shows the newest `pin_index_count_max` titles and folds the rest behind a **loud, directive-aware disclosure** (how many older pins are held back, that each may carry a standing instruction, that `list-pins` shows them all). This is not the old silent rank-out — nothing leaves storage and the remainder is announced — and it is what lets the pins block carry a hard `pins_block_chars_max` budget. The live-read `briefing-budget` policy is amended in lockstep, not just this record.
- **A mechanic's never-shed grounding gains a budget and its own canary.** An engine-mechanic carries mandatory build-safety grounding a plain deployment does not, so `mechanic_grounding_chars_max` (code-floored above the real render) alarms on its growth, and a **separate margin canary models the mechanic shape** — which product CI's plain canary never exercises (it runs the home shape, where the grounding does not render: the exact blindness that let the mechanic pack shed continuity and pins every session). That mechanic canary is tuned to the mechanic's own runtime; a heavier runtime sits tighter and is disclosed as such in the test, never silently assumed to hold.

## Rationale

A cold session must reground itself without depending on the operator to remember a command, and most of what it must say is already owned elsewhere — the cursor store, recall, the branch-protection signal, the substrate health. Making orientation a family lets the heavy cost fall where latency is tolerable (building) and stay near zero where it is not (every prompt), while a single rendering point keeps the operator from meeting four different voices for four different problems. The trade is deliberate: boot accepts being downstream of everything and inventing nothing, so that each upstream system can settle its own contract independently and boot simply honours the handoff rather than racing it.

The byte-fit governance is the same posture applied to the platform's hard size limit: past that limit the platform silently substitutes a truncated preview, which once told an operator "nothing alarming was cut" about content it never saw. A per-component budget with a fixed set-aside order and a disclosed margin turns that silent loss into a governed, announced trim — and the margin's hard code floor keeps the number that defines "eroded" from being quietly lowered at one remove.

## Anti-choice

The strongest rejected alternative framed boot as setting a per-event cost ceiling that the prioritiser then allocates within. It lost because the prioritiser already owns the within-event budget split and its flex — a clean session gets more orientation, a high-debt one less — so a boot-owned ceiling would contradict that ownership and split one decision across two systems; the honest line is event-model here, within-event budget there. A second rejected option had a malformed state file hard-halt the session-start moment via an exit code. It lost because that moment has no safe halt: an exit-halt strands a non-engineer with a dead session and no recourse, where the correct posture is fail-loud within fail-open — surface the refusal, emit a finding, and fall through to the committed floor so the session degrades plainly instead of crashing.

The byte-fit budget added here does **not** reopen that first rejected ceiling. That ceiling was a *cognitive* one — how many items of each kind are worth surfacing — and it stays with the prioritiser (eADR-0032, the attention policy's item-count budgets and five-kind ranking), applied *before* rendering. The `briefing-budget` dials are a *mechanical* one: how the already-prioritised, already-rendered pack is trimmed to fit the platform's physical byte limit. Attention decides what is worth showing; boot decides how the rendered result is made to physically fit. Keeping the two in separate policies with that stated boundary is what prevents either from silently re-owning the other's decision.

## Status

accepted
