---
title: Attention
status: accepted
date: 2026-06-04
values:
  budget_blocking_debt: 0.30
  budget_in_flight: 0.25
  budget_recent_decisions: 0.15
  budget_structural_neighbors: 0.15
  budget_orientation: 0.15
  precedence_blocking_debt: 1
  precedence_in_flight: 2
  precedence_recent_decisions: 3
  precedence_structural_neighbors: 4
  precedence_orientation: 5
  trim_orientation: 1
  trim_structural_neighbors: 2
  trim_recent_decisions: 3
  trim_in_flight: 4
  trim_blocking_debt: 5
  weight_recency: 0.5
  weight_severity: 1.0
  weight_proximity: 0.5
  flex_high_debt_count: 3
  flex_orientation_delta: 0.10
  debt_blocking_threshold: 2
  scent_strong_match_threshold: 0.5
---

## Rule

This policy is the home of the dials that decide what the engine shows you first, and how much room each
kind of thing gets, every time it gets its bearings. The numbers live in this file's settings block — the
`values` at the very top — in plain sight rather than buried in code, so the engine's priorities can be read
and checked. That block is the one place the engine actually reads; the numbers are not set by hand or by
feel but calibrated from how the engine performs, and any change to them is proposed and reviewed, never silent.

There are five kinds of thing the engine can surface, and they always come in the same fixed order of
importance: anything **blocking** your work, then **work already in flight**, then **decisions made
recently**, then **the parts of the project next to what you are touching**, then general **orientation**.
That order is structural — no single dial can override it — so a genuine blocker can never be crowded out by
something less important. The dials only adjust matters *inside* that fixed frame:

- **How much room each kind gets** (`budget_*`): each kind's share of the limited space, written as fractions
  meant to add up to one whole.
- **The fixed order of importance** (`precedence_*`): the first-to-last ranking of the five kinds, where
  **rank 1 is surfaced first**.
- **What is dropped first when space runs short** (`trim_*`): where **rank 1 is dropped first**; it starts as
  the reverse of the order of importance (shed the least important first).
- **What rises to the top within one kind** (`weight_recency`, `weight_severity`, `weight_proximity`): inside
  a single kind, how much to favour the most recent item, the most severe problem, and the item closest to
  what you are working on.
- **How the room flexes with the day** (`flex_high_debt_count`, `flex_orientation_delta`): when at least this
  many blocking problems are open the session counts as busy and orientation is squeezed to make room for
  them; an easy session gives that room back to orientation. The second number is how much room moves.
- **How bad an open problem must be to actually stop you** (`debt_blocking_threshold`): the severity an open
  problem must reach before it blocks the start of new work rather than just being mentioned.
- **How strong a word-match must be for the per-prompt hint to fire** (`scent_strong_match_threshold`): the
  bar a quick keyword match must clear before the engine volunteers a pointer mid-conversation.

## Scope

These dials govern only how the engine *prioritises and sizes* what it shows you when it gets its bearings —
at the start of a session and on each prompt. They decide ordering and room, nothing else: not whether
something is a tracked problem in the first place (that is the monitoring policy's job), not what is fetched
or stored, and not the product you are building — only the engine's own attention. They take full effect once
the start-of-session and per-prompt orientation events ship in later slices; until then they stand as a
documented, working starting point that the ranking tool already reads.

When the engine looks at the parts of the project next to what you are touching, it follows only the
**structural wiring** between them — who owns a file, which rule governs it, what a check targets, and which
parts depend on which. That neighbourhood walk deliberately ignores other kinds of links the project's map
may record (for example, which decision replaced an earlier one): those are looked up only when you ask for
them directly, never pulled in as bulk background, so getting your bearings stays fast and cheap no matter
how rich the map grows. This is a fixed rule of how the engine orients, not one of the tunable numbers above.

## Rationale

Left to itself, an assistant shows you whatever is easiest to reach, not what matters most. These dials make
that choice explicit. The fixed order of importance is the safety net: a real blocker can never be pushed
below a routine feature by a mis-set number, because the order is built into the structure, not into a weight
someone has to get right. The dials inside that frame are not set by hand or by guesswork — each is meant to
be calibrated from how the engine actually performs (which things it surfaced, which blockers it caught or
missed), and any change is proposed and reviewed before it takes effect. They are kept here, explicit and
legible, so the engine's priorities can be inspected and questioned, not so they are fiddled with by feel.
Nothing here is urgent: these are deliberate starting values, not yet proven against real use; they earn
their numbers as the engine is observed in practice.

## Enforcement-tier

**Posture.** These values are simply read — by the ranking tool that orders what you see and sizes each kind.
This policy does not itself check or block anything, and nothing is enforced on you by it. The fixed order of
importance is held by the *structure* of that tool, not by this file: the tool sorts the five kinds into
their ranked slots before any weight is applied, so the order holds even if a dial is mis-set. The shares,
weights, and thresholds take real effect only once the start-of-session and per-prompt orientation events
ship in later slices; today the tool reads them and would apply them, but no orientation event yet surfaces
the result and nothing blocks work. This policy's whole force is the expectation that these numbers stay
here — legible, calibrated from evidence, and surfaced for review when they change — rather than being
hidden as fixed constants in code.
