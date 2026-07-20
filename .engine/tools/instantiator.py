#!/usr/bin/env python3
"""First-run setup orchestrator — the GATHER and CONFIRM half.

The instantiator stands a freshly-generated repo up: it derives the repo's coordinates, takes the
operator's one non-derivable choice (the identity tier) and their feature selection, then writes the engine
manifest as the resumability checkpoint — after which the later phases (apply, verify, retire) install the
selection and the engine's own guardrails. This file ships the **non-destructive front half**: GATHER
(present the choices) and CONFIRM (write the checkpoint). The destructive/installing APPLY phase, the VERIFY
pause, and self-RETIRE land in core slices 27b/27c.

The signal model:
- The instantiator's own presence is the "this repo is not set up yet" signal; it self-deletes at retire,
  so its absence means setup is done. Within a run, the **engine manifest** is the checkpoint — absent means
  the operator has not confirmed yet (re-offer everything), present means they have (resume the install).
  We key `is_provisioned()` off the manifest's presence; we introduce **no new state file** (the manifest is
  the checkpoint, by design).
- THE DEGENERACY (the loudest line): this tool NEVER runs in the construction repo — `engine-template` is the
  template tree, not a generated repo. Its only evidence is the fixture demonstration below,
  which runs the real GATHER/CONFIRM logic against a throwaway generated-repo fixture. "Works on the fixture
  ⇒ works for a real adopter" is an inductive step the fixture cannot discharge — named, not hidden.

Heavy steps reuse permanent primitives: `boot` for the derived coordinates, `module_coherence` for the
present modules, the shared `module_catalog` reader for the optional set. The interactive prompting lives in
the `/engine-setup` skill + the `first-run.md` runbook; this tool provides the walkthrough text, the derived
facts, and the checkpoint write.
"""
from __future__ import annotations
import contextlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402  (ROOT/ENGINE_DIR + paths)
import boot               # noqa: E402  (the derived repo coordinates)
import module_coherence   # noqa: E402  (the present modules + their versions)
import module_catalog     # noqa: E402  (the shared optional-module catalog reader)
import module_manager     # noqa: E402  (remove() — the delete-unselected reuse; derive_uv_groups)
import wiring             # noqa: E402  (render_codeowners + apply_all — the apply-phase appliers)
import knowledge_gen      # noqa: E402  (generate() — substrate re-derive)
import self_map           # noqa: E402  (generate() — the wiring map re-derives at retire, #513)
import license_seeds      # noqa: E402  (the permanent seed set + recognizer, shared with license_health)
import bootstrap          # noqa: E402  (ControlPlane + render — the control-plane bootstrap; _parse_sections)
import security_floor     # noqa: E402  (the native-scanning toggles — reuses ControlPlane's transport)
import repo_behavior      # noqa: E402  (the repository-behavior settings leg, #541 — same transport reuse)

# These sibling tools import only the Python standard library plus each other (validate binds its two
# third-party packages LAZILY), and every one carries `from __future__ import annotations`
# (so an `X | None` annotation never evaluates at import) — both are LOAD-BEARING: the apply phase below
# runs on the operator's SYSTEM python BEFORE it installs the engine's own 3.11+ tool-runtime, and
# system python on macOS is 3.9. `test_instantiator` proves the whole chain imports + starts there.

# The recognized SDLC discipline groups the optional features are presented under (recognized,
# googleable labels the operator prefers), in a fixed order, each with a plain one-line gloss.
_CATEGORY_ORDER = ("Product Management", "Software Configuration Management", "Verification & Validation")
_CATEGORY_GLOSS = {
    "Product Management": "tools for planning and tracking your work",
    "Software Configuration Management": "tools that keep your project's parts and changes orderly over time",
    "Verification & Validation": "tools for checking and testing your work",
}

# ---- pinned operator copy (plain language; no engine/maintainer jargon) -----------------------
_BANNER = (
    "What this is, and what it can't be: this setup runs once, in a brand-new project made from this\n"
    "template. This project is the workshop where the engine is built, so the real setup has never run\n"
    "here and never will. What follows runs the real setup steps against a throwaway practice project. It\n"
    "shows the steps behave on what I tried — it cannot prove a real adopter's computer, account, and\n"
    "network behave the same. That gap is real; I'm naming it, not hiding it."
)
_ALREADY_SET_UP = ("This project is already set up — first-time setup only runs on a brand-new project. "
                   "Nothing to do here.")
# The one-time lifecycle verbs (apply/verify/retire) refuse a bare hand-run so re-running them on a project
# that is already set up — or in THIS workshop — never re-fires the one-time, file-replacing setup steps.
# apply runs only through the setup walkthrough (which passes the first-run token); a bare apply points the
# operator back there. The flag itself is internal machinery, so the copy never mentions it.
_APPLY_NOT_FIRST_RUN = ("First-time setup runs through the setup walkthrough, not by hand — run /engine-setup, "
                        "which sets a project up or tells you it's already done. Nothing was changed.")
# verify/retire refuse while the root CLAUDE.md is still the engine's construction file (the workshop, or a
# generated repo whose setup has not finished). {what} = "check for consistency" / "tidy up".
_WORKSHOP_NO_SETUP = ("This is the workshop where the engine is built — first-time setup never runs here, so "
                      "there's nothing to {what}. Nothing was changed.")
_EMPTY_CATALOG_LINE = ("There are no optional add-ons to choose yet — the essentials are already included, "
                       "and I'll set those up when you confirm.")
_TIER_PROMPT = (
    "One choice only you can make — who reviews changes here:\n"
    "  • On your own: I'll make changes as you, and you approve each one. (The usual choice — start here.)\n"
    "  • With a team: I'll make changes under a separate account, and your approval is required before "
    "anything merges — and because that account can't change your safety rules, not even I can weaken them. "
    "Worth it even on your own if you want that stronger guarantee. It's a bigger, one-time setup, so you "
    "start on-your-own and I walk you through switching whenever you're ready."
)
_PRODUCT_PROMPT = (
    "One thing worth confirming — what this engine builds:\n"
    "  • Most projects build themselves: this engine works on the very project it's set up in. If that's you, "
    "there's nothing to do — I'll take this project as what you're building.\n"
    "  • If instead this engine exists to work on a DIFFERENT project — a fork you contribute to, or a "
    "template it maintains — tell me which project that is (its owner/name), and I'll record it, so every "
    "session knows where the work is headed."
)
_DESELECT_PREFACE = (
    "When you confirm: the optional add-ons you did NOT keep will be removed from this project — their files\n"
    "are deleted, not just switched off. Wanting one later is a fresh request, not a checkbox you flip back."
)
# The first-run WELCOME orientation — for someone who has already adopted the Engine and is now its operator,
# not a prospect being convinced (that is the README's job). Fuller and more concrete than the README on
# purpose: it walks the new operator through what is ALREADY running, in plain "here is what this does for you"
# terms — the always-present essentials, never a choice, so they are described, not offered. Named and framed to
# match the README's "What's inside" so the two never tell the story two different ways, but authored at its own
# onboarding depth, never copied. Capability-level (Memory, State, Knowledge, …), never module ids — so no raw
# id leaks into operator copy, and the render is the same in every Engine because the spine is invariant.
_LIVE_ALREADY_RUNNING = (
    "What's already running. You didn't choose these and you don't set them up — they came with your Engine and\n"
    "are on from this first session:\n"
    "\n"
    "  Your project's memory and bearings\n"
    "    • Memory — I keep a searchable record of the decisions we make, the pushback you give me, and the\n"
    "      lessons we learn, so a fresh session picks up where the last one left off instead of starting cold. It\n"
    "      captures as we work and distills itself over time. It can also back itself up to a private repo — that\n"
    "      backup stays off until you ask for it.\n"
    "    • State — a short 'where things stand' note I read first each session to get my bearings, and the floor\n"
    "      I fall back on when GitHub can't be reached.\n"
    "    • Knowledge — a map of how your project's parts actually connect, built from your code rather than\n"
    "      guessed, so before I change something I can see what else depends on it.\n"
    "    • Attention — how I work out what to do next, with a built-in rule that blocking problems come ahead of\n"
    "      new features, so nothing urgent gets buried.\n"
    "\n"
    "  What keeps your changes safe\n"
    "    • The review gate — every change I make arrives as a pull request against a protected main branch I\n"
    "      cannot merge on my own. Your approval is the one gate nothing gets past. Automatic checks run on each\n"
    "      change, and a separate guard makes me get your deliberate sign-off before anything can weaken a\n"
    "      safety check.\n"
    "    • Explore and Build — I start every session able only to read and look around; I can change files only\n"
    "      after you deliberately put me into Build — and even then the change still goes through the review\n"
    "      gate.\n"
    "\n"
    "  How each session runs\n"
    "    • The boot briefing and status — a plain-language orientation each time a session starts, and a readout\n"
    "      you can ask for anytime showing where things stand, what shipped, and what needs you.\n"
    "    • Unattended routines — when you set one up, I can advance a plan you've already approved on a schedule\n"
    "      while you're away. I never merge on my own, even then.\n"
    "    • Periodic self-review — a cold, independent check of the Engine's own health that reports what has\n"
    "      drifted or outlived its use. It only tells you; it never changes anything itself.\n"
    "\n"
    "  Wherever you work\n"
    "    • I run natively in both Claude Code and Codex.\n"
    "    • Everything falls back to plain files in your repo: if a service is down the work slows but is never\n"
    "      stranded, because every index and cache rebuilds from what's committed."
)
_DEMO_BRIDGE = ("(In this first step I only show you the choice and what confirming would do — nothing is "
                "deleted yet. The actual setup runs in the next part.)")
_DEMO_LIVE_NOTE = ("(One real touch in this practice run: the project name and branch just below are read live "
                   "from this repo — that part really runs. The add-ons and the saving below all happen in the "
                   "throwaway practice project, not here.)")

# ---- the apply-phase copy surface (heading-keyed; the template is preferred, with built-in fallbacks) ----
# Mirrors bootstrap's load_copy mechanism (reusing its `_parse_sections`). The pre-bootstrap explanation and
# the control-plane degraded banners are NOT re-authored here — they are bootstrap's own copy, surfaced by
# delegating to ControlPlane.apply(announce=…) + bootstrap.render(), so there is one home for them and no
# drift. These keys are the copy the apply phase itself owns.
TEMPLATE_PATH = os.path.join(validate.ENGINE_DIR, "templates", "first-run.md")

COPY_HEADINGS = {
    "tool-runtime-consent": "Before I set up the engine's own tools",
    "tool-runtime-degraded": "If the engine's tools couldn't be set up",
    "plan-mode-adopted": "Your safer default is on",
    "plan-mode-conflict": "Your editing default — keep yours, or use the safer one",
    "plan-mode-conflict-here": "This project already sets an editing default — keep it, or use the safer one",
    "conduct-seeded": "Your stance came with this project",
    "security-seeded": "A security-contact file came with this project",
    "readme-seeded": "Your project's front page is now yours",
    "license-cleared": "Your project starts without a license — and that's normal",
    "claude-floor-seeded": "Your project's working guide",
    "agents-floor-seeded": "Your project's working guide for Codex",
    "state-reseeded": "Your project starts from a clean slate",
    "product-version-seeded": "Your product's release version is ready to use",
    "codeowners-degraded": "If I couldn't set up file ownership for reviews",
    "control-plane-unavailable": "If I couldn't reach your project on GitHub",
    "actions-enablement": "One more switch only you can flip",
    # The finish (verify + tidy-up) phase.
    "verify-paused": "If something needs fixing before finishing",
    "verify-next-actions": "Your two ways forward",
    "verify-ok": "Setup checks out",
    "verify-gate-on": "Your review gate is on",
    "verify-gate-pending": "Your review gate isn't on yet",
    "retire-success": "Setup is complete",
    # The brownfield arrival surface — the live overlap check (see the collision-check + arrive sections) and
    # the team-tier recommendation surfaced during gather when an existing team is detected.
    "team-recommended": "Your project looks like it already has a team",
    "collision-intro": "Before I add the engine, here's what I found in your project",
    "collision-exclusive": "A file of yours sits where the engine keeps its own",
    "collision-shared": "The engine and your project both use the same file",
    "collision-codeowners": "One of your review rules also covers the engine's files",
    "collision-none": "Nothing of yours is in the way",
    "collision-unreadable": "I couldn't safely read one of your files",
}

FALLBACK_COPY = {
    "tool-runtime-consent": (
        "To do its work the engine needs a small set of its own programs, kept in a private folder inside "
        "this project — separate from anything already on your computer, and never touching your own setup. "
        "With your go-ahead I'll download them from their official source, at a fixed version I can name for "
        "you, and place them only in that folder. Nothing is downloaded or installed until you say yes."
    ),
    "tool-runtime-degraded": (
        "I couldn't finish setting up the engine's own programs just now — most often that's a brief network "
        "problem. Nothing is broken and nothing was left half-done; I just can't run the rest of setup until "
        "those programs are in place, and I'll never quietly use a different setup on your computer instead. "
        "Let's try again in a moment, or once you're back online — I'll pick up exactly where I left off."
    ),
    "plan-mode-adopted": (
        "You're set to start in planning mode by default in this project: I'll lay out what I'm about to do "
        "and wait for your go-ahead before making any change — the safer way to work here. This is only a "
        "convenience setting for this project; it changes nothing about your own setup elsewhere, and it "
        "removes no safety check (you still review and approve every change). Change it any time with /config."
    ),
    "plan-mode-conflict": (
        "You already have your own editing default set, so I've left it alone. If you'd like, this project can "
        "start in planning mode instead — I'd lay out my plan and wait for your go-ahead before changing "
        "anything, which is a little safer. Use planning mode for this project, or keep your own default? "
        "Keeping yours changes nothing. Either way, nothing about your setup elsewhere changes and no safety "
        "check is removed."
    ),
    "plan-mode-conflict-here": (
        "This project's own settings already choose an editing default, so I've left it exactly as it is. If "
        "you'd like, this project can start in planning mode instead — I'd lay out my plan and wait for your "
        "go-ahead before changing anything, which is a little safer. Choosing planning mode replaces the "
        "editing default saved in this project; keeping yours leaves that setting exactly as it is. Use "
        "planning mode for this project, or keep the one it already has? Either way no safety check is removed "
        "— you still review and approve every change."
    ),
    "security-seeded": (
        "I added a short file called SECURITY.md at the top of your project. It tells anyone who finds a "
        "security problem — a bug that could let someone get in, or get at your data — how to report it to you "
        "privately, instead of posting it in the open where it could be misused. You own this file and can edit "
        "it any time; if your project already had one, I left yours exactly as it is. I didn't add it silently "
        "— this note is me telling you it's there."
    ),
    "readme-seeded": (
        "This project started from a template, and its front page — the README.md at the top — was the Engine's "
        "own landing page, the page that advertises the Engine to people deciding whether to use it. I replaced "
        "it with a short starter for YOUR project, so your repository's front door is about your work, not the "
        "Engine. This was intentional setup of your project's front page, not a change to anything you wrote — and "
        "the starter is yours to edit freely. I didn't do it silently — this note is me telling you."
    ),
    "license-cleared": (
        "A brand-new project normally starts with no license file at all — and that's a safe, normal place to "
        "begin: your code is yours, and stays yours, until you choose to share it on your own terms. This project "
        "started from a template, and that template carried its own license file — a LICENSE at the top naming the "
        "template's author and their copyright, not you. Left in place it would have set the terms for YOUR project "
        "under someone else's name, so I removed it — and I added nothing in its place, because which license to use "
        "is your decision to make, not mine. GitHub treats a project with no license file as exactly this normal "
        "starting state, and may show a small \"No license\" note on the project page; that's expected, not a "
        "problem. When you're ready to pick one, GitHub's choosealicense.com walks through the common options in "
        "plain language — and I can explain what a license file is and help you add the one you choose, though I "
        "can't tell you which terms are right for you; for anything that really matters legally, ask a person. I "
        "didn't do this silently — this note is me telling you."
    ),
    "codeowners-degraded": (
        "I couldn't read your account name just now, so I haven't yet set up who owns the engine's own files "
        "for review — the part that routes any change to those files to you for approval. It isn't blocking "
        "the rest of setup. Once I can read your account name — for example after you sign in to GitHub from "
        "the command line — I'll set it; or just tell me your account name and I'll set it now."
    ),
    "control-plane-unavailable": (
        "I couldn't find this project on GitHub or sign in just now, so I couldn't turn on the review gate that "
        "protects your main branch. The rest of setup is unaffected. Once you're signed in to GitHub from the "
        "command line and the project is connected, I can turn it on — just ask me to finish setup."
    ),
    "actions-enablement": (
        "One more one-time switch, and GitHub reserves it for you: your review gate waits for two automatic "
        "checks that run through GitHub Actions, and on a brand-new project GitHub keeps Actions off until "
        "the owner turns it on — a click I can't make for you. Open your repository's Actions tab on GitHub; "
        "if you see a button asking you to enable workflows, that's the one — click it. If the tab already "
        "shows your workflows with no button asking to enable anything, this switch is already on and you're "
        "done. Until it's on, those checks never start, so nothing can be approved into your project — "
        "including this setup change itself. And if a waiting change still shows its checks as waiting a "
        "minute or two after you've clicked, tell me and I'll give them a fresh nudge."
    ),
    "verify-paused": (
        "Before finishing, I check that everything fits together — and something doesn't line up yet, so I've "
        "paused rather than carry on with a setup that isn't right. Here's what I found:"
    ),
    "verify-next-actions": (
        "Neither choice loses anything you've already decided. You can fix what's listed above and run setup "
        "again — it picks up right here. Or, if this looks like something you can't sort out yourself, stop here "
        "and report it (copy the lines above so someone can help). I won't carry on with a setup that isn't "
        "consistent."
    ),
    "verify-ok": (
        "Everything fits together — your setup is consistent and ready to use."
    ),
    "verify-gate-on": (
        "Your branch review gate is on: every change to your main branch now goes through approval."
    ),
    "verify-gate-pending": (
        "Your branch review gate isn't on yet — but nothing else is held up by it. I'll remind you each time I "
        "start, and you can turn it on any time by asking me to finish setup."
    ),
    "retire-success": (
        "Setup is complete. I've cleaned up the one-time setup files — the walkthrough, its notes, and the setup "
        "helper itself — now that they've done their job. Everything your project needs to keep running stays in "
        "place, and all your choices are saved. You're ready to start."
    ),
    "team-recommended": (
        "Your project looks like it already has a team — others review changes here. Team mode fits: the engine "
        "commits under a separate account and your approval is required before anything merges, keeping review "
        "the way it already works here — and that account can't change your safety rules. I'll set you up "
        "on-your-own now and walk you through switching to team whenever you're ready (it's a bigger, one-time "
        "setup). On-your-own stays available, and the choice is yours."
    ),
    "collision-intro": (
        "Your project already has files and settings of its own. I'm adding the engine alongside them, so first "
        "I'll show you anywhere the two would overlap — what I'd do, and what you'd keep or lose — and let you "
        "decide. I never change anything without showing you first."
    ),
    "collision-exclusive": (
        "You already have a file here: {paths}. This is a spot the engine normally keeps to itself. If you let "
        "it, the engine would put its own file here in place of yours — so you'd only lose your version if you "
        "choose that. Your choices: let the engine use this spot (your file here is replaced) · keep your file "
        "and have the engine skip it · stop, and decide later. Nothing is replaced until you choose."
    ),
    "collision-shared": (
        "You already have your own entries in {paths}. The engine adds its own clearly-marked section here and "
        "leaves everything else of yours exactly as it is. Your choices: add the engine's section and keep all "
        "of yours · leave this file untouched for now · stop, and decide later."
    ),
    "collision-codeowners": (
        "You have a rule that decides which teammate is asked to review changes to particular files — "
        "`{rule}` — and it also covers the engine's own files. The engine adds its own such rule so changes to "
        "its files are always sent to you; yours keeps covering everything else. I'm pointing this out so the "
        "overlap is no surprise. Your choices: add the engine's rule (so review requests for the engine's files "
        "always come to you) · leave your rules as they are · stop, and decide later."
    ),
    "collision-none": (
        "Good news — none of your files or settings overlap with what the engine adds. I can set it up "
        "alongside your project cleanly."
    ),
    "collision-unreadable": (
        "I couldn't make sense of {paths} just now, so I've left it completely untouched rather than risk "
        "changing it wrongly. Take a look at it, then run setup again — I'll pick up from here."
    ),
    "claude-floor-seeded": (
        "This project started from a template, and its working guide — the CLAUDE.md at the top — arrived "
        "carrying the template's own setup notes, which are about building the template itself, not your "
        "project. I've replaced it with the engine's working guide for YOUR project, kept inside a clearly "
        "marked block I keep current as the engine updates — so if you open that file and see the marker "
        "lines around it, that part is mine to maintain, not something you need to edit. The part that's "
        "yours to shape — how you like me to work with you — lives in your codes of conduct instead: change "
        "it any time with /engine-conduct ($engine-conduct in Codex). I didn't do this silently — this note is me telling you."
    ),
    "agents-floor-seeded": (
        "This project also arrived with the template's own Codex working guide (the AGENTS.md at the top — "
        "the same role CLAUDE.md plays when you work in Claude Code, for sessions run in Codex). I've "
        "replaced it with the engine's working guide for YOUR project, kept inside the same kind of clearly "
        "marked block I maintain as the engine updates. You don't need to edit it — how you like me to work "
        "with you lives in your codes of conduct, changeable any time with /engine-conduct ($engine-conduct in Codex)."
    ),
    "state-reseeded": (
        "I reset this project's starting point to a clean slate. The engine keeps a small saved note of where "
        "a project stands — its current focus and how much work is open. The copy that arrived with the "
        "template still pointed at the engine's OWN workshop, so I cleared it: no borrowed focus, no borrowed "
        "counts, nothing pointing back at the template's own to-do list. Nothing of yours was lost — a "
        "brand-new project has nothing there yet — and this changes nothing else. I didn't do it silently — "
        "this note is me telling you."
    ),
    "conduct-seeded": (
        "This project came set up with a starting set of codes of conduct — short notes on how you like me "
        "to work with you (for example, speaking plainly, and explaining choices before you make them). "
        "They're here from the first session, and they're yours: change, add, or remove any of them any time "
        "with /engine-conduct ($engine-conduct in Codex). I didn't put them in place silently — this note is me telling you they're here."
    ),
    "product-version-seeded": (
        "Your project now carries its own version file — product-version.json at the top level, starting at "
        "0.0.0. This is where your PRODUCT's release version lives, separate from the engine's own version. "
        "When you want to publish a release of your product, the engine's release workflow reads and updates "
        "this file, tags your repository, and publishes a GitHub Release — the same reviewed flow, where your "
        "merge is the only go-ahead, that the engine uses for itself. Publishing your first release needs a "
        "one-time credential the release workflow walks you through the first time you run it. The file is "
        "yours: change the starting version if you like, and it stays put when the engine updates. I didn't add "
        "it silently — this note is me telling you it's here."
    ),
}


def load_copy(path: str = TEMPLATE_PATH) -> dict:
    """The apply-phase operator copy: the template surface preferred, built-in fallbacks where a section is
    missing or the template is unreadable (the bootstrap.load_copy shape, reusing its section parser). Keyed
    by the stable COPY_HEADINGS keys."""
    by_heading: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            by_heading = bootstrap._parse_sections(fh.read())
    except OSError:
        by_heading = {}
    return {key: (by_heading.get(heading, "").strip() or FALLBACK_COPY[key])
            for key, heading in COPY_HEADINGS.items()}


