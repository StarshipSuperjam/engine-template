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
2. **Orient the new operator, then present the choices.** That same command opens with a plain-language
   welcome to **what's already running** — the always-present essentials (memory, state, knowledge, attention,
   the review gate, Explore/Build, the boot briefing, unattended routines, periodic self-review), described,
   never offered as a choice. Then it prints the project's details and the choices to make: who reviews changes
   here (on their own, the usual choice, or with a team), and which optional add-ons to include or leave out
   (grouped by what they help with; each can be added or removed later). It also confirms **what this engine
   builds** — usually the very repo it's set up in; if it exists to work on a *different* project (a fork it
   contributes to, a template it maintains), it asks which. Show all of this to the operator in plain words.
3. **Take the operator's answers** — their reviewer choice, which optional add-ons to keep, which of the
   add-ons marked *included unless you say otherwise* they want left out, and — only if this engine works on a
   project *different* from the repo it's set up in — which project that is (its owner/name).
4. **State plainly what confirming does, then confirm.** Before saving, tell the operator: any optional add-on
   they did not keep will be removed from the project — its files are deleted, not just switched off — and
   adding one back later is a fresh request, not a checkbox they flip back. On their go-ahead, save their
   choices: run `python3 .engine/tools/instantiator.py confirm` with their reviewer choice, the add-ons they
   kept, any they turned down, and their account name (for example `confirm --tier solo --keep "" --handle
   their-account`). An add-on marked *included unless you say otherwise* needs no `--keep`; it is left out
   only by naming it in `--decline`, so pass `--decline` exactly when the operator asked for one to be left out.
   If they named a *different* project for this engine to build, pass `--product-repository owner/name`; omit
   it for the usual self-building case. Before this point nothing is changed; saving is what the rest builds on.
5. **Install the choices and turn on the review gate.** With the choices saved, run
   `python3 .engine/tools/instantiator.py apply --first-run`. In order, the engine: removes the add-ons that were not
   kept (their files are deleted); places its own ignore rules in the project's `.gitignore` (its private tool
   folder and caches, never committed); sets who reviews changes to the engine's own files;
   **sets up the engine's own programs in a private project folder —
   asking the operator's one-time go-ahead first, because this downloads software onto their machine**; seeds the
   operator's starting codes of conduct from the project's seed and tells them, plainly, that the stance is
   present and theirs to tune; resets the project's starting place-marker to a clean slate so a new project
   never inherits the template's own focus, open-work count, or issue list (disclosed in plain language, and
   left untouched once the project has set its own); seeds a starting version file for the project's OWN
   releases (`product-version.json`, at `0.0.0`) so once deployed the release workflow cuts the project's
   product release, not the engine's — publishing a real release still needs the one-time `RELEASE_PAT` the
   release workflow explains; switches the engine on; turns on the branch review gate that makes every
   change go through approval (which may ask for a one-time GitHub approval, explained first); tells the
   operator about GitHub's one-time Actions switch, which only they can flip; turns on GitHub's native
   security features; and turns on the working-comfort repository settings. Show the plain-language result
   of each step. If the engine's programs can't be set up (no internet, say), setup **stops
   safely there and never falls back to a different setup** — say so, and run `apply --first-run` again later to
   resume. When the steps are done, show a plain summary of what was set up and anything still left.
6. **Check it all fits together — and pause if not.** With the steps done, run
   `python3 .engine/tools/instantiator.py verify --first-run`, which confirms the installed engine is consistent
   and states whether the review gate is on. If something doesn't line up, setup **pauses** and tells the
   operator plainly what's wrong and the two ways forward — fix it and run setup again (it resumes from here,
   losing none of their choices), or stop and report it. The engine never carries on with an inconsistent setup.
7. **Tidy up the one-time setup files.** Run `python3 .engine/tools/instantiator.py retire --first-run`. Once the
   check is clean, this removes the files that exist only for first-time setup — the walkthrough, its notes, the
   setup tool itself — while everything the project needs to keep running stays, choices included. (If the check
   still finds a problem, it refuses and changes nothing.) Retire reports setup **applied**, not complete: the
   transformation is still uncommitted in the working tree, so it names the one step left — landing it through
   review — and drops a private local marker so the engine can confirm completion once it lands (step 8).
