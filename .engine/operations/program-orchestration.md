---
title: Program orchestration — the judgment upstream of a multi-PR program
---

## Purpose

Some work is too large for one pull request and has an *order* that matters: a capability delivered across several
PRs, a backlog cleared in a planned sequence, a migration whose steps depend on each other. A **program** is how
the engine holds that order — a durable record of the multi-PR shape, authored and read through the Program
Manager (`program_manager.py`), order authority beside the Project Manager, which still owns every individual
plan; the program records only how those plans relate and in what order they land.

This runbook is the judgment half. The tool owns the sequence — it shows the next move, names what a step
requires, and refuses an out-of-order one while naming the way forward — so ask *it* for anything mechanical.
This page carries what the tool cannot: whether work is a program at all, how its children get shaped, when the
order should be re-decided, and when only the operator's own hand should move next. Enter it when multi-PR work
needs its order held, when an operator asks where a program stands, or when a backlog needs a planned sequence.

## Steps

### 1. Decide whether this is a program at all

A program is a deliberate act, never inferred from resemblance. Two plans that happen to touch the same area are
not a program; three PRs that only make sense delivered in a particular order, carrying obligations forward from
one to the next, are. Ask: does the work genuinely span more than one PR, or is it one change split for comfort
(one PR's worth belongs in a plan)? Does the *order* carry meaning — a later PR depending on an earlier one, or
inheriting an obligation it must answer for (independent pieces are separate plans)? Is there a single objective
the whole sequence serves (unrelated debt listed together is not a program)? If no, say so and stay with ordinary
plans: a program manufactured around work that does not need one adds ceremony the operator carries forever.

### 2. Record the intended order up front; author children just-in-time

When a program starts, record what has actually been DECIDED about the steps and which follows which, with the
reason on each edge. The intended order is declared precedence recorded up front — never derived by the engine and
never a total order. Dependency is one reason among several an edge can carry: an evidence gate (reproduce the
failure before fixing it), a risk sequencing (the riskier slice first, to learn), a merge-order constraint. Two
steps with no decided precedence are left unordered rather than ranked; the tool does not and will not rank them.

A program is a chain of plans, each still authored, reviewed and sealed through the Project Manager and
[plan orchestration](plan-orchestration.md); the program adds no second planning path. A session reads the
recorded intended order BEFORE authoring the next child, the way it already reads what the predecessor owes, and
authors the *next* child when its predecessor's shape is settled enough to build against, not the whole chain up
front: a plan written against a guess about a later one bakes that guess in and resists re-deciding.

Each child carries its linkage — its program, the plan it succeeds, any obligation handed forward, and whether it
fulfils a recorded intent, stands outside the intended order, or was authorized to jump ahead. The next child
either claims an intent (out of order with a reason if the precedence graph demands it), records that it stands
outside the intended order, or is refused until one of those doors is passed. A carried obligation is the one
thing the program refuses to let drop silently: the next child must answer it — met, still carried, or released
with a reason — and the tool enforces that at the seam, so read what the predecessor owes before authoring.

### 3. Re-decide the order as evidence arrives

The chain records a decision, and a decision can be revisited. The intended order is never sealed but always
recorded, so each revision carries a reason into history. When evidence shows an unbuilt intent is wrong,
`program intend revise` replaces its title, statement or declared precedence, and `program intend withdraw` marks
it withdrawn while keeping it visible. When a built child must be replaced, `program supersede` keeps it and its
place visible, and a claim it held on an intent passes to the replacement, recorded in history. When a build
teaches you the order was wrong, change the record rather than working around it: record or revise an intent,
insert a plan before an existing child, or supersede one. The only order that cannot be re-decided is one history
has already merged; ask it which verb fits, and trust it to refuse the ones that would rewrite merged history.
What never happens here is dispatch: the program records and recommends order; it never selects, starts, or
advances a child, and which plan a session works, one at a time, is the operator's call.

### 4. Lanes: the operator's concurrency, made visible — including when not to lane

A repository can carry more than one line of work at once, and a program can record a **lane split**: a decision
that certain children may ride concurrently because they touch disjoint territory. The engine may *propose* a
split from what each child's plan would touch, but the split itself is the operator's decision — recorded,
revisable, withdrawable — never a schedule the engine runs. The lived shape when an operator asks how to clear a
backlog in parallel: a handful of lanes drawn by **file territory** (so two lanes developed at once will not
collide in the same files), one session driving each lane, dropping back to serial once the concurrency stops
paying — and any merge-order constraint *across* lanes stated up front rather than discovered at rebase time.

And the honest answer is sometimes **do not lane it**. A backlog whose own discipline is serial — where each
piece must land before the next is even ready — is not made faster by drawing lanes on it; lanes there invent a
concurrency the work does not have. Recommending serial execution, or one lane, is a real answer, not a failure
to find parallelism. Once a split stands, the portfolio shows per-lane standing and the program's own view the
complete picture; neither ranks the lanes or says what to do next — they disclose, they do not dispatch.

### 5. End the program honestly

A program does not complete itself: every child landing is not the objective being met, and the engine will not
derive completion from a full chain — it is a judgment someone records deliberately, reopenable if premature.

- **Reading a finished chain as a finished program.** When every authored child has landed but the objective is
  not recorded complete, that is what the surfaces say — unwritten successors are unknown, not done. If more work
  is needed, author the next child; if it is truly done, record it.
- **Dropping an obligation by saying nothing.** A carried obligation with no successor left to answer it is
  released only with a stated reason, the whole price of letting it go.
- **Dropping a recorded intent silently.** An unbuilt intent is still a step the program decided on: `program
  complete` refuses while any remain, naming `program intend withdraw --reason` as the way through; `program
  retire` and `program abandon` accept them and record in the closure itself what was never built.

Completing, retiring, abandoning and reopening are the operator's recorded acts: surface the state, let them
make the call, and never close a program on your own initiative.

### 6. The reading surfaces

- **The portfolio** — every open program at a glance: what each is for, how far along it is as facts (not a
  percentage), what is in flight, per-lane standing, and the next intended step — what a flat plan list cannot give.
- **The program's own view** — one program in full: its children in chain order, what each owes, the complete
  lane standing, the intended steps ready and unclaimed, and the history of every revision, with reasons.
- **The generated file at rest** — a generated document in the program's own folder, the way a plan keeps one.
  It holds whatever it last rendered, so it can lag a child changed outside a program verb until the next verb or
  the regeneration sweep; its timestamp is the truth about its freshness.

## Done when

The work's shape is settled honestly: either it is a program, with its order recorded, its next child authored
when its predecessor is ready, its obligations carried or released with reasons, and its lanes (if any) reflecting
real disjoint territory — or it is not, and it stays as ordinary plans, which is a finished outcome too.

## Notes

The failure this runbook most guards against is **manufacturing order that isn't there** — drawing lanes on serial
work, splitting one PR's change into a ceremonial program, or reading a merged chain as a met objective. The
machinery makes real order legible; it cannot make invented order true, and a program recording a shape the work
does not have is worse than no program at all.

The program tool is the authority on sequence; this page does not restate its order. Reach it for every mechanic
and trust what it refuses: a refusal names the way forward, and restating its order here would only drift.