def _write_json(path: str, data: dict) -> None:
    """Write `data` as 2-space-indented JSON with a trailing newline — the same on-disk shape the engine
    manifest already uses, so a written manifest matches the rest of the tree (a minimal diff)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def _engine_manifest_path(root: str | None = None) -> str:
    return os.path.join(root or validate.ROOT, ".engine", "engine.json")


def is_provisioned(root: str | None = None) -> bool:
    """True iff the engine manifest (.engine/engine.json) is PRESENT — the RAW manifest-presence predicate,
    NOT on its own "this repo is set up." The manifest TRAVELS with the template, so a fresh generated copy
    inherits one too; keying "already set up" on presence alone is exactly the dead-on-arrival bug (#353).
    Callers that must tell a finished repo from an un-transformed copy pair this with observable installed
    shape — `module_coherence.is_downstream_copy(...)` (origin != recorded home) and the setup tool's presence
    (`first_run_health`); the `show` branch below does. Kept as the low-level predicate other callers/tests
    rely on (no separate state file is introduced — the derivation reads existing shape)."""
    return os.path.isfile(_engine_manifest_path(root))


def derive_identity(root: str | None = None) -> dict:
    """The repo coordinates read from git/GitHub (derive-first) — owner, name, and the protected branch.
    Best-effort: any field is None when it cannot be read, and the walkthrough says so rather than guessing.
    (The operator handle, used later to render code-ownership, is captured in the apply phase.)"""
    slug = boot.repo_slug()
    owner, name = slug.split("/", 1) if slug and "/" in slug else (None, None)
    return {"owner": owner, "name": name, "branch": boot.PROTECTED_BRANCH}


def selectable(catalog_entries: list) -> dict:
    """The catalog's optional features grouped by their SDLC discipline, in the fixed category order, ready to
    present as choices. Only the catalog set is offered — the always-present essentials (the required spine)
    are never a choice. An entry whose category is unrecognized is grouped last under its own raw label
    rather than dropped (degrade, never hide a real option). All entries are presented uniformly today; a
    catalog `status` of `default-on` (added unless opted out) or `experimental` (opt-in) will want a distinct
    default-state/label — `owes →` whoever first sets a catalog `status` other than plain `optional` (every
    committed entry ships as plain `optional` today)."""
    grouped: dict = {cat: [] for cat in _CATEGORY_ORDER}
    for entry in catalog_entries:
        grouped.setdefault(entry.get("category") or "Other", []).append(entry)
    # Drop empty recognized groups, keep any non-empty (including an unexpected category), category order first.
    ordered = [c for c in _CATEGORY_ORDER if grouped.get(c)]
    extra = sorted(c for c in grouped if c not in _CATEGORY_ORDER and grouped.get(c))
    return {c: sorted(grouped[c], key=lambda e: (e.get("verb", ""), e.get("id", ""))) for c in ordered + extra}


def optional_dependency_closure(manifests) -> dict:
    """For each OPTIONAL module, the OTHER OPTIONAL modules it transitively `depends` on — the
    optional→optional pull-ins the gather step surfaces at the choice moment. Always-present
    `required` dependencies (core, validators-core, …) are DELIBERATELY excluded — they are the spine, never
    offered as a choice, so surfacing them would only add noise. `manifests` is the
    (path, manifest) list module_coherence.discover_manifests yields.

    Vacuous today: every optional module depends only on core, so every list is empty. The mechanism is
    spec-mandated and armed for the first optional module that depends on another — v1 ships it complete, not
    deferred."""
    depends, status = {}, {}
    for _path, manifest in manifests:
        mid = manifest.get("id")
        if not mid:
            continue
        depends[mid] = manifest.get("depends") or {}
        status[mid] = manifest.get("status")
    optional = {mid for mid, s in status.items() if s == "optional"}
    closure = {}
    for mid in optional:
        seen, stack = set(), list(depends.get(mid, {}))
        while stack:
            dep = stack.pop()
            if dep in seen:
                continue
            seen.add(dep)
            stack.extend(depends.get(dep, {}))
        closure[mid] = sorted(d for d in seen if d in optional and d != mid)
    return closure


def present_gather(root: str | None = None, catalog_path: str | None = None, team=None,
                   manifests=None) -> str:
    """The plain-language GATHER walkthrough the operator reads: the repo coordinates I derived, the WELCOME
    orientation to what is already running (the always-present essentials — described, never offered), the one
    identity choice (plus a team-tier recommendation when an existing team is detected — brownfield arrival,
    a suggestion, not a seizure), the optional features to pick from
    (grouped by discipline, or the no-add-ons line when the catalog is empty), and the plain statement that
    not-kept add-ons are deleted on confirm. Pure text — no prompts, no writes; the skill/runbook does the
    asking. `team` (the detect_team result) and `manifests` (the discover_manifests list, for the
    dependency closure annotation) are injectable for tests/the demo."""
    ident = derive_identity(root)
    closure = optional_dependency_closure(
        manifests if manifests is not None else module_coherence.discover_manifests())
    coords = (f"{ident['owner']}/{ident['name']}" if ident["owner"] and ident["name"]
              else "(I couldn't read your project's name from GitHub — I'll ask you instead)")
    lines = [
        "Welcome — your Engine is here, and this is your first-run walkthrough. Here's what I found, what's",
        "already running, and the few choices I'll ask you to make:",
        "",
        f"Your project: {coords}",
        f"The branch I'll protect with a review gate: {ident['branch']}",
        "",
        _LIVE_ALREADY_RUNNING,
        "",
        _TIER_PROMPT,
        "",
        _PRODUCT_PROMPT,
    ]
    team = team if team is not None else detect_team(root=root)
    if team.get("detected"):
        lines += ["", load_copy()["team-recommended"]]
    lines += [
        "",
        "Optional add-ons — include what you want now; you can add any later just by asking, and any is",
        "removable. Leave out anything you don't need:",
        "",
    ]
    grouped = selectable(module_catalog.entries(catalog_path))
    if not grouped:
        lines.append("  " + _EMPTY_CATALOG_LINE)
    else:
        for category, entries in grouped.items():
            gloss = _CATEGORY_GLOSS.get(category, "")
            lines.append(f"  {category}" + (f" — {gloss}:" if gloss else ":"))
            for entry in entries:
                # A command-bearing module leads with its command; a command-less one (no verb — fired by a
                # gate, never typed) leads with its plain-language description, so the menu never shows a
                # command-shaped token an operator can't actually type.
                if entry["verb"]:
                    lines.append(f"    • {entry['verb']} — {entry['description']}")
                else:
                    lines.append(f"    • {entry['description']}")
                # Its dependency closure: any OTHER optional feature this one pulls in (required-spine deps
                # are never surfaced — they are always present). Vacuous until an optional module depends on
                # another optional one, but presented at the choice moment so the pull-in is never a surprise.
                pulls = closure.get(entry.get("id")) or []
                if pulls:
                    lines.append(f"        Including this also turns on: {', '.join(pulls)} "
                                 f"(it depends on {'them' if len(pulls) > 1 else 'it'}).")
            lines.append("")
    lines.append(_DESELECT_PREFACE)
    return "\n".join(lines)


def confirm(kept_optional_ids: list, tier: str, *, root: str | None = None,
            engine_release: str | None = None, handle: str | None = None,
            default_branch: str | None = None, product_repository: str | None = None,
            manifests=None) -> dict:
    """CONFIRM — write the engine manifest, the resumability checkpoint. Records the engine release,
    the identity tier, the kept package set (the always-present required spine plus the optional features the
    operator kept — an unkept optional is simply left out of the manifest, its files removed later in the
    apply phase, not here), the operator's handle when known (the preserved-config owner the apply phase
    renders code-ownership from), and the repo's derived default-branch name when known (the preserved-config
    coordinate offline classification reads — checkout_health's operator-checkout strand model, #342 — instead
    of a frequently-unset `origin/HEAD`), and the engine's update HOME carried forward from the traveled/seed
    manifest (where the engine fetches its own updates from — #367). Each derived/carried field is
    omitted when None, keeping the manifest valid either way. This is the single committing step; before it,
    nothing is written. Returns the written path and the manifest. `root`, `engine_release`, `handle`,
    `default_branch`, and `manifests` (for the dependency closure) are injectable for tests and the demo."""
    kept = set(kept_optional_ids or [])
    manifests = manifests if manifests is not None else module_coherence.discover_manifests()
    # Honor the dependency closure the gather step surfaced: keeping an optional module also installs the
    # optional modules it depends on (the pull-ins present_gather annotated with "Including this also turns
    # on: …"), so the written manifest never records a kept module without its optional dependency — the
    # annotation's promise is kept, and the apply phase never halts on a missing-dependency coherence finding.
    # Vacuous today (no optional module depends on another optional one); required deps are already always-present.
    closure = optional_dependency_closure(manifests)
    kept |= {dep for mid in list(kept) for dep in closure.get(mid, [])}
    packages: dict = {}
    for _rel, manifest in manifests:
        mid, status = manifest.get("id"), manifest.get("status")
        if not mid:
            continue  # a manifest with no id fails the module schema upstream; never crash the committing step
        if status == "required" or mid in kept:
            packages[mid] = str(manifest.get("version") or "")
    release = engine_release or _existing_release(root) or "0.0.0-dev"
    written = {"engine_release": release, "packages": dict(sorted(packages.items())), "identity": tier}
    if handle:
        written["handle"] = handle
    if default_branch:
        written["default_branch"] = default_branch
    # Carry the engine's update HOME forward from the traveled/seed manifest (#367): it is
    # seeded as data in the template and preserved across setup like the release, not derived here. Omitted
    # when absent so a manifest without it stays valid.
    home_repository = _existing_home_repository(root)
    if home_repository:
        written["home_repository"] = home_repository
    # The PRODUCT coordinate — the repo this engine works ON when that differs from the repo it is deployed
    # into (eADR-0026's fork-native arrangement). Precedence: an explicit external override (the caller passes
    # it ONLY when the product is a repository distinct from self) wins; else an already-recorded product is
    # carried FORWARD, so a resumed/re-run confirm never clobbers the operator's choice (the home_repository
    # precedence at #367). NEVER a self-default: self is derivable live from origin, so storing it would only
    # duplicate origin and drift stale on a rename — a self-building deployment writes nothing here.
    product_repository = (product_repository or "").strip() or _existing_product_repository(root)
    if product_repository and product_repository.strip():
        written["product_repository"] = product_repository.strip()
    path = _engine_manifest_path(root)
    _write_json(path, written)
    return {"path": path, "manifest": written}


def derive_handle() -> str | None:
    """The operator's OWN account handle — the owner the apply phase writes into the engine's code-ownership
    block (the same handle in both identity tiers; the tier governs who commits and the gate, not whose
    handle owns the engine's files). Best-effort from the signed-in `gh` CLI; None when `gh` is absent or not
    signed in, in which case the ownership step degrades and says so rather than guessing. Distinct from the
    repo owner, which may be an organization."""
    import subprocess
    try:
        out = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                             capture_output=True, text=True, timeout=15, check=False)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — missing binary / timeout / OS error → no handle, degrade downstream
        return None


def derive_default_branch(root: str | None = None, *, slug: str | None = None, gh_api=None) -> str | None:
    """The repo's DEFAULT branch name, derived best-effort at first run (provisioning: a derived coordinate,
    persisted by `confirm` as operator config). `gh` first (authoritative), then git's `origin/HEAD`, then
    None — never a bare guess. `confirm` persists it so later OFFLINE classification (checkout_health's #342
    strand model) reads a known name rather than a `refs/remotes/origin/HEAD` that is frequently unset (and
    absent on a no-remote checkout).

    Scoped to the TARGET repo, not the process cwd: `slug` (the GitHub `owner/repo` for the gh read) defaults
    to the cwd's origin on the greenfield path, but the brownfield arrival passes the target's slug; `root`
    scopes the git fallback to the target tree; `gh_api` is injectable (the arrival's transport, and tests)."""
    import subprocess
    gh = gh_api if gh_api is not None else _gh_api_json
    slug = slug or boot.repo_slug()
    if slug:
        data = gh(f"repos/{slug}")
        if isinstance(data, dict) and isinstance(data.get("default_branch"), str) and data["default_branch"]:
            return data["default_branch"]
    try:
        out = subprocess.run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                             capture_output=True, text=True, timeout=15, check=False, cwd=root or validate.ROOT)
        ref = out.stdout.strip() if out.returncode == 0 else ""
        return ref.split("origin/", 1)[1] if ref.startswith("origin/") else (ref or None)
    except Exception:  # noqa: BLE001 — missing binary / timeout / OS error → no derived name, persist nothing
        return None


def _gh_api_json(path: str):
    """One best-effort `gh api <path>` read, parsed as JSON — None on any failure (gh absent / not signed in /
    missing scope / network / non-zero exit / unparseable). The team-detection network boundary; injectable so
    tests and the demo never reach GitHub."""
    import subprocess
    try:
        out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=15, check=False)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except Exception:  # noqa: BLE001 — missing binary / timeout / decode error → unknown signal, degrade
        return None


def _codeowners_distinct_owners(root: str) -> int:
    """How many DISTINCT @owners the project's own CODEOWNERS assigns, OUTSIDE any engine-managed block (so
    the engine's own single-owner rule never counts). >1 distinct owners is the local 'a team already reviews
    here' signal. 0 when the file is absent/unreadable/malformed (no signal, never a crash)."""
    path = os.path.join(root, ".github", "CODEOWNERS")
    text = _read_text_opt(path)
    if text is None:
        return 0
    lines = text.split("\n")
    try:
        span = wiring._find_fence(lines, wiring.CODEOWNERS_FENCE)
    except wiring.WiringError:
        return 0
    excluded = set(range(span[0], span[1] + 1)) if span else set()
    owners = set()
    for i, ln in enumerate(lines):
        if i in excluded:
            continue
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue
        owners.update(tok for tok in stripped.split()[1:] if tok.startswith("@"))
    return len(owners)


