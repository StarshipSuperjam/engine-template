---
name: engine-manage-programs
description: Recognize shaping multi-PR work as a program, reading where a program stands, or splitting a backlog into lanes.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: operation
    ref: .engine/operations/program-orchestration.md
    availability: active
---

## Steps

1. Recognize that the operator is engaging **multi-PR program work** — the order-of-record for work
   too large for one pull request, held by the Program Manager (`program_manager.py`), beside the
   Project Manager that still owns each individual plan. The request arrives in either register of the
   repository's vocabulary, and both reach this door:

   - **The program vocabulary this component names.** "Make this a program", "add the next PR to the
     program", "where does this program stand", "how is it sliced into lanes", "supersede that child",
     "is the program done" — the direct terms now that programs have a proper name.
   - **The pre-name phrasing the same asks still arrive in.** A backlog or milestone with a
     sequencing-and-concurrency question: "what order should I clear these issues in", "which of these
     can be combined into one PR", "can these run in concurrent lanes", "give me a grouping outline so
     I can start a session per lane". These describe exactly a program and its lanes without using the
     word *program*, and they are the shape this component was built for.

   Name the Engine's program procedure in `.engine/operations/program-orchestration.md` as the way to
   run it, and the Program Manager (`program_manager.py`) as the one address for the mechanics.

2. Read that procedure before answering, because the judgment is the part that matters: whether the
   work is a program at all or just plans, how children are authored just-in-time through the Project
   Manager, when the order should be re-decided, what a lane split is for — and, just as often, when
   *not* to lane, because a backlog whose own discipline is serial is not made faster by drawing lanes
   on it. The sequence itself belongs to the tool, which shows the next move and refuses an
   out-of-order one — ask it, not this page.

## Notes

This route recognizes and points. It does not author a plan, start a build, record a program, or
advance a child — the Program Manager records and recommends order, and never dispatches work; which
plan a session actually works is the operator's call. Turning agreed work into a sealed plan is the
Project Manager's door, `engine-manage-plans`; building something now is `engine-coordinate-build`.
