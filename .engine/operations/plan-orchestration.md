---
title: Plan orchestration — the judgment upstream of a Build
---

## Purpose

Every Build begins as a plan authored, reviewed and sealed through the Project Manager. That tool owns the
sequence — it shows the one next move, names what still blocks a seal, and refuses an out-of-order move while
naming the way forward. What it cannot supply is judgment: whether the issue in front of you is the real
problem, what a good fix would even look like, and when to stop and ask the operator rather than push on.
This runbook is that half. Enter it when agreed work needs to become a plan a later Build can pick up, or
when a plan already on the shelf is being picked back up.

## Steps

### 1. Ground yourself before you have an opinion

These are your obligations, not a form to fill. Answer them; do not recite them.

- What does the issue actually say, read whole — its body, its comments, the history it grew out of, and any
  earlier attempt at it? A plan built from a title is a plan built from a guess.
- What has this project already decided in this area? Recall its own memory before asserting anything about
  where it stands, so a settled decision is not quietly reopened.
- What does the change actually touch? Ask the wiring map what the affected parts belong to, what depends on
  them, and what checks govern them. An impact check is cheap now and expensive after a seal.
- What is unclear enough that guessing at it would waste the Build?

### 2. Judge what the issue really is

- Is the issue as stated the real problem, or a symptom of a larger one? If it reads as a symptom, say so
  plainly before authoring, and let the operator choose which problem gets planned.
- Does it imply problems with how someone uses this — interactions you cannot grasp from the inside? Then
  you need the operator's experience of it before you understand the fix at all.
- What risks does a fix carry, and do those risks warrant discussion rather than a line in the plan?
- What does a successful fix look like, concretely — and does the operator agree with that vision? If you
  cannot state the success condition in a sentence they would recognize, you are not ready to author.

Planning is deliberative, not form filling. Before you commit to a shape, put the strongest case you can
build AGAINST it: what a smaller change would achieve, what no change at all would cost, how this is most
likely to fail, and — the one easiest to miss — whether the plan quietly turns your uncertainty into
certainty by writing a guess down as a fact.

### 3. Ask with suggested answers, never with a bare need

When an answer is genuinely the operator's to give, ask for it — and carry the work of the question. Offer
concrete options, name the one you recommend and why, and state the trade-off each carries. "How do you want
this fixed?" with candidate fixes beside it is a question; "this needs a decision about how it is fixed"
hands the thinking back and calls it consultation. Use the platform's question facility where one exists,
and otherwise ask plainly in the message.

### 4. Scope the dialogue to the issue's realities

A small issue earns trivially small answers and a small dialogue. The protocol above is judgment to
exercise, never a checklist to recite: when the grounding questions answer themselves in a sentence each,
they are answered, and the right dialogue is one exchange or none. Walking an operator through five
ceremonial questions on a one-line fix is the same failure as skipping them on a change that deserved them.

### 5. Work with the intent you actually have

Most planning starts from an issue, and when it does, that issue is what you ground in. Two other cases are
ordinary and should not be forced into the first:

- **Attended planning may ground in the operator's direct intent.** When they are here and telling you what
  they want, that is the source, recorded as such — no issue is needed to make the plan legitimate.
- **Unattended work needs an authorizing issue.** Nothing runs unattended on intent nobody can point at
  afterwards. If there is no issue and no operator, there is no plan to author yet.

Never file an intake issue for work already in flight. Intake is how work arrives; a plan and its pull
request are how in-flight work is carried, and an issue opened alongside them records nothing that is not
already recorded.

### 6. Move through the operator's stops, in order

- **Discuss the shape first.** Before anything is authored into the library, the operator hears what you
  understand the problem to be and what shape a fix would take, and gets to redirect it.
- **Show the drafted plan with no ask attached.** Present it in full — which means handing over the link to
  its `PLAN.md` projection, and after a revision the link to the updated head — invite questions, take
  revisions, and stop there. The depth choice never rides along with the plan's first showing: a plan and an
  approval menu delivered in one breath is not a stop, it is a formality wearing one.