def _target_slug(target_root: str):
    """The owner/repo of an arrival TARGET, read from ITS git remote — NOT the process cwd, which on a
    brownfield run is the extracted release tree. So every live GitHub side of the arrival (branch protection,
    native scanning, team detection, the arrival PR) is aimed at the project named by --target, never wherever
    the tool happened to be launched. None when it can't be read (the dependent steps then degrade and say so)."""
    import subprocess, re
    try:
        out = subprocess.run(["git", "-C", target_root, "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=15, check=False)
        if out.returncode != 0:
            return None
    except Exception:  # noqa: BLE001 — missing binary / timeout / OS error → unknown, degrade
        return None
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", out.stdout.strip())
    return m.group(1) if m else None


def detect_team(*, root: str | None = None, slug: str | None = None, gh_api=None) -> dict:
    """Brownfield team detection: does this project already have a team
    reviewing changes? Three READ-ONLY signals — a multi-owner CODEOWNERS (local), an existing required-review
    rule, or an organization-owned repo (both via `gh api`). Any one → a recommendation to use the team tier,
    NEVER a switch (the operator still chooses). Each network signal degrades to 'unknown' (not a false
    positive) when `gh` can't answer. `slug` is the TARGET's owner/repo (the live caller passes the arrival
    target's, never the process cwd's); `gh_api(path) -> parsed-json-or-None` is injectable for tests/the demo.
    Returns {detected, reason, signals}."""
    base = root if root is not None else validate.ROOT
    gh = gh_api if gh_api is not None else _gh_api_json
    slug = slug if slug is not None else boot.repo_slug()
    signals = []
    if _codeowners_distinct_owners(base) > 1:
        signals.append("more than one reviewer is already named in your CODEOWNERS")
    if slug:
        repo = gh(f"repos/{slug}")
        if isinstance(repo, dict) and (repo.get("owner") or {}).get("type") == "Organization":
            signals.append("the project belongs to an organization")
        reviews = gh(f"repos/{slug}/branches/{boot.PROTECTED_BRANCH}/protection/required_pull_request_reviews")
        if isinstance(reviews, dict) and (reviews.get("required_approving_review_count") or 0) > 0:
            signals.append("changes here already require a review before merging")
    return {"detected": bool(signals), "reason": signals[0] if signals else None, "signals": signals}


def _existing_release(root: str | None = None) -> str | None:
    """The engine release recorded in an existing manifest, if any — so a re-run keeps the same release
    rather than resetting it. None when there is no readable manifest."""
    try:
        with open(_engine_manifest_path(root), encoding="utf-8") as fh:
            return json.load(fh).get("engine_release")
    except Exception:
        return None


def _existing_home_repository(root: str | None = None) -> str | None:
    """The engine's HOME repository recorded in the traveled/existing manifest, if any — carried FORWARD so
    first-run setup preserves where the engine updates from (seeded as ground-truth data in the template's
    committed manifest, never a code constant; #367). None when there is no readable manifest
    or it records no home (a repo generated before this coordinate shipped), in which case the field is
    simply left out and the update path refuses-with-a-remedy rather than guessing a home."""
    try:
        with open(_engine_manifest_path(root), encoding="utf-8") as fh:
            home = json.load(fh).get("home_repository")
        return home if isinstance(home, str) and home.strip() else None
    except Exception:
        return None


def _existing_product_repository(root: str | None = None) -> str | None:
    """The engine's recorded PRODUCT repository, if any — carried FORWARD on a resumed/re-run confirm so a
    resume never clobbers an operator's external-product override with nothing (the _existing_home_repository
    precedence). None when there is no readable manifest or none is recorded (the common self-building case),
    in which case the product is this repository itself and is derived live at read time, never stored."""
    try:
        with open(_engine_manifest_path(root), encoding="utf-8") as fh:
            product = json.load(fh).get("product_repository")
        return product if isinstance(product, str) and product.strip() else None
    except Exception:
        return None


def _external_product_or_none(supplied: str | None, self_slug: str | None) -> str | None:
    """The product coordinate to RECORD from an operator-supplied `--product-repository`, or None to record
    nothing. Records ONLY a genuine EXTERNAL product: the trimmed value when it differs from `self_slug` (the
    deployed-into repo), else None — a self-equal override is the common self-building case, derived live and
    never stored, so it can't drift on a rename. The self-comparison is NORMALIZED — case-insensitive and
    ignoring a trailing `.git` and surrounding whitespace — because GitHub owner/repo is case-insensitive and an
    operator hand-types the value (so `Acme/Widget`, `acme/widget.git`, and ` acme/widget ` all read as self).
    When `self_slug` is None (origin unreadable) the value cannot be proven self, so it is kept trimmed — a
    conservative fallback: there is then no live origin for it to duplicate, and the coordinate is display-only."""
    product = (supplied or "").strip()
    if not product:
        return None

    def _norm(s: str) -> str:
        s = s.strip().casefold()
        return s[:-4] if s.endswith(".git") else s

    if self_slug and _norm(product) == _norm(self_slug):
        return None
    return product


# ==== APPLY — install the confirmed selection and turn on the engine's guardrails =====
#
# The seven ordered, idempotent, manifest-driven apply steps. The
# phase runs on the operator's SYSTEM python; steps 1–3 need nothing extra, step 4 materializes the engine's
# own tool-runtime (uv + the .venv), and steps 5–7 follow. Each step degrades INTERNALLY (it never crashes
# the phase) — EXCEPT a degraded tool-runtime (step 4), which HALTS the phase, because steps 5–7 presuppose
# a materialized runtime; a retry resumes from the manifest checkpoint. Apply ENDS after the control-plane
# attempt — the verify/coherence pause and self-retire are the next phase.
#
# THE DEGENERACY (unchanged from gather/confirm): none of this runs in the construction repo. The demo runs
# the REAL step logic against a throwaway generated-repo fixture, faking ONLY the external boundaries (the
# operator's home settings, the uv install + sync, the GitHub review-gate calls). "Works on the fixture ⇒
# works for a real adopter" is the named inductive step the fixture cannot discharge.

UV_PIN = "0.11.8"  # the pinned uv version to bootstrap — MUST match the committed CI pin (every
                   # .github/workflows/*.yml astral-sh/setup-uv `version:`) so the runtime the instantiator
                   # materializes matches the engine's resolved uv.lock. (Faked in every test and the demo; a
                   # real install runs only on a generated repo.) The tie is enforced at merge by
                   # test_instantiator.test_uv_pin_ties_to_every_ci_workflow_setup_uv_version — construction-
                   # coupled ON PURPOSE: UV_PIN is bootstrap-only and this file + that test both retire at
                   # first-run, so the tie lives with the instantiator rather than as a traveling check that
                   # would reference retired code once the instantiator is gone (#411 weighed and rejected
                   # a first-class check here for exactly that reason).
UV_INSTALL_DIR_REL = os.path.join(".engine", ".uv")
UV_INSTALL_URL = f"https://astral.sh/uv/{UV_PIN}/install.sh"


def _codeowners_path() -> str:
    return os.path.join(validate.ROOT, ".github", "CODEOWNERS")


def _read_home_settings() -> dict:
    """The operator's OWN global Claude settings, read-only. The engine reads the interactive default from
    here but NEVER writes `~/.claude` — the operator's global settings are the operator's. Returns
    {} when absent or unreadable (the no-conflict path: adopt the safer default)."""
    path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — absent / unreadable / malformed → treat as no global preference
        return {}


def _uv_present() -> str | None:
    """The path to a usable uv: the engine-known install location first, then one already on PATH (the
    operator may already have uv — then no install/consent is needed), else None."""
    known = os.path.join(validate.ROOT, UV_INSTALL_DIR_REL, "uv")
    if os.path.isfile(known) and os.access(known, os.X_OK):
        return known
    import shutil
    return shutil.which("uv")


def _install_uv() -> str | None:
    """Install the pinned uv into the engine-known folder, PATH-independently, from the official Astral
    installer. `UV_UNMANAGED_INSTALL` places the binary directly in that folder, edits no shell profile or
    environment, and disables self-update (so the engine controls the version). Returns the binary path on
    success, None on any failure (offline / blocked download / unsupported platform) — the caller degrades
    LOUD and never falls back to system python."""
    import subprocess
    install_dir = os.path.join(validate.ROOT, UV_INSTALL_DIR_REL)
    os.makedirs(install_dir, exist_ok=True)
    env = dict(os.environ)
    env["UV_UNMANAGED_INSTALL"] = install_dir
    try:
        proc = subprocess.run(["sh", "-c", f"curl -LsSf {UV_INSTALL_URL} | sh"],
                              env=env, capture_output=True, text=True, timeout=300, check=False)
        uv_path = os.path.join(install_dir, "uv")
        if proc.returncode == 0 and os.path.isfile(uv_path):
            return uv_path
        return None
    except Exception:  # noqa: BLE001 — missing curl/sh, timeout, OS error → degrade
        return None


def _uv_sync(uv_path: str, groups: list) -> bool:
    """Group-scoped `uv sync` into the engine-namespaced runtime, using the bootstrapped uv by absolute path
    (mirrors module_manager._resync_tool_runtime; the kept group selection is already written into the
    committed config by the delete-unselected step, so a plain sync is group-scoped). NEVER system python.
    Returns True on success, False on any failure (degrade)."""
    import subprocess
    try:
        subprocess.run([uv_path, "sync"], cwd=os.path.join(validate.ROOT, ".engine"),
                       check=True, capture_output=True, timeout=600)
        return True
    except Exception:  # noqa: BLE001 — unreachable index / resolution failure → degrade
        return False


# ---- the eight apply steps (each returns one ledger entry; each idempotent) -------------------

def _apply_delete_unselected(manifest: dict, say) -> dict:
    """STEP 1 — remove every module present on disk that the confirmed manifest did not keep (installed
    means present). Reuses module_manager.remove: it discovers from disk, refuses a `required` module and a
    module another kept module still depends on (recorded, never crashed — a kept module and its dependency
    both remain, which is coherent), and reverses any wiring + deletes the files it owns. Idempotent: an
    already-deleted module is simply not present on a re-run."""
    kept = set((manifest.get("packages") or {}).keys())
    present = sorted({m.get("id") for _p, m in module_coherence.discover_manifests() if m.get("id")})
    deleted, refused = [], []
    for mid in present:
        if mid in kept:
            continue
        res = module_manager.remove(mid)
        if res.get("refused"):
            refused.append({"id": mid, "reason": res.get("reason")})
        else:
            deleted.append(mid)
    return {"step": "remove-unselected", "status": "done", "deleted": deleted, "refused": refused}


def _apply_foundation_ignores(say) -> dict:
    """STEP (foundation ignores) — place the engine's keyed `.gitignore` fence (`.engine/.venv/`,
    `.engine/.uv/`, `.claude/worktrees/`) via the wiring library helper (#409). Runs BEFORE codeowners so
    the file exists when `codeowners_path_set()` globs it (the `/.gitignore @owner` line renders on first
    brownfield apply), and pre-runtime so a tool-runtime halt still leaves `.venv/` ignored — the strand
    pre-check's clean-tree read stays true. Idempotent (fence_apply inserts iff absent); fails open (the
    helper degrades, never crashes). No operator disclosure — this is engine infra placement, not operator
    config (unlike plan-mode / conduct)."""
    outcome = wiring.apply_foundation_ignores(wiring.GITIGNORE_PATH)
    return {"step": "foundation-ignores", "status": outcome["status"]}


def _apply_codeowners(handle, say, copy) -> dict:
    """STEP 2 — render the engine's code-ownership block so any change to the engine's own files routes to
    the operator for review. The owner is the stored handle; with no handle the renderer refuses, so the step
    DEGRADES (announce + skip), never crashes. Write-iff-changed (idempotent). Delegates the path set
    (module_coherence.codeowners_path_set — which self-adds CODEOWNERS so the block owns its own routing
    rule from the first render) and the render-and-write (wiring.apply_codeowners) to the shared home an
    engine upgrade's re-render also uses, so the two render sites cannot drift."""
    if not handle:
        say(copy["codeowners-degraded"])
        return {"step": "codeowners", "status": "degraded", "detail": "no operator handle available"}
    try:
        outcome = wiring.apply_codeowners(_codeowners_path(),
                                          module_coherence.codeowners_path_set(), handle)
    except wiring.WiringError:
        say(copy["codeowners-degraded"])
        return {"step": "codeowners", "status": "degraded", "detail": "could not render ownership"}
    if outcome["status"] == "already":
        return {"step": "codeowners", "status": "already", "owner": handle}
    return {"step": "codeowners", "status": "written", "owner": handle, "paths": outcome["paths"]}


def _apply_plan_mode(home_reader, settings_path, consent, say, copy) -> dict:
    """STEP 3 — recommend the planning permission-mode as this repo's interactive default, obeying
    yield-to-the-operator. Read the operator's existing default read-only — BOTH the GLOBAL default
    (`~/.claude`, never written) AND, on brownfield, any default this project's own committed
    `.claude/settings.json` already carries. With no conflicting preference (or one already planning) ADOPT plan into the project
    settings with a plain disclosure; on a conflict OFFER adopt-or-keep once — keep writes nothing / leaves the
    project value exactly as it is (the yield). The project scalar is checked INDEPENDENTLY of the global value:
    a committed project default is a recorded operator decision in THIS repo, so a global default of plan/unset
    must never license silently overwriting it (#409). Idempotent: a project default already set to planning
    is a no-op. The project write is surgical (preserves the operator's other settings)."""
    proj_path = settings_path or wiring.SETTINGS_PATH
    proj, err = wiring._read_json_tolerant(proj_path, create=True)
    if err is not None:
        return {"step": "plan-mode", "status": "degraded", "detail": "project settings unreadable"}
    proj_mode = (proj.get("permissions") or {}).get("defaultMode")
    if proj_mode == "plan":
        return {"step": "plan-mode", "status": "already"}
    home = home_reader() if home_reader is not None else _read_home_settings()
    global_mode = (home.get("permissions") or {}).get("defaultMode") if isinstance(home, dict) else None
    # A conflicting operator preference in EITHER place → offer adopt-or-keep once. The project-here conflict
    # takes precedence: its consequence differs (adopt REPLACES a value the operator committed in this repo;
    # keep leaves it untouched), so it gets copy that names that — not the global copy, which would falsely
    # reassure that "nothing about your setup elsewhere changes" while the setting being changed is right here.
    if proj_mode not in (None, "plan"):                        # the operator's own committed PROJECT default
        if not (consent("plan-mode-adopt") if consent is not None else False):
            say(copy["plan-mode-conflict-here"])
            return {"step": "plan-mode", "status": "kept-operator-default"}
    elif global_mode not in (None, "plan"):                    # a conflicting GLOBAL preference (elsewhere)
        if not (consent("plan-mode-adopt") if consent is not None else False):
            say(copy["plan-mode-conflict"])
            return {"step": "plan-mode", "status": "kept-operator-default"}
    proj.setdefault("permissions", {})["defaultMode"] = "plan"  # adopt (no conflict, or operator chose to)
    wiring._write_json(proj_path, proj)
    say(copy["plan-mode-adopted"])
    return {"step": "plan-mode", "status": "adopted"}


def _apply_tool_runtime(uv_present, uv_installer, uv_runner, consent, say, copy) -> dict:
    """STEP 4 (the heaviest) — materialize the engine's own tool-runtime: ensure uv is present (install it,
    behind an explicit consent gate, when it is not — software placed on the operator's machine, a heavier
    trust class than a permission grant), then group-scoped `uv sync`. DEGRADE LOUD, never to system python;
    a degraded outcome HALTS apply (the `halt` flag) — a retry resumes from the checkpoint. Every boundary is
    injectable so tests/the demo run the real control flow without touching the network or the machine."""
    present = uv_present() if uv_present is not None else _uv_present()
    if not present:
        say(copy["tool-runtime-consent"])                       # explain BEFORE asking
        if not (consent("install-uv") if consent is not None else False):
            return {"step": "tool-runtime", "status": "degraded", "halt": True,
                    "detail": "waiting for your go-ahead to install the engine's tools"}
        present = uv_installer() if uv_installer is not None else _install_uv()
        if not present:
            say(copy["tool-runtime-degraded"])
            return {"step": "tool-runtime", "status": "degraded", "halt": True,
                    "detail": "the engine's tools could not be installed"}
    groups = []
    try:
        groups = module_manager.derive_uv_groups()
    except Exception:  # noqa: BLE001 — group derivation is best-effort; sync uses the committed selection
        groups = []
    ok = uv_runner(present, groups) if uv_runner is not None else _uv_sync(present, groups)
    if not ok:
        say(copy["tool-runtime-degraded"])
        return {"step": "tool-runtime", "status": "degraded", "halt": True,
                "detail": "the engine's tools could not be set up"}
    return {"step": "tool-runtime", "status": "materialized", "groups": groups}


_EMPTY_OPERATOR = (
    "---\ncodes: []\n---\n\n"
    "<!-- Your own codes of conduct go here — add, revise, or remove them with /engine-conduct "
    "($engine-conduct in Codex). They sit "
    "alongside the engine's defaults and take priority when they share an id. This file is yours: an engine "
    "update never overwrites it. It starts empty — the engine's defaults are already in force. -->\n"
)


def _seed_conduct(say, copy=None) -> str:
    """Seed the operator's codes-of-conduct override from the maintainer's template seed — the seed-then-own
    pattern, the same SHAPE and DISCLOSURE as _seed_security, and like it COPY-IF-ABSENT: once
    .engine/conduct/operator.md exists it is operator config, so the engine NEVER overwrites it — a
    resumed/re-run apply leaves a /engine-conduct-tuned stance exactly as it is (returns "present"). On first
    run copies .engine/provisioning/conduct-seed.md into the committed .engine/conduct/operator.md; an absent or
    empty seed yields a valid empty override, never an error. Then discloses, in plain language, that the stance
    is present and theirs to tune — only when it actually seeds. Paths are validate.ROOT-relative, so a
    redirected demo/test seeds only the fixture, never the real tree."""
    seed_path = os.path.join(validate.ROOT, ".engine", "provisioning", "conduct-seed.md")
    target = os.path.join(validate.ROOT, ".engine", "conduct", "operator.md")
    if os.path.exists(target):
        return "present"                        # once seeded, operator.md is operator config — never overwrite it
    try:
        content = ""
        if os.path.isfile(seed_path):
            with open(seed_path, encoding="utf-8") as fh:
                content = fh.read()
        if not content.strip():
            content = _EMPTY_OPERATOR
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError:
        return "skipped"
    if copy is not None:
        say(copy["conduct-seeded"])
    return "seeded"


# A minimal, valid SECURITY.md used only when the maintainer's template seed is absent or empty — never an
# error (provisioning "the security floor": an absent template seed yields a minimal default file). No personal
# contact: this default, like the seed, travels to every generated repo.
_DEFAULT_SECURITY_MD = (
    "# Security Policy\n\n"
    "## Reporting a security vulnerability\n\n"
    "Please report security problems **privately** rather than opening a public issue. The preferred way is "
    "GitHub's private vulnerability reporting — open this repository's **Security** tab and choose "
    "**\"Report a vulnerability\"** — or contact the project's owner or maintainer privately. Include enough "
    "detail to reproduce the problem.\n"
)

# The locations GitHub recognizes a SECURITY.md in, in precedence order (.github/ wins, then root, then docs/).
# The seed scans ALL THREE and seeds only if NONE exists, so it never creates a root file GitHub would ignore
# because a .github/ one already wins.
_SECURITY_LOCATIONS = ("SECURITY.md", os.path.join(".github", "SECURITY.md"), os.path.join("docs", "SECURITY.md"))


def _seed_security(say, copy=None) -> str:
    """Seed a root SECURITY.md vulnerability-disclosure channel from the maintainer's template seed — the
    seed-then-own pattern, the same SHAPE, DISCLOSURE, and COPY-IF-ABSENT posture as _seed_conduct: it NEVER
    overwrites. If the project already carries a SECURITY.md in any GitHub-recognized location
    (root, .github/, or docs/), the engine leaves it exactly as it is and seeds nothing (returns "present").
    Otherwise it copies .engine/provisioning/security-seed.md into a ROOT SECURITY.md; an absent or empty seed
    yields a minimal default, never an error. The seeded file is operator-owned product config — in no `provides`,
    preserved across the upgrade overlay with no engine carve-out (it lives outside .engine/, so the ownership
    leg never reaches it). Discloses, in plain language, that the file was added — only when it actually seeds.
    Paths are validate.ROOT-relative, so a redirected demo/test touches only the fixture, never the real tree."""
    if any(os.path.exists(os.path.join(validate.ROOT, rel)) for rel in _SECURITY_LOCATIONS):
        return "present"                        # never overwrite a project's existing disclosure file
    seed_path = os.path.join(validate.ROOT, ".engine", "provisioning", "security-seed.md")
    target = os.path.join(validate.ROOT, "SECURITY.md")
    try:
        content = ""
        if os.path.isfile(seed_path):
            with open(seed_path, encoding="utf-8") as fh:
                content = fh.read()
        if not content.strip():
            content = _DEFAULT_SECURITY_MD
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError:
        return "skipped"
    if copy is not None:
        say(copy["security-seeded"])
    return "seeded"


# The engine's marketing landing-front marker — an invisible HTML comment the template's root README LEADS WITH
# (and the product starter deliberately does NOT carry). It is the ONE positive-match the README seed/replace fires
# on, so the replace can only ever touch the engine's own landing page, never operator content. The landing front LEADS with this marker (the recognizer requires it at the file's start, not
# merely somewhere inside) — so a README that only mentions the marker in passing is NOT a match and is preserved
# (the conservative preserve-on-any-doubt law; #134 authors the marketing copy BELOW this leading marker). Stable
# across marketing-copy rewrites; a fingerprint would instead need updating on every wording tweak or the replace
# silently dies. The starter carrying no marker makes a re-run a true no-op (the engine never re-touches the root
# README after instantiation).
_MARKETING_SEED_MARKER = "<!-- engine-template:landing-front -->"

# A minimal, valid product-starter README used only when the maintainer's template seed is absent or empty — never
# an error (the same fallback shape as _DEFAULT_SECURITY_MD). It carries the required-spine disclosure in plain
# operator language (no maintainer vocabulary) and the no-automated-style-floor gap, and it carries NO marker.
_DEFAULT_README_MD = (
    "# Your project\n\n"
    "<!-- Replace this with your project's name and a one-line description of what it does. This starter was\n"
    "     placed here when you set up the Engine; it is yours to edit freely as your project takes shape. -->\n\n"
    "A new project, set up to be built with the help of an AI engine.\n\n"
    "## What the engine does for this project\n\n"
    "A few things are built in and always on — they are part of how the engine works, not add-ons you chose:\n\n"
    "- **It remembers across sessions.** The engine keeps track of what you have decided and why, so a new "
    "session starts from where the last one left off instead of from a blank slate.\n"
    "- **It keeps your work safe.** Your history and progress are preserved between sessions, so a fresh start "
    "never loses what came before.\n"
    "- **It works in a steady routine and checks itself.** The engine follows a consistent way of working and "
    "runs its own checkups to catch problems early.\n\n"
    "One thing is **not** yet built in: there is no automatic check of your code's style or formatting. A future "
    "add-on called `clean-code` is planned to fill that gap; until it lands, code style is not checked for you "
    "automatically.\n\n"
    "You own this file — edit it freely.\n"
)


def _is_marketing_seed(text) -> bool:
    """True iff `text` LEADS WITH the engine's marketing landing-front marker — the ONE positive-match predicate
    that fires the README replace. Requiring the marker at the START (not merely somewhere inside) is the
    conservative reading of "the slot still holds the engine's recognizable marketing seed": a README that only
    mentions the marker in passing is NOT a match and is preserved. Anything else — operator content, an
    already-seeded starter, or an absent/unreadable/empty file passed as "" or None — is not a match either, so the
    README is left exactly as it is. The starter the engine writes deliberately carries no marker → a re-run is a
    no-op (preserve on any doubt; operator content is structurally never clobbered)."""
    return bool(text) and text.lstrip().startswith(_MARKETING_SEED_MARKER)


def _seed_readme(say, copy=None) -> str:
    """Seed the product's own starter README over the engine's marketing landing front — the seed-then-own pattern,
    the same SHAPE and DISCLOSURE as _seed_security, but REPLACE-IFF-MARKETING-SEED instead of copy-if-absent. At rest in the template the root README is the engine's marketing landing front; "Use this
    template" copies it to a generated repo's root, which topology reserves for the product. Apply replaces it with
    a product starter, but ONLY where the current root README still carries the engine's own recognizable marketing
    marker (_is_marketing_seed). Conservative positive-match-or-preserve: greenfield (the traveled marketing front)
    -> replaced; brownfield (the product's own README), a re-run (the starter, no marker), or an absent/unreadable
    file -> left exactly as it is, returns "present". The starter comes from .engine/provisioning/readme-seed.md; an
    absent or empty seed yields a minimal default, never an error. The seeded README is operator-owned product config
    (in no `provides`, at the repo root), preserved across the upgrade overlay. Discloses, in plain language, WHAT
    CHANGED AND WHY IT IS THEIRS — only when it actually replaces (never silent, never on a no-op). Paths are
    validate.ROOT-relative, so a redirected demo/test touches only the fixture, never the real tree."""
    target = os.path.join(validate.ROOT, "README.md")
    try:
        current = ""
        if os.path.isfile(target):
            with open(target, encoding="utf-8") as fh:
                current = fh.read()
    except OSError:
        return "present"                          # unreadable -> preserve on any doubt (never replace)
    if not _is_marketing_seed(current):
        return "present"                          # operator content / a seeded starter / absent -> untouched
    seed_path = os.path.join(validate.ROOT, ".engine", "provisioning", "readme-seed.md")
    try:
        content = ""
        if os.path.isfile(seed_path):
            with open(seed_path, encoding="utf-8") as fh:
                content = fh.read()
        if not content.strip():
            content = _DEFAULT_README_MD
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError:
        return "skipped"
    if copy is not None:
        say(copy["readme-seeded"])
    return "replaced"


# The engine's own shipped template LICENSE and its recognizer live in the permanent `license_seeds` module,
# shared with the standing foreign-`LICENSE`-seed detector (license_health.py). That detector outlives this
# FIRST-RUN-RETIRED file, so the seed set cannot live here (#471). These aliases keep the
# first-run clear's call sites — and the retiring parity test that binds the seed to the committed root
# LICENSE — unchanged. The recognizer is a whitespace-normalized FULL-TEXT match: an adopter who chose
# another license, or who kept this text but renamed the Licensor/copyright to themselves, is PRESERVED.
_TEMPLATE_LICENSE_SEED = license_seeds.CURRENT_SEED
_is_template_license = license_seeds.recognize


def _seed_license(say, copy=None) -> str:
    """Clear the engine's OWN traveled template LICENSE at greenfield first-run — the reconcile-the-root pattern, the
    same SHAPE and DISCLOSURE as the README/SECURITY seeds, but CLEAR-IFF-TEMPLATE-SEED and seeding NO replacement (a
    license is the adopter's legal choice, never the engine's to make). At rest the
    template ships a stock Apache-2.0 + Commons Clause LICENSE (its author's copyright) so the public template repo is legally usable; "Use
    this template" copies it to a generated repo's root, where it would govern the ADOPTER's product. Apply DELETES
    it, but ONLY where the current root LICENSE still positively matches the engine's own shipped template-license
    seed (_is_template_license: a whitespace-normalized full-text match against the shipped seed). Conservative
    clear-or-preserve: greenfield (the traveled template license) -> removed; brownfield (the product's own license —
    even one that keeps this text but names a different Licensor), a re-run (the slot is now empty), or an absent/unreadable
    file -> left exactly as it is, returns "present". No replacement is written. The root LICENSE is product-owned
    config (in no `provides`, at the repo root, outside .engine/ so the ownership leg never reaches it); the engine
    never re-touches it after instantiation. Discloses, in plain language, WHAT WAS REMOVED AND WHY — only when it
    actually clears (never silent, never on a no-op). Paths are validate.ROOT-relative, so a redirected demo/test
    touches only the fixture, never the real tree."""
    target = os.path.join(validate.ROOT, "LICENSE")
    try:
        current = ""
        if os.path.isfile(target):
            with open(target, encoding="utf-8") as fh:
                current = fh.read()
    except OSError:
        return "present"                          # unreadable -> preserve on any doubt (never delete)
    if not _is_template_license(current):
        return "present"                          # the product's own license / empty slot / absent -> untouched
    try:
        os.remove(target)
    except OSError:
        return "skipped"
    if copy is not None:
        say(copy["license-cleared"])
    return "cleared"


# The genesis marker the construction-governance CLAUDE.md LEADS WITH ("# engine-template — construction
# governance …", its first line) — the ONE positive-match the floor swap fires on (#272). Kept identical to the
# construction-repo sentinel's marker (memory_pointer_public_safety_check._CONSTRUCTION_MARKER), so the two key
# on the SAME phrase — a parity test in the (retiring) test_instantiator.py binds the string. The recognizer
# anchors the marker to the LEADING heading line (below), a strictly NARROWER match than the sentinel's
# substring-anywhere test: the swap therefore never fires on a file the sentinel would not also call the
# construction repo, so the two can never disagree in the dangerous (clobber) direction.
_CONSTRUCTION_CLAUDE_MARKER = "construction governance"
_ROOT_AGENTS_REL = "AGENTS.md"                    # the Codex floor pair mirrors the Claude one: both
_DEPLOYED_AGENTS_FLOOR_REL = "AGENTS.deployed.md"  # construction files lead with the same marker heading
_ROOT_CLAUDE_REL = "CLAUDE.md"
_DEPLOYED_FLOOR_REL = "CLAUDE.deployed.md"
# The floor is written as a keyed, comment-fenced engine section (the Markdown/HTML style) so it can later
# co-exist inside a brownfield operator's own CLAUDE.md and be keyed-merged on upgrade rather than
# replaced wholesale (#234). Same fence id the upgrade overlay's keyed-merge uses.
_FLOOR_FENCE = "floor"


def _is_construction_claude(text) -> bool:
    """True iff `text` is the engine's traveled CONSTRUCTION-governance CLAUDE.md — the positive-match predicate
    that fires the floor swap: its LEADING heading (the first non-empty line) carries the construction marker
    (case-insensitive). Anchoring to the first line — not anywhere in the file — is the conservative reading of
    "this IS the engine's construction file" (the same lead-the-file discipline _is_marketing_seed uses for the
    README): an operator's own CLAUDE.md that merely MENTIONS the phrase in its body is NOT a match and is
    preserved, never clobbered. An already-swapped floor (no such heading), operator content, or an
    absent/unreadable/empty file passed as "" or None is likewise not a match. Greenfield ("Use this template"
    copies the construction file, whose H1 carries the marker, to the generated repo's root) -> match ->
    swapped; everything else -> preserved."""
    if not text:
        return False
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    return _CONSTRUCTION_CLAUDE_MARKER in first.lower()


def _root_is_construction() -> bool:
    """True iff this repo's root CLAUDE.md is still the engine's construction-governance file — i.e. this is the
    workshop, or a generated repo whose first-run apply has not yet swapped the deployed floor in. The
    verify/retire CLI guards key off this: a legitimate first run reaches verify/retire only AFTER apply swapped
    the floor (so the root file is the deployed floor, not construction — the guard passes through to the real
    verb), while in the workshop the construction file is present, so the one-time verbs refuse rather than
    re-fire setup or (for retire) self-delete the real tooling. Reuses the conservative leading-heading
    predicate; an absent, unreadable, or non-text file reads as not-construction (no refusal — never block on
    doubt; UnicodeDecodeError is caught alongside OSError so a binary root file degrades, it does not crash)."""
    try:
        with open(os.path.join(validate.ROOT, _ROOT_CLAUDE_REL), encoding="utf-8") as fh:
            return _is_construction_claude(fh.read())
    except (OSError, UnicodeDecodeError):
        return False


def _seed_deployed_floor(say, copy=None) -> str:
    """Swap the engine's thin deployed floor in as the generated repo's root CLAUDE.md, retiring the traveled
    construction-governance CLAUDE.md — the reconcile-the-root pattern, the same SHAPE and DISCLOSURE as the
    README/LICENSE seeds, but SWAP-IFF-CONSTRUCTION-SEED (#272). "Use this template" copies BOTH the internal
    construction-governance CLAUDE.md (which governs building the template itself) AND the thin deployed floor
    (CLAUDE.deployed.md) into a generated repo; topology reserves the root CLAUDE.md slot for the engine's
    deployed floor, not its build scaffolding. Apply OVERWRITES CLAUDE.md with the floor's content and DELETES
    the consumed CLAUDE.deployed.md, but ONLY where the current root CLAUDE.md still positively matches the
    engine's own construction seed (_is_construction_claude). Conservative swap-or-preserve: greenfield (the
    traveled construction file) -> swapped; brownfield (the operator's own CLAUDE.md, no marker), a re-run (the
    floor is already in place, no marker), or an absent/unreadable file -> left exactly as it is, returns
    "present". NEVER STRANDS: if CLAUDE.md is the construction file but the floor source is missing/unreadable/
    empty, it preserves the construction file rather than deleting it with nothing to put in its place (returns
    "present") — impossible on a real template copy, but the safe degrade. The root CLAUDE.md is engine-owned
    foundation infrastructure; the operator's OWN stance lives in their codes of
    conduct (preserved across an overlay), so the disclosure points customization at /engine-conduct rather than
    inviting edits to CLAUDE.md. Discloses, in plain language, WHAT CHANGED — only when it actually swaps (never
    silent, never on a no-op). Paths are validate.ROOT-relative, so a redirected demo/test touches only the
    fixture, never the real tree."""
    status = _swap_floor(_ROOT_CLAUDE_REL, _DEPLOYED_FLOOR_REL)
    if status == "swapped" and copy is not None:
        say(copy["claude-floor-seeded"])
    return status


def _seed_agents_floor(say, copy=None) -> str:
    """The AGENTS.md half of the floor swap — the SAME swap-iff-construction mechanics over the Codex
    floor pair (AGENTS.md / AGENTS.deployed.md). Both runtime floors travel as construction files whose
    leading heading carries the construction marker, and each generated repo swaps each in independently
    (a repo may adopt with either floor already customized). One disclosure covers the pair — the
    claude-floor copy already frames "the working guide"; this one only fires when the Codex floor
    ALONE swapped, so setup never narrates the same swap twice."""
    status = _swap_floor(_ROOT_AGENTS_REL, _DEPLOYED_AGENTS_FLOOR_REL)
    if status == "swapped" and copy is not None:
        say(copy["agents-floor-seeded"])
    return status


def _swap_floor(root_rel: str, source_rel: str) -> str:
    """The shared floor-swap mechanics for one (root file, deployed source) pair — see
    _seed_deployed_floor's contract: swap-iff-construction, never-strand, preserve on any doubt."""
    root_path = os.path.join(validate.ROOT, root_rel)
    floor_path = os.path.join(validate.ROOT, source_rel)
    try:
        current = ""
        if os.path.isfile(root_path):
            with open(root_path, encoding="utf-8") as fh:
                current = fh.read()
    except OSError:
        return "present"                          # unreadable -> preserve on any doubt (never overwrite)
    if not _is_construction_claude(current):
        return "present"                          # operator content / already-swapped / absent -> untouched
    try:
        floor = ""
        if os.path.isfile(floor_path):
            with open(floor_path, encoding="utf-8") as fh:
                floor = fh.read()
    except OSError:
        return "present"                          # floor unreadable -> never strand: keep the existing file
    if not floor.strip():
        return "present"                          # no floor source to swap in -> preserve (never strand)
    floor_lines = floor.split("\n")
    if floor_lines and floor_lines[-1] == "":
        floor_lines = floor_lines[:-1]            # drop the trailing-newline empty element; fence re-terminates
    try:
        # Write the floor wrapped in the engine `floor` fence (greenfield converges on the keyed model the
        # upgrade keyed-merge and brownfield coexistence both use). The whole file is the engine block on
        # greenfield; on a brownfield arrival (#234 6b) the same fence is inserted into the operator's file.
        fenced = wiring.fence_apply("", _FLOOR_FENCE, floor_lines, style=wiring.MD_FENCE)
        with open(root_path, "w", encoding="utf-8") as fh:
            fh.write(fenced)
        os.remove(floor_path)                     # consume the now-redundant source
    except (OSError, wiring.WiringError):
        return "skipped"
    return "swapped"


# The genesis cursor a brand-new project starts from (state.v1 field descriptions: both
# standing-situation pointers null, the debt count zero, the register null until the project sets one).
# Provably schema-valid (a test binds it to state.v1.json), so a re-seeded generated repo never ships a
# cursor read_state would then refuse.
_GENESIS_CURSOR = {
    "schema_version": 1,
    "standing_situation": {"milestone": None, "phase": None},
    "integration_debt": {"open_count": 0, "as_of": None, "register": None},
}
_STATE_REL = os.path.join(".engine", "state", "state.json")


def _seed_state(say, copy=None) -> str:
    """Reset a generated repo's committed state cursor to genesis at first-run, so a fresh project does
    not inherit the engine's OWN construction cursor — the workshop phase, debt count, and register URL that
    "Use this template" copies in verbatim (it would otherwise surface on an offline/degraded first boot and
    sit in the committed file). Seed-then-own, but RISK-ORIENTED for state: the traveled cursor carries NO
    operator content at first-run (it is engine scaffolding), so the danger is UNDER-matching (leaving the
    workshop's data in place), never over-matching.

    Recognition is STRUCTURAL and rename-immune (no hardcoded upstream slug): a cursor is traveled/foreign
    when its `integration_debt.register` is a non-empty URL that does NOT name THIS repo's own origin
    (boot.repo_slug()). The construction repo's own cursor names its own origin -> preserved; a genesis cursor
    (null register) -> a no-op; an operator who has re-grounded (register names their repo) -> preserved.
    BELT: _root_is_construction() (this seed is ordered AFTER _seed_deployed_floor, so a generated repo's root
    is already the swapped floor -> the guard passes; the real workshop, or an un-redirected test whose root is
    the construction file -> preserved), which also makes the unknown-origin fallback safe. Fail-open: an
    unreadable/malformed/wrong-shape cursor is preserved, never crashing the phase. Paths are
    validate.ROOT-relative, so a redirected demo/test touches only the fixture. Discloses only on an actual
    reset (never silent, never on a no-op)."""
    if _root_is_construction():
        return "present"                          # workshop / pre-swap -> never reset (belt: protects the real cursor)
    path = os.path.join(validate.ROOT, _STATE_REL)
    try:
        with open(path, encoding="utf-8") as fh:
            register = json.load(fh)["integration_debt"]["register"]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return "present"                          # unreadable / malformed / wrong shape -> preserve, never crash
    if not isinstance(register, str) or not register.strip():
        return "present"                          # already genesis (null / empty register) -> idempotent no-op
    own = boot.repo_slug()
    # SEGMENT-anchored match (not a bare substring): a register URL is `…/OWNER/REPO/issues?…`, so the own
    # slug must appear as a whole `/OWNER/REPO/` path segment (or trailing) — else `acme/engine` would falsely
    # match `…/acme/engine-template/…` and wrongly PRESERVE a borrowed cursor (the leak this match exists to prevent).
    if own and (f"/{own}/" in register or register.rstrip("/").endswith(f"/{own}")):
        return "present"                          # register names THIS repo -> operator's own cursor -> preserve
    try:                                          # foreign / traveled register (or unknown origin past the belt) -> reset
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_GENESIS_CURSOR, fh, indent=2)
            fh.write("\n")
    except OSError:
        return "skipped"                          # unwritable -> never strand the phase
    if copy is not None:
        say(copy["state-reseeded"])
    return "reseeded"


_PRODUCT_VERSION_REL = "product-version.json"
_PRODUCT_VERSION_SEED = "0.0.0"


def _seed_product_version(say, copy=None) -> str:
    """Seed the deployed repo's own PRODUCT version file (#516) at first-run, so a deployment inherits a
    working product-release lane: once deployed, the engine's release workflow cuts THIS file's version, not the
    engine's. A product-OWNED root file (eADR-0007 — it lives in product territory and SURVIVES an engine
    uninstall, unlike anything under .engine/). Seed-then-own: seed-iff-absent, so a re-run, or an operator who
    already set a product version, is a no-op. BELT: `_root_is_construction()` — this runs AFTER
    `_seed_deployed_floor`, so a generated repo's root is already the swapped floor and the guard passes; the
    construction workshop (or an un-redirected test whose root is still the construction file) never gets a
    product file, because there the engine IS the product and cuts the engine version. Fail-open: an unwritable
    path never strands the phase. Discloses only on an actual seed (never on a no-op). Product-mode does not
    strictly NEED the file — a deployed repo without it still cuts a product release, creating it on the first
    cut — but seeding gives a new deployment a visible, discoverable starting point from day one."""
    if _root_is_construction():
        return "present"                          # workshop / pre-swap -> the engine is the product; never seed
    path = os.path.join(validate.ROOT, _PRODUCT_VERSION_REL)
    if os.path.exists(path):
        return "present"                          # already seeded / operator-owned -> idempotent no-op
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": _PRODUCT_VERSION_SEED}, fh, indent=2)
            fh.write("\n")
    except OSError:
        return "skipped"                          # unwritable -> never strand the phase
    if copy is not None:
        say(copy["product-version-seeded"])
    return "seeded"


def _apply_substrates(say, copy=None) -> dict:
    """STEP 5 — initialize the kept set's committed substrates (runs AFTER the runtime materializes). Today:
    re-derive the knowledge graph (idempotent), confirm the state seed is present AND reset a traveled
    construction cursor to a clean genesis start (AFTER the floor swap so its construction-repo belt is
    valid), seed the operator's codes-of-conduct override from the template seed, seed a root SECURITY.md
    disclosure channel, seed the
    product's own starter README over the engine's marketing landing front, clear the traveled template
    LICENSE, and swap the thin deployed floor in as the root CLAUDE.md (retiring the traveled construction
    file) (all disclosed in plain language). The root reconciles sit here — co-located, AFTER the runtime — a
    deliberate mirror of the as-built conduct-seed placement: a pure file write/delete has no runtime dependency,
    and on a tool-runtime halt the phase never reaches here, so each lands on the resume (every one is idempotent —
    copy-if-absent for conduct/security, replace-iff-marketing-seed for the README, clear-iff-template-seed for the
    LICENSE — so a re-run is a no-op). The LICENSE clear is a full-text match to the shipped
    template seed (whose Commons Clause header names the template author as Licensor), so it must run before any
    step that could rewrite that text with operator identity; none does today (the only identity renderer,
    _apply_codeowners, touches CODEOWNERS only), so STEP 5 is safe — a forward-defensive invariant. The
    graph path is bound at import (knowledge_gen), so the demo redirects it AND we pass it explicitly — a redirected
    run never rewrites the real graph. Memory-backup setup is owed to the memory module (backup_vault), not done here."""
    result = {"step": "substrates", "status": "done"}
    try:
        knowledge_gen.generate(path=knowledge_gen.GRAPH_PATH)
        result["knowledge"] = "derived"
    except Exception as exc:  # noqa: BLE001 — degrade-and-disclose, never crash the phase
        result["knowledge"] = f"skipped ({type(exc).__name__})"
        result["status"] = "degraded"
    result["state_present"] = os.path.isfile(os.path.join(validate.ROOT, ".engine", "state", "state.json"))
    result["conduct"] = _seed_conduct(say, copy)
    result["security"] = _seed_security(say, copy)
    result["readme"] = _seed_readme(say, copy)
    result["license"] = _seed_license(say, copy)
    result["claude_floor"] = _seed_deployed_floor(say, copy)
    result["agents_floor"] = _seed_agents_floor(say, copy)
    result["state"] = _seed_state(say, copy)   # AFTER the floor swap (its construction-repo belt reads the swapped root)
    result["product_version"] = _seed_product_version(say, copy)   # AFTER the floor swap (same belt), #516
    return result


def _apply_wires(say) -> dict:
    """STEP 6 — install EVERY kept module's wiring: the hooks (boot, the exploration write-gate, the close
    gate, the commit-boundary refresh), the knowledge query server, and the cache ignores. Until this runs a
    generated repo's settings carry no engine hooks, so the engine is inert — this is the step that turns it
    on. Reuses wiring.apply_all exactly as module-add does; insert-iff-absent
    (idempotent)."""
    applied = []
    for _p, m in module_coherence.discover_manifests():
        for f in wiring.apply_all(m.get("wires") or []):
            applied.append(validate.fmt(f))
    return {"step": "wires", "status": "done", "applied": applied}


def _persist_control_plane_marker(root, marker) -> None:
    """Record the control-plane outcome in engine.json (under `control_plane`) so a later clean removal can
    reverse EXACTLY what the arrival did to branch protection — whether the engine created its own ruleset or
    augmented a pre-existing PRODUCT one, and which exact pieces it added. A read-modify-write (confirm wrote
    the manifest earlier this phase). Best-effort: a write failure never fails the gate — it only means a
    later de-bootstrap falls back to a bounded, name-only strip."""
    if not marker:
        return
    path = _engine_manifest_path(root)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    data["control_plane"] = marker
    try:
        _write_json(path, data)
    except OSError:
        return


def _apply_control_plane(control_transport, gh_refresh, control_issues, say, copy, repo=None, token=None,
                         root=None) -> dict:
    """STEP 7 — turn on the protected-branch review gate (the control-plane bootstrap, the permanent
    primitive). On a brownfield arrival this AUGMENTS the project's own branch-protection rule in place rather
    than creating a second; either way it records, in engine.json, exactly what it did so removal can reverse
    it. Degrades LOUD when the repo/sign-in/capability is unavailable (never fakes the gate). Apply ENDS
    regardless — the gate can be completed any time later, and boot keeps surfacing an unprotected repo. Every
    boundary is injected — the repo coordinates + token AND the GitHub transport — so tests/the demo run the
    real orchestration deterministically, independent of the ambient environment (e.g. CI's own token)."""
    repo = repo or boot.repo_slug()
    token = token or boot.gh_token()
    if not repo or not token:
        say(copy["control-plane-unavailable"])
        return {"step": "control-plane", "status": "degraded", "detail": "no project/sign-in", "protected": False}
    cp = bootstrap.ControlPlane(repo, token, transport=control_transport, refresh_fn=gh_refresh,
                                issues=control_issues)
    result = cp.apply(branch=boot.PROTECTED_BRANCH, announce=say)
    say(bootstrap.render(result))
    _persist_control_plane_marker(root, result.marker)
    return {"step": "control-plane", "status": result.status, "mode": result.mode,
            "protected": result.is_protected()}


def _apply_actions_enablement(control_transport, say, copy, repo=None, token=None) -> dict:
    """STEP 7b — the one-time GitHub Actions enablement only the OWNER can perform (#514). A repo created
    via "Use this template" has workflow runs gated behind the owner's explicit Actions-tab click; until
    then the required checks the control plane just bound never start, and no pull request — including the
    setup one — can merge. The API cannot perform that click, and the deployment evidence shows the
    permissions endpoint can report enabled while the UI gate still blocks — so this step TELLS,
    unconditionally, and never silently automates. NO detection buys silence: the review of this step
    proved every candidate signal dishonest in exactly the deadlock state — GitHub-managed scan runs
    (CodeQL/Dependabot) appear in the runs listing while real workflows are still gated, and any run
    history proves Actions worked once, never that it can run now. The message itself carries the
    already-on branch ("if the tab shows no enable button, you're done"), so telling is never misleading.
    The `control_transport` parameter is accepted for signature symmetry with the sibling steps but unused."""
    repo = repo or boot.repo_slug()
    token = token or boot.gh_token()
    if not repo or not token:
        return {"step": "actions-enablement", "status": "skipped", "detail": "no project/sign-in"}
    say(copy["actions-enablement"])
    return {"step": "actions-enablement", "status": "operator-step-told"}


def _apply_security_toggles(control_transport, say, copy, repo=None, token=None) -> dict:
    """STEP 8 — turn on GitHub's NATIVE security features (secret scanning + push protection, code scanning,
    private vulnerability reporting) where the repository's tier supports them, branching on each call's
    status and disclosing the outcome in plain language. REUSES the operator-privileged transport the ruleset
    bootstrap already holds (no new capability) — the same injected `control_transport`. Degrades in place
    (never halts) and ADDS NO required merge check (alerts are advisory). When the repo/sign-in is
    unavailable (e.g. the construction repo) it skips quietly — the protected-branch gate is the real
    guarantee, and these are advisory upgrades that can be turned on any time later."""
    repo = repo or boot.repo_slug()
    token = token or boot.gh_token()
    if not repo or not token:
        return {"step": "security-floor", "status": "skipped", "detail": "no project/sign-in"}
    floor = security_floor.SecurityFloor(repo, token, transport=control_transport)
    toggles = floor.apply(announce=say)
    status = "applied" if all(t.is_good() for t in toggles) else "degraded"
    return {"step": "security-floor", "status": status,
            "toggles": {t.key: t.state for t in toggles}}


def _github_projects_sync_present() -> bool:
    """True iff the github-projects-sync module is installed (its manifest is present). Read at the
    repo-behavior step, AFTER the deselect step has already removed unpicked modules — so it reflects the
    deployer's actual selection. Fails toward RETAIN (return True) when the present set can't be read, so an
    unreadable module tree never turns off project boards a retained module might need."""
    try:
        return any((m or {}).get("id") == "github-projects-sync"
                   for _path, m in module_coherence.discover_manifests())
    except Exception:  # noqa: BLE001 — can't tell -> keep project boards on (never turn off on doubt)
        return True


def _apply_repo_behavior(control_transport, say, copy, repo=None, token=None, brownfield=False) -> dict:
    """STEP 10 — the repository-behavior settings a new engine repo should carry (#541). Turns ON
    delete-branch-on-merge, the pull-request update button, and Dependabot alerts + automatic security-fix
    pull requests; and — on a FRESH repo only (item 4) — turns OFF the project wiki, and project boards when
    the github-projects-sync module is not installed (retained if it is). Same posture as the security floor
    beside it: the same operator-privileged transport (no new capability), verify-after-write,
    degrade-never-fake with a plain-language disclosure, augment-never-override, and never a required merge
    check. On a BROWNFIELD arrival the turn-offs are skipped — the operator's own wiki/projects choices are
    left untouched, since hiding an active project's wiki would be an override. Skips quietly when the
    repo/sign-in is unavailable (e.g. the construction repo)."""
    repo = repo or boot.repo_slug()
    token = token or boot.gh_token()
    if not repo or not token:
        return {"step": "repo-behavior", "status": "skipped", "detail": "no project/sign-in"}
    leg = repo_behavior.RepoBehavior(repo, token, transport=control_transport)
    # Greenfield-only turn-offs (augment-never-override on brownfield); project boards stay on when the
    # board-sync module is retained.
    disable_wiki = not brownfield
    disable_projects = (not brownfield) and not _github_projects_sync_present()
    toggles = leg.apply(announce=say, disable_wiki=disable_wiki, disable_projects=disable_projects)
    status = "applied" if repo_behavior.all_good(toggles) else "degraded"
    return {"step": "repo-behavior", "status": status,
            "toggles": {t.key: t.state for t in toggles}}


def apply(*, root=None, announce=None, home_reader=None, settings_path=None, uv_present=None,
          uv_installer=None, uv_runner=None, consent=None, control_transport=None, gh_refresh=None,
          control_issues=None, control_repo=None, control_token=None, handle=None, brownfield=False) -> dict:
    """The apply phase: run the eleven ordered steps against the confirmed manifest. Refuses (no change) when
    the manifest is absent — apply presupposes a confirmed selection. The handle is the passed one, else the
    one the manifest stored. Returns a step ledger: {refused, halted, steps:[…]}. A degraded tool-runtime
    sets `halted` and the remaining steps are not attempted (they presuppose the runtime); every other step
    degrades in place. Apply does NOT verify, pause, or retire. Every external boundary is
    injectable, so this runs the REAL control flow under a fixture with nothing real touched."""
    say = announce if announce is not None else (lambda text: print(text))
    copy = load_copy()
    manifest = module_coherence.load_engine_manifest()
    if not manifest:
        return {"refused": True, "reason": "not-confirmed", "halted": False, "steps": []}
    handle = handle if handle is not None else manifest.get("handle")
    steps = [_apply_delete_unselected(manifest, say),
             _apply_foundation_ignores(say),
             _apply_codeowners(handle, say, copy),
             _apply_plan_mode(home_reader, settings_path, consent, say, copy)]
    runtime = _apply_tool_runtime(uv_present, uv_installer, uv_runner, consent, say, copy)
    steps.append(runtime)
    if runtime.get("halt"):
        return {"refused": False, "halted": True, "steps": steps}
    steps.append(_apply_substrates(say, copy))
    steps.append(_apply_wires(say))
    steps.append(_apply_control_plane(control_transport, gh_refresh, control_issues, say, copy,
                                      repo=control_repo, token=control_token, root=root))
    steps.append(_apply_actions_enablement(control_transport, say, copy,
                                           repo=control_repo, token=control_token))
    steps.append(_apply_security_toggles(control_transport, say, copy,
                                         repo=control_repo, token=control_token))
    steps.append(_apply_repo_behavior(control_transport, say, copy,
                                      repo=control_repo, token=control_token, brownfield=brownfield))
    return {"refused": False, "halted": False, "steps": steps}


# ==== VERIFY + RETIRE — the first-run lifecycle close =================================
#
# After apply installs the selection and turns the guardrails on, VERIFY confirms the result is consistent
# and RETIRE tidies the one-time setup assets away. Both run in the SAME system-python instantiator process
# as apply (they reuse only stdlib + sibling tools — check_coherence and knowledge_gen.generate read JSON +
# walk the tree, never yaml/jsonschema), so they start on a bare adopter machine like the rest of the phase.
#
# The locked ordering: a HARD consistency finding at
# verify PAUSES — the engine never proceeds on something inconsistent — and surfaces, in plain language, what
# is wrong and the two next actions (fix and re-run — resumable from the checkpoint, nothing lost — or stop
# and report). Retire then self-deletes the orchestrator + first-run assets, but ONLY on a consistent setup:
# a hard finding blocks the (irreversible) retire, while a merely deferred review gate does NOT (deferred is
# the common path — boot keeps surfacing it, the bootstrap primitive survives retirement). DEGENERACY
# (unchanged): none of this runs in the construction repo; the finish-demo runs the REAL logic against a
# throwaway fixture and asserts this repo's files are byte-for-byte unchanged.

# The first-run-only assets retire removes once setup is sound — repo-relative, joined to validate.ROOT at
# call time so a redirected demo/test deletes only the fixture, never the real tree. PRESERVED (not listed):
# the shared catalog reader `module_catalog.py` (used by /engine-help) + its test, the catalog data + schema,
# and every permanent provisioning primitive.
_FIRST_RUN_ASSET_FILES = (
    ".engine/tools/instantiator.py",
    ".engine/tools/test_instantiator.py",
    # The SECURITY.md-seed test + its demo exercise first-run-only machinery (instantiator._seed_security) and
    # import the retired instantiator / test_instantiator, so they retire in the SAME pass — else they survive
    # into a generated repo and abort its first `unittest discover` at collection (the first-run reference-closure
    # invariant). The .engine/provisioning/first-run-assets.json manifest mirrors
    # this list (parity-tested) so the closure check can read the retired set without importing the instantiator.
    ".engine/tools/test_security_seed.py",
    ".engine/tools/demo_security_seed.py",
    # demo_first_run_guard exercises retire()'s own fail-closed safety guard against a redirected fixture; it
    # imports the retiring instantiator, so it retires in the SAME pass (else it dangles a reference to a
    # removed module — the first-run reference-closure invariant). Mirrored in first-run-assets.json (parity).
    ".engine/tools/demo_first_run_guard.py",
    # Construction-phase behavioral demos: falsifications over real engine surfaces that are maintainer build
    # evidence, not operator capability — so they retire here rather than travel into a generated repo
    # (a construction demo does not travel unless promoted by an explicit logged
    # decision). Unlike the security-seed pair above, these reference no retiring machinery; they retire only
    # so the construction set does not ship as a junk drawer. Each is mirrored in first-run-assets.json
    # (parity-tested). The per-tool `demo` subcommand convention is the promoted standing form, decided upstream.
    ".engine/tools/demo_467_deployment_eadr_namespace.py",
    ".engine/tools/demo_audit_concern_list.py",
    ".engine/tools/demo_audit_digest.py",
    ".engine/tools/demo_boot_slice.py",
    ".engine/tools/demo_ci_author_exempt.py",
    ".engine/tools/demo_derived_reconcile.py",
    ".engine/tools/demo_focus_read.py",
    ".engine/tools/demo_reverse_adjacency.py",
    ".engine/tools/demo_remember_this.py",
    ".engine/tools/demo_release_pr_mergeable.py",
    ".engine/tools/demo_build_entry_depth_gate.py",
    ".engine/tools/demo_state_cursor_honesty.py",
    # #424 U13a: nine further construction demos brought into the census. Each is maintainer build evidence,
    # imported by NOTHING (no surviving tool or test reaches them) and wired to no operator capability, so they
    # retire here rather than travel into a generated repo. Five OTHER out-of-census demos are
    # deliberately NOT walled — demo_pr_reconcile (a subcommand delegate of the surviving pr_reconcile.py) and
    # demo_actionlint / demo_secret_scan / demo_security_floor / demo_first_run_reference_closure (each imported
    # by a traveling companion test): walling any of them would dangle that surviving reference (the reference-
    # closure invariant). The census-completeness check (engine/check/census-completeness) guards
    # this boundary going forward, so this set cannot silently drift again.
    ".engine/tools/demo_map_reachability.py",
    ".engine/tools/demo_audit_soft_findings.py",
    ".engine/tools/demo_audit_soft_promote.py",
    ".engine/tools/demo_boot_alarm_collapse.py",
    ".engine/tools/demo_hook_runner.py",
    ".engine/tools/demo_inbox_drain.py",
    ".engine/tools/demo_release_cut.py",
    ".engine/tools/demo_release_terminal.py",
    ".engine/tools/demo_release_product_mode.py",
    # #599 Slice 3: the migration-accumulation guard falsification. Engine-version cuts are construction-repo
    # activity (a deployed repo cuts products), so this is maintainer build evidence, imported by nothing —
    # retires here; the permanent regression lives in test_release_cut.MigrationAccumulation.
    ".engine/tools/demo_599c_migration_accumulation.py",
    ".engine/tools/demo_weakening_guard_narrowed_set.py",
    ".engine/tools/demo_memory_degradation_backup.py",
    ".engine/tools/demo_attention_live_dials.py",
    ".engine/tools/demo_boot_set_aside_readout.py",
    ".engine/tools/demo_contract_rate.py",
    ".engine/tools/demo_restore_migration_routing.py",
    ".engine/tools/demo_operator_backlog.py",
    ".engine/tools/demo_control_plane_labels.py",
    # #424 U13b — a KEEP disposition recorded on the census for memory_pointer_public_safety_check.py: it is
    # construction-scoped (self-no-ops outside this repo) yet is deliberately NOT retired here. Its check.json
    # (.engine/check/memory-pointer-public-safety.json) travels in validators-core `provides.check`, so retiring
    # the script would leave that check pointing at a deleted file — a first-run reference-closure violation (the
    # #411 trap). It ships and harmlessly no-ops in a generated repo; that is the correct fate, not an
    # oversight. (It is not a demo_*.py, so the census-completeness check does not enumerate it.)
    # The committed audit self-review digest is THIS template repo's own construction history — a generated repo
    # must not boot reporting a self-review it never ran, nor read the template's findings as its own. So it
    # retires at first-run: a fresh repo starts with no inherited digest (its absence is the honest "not yet
    # self-reviewed" state), and the audit cron writes a genuine one on its first run (#404). Unlike every
    # other asset here — which are construction-only and gone for good — this one stays in audit-library's
    # `provides` and is REGENERATED by the audit cron, so the first-run reference-closure check names it in its
    # narrow _REGENERATED_RETIRED_ASSETS allowlist (a surviving reference to it is not a dangling reference).
    # Mirrored in first-run-assets.json (parity-tested).
    ".engine/audits/audit-digest.md",
    ".engine/operations/first-run.md",
    ".engine/templates/first-run.md",
    # The engine's marketing banner — go-to-market content referenced only by the template's marketing landing
    # README, carried into every generated repo by "Use this template". Retired at first-run alongside the README
    # reseed (the product starter references no banner), so a generated repo carries no engine marketing residue
    # (#410). Retired as the specific FILE, not the `assets/`
    # directory: retire() ALSO runs on the brownfield "add the engine to an existing project" arrival, where
    # `assets/` is the OPERATOR's own directory (the engine provides no `assets/`) — a whole-dir rmtree there
    # would delete their files. Targeting just the banner is provenance-precise: it is absent in brownfield (a
    # no-op) and the only thing in `assets/` in greenfield, and an emptied `assets/` does not travel (git commits
    # no empty directory).
    "assets/engine_banner.jpg",
)
_FIRST_RUN_ASSET_DIRS = (os.path.join(".claude", "skills", "engine-setup"),
                         os.path.join(".agents", "skills", "engine-setup"))

# Every retirement target must be engine-owned. The `.engine/` subtree is wholly the engine's, even on a
# brownfield "add the engine to an existing project" arrival; `.claude/` and `.agents/` are NOT — there they
# are the operator's own tool namespaces (the same reason the banner is retired as a FILE, not the `assets/`
# directory, above). So outside `.engine/`, only these explicitly-sanctioned engine-owned paths may ever be
# retired; anything else is refused before any delete. A new non-`.engine/` retire entry must be added here on
# purpose — the manifest-safety test (TestRetireGuard.test_the_committed_first_run_asset_set_is_all_engine_owned) fails until it is, so the choice
# is deliberate, not accidental.
_SANCTIONED_NON_ENGINE_RETIRE_PATHS = frozenset({
    os.path.join(".claude", "skills", "engine-setup"),
    os.path.join(".agents", "skills", "engine-setup"),
    os.path.join("assets", "engine_banner.jpg"),
})


def _unsafe_retire_reason(base: str, rel: str):
    """Return a plain-language reason if `rel` is NOT a safe retirement target under `base`, else None.
    Fail-closed: a target is safe only if it resolves strictly inside the repo (realpath — so `..`, absolute,
    and symlink escapes are refused) AND is either under `.engine/` (engine-exclusive) or an explicitly
    sanctioned engine-owned path. This guards the irreversible `retire()` delete against a future manifest
    entry that would remove a brownfield adopter's own file or directory."""
    if not rel or os.path.isabs(rel):
        return f"retire target is empty or an absolute path: {rel!r}"
    norm = os.path.normpath(rel)
    if norm == os.curdir or norm == os.pardir or norm.startswith(os.pardir + os.sep):
        return f"retire target escapes the repository: {rel!r}"
    root = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base, rel))
    if target == root:
        return f"retire target is the repository root itself: {rel!r}"
    if not target.startswith(root + os.sep):
        return f"retire target resolves outside the repository: {rel!r}"
    norm_slash = norm.replace(os.sep, "/")
    if norm_slash.startswith(".engine/"):
        return None
    if norm in _SANCTIONED_NON_ENGINE_RETIRE_PATHS:
        return None
    return (f"retire target is not an engine-owned path (not under .engine/ and not a sanctioned engine "
            f"path): {rel!r}")


