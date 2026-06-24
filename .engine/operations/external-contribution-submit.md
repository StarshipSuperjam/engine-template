---
title: Contribute to a project you don't own — open a clean pull request to an upstream from your fork
---

## Purpose

How the engine sends a change to a project you **do not own** — an open-source project you've forked, or the
engine-template itself — as a pull request from your fork, carrying only the project's files and never the
engine's own. Enter this when you have work on your fork that you want to offer upstream. The engine does the
mechanical git a non-engineer shouldn't have to (cutting a clean branch, comparing against the upstream,
matching the project's pull-request form); **opening the pull request is always your call**, and at no point
are you dropped into a raw git conflict — anything that needs a choice becomes a plain question. The tool is
`tools/external_contribution/submit.py`. **Heads-up:** the final open-the-pull-request step has not yet been
exercised against a live project — see Notes.

## Steps

1. **Confirm the setup.** You've forked the upstream project and the engine is installed in your fork (an
   ordinary brownfield install). You own the fork; the upstream you only contribute *to*. If that isn't the
   case, stop here — this runbook is only for contributing to a repo you don't own.
2. **Cut an engine-clean branch from the upstream's default.** The engine creates the feature branch from the
   upstream's default branch, which carries no engine files — so the branch is clean of the engine by origin.
   The engine's own memory and knowledge stay on your fork's main, never on this branch.
3. **Make the change as ordinary commits.** The engine authors the product change on that branch. Nothing of
   the engine's own machinery is committed to it.
4. **Check the contribution is clean.** The engine compares the whole outgoing change against the upstream's
   default and runs the leaked-engine-files check. **If it finds any engine files, the engine stops before
   submitting** and names exactly which files to take off the branch (your fork keeps its copy — nothing is
   lost). Clear them, then return to this step. A clean contribution passes silently.
5. **Review the prepared pull request.** The engine assembles the pull-request text to the **project's own
   template** when it has one (a contributor follows the host's conventions), or a plain fallback shape when
   it doesn't. It shows you what it will open — the title, the text, and which branch goes where.
6. **Authorize the submission — your call.** The engine opens the pull request **only on your go-ahead**;
   without it, the prepared request just waits. When it opens, it tells you plainly that *submitting is not
   the same as being accepted* — the project's maintainers decide, it may take a while or be declined, and
   either way your fork keeps the work.

## Done when

The engine reports the pull request is **open** and prints its link — or, if you haven't authorized it yet,
that it is **prepared and waiting** for your go-ahead — or, if the upstream couldn't be reached, that it is
**drafted and safe on your fork** for you to file later. In every case the work is committed on your own fork,
and **no step has left you at a raw git conflict**: anything the engine couldn't resolve on its own was put to
you as a plain "I need a decision from you" question.

## Notes

- **Submitted is not accepted.** The maintainers decide whether your change lands. A decline still leaves you
  a working fork you can use, revise, and resubmit. If the project does no review at all, your own checks
  before submitting are the only real gate — the engine won't dress an unreviewed merge up as a review.
- **The engine only proposes.** It opens a pull request and nothing more — it never changes the upstream's
  settings, never merges for the maintainers, and the upstream never depends on your engine.
- **If the upstream is unreachable,** nothing is lost: the work is committed on your fork, and the engine
  drafts the submission so it can be filed once the project is reachable (or you can open it yourself with
  your own `gh`).
- **Not yet exercised end to end.** Every part of this except the final open-the-pull-request step is tested
  offline; the live `gh pr create` runs for the first time when you make a real submission. Treat your first
  contribution as the shake-out of that last step.
