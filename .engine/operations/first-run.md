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

1. **Check it's a new project.** Run `python3 .engine/tools/instantiator.py show`. If it reports the project is
   already set up, stop and tell the operator — first-time setup only runs once, on a brand-new project.
2. **Present the choices.** That same command prints the project's details and the two choices to make: who
   reviews changes here (on their own — the usual choice; or with a team), and which optional add-ons to
   include or leave out (grouped by what they help with). Show these to the operator in plain words.
3. **Take the operator's answers** — their reviewer choice, and which optional add-ons to keep.
4. **State plainly what confirming does, then confirm.** Before saving, tell the operator: any optional add-on
   they did not keep will be removed from the project — its files are deleted, not just switched off — and
   adding one back later is a fresh request, not a checkbox they flip back. On their go-ahead, save their
   choices: run `python3 .engine/tools/instantiator.py confirm` with their reviewer choice, the add-ons they
   kept, and their account name (for example `confirm --tier solo --keep "" --handle their-account`). Before
   this point nothing is changed; saving is the step the rest of setup builds on.
5. **Install the choices and turn on the review gate.** With the choices saved, run
   `python3 .engine/tools/instantiator.py apply`. In order, the engine: removes the add-ons that were not
   kept (their files are deleted); sets who reviews changes to the engine's own files; turns on the safer
   planning default for this project (or, if the operator already has their own editing default, offers to —
   and leaves theirs alone if they decline); **sets up the engine's own programs in a private project folder —
   asking the operator's one-time go-ahead first, because this downloads software onto their machine**; switches
   the engine on; and turns on the branch review gate that makes every change go through approval (which may ask
   for a one-time GitHub approval, explained in plain words first). Show the operator the plain-language result
   of each step. If the engine's programs can't be set up (for example, no internet), setup **stops safely at
   that point and never falls back to a different setup** — say so, and run `apply` again later to resume from
   where it left off. When the steps are done, show a plain summary of what was set up and anything still left
   for the operator (for example, finishing the review gate later). The closing checks and tidy-up that end
   setup are the final part of the flow.

## Done when

The operator's choices are saved and the engine has gone on to install them and turn on the review gate — or
has clearly told the operator, in plain words, what one step is left (for example, a one-time approval to turn
on the review gate). On a project that was already set up, the command reported so and nothing changed.

## Notes

Setup runs only in a brand-new project, never in the workshop where the engine itself is built. The operator's
choices are saved as the record the engine reads as it sets things up, so if setup is interrupted after the
choices are saved, the next session picks up from there rather than asking again. Who reviews changes can be
changed later, and optional add-ons can be added or removed later too — each is a separate, deliberate request.

Setup is launched with plain `python3`, not the engine's own tool runner, because it is the one step that runs
*before* it installs that runner — so it cannot depend on it. Every other engine command runs through the
installed runner; this one alone runs on the system's Python, and only until setup has installed the runner.