def _hard_findings() -> list:
    """The hard consistency findings over the present engine (the verify check). Pure read."""
    return [f for f in module_coherence.check_coherence("hard") if f.get("severity") == "hard"]


def _say_consistency_pause(say, copy, hard: list) -> None:
    """The plain-language pause: provisioning's frame around validation's per-finding message (the locked
    ownership split — validation owns the message, provisioning the first-run pause UX)."""
    say(copy["verify-paused"])
    for f in hard:
        say("  • " + validate.fmt(f))
    say(copy["verify-next-actions"])


def verify(*, root=None, announce=None, control_status=None) -> dict:
    """VERIFY — the consistency pause. Run the hard check over the installed engine; a hard finding PAUSES
    setup and surfaces what is inconsistent + the two next actions (fix and re-run, or stop and report). A
    clean check confirms setup is sound, and (when the review-gate outcome is known — passed in from apply's
    last step) states it plainly; standalone, it leaves the standing gate surfacing to boot rather than
    re-checking GitHub here. Pure read — re-running after a repair re-checks (resumable from the checkpoint).
    Returns {paused, findings, control, steps}."""
    say = announce if announce is not None else (lambda text: print(text))
    copy = load_copy()
    hard = _hard_findings()
    if hard:
        _say_consistency_pause(say, copy, hard)
        return {"paused": True, "findings": hard, "control": control_status,
                "steps": [{"step": "verify", "status": "paused", "issues": len(hard)}]}
    say(copy["verify-ok"])
    if control_status is not None:
        say(copy["verify-gate-on"] if control_status.get("protected") else copy["verify-gate-pending"])
    return {"paused": False, "findings": [], "control": control_status,
            "steps": [{"step": "verify", "status": "ok"}]}


def _drop_bytecode(base: str, stems) -> None:
    """Remove the stale .pyc companions of the deleted tools (hygiene — coherence already prunes
    __pycache__, so a leftover is harmless; we clean it so nothing stale lingers)."""
    import glob as _glob
    cache = os.path.join(base, ".engine", "tools", "__pycache__")
    for stem in stems:
        for p in _glob.glob(os.path.join(cache, stem + ".*")):
            try:
                os.remove(p)
            except OSError:
                pass


