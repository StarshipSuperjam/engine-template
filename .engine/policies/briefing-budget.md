---
title: Briefing budget
status: accepted
date: 2026-08-09
values:
  excerpt_chars: 200
  pin_index_title_chars: 80
  pin_index_count_max: 8
  pins_block_chars_max: 1300
  posture_lines_max: 8
  posture_chars_max: 700
  neighborhood_groups_max: 8
  mechanic_grounding_chars_max: 900
  dashboard_chars_max: 4500
  margin_floor_chars: 300
---

## Rule

Project state reaches a session through **three projections**, not one boot pack, and this policy's dials
are distributed across them. Naming the three is what makes each dial's job legible:

1. **The pushed session-start relay** — one compact typed envelope injected at session start. It carries a
   **never-shed core**: the grounding receipt, the action-forcing alarms, identity, the write-gate authority
   contract, the task binding, the standing directives (the **pins index** and the one-line where-we-left-off
   pointer), and the closed-enumeration pointers. Behind the never-shed core rides a small **reconstructible
   tail** — the work-neighbourhood *pointer*, and (in an engine-mechanic checkout) the build-sprawl note —
   which is the only content that sheds under size pressure, in a fixed order, and always with a plain
   disclosure of what went.
2. **The pulled status dashboard** — the full ranked project-state view (fact and count lines, the stance
   line, the shipped-work digest, the backlog register). It is **not pushed**: it renders only when the
   operator asks (`/engine-status` / `engine_status.py`). It left the boot pack entirely in the
   typed-envelope cutover; it is not a component the set-aside ladder can shed, because it is not in the pack.
3. **The point-of-use pulls** — the knowledge-graph neighbourhood walk and the memory-recall session
   excerpts, each rendered on demand when a change actually reaches that context, never pushed every session.

Two guarantees hold in the pushed relay, every session:

- The **governance and consent content is never set aside** — and, since the typed-envelope cutover, neither
  is the **pins index** nor the **where-we-left-off continuity pointer**: both were promoted into the
  never-shed core, so they now *outlast* the reconstructible tail rather than yielding before it. A **margin
  canary** holds a stated character **margin** (`margin_floor_chars`) between the measured never-shed core and
  the platform's injection cap, so ordinary *structural* growth of the never-shed content is caught before it
  can tip the core over the cap. That margin has a hard minimum set in code that this file may raise but never
  lower. (Governance **alarms** can still, when enough fire at once, grow the never-shed core toward the cap;
  the canary models that heavy-alarm case explicitly rather than assuming a quiet session.)
- Every component that can grow has a **character budget**, and the reconstructible tail has a **place in the
  set-aside order** (see Scope). When the relay would exceed the cap, the lowest-value tail components are set
  aside first, and their absence is disclosed in plain words — including a distinct, always-shown alert if the
  operator's own pinned notes overflow the index, so the operator learns to prune them rather than losing them
  silently.

The dials live in this file's `values` block — in plain sight, read directly by the engine, not buried as
constants — so the priorities can be read and reviewed.

## Scope

This governs how each projection is fit to the platform's per-value output size limit: the character bounds on
each growing component and the order the pushed relay's reconstructible tail is set aside under pressure. It
governs the **mechanical fit** to a physical size limit — not the **cognitive priority** of what to surface,
which is the attention policy's job (its item-count budgets and its five-kind ranking). The two are
deliberately separate: attention decides *what is worth showing and how much room each kind of thing gets*
across the three projections; this policy decides *how each assembled result is trimmed to physically fit the
platform's byte limit*. It does not govern the per-prompt scent.

Each dial below is marked **[relay]** (read at session-start pack build, bounding the pushed never-shed core or
its reconstructible tail), **[point-of-use]** (read by a renderer invoked on demand at a pull, no longer pushed
every session), or **[pull-dashboard]** (a growth alarm on the pull-only status dashboard). No dial is retired:
every one still has a live consumer, proven by the dial-consumption test.

- `excerpt_chars` **[point-of-use]** — the longest a single quoted line from the operator's own past sessions
  may run before it is clipped. It bounds the recent-sessions renderer (`render_recent_sessions`), the
  point-of-use renderer for the **memory-recall pull**; the pushed relay carries only a one-line
  where-we-left-off pointer, not the full excerpts. Applies to conversational quotes only, never to a pinned
  note's text.
- `pin_index_title_chars` **[relay]** — pins are shown as a compact **index**: one title-length line each,
  with full text pulled on request. This is the longest a single pin's index line may run. The pins index is
  never-shed core.
- `pin_index_count_max` **[relay]** — how many pins the index shows, newest first. When more exist, the rest
  are folded behind a **loud, directive-aware disclosure** (how many older pins are not shown, that each may
  carry a standing instruction, and that the full set is one `list-pins` away) — never a silent drop, and
  nothing is removed from storage. A pin list grown past this is itself the signal to prune.
- `pins_block_chars_max` **[relay]** — a backstop on the whole pins block: if the index still overflows this
  after the count cap, the shown count is trimmed further (folding into the same disclosed count), so the block
  always fits its budget while the loud disclosure and provenance caveat are always kept. Because pins are
  never-shed, this ceiling is also the term the margin canary reserves for a full pins block.
- `mechanic_grounding_chars_max` **[relay]** — a **growth alarm** on the prose of an engine-**mechanic**'s
  never-shed build-safety grounding (do-not-build-in-the-shared-clone, the isolated-worktree route,
  non-reflexivity), measured at a representative checkout path so it trips when the *prose* grows. Its code
  floor keeps an unguarded policy edit from setting the budget uselessly low; it is not a promise to bound
  every deployment's render, since the checkout path is deployment-specific. The real overflow guard is the
  **mechanic margin canary**, which measures the actual assembled never-shed core (path included) against the
  cap.
