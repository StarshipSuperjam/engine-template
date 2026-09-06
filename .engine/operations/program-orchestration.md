---
title: Program orchestration — the judgment upstream of a multi-PR program
---

## Purpose

Some work is too large for one pull request and has an *order* that matters: a capability delivered across several
PRs, a backlog cleared in a planned sequence, a migration whose steps depend on each other. A **program** is how
the engine holds that order — a durable record of the multi-PR shape, authored and read through the Program
Manager (`program_manager.py`), order authority beside the Project Manager's plan authority. The Project Manager
still owns every individual plan; the program records only how those plans relate and in what order they land.

This runbook is the judgment half. The tool owns the sequence — it shows the next move, names what a step
requires, and refuses an out-of-order one while naming the way forward — so ask *it* for anything mechanical.
This page carries what the tool cannot: whether work is a program at all, how its children get shaped, when the
order should be re-decided, and when only the operator's own hand should move next. Enter it when work spanning
several PRs needs its order held, when an operator asks where a program stands or how it is sliced, or when a
backlog needs turning into a planned sequence of PRs.

## Steps

### 1. Decide whether this is a program at all

A program is a deliberate act, never inferred from resemblance. Two plans that happen to touch the same area are
not a program; three PRs that only make sense delivered in a particular order, carrying obligations forward from
one to the next, are. Ask: does the work genuinely span more than one PR, or is it one change you are tempted to
split for comfort (one PR's worth belongs in a plan)? Does the *order* carry meaning — a later PR depending on
an earlier one having landed, or inheriting an obligation it must answer for (independent pieces are separate
plans)? Is there a single objective the whole sequence serves (unrelated debt does not become a program by being
listed together)? If no, say so and stay with ordinary plans: a program manufactured around work that does not
need one adds ceremony the operator carries forever.

### 2. Record the intended order up front; author children just-in-time

When a program starts, record what has actually been DECIDED about the steps and which follows which, with the
reason on each edge. The intended order is declared precedence recorded up front — never derived by the engine and
never a total order. Dependency between steps is one reason among several that edges carry: an evidence gate
(reproduce the failure before fixing it), a risk sequencing (the riskier slice first, to learn), a merge-order
constraint, or any other reason you decided one step must follow another. Two steps with no decided precedence
between them are left unordered rather than ranked; the tool does not and will not rank them.

A program is a chain of plans, and each plan is still authored, reviewed and sealed through the Project Manager
and [plan orchestration](plan-orchestration.md); the program adds no second planning path. A session reads the
program's recorded intended order BEFORE authoring the next child, the way it already reads what the predecessor
owes. Author the *next* child when its predecessor's shape is settled enough to build against, not the whole chain
up front: an early plan written against a guess about a later one bakes that guess in, and the order is meant to be
re-decided as evidence arrives, which a fully pre-authored chain resists.

Each child carries its linkage — which program it belongs to, which plan it succeeds, and any obligation handed
forward from its predecessor, and whether it fulfils a recorded intent, stands outside the intended order, or was
authorized to jump ahead out of order. The next child on its branch either claims an intent it fulfils (out of
order with a reason if the precedence graph demands it), records that it stands outside the intended order when it
fulfils none, or is refused until one of those doors is passed. A carried obligation is the one thing the program
refuses to let drop silently: the next child on its branch must answer it — met, still carried, or explicitly
released with a reason — and the tool enforces that at the seam; read what the predecessor owes before authoring
the successor, so the successor answers for it by design.

### 3. Re-decide the order as evidence arrives

The chain records a decision, and a decision can be revisited. The intended order is never sealed but always
recorded, so each revision carries a reason into history. When evidence arrives that an unbuilt intent is wrong,
revise or withdraw it: `program intend revise` replaces its title, statement or declared precedence, and `program intend withdraw`
marks an intent withdrawn while keeping it visible. When a built child needs to be replaced, `program supersede`
does that while keeping it and its place visible — and if the superseded child held a claim to an intent, the
replacement inherits it, and the claim transfer is recorded in history.

When a build teaches you the order was wrong — a step needs to come earlier, a child turned out misconceived, a
new piece belongs in the middle — change the record rather than working around it: record a new intent or revise
an existing one, insert a plan before an existing child, or supersede a child that turned out wrong. The only order
that cannot be re-decided is one history has already merged; ask it which verb fits, and trust it to refuse the
ones that would rewrite merged history. What never happens here is dispatch: the program records and recommends
order; it never selects, starts, or advances a child, and which plan a session works, one at a time, is the
operator's call.

### 4. Lanes: the operator's concurrency, made visible — including when not to lane

A repository can carry more than one line of work at once, and a program can record a **lane split**: a decision
that certain children may ride concurrently because they touch disjoint territory. The engine may *propose* a
split from what each child's plan would touch, but the split itself is the operator's decision — recorded,
revisable, withdrawable — never a schedule the engine runs. The lived shape worth recognizing when an operator
asks how to clear a backlog in parallel: a handful of lanes drawn by **file territory** (so two lanes developed at
once will not collide in the same files), one session driving each lane, dropping back to serial once the
concurrency stops paying — and any merge-order constraint *across* lanes stated up front rather than discovered at
rebase time.

And the honest answer is sometimes **do not lane it**. A backlog whose own discipline is serial — where each
piece must land before the next is even ready — is not made faster by drawing lanes on it; lanes there invent a
concurrency the work does not have. Recommending serial execution, or one lane, is a real answer, not a failure
to find parallelism. Lane only what genuinely has disjoint territory and no ordering dependency between the
lanes. Once a split stands, it is legible at a glance: the portfolio shows per-lane standing (what is in flight
on each lane, what has settled, any cross-lane merge-order risk) and the program's own view shows the complete
picture; neither ranks the lanes or tells anyone what to do next — they disclose, they do not dispatch.

### 5. End the program honestly

A program does not complete itself. Every child landing is not the same as the objective being met, and the engine
will not derive completion from a full chain — completion is a judgment someone records deliberately, reopenable
if it turns out premature. Two failure modes to avoid:

- **Reading a finished chain as a finished program.** When every authored child has landed but the objective is
  not yet recorded complete, that is exactly what the surfaces say — unwritten successors are unknown, not done.
  If more work is needed, author the next child; if it is truly done, record it.
- **Dropping an obligation by saying nothing.** A carried obligation with no successor left to answer it is
  released only with a stated reason — the reason is the whole price of letting it go.
- **Dropping a recorded intent silently.** An unbuilt recorded intent is still a step the program decided on, and
  closure cannot drop it without a record: `program complete` refuses while unbuilt intents remain, naming
  `program intend withdraw --reason` as the way through; `program retire` and `program abandon` accept unbuilt
  intents and record what was never built in the closure itself, so the record says what happened to the intended
  steps.

Completing, retiring, abandoning and reopening are the operator's recorded acts. Surface the state and let them
make the call; do not close a program on your own initiative.

### 6. The reading surfaces

- **The portfolio** — every open program at a glance: what each is for, how far along it is as facts (not a
  percentage), what is in flight, where a split stands per-lane standing, and the next intended step (when the
  program has recorded intents) — the grouped view the flat plan list cannot give.
- **The program's own view** — one program in full: its children in chain order, what each owes, the complete
  lane standing, the next intended steps (if any are ready and unclaimed), the history of how the order and
  objective were revised, and any revisions to the intended steps with their reasons.
- **The generated file at rest** — each program keeps a generated document in its own folder, the way a plan
  keeps one. It states the moment it was generated and holds whatever it last rendered — content and
  presentation alike — so it can lag a child changed outside a program verb, or show an older rendering than the
  live views, until the next verb or the regeneration sweep; its timestamp is the truth about its freshness.

### Come in through the right door, and ask the tool for the rest

The program tool is the authority on sequence; this page does not restate its order. Reach it for the mechanics
— starting a program, appending or inserting a child, recording or withdrawing a lane split, reading the
portfolio or one program, closing or reopening — and trust what it refuses: a refusal names the way forward, and
re-deriving its order here would only give a second answer free to drift.

## Done when

The work's shape is settled honestly: either it is a program, with its order recorded, its next child authored
when its predecessor is ready, its obligations carried or released with reasons, and its lanes (if any)
reflecting real disjoint territory — or it is not a program, and it stays as ordinary plans. Concluding that
multi-PR work does *not* need a program is a finished outcome too.

## Notes

The failure this runbook most guards against is **manufacturing order that isn't there** — drawing lanes on serial
work, splitting one PR's change into a ceremonial program, or reading a merged chain as a met objective. The
machinery makes real order legible; it cannot make invented order true, and a program recording a shape the work
does not have is worse than no program at all.