def retire(*, root=None, announce=None) -> dict:
    """RETIRE — the lifecycle close: once setup is consistent, tidy the one-time setup assets away and
    confirm completion. PRECONDITION (the locked 'never proceed on something inconsistent'): re-run the hard
    check and REFUSE if anything is inconsistent — an irreversible self-delete must not run on a broken setup
    (a merely deferred review gate does NOT block; that is the common path). Then delete the first-run assets
    (delete-if-present, so a resumed retire is safe), drop their stale bytecode, and re-derive the engine's
    saved information so the repo stays consistent after the tools are gone. PRESERVES the shared catalog
    reader, the catalog + schema, and every permanent primitive. Self-deletes its own source last; the
    running process keeps executing from memory (POSIX). Returns {refused, deleted, already_absent,
    preserved, graph, self_map, steps}."""
    say = announce if announce is not None else (lambda text: print(text))
    copy = load_copy()
    base = root or validate.ROOT
    # SAFETY FIRST — before the consistency check and before any deletion, validate the WHOLE retire set: no
    # target may resolve outside the engine's own paths. This guards the irreversible delete against a future
    # bad manifest entry that would remove a brownfield adopter's own file/directory, and refusing up front
    # (delete nothing) means no half-retired tree. A clean set is the common path; a hit is a code defect the
    # manifest-safety test already fails on in CI, so this is the fail-closed last line, not a routine branch.
    for _rel in _FIRST_RUN_ASSET_FILES + _FIRST_RUN_ASSET_DIRS:
        _unsafe = _unsafe_retire_reason(base, _rel)
        if _unsafe is not None:
            say("Setup cleanup was stopped for safety: an item in the cleanup list is not one of the engine's "
                f"own files or folders, so nothing was removed. The item was: {_rel}.")
            return {"refused": True, "reason": "unsafe-retire-target", "target": _rel,
                    "deleted": [], "already_absent": [], "preserved": [],
                    "graph": "unchanged", "self_map": "unchanged",
                    "steps": [{"step": "retire", "status": "refused", "unsafe": _rel}]}
    hard = _hard_findings()
    if hard:
        _say_consistency_pause(say, copy, hard)
        return {"refused": True, "reason": "inconsistent", "deleted": [], "already_absent": [],
                "preserved": [], "graph": "unchanged", "self_map": "unchanged",
                "steps": [{"step": "retire", "status": "refused", "issues": len(hard)}]}
    deleted, already = [], []
    for rel in _FIRST_RUN_ASSET_FILES:
        p = os.path.join(base, rel)
        if os.path.isfile(p):
            os.remove(p)
            deleted.append(rel)
        else:
            already.append(rel)
    for rel in _FIRST_RUN_ASSET_DIRS:
        p = os.path.join(base, rel)
        if os.path.isdir(p):
            import shutil
            shutil.rmtree(p)
            deleted.append(rel)
        else:
            already.append(rel)
    # Derive the retiring tool-module stems from the census itself (single source) — every `.engine/tools/*.py`
    # in _FIRST_RUN_ASSET_FILES, so this can never drift out of sync with the retire set (#424; it was a
    # hand-maintained partial copy before). Bytecode drop is best-effort hygiene, so a wider-but-correct set is
    # strictly fine.
    _tool_stems = tuple(os.path.splitext(os.path.basename(rel))[0]
                        for rel in _FIRST_RUN_ASSET_FILES
                        if rel.startswith(".engine/tools/") and rel.endswith(".py"))
    _drop_bytecode(base, _tool_stems)
    graph_status = "regenerated"
    try:
        knowledge_gen.generate(path=knowledge_gen.GRAPH_PATH)  # so the saved information no longer lists the
    except Exception as exc:  # noqa: BLE001 — degrade-and-disclose; never crash the close   # removed tools
        graph_status = f"skipped ({type(exc).__name__})"
    # The wiring map is the graph's sibling index and re-derives here for the same reason (#513): its
    # provides render filters retired-and-absent census entries, so without this regen the deployed repo
    # ships a map still advertising the files this step just deleted (the map otherwise refreshes only at
    # the adopter's own first commit). Re-derive-if-present, never create: a tree that carries no map (the
    # demo's minimal practice project) gets none — writing one there would orphan an unowned engine file.
    # Same degrade-and-disclose posture as the graph.
    map_status = "regenerated"
    try:
        if os.path.isfile(self_map.SELF_MAP_PATH):
            self_map.generate()
        else:
            map_status = "absent (nothing to re-derive)"
    except Exception as exc:  # noqa: BLE001
        map_status = f"skipped ({type(exc).__name__})"
    preserved = [".engine/tools/module_catalog.py", ".engine/tools/test_module_catalog.py",
                 ".engine/schemas/provisioning-catalog.v1.json", ".engine/provisioning/module-catalog.json"]
    say(copy["retire-success"])
    return {"refused": False, "deleted": deleted, "already_absent": already, "preserved": preserved,
            "graph": graph_status, "self_map": map_status,
            "steps": [{"step": "retire", "status": "done", "deleted": deleted}]}


# ---- demo (mutation-free, real logic, fixture boundary) ---------------------------------------

@contextlib.contextmanager
def _redirect_root(root: str):
    """Point every ROOT-derived WRITE path at a throwaway fixture tree, restore on exit (the demo/test
    idiom). Beyond validate's ROOT/ENGINE_DIR/CATALOG_PATH, the wiring library's path constants AND
    knowledge_gen's graph paths are bound at import time, so they are redirected EXPLICITLY — without this an
    apply step would escape the fixture and write the real `.claude/settings.json` / `.mcp.json` / `.gitignore`
    / CODEOWNERS / knowledge graph (the isolation guarantee the apply demo then asserts byte-for-byte)."""
    saved = (validate.ROOT, validate.ENGINE_DIR, validate.CATALOG_PATH,
             wiring.SETTINGS_PATH, wiring.MCP_PATH, wiring.GITIGNORE_PATH, wiring.CATALOG_PATH,
             wiring.CODEX_HOOKS_PATH, wiring.CODEX_CONFIG_PATH,
             knowledge_gen.KNOWLEDGE_DIR, knowledge_gen.GRAPH_PATH, self_map.SELF_MAP_PATH)
    validate.ROOT = root
    validate.ENGINE_DIR = os.path.join(root, ".engine")
    validate.CATALOG_PATH = os.path.join(root, ".engine", "schemas", "surface-catalog.json")
    wiring.SETTINGS_PATH = os.path.join(root, ".claude", "settings.json")
    wiring.MCP_PATH = os.path.join(root, ".mcp.json")
    wiring.GITIGNORE_PATH = os.path.join(root, ".gitignore")
    wiring.CATALOG_PATH = validate.CATALOG_PATH
    wiring.CODEX_HOOKS_PATH = os.path.join(root, ".codex", "hooks.json")
    wiring.CODEX_CONFIG_PATH = os.path.join(root, ".codex", "config.toml")
    knowledge_gen.KNOWLEDGE_DIR = os.path.join(root, ".engine", "knowledge")
    knowledge_gen.GRAPH_PATH = os.path.join(knowledge_gen.KNOWLEDGE_DIR, "graph.json")
    self_map.SELF_MAP_PATH = os.path.join(root, ".engine", "self-map.md")
    try:
        yield
    finally:
        (validate.ROOT, validate.ENGINE_DIR, validate.CATALOG_PATH,
         wiring.SETTINGS_PATH, wiring.MCP_PATH, wiring.GITIGNORE_PATH, wiring.CATALOG_PATH,
         wiring.CODEX_HOOKS_PATH, wiring.CODEX_CONFIG_PATH,
         knowledge_gen.KNOWLEDGE_DIR, knowledge_gen.GRAPH_PATH, self_map.SELF_MAP_PATH) = saved


# A representative subset of core's real wiring for the fixture: hooks across the gating events (boot at
# session start, the exploration write-gate + commit-boundary refresh on PreToolUse, the close gate on Stop),
# the knowledge query server, and a cache ignore. The apply phase installs ALL of these — proving a generated
# repo gets its HOOKS, not only its query server, into a hook-less settings.
_FIXTURE_CORE_WIRES = [
    {"type": "hook", "event": "SessionStart", "matcher": "startup",
     "hook": {"type": "command", "command": "${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python .engine/tools/boot.py"}},
    {"type": "hook", "event": "PreToolUse", "matcher": "",
     "hook": {"type": "command", "command": "${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python .engine/tools/modes.py"}},
    {"type": "hook", "event": "Stop", "matcher": "",
     "hook": {"type": "command", "command": "${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python .engine/tools/close.py"}},
    {"type": "mcp", "name": "engine-knowledge-graph",
     "definition": {"command": "uv", "args": ["run", "--directory", ".engine", "python", "tools/x.py"]}},
    {"type": "gitignore", "key": "core-knowledge-cache", "lines": [".engine/knowledge/.cache/"]},
]


def _build_fixture(root: str) -> None:
    """A throwaway 'freshly generated' repo: the required spine (with representative wiring + `provides` so
    coherence reads it cleanly), one planted optional add-on with a catalog entry, a minimal surface catalog,
    and a HOOK-LESS `.claude/settings.json` — modelling the published template, which ships without the engine
    hooks installed (apply installs them). NO engine manifest yet, so it reads as not-set-up (a real first
    run's starting state)."""
    eng = os.path.join(root, ".engine")
    os.makedirs(os.path.join(eng, "modules", "core"))
    os.makedirs(os.path.join(eng, "modules", "extras-demo"))
    os.makedirs(os.path.join(eng, "provisioning"))
    os.makedirs(os.path.join(eng, "schemas"))
    os.makedirs(os.path.join(root, ".claude"))
    _write_json(os.path.join(eng, "modules", "core", "manifest.json"),
                {"id": "core", "version": "1.0.0", "status": "required",
                 "provides": {"provisioning": [".engine/provisioning/module-catalog.json"],
                              "schema": [".engine/schemas/*.json"],
                              "knowledge": [".engine/knowledge/*.json"],
                              # the globs that own the first-run assets the finish-demo plants + retires; the
                              # base fixture has no files under these dirs, so they claim nothing here (apply
                              # is unaffected) and own the planted assets once the finish fixture plants them.
                              # `audits` owns the planted audit digest (retired-but-provided, #404):
                              # in the real repo audit-library provides it; here core's glob stands in.
                              "tool": [".engine/tools/*.py"],
                              "operation": [".engine/operations/*.md"],
                              "template": [".engine/templates/*.md"],
                              "audits": [".engine/audits/*.md"]},
                 "wires": _FIXTURE_CORE_WIRES, "depends": {}})
    _write_json(os.path.join(eng, "modules", "extras-demo", "manifest.json"),
                {"id": "extras-demo", "version": "1.0.0", "status": "optional", "provides": {}, "depends": {}})
    _write_json(os.path.join(eng, "provisioning", "module-catalog.json"),
                [{"id": "extras-demo", "verb": "engine-extras", "category": "Verification & Validation",
                  "status": "optional",
                  "description": "A practice add-on for this demonstration — checks and tests your work."}])
    _write_json(os.path.join(eng, "schemas", "surface-catalog.json"), {"surfaces": {}})
    # A real generated repo ships a committed knowledge graph (it travels with the template); the substrate
    # step re-derives it idempotently. Seeding one here keeps the code-ownership path set stable from the
    # first pass (it claims `.engine/knowledge/*.json`), so a resumed run is a true no-op, not a re-render.
    os.makedirs(os.path.join(eng, "knowledge"))
    _write_json(os.path.join(eng, "knowledge", "graph.json"), {"schema_version": 1, "entities": [], "edges": []})
    _write_json(os.path.join(root, ".claude", "settings.json"), {})  # hook-less: the engine is not wired yet
    # A real generated repo's root README is the template's marketing landing front — it travels via "Use this
    # template", carrying the engine marker. Plant it so STEP 5's greenfield replace path (marker present ->
    # replaced) is exercised; the apply-demo also proves the construction repo's own README stays untouched.
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(_MARKETING_SEED_MARKER + "\n\n# engine-template\n")
    # The template's own Apache-2.0 + Commons Clause LICENSE travels the same way (its author's copyright). Plant
    # a byte-true copy (the recognizer's own seed verbatim) so STEP 5's greenfield clear path (recognizer matches ->
    # removed) is exercised; the apply-demo also proves the construction repo's own LICENSE stays untouched.
    with open(os.path.join(root, "LICENSE"), "w", encoding="utf-8") as fh:
        fh.write(_TEMPLATE_LICENSE_SEED)
    # Both CLAUDE files travel via "Use this template" too: the construction-governance CLAUDE.md (which governs
    # building the template itself) and the thin deployed floor (CLAUDE.deployed.md). Plant both so STEP 5's
    # greenfield swap (#272: construction file recognized -> floor swapped in, source removed) is exercised; the
    # apply-demo also proves the construction repo's own two files stay untouched. The construction stand-in
    # carries the recognizer marker; the floor stand-in carries boot's present marker, like the real floor.
    with open(os.path.join(root, "CLAUDE.md"), "w", encoding="utf-8") as fh:
        fh.write("# engine-template — construction governance (stand-in for the practice project)\n")
    with open(os.path.join(root, "CLAUDE.deployed.md"), "w", encoding="utf-8") as fh:
        fh.write("# Your project runs on an Engine\n\nI show a Project status block first each session.\n")


def _catalog_path(root: str) -> str:
    return os.path.join(root, ".engine", "provisioning", "module-catalog.json")


def _demo() -> int:
    """The operator-runnable demonstration. Prints the honest-ceiling banner, then runs the REAL gather and
    confirm logic against a throwaway generated-repo fixture: the walkthrough lists a planted optional add-on;
    confirming while KEEPING it records it in the saved choices; confirming while LEAVING it OUT records only
    the essentials (its files stay until the later removal step — this first step deletes nothing); and
    stopping before confirm writes nothing, so the next run re-offers everything. Real files are untouched."""
    import tempfile
    print(_BANNER + "\n")
    print(_DEMO_LIVE_NOTE + "\n")

    ok = True
    # Part A — keep the optional add-on.
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        with _redirect_root(tmp):
            print("This practice project is set up already? " + ("yes" if is_provisioned() else "no — so setup runs.\n"))
            print(present_gather(catalog_path=_catalog_path(tmp)))
            print("\n— You choose: keep the add-on, on your own.")
            res = confirm(["extras-demo"], "solo", engine_release="1.0.0")
            kept = sorted(res["manifest"]["packages"])
            print(f"  Saved your choices: kept {kept}, reviewer = on your own.")
            print(f"  {_DEMO_BRIDGE}")
            ok &= ("extras-demo" in kept and is_provisioned())

    # Part B — leave the optional add-on out.
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        with _redirect_root(tmp):
            res = confirm([], "solo", engine_release="1.0.0")
            kept = sorted(res["manifest"]["packages"])
            extras_still_on_disk = os.path.isdir(os.path.join(tmp, ".engine", "modules", "extras-demo"))
            print(f"\n— You choose: leave the add-on out. Saved choices: kept {kept}.")
            print("  The add-on is no longer in your saved choices (it gets removed in the next part);")
            print(f"  its files are still here for now: {extras_still_on_disk} — this first step deletes nothing.")
            ok &= ("extras-demo" not in kept and extras_still_on_disk)

    # Part C — stop before confirming: nothing is written, the next run re-offers everything.
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        with _redirect_root(tmp):
            before = is_provisioned()
            # (operator stops here — confirm is never called)
            print(f"\n— You stop before confirming. Anything saved? {is_provisioned()} "
                  f"(was {before}) — so the next run starts over and re-offers every choice.")
            ok &= (not is_provisioned())

    print("\n" + ("All steps behaved." if ok else "A STEP DID NOT BEHAVE — see above."))
    return 0 if ok else 1


# ---- apply demo (the consent instrument: REAL 7-step logic, only the external boundaries faked) ----

_APPLY_DEMO_NOTE = (
    "What's real here, and what's a stand-in: every setup step below runs its REAL logic against the "
    "throwaway practice project — removing the unkept add-on, setting who reviews the engine's files, turning "
    "on the safer default, switching the engine on, and preparing its saved information all really happen "
    "there. The three things that would reach OUTSIDE the practice project are stand-ins, marked on each line: "
    "reading your computer's own settings, downloading and setting up the engine's tools, and talking to "
    "GitHub. Nothing here touches your real machine, your accounts, or this project — which the isolation "
    "check at the end proves."
)

# The construction repo's own files the apply steps would write (or DELETE) if redirection ever leaked — the demo
# asserts they are byte-for-byte unchanged afterward (the isolation guarantee, shown mechanically, not just claimed).
# LICENSE is load-bearing here: the construction repo's own root LICENSE positively MATCHES the clear recognizer, so
# this snapshot is the mechanical proof a redirected run never deletes it (stronger than the README case). CLAUDE.md
# (the construction-governance file) likewise MATCHES the floor-swap recognizer, and CLAUDE.deployed.md is the real
# floor the swap would consume — both are snapshotted so a redirected run is proven never to swap/delete them (#272).
_REAL_ISOLATION_FILES = (".engine/knowledge/graph.json", ".claude/settings.json", ".mcp.json",
                         ".gitignore", ".github/CODEOWNERS", "CLAUDE.md", "CLAUDE.deployed.md", "SECURITY.md",
                         "README.md", "LICENSE", ".engine/conduct/operator.md", ".engine/conduct/defaults.md")


class _FakeIssues:
    """Stands in for the GitHub label boundary so the demo never creates a real label."""
    def ensure_label(self):
        return None


def _approve_transport():
    """An in-memory GitHub where the operator can administer the repo and the branch starts UNprotected:
    apply creates the engine ruleset and the verify read then sees the floor met → a true 'applied'."""
    state = {"met": False}
    rb_state = {}

    def t(method, path, body=None):
        headers = {"X-OAuth-Scopes": "repo"}
        if method == "GET" and path.endswith("/rules/branches/main"):
            return 200, (bootstrap.floor_ruleset(tier=bootstrap.SOLO)["rules"] if state["met"] else []), headers
        if method == "GET" and path.endswith("/rulesets"):
            return 200, [], headers
        if method == "POST" and path.endswith("/rulesets"):
            state["met"] = True
            return 201, {"id": 901, "name": (body or {}).get("name", "")}, headers
        if method == "PUT" and "/rulesets/" in path:
            state["met"] = True
            return 200, {"id": 901}, headers
        rb = _repo_behavior_responses(method, path, body, rb_state, sec_available=True)
        if rb is not None:
            return rb[0], rb[1], headers
        sec = _security_floor_responses(method, path, body, available=True)
        if sec is not None:
            return sec[0], sec[1], headers
        if path.startswith("/repos/"):
            return 200, {"full_name": "you/your-project"}, headers
        return 404, None, headers
    return t


def _repo_behavior_responses(method: str, path: str, body, state: dict, sec_available: bool):
    """Demo-fake answers for the repo-behavior endpoints (#541): the repo-settings read/PATCH and the two
    Dependabot switches. STATEFUL, so a write really flips what the read-back reports — the leg's
    verify-after-write is exercised for real. Also owns the bare `GET /repos/{repo}` answer (merging the
    security-floor's read-back fields, keyed on `sec_available`), since both legs read that one endpoint.
    Returns (status, json) for a handled endpoint, else None."""
    if path.endswith("/vulnerability-alerts"):
        if method == "PUT":
            state["alerts"] = True
            return 204, None
        return (204, None) if state.get("alerts") else (404, None)
    if path.endswith("/automated-security-fixes"):
        if method == "PUT":
            state["fixes"] = True
            return 204, None
        return 200, {"enabled": bool(state.get("fixes")), "paused": False}
    if method == "PATCH" and isinstance(body, dict) and (
            "delete_branch_on_merge" in body or "allow_update_branch" in body
            or "has_wiki" in body or "has_projects" in body):
        state.setdefault("repo", {}).update(body)
        return 200, {}
    if method == "GET" and path.startswith("/repos/") and path.count("/") == 3:
        sa = {"secret_scanning": {"status": "enabled" if sec_available else "disabled"}}
        # A fresh "Use this template" repo inherits the template's defaults: the working-comfort settings off,
        # the wiki + project boards ON (GitHub's defaults). The step turns the first pair on and the latter off.
        data = {"full_name": "you/your-project", "security_and_analysis": sa,
                "delete_branch_on_merge": False, "allow_update_branch": False,
                "has_wiki": True, "has_projects": True}
        data.update(state.get("repo", {}))
        return 200, data
    return None


def _security_floor_responses(method: str, path: str, body, available: bool):
    """Demo-fake answers for the security-floor endpoints (security_and_analysis PATCH + repo read, CodeQL
    default-setup, private vulnerability reporting). `available=True` → everything enables and reads back on;
    `available=False` → the tier-unsupported codes, so the floor honestly discloses the gap. Returns
    (status, json) for a security endpoint, else None (the caller then handles its own ruleset paths)."""
    if path.endswith("/code-scanning/default-setup"):
        if method == "PATCH":
            return (202, {"run_id": 1}) if available else (403, {"message": "Advanced Security is not enabled"})
        if method == "GET":
            return (200, {"state": "configured" if available else "not-configured"})
    if path.endswith("/private-vulnerability-reporting"):
        if method == "PUT":
            return (204, None) if available else (422, {"message": "public repositories only"})
        if method == "GET":
            return (200, {"enabled": bool(available)})
    if method == "PATCH" and isinstance(body, dict) and "security_and_analysis" in body:
        return (200, {}) if available else (403, {"message": "Advanced Security is not enabled"})
    if method == "GET" and path.startswith("/repos/") and path.count("/") == 3:
        sa = {"secret_scanning": {"status": "enabled" if available else "disabled"}}
        return (200, {"full_name": "you/your-project", "security_and_analysis": sa})
    return None


def _already_transport():
    """An in-memory GitHub where the branch is ALREADY protected (models a resumed run after the first
    pass turned the gate on) → a clean 'already', no write. The native security features read back as on,
    and the repo-behavior settings likewise read as already chosen — the resumed-run shape throughout."""
    rb_state = {"repo": {"delete_branch_on_merge": True, "allow_update_branch": True,
                         "has_wiki": False, "has_projects": False},
                "alerts": True, "fixes": True}

    def t(method, path, body=None):
        headers = {"X-OAuth-Scopes": "repo"}
        if method == "GET" and path.endswith("/rules/branches/main"):
            return 200, bootstrap.floor_ruleset(tier=bootstrap.SOLO)["rules"], headers
        rb = _repo_behavior_responses(method, path, body, rb_state, sec_available=True)
        if rb is not None:
            return rb[0], rb[1], headers
        sec = _security_floor_responses(method, path, body, available=True)
        if sec is not None:
            return sec[0], sec[1], headers
        if path.startswith("/repos/"):
            return 200, {"full_name": "you/your-project"}, headers
        return 404, None, headers
    return t


def _defer_transport():
    """An in-memory GitHub that denies the protection write (the operator can't administer the repo) → a
    cause-matched degraded banner; the engine never pretends the gate is on. The native security features
    read back as unavailable (the free-private/public-only gaps), so the floor discloses them. The
    repo-behavior writes are denied the same way (a permission-starved sign-in), so that step degrades
    honestly rather than riding the generic catch-all."""
    def t(method, path, body=None):
        headers = {"X-OAuth-Scopes": "repo"}
        if method == "GET" and path.endswith("/rules/branches/main"):
            return 200, [], headers
        if method == "GET" and path.endswith("/rulesets"):
            return 200, [], headers
        sec = _security_floor_responses(method, path, body, available=False)
        if sec is not None:
            return sec[0], sec[1], headers
        if method in ("POST", "PUT", "PATCH"):
            return 403, {"message": "Resource not accessible by integration"}, headers
        if path.startswith("/repos/"):
            return 200, {"full_name": "you/your-project"}, headers
        return 404, None, headers
    return t