8. **Land the setup through review.** The transformation lives only in the working tree; make it durable the way
   every later change is — through the reviewed path, never a direct commit to the protected default. Put the
   whole transformation on a branch, commit it, and open a pull request into the default branch, titled with a
   release-notes kind (e.g. `Feature: stand up this project on the Engine`). **Fill the pull-request body from
   `.github/pull_request_template.md`** — every section, in plain language — because the project ships with the
   engine's own checks and an incomplete body fails a hard merge check a re-run can't clear. This first pull
   request *is* their setup; nothing is durable until they review and merge it. After the merge, bring the local
   default branch clean and current on the merged commit (fast-forward, or the engine's catch-up on consent). At
   the **next** session start the engine confirms **Setup is now complete** on its own — once. (The operator may
   land it themselves if they prefer.)
9. **Nothing to do to activate — but say where it stands if asked.** Qualifying this clone's Engine code to
   write canonical memory is ambient: every session start attempts it, bounded and without a prompt, and it
   succeeds once the setup pull request is merged and the local default branch is clean and current on that
   merge. An unqualified clone is not a broken one — memory reads and health work throughout; only writing
   waits. If the operator asks, `uv run --directory .engine --frozen -- python tools/accepted_hook_dispatch.py
   ensure --root .. --ambient` reports the current state and what, if anything, is holding it back.
10. **Offer to back up the project's memory.** The engine can keep a private, off-computer copy of the notes it
   saves about this project — the decisions, lessons, and plans it remembers (never the operator's code or work) —
   so a copy is safe if this machine is lost. Get plain-language consent **before anything is created**: run
   `uv run --directory .engine -- python tools/memory/backup_vault.py disclosure` and show the operator, in its
   own words, the shared-vs-separate choice — one shared backup for every engine project (simplest) or one just
   for this project — and the trade-off (one accidental flip to public would expose every project at once). Take
   their answer; then run the same command with `--scope shared` (or `--scope per-project`) to show exactly which
   private repository will be created and that it must stay private. **Only on a clear yes**, run
   `uv run --directory .engine --frozen -- python tools/accepted_hook_dispatch.py attended --root .. --script .engine/tools/memory/backup_vault.py --operation attended-backup-setup -- setup --scope <their choice> --consent y` to
   create it. If they decline, or there is no GitHub access yet, nothing is created — say so plainly, and note
   they can set a backup up later by asking. Memory is never backed up to a destination they weren't shown.
11. **Turn on the engine's live helpers.** The engine ships two live helpers — its saved-memory recall and its
   wiring-map (the `engine-memory` and `engine-knowledge-graph` servers, defined in the project's `.mcp.json`).
   Until they are switched on, the engine runs on its **committed-file fallback**: fully functional, but recall
   and the wiring map read from saved files, not the live version. Walk the operator through switching them
   on: **approve the engine's memory and knowledge servers when their Claude app prompts them (or in its
   MCP settings), then fully quit and reopen Claude** — they only come online after a restart. The engine
   surfaces the same notice at any session start while a helper is off (`boot.py` `MCP_AVAILABILITY_CHECK`), so
   this can wait; the restart ends this session, which is why it is the last step.

## Done when

The operator's choices are saved, the engine installed them and turned on the review gate, the consistency check
passed, **the transformation was landed durably through review — committed on a branch, merged into the default
via a pull request, and the local checkout brought clean and current on that merged commit** (so the folder is
never left dirty-and-stranded, the state that stranded an early adopter), the operator was offered a memory
backup and their choice was honored, and the one-time setup files were tidied away. Completion is **two-staged**:
retire reports setup *applied* and names the landing step, and the engine confirms *Setup is now complete* on its
own — once — at the first start after the change has landed and the checkout is durable. Short of that, setup has
told the operator plainly what one step is left (landing the setup pull request, a one-time approval to turn on
the review gate, setting up a backup later, turning on the live helpers, or a problem to fix). On a project that
was already set up, the command reported so and nothing changed.

## Notes

Setup runs only in a brand-new project, never in the engine's own workshop. The operator's choices are saved as
the record the engine reads as it sets things up, so an interrupted setup resumes from there rather than asking
again. Who reviews changes, and which add-ons are on, can each be changed later by a separate, deliberate request.

Setup is launched with plain `python3`, not the engine's own tool runner, because it is the one step that runs
*before* it installs that runner — so it cannot depend on it. Every other engine command runs through the
installed runner; this one alone runs on the system's Python, and only until setup has installed the runner.
