---
title: Routine entry — the unattended, scope-locked entry procedure
---

## Purpose

The procedure an unattended Routine session enters when a Local Desktop routine fires `/engine-routine`.
It is the drift-firewall for unattended work: it confirms the run genuinely cannot ask and is isolated
from the operator's checkout, locks onto the frozen scope-locked build Issue, advances the build by one
planned chunk inside that scope, routes anything needing a human to a GitHub Issue (because it cannot ask),
and never merges the protected branch. Enter it at the start of every routine fire; the build's actual
work follows the distributed-implement workflow in `.engine/operations/build-orchestration.md`, which this
procedure references and does not restate.

## Steps

1. **Confirm the run is non-interactive (it genuinely cannot ask).** The operator configured this routine
   with a non-interactive permission mode — set in their Claude Desktop permission settings — so it proceeds
   without prompts. This is the operator's setup, not something the engine sets here. If at any point an
   action would require an approval this run cannot give — including that mode being unavailable for the
   run — treat it as a blocker: do not wait silently, go to step 6 (file an Issue and halt).
2. **Confirm isolation from the operator's checkout.** A scheduled run does not isolate into its own
   worktree by default, so the `/engine-routine` setup has the operator enable worktree mode ("Work in an
   isolated copy of the repo"). Confirm the run is in an isolated worktree, not the operator's top-level
   checkout. If it is running in the top-level checkout, do not commit there — the never-strand-main floor
   forbids mutating the operator's checkout; go to step 6 (file an Issue naming the missing isolation and
   halt).
3. **Read git state and the frozen scope-locked build Issue, then decide which of three situations holds.**
   The build Issue holds the ordered commit-sequence checklist (the durable plan a cold session reads) and
   the permitted write-scope alongside it.
   - **GitHub is unreachable** — there is no plan to read: exit without proceeding (fail-safe), no Issue.
   - **The build is finished** — every checklist item is already done, or its pull request has been
     finalized or closed. There is nothing to do: exit cleanly (step 6) and **file no Issue — completion is
     never an alarm.** A completed build whose schedule keeps firing simply no-ops each run until the
     operator stops the routine or points it at a new build.
   - **The build cannot be found where one was expected** — the routine is pointed at a build Issue that is
     missing or does not match, *and* git shows no pull request or commits from this routine to resume (a
     genuine first-fire mis-aim, not a finished build). File a durable **misfire Issue** (step 6's helper)
     and exit — but only if an open misfire Issue for this routine is not already present, so a mis-aim
     surfaces **once**, never as a fresh Issue every fire.
4. **On the first fire, echo the build Issue this routine locked onto** — "starting the routine on #N —
   <title>" — so a mis-aimed target surfaces on the first cycle rather than after a wasted batch.
5. **Advance one chunk within the scope-lock.** Find the next unchecked checklist item and its scope;
   execute it so that every write stays inside the permitted write-scope, re-checking scope before each
   commit; add the commit(s) to the open pull request; and report progress derived from git and the
   checklist ("commit X landed — N of M planned done"). Never close or merge the pull request.
6. **Escalate anything that needs a human, because this run cannot ask.** An out-of-scope observation
   files an Issue and the run continues; a genuine blocker or a decision needing a human files an Issue and
   halts this task, leaving a plain-language status that names the next step ("stopped at N of M — I need a
   decision on X; I opened Issue #K. Answer there, then re-run the routine."). Author every such Issue —
   misfire, out-of-scope, or blocker — through the shared engine issue-authoring helper
   (`.engine/tools/issue_author.py`) so it reads like every engine-authored Issue, and **never file one
   that duplicates an Issue already open** for the same thing. A finished build, or a run with no eligible
   scope left, exits cleanly ("nothing to do") and **files no Issue**.

## Done when

The routine either advanced the build by one chunk — commits added inside the scope-lock, progress
reported, the pull request left open — or exited without proceeding. It exits **cleanly and files no
Issue** when GitHub is unreachable, or when the build is finished or has no eligible scope left (a
completed build whose schedule keeps firing simply no-ops). It leaves a durable, operator-visible Issue
only for a real anomaly — a first-fire mis-aim with nothing to resume, a blocker, or a posture it could not
satisfy — and **never a duplicate of an Issue already open**. In every outcome the protected branch was
never merged and the operator's checkout was never mutated. An interactive Finalize session later
integrates, reviews for cohesion, validates, and submits the pull request for the operator's merge.

## Notes

The scope-lock is this run **following the plan**, not a mechanical lock it cannot break — the same honest
tier as the Explore write-gate. The only unbypassable wall is the protected-branch merge, which Routine
never performs; the interactive Finalize review is the cohesion backstop. Single-flight is the Desktop
scheduler's skip-a-run-while-one-is-in-progress behavior; orphan recovery is reading git state, not a
lease — a run that dies mid-task leaves its commits (or none) and the PR open, and the next run resumes
from git and the checklist. The non-interactive permission mode and the per-task worktree toggle are
operator-side Claude Desktop settings set during `/engine-routine` setup; this procedure confirms them in
effect but never sets them.