def _read_json_or(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def _read_text_or(path: str, default: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return default


def _snapshot_real_files() -> dict:
    snap = {}
    for rel in _REAL_ISOLATION_FILES:
        try:
            with open(os.path.join(validate.ROOT, rel), "rb") as fh:
                snap[rel] = fh.read()
        except OSError:
            snap[rel] = None       # absent now; the demo asserts it stays absent
    return snap


def _assert_real_files_unchanged(snap: dict) -> bool:
    ok = True
    for rel, before in snap.items():
        try:
            with open(os.path.join(validate.ROOT, rel), "rb") as fh:
                after = fh.read()
        except OSError:
            after = None
        if before != after:
            ok = False
            print(f"    ISOLATION BREACH: {rel} changed during the demo!")
    return ok


_STEP_LABELS = {
    "remove-unselected": "Remove the add-ons you didn't keep",
    "codeowners": "Set who reviews changes to the engine's own files",
    "plan-mode": "Turn on the safer planning default",
    "tool-runtime": "Set up the engine's own tools",
    "substrates": "Prepare the engine's saved information",
    "wires": "Switch the engine on (its automatic helpers)",
    "control-plane": "Turn on the branch review gate",
    "actions-enablement": "Tell you about GitHub's one-time Actions switch",
    "security-floor": "Turn on GitHub's native security features",
    "repo-behavior": "Turn on the working-comfort repository settings",
}
# A security-floor "applied" means every native toggle reached an honest outcome (on / already / pending /
# unavailable-and-disclosed); "skipped" is the clean no-project/sign-in case. Only a failed/unconfirmed
# toggle degrades the step.
_GOOD_STATUSES = {"done", "written", "adopted", "already", "materialized", "applied",
                  "kept-operator-default", "skipped", "operator-step-told"}


def _step(steps: list, name: str) -> dict:
    """The ledger entry for a named step (the control-plane gate is no longer the LAST step — a security-
    floor step follows it — so gate-result consumers must address it BY NAME, never by position)."""
    return next((s for s in steps if s.get("step") == name), {})


def _print_apply_ledger(steps: list, faked: dict) -> None:
    for s in steps:
        name, status = s["step"], s["status"]
        mark = "✓" if status in _GOOD_STATUSES else ("⚠" if status == "degraded" else "·")
        note = faked.get(name)
        tail = f"   [stand-in: {note}]" if note else "   [real — ran on the practice project]"
        print(f"  {mark} {_STEP_LABELS.get(name, name)} — {status}{tail}")


def _apply_demo() -> int:
    """Operator-runnable demonstration of the APPLY phase. Runs the REAL eleven-step apply logic against a
    throwaway generated-repo fixture, faking ONLY the external boundaries (your computer's settings, the uv
    install + sync, the GitHub review-gate calls — each marked in the ledger). Shows the full happy path, an
    interrupted-then-resumed run, a tools-failure that halts safely, and a review-gate that can't be turned
    on; then proves THIS real project's files are byte-for-byte unchanged. Vary it: change which boundaries
    succeed or fail, the handle, the home preference. Leads with the honest-ceiling banner."""
    import tempfile
    print(_BANNER + "\n")
    print(_APPLY_DEMO_NOTE + "\n")
    real_before = _snapshot_real_files()
    ok = True
    faked = {"plan-mode": "your computer's own settings",
             "tool-runtime": "downloading + setting up the tools",
             "control-plane": "GitHub",
             "repo-behavior": "GitHub"}
    common = dict(home_reader=lambda: {}, uv_present=lambda: None, uv_runner=lambda uv, g: True,
                  consent=lambda kind: True, gh_refresh=lambda s: True, control_issues=_FakeIssues(),
                  control_repo="you/your-project", control_token="demo-token")  # the GitHub coordinates,
                  # injected so the practice run is identical everywhere (never the ambient environment's)

    # Scenario 1 — the full setup: install everything, turn the review gate ON.
    print("— FULL SETUP: you keep the essentials, approve the tools, and turn on the review gate.")
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        with _redirect_root(tmp):
            confirm([], "solo", engine_release="1.0.0", handle="acme-dev")
            res = apply(announce=lambda t: None,
                        uv_installer=lambda: os.path.join(tmp, ".engine", ".uv", "uv"),
                        control_transport=_approve_transport(), **common)
            settings = _read_json_or(os.path.join(tmp, ".claude", "settings.json"), {})
            hooks_on = "hooks" in settings
            plan_on = (settings.get("permissions") or {}).get("defaultMode") == "plan"
            mcp_on = os.path.isfile(os.path.join(tmp, ".mcp.json"))
            graph_on = os.path.isfile(os.path.join(tmp, ".engine", "knowledge", "graph.json"))
            extras_gone = not os.path.isdir(os.path.join(tmp, ".engine", "modules", "extras-demo"))
        _print_apply_ledger(res["steps"], faked)
        gate_on = bool(_step(res["steps"], "control-plane").get("protected"))
        print(f"    → the engine is switched on (its helpers are wired: {hooks_on}), the safer default is on "
              f"({plan_on}), its query server is registered ({mcp_on}) and saved information prepared "
              f"({graph_on}); the unkept add-on is gone ({extras_gone}); the review gate is on ({gate_on}).")
        ok &= (not res["halted"] and hooks_on and plan_on and mcp_on and graph_on and extras_gone and gate_on)

    # Scenario 2 — interrupt then resume: a re-run repeats nothing and finishes cleanly.
    print("\n— INTERRUPTED, THEN RE-RUN: setup picks up where it left off and changes nothing twice.")
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        with _redirect_root(tmp):
            confirm([], "solo", engine_release="1.0.0", handle="acme-dev")
            apply(announce=lambda t: None, uv_installer=lambda: os.path.join(tmp, ".engine", ".uv", "uv"),
                  control_transport=_approve_transport(), **common)
            second = apply(announce=lambda t: None,
                           uv_installer=lambda: os.path.join(tmp, ".engine", ".uv", "uv"),
                           control_transport=_already_transport(), **common)
        by_step = {s["step"]: s["status"] for s in second["steps"]}
        repeats = [(s["step"], s["status"]) for s in second["steps"]]
        # The two steps that WROTE on the first pass must be clean no-ops on the resume; nothing degrades.
        idempotent = (not second["halted"]) and by_step.get("codeowners") == "already" \
            and by_step.get("plan-mode") == "already" and by_step.get("control-plane") == "already" \
            and all(s["status"] in _GOOD_STATUSES for s in second["steps"])
        print(f"    → re-run results: {[f'{n}:{st}' for n, st in repeats]}")
        print(f"    → the steps that wrote the first time are now no-ops; a resumed setup is safe ({idempotent}).")
        ok &= idempotent

    # Scenario 3 — the tools can't be set up: setup HALTS safely, never falls back to system python.
    print("\n— TOOLS CAN'T BE SET UP (e.g. offline): setup stops safely and resumes later — never a fallback.")
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        with _redirect_root(tmp):
            confirm([], "solo", engine_release="1.0.0", handle="acme-dev")
            res = apply(announce=lambda t: None, uv_installer=lambda: None,  # install FAILS
                        control_transport=_approve_transport(), **common)
        steps_run = [s["step"] for s in res["steps"]]
        later_absent = not ({"substrates", "wires", "control-plane"} & set(steps_run))
        runtime_degraded = res["steps"][-1]["step"] == "tool-runtime" and res["steps"][-1]["status"] == "degraded"
        print(f"    → steps attempted: {steps_run}")
        print(f"    → setup HALTED at the tools step (halted={res['halted']}, {runtime_degraded}); the later "
              f"steps (saved information, switching on, the review gate) were not attempted ({later_absent}).")
        print("    → a retry later resumes from here; the engine never quietly falls back to a different setup.")
        ok &= (res["halted"] and later_absent and runtime_degraded)

    # Scenario 4 — the review gate can't be turned on: setup finishes and says so plainly (never fakes it).
    print("\n— REVIEW GATE CAN'T BE TURNED ON (no permission): setup completes and says so honestly.")
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        with _redirect_root(tmp):
            confirm([], "solo", engine_release="1.0.0", handle="acme-dev")
            res = apply(announce=lambda t: None,
                        uv_installer=lambda: os.path.join(tmp, ".engine", ".uv", "uv"),
                        control_transport=_defer_transport(), **dict(common, gh_refresh=lambda s: False))
        cp = _step(res["steps"], "control-plane")
        ended = (not res["halted"]) and cp["step"] == "control-plane" and len(res["steps"]) == 11
        print(f"    → the review-gate step: {cp['status']} (the engine never pretends it's on: "
              f"protected={cp.get('protected')}).")
        print(f"    → setup still completed every other step and ended cleanly ({ended}).")
        ok &= (ended and cp["status"] == "degraded" and not cp.get("protected"))

    # Scenario 5 — your starting codes of conduct are seeded, and you are TOLD (never installed silently).
    print("\n— YOUR STARTING STANCE: setup seeds your codes of conduct and discloses them in plain words.")
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        with _redirect_root(tmp):
            confirm([], "solo", engine_release="1.0.0", handle="acme-dev")
            apply(announce=lambda t: None, uv_installer=lambda: os.path.join(tmp, ".engine", ".uv", "uv"),
                  control_transport=_approve_transport(), **common)
            seeded = os.path.isfile(os.path.join(tmp, ".engine", "conduct", "operator.md"))
        print(f"    → your own codes-of-conduct file is in place, ready to tune ({seeded}).")
        print("    → and here, in plain words, is exactly what I'd tell you — never installed silently:")
        print(f'      "{load_copy()["conduct-seeded"]}"')
        ok &= seeded

    # Scenario 6 — your project's front page: the engine replaces ITS OWN marketing landing front with a starter
    # for YOUR project (greenfield), leaves a README you wrote yourself untouched (brownfield), and never re-touches
    # it on a second pass — so the replace can only ever reach the engine's own landing page, never your content.
    print("\n— YOUR PROJECT'S FRONT PAGE: the engine seeds a starter README — but only over its OWN landing page.")
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)                              # plants the Engine's marketing front (carrying the marker)
        readme = os.path.join(tmp, "README.md")
        had_marker = _MARKETING_SEED_MARKER in _read_text_or(readme, "")
        with _redirect_root(tmp):
            confirm([], "solo", engine_release="1.0.0", handle="acme-dev")
            apply(announce=lambda t: None, uv_installer=lambda: os.path.join(tmp, ".engine", ".uv", "uv"),
                  control_transport=_approve_transport(), **common)
            after = _read_text_or(readme, "")
            replaced = (_MARKETING_SEED_MARKER not in after) and ("your project" in after.lower())
            second_pass = _seed_readme(say=lambda t: None)     # the starter carries no marker → nothing matches
        print(f"    → greenfield: the slot held the Engine's landing page (marker present: {had_marker}); it's now "
              f"a starter for your project (replaced: {replaced}); a second setup pass changes nothing "
              f"(no-op: {second_pass == 'present'}).")
        print("    → and here, in plain words, is exactly what I'd tell you — never done silently:")
        print(f'      "{load_copy()["readme-seeded"]}"')
        ok &= (had_marker and replaced and second_pass == "present")

    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        readme = os.path.join(tmp, "README.md")
        mine = "# My Project\n\nMy own words — nothing to do with the Engine.\n"   # an operator-written README
        with open(readme, "w", encoding="utf-8") as fh:
            fh.write(mine)
        with _redirect_root(tmp):
            confirm([], "solo", engine_release="1.0.0", handle="acme-dev")
            apply(announce=lambda t: None, uv_installer=lambda: os.path.join(tmp, ".engine", ".uv", "uv"),
                  control_transport=_approve_transport(), **common)
            preserved = _read_text_or(readme, "") == mine
        print(f"    → brownfield: a README you wrote yourself (no engine marker) is left exactly as it is "
              f"(unchanged: {preserved}).")
        ok &= preserved

    # Scenario 7 — your project's license: the engine REMOVES its OWN traveled license (greenfield) so the template
    # author's copyright doesn't govern your product, adds nothing in its place, leaves a license you chose yourself
    # untouched (brownfield), and never re-touches it on a second pass — so the clear can only ever reach the
    # engine's own traveled license, never a license that is yours.
    print("\n— YOUR PROJECT'S LICENSE: the engine removes its OWN traveled license — but only its own, never yours.")
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)                              # plants the template's own traveled Apache-2.0 + Commons Clause LICENSE
        lic = os.path.join(tmp, "LICENSE")
        had_template_license = _is_template_license(_read_text_or(lic, ""))
        with _redirect_root(tmp):
            confirm([], "solo", engine_release="1.0.0", handle="acme-dev")
            apply(announce=lambda t: None, uv_installer=lambda: os.path.join(tmp, ".engine", ".uv", "uv"),
                  control_transport=_approve_transport(), **common)
            cleared = not os.path.exists(lic)
            second_pass = _seed_license(say=lambda t: None)    # the slot is now empty → nothing matches
        print(f"    → greenfield: the slot held the template's own license (template license present: "
              f"{had_template_license}); it has been removed and nothing put in its place (cleared: {cleared}); a "
              f"second setup pass changes nothing (no-op: {second_pass == 'present'}).")
        print("    → and here, in plain words, is exactly what I'd tell you — never done silently:")
        print(f'      "{load_copy()["license-cleared"]}"')
        ok &= (had_template_license and cleared and second_pass == "present")

    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        lic = os.path.join(tmp, "LICENSE")
        mine = _TEMPLATE_LICENSE_SEED.replace("StarshipSuperjam", "Acme Corp")   # an adopter who kept our text but put THEIR name on it
        with open(lic, "w", encoding="utf-8") as fh:
            fh.write(mine)
        with _redirect_root(tmp):
            confirm([], "solo", engine_release="1.0.0", handle="acme-dev")
            apply(announce=lambda t: None, uv_installer=lambda: os.path.join(tmp, ".engine", ".uv", "uv"),
                  control_transport=_approve_transport(), **common)
            preserved = _read_text_or(lic, "") == mine
        print(f"    → brownfield: a license you chose yourself (same words, but YOUR name on the copyright) is left "
              f"exactly as it is (unchanged: {preserved}).")
        ok &= preserved

    # Scenario 8 — your project's working guide: the engine SWAPS its thin deployed floor in as the root
    # CLAUDE.md and removes the now-redundant source (greenfield), so the template's own build-governance file
    # never travels into your project; it leaves a CLAUDE.md you wrote yourself untouched (brownfield), and a
    # second pass changes nothing — so the swap can only ever reach the engine's own traveled construction file
    # (#272). This is exactly the issue's required demonstration.
    print("\n— YOUR PROJECT'S WORKING GUIDE: the engine swaps its deployed floor in as CLAUDE.md — only over its OWN build file.")
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)                              # plants the construction CLAUDE.md + the deployed floor
        claude = os.path.join(tmp, "CLAUDE.md")
        floor_src = os.path.join(tmp, "CLAUDE.deployed.md")
        had_construction = _is_construction_claude(_read_text_or(claude, ""))
        floor_text = _read_text_or(floor_src, "")
        with _redirect_root(tmp):
            confirm([], "solo", engine_release="1.0.0", handle="acme-dev")
            apply(announce=lambda t: None, uv_installer=lambda: os.path.join(tmp, ".engine", ".uv", "uv"),
                  control_transport=_approve_transport(), **common)
            now = _read_text_or(claude, "")         # the floor, wrapped in the engine `floor` fence (6a)
            swapped = (wiring.fence_present(now, _FLOOR_FENCE, style=wiring.MD_FENCE)
                       and floor_text.rstrip("\n") in now and not os.path.exists(floor_src))
            second_pass = _seed_deployed_floor(say=lambda t: None)   # CLAUDE.md is now the fenced floor → no match
        print(f"    → greenfield: the slot held the engine's own build file (construction file present: "
              f"{had_construction}); the deployed floor is now your CLAUDE.md and the redundant copy is gone "
              f"(swapped: {swapped}); a second setup pass changes nothing (no-op: {second_pass == 'present'}).")
        print("    → and here, in plain words, is exactly what I'd tell you — never done silently:")
        print(f'      "{load_copy()["claude-floor-seeded"]}"')
        ok &= (had_construction and swapped and second_pass == "present")

    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        claude = os.path.join(tmp, "CLAUDE.md")
        mine = "# My Project\n\nMy own working notes — nothing to do with the Engine.\n"   # an operator-written CLAUDE.md
        with open(claude, "w", encoding="utf-8") as fh:
            fh.write(mine)
        with _redirect_root(tmp):
            confirm([], "solo", engine_release="1.0.0", handle="acme-dev")
            apply(announce=lambda t: None, uv_installer=lambda: os.path.join(tmp, ".engine", ".uv", "uv"),
                  control_transport=_approve_transport(), **common)
            preserved = _read_text_or(claude, "") == mine
        print(f"    → brownfield: a working guide you wrote yourself (no engine marker) is left exactly as it is "
              f"(unchanged: {preserved}).")
        ok &= preserved

    # The isolation guarantee, shown.
    print("\n— ISOLATION CHECK: did any of that touch THIS real project's files?")
    unchanged = _assert_real_files_unchanged(real_before)
    print(f"    → this project's own setup files are byte-for-byte unchanged: {unchanged}.")
    ok &= unchanged

    print("\n" + ("All apply steps behaved." if ok else "AN APPLY STEP DID NOT BEHAVE — see above."))
    return 0 if ok else 1


# ---- finish demo (verify + retire: the lifecycle close, real logic, fixture boundary) ----------

_FINISH_DEMO_NOTE = (
    "What's real here, and what's a stand-in: the consistency check and the tidy-up below run their REAL "
    "logic against the throwaway practice project — really checking the installed engine fits together, "
    "really deleting the one-time setup files there, and really re-deriving the saved information. The only "
    "stand-ins are the same outside-the-project boundaries as before (your computer's settings, the engine's "
    "tools, and GitHub), faked so the practice run is self-contained. Nothing here touches your real machine, "
    "your accounts, or this project — which the isolation check at the end proves, naming each file."
)

_ORPHAN_WIRE = {"type": "hook", "event": "PostToolUse", "matcher": "",
                "hook": {"type": "command", "command": ".engine/.venv/bin/python .engine/tools/boot.py --x"}}


def _plant_first_run_assets(root: str) -> None:
    """Plant stand-in first-run assets in the fixture so the tidy-up step has something to remove — the REAL
    assets live in this construction repo and the demo/test must never touch them (the isolation check proves
    they survive). Owned by the fixture core's tool/operation/template globs, so they keep it consistent."""
    for rel in _FIRST_RUN_ASSET_FILES:
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("# stand-in first-run asset for the practice project\n")
    for rel in _FIRST_RUN_ASSET_DIRS:
        p = os.path.join(root, rel)
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: engine-setup\n---\n# stand-in walkthrough\n")