- **Then, once they are satisfied, the approval.** Offer only the review depths worth offering for this
  repository's installed reviewers — no reviewer installed is a disclosed no-extra-review result, never a
  false green — and fill `.engine/templates/risk-assessment.md` in plain language. Follow that template
  rather than paraphrasing it: it carries the rules this stop turns on, including that a care level follows
  the risk and is never lowered to a depth the operator preferred earlier, that no time or cost is invented,
  and how a weakened guardrail must be worded. Take the operator's approval in their own words. That single
  choice covers both the plan's cold review and the Build's later one; consent is given once, here.
- **One cold review, then its findings.** A plan gets exactly one cold panel. Adjudicate it yourself:
  accepting a concern is not accepting its remedy, and severity is advice that never selects a remedy for
  you. Synthesize the panel into one recommended call rather than relaying raw reviewer output, and return
  to the operator where it changes design, authority, or the agreed capability boundary. Then show them the
  outcome — what was found, and what you did about each one — before the plan locks.
- **Seal, then hand back.** The seal is terminal: it freezes the plan, its review, and its dispositions
  together, because the pull request publishes them as they stood. Hand back before the Build starts (below).

### 7. Come in through the right door, and ask the tool for the rest

Four doors reach this runbook:

- **Start a new plan** — `init` — when agreed work needs a plan a later Build can pick up.
- **Pick one back up** — `resume` — when a plan already in the library is being carried on.
- **See the shelf** — `list` — when the question is what is waiting. The shelf is a flat list of
  plans; when the question is really about a multi-PR *program* — where one stands, how its children
  group, how it splits into lanes — that is the Program Manager's grouped view, reached through
  [program orchestration](program-orchestration.md) (`engine-manage-programs`), not this flat list.
- **A plan that arrived on its own.** A plan the operator accepted in the platform's own plan mode lands
  through the intake adapter (`import-native` does the same by hand) as an unapproved draft whose judgment
  is deliberately left blank — no interpretation, no evidence, no risks, no review strategy. Filling that
  blank is exactly the protocol above, and an arrived plan has had none of it. Treat it as raw intent that
  happens to be well organized, not as a plan that is nearly done.

Beyond those doors, ask the tool rather than this page for anything sequential, and trust what it refuses.
Restating its order here would only give you a second answer, free to drift from the one the tool enforces.

### 8. Know the seams

- **Plans live on this workstation.** The library is local and never committed, so no plan is published to
  GitHub — no promotion step, no plan block in an issue or a pull request. An issue may authorize work; it
  is not the plan and never carries one.
- **The shelf has its own moments**, and they need no ceremony: seeing what is waiting, picking one back up,
  retiring a plan whose work is done, abandoning one that is not going to happen.
- **The seal hands back before the Build starts.** Sealing and building are different jobs that often want
  different settings. So at the seal: settle into the record anything that still lives only in the
  conversation; judge the Build ahead and suggest a model and effort for the harness in use — how they manage
  their context is theirs, on every runtime, so prescribe no runtime control; and tell them, in their own
  runtime's spelling, that the Build begins only when they type the engine-start command. That typed start
  enters the Build stance; the bind's recorded decision is then their consent to this Build, in that order.
  This is an offer, not a gate; nothing mechanical checks it.
- **The Build is downstream and owns itself.** Binding the sealed plan to a draft pull request, and
  everything after it, belongs to [Build orchestration](build-orchestration.md).

## Done when

The operator has discussed the shape, seen the drafted plan and had the chance to revise it, chosen its
review depth in their own words, seen what the cold panel found and how each finding was answered, and
sealed it — and the sealed plan sits in the library ready for a Build to bind. Concluding that this is not
the change to make is also a finished outcome: nothing is authored, and the operator knows why.

## Notes

Two ways this goes wrong, both observed:

- **Guidance parroted instead of exercised.** A session asked what to do next answered "the next step is
  preview" — repeatedly, correctly, and uselessly. The tool's mechanics were never the missing piece; the
  reasoning about the issue was.
- **Ceremony collapsed into one turn.** A session delivered a freshly minted plan and a depth-approval menu
  in the same breath, with no discussion of shape and no invitation to revise. Every gate was technically
  offered and none of them was a stop.

Both are the same failure: treating the lifecycle as a sequence to complete rather than a conversation to
have. The mechanics cannot catch it — a rushed plan and a considered one reach the seal by identical moves.
