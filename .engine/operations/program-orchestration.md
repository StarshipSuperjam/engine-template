---
title: Program orchestration — the judgment upstream of a multi-PR program
---

## Purpose

Some work is too large for one pull request and has an *order* that matters: a capability delivered
across several PRs, a backlog cleared in a planned sequence, a migration whose steps depend on each
other. A **program** is how the engine holds that order — a durable record of the multi-PR shape,
authored and read through the Program Manager (`program_manager.py`), which lives beside the Project
Manager the way order authority lives beside plan authority. The Project Manager still owns every
individual plan; the program records only how those plans relate and in what order they land.

This runbook is the judgment half. The tool owns the sequence — it shows the next move, names what a
step requires, and refuses an out-of-order one while naming the way forward — so ask *it*, not this
page, for anything mechanical. What the tool cannot supply is the judgment this page carries: whether
work is a program at all, how its children get shaped, when the order should be re-decided, and when
the operator's own hand is the only thing that should move next.

Enter it when work that spans several PRs needs its order held, when an operator asks where a program
stands or how it is sliced, or when a backlog needs turning into a planned sequence of PRs.

## Steps

### 1. Decide whether this is a program at all

A program is a deliberate act, never inferred from resemblance. Two plans that happen to touch the
same area are not a program; three PRs that only make sense delivered in a particular order, carrying
obligations forward from one to the next, are. Ask:

- Does the work genuinely span more than one PR, or is it one change you are tempted to split for
  comfort? One PR's worth of work belongs in a plan, not a program.
- Does the *order* carry meaning — does a later PR depend on an earlier one having landed, or inherit
  an obligation it must answer for? If the pieces are independent, they are separate plans, not a
  program's chain.
- Is there a single objective the whole sequence serves? A program has one end it is working toward;
  a pile of unrelated debt does not become a program by being listed together.

If the answer is no, say so and stay with ordinary plans. Manufacturing a program around work that
does not need one adds ceremony the operator will have to carry forever.

### 2. Author children just-in-time, not all at once

A program is a chain of plans, and each plan is still authored, reviewed and sealed through the
Project Manager and [plan orchestration](plan-orchestration.md) — the program adds no second planning
path. Author the *next* child when its predecessor's shape is settled enough to build against, not the
whole chain up front: an early plan written against a guess about a later one bakes that guess in, and
the order is meant to be re-decided as evidence arrives, which a fully pre-authored chain resists.

Each child carries its linkage — which program it belongs to, which plan it succeeds, and any
obligation handed forward from its predecessor. An obligation carried forward is the one thing the
program refuses to let drop silently: it must be answered by the next child on its branch — met, still
carried, or explicitly released with a reason — and the tool enforces that at the seam. Read what the
predecessor owes before authoring the successor, so the successor answers for it by design.

### 3. Re-decide the order as evidence arrives

The chain records a decision, and a decision can be revisited. When a build teaches you the order was
wrong — a step needs to come earlier, a child turned out misconceived, a new piece belongs in the
middle — change the record rather than working around it. Insert a plan before an existing child;
supersede a child that turned out wrong while keeping it and its place visible; the only order that
cannot be re-decided is one history has already merged. The tool has a verb for each of these and
refuses the ones that would rewrite merged history; ask it which move fits.

What never happens here is dispatch. The program records and recommends order; it never selects,
starts, or advances a child. A session works one plan at a time, and which plan it works is the
operator's call, not the program's.

### 4. Lanes: the operator's concurrency, made visible — including when not to lane

A repository can carry more than one line of work at once, and a program can record a **lane split**:
a decision that certain children may ride concurrently because they touch disjoint territory. The
engine can *propose* a split by looking at what each child's plan would touch, but the split itself is
the operator's decision — recorded, revisable, withdrawable — never a schedule the engine runs.

The lived shape of concurrent work worth recognizing: a body of work split into a handful of lanes
drawn by **file territory** (so two lanes developed at once will not collide in the same files), one
session driving each lane, dropping back to serial once the concurrency stops paying — and any
merge-order constraint *across* lanes stated up front rather than discovered at rebase time. When an
operator arrives with a backlog and asks how to clear it in parallel, that is the shape to reach for.

And the honest answer is sometimes **do not lane it**. A backlog whose own discipline is serial —
where each piece must land before the next is even ready — is not made faster by drawing lanes on it;
lanes there invent a concurrency the work does not have. Recommending serial execution, or one lane,
is a real answer, not a failure to find parallelism. Lane only what genuinely has disjoint territory
and no ordering dependency between the lanes.

Once a split stands, it is legible at a glance: the portfolio shows per-lane standing (what is in
flight on each lane, what has settled, any cross-lane merge-order risk), and the program's own view
shows the complete picture. Neither ranks the lanes or tells anyone what to do next — they disclose,
they do not dispatch.

### 5. End the program honestly

A program does not complete itself. Every child landing is not the same as the objective being met,
and the engine will not derive completion from a full chain — completion is a judgment someone records
deliberately, and it can be reopened if that judgment turns out premature. Two failure modes to avoid:

- **Reading a finished chain as a finished program.** When every authored child has landed but the
  objective is not yet recorded complete, that is exactly what the surfaces will say — unwritten
  successors are unknown, not done. Do not let "all the PRs merged" stand in for "the objective is
  met"; if more work is needed, author the next child, and if it is truly done, record it.
- **Dropping an obligation by saying nothing.** A carried obligation with no successor left to answer
  it is released only with a stated reason — the reason is the whole price of letting it go.

Completing, retiring, abandoning and reopening are the operator's recorded acts. Surface the state and
let them make the call; do not close a program on your own initiative.

### 6. The reading surfaces

Three surfaces answer three different questions, and reaching for the right one is most of engaging a
program well:

- **The portfolio** — every open program at a glance: what each is for, how far along it is as facts
  (not a percentage), what is in flight, and, where a split stands, per-lane standing. This is the
  answer to "where do things stand across all my programs" — the grouped view the flat plan list does
  not give.
- **The program's own view** — one program in full: its children in chain order, what each owes, the
  complete lane standing, and the history of how the order and objective were revised.
- **The generated file at rest** — each program keeps a generated document in its own folder, the way
  a plan keeps one, for reading a program at rest. It states the moment it was generated and can lag a
  child changed outside a program verb until the next verb or the regeneration sweep; treat its
  timestamp as the truth about its freshness.

## Come in through the right door, and ask the tool for the rest

The program tool is the authority on sequence; this page does not restate its order. Reach it for the
mechanics — starting a program, appending or inserting a child, recording or withdrawing a lane split,
reading the portfolio or one program, closing or reopening — and trust what it refuses: a refusal
names the way forward, and re-deriving its order here would only give a second answer free to drift
from the one the tool enforces.

## Done when

The work's shape is settled honestly: either it is a program, with its order recorded, its next child
authored when its predecessor is ready, its obligations carried or released with reasons, and its
lanes (if any) reflecting real disjoint territory — or it is not a program, and it stays as ordinary
plans. Concluding that multi-PR work does *not* need a program is a finished outcome too.

## Notes

The failure this runbook most guards against is **manufacturing order that isn't there** — drawing
lanes on serial work, splitting one PR's change into a ceremonial program, or reading a merged chain
as a met objective. The program machinery makes real order legible; it cannot make invented order
true, and a program that records a shape the work does not have is worse than no program at all.
