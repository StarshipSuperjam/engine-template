---
title: Contribute to a project you don't own — open a clean pull request to an upstream from your fork
---

## Purpose

How the engine sends a change to a project you **do not own** — an open-source project you've forked, or (the
special case) engine-template itself, which the engine-mechanic contributes to from a **separate checkout** — as
a pull request, carrying only the project's files and never the engine's own. Enter this when you have work you
want to offer upstream. The engine does the mechanical git a non-engineer shouldn't have to (cutting a clean
branch, comparing against the upstream, matching the project's pull-request form); **opening the pull request is
always your call**, and at no point are you dropped into a raw git conflict — anything that needs a choice
becomes a plain question. The tool is `tools/external_contribution/submit.py`. **Heads-up:** the final
open-the-pull-request step has not yet been exercised against a live project — see Notes.

## Steps

1. **Confirm the setup.** Two shapes fit here, and both reach the same steps below:
   - *Contributing to a project you forked (the usual case):* you've forked the upstream project and the engine
     is installed in your fork (an ordinary brownfield install). You own the fork; the upstream you only
     contribute *to*.
   - *Building engine-template itself (the engine-mechanic):* your workspace is a **separate copy** of
     engine-template that you contribute to — the engine is not installed *into* it (that would put the engine
     inside a repo that already is the engine). It only ever contributes up to the template; the template
     never depends on it.

   If neither fits — if this is a repo you own and control outright — stop here; this runbook is only for
   contributing to a project you don't own.
2. **Cut an engine-clean branch from the upstream's default.** The engine creates the feature branch from the
   upstream's default branch, which carries no engine files — so the branch is clean of the engine by origin.
   The engine's own memory and knowledge stay on your fork's main, never on this branch. This step also brings
   the upstream's latest down locally, which the clean-check later compares against; if that hasn't happened
   (the project was never reached), the engine can't check what your change would carry, so it holds and tells
   you plainly rather than opening a pull request on an unchecked change.
3. **Make the change as ordinary commits.** The engine authors the product change on that branch. Nothing of
   the engine's own machinery is committed to it.
4. **Check the contribution is clean.** The engine compares the whole outgoing change against the upstream's
   default and runs the leaked-engine-files check. **If it finds any of the engine's own files, it pauses and
   shows you which ones** — most likely slipped in by accident — and names exactly which to take off the branch
   (your fork keeps its copy — nothing is lost). Clearing them is the clean fix; but this is a decision, not a
   wall — if you're sure, you can tell the engine to go ahead and open it as is. It flags by file name, so if
   the upstream project happens to keep its own file with an engine-like name (its own `CLAUDE.md`, say),
   you'll see that flagged too — clearing it or telling the engine to proceed is your call. A clean
   contribution passes silently.
   - **Contributing back to the engine's own home** (the special case above — the engine-mechanic building
     engine-template, or a fork escalating an engine fix): here the engine's own code *is* the contribution, so
     it's allowed to travel — the check only flags what belongs to *this* copy and no one else: your saved
     memory and state, your project's own settings and identity, your private tuning, and your own decision
     records. Those never ride into the shared template, but the engine's code and its regenerated maps do.
5. **Have the change looked over before you submit.** For a change to the engine's *own* project, the engine
   runs a second, independent review that hunts for mistakes the tests can miss. That review does **not** run
   by itself on a contribution to another project — so for anything beyond a trivial change, ask the engine to
   run it before you send this (it uses the same review it runs on its own work). If it isn't run, the engine
   says so plainly — on the prepared pull request and in a note in the pull-request text — rather than letting
   the change look as reviewed as any other; **that note is a backstop, not a substitute for the review**.
6. **Review the prepared pull request.** The engine assembles the pull-request text to the **project's own
   template** when it has one (a contributor follows the host's conventions), or a plain fallback shape when
   it doesn't. For a project whose template has sections to fill and a check that the pull-request text is
   complete — engine-template itself is one — the engine **writes those sections in full** (the same way it
   authors the text for a change to this project) and opens *that*, rather than a template with the prompts
   left blank. If the sections aren't written and the project is the engine's own home, the engine **won't
   open it** — an unfilled body would just come back red on the home's own checks — and tells you plainly it
   needs the text authored first; for any other project it opens on your go-ahead but says plainly that the
   sections aren't filled, so you can have it complete them before you send. It shows you what it will open —
   the title, the text, which branch goes where, **and the branch it measured your change against** (the
   project's own default). That comparison is what the clean-check rests on, so if the branch it names isn't
   the project you're contributing to, say so before it opens — it's the one thing to glance at here.
7. **Authorize the submission — your call.** The engine opens the pull request **only on your go-ahead**;
   without it, the prepared request just waits. When it opens, it tells you plainly that *submitting is not
   the same as being accepted* — the project's maintainers decide, it may take a while or be declined, and
   either way your fork keeps the work.

## Done when

The engine reports the pull request is **open** and prints its link — or, if you haven't authorized it yet,
that it is **prepared and waiting** for your go-ahead, its text either complete or plainly flagged as still
needing sections filled — or, if the pull-request text isn't authored for a contribution to the engine's own
home, that it is **held until that text is written** (with the engine offering to write it) — or, if the
upstream couldn't be reached, that it is **drafted and safe on your fork** for you to file later. In every
case the work is committed on your own fork, and **no step has left you at a raw git conflict**: anything the
engine couldn't resolve on its own was put to you as a plain "I need a decision from you" question.

## Notes

- **Submitted is not accepted.** The maintainers decide whether your change lands. A decline still leaves you
  a working fork you can use, revise, and resubmit. If the project does no review at all, your own checks
  before submitting are the only real gate — the engine won't dress an unreviewed merge up as a review.
- **Checking where a submission stands.** Whenever you want to know, just ask the engine — it checks the pull
  request's live state and tells you plainly whether it's still an open proposal, landed, or was declined (and
  if it can't reach the project right then, it says so rather than guess). There's no separate tracker running
  in the background; you ask, it looks, it answers.
- **The engine only proposes.** It opens a pull request and nothing more — it never changes the upstream's
  settings, never merges for the maintainers, and the upstream never depends on your engine.
- **If the upstream is unreachable,** nothing is lost: the work is committed on your fork, and the engine
  drafts the submission so it can be filed once the project is reachable (or you can open it yourself with
  your own `gh`).
- **Not yet exercised end to end.** Every part of this except the final open-the-pull-request step is tested
  offline; the live `gh pr create` runs for the first time when you make a real submission. Treat your first
  contribution as the shake-out of that last step.