def _finish_demo() -> int:
    """Operator-runnable demonstration of the FINISH phase (the consistency check + the tidy-up). Runs the
    REAL verify/retire logic against a throwaway generated-repo fixture: a clean setup checks out and is
    tidied (the one-time files removed, the permanent engine + your choices kept); an inconsistent setup is
    PAUSED with a plain explanation and refuses to tidy up (the irreversible step never runs on a broken
    setup), then a repair lets it finish (fail-then-pass). Proves THIS real project's files — this very tool
    included — are byte-for-byte unchanged. Leads with the honest-ceiling banner."""
    import tempfile
    print(_BANNER + "\n")
    print(_FINISH_DEMO_NOTE + "\n")
    real_before = _snapshot_real_files()
    real_self = os.path.join(validate.ROOT, ".engine", "tools", "instantiator.py")
    real_reader = os.path.join(validate.ROOT, ".engine", "tools", "module_catalog.py")
    ok = True

    # Scenario 1 — clean setup: the check passes (review gate on), then the one-time files are tidied away.
    print("— CLEAN SETUP: the consistency check passes and the one-time setup files are tidied away.")
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        _plant_first_run_assets(tmp)
        with _redirect_root(tmp):
            confirm([], "solo", engine_release="1.0.0", handle="acme-dev")
            applied = _finish_apply(tmp)
            gate = _step(applied["steps"], "control-plane")     # the gate is no longer the last step
            v = verify(announce=lambda t: print("    " + t), control_status=gate)
            r = retire(announce=lambda t: print("    " + t))
            assets_gone = all(not os.path.exists(os.path.join(tmp, rel))
                              for rel in _FIRST_RUN_ASSET_FILES + _FIRST_RUN_ASSET_DIRS)
            catalog_kept = os.path.isfile(os.path.join(tmp, ".engine", "provisioning", "module-catalog.json"))
            still_clean = not _hard_findings()
        print(f"    → the check passed ({not v['paused']}); the one-time files are gone ({assets_gone}); the "
              f"catalog the engine keeps is still here ({catalog_kept}); the result is still consistent "
              f"({still_clean}); saved information re-derived ({r['graph']}; wiring map {r['self_map']}).")
        ok &= (not v["paused"] and not r["refused"] and assets_gone and catalog_kept and still_clean)

    # Scenario 2 — inconsistent setup: the check PAUSES, tidy-up REFUSES; a repair then lets it finish.
    print("\n— SOMETHING INCONSISTENT: the check pauses and tidy-up refuses — then a repair lets it finish.")
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        _plant_first_run_assets(tmp)
        with _redirect_root(tmp):
            confirm([], "solo", engine_release="1.0.0", handle="acme-dev")
            _finish_apply(tmp)
            wiring.apply(_ORPHAN_WIRE)                         # leave a setting that belongs to no add-on
            v_bad = verify(announce=lambda t: print("    " + t))
            r_bad = retire(announce=lambda t: None)            # must refuse, delete nothing
            assets_still_there = all(os.path.exists(os.path.join(tmp, rel)) for rel in _FIRST_RUN_ASSET_FILES)
            wiring.reverse(_ORPHAN_WIRE)                       # the operator fixes it
            v_fixed = verify(announce=lambda t: None)
            r_fixed = retire(announce=lambda t: None)
            finished = all(not os.path.exists(os.path.join(tmp, rel)) for rel in _FIRST_RUN_ASSET_FILES)
        print(f"    → it paused on the problem ({v_bad['paused']}) and refused to tidy up ({r_bad['refused']}), "
              f"so nothing was deleted ({assets_still_there}).")
        print(f"    → after the fix, the check passes ({not v_fixed['paused']}) and tidy-up completes "
              f"({not r_fixed['refused']}, files gone: {finished}).")
        ok &= (v_bad["paused"] and r_bad["refused"] and assets_still_there
               and not v_fixed["paused"] and not r_fixed["refused"] and finished)

    # Scenario 3 — the bare-hand guard (#297): a stray hand-run of the one-time verbs on a set-up/workshop tree
    # must change NOTHING, so it never re-fires the file-replacing setup steps. Driven through the REAL CLI
    # dispatch (main), the surface the guard lives on. The dangerous one is retire — bare, it would self-delete
    # the setup tool — so the stand-in tool's survival is the headline assertion.
    print("\n— A BARE HAND-RUN OF THE ONE-TIME VERBS: each refuses and changes nothing.")
    import io
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)                                   # a workshop-like tree: root CLAUDE.md is construction
        _plant_first_run_assets(tmp)                          # the real-tool stand-ins a stray retire would delete

        def _run(argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(argv)
            return rc, buf.getvalue()
        with _redirect_root(tmp):
            rc_apply, out_apply = _run(["apply"])             # bare apply → no-op, points back to the walkthrough
            rc_token, out_token = _run(["apply", "--first-run"])  # the walkthrough's token lets apply through
            rc_verify, out_verify = _run(["verify"])          # workshop verify → refuse
            rc_retire, out_retire = _run(["retire"])          # workshop retire → refuse (the dangerous one)
            tool_alive = os.path.isfile(os.path.join(tmp, ".engine", "tools", "instantiator.py"))
            still_construction = _root_is_construction()
        bare_apply_noop = (rc_apply == 0 and _APPLY_NOT_FIRST_RUN in out_apply)
        token_reaches_apply = (_APPLY_NOT_FIRST_RUN not in out_token and "hasn't been confirmed" in out_token)
        verify_refused = ("workshop where the engine is built" in out_verify)
        retire_refused = ("workshop where the engine is built" in out_retire)
        print(f"    → bare apply changed nothing and sent you back to setup ({bare_apply_noop}); the setup "
              f"walkthrough's run still goes through ({token_reaches_apply}).")
        print(f"    → bare verify refused ({verify_refused}); bare retire refused ({retire_refused}) and the "
              f"setup tool is still here ({tool_alive}); the project guide was not swapped ({still_construction}).")
        ok &= (bare_apply_noop and token_reaches_apply and verify_refused and retire_refused
               and tool_alive and still_construction)

    # The isolation guarantee, shown by name (each file named, not just a silent pass).
    print("\n— ISOLATION CHECK: did any of that touch THIS real project's files?")
    unchanged = True
    for rel in _REAL_ISOLATION_FILES:
        before = real_before.get(rel)
        try:
            with open(os.path.join(validate.ROOT, rel), "rb") as fh:
                after = fh.read()
        except OSError:
            after = None
        same = (before == after)
        unchanged &= same
        print(f"    {'✓' if same else 'ISOLATION BREACH —'} {rel} — {'unchanged' if same else 'CHANGED!'}")
    self_alive = os.path.isfile(real_self)
    reader_alive = os.path.isfile(real_reader)
    print(f"    {'✓' if self_alive else '✗'} this setup tool itself (.engine/tools/instantiator.py) still "
          f"exists: {self_alive}")
    print(f"    {'✓' if reader_alive else '✗'} the kept catalog reader (.engine/tools/module_catalog.py) "
          f"still exists: {reader_alive}")
    print(f"    → this project's own files are byte-for-byte unchanged: {unchanged}.")
    ok &= (unchanged and self_alive and reader_alive)

    print("\n" + ("All finish steps behaved." if ok else "A FINISH STEP DID NOT BEHAVE — see above."))
    return 0 if ok else 1


def _finish_apply(tmp: str) -> dict:
    """Run apply with every external boundary faked (the finish-demo's shared apply setup), returning the
    ledger — so the consistency check + tidy-up run against a real, fully-installed practice engine."""
    return apply(announce=lambda t: None, home_reader=lambda: {}, uv_present=lambda: None,
                 uv_installer=lambda: os.path.join(tmp, ".engine", ".uv", "uv"),
                 uv_runner=lambda uv, g: True, consent=lambda kind: True,
                 control_transport=_approve_transport(), gh_refresh=lambda s: True,
                 control_issues=_FakeIssues(), control_repo="you/your-project", control_token="demo-token")


# ==== BROWNFIELD COLLISION CHECK — surface, never overwrite ============================================
#
# When the engine joins an ALREADY-POPULATED project (brownfield), it inspects the project for any overlap
# between what it would add and what is already there, and SURFACES each overlap in plain language with a
# concrete consequence + a three-way choice (accept / leave-as-is / abort) — never a raw path-versus-pattern
# report, and it NEVER silently overwrites. Three kinds of overlap:
#   1. a product file sitting where the engine keeps its own files (the engine would replace it),
#   2. product content in a file the engine and the project both use (the engine adds its own marked section
#      and keeps the rest), and
#   3. a product review rule that also covers the engine's files (the engine adds its own, placed to win).
#
# The LIVE caller is arrive() (the brownfield-arrival path, #234): it runs this check read-only against the
# target BEFORE any write, passing the RELEASE-derived owned path set (computed from the extracted release,
# before the overlay lands). It never runs in the construction repo (engine == product), and on a GREENFIELD
# adopter it is a no-op (a project made from the template starts empty). The remaining inductive gap — "behaves
# on the fixture ⇒ behaves for a real adopter fetched from a real release" — is named, not hidden: this
# construction repo never has a real outside project arrive, so the demo + tests are as far as it can reach.
# The engine path set is INJECTED so a test/demo can pass a real one; it defaults to the engine's own owned set.

# The platform-shared root files the engine co-occupies: it adds only its OWN marked/keyed entries and leaves
# the project's content alone. An EXPLICIT set — there is no path constant for the
# root project guide, and it must CO-EXIST (never be seized) — so a pre-existing one is surfaced
# as additive, never as "the engine would replace it". (CODEOWNERS is a shared file too, but its meaningful
# overlap is the review-rule shadow — class 3 — so it is handled there, not double-reported here.)
_COLLISION_SHARED = (".gitignore", ".mcp.json", ".claude/settings.json", "CLAUDE.md",
                     "AGENTS.md", ".codex/hooks.json", ".codex/config.toml")
# The engine-EXCLUSIVE path patterns — where the engine expects sole ownership; a pre-existing product file
# here would be replaced by the incoming engine file. The DECLARATIVE patterns (checked against the project
# tree), NOT the live-filtered owned set: the engine has not written here yet at arrival, so any occupant is
# the project's own.
_COLLISION_EXCLUSIVE_GLOBS = (".engine/**", ".github/workflows/engine-*.yml",
                              ".github/pull_request_template.md", ".github/ISSUE_TEMPLATE/*.md")
_COLLISION_CHOICES = ("accept", "leave-as-is", "abort")


def _read_text_opt(path: str):
    """The file's text, or None when it cannot be read or decoded — so the caller surfaces an honest
    'unreadable' overlap rather than crashing on a mis-encoded file or silently treating it as empty.
    (UnicodeDecodeError is a ValueError; validate.read decodes as UTF-8.)"""
    try:
        return validate.read(path)
    except (OSError, ValueError):
        return None


def _collision(klass: int, key: str, paths: list, copy: dict, **detail) -> dict:
    """One surfaced overlap: its class, the concrete project path(s) (never a bare pattern), the plain
    consequence (paths/rule interpolated) and the three stable choice keys the operator picks from."""
    consequence = copy[key]
    if "{paths}" in consequence:
        consequence = consequence.replace("{paths}", ", ".join(paths))
    if "{rule}" in consequence and detail.get("rule"):
        consequence = consequence.replace("{rule}", detail["rule"])
    return {"klass": klass, "paths": list(paths), "consequence": consequence,
            "choices": list(_COLLISION_CHOICES), "detail": detail}


def _class1_exclusive(root: str, copy: dict) -> list:
    """Class 1 — product files/dirs/symlinks sitting at an engine-exclusive path (the engine would replace
    them); lists the concrete occupants. The engine's own corner (.engine/) is WALKED with os.walk, NOT a
    `**` glob: on Python 3.9 a `**` glob silently skips dot-prefixed entries (a hidden-only product .engine/
    would escape) and would follow symlink loops — os.walk does neither (followlinks=False). The named
    .github artifacts (no hidden names) use a plain glob."""
    import glob as _glob
    found = set()
    eng = os.path.join(root, ".engine")
    if os.path.islink(eng):                             # a symlink standing in for the whole engine corner
        found.add(".engine")
    elif os.path.isdir(eng):
        for dirpath, dirs, files in os.walk(eng):       # followlinks=False → no symlink-loop recursion/escape
            for name in list(dirs) + files:
                p = os.path.join(dirpath, name)
                if os.path.isfile(p) or os.path.islink(p):   # files + symlinked dirs; real subdirs skipped
                    found.add(os.path.relpath(p, root).replace(os.sep, "/"))
    for pattern in (".github/workflows/engine-*.yml", ".github/pull_request_template.md",
                    ".github/ISSUE_TEMPLATE/*.md"):
        for p in _glob.glob(os.path.join(root, pattern)):
            if os.path.isfile(p) or os.path.islink(p):
                found.add(os.path.relpath(p, root).replace(os.sep, "/"))
    found = sorted(found)
    return [_collision(1, "collision-exclusive", found, copy)] if found else []


def _json_has_engine_entry(rel: str, data: dict) -> bool:
    """Whether the engine's OWN entries are already present in a keyed-JSON shared file (a resume) — an
    engine-prefixed query server in .mcp.json, or an engine hook (its command resolves under .engine/) in
    the project settings. Mirrors wiring's engine-namespaced-identity keying."""
    if rel.endswith(".mcp.json"):
        return any(isinstance(n, str) and n.startswith(wiring.MCP_NAME_PREFIX)
                   for n in (data.get("mcpServers") or {}))
    for groups in (data.get("hooks") or {}).values():
        for group in (groups or []):
            if not isinstance(group, dict):
                continue
            for h in (group.get("hooks") or []):
                if isinstance(h, dict) and wiring.ENGINE_DIR_MARKER in (h.get("command") or ""):
                    return True
    return False


def _shared_state(rel: str, path: str) -> str:
    """The overlap state of one shared file: 'additive' (product content present, engine entries absent — the
    engine will add its own and keep the rest), 'resume' (the engine's entries are already there — no flag),
    'empty' (absent/empty — a clean seed), or 'unreadable' (malformed — leave untouched and say so)."""
    if rel.endswith(".json"):
        data, err = wiring._read_json_tolerant(path, create=False)
        if err is not None:
            return "unreadable"
        if not data:
            return "empty"
        return "resume" if _json_has_engine_entry(rel, data) else "additive"
    if rel == "CLAUDE.md":
        # Fence-aware: the engine's CLAUDE.md section shape IS the `floor` fence (wiring.MD_FENCE, the
        # HTML-comment style, #234). An already-present engine floor is a 'resume' (no flag); a
        # pre-existing project guide with no engine floor is 'additive' (the engine inserts its keyed section
        # and keeps the rest). Mirrors the .gitignore branch below, but with the Markdown begin-token.
        text = _read_text_opt(path)
        if text is None:
            return "unreadable"
        if not text.strip():
            return "empty"
        return "resume" if wiring._MD_FENCE_BEGIN_TOKEN in text else "additive"
    text = _read_text_opt(path)                         # fenced text (.gitignore)
    if text is None:
        return "unreadable"
    if not text.strip():
        return "empty"
    return "resume" if wiring._FENCE_BEGIN_TOKEN in text else "additive"


def _class2_shared(root: str, copy: dict) -> list:
    """Class 2 — product content in a file the engine and the project both use (the engine adds its own marked
    section additively). Per-file-kind detection (fenced text / keyed JSON / presence-only)."""
    out = []
    for rel in _COLLISION_SHARED:
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            continue
        state = _shared_state(rel, path)
        if state == "additive":
            out.append(_collision(2, "collision-shared", [rel], copy))
        elif state == "unreadable":
            out.append(_collision(2, "collision-unreadable", [rel], copy, reason="unreadable"))
    return out


def _codeowners_matches(pattern: str, path: str) -> bool:
    """Does a CODEOWNERS rule's pattern shadow an engine path? Deliberately OVER-matches — CODEOWNERS glob
    semantics are broader than fnmatch, and surfacing one extra overlap is safe while missing one is not
    (the whole posture is 'surface, never silently overwrite'). Handles the load-bearing cases: '*'/'**'
    (everything), a trailing-'/' directory rule, a leading-'/' anchor, and a plain pattern."""
    import fnmatch
    pat = pattern.strip().lstrip("/")
    p = path.lstrip("/")
    if pat in ("", "*", "**"):
        return True
    if pat.endswith("/"):
        return p == pat[:-1] or p.startswith(pat)
    return (fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(p, pat + "/*") or p.startswith(pat + "/"))


def _class3_codeowners(root: str, engine_paths: list, copy: dict) -> list:
    """Class 3 — a product review rule that also covers the engine's files. Parse the project's CODEOWNERS
    rules OUTSIDE the engine's own marked block (so the engine's rules are never flagged); a rule whose
    pattern matches any engine path is surfaced (the engine appends its block last to win — last-match-wins —
    but the overlap is disclosed so it is no surprise). A malformed engine block → the unreadable finding."""
    path = os.path.join(root, ".github", "CODEOWNERS")
    if not os.path.exists(path):
        return []
    text = _read_text_opt(path)
    if text is None:
        return [_collision(2, "collision-unreadable", [".github/CODEOWNERS"], copy, reason="unreadable")]
    lines = text.split("\n")
    try:
        span = wiring._find_fence(lines, wiring.CODEOWNERS_FENCE)
    except wiring.WiringError:
        return [_collision(2, "collision-unreadable", [".github/CODEOWNERS"], copy, reason="unreadable")]
    excluded = set(range(span[0], span[1] + 1)) if span else set()
    out, seen = [], set()
    for i, ln in enumerate(lines):
        if i in excluded:
            continue
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pattern = stripped.split()[0]
        if pattern in seen:
            continue
        if any(_codeowners_matches(pattern, ep) for ep in engine_paths):
            seen.add(pattern)
            out.append(_collision(3, "collision-codeowners", [".github/CODEOWNERS"], copy, rule=stripped))
    return out


def collision_check(*, root=None, engine_paths=None, copy=None) -> dict:
    """The brownfield overlap check (pure read, no writes): inspect the project at `root` for the three kinds
    of overlap and return them, each framed as a plain consequence + the three choices. `engine_paths` (the
    set the engine would own — the live caller passes the release-derived set, computed BEFORE the overlay
    writes) defaults to the engine's own owned set. Returns {collisions, clean, checked}."""
    base = root or validate.ROOT
    copy = copy if copy is not None else load_copy()
    if engine_paths is None:
        engine_paths = module_coherence.engine_owned_paths(module_coherence.discover_manifests())
    collisions = (_class1_exclusive(base, copy) + _class2_shared(base, copy)
                  + _class3_codeowners(base, engine_paths, copy))
    return {"collisions": collisions, "clean": not collisions,
            "checked": {"exclusive_globs": len(_COLLISION_EXCLUSIVE_GLOBS),
                        "shared_files": len(_COLLISION_SHARED), "engine_paths": len(engine_paths)}}


def _insert_floor(release_tree: str) -> str:
    """Insert the engine's root-CLAUDE.md floor into the LIVE project's own CLAUDE.md on arrival — the engine
    `floor` fence, keyed and APPEND-when-absent (wiring.fence_apply, MD_FENCE), so an operator's existing guide
    keeps all its content and the engine adds its section below it. The floor body
    is read from the RELEASE's CLAUDE.deployed.md (NOT its construction-governance CLAUDE.md, and not the target
    — CLAUDE.deployed.md is not overlaid). This is the INSERT-on-arrival counterpart to module_manager's
    SKIP-on-absent _merge_claude_floor (the upgrade path, which must never duplicate a floor): arrival is the
    one path that creates the fence. Returns 'inserted' | 'present' (a floor is already there — a resume, no
    duplicate) | 'skipped' (the release ships no floor) | 'degraded' (a malformed local fence — left untouched).
    Paths are validate.ROOT-relative, so a redirected arrival writes only the live target."""
    src = os.path.join(release_tree, _DEPLOYED_FLOOR_REL)
    floor = _read_text_opt(src) if os.path.isfile(src) else None
    if not floor or not floor.strip():
        return "skipped"
    floor_lines = floor.split("\n")
    if floor_lines and floor_lines[-1] == "":
        floor_lines = floor_lines[:-1]              # drop the trailing-newline empty element; fence re-terminates
    local_path = os.path.join(validate.ROOT, _ROOT_CLAUDE_REL)
    local = _read_text_opt(local_path) or "" if os.path.isfile(local_path) else ""
    try:
        if wiring.fence_present(local, _FLOOR_FENCE, style=wiring.MD_FENCE):
            return "present"                        # an engine floor is already in place — never a second one
        merged = wiring.fence_apply(local, _FLOOR_FENCE, floor_lines, style=wiring.MD_FENCE)
    except wiring.WiringError:
        return "degraded"                           # malformed local fence → leave untouched, never crash
    with open(local_path, "w", encoding="utf-8") as fh:
        fh.write(merged)
    return "inserted"


def arrive(*, target_root: str, release_tree: str, engine_release: str | None = None,
           keep=None, tier: str | None = None, handle=None, default_branch=None, decide=None, apply_changes: bool = False,
           announce=None, opener=None, gh_api=None,
           home_reader=None, settings_path=None, uv_present=None, uv_installer=None, uv_runner=None,
           consent=None, control_transport=None, gh_refresh=None, control_issues=None,
           control_repo=None, control_token=None) -> dict:
    """BROWNFIELD ARRIVAL (#234) — overlay the engine onto a LIVE
    product tree and run the SAME instantiator, with the collision check as the one brownfield-only gate. The
    engine isn't on the target yet, so this runs from the EXTRACTED release (`release_tree`, the documented
    bootstrap's temp extraction) and is the SOLE writer to the live tree (`target_root`); ROOT is bound to the
    target for every write.

    TWO MODES. `apply_changes=False` (the default) is SURFACE-ONLY: it runs the read-only collision check,
    shows every overlap + the team-tier recommendation, and STOPS — writing nothing, whether or not overlaps
    were found (so the 'just show me' step is truly read-only even on a clean project). `apply_changes=True`
    then performs the arrival: per-collision choices via `decide(collision) -> 'accept'|'leave-as-is'|'abort'`
    (any 'abort', or a 'leave-as-is' on a shared file the engine needs (class 2) or a review rule (class 3),
    stops BEFORE the first write — a class-1 'leave-as-is' path is kept, excluded from the overlay); then
    overlay the full release module set, insert the engine floor into the operator's CLAUDE.md, run confirm →
    apply → verify → retire unforked, and land the arrival as a reviewed PR (via `opener`).

    The TARGET is the single source of truth for every write: the live GitHub side (branch protection, native
    scanning via apply's control args, team detection, the arrival PR) is aimed at the target's own owner/repo
    (`control_repo`, else read from the target's git remote — never the process cwd, which is the release tree).
    Every boundary is injectable so tests/the demo run the REAL flow with nothing real touched. Returns a
    structured result the caller renders in plain language."""
    say = announce if announce is not None else (lambda text: print(text))
    decide = decide if decide is not None else (lambda c: "abort")
    if not release_tree:
        raise ValueError("arrive needs the extracted engine release tree (release_tree).")
    result = {"proceeded": False, "surfaced": False, "stopped_on": None, "reason": None, "collisions": [],
              "overlaid": [], "floor": None, "tier": None, "team": None, "steps": [], "pr": None}
    with _redirect_root(target_root):
        copy = load_copy()
        # The target's own owner/repo — the single aim for every live GitHub write (never the process cwd).
        slug = control_repo if control_repo is not None else _target_slug(target_root)
        # The owned set + the full module id set the engine would deliver — computed with ROOT at the RELEASE
        # tree (module_coherence reads validate.ROOT), then ROOT is restored to the target before any write.
        with _redirect_root(release_tree):
            release_paths = module_coherence.engine_owned_paths(module_coherence.discover_manifests())
            release_ids = sorted(m.get("id") for _rel, m in module_coherence.discover_manifests() if m.get("id"))
        if not release_ids:
            return {**result, "stopped_on": "release",
                    "reason": "the engine release looks empty or unreadable, so the arrival stopped and "
                              "nothing was changed."}
        # (1) READ-ONLY collision check, BEFORE any write.
        check = collision_check(root=target_root, engine_paths=release_paths, copy=copy)
        result["collisions"] = check["collisions"]
        say(copy["collision-intro"] if check["collisions"] else copy["collision-none"])
        # (2) Surface each overlap, then the team-tier recommendation. Read-only.
        for c in check["collisions"]:
            say("  • " + c["consequence"])
        team = detect_team(root=target_root, slug=slug, gh_api=gh_api)
        result["team"] = team
        # The target's default-branch name, derived target-scoped (its slug + tree, never the process cwd) and
        # persisted at confirm below — so the arrived repo classifies its checkout against a known name (#342).
        target_default_branch = derive_default_branch(root=target_root, slug=slug, gh_api=gh_api)
        if team.get("detected") and (tier or "solo") != "team":
            say(copy["team-recommended"])
        result["surfaced"] = True
        # SURFACE-ONLY stops here — nothing is written, whether or not overlaps were found.
        if not apply_changes:
            return {**result, "reason": "showed the overlaps, read-only — nothing was changed."}
        # (3) Collect the operator's per-collision choice and decide whether to proceed. Any 'abort' stops.
        # 'leave-as-is' on a class-1 (engine-exclusive) path is honored — that path is kept, excluded from the
        # overlay. 'leave-as-is' on a class-2 shared file the engine needs to function, or a class-3 review
        # rule, stops (the engine never half-installs). Nothing is written until this passes.
        exclude = set()
        for c, choice in [(c, decide(c)) for c in check["collisions"]]:
            if choice == "abort":
                return {**result, "stopped_on": f"class{c['klass']}",
                        "reason": "you chose to stop, so the arrival stopped and nothing was changed."}
            if choice == "leave-as-is":
                if c["klass"] == 1:
                    exclude.update(c["paths"])
                else:
                    return {**result, "stopped_on": f"class{c['klass']}",
                            "reason": "you chose to leave a file the engine needs as it is, so the arrival "
                                      "stopped and nothing was changed — accept it to go on, or sort it out "
                                      "and run the arrival again."}
        # (4) OVERLAY the full release module set onto the live tree (kept class-1 paths excluded), then
        # DELIVER the file category the copy-only overlay misses — the committed fixtures (#599). Arrival shares
        # the upgrade's delivery gap, so it shares the fix: the same exclude-aware deliver primitive. Arrival
        # delivers the FULL template surface and runs retire() itself at step 6, so project_retire=False here —
        # it must NOT project the first-run set out (that is the deployed-upgrade projection, not arrival's).
        try:
            result["overlaid"], candidates = module_manager._overlay_engine_code(
                release_tree, release_ids, exclude=exclude)
            result["overlaid"] += module_manager._deliver_synced(
                release_tree, candidates, project_retire=False, exclude=exclude)
        except module_manager._UpgradeRefused as ur:
            return {**result, "stopped_on": "overlay", "reason": ur.reason}
        # (5) INSERT the engine floor into the operator's own CLAUDE.md (keyed, append-when-absent).
        result["floor"] = _insert_floor(release_tree)
        # (6) Run the SAME instantiator: confirm (the checkpoint) → apply → verify → retire. The control-plane
        # args carry the TARGET's slug so branch protection + native scanning land on the target, not the cwd.
        confirm(keep or [], tier or "solo", engine_release=engine_release, handle=handle,
                default_branch=default_branch or target_default_branch)
        applied = apply(announce=say, home_reader=home_reader, settings_path=settings_path,
                        uv_present=uv_present, uv_installer=uv_installer, uv_runner=uv_runner,
                        consent=consent, control_transport=control_transport, gh_refresh=gh_refresh,
                        control_issues=control_issues, control_repo=slug,
                        control_token=control_token, handle=handle, brownfield=True)
        result["steps"] = applied.get("steps", [])
        result["tier"] = tier or "solo"
        if applied.get("refused") or applied.get("halted"):
            return {**result, "proceeded": True,
                    "reason": "the engine's files are in place but setup did not finish (see the steps); "
                              "fix the cause and run the arrival again — it resumes from here."}
        verify(announce=say)
        retired = retire(announce=say)
        if retired.get("refused"):
            if retired.get("reason") == "unsafe-retire-target":
                return {**result, "proceeded": True,
                        "reason": "the engine is installed, but its one-time setup cleanup was stopped for "
                                  "safety — the cleanup list named an item that isn't one of the engine's own "
                                  "files or folders, so nothing was removed. This is an engine defect to "
                                  "report, not something re-running fixes; the setup files were left in place."}
            return {**result, "proceeded": True,
                    "reason": "the engine is installed but a consistency check did not pass, so the one-time "
                              "setup files were left in place; fix the cause and run the arrival again."}
        # (7) Land the arrival as a reviewed pull request on the TARGET (the merge wall; the operator approves).
        if opener is not None:
            # `Feature:` — the release-notes change-kind prefix (release_cut._RELEASE_NOTE_KINDS): arriving in a
            # project is a new capability, and the prefix is what groups it in the deployed repo's release notes.
            title = "Feature: add the engine to this project"
            body = ("This pull request adds the engine to the project: its files are placed in their own "
                    "namespaced corners, any overlap with the project's own files was surfaced and settled, "
                    "and the engine's working guide was added to CLAUDE.md alongside the project's own content. "
                    "Merging it turns on the review gate; reverting it removes the engine again.")
            result["pr"] = opener(branch="engine-arrival", title=title, body=body, repo=slug)
        result["proceeded"] = True
    return result


# ---- collision demo (the consent instrument: REAL detection, throwaway PRODUCT fixtures) -------------

_COLLISION_DEMO_NOTE = (
    "What this is: the overlap check is now the live first step when the engine is added to a project that "
    "ALREADY has its own files (the brownfield arrival). This demonstration runs that REAL check against "
    "throwaway practice projects so you can see it behave — a clean project, a populated one, and a re-run. "
    "What it can't prove here: this construction repo never has a real outside project arrive, so 'it behaved "
    "on these practice projects' is as far as a demonstration can reach — the live arrival end to end is "
    "exercised by the arrival demo and the tests. Nothing here touches your real project."
)


def _build_collision_fixture(root: str, *, populated: bool) -> None:
    """A throwaway PRODUCT repo (NOT a generated engine repo): some product source + config. With
    populated=True, plant one overlap of each kind for the demo/tests to surface. NEVER touches the real
    tree (its own tempdir)."""
    os.makedirs(os.path.join(root, "src"))
    with open(os.path.join(root, "src", "app.py"), "w", encoding="utf-8") as fh:
        fh.write("print('the product')\n")
    os.makedirs(os.path.join(root, ".github"))
    if not populated:                       # a clean coexistence: nothing where the engine keeps its own,
        with open(os.path.join(root, ".github", "CODEOWNERS"), "w", encoding="utf-8") as fh:
            fh.write("/src/ @product-team\n")          # and a disjoint product rule (no engine path covered)
        return
    # class 1 — a product file where the engine keeps its own
    os.makedirs(os.path.join(root, ".engine", "legacy"))
    with open(os.path.join(root, ".engine", "legacy", "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("an old file of the product's that happens to live here\n")
    # class 2 — product content in files the engine also uses (engine entries ABSENT), one per file kind
    with open(os.path.join(root, ".gitignore"), "w", encoding="utf-8") as fh:    # fenced text
        fh.write("node_modules/\n*.log\n")
    _write_json(os.path.join(root, ".mcp.json"), {"mcpServers": {"product-tool": {"command": "x"}}})  # JSON
    os.makedirs(os.path.join(root, ".claude"))                                   # JSON (the consequential one)
    _write_json(os.path.join(root, ".claude", "settings.json"),
                {"hooks": {"PreToolUse": [{"matcher": "", "hooks": [
                    {"type": "command", "command": "scripts/product-hook.sh"}]}]}})
    with open(os.path.join(root, "CLAUDE.md"), "w", encoding="utf-8") as fh:     # presence-only
        fh.write("# The product's own project guide\nBuild with make.\n")
    # class 3 — a product review rule that also covers the engine's files (the expansive rule)
    with open(os.path.join(root, ".github", "CODEOWNERS"), "w", encoding="utf-8") as fh:
        fh.write("* @product-team\n/src/ @product-team\n")


def _plant_engine_entries(root: str) -> None:
    """Model the engine already wired into the shared files (a resume): an engine-managed block in .gitignore, an
    engine query server in .mcp.json, an engine hook in settings.json, and the engine `floor` fence in CLAUDE.md
    — so those overlaps no longer flag on a re-run."""
    claude = os.path.join(root, "CLAUDE.md")
    with open(claude, "w", encoding="utf-8") as fh:
        fh.write(wiring.fence_apply(_read_text_opt(claude) or "", _FLOOR_FENCE,
                                    ["Project status block."], style=wiring.MD_FENCE))
    with open(os.path.join(root, ".gitignore"), "a", encoding="utf-8") as fh:
        fh.write(wiring.FENCE_BEGIN.format(id="core-knowledge-cache") + "\n.engine/knowledge/.cache/\n"
                 + wiring.FENCE_END.format(id="core-knowledge-cache") + "\n")
    data, _err = wiring._read_json_tolerant(os.path.join(root, ".mcp.json"), create=True)
    data.setdefault("mcpServers", {})["engine-knowledge-graph"] = {"command": "uv"}
    _write_json(os.path.join(root, ".mcp.json"), data)
    sp = os.path.join(root, ".claude", "settings.json")
    sdata, _e = wiring._read_json_tolerant(sp, create=True)
    sdata.setdefault("hooks", {}).setdefault("SessionStart", []).append(
        {"matcher": "startup", "hooks": [{"type": "command",
         "command": "${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python .engine/tools/boot.py"}]})
    _write_json(sp, sdata)


_COLLISION_LABELS = {1: "A file where the engine keeps its own", 2: "A file you both use",
                     3: "A review rule that also covers the engine's files"}
# Plain renderings of the stable choice keys, so the demo SHOWS the operator the three real choices for each
# overlap (not just a count) — the maintainer can't read the code that asserts there are three.
_CHOICE_LABELS = {"accept": "accept", "leave-as-is": "leave yours as is", "abort": "stop and decide later"}


def _print_collision_ledger(res: dict, copy: dict) -> None:
    if res["clean"]:
        print("    " + copy["collision-none"])
        return
    for c in res["collisions"]:
        print(f"    • [{_COLLISION_LABELS.get(c['klass'], c['klass'])}] {', '.join(c['paths'])}")
        print(f"        {c['consequence']}")
        print(f"        your choices: " + " · ".join(_CHOICE_LABELS.get(ch, ch) for ch in c["choices"]))


def _classes(res: dict) -> set:
    return {c["klass"] for c in res["collisions"]}


def _shared_paths_flagged(res: dict) -> set:
    return {p for c in res["collisions"] if c["klass"] == 2 for p in c["paths"]}


def _demo_collisions() -> int:
    """Operator-runnable demonstration of the brownfield overlap check. Leads with the honest-ceiling banner
    and the NO-LIVE-TRIGGER disclosure, then runs the REAL detection against throwaway PRODUCT fixtures: a
    clean project shows no overlaps; a populated project surfaces one of each kind with its plain consequence
    and choices; and a project where the engine's entries are already in place shows the shared-file overlaps
    no longer flag (a safe re-run). Proves THIS real project's files — this tool included — are unchanged."""
    import tempfile
    print(_BANNER + "\n")
    print(_COLLISION_DEMO_NOTE + "\n")
    real_before = _snapshot_real_files()
    copy = load_copy()
    engine_paths = module_coherence.engine_owned_paths(module_coherence.discover_manifests())
    ok = True

    # Scenario A — a clean project: nothing of the project's overlaps with what the engine adds.
    print("— CLEAN PROJECT: nothing of the project's is in the way.")
    with tempfile.TemporaryDirectory() as tmp:
        _build_collision_fixture(tmp, populated=False)
        res = collision_check(root=tmp, engine_paths=engine_paths, copy=copy)
        _print_collision_ledger(res, copy)
        print(f"    → nothing to surface ({res['clean']}); the engine can set up alongside the project cleanly.")
        ok &= (res["clean"] and res["checked"]["engine_paths"] > 0)

    # Scenario B — a populated project: one overlap of each kind, each with a plain consequence + choices.
    print("\n— POPULATED PROJECT: each overlap is surfaced — what I'd do, and your three choices.")
    with tempfile.TemporaryDirectory() as tmp:
        _build_collision_fixture(tmp, populated=True)
        res = collision_check(root=tmp, engine_paths=engine_paths, copy=copy)
        _print_collision_ledger(res, copy)
        classes = _classes(res)
        every_actionable = all(c["consequence"] and len(c["choices"]) == 3 for c in res["collisions"])
        print(f"    → all three kinds surfaced ({classes == {1, 2, 3}}); each states a consequence and the "
              f"three choices ({every_actionable}); nothing was changed (the check only reads).")
        ok &= (classes == {1, 2, 3} and every_actionable and not res["clean"])

    # Scenario C — the engine's entries are already in place (a re-run): the shared-file overlaps don't re-flag.
    print("\n— ALREADY PARTLY SET UP: a re-run doesn't re-raise overlaps the engine has already settled.")
    with tempfile.TemporaryDirectory() as tmp:
        _build_collision_fixture(tmp, populated=True)
        before = _shared_paths_flagged(collision_check(root=tmp, engine_paths=engine_paths, copy=copy))
        _plant_engine_entries(tmp)
        after = _shared_paths_flagged(collision_check(root=tmp, engine_paths=engine_paths, copy=copy))
        settled = {".gitignore", ".mcp.json", ".claude/settings.json", "CLAUDE.md"}
        print(f"    → shared-file overlaps first time: {sorted(before)}")
        print(f"    → after the engine has settled its part: {sorted(after)} — the settled files no longer "
              f"re-flag ({settled.isdisjoint(after)}).")
        ok &= (settled <= before and settled.isdisjoint(after))

    # The isolation guarantee, shown by name (the check only reads, so this is belt-and-suspenders).
    print("\n— ISOLATION CHECK: did any of that touch THIS real project's files?")
    unchanged = _assert_real_files_unchanged(real_before)
    real_self = os.path.isfile(os.path.join(validate.ROOT, ".engine", "tools", "instantiator.py"))
    print(f"    → this project's own files are byte-for-byte unchanged: {unchanged}; this tool still exists: "
          f"{real_self}.")
    ok &= (unchanged and real_self)

    print("\n" + ("All overlap checks behaved." if ok else "AN OVERLAP CHECK DID NOT BEHAVE — see above."))
    return 0 if ok else 1


# ---- arrival demo (the live brownfield arrival end to end: REAL overlay + collision + floor + setup) ----

_ARRIVAL_DEMO_NOTE = (
    "What this is: the LIVE brownfield arrival — adding the engine to a project that already has its own "
    "files. It runs the REAL arrival against a throwaway practice project: the engine is overlaid from a "
    "stand-in release, overlaps are surfaced, the engine's working-guide block is inserted into the project's "
    "own CLAUDE.md (keeping the project's content), team review is recommended because the practice project "
    "already has more than one reviewer, and setup runs — all behind a pull request the owner would approve. "
    "Faked only at the edges: fetching the release, the engine's own tools install, GitHub, and opening the "
    "pull request. What it can't prove here: a real outside project, fetched from a real release, arrives the "
    "same way — this construction repo never has one arrive. 'It behaved on the practice project ⇒ it behaves "
    "for a real adopter' is the step the fixture cannot discharge. Nothing here touches your real project."
)


def _build_arrival_product(root: str) -> None:
    """A throwaway LIVE product repo the engine arrives onto: the project's own source + working guide
    (CLAUDE.md, no engine floor) + .gitignore (class-2 overlaps), a multi-owner CODEOWNERS (class-3 overlap AND
    the 'a team already reviews here' signal), and the project's own SECURITY.md / README (no engine marker) /
    LICENSE (the engine must leave all three as they are). Deliberately NO files under .engine/ — keeping a
    product file inside the engine's own namespace is a separate, consistency-flagged choice; this fixture
    proves the clean arrival. Its own tempdir; never the real tree."""
    os.makedirs(os.path.join(root, "src"))
    with open(os.path.join(root, "src", "app.py"), "w", encoding="utf-8") as fh:
        fh.write("print('the product')\n")
    with open(os.path.join(root, "CLAUDE.md"), "w", encoding="utf-8") as fh:
        fh.write("# Our product's working guide\n\nHow we work here. Build with make.\n")
    with open(os.path.join(root, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write("node_modules/\n*.log\n")
    os.makedirs(os.path.join(root, ".github"))
    with open(os.path.join(root, ".github", "CODEOWNERS"), "w", encoding="utf-8") as fh:
        fh.write("* @product/alice @product/bob\n")          # two reviewers → class-3 + team signal
    with open(os.path.join(root, "SECURITY.md"), "w", encoding="utf-8") as fh:
        fh.write("# Security\n\nEmail security@ourproduct.example.\n")
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("# Our Product\n\nWhat it does.\n")          # no engine marketing marker → left untouched
    with open(os.path.join(root, "LICENSE"), "w", encoding="utf-8") as fh:
        fh.write("Apache License 2.0\n\nCopyright 2026 Our Product Inc.\n")  # not the template seed → untouched


def arrival_demo() -> bool:
    """Operator-runnable demonstration of the LIVE brownfield arrival. Builds a throwaway live product and a
    stand-in extracted release, then runs the REAL arrive() twice — accept and abort — with only the edges
    faked (release fetch, uv, GitHub, PR opener). Asserts: the accept run surfaces the overlaps, overlays the
    engine onto the product, inserts EXACTLY ONE engine floor into the product's own CLAUDE.md while keeping
    the product's content (and never copies the release's construction CLAUDE.md over it), recommends team
    review, leaves the product's SECURITY/README/LICENSE as they are, and opens one pull request; the abort
    run changes nothing and opens no pull request; and this real repo's files are untouched throughout."""
    import tempfile
    print(_BANNER + "\n")
    print(_ARRIVAL_DEMO_NOTE + "\n")
    real_before = _snapshot_real_files()
    ok = True
    quiet = lambda text: None
    faked = dict(home_reader=lambda: {}, uv_present=lambda: None,
                 uv_installer=lambda: "uv", uv_runner=lambda uv, g: True,
                 consent=lambda kind: True, control_transport=_approve_transport(),
                 gh_refresh=lambda s: True, control_issues=_FakeIssues(), gh_api=lambda path: None,
                 control_repo="you/your-project", control_token="demo-token")

    # — ACCEPT: the engine arrives, surfacing every overlap and keeping the project's own content.
    print("— ADDING THE ENGINE (accept): overlay + surface + insert the floor + set up, behind a pull request.")
    with tempfile.TemporaryDirectory() as d:
        target, release = os.path.join(d, "product"), os.path.join(d, "release")
        os.makedirs(target)
        _build_arrival_product(target)
        _build_fixture(release)
        before_guide = _read_text_or(os.path.join(target, "CLAUDE.md"), "")
        prs = []
        res = arrive(target_root=target, release_tree=release, engine_release="v1.2.3",
                     keep=[], tier="team", handle="you", decide=lambda c: "accept", apply_changes=True,
                     announce=quiet, opener=lambda **kw: prs.append(kw) or {"number": 1}, **faked)
        guide = _read_text_or(os.path.join(target, "CLAUDE.md"), "")
        floors = guide.count(wiring._MD_FENCE_BEGIN_TOKEN)
        engine_landed = os.path.isfile(os.path.join(target, ".engine", "modules", "core", "manifest.json"))
        checks = {
            "the arrival proceeded": res["proceeded"],
            "the overlaps were surfaced": len(res["collisions"]) > 0,
            "a team was detected and recommended": bool(res.get("team", {}).get("detected")),
            "the engine's files were overlaid onto the product": engine_landed and len(res["overlaid"]) > 0,
            "exactly one engine floor was inserted into the project's CLAUDE.md": floors == 1,
            "the project's own guide text was kept": "How we work here." in guide,
            "the release's construction CLAUDE.md never overlaid the guide": "construction governance" not in guide,
            "the project's SECURITY file was left as it is": "security@ourproduct.example" in
                _read_text_or(os.path.join(target, "SECURITY.md"), ""),
            "the project's README was left as it is": _MARKETING_SEED_MARKER not in
                _read_text_or(os.path.join(target, "README.md"), ""),
            "the project's LICENSE was left as it is": "Our Product Inc." in
                _read_text_or(os.path.join(target, "LICENSE"), ""),
            "one pull request was opened for review": len(prs) == 1,
        }
        for label, passed in checks.items():
            print(f"    {'[ok]' if passed else '[FAIL]'} {label}")
            ok &= passed

    # — ABORT: the owner stops at an overlap; nothing is written and no pull request is opened.
    print("\n— STOPPING AT AN OVERLAP (abort): nothing is changed, no pull request is opened.")
    with tempfile.TemporaryDirectory() as d:
        target, release = os.path.join(d, "product"), os.path.join(d, "release")
        os.makedirs(target)
        _build_arrival_product(target)
        _build_fixture(release)
        snap = {p: _read_text_or(os.path.join(target, p), "")
                for p in ("CLAUDE.md", ".gitignore", ".github/CODEOWNERS")}
        prs = []
        res = arrive(target_root=target, release_tree=release, decide=lambda c: "abort", apply_changes=True,
                     announce=quiet, opener=lambda **kw: prs.append(kw) or {"number": 1}, **faked)
        after = {p: _read_text_or(os.path.join(target, p), "") for p in snap}
        no_engine = not os.path.isdir(os.path.join(target, ".engine"))
        checks = {
            "the arrival stopped": not res["proceeded"],
            "the project's files are byte-for-byte unchanged": after == snap,
            "no engine files were written": no_engine and not res["overlaid"],
            "no pull request was opened": len(prs) == 0,
        }
        for label, passed in checks.items():
            print(f"    {'[ok]' if passed else '[FAIL]'} {label}")
            ok &= passed

    # — ISOLATION: nothing above touched THIS real project's files.
    print("\n— ISOLATION CHECK: did any of that touch THIS real project's files?")
    unchanged = _assert_real_files_unchanged(real_before)
    real_self = os.path.isfile(os.path.join(validate.ROOT, ".engine", "tools", "instantiator.py"))
    print(f"    → this project's own files are byte-for-byte unchanged: {unchanged}; this tool still exists: "
          f"{real_self}.")
    ok &= (unchanged and real_self)

    print("\n" + ("The brownfield arrival behaved." if ok else "THE ARRIVAL DID NOT BEHAVE — see above."))
    return ok


def augment_demo() -> bool:
    """Behavioral demonstration of the brownfield RULESET AUGMENT: on a project that already protects its main
    branch with its OWN rule, the engine adds its two checks (and any missing floor protection) INTO that rule
    rather than standing up a second one — preserving everything else of the operator's, byte for byte — and a
    later clean removal takes back EXACTLY what was added. The REAL control-plane logic runs (apply →
    augment → verify, then de_bootstrap); only the GitHub network is faked. It can fail: the before/after of
    the operator's own rule is compared byte-for-byte, so a non-additive write would turn this red.

    Named inductive ceiling: 'works on this in-memory product ruleset ⇒ works on a real adopter's live
    branch-protection rule fetched from GitHub' is the step the fixture cannot discharge."""
    import copy as _copy
    ENG = list(bootstrap.protection_guard.REQUIRED_CHECKS)
    # The operator's OWN ruleset: a PR rule, their own required check, force-push/deletion protection, and a
    # deploy-bot bypass the engine must never disturb.
    product = {
        "id": 9, "name": "team protections", "target": "branch", "enforcement": "active",
        "node_id": "RRS_x", "_links": {"self": {"href": "x"}}, "created_at": "2026-01-01T00:00:00Z",
        "source": "owner/repo", "source_type": "Repository", "current_user_can_bypass": "always",
        "bypass_actors": [{"actor_id": 7, "actor_type": "Integration", "bypass_mode": "always"}],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "pull_request", "parameters": {"required_approving_review_count": 2,
                                                    "required_review_thread_resolution": True},
             "ruleset_id": 9, "ruleset_source_type": "Repository"},
            {"type": "required_status_checks",
             "parameters": {"required_status_checks": [{"context": "product-ci"}],
                            "strict_required_status_checks_policy": False}, "ruleset_id": 9},
            {"type": "non_fast_forward", "ruleset_id": 9},
            {"type": "deletion", "ruleset_id": 9},
        ],
    }
    store = {9: _copy.deepcopy(product)}

    def transport(method, path, body=None):
        h = {"X-OAuth-Scopes": "repo"}
        if method == "GET" and path.endswith("/rules/branches/main"):
            return 200, [{**r, "ruleset_id": rid, "ruleset_source_type": "Repository"}
                         for rid, rs in store.items() for r in rs["rules"]], h
        if method == "GET" and path.endswith("/rulesets"):
            return 200, [{"id": rid, "name": rs["name"]} for rid, rs in store.items()], h
        if method == "GET" and "/rulesets/" in path:
            return 200, _copy.deepcopy(store[int(path.rsplit("/", 1)[1])]), h
        if method == "PUT" and "/rulesets/" in path:
            rid = int(path.rsplit("/", 1)[1]); store[rid] = {**store[rid], **body, "id": rid}
            return 200, {"id": rid}, h
        if path.startswith("/repos/") and "/ruleset" not in path and "/rules" not in path:
            return 200, {"full_name": "owner/repo"}, h
        return 404, None, h

    print("=" * 70)
    print("AUGMENT DEMO — the engine joins a project that ALREADY protects its main branch.\n"
          "Only the GitHub network is faked; the real read-modify-write augment + de-bootstrap run.")
    ok = True

    def _bypass(rid):
        return store[rid].get("bypass_actors")

    def _pr_rule(rid):
        return next((r for r in store[rid]["rules"] if r["type"] == "pull_request"), None)
    # Compare the WRITABLE form (type + parameters): the engine strips read-only metadata (ruleset_id, …)
    # the PUT can't accept, so the operator's rule is preserved in its writable content, which is the promise.
    before_pr = bootstrap._project_rule(_copy.deepcopy(_pr_rule(9)))
    before_bypass = _copy.deepcopy(_bypass(9))

    cp = bootstrap.ControlPlane("owner/repo", "tok", transport=transport,
                                refresh_fn=lambda s: True, issues=_FakeIssues())
    res = cp.apply(branch="main", announce=lambda t: None)

    checks = bootstrap._bound_checks(store[9]["rules"])
    print("\nApply — the engine augments the operator's existing rule in place:")
    a_checks = {
        "the operator now has ONE rule, not two (no second ruleset created)": len(store) == 1,
        "the engine's two checks were added to the operator's rule": set(ENG).issubset(checks),
        "the operator's own check is still there": "product-ci" in checks,
        "the operator's pull-request rule is byte-for-byte unchanged": _pr_rule(9) == before_pr,
        "the operator's bypass list is byte-for-byte unchanged": _bypass(9) == before_bypass,
        "the outcome is recorded for an exact later removal":
            res.marker and res.marker.get("ruleset_mode") == "augmented"
            and res.marker.get("augmented_ruleset_id") == 9,
    }
    for label, good in a_checks.items():
        print(f"    [{'ok' if good else 'FAIL'}] {label}")
        ok = ok and good

    print("\nRemoval — de-bootstrap takes back EXACTLY what was added, leaving the operator's rule:")
    db = cp.de_bootstrap(marker=res.marker, announce=lambda t: None)
    r_checks = {
        "the engine's checks are gone; the operator's check remains":
            bootstrap._bound_checks(store[9]["rules"]) == {"product-ci"},
        "the operator's rule was NOT deleted (it is theirs)": 9 in store,
        "the operator's pull-request rule is byte-for-byte unchanged": _pr_rule(9) == before_pr,
        "the operator's bypass list is byte-for-byte unchanged": _bypass(9) == before_bypass,
        "removal reported it only un-augmented (no keep/drop choice)": db.get("status") == "unaugmented",
    }
    for label, good in r_checks.items():
        print(f"    [{'ok' if good else 'FAIL'}] {label}")
        ok = ok and good

    print("\n" + ("The brownfield augment behaved: additive on arrival, exact on removal, the operator's own\n"
                  "rule untouched throughout. Inductive ceiling: a real adopter's live rule fetched from\n"
                  "GitHub is the step this fixture cannot discharge."
                  if ok else "THE AUGMENT DID NOT BEHAVE — see above."))
    return ok


def _parse_apply_flags(argv: list) -> dict:
    """Translate the apply CLI flags into the per-kind operator decisions the apply phase consents on:
    `--install-uv` approves installing the engine's tools; `--plan-mode adopt|keep` answers the planning
    default conflict-offer. A decision absent from the map reads as 'not given' (conservative: the tools
    step waits, the planning conflict keeps the operator's own default)."""
    decisions = {}
    if "--install-uv" in argv:
        decisions["install-uv"] = True
    if "--plan-mode" in argv:
        i = argv.index("--plan-mode")
        if i + 1 < len(argv):
            decisions["plan-mode-adopt"] = (argv[i + 1] == "adopt")
    return decisions


def _flag_value(argv: list, name: str):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def _print_ledger_plain(res: dict) -> None:
    if res.get("refused"):
        print("Setup hasn't been confirmed yet — run the choices step first. Nothing was changed.")
        return
    for s in res["steps"]:
        print(f"  {_STEP_LABELS.get(s['step'], s['step'])}: {s['status']}"
              + (f" — {s['detail']}" if s.get("detail") else ""))
    if res.get("halted"):
        print("Setup paused at the tools step. Fix the cause and run setup again — it resumes from here.")


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    if argv and argv[0] == "apply-demo":
        return _apply_demo()
    if argv and argv[0] == "finish-demo":
        return _finish_demo()
    if argv and argv[0] == "collision-demo":
        return _demo_collisions()
    if argv and argv[0] == "arrival-demo":
        return 0 if arrival_demo() else 1
    if argv and argv[0] == "augment-demo":
        return 0 if augment_demo() else 1
    if argv and argv[0] == "confirm":
        keep = [k for k in (_flag_value(argv, "--keep") or "").split(",") if k]
        tier = _flag_value(argv, "--tier") or "solo"
        handle = _flag_value(argv, "--handle") or derive_handle()
        default_branch = _flag_value(argv, "--default-branch") or derive_default_branch()
        # The PRODUCT override (eADR-0026): the operator names an EXTERNAL product only when the engine builds a
        # repo DIFFERENT from the one it is deployed into. _external_product_or_none records it ONLY when it
        # genuinely differs from self (the deployed-into slug, compared normalized) — a self-equal override is
        # the common self-building case, left unstored (derived live), never a duplicate that could drift.
        ident = derive_identity()
        self_slug = (f"{ident['owner']}/{ident['name']}"
                     if ident.get("owner") and ident.get("name") else None)
        product = _external_product_or_none(_flag_value(argv, "--product-repository"), self_slug)
        res = confirm(keep, tier, handle=handle, default_branch=default_branch, product_repository=product)
        saved = (f"Saved your choices ({', '.join(sorted(res['manifest']['packages']))}; "
                 f"reviewer = {'a team' if tier == 'team' else 'on your own'}).")
        if res["manifest"].get("product_repository"):   # close the loop on the external path
            saved += f" Recorded what this engine builds: {res['manifest']['product_repository']}."
        print(saved)
        return 0
    if argv and argv[0] == "apply":
        # FIRST-RUN GUARD (#297): apply re-fires the one-time, file-replacing setup steps, so a bare hand-run on
        # an already-set-up project — or in this workshop — must do nothing. The setup walkthrough passes the
        # `--first-run` token; a bare apply refuses and points back there. The token (not a construction-repo
        # check) is what guards apply, because a legitimate apply interrupted before the floor swap is
        # CONTENT-IDENTICAL to the workshop, and the locked design requires that interrupted apply to RESUME.
        if "--first-run" not in argv:
            print(_APPLY_NOT_FIRST_RUN)
            return 0
        decisions = _parse_apply_flags(argv)
        res = apply(consent=lambda kind: decisions.get(kind, False))
        _print_ledger_plain(res)
        return 1 if res.get("refused") else 0
    if argv and argv[0] == "verify":
        # The consistency check. A hard finding pauses (exit 1) with a plain explanation + the two next
        # actions; clean is exit 0. The standing review-gate surfacing is boot's, so verify run on its own
        # leaves the gate status to the start-of-session check rather than re-checking GitHub here.
        # FIRST-RUN GUARD (#297): refuse while the root CLAUDE.md is still the construction file — the workshop,
        # or a generated repo whose setup has not finished. A real first-run verify runs only after apply
        # swapped the floor in, so this never blocks a legitimate run.
        if _root_is_construction():
            print(_WORKSHOP_NO_SETUP.format(what="check for consistency"))
            return 0
        res = verify()
        return 1 if res.get("paused") else 0
    if argv and argv[0] == "retire":
        # The tidy-up: refuses (exit 1) on an inconsistent setup — the irreversible self-delete never runs on
        # a broken setup; otherwise removes the one-time setup files, re-derives the saved information, and
        # confirms completion (exit 0).
        # FIRST-RUN GUARD (#297): refuse while the root CLAUDE.md is still the construction file. This is the
        # highest-severity case — a bare retire in the workshop would self-delete the REAL instantiator, tests,
        # demos, and setup skill. A real first-run retire runs only after apply swapped the floor in.
        if _root_is_construction():
            print(_WORKSHOP_NO_SETUP.format(what="tidy up"))
            return 0
        res = retire()
        return 1 if res.get("refused") else 0
    if argv and argv[0] == "arrive":
        # BROWNFIELD ARRIVAL — run from the EXTRACTED release against a live project (--target). Without
        # --accept-all the run is SURFACE-ONLY: it shows every overlap, read-only, and changes nothing (even on
        # a clean project), so the operator can review first. With --accept-all (after that review) it overlays
        # the engine, inserts the floor, runs setup, and opens the arrival as a reviewed pull request. The
        # release tree defaults to this extracted engine's own root.
        target = _flag_value(argv, "--target") or os.getcwd()
        release = _flag_value(argv, "--release-tree") or validate.ROOT
        keep = [k for k in (_flag_value(argv, "--keep") or "").split(",") if k]
        tier = _flag_value(argv, "--tier") or "solo"
        handle = _flag_value(argv, "--handle") or derive_handle()
        ref = _flag_value(argv, "--engine-release")
        accept_all = "--accept-all" in argv
        decide = (lambda c: "accept") if accept_all else (lambda c: "abort")
        opener = module_manager._open_upgrade_pr if accept_all else None
        try:
            res = arrive(target_root=target, release_tree=release, engine_release=ref, keep=keep, tier=tier,
                         handle=handle, decide=decide, apply_changes=accept_all, opener=opener)
        except Exception as exc:  # noqa: BLE001 — a live write to someone's project must never end in a raw
            print(f"The arrival hit an unexpected problem and stopped: {exc}. Check the project's working "  # traceback
                  "tree, undo any partial change with git if needed, and run the arrival again.")
            return 1
        if res["proceeded"]:
            print(res.get("reason") or "The engine arrived: its files are in place, every overlap was "
                  "settled, and the change is open for you to review and approve.")
            return 0
        if res.get("stopped_on") in ("release", "overlay"):
            print(res.get("reason"))
            return 1
        if res.get("stopped_on"):                       # an --accept-all run the operator stopped at an overlap
            print(res.get("reason"))
            return 0
        # Surface-only (no --accept-all): everything was shown, read-only, nothing changed.
        if res["collisions"]:
            print("Those are the overlaps. Review them with the owner, then run `arrive --accept-all` to go on "
                  "(or keep anything you want by sorting it out first, then run the arrival again).")
        else:
            print("No overlaps — the engine can be added cleanly. Run `arrive --accept-all` to add it.")
        return 0
    if argv and argv[0] == "collision-check":
        # The overlap check's LIVE caller is `arrive` (brownfield arrival, #234). Run on its own in THIS
        # construction repo (engine == product) there is nothing to detect — it would read every engine file as
        # a project file colliding with itself — so the verb short-circuits, read-only. The real check runs
        # from the extracted release against a live project; the detection is exercised by `arrive`,
        # `collision-demo`, and the tests.
        print("This is the workshop where the engine is built — the overlap check runs when the engine is "
              "added to a project that already has its own files. Nothing to detect here.")
        return 0
    # `show` (or no argument): the read-only gather walkthrough. Short-circuit "already set up" ONLY when a
    # manifest is present AND this is NOT a downstream copy still pending setup (#353). The manifest TRAVELS
    # with the template, so its mere presence cannot mean "provisioned": a fresh generated copy inherits one.
    # A downstream copy (its origin differs from the recorded update home) falls into the gather and offers
    # setup; the workshop (origin == home) and any repo whose origin can't be read stay short-circuited
    # (safe-quiet — never a false "set up your project" in the workshop itself).
    if is_provisioned() and not module_coherence.is_downstream_copy(boot.repo_slug()):
        print(_ALREADY_SET_UP)
        return 0
    print(present_gather())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