- `posture_lines_max` / `posture_chars_max` **[relay]** — the most lines and characters the execution-posture
  relay (always-shown operating guidance) may occupy before it is clipped to a pointer. Never-shed; shipped
  well above the real posture size, so clipping is insurance, never the normal case, and the code floor keeps
  the safety text from being gutted.
- `neighborhood_groups_max` **[point-of-use]** — the most relationship groups the work-neighbourhood summary
  lists before the remainder is disclosed as a count. It bounds the neighbourhood renderer
  (`render_neighborhood`), the point-of-use renderer for the **knowledge-graph pull**; the pushed relay carries
  only a compact neighbourhood pointer, and the full walk (bounded by this dial) is pulled on demand.
- `dashboard_chars_max` **[pull-dashboard]** — a **growth alarm**, not a trimmer: the size the pull-only status
  dashboard's routine body is not expected to exceed. A dashboard that grows past it fails the growth check
  loudly rather than drifting. It no longer sizes any pushed content: the dashboard left the pushed relay in
  the typed-envelope cutover, so this dial is a regression bound on the pull surface, not a boot-pack budget.
- `margin_floor_chars` **[relay]** — the character margin the measured never-shed core must keep under the
  injection cap in the worst modelled case. A hard minimum in code bounds this from below.

The fixed set-aside order in the pushed relay (first set aside → last kept): the build-sprawl cleanup note,
then the work-neighbourhood pointer. Governance and consent content — including a mechanic's never-shed
build-safety grounding — the pins index, and the where-we-left-off continuity pointer are **never** set aside.
The status dashboard is not in this order at all: it is a separate pulled projection, never a candidate to
shed. The build-sprawl note sheds first because it is a low-value housekeeping nudge whose operator-facing
detail already rides the (pulled) status dashboard, so setting it aside loses nothing the operator needs.

### Per-shape relay ceilings

The size spike measured the pushed relay's never-shed core against the platform's 10,000-character injection
cap for each deployment shape that carries mandatory content, modelling every never-shed term at its ceiling
(a full pins block at `pins_block_chars_max`, execution posture at `posture_chars_max`, and a heavy
simultaneous-alarm pile-up) rather than at whatever a quiet session happens to render — the honest worst case:

- **Plain deployment** — measured worst-case never-shed core ≈ 6.4 KB, held by the plain margin canary.
- **Engine-mechanic** — measured worst-case never-shed core ≈ 7.6 KB (it carries the mandatory build-safety
  grounding a plain deployment does not), held by the separate mechanic margin canary.

Each fits the 10,000-character cap with `margin_floor_chars` to spare, and the receipt plus the action-forcing
alarms render inside the first 2,000 characters (the platform's truncation-preview window). The engine-mechanic
shape is retained but treated as vestigial; its canary and floors are left untouched.

## Rationale

Past the platform's per-value limit the platform silently replaces the value with a truncated preview — which
would drop the operator's status and the AI's orientation with no notice, the exact failure this policy exists
to prevent (a session once told the operator "nothing alarming was cut" about content it never saw). Splitting
project state into three projections — a minimal pushed relay, a pulled dashboard, and point-of-use pulls —
keeps the pushed relay small enough that its never-shed core clears the cap with margin, while the fuller views
stay available on demand. Bounding each growing component and setting aside only the reconstructible tail —
with a plain disclosure of what went — keeps the consent-critical content, the pins index, and the continuity
pointer always present, and the margin keeps one extra line from silently tipping the core over.

Raise a budget when a component legitimately needs more room and the margin allows it; lower one when a
component is crowding the core. Raise `margin_floor_chars` to demand more headroom; it cannot be lowered past
the code minimum, because the number that defines "eroded" must not itself be quietly editable — otherwise the
guarantee against silent erosion could be removed at one remove.

## Enforcement-tier

**Layered.** The `values` are read live by a reader that never raises: a missing or malformed file falls back
to the shipped defaults compiled into code, so the boot pack always assembles. The set-aside itself is
mechanical, in the pack assembler. Hard checks run in CI:

- a **margin canary** asserts the *measured* never-shed core — the governance structure, a full pins block at
  its ceiling, execution posture at its ceiling, and a heavy simultaneous-alarm pile-up, summed as
  independently-worst terms — fits the injection cap with `margin_floor_chars` to spare. The bound is the
  measured never-shed core against `cap − margin_floor_chars`; it does not reserve a dashboard-sized headroom,
  because the dashboard no longer rides the pushed relay. An engine-**mechanic** carries mandatory never-shed
  build-safety grounding, so its shape is modelled by a **separate margin canary** (StarshipSuperjam/engine-template#950); a mechanic
  driven by a heavier runtime sits tighter and is disclosed as such in the test rather than silently assumed to
  hold — the honest bound, not the flattering one.
- **per-component budget tests** each fail — naming the component — when one outgrows its budget, and the
  pull-only dashboard's routine body is held to `dashboard_chars_max` by its own growth check.
- a **dial-consumption test** proves the set of declared dials is exactly the set consumed by code — every
  declared dial is read by a live consumer (the pack-build relay, a point-of-use renderer, or a pull-surface
  growth check), and no read references an undeclared dial — so a dial can never become dead config nor a typo
  slip in unnoticed.
- the **hard code minimum** on the margin is what this policy cannot weaken: a test asserts `margin_floor_chars`
  is at least that minimum, and the shipped-defaults pinning test holds the code fallback equal to this file's
  `values` so the doc, the code, and the canary cannot drift while the file is readable.

The pin-overflow alert is a posture surfaced to the assistant to relay; the protected-branch merge remains the
real gate.
