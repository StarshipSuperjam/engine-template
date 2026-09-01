---
name: engine-manage-plans
description: Recognize a request to plan work for a later build, or to see, resume or retire waiting plans.
invocation: model-only
user-invocable: false
engine-targets:
  - kind: operation
    ref: .engine/operations/plan-orchestration.md
    availability: active
---

## Steps

1. Recognize that the operator is asking to turn agreed work into a plan a later build can pick up — "make
   this a plan for later", "turn this issue into a plan", "queue this up for a later build" — or to work the
   shelf of plans already written: what plans are waiting, picking one back up, retiring one whose work is
   done, abandoning one that is not going to happen. Name the Engine's planning procedure in
   `.engine/operations/plan-orchestration.md` as the way to run it. If the question is really about a
   multi-PR *program* — where one stands, how its children group, how a backlog splits into concurrent
   lanes — that is the sibling door `engine-manage-programs`, not this one.

2. Read that procedure before answering, because the judgment is the part that matters: how to ground in the
   issue before forming an opinion, when the thing in front of you is a symptom rather than the problem, how
   to ask the operator with real options rather than a bare need, and where their stops fall. The sequence
   itself belongs to the Project Manager, which shows the next move and refuses an out-of-order one — ask it,
   not this page.

## Notes

This route recognizes and points. It does not author a plan, approve one, or begin building. A request to
build something *now* is the other door, `engine-coordinate-build`; a plan reaches a build only when the
operator starts one.
