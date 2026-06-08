---
title: First-run setup — stand up a brand-new project
---

## Purpose

Set up a brand-new project made from this template: gather the few choices only the operator can make, save
them, then install those choices and turn on the review gate that makes the engine safe to trust. Enter this
whenever the operator types `/engine-setup` or asks to set the project up for the first time. It runs once;
afterwards the command has nothing to do. The end state: the operator's choices are saved, their selected
add-ons are in place, the review gate is on, and setup has tidied up after itself.

## Steps

1. **Check it's a new project.** Run `uv run --directory .engine -- python tools/instantiator.py show`. If it
   reports the project is already set up, stop and tell the operator — first-time setup only runs once, on a
   brand-new project.
2. **Present the choices.** That same command prints the project's details and the two choices to make: who
   reviews changes here (on their own — the usual choice; or with a team), and which optional add-ons to
   include or leave out (grouped by what they help with). Show these to the operator in plain words.
3. **Take the operator's answers** — their reviewer choice, and which optional add-ons to keep.
4. **State plainly what confirming does, then confirm.** Before saving, tell the operator: any optional add-on
   they did not keep will be removed from the project — its files are deleted, not just switched off — and
   adding one back later is a fresh request, not a checkbox they flip back. On their go-ahead, save their
   choices. Before this point nothing is changed; saving is the step the rest of setup builds on.
5. **Install the choices and turn on the review gate.** With the choices saved, the engine carries them out:
   it removes the add-ons that were not kept, sets up its own private tool area, turns on the branch review
   gate that makes every change go through approval, checks everything is consistent, and then removes its own
   setup files so it does not run again. If turning on the review gate needs a one-time approval from the
   operator, it asks for that in plain words and explains why.

## Done when

The operator's choices are saved and the engine has gone on to install them and turn on the review gate — or
has clearly told the operator, in plain words, what one step is left (for example, a one-time approval to turn
on the review gate). On a project that was already set up, the command reported so and nothing changed.

## Notes

Setup runs only in a brand-new project, never in the workshop where the engine itself is built. The operator's
choices are saved as the record the engine reads as it sets things up, so if setup is interrupted after the
choices are saved, the next session picks up from there rather than asking again. Who reviews changes can be
changed later, and optional add-ons can be added or removed later too — each is a separate, deliberate request.
