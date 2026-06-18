#!/usr/bin/env python3
"""First-run setup orchestrator (core slice 27a) — the GATHER and CONFIRM half.

The instantiator stands a freshly-generated repo up: it derives the repo's coordinates, takes the
operator's one non-derivable choice (the identity tier) and their feature selection, then writes the engine
manifest as the resumability checkpoint — after which the later phases (apply, verify, retire) install the
selection and the engine's own guardrails. This file ships the **non-destructive front half**: GATHER
(present the choices) and CONFIRM (write the checkpoint). The destructive/installing APPLY phase, the VERIFY
pause, and self-RETIRE land in core slices 27b/27c.

The signal model (provisioning README §gather→confirm→apply→verify→retire, ground-truthed):
- The instantiator's own presence is the "this repo is not set up yet" signal; it self-deletes at retire,
  so its absence means setup is done. Within a run, the **engine manifest** is the checkpoint — absent means
  the operator has not confirmed yet (re-offer everything), present means they have (resume the install).
  We key `is_provisioned()` off the manifest's presence; we introduce **no new state file** (the manifest is
  the checkpoint, by design).
- THE DEGENERACY (the loudest line): this tool NEVER runs in the construction repo — `engine-template` is the
  template tree, not a generated repo (stage-0 §6). Its only evidence is the fixture demonstration below,
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
import bootstrap          # noqa: E402  (ControlPlane + render — the control-plane bootstrap; _parse_sections)
import security_floor     # noqa: E402  (the native-scanning toggles — reuses ControlPlane's transport)

# These sibling tools import only the Python standard library plus each other (validate binds its two
# third-party packages LAZILY, slice 27b-pre), and every one carries `from __future__ import annotations`
# (so an `X | None` annotation never evaluates at import) — both are LOAD-BEARING: the apply phase below
# runs on the operator's SYSTEM python BEFORE it installs the engine's own 3.11+ tool-runtime (D-156), and
# system python on macOS is 3.9. `test_instantiator` proves the whole chain imports + starts there.

# The recognized SDLC discipline groups the optional features are presented under (D-067 / D-046: recognized,
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
_EMPTY_CATALOG_LINE = ("There are no optional add-ons to choose yet — the essentials are already included, "
                       "and I'll set those up when you confirm.")
_TIER_PROMPT = (
    "One choice only you can make — who reviews changes here:\n"
    "  • On your own: I'll make changes as you, and you approve each one. (The usual choice.)\n"
    "  • With a team: I'll make changes under a separate name, and a teammate approves them."
)
_DESELECT_PREFACE = (
    "When you confirm: the optional add-ons you did NOT keep will be removed from this project — their files\n"
    "are deleted, not just switched off. Wanting one later is a fresh request, not a checkbox you flip back."
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
    "conduct-seeded": "Your stance came with this project",
    "security-seeded": "A security-contact file came with this project",
    "readme-seeded": "Your project's front page is now yours",
    "codeowners-degraded": "If I couldn't set up file ownership for reviews",
    "control-plane-unavailable": "If I couldn't reach your project on GitHub",
    # The finish (verify + tidy-up) phase — slice 27c.
    "verify-paused": "If something needs fixing before finishing",
    "verify-next-actions": "Your two ways forward",
    "verify-ok": "Setup checks out",
    "verify-gate-on": "Your review gate is on",
    "verify-gate-pending": "Your review gate isn't on yet",
    "retire-success": "Setup is complete",
    # The brownfield overlap check — slice 27d (no live caller yet; see the collision-check section).
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
        "overlap is no surprise. Your choices: add the engine's rule (it takes priority for the engine's "
        "files) · leave your rules as they are · stop, and decide later."
    ),
    "collision-none": (
        "Good news — none of your files or settings overlap with what the engine adds. I can set it up "
        "alongside your project cleanly."
    ),
    "collision-unreadable": (
        "I couldn't make sense of {paths} just now, so I've left it completely untouched rather than risk "
        "changing it wrongly. Take a look at it, then run setup again — I'll pick up from here."
    ),
    "conduct-seeded": (
        "This project came set up with a starting set of codes of conduct — short notes on how you like me "
        "to work with you (for example, speaking plainly, and explaining choices before you make them). "
        "They're here from the first session, and they're yours: change, add, or remove any of them any time "
        "with /engine-conduct. I didn't put them in place silently — this note is me telling you they're here."
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
    """True when this repo has already been set up — keyed off the engine manifest's presence (the
    checkpoint the design uses; no separate state is introduced). A fresh generated repo has no
    confirm-written manifest yet, so setup runs; the construction repo has one, so setup short-circuits."""
    return os.path.isfile(_engine_manifest_path(root))


def derive_identity(root: str | None = None) -> dict:
    """The repo coordinates read from git/GitHub (derive-first) — owner, name, and the protected branch.
    Best-effort: any field is None when it cannot be read, and the walkthrough says so rather than guessing.
    (The operator handle, used later to render code-ownership, is captured in the apply phase — slice 27b.)"""
    slug = boot.repo_slug()
    owner, name = slug.split("/", 1) if slug and "/" in slug else (None, None)
    return {"owner": owner, "name": name, "branch": boot.PROTECTED_BRANCH}


def selectable(catalog_entries: list) -> dict:
    """The catalog's optional features grouped by their SDLC discipline, in the fixed category order, ready to
    present as choices. Only the catalog set is offered — the always-present essentials (the required spine)
    are never a choice (D-067). An entry whose category is unrecognized is grouped last under its own raw label
    rather than dropped (degrade, never hide a real option). All entries are presented uniformly today; a
    catalog `status` of `default-on` (added unless opted out) or `experimental` (opt-in) will want a distinct
    default-state/label — `owes →` whoever first populates the catalog (the catalog ships empty here)."""
    grouped: dict = {cat: [] for cat in _CATEGORY_ORDER}
    for entry in catalog_entries:
        grouped.setdefault(entry.get("category") or "Other", []).append(entry)
    # Drop empty recognized groups, keep any non-empty (including an unexpected category), category order first.
    ordered = [c for c in _CATEGORY_ORDER if grouped.get(c)]
    extra = sorted(c for c in grouped if c not in _CATEGORY_ORDER and grouped.get(c))
    return {c: sorted(grouped[c], key=lambda e: e.get("verb", "")) for c in ordered + extra}


def present_gather(root: str | None = None, catalog_path: str | None = None) -> str:
    """The plain-language GATHER walkthrough the operator reads: the repo coordinates I derived, the one
    identity choice, the optional features to pick from (grouped by discipline, or the no-add-ons line when
    the catalog is empty), and the plain statement that not-kept add-ons are deleted on confirm. Pure text —
    no prompts, no writes; the skill/runbook does the asking."""
    ident = derive_identity(root)
    coords = (f"{ident['owner']}/{ident['name']}" if ident["owner"] and ident["name"]
              else "(I couldn't read your project's name from GitHub — I'll ask you instead)")
    lines = [
        "Setting up your project. Here's what I found and what I'll ask you:",
        "",
        f"Your project: {coords}",
        f"The branch I'll protect with a review gate: {ident['branch']}",
        "",
        _TIER_PROMPT,
        "",
        "Optional add-ons you can include or leave out:",
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
                lines.append(f"    • {entry['verb']} — {entry['description']}")
            lines.append("")
    lines.append(_DESELECT_PREFACE)
    return "\n".join(lines)


def confirm(kept_optional_ids: list, tier: str, *, root: str | None = None,
            engine_release: str | None = None, handle: str | None = None) -> dict:
    """CONFIRM — write the engine manifest, the resumability checkpoint (D-024). Records the engine release,
    the identity tier, the kept package set (the always-present required spine plus the optional features the
    operator kept — an unkept optional is simply left out of the manifest, its files removed later in the
    apply phase, not here), and the operator's handle when known (the preserved-config owner the apply phase
    renders code-ownership from; omitted when None, keeping the manifest valid either way). This is the single
    committing step; before it, nothing is written. Returns the written path and the manifest. `root`,
    `engine_release`, and `handle` are injectable for tests and the demo."""
    kept = set(kept_optional_ids or [])
    packages: dict = {}
    for _rel, manifest in module_coherence.discover_manifests():
        mid, status = manifest.get("id"), manifest.get("status")
        if not mid:
            continue  # a manifest with no id fails the module schema upstream; never crash the committing step
        if status == "required" or mid in kept:
            packages[mid] = str(manifest.get("version") or "")
    release = engine_release or _existing_release(root) or "0.0.0-dev"
    written = {"engine_release": release, "packages": dict(sorted(packages.items())), "identity": tier}
    if handle:
        written["handle"] = handle
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


def _existing_release(root: str | None = None) -> str | None:
    """The engine release recorded in an existing manifest, if any — so a re-run keeps the same release
    rather than resetting it. None when there is no readable manifest."""
    try:
        with open(_engine_manifest_path(root), encoding="utf-8") as fh:
            return json.load(fh).get("engine_release")
    except Exception:
        return None


# ==== APPLY (core slice 27b) — install the confirmed selection and turn on the engine's guardrails =====
#
# The seven ordered, idempotent, manifest-driven apply steps (provisioning README §gather→…→apply). The
# phase runs on the operator's SYSTEM python; steps 1–3 need nothing extra, step 4 materializes the engine's
# own tool-runtime (uv + the .venv), and steps 5–7 follow. Each step degrades INTERNALLY (it never crashes
# the phase) — EXCEPT a degraded tool-runtime (step 4), which HALTS the phase, because steps 5–7 presuppose
# a materialized runtime; a retry resumes from the manifest checkpoint. Apply ENDS after the control-plane
# attempt — the verify/coherence pause and self-retire are the next phase (slice 27c).
#
# THE DEGENERACY (unchanged from gather/confirm): none of this runs in the construction repo. The demo runs
# the REAL step logic against a throwaway generated-repo fixture, faking ONLY the external boundaries (the
# operator's home settings, the uv install + sync, the GitHub review-gate calls). "Works on the fixture ⇒
# works for a real adopter" is the named inductive step the fixture cannot discharge.

UV_PIN = "0.11.8"  # the pinned uv version to bootstrap — MUST match the committed CI pin
                   # (.github/workflows/engine-ci.yml + engine-guard.yml `version:`) so the runtime the
                   # instantiator materializes matches the engine's resolved uv.lock. (Faked in every test
                   # and the demo; a real install runs only on a generated repo. owes → a check tying this
                   # constant to the workflow pin so the two can't drift.)
UV_INSTALL_DIR_REL = os.path.join(".engine", ".uv")
UV_INSTALL_URL = f"https://astral.sh/uv/{UV_PIN}/install.sh"


def _codeowners_path() -> str:
    return os.path.join(validate.ROOT, ".github", "CODEOWNERS")


def _read_home_settings() -> dict:
    """The operator's OWN global Claude settings, read-only. The engine reads the interactive default from
    here but NEVER writes `~/.claude` — the operator's global settings are the operator's (D-185). Returns
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
    LOUD and never falls back to system python (D-156)."""
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


def _apply_codeowners(handle, say, copy) -> dict:
    """STEP 2 — render the engine's code-ownership block so any change to the engine's own files routes to
    the operator for review. The owner is the stored handle; with no handle the renderer refuses, so the step
    DEGRADES (announce + skip), never crashes (Q7). Write-iff-changed (idempotent)."""
    if not handle:
        say(copy["codeowners-degraded"])
        return {"step": "codeowners", "status": "degraded", "detail": "no operator handle available"}
    path_set = module_coherence.engine_owned_paths(module_coherence.discover_manifests())
    # CODEOWNERS is itself a foundation infrastructure artifact (it must be engine-owned, or a product rule
    # could shadow it), but the shared path-set is file-precise over EXISTING files and this step is the one
    # that CREATES it — so on a greenfield first pass it would be absent from its own block, complete only on
    # a re-render. Include it explicitly so the block owns itself from the first render and a resume is a true
    # no-op (the engine/product wall covers the highest-trust file immediately).
    co_rel = ".github/CODEOWNERS"
    if co_rel not in path_set:
        path_set = sorted(set(path_set) | {co_rel})
    co_path = _codeowners_path()
    existing = validate.read(co_path) if os.path.isfile(co_path) else ""
    try:
        new_text = wiring.render_codeowners(existing, path_set, handle)
    except wiring.WiringError:
        say(copy["codeowners-degraded"])
        return {"step": "codeowners", "status": "degraded", "detail": "could not render ownership"}
    if new_text == existing:
        return {"step": "codeowners", "status": "already", "owner": handle}
    os.makedirs(os.path.dirname(co_path), exist_ok=True)
    with open(co_path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    return {"step": "codeowners", "status": "written", "owner": handle, "paths": len(path_set)}


def _apply_plan_mode(home_reader, settings_path, consent, say, copy) -> dict:
    """STEP 3 — recommend the planning permission-mode as this repo's interactive default, obeying
    yield-to-the-operator (D-185). Read the operator's GLOBAL default read-only (never write `~/.claude`);
    with no conflicting preference (or one already planning) ADOPT it into the project settings with a plain
    disclosure; on a conflict OFFER adopt-or-keep once — keep writes nothing (the yield). Idempotent: a
    project default already set to planning is a no-op. The project write is surgical (preserves the
    operator's other settings)."""
    proj_path = settings_path or wiring.SETTINGS_PATH
    proj, err = wiring._read_json_tolerant(proj_path, create=True)
    if err is not None:
        return {"step": "plan-mode", "status": "degraded", "detail": "project settings unreadable"}
    if (proj.get("permissions") or {}).get("defaultMode") == "plan":
        return {"step": "plan-mode", "status": "already"}
    home = home_reader() if home_reader is not None else _read_home_settings()
    global_mode = (home.get("permissions") or {}).get("defaultMode") if isinstance(home, dict) else None
    if global_mode not in (None, "plan"):                      # a conflicting operator preference → offer once
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
    "<!-- Your own codes of conduct go here — add, revise, or remove them with /engine-conduct. They sit "
    "alongside the engine's defaults and take priority when they share an id. This file is yours: an engine "
    "update never overwrites it. It starts empty — the engine's defaults are already in force. -->\n"
)


def _seed_conduct(say, copy=None) -> str:
    """Seed the operator's codes-of-conduct override from the maintainer's template seed — the seed-then-own
    pattern (like the permission-mode default). Copies .engine/provisioning/conduct-seed.md into the committed
    .engine/conduct/operator.md; an absent or empty seed yields a valid empty override, never an error. Then
    discloses, in plain language, that the stance is present and theirs to tune (never silent). Paths are
    validate.ROOT-relative, so a redirected demo/test seeds only the fixture, never the real tree."""
    seed_path = os.path.join(validate.ROOT, ".engine", "provisioning", "conduct-seed.md")
    target = os.path.join(validate.ROOT, ".engine", "conduct", "operator.md")
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
    seed-then-own pattern, the same SHAPE and DISCLOSURE as _seed_conduct, but COPY-IF-ABSENT: unlike the conduct
    seed it NEVER overwrites. If the project already carries a SECURITY.md in any GitHub-recognized location
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
# on, so the replace can only ever touch the engine's own landing page, never operator content (D-213/D-214,
# topology law 2). The landing front LEADS with this marker (the recognizer requires it at the file's start, not
# merely somewhere inside) — so a README that only mentions the marker in passing is NOT a match and is preserved
# (the conservative preserve-on-any-doubt law; #134 authors the marketing copy BELOW this leading marker). Stable
# across marketing-copy rewrites; a fingerprint would instead need updating on every wording tweak or the replace
# silently dies. The starter carrying no marker makes a re-run a true no-op (the engine never re-touches the root
# README after instantiation).
_MARKETING_SEED_MARKER = "<!-- engine-template:landing-front -->"

# A minimal, valid product-starter README used only when the maintainer's template seed is absent or empty — never
# an error (the same fallback shape as _DEFAULT_SECURITY_MD). It carries the D-067 required-spine disclosure in plain
# operator language (no maintainer vocabulary) and the D-095 no-automated-style-floor gap, and it carries NO marker.
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
    the same SHAPE and DISCLOSURE as _seed_security, but REPLACE-IFF-MARKETING-SEED instead of copy-if-absent
    (D-213/D-214). At rest in the template the root README is the engine's marketing landing front; "Use this
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


def _apply_substrates(say, copy=None) -> dict:
    """STEP 5 — initialize the kept set's committed substrates (runs AFTER the runtime materializes). Today:
    re-derive the knowledge graph (idempotent), confirm the state seed is present, seed the operator's
    codes-of-conduct override from the template seed, seed a root SECURITY.md disclosure channel, and seed the
    product's own starter README over the engine's marketing landing front (all disclosed in plain language).
    The three seeds sit here — co-located, AFTER the runtime — a deliberate mirror of the as-built conduct-seed
    placement: a pure file copy has no runtime dependency, and on a tool-runtime halt the phase never reaches
    here, so a seed lands on the resume (each is idempotent — copy-if-absent for conduct/security, replace-iff-
    marketing-seed for the README). The graph path is bound at import (knowledge_gen), so the demo redirects it
    AND we pass it explicitly — a redirected run never rewrites the real graph. Memory-backup setup is owed to
    the memory module (not yet built)."""
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
    return result


def _apply_wires(say) -> dict:
    """STEP 6 — install EVERY kept module's wiring: the hooks (boot, the exploration write-gate, the close
    gate, the commit-boundary refresh), the knowledge query server, and the cache ignores. Until this runs a
    generated repo's settings carry no engine hooks, so the engine is inert — this is the step that turns it
    on (provisioning README L110–114). Reuses wiring.apply_all exactly as module-add does; insert-iff-absent
    (idempotent)."""
    applied = []
    for _p, m in module_coherence.discover_manifests():
        for f in wiring.apply_all(m.get("wires") or []):
            applied.append(validate.fmt(f))
    return {"step": "wires", "status": "done", "applied": applied}


def _apply_control_plane(control_transport, gh_refresh, control_issues, say, copy, repo=None, token=None) -> dict:
    """STEP 7 — turn on the protected-branch review gate (the control-plane bootstrap, the permanent
    primitive). Degrades LOUD when the repo/sign-in/capability is unavailable (never fakes the gate). Apply
    ENDS regardless — the gate can be completed any time later, and boot keeps surfacing an unprotected repo.
    Every boundary is injected — the repo coordinates + token AND the GitHub transport — so tests/the demo run
    the real orchestration deterministically, independent of the ambient environment (e.g. CI's own token)."""
    repo = repo or boot.repo_slug()
    token = token or boot.gh_token()
    if not repo or not token:
        say(copy["control-plane-unavailable"])
        return {"step": "control-plane", "status": "degraded", "detail": "no project/sign-in", "protected": False}
    cp = bootstrap.ControlPlane(repo, token, transport=control_transport, refresh_fn=gh_refresh,
                                issues=control_issues)
    result = cp.apply(branch=boot.PROTECTED_BRANCH, announce=say)
    say(bootstrap.render(result))
    return {"step": "control-plane", "status": result.status, "protected": result.is_protected()}


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


def apply(*, root=None, announce=None, home_reader=None, settings_path=None, uv_present=None,
          uv_installer=None, uv_runner=None, consent=None, control_transport=None, gh_refresh=None,
          control_issues=None, control_repo=None, control_token=None, handle=None) -> dict:
    """The apply phase: run the eight ordered steps against the confirmed manifest. Refuses (no change) when
    the manifest is absent — apply presupposes a confirmed selection. The handle is the passed one, else the
    one the manifest stored. Returns a step ledger: {refused, halted, steps:[…]}. A degraded tool-runtime
    sets `halted` and the remaining steps are not attempted (they presuppose the runtime); every other step
    degrades in place. Apply does NOT verify, pause, or retire (slice 27c). Every external boundary is
    injectable, so this runs the REAL control flow under a fixture with nothing real touched."""
    say = announce if announce is not None else (lambda text: print(text))
    copy = load_copy()
    manifest = module_coherence.load_engine_manifest()
    if not manifest:
        return {"refused": True, "reason": "not-confirmed", "halted": False, "steps": []}
    handle = handle if handle is not None else manifest.get("handle")
    steps = [_apply_delete_unselected(manifest, say),
             _apply_codeowners(handle, say, copy),
             _apply_plan_mode(home_reader, settings_path, consent, say, copy)]
    runtime = _apply_tool_runtime(uv_present, uv_installer, uv_runner, consent, say, copy)
    steps.append(runtime)
    if runtime.get("halt"):
        return {"refused": False, "halted": True, "steps": steps}
    steps.append(_apply_substrates(say, copy))
    steps.append(_apply_wires(say))
    steps.append(_apply_control_plane(control_transport, gh_refresh, control_issues, say, copy,
                                      repo=control_repo, token=control_token))
    steps.append(_apply_security_toggles(control_transport, say, copy,
                                         repo=control_repo, token=control_token))
    return {"refused": False, "halted": False, "steps": steps}


# ==== VERIFY + RETIRE (core slice 27c) — the first-run lifecycle close =================================
#
# After apply installs the selection and turns the guardrails on, VERIFY confirms the result is consistent
# and RETIRE tidies the one-time setup assets away. Both run in the SAME system-python instantiator process
# as apply (they reuse only stdlib + sibling tools — check_coherence and knowledge_gen.generate read JSON +
# walk the tree, never yaml/jsonschema), so they start on a bare adopter machine like the rest of the phase.
#
# The locked ordering (provisioning README §verify/§retire, ground-truthed): a HARD consistency finding at
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
    ".engine/operations/first-run.md",
    ".engine/templates/first-run.md",
)
_FIRST_RUN_ASSET_DIRS = (os.path.join(".claude", "skills", "engine-setup"),)


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
    preserved, graph, steps}."""
    say = announce if announce is not None else (lambda text: print(text))
    copy = load_copy()
    base = root or validate.ROOT
    hard = _hard_findings()
    if hard:
        _say_consistency_pause(say, copy, hard)
        return {"refused": True, "reason": "inconsistent", "deleted": [], "already_absent": [],
                "preserved": [], "graph": "unchanged",
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
    _drop_bytecode(base, ("instantiator", "test_instantiator"))
    graph_status = "regenerated"
    try:
        knowledge_gen.generate(path=knowledge_gen.GRAPH_PATH)  # so the saved information no longer lists the
    except Exception as exc:  # noqa: BLE001 — degrade-and-disclose; never crash the close   # removed tools
        graph_status = f"skipped ({type(exc).__name__})"
    preserved = [".engine/tools/module_catalog.py", ".engine/tools/test_module_catalog.py",
                 ".engine/schemas/provisioning-catalog.v1.json", ".engine/provisioning/module-catalog.json"]
    say(copy["retire-success"])
    return {"refused": False, "deleted": deleted, "already_absent": already, "preserved": preserved,
            "graph": graph_status, "steps": [{"step": "retire", "status": "done", "deleted": deleted}]}


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
             knowledge_gen.KNOWLEDGE_DIR, knowledge_gen.GRAPH_PATH)
    validate.ROOT = root
    validate.ENGINE_DIR = os.path.join(root, ".engine")
    validate.CATALOG_PATH = os.path.join(root, ".engine", "schemas", "surface-catalog.json")
    wiring.SETTINGS_PATH = os.path.join(root, ".claude", "settings.json")
    wiring.MCP_PATH = os.path.join(root, ".mcp.json")
    wiring.GITIGNORE_PATH = os.path.join(root, ".gitignore")
    wiring.CATALOG_PATH = validate.CATALOG_PATH
    knowledge_gen.KNOWLEDGE_DIR = os.path.join(root, ".engine", "knowledge")
    knowledge_gen.GRAPH_PATH = os.path.join(knowledge_gen.KNOWLEDGE_DIR, "graph.json")
    try:
        yield
    finally:
        (validate.ROOT, validate.ENGINE_DIR, validate.CATALOG_PATH,
         wiring.SETTINGS_PATH, wiring.MCP_PATH, wiring.GITIGNORE_PATH, wiring.CATALOG_PATH,
         knowledge_gen.KNOWLEDGE_DIR, knowledge_gen.GRAPH_PATH) = saved


# A representative subset of core's real wiring for the fixture: hooks across the gating events (boot at
# session start, the exploration write-gate + commit-boundary refresh on PreToolUse, the close gate on Stop),
# the knowledge query server, and a cache ignore. The apply phase installs ALL of these — proving a generated
# repo gets its HOOKS, not only its query server (provisioning README L110–114), into a hook-less settings.
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
                              "tool": [".engine/tools/*.py"],
                              "operation": [".engine/operations/*.md"],
                              "template": [".engine/templates/*.md"]},
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

# The construction repo's own files the apply steps would write if redirection ever leaked — the demo asserts
# they are byte-for-byte unchanged afterward (the isolation guarantee, shown mechanically, not just claimed).
_REAL_ISOLATION_FILES = (".engine/knowledge/graph.json", ".claude/settings.json", ".mcp.json",
                         ".gitignore", ".github/CODEOWNERS", "CLAUDE.md", "SECURITY.md", "README.md",
                         ".engine/conduct/operator.md", ".engine/conduct/defaults.md")


class _FakeIssues:
    """Stands in for the GitHub label boundary so the demo never creates a real label."""
    def ensure_label(self):
        return None


def _approve_transport():
    """An in-memory GitHub where the operator can administer the repo and the branch starts UNprotected:
    apply creates the engine ruleset and the verify read then sees the floor met → a true 'applied'."""
    state = {"met": False}

    def t(method, path, body=None):
        headers = {"X-OAuth-Scopes": "repo"}
        if method == "GET" and path.endswith("/rules/branches/main"):
            return 200, (bootstrap.floor_ruleset()["rules"] if state["met"] else []), headers
        if method == "GET" and path.endswith("/rulesets"):
            return 200, [], headers
        if method == "POST" and path.endswith("/rulesets"):
            state["met"] = True
            return 201, {"id": 901, "name": (body or {}).get("name", "")}, headers
        if method == "PUT" and "/rulesets/" in path:
            state["met"] = True
            return 200, {"id": 901}, headers
        sec = _security_floor_responses(method, path, body, available=True)
        if sec is not None:
            return sec[0], sec[1], headers
        if path.startswith("/repos/"):
            return 200, {"full_name": "you/your-project"}, headers
        return 404, None, headers
    return t


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
    pass turned the gate on) → a clean 'already', no write. The native security features read back as on."""
    def t(method, path, body=None):
        headers = {"X-OAuth-Scopes": "repo"}
        if method == "GET" and path.endswith("/rules/branches/main"):
            return 200, bootstrap.floor_ruleset()["rules"], headers
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
    read back as unavailable (the free-private/public-only gaps), so the floor discloses them."""
    def t(method, path, body=None):
        headers = {"X-OAuth-Scopes": "repo"}
        if method == "GET" and path.endswith("/rules/branches/main"):
            return 200, [], headers
        if method == "GET" and path.endswith("/rulesets"):
            return 200, [], headers
        sec = _security_floor_responses(method, path, body, available=False)
        if sec is not None:
            return sec[0], sec[1], headers
        if method in ("POST", "PUT"):
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
    "security-floor": "Turn on GitHub's native security features",
}
# A security-floor "applied" means every native toggle reached an honest outcome (on / already / pending /
# unavailable-and-disclosed); "skipped" is the clean no-project/sign-in case. Only a failed/unconfirmed
# toggle degrades the step.
_GOOD_STATUSES = {"done", "written", "adopted", "already", "materialized", "applied",
                  "kept-operator-default", "skipped"}


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
    """Operator-runnable demonstration of the APPLY phase. Runs the REAL seven-step apply logic against a
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
             "control-plane": "GitHub"}
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
        ended = (not res["halted"]) and cp["step"] == "control-plane" and len(res["steps"]) == 8
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
              f"({still_clean}); saved information re-derived ({r['graph']}).")
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


# ==== BROWNFIELD COLLISION CHECK (core slice 27d) — surface, never overwrite ===========================
#
# When the engine joins an ALREADY-POPULATED project (brownfield), it inspects the project for any overlap
# between what it would add and what is already there, and SURFACES each overlap in plain language with a
# concrete consequence + a three-way choice (accept / leave-as-is / abort) — never a raw path-versus-pattern
# report, and it NEVER silently overwrites (provisioning README §greenfield-and-brownfield; topology §the
# wall). Three kinds of overlap:
#   1. a product file sitting where the engine keeps its own files (the engine would replace it),
#   2. product content in a file the engine and the project both use (the engine adds its own marked section
#      and keeps the rest), and
#   3. a product review rule that also covers the engine's files (the engine adds its own, placed to win).
#
# DOUBLED DEGENERACY (the loudest line, doubled): this check has NO live caller yet. It never runs in the
# construction repo (engine == product), AND — because a project made from the template starts empty — it is
# a no-op even on a real GREENFIELD adopter. It fires only when the deferred brownfield-ARRIVAL path (overlay
# a fetched release onto a live tree, then run this) is built; until then its only exercise is the fixture
# demo below. "Works on the fixture ⇒ works when the deferred caller runs it on a real populated repo" is an
# inductive step the fixture cannot discharge — named, not hidden. The pure detection takes the engine path
# set INJECTED (the deferred caller passes the release-derived set, BEFORE the overlay writes), defaulting to
# the engine's own owned set so a test/demo can pass a real one.

# The platform-shared root files the engine co-occupies: it adds only its OWN marked/keyed entries and leaves
# the project's content alone (topology §the wall L40-49). An EXPLICIT set — there is no path constant for the
# root project guide, and it must CO-EXIST (never be seized, topology L49) — so a pre-existing one is surfaced
# as additive, never as "the engine would replace it". (CODEOWNERS is a shared file too, but its meaningful
# overlap is the review-rule shadow — class 3 — so it is handled there, not double-reported here.)
_COLLISION_SHARED = (".gitignore", ".mcp.json", ".claude/settings.json", "CLAUDE.md")
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
        # Presence-only: the engine has no CLAUDE.md section shape yet (it currently ships a whole file), so
        # we do not invent a marker — a pre-existing project guide is an additive overlap to surface. The
        # concrete coexistence mechanism (so a resume stops re-flagging) is a deferred brownfield-arrival owe.
        text = _read_text_opt(path)
        if text is None:
            return "unreadable"
        return "additive" if text.strip() else "empty"
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


# ---- collision demo (the consent instrument: REAL detection, throwaway PRODUCT fixtures) -------------

_COLLISION_DEMO_NOTE = (
    "Important — what this does and doesn't do yet: this overlap check has NO live trigger at the moment. A "
    "project made from this template starts empty, so there is nothing to overlap; the check only matters when "
    "the engine is added to a project that ALREADY has its own files — and that 'add to an existing project' "
    "path is not built yet. So this does not protect a real project today. What follows runs the REAL overlap "
    "logic against throwaway practice projects, to show it behaves; nothing here touches your real project."
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
    """Model the engine already wired into the shared files (a resume): an engine-managed block in .gitignore
    and an engine query server in .mcp.json — so those overlaps no longer flag on a re-run."""
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
        settled = {".gitignore", ".mcp.json", ".claude/settings.json"}
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
    if argv and argv[0] == "confirm":
        keep = [k for k in (_flag_value(argv, "--keep") or "").split(",") if k]
        tier = _flag_value(argv, "--tier") or "solo"
        handle = _flag_value(argv, "--handle") or derive_handle()
        res = confirm(keep, tier, handle=handle)
        print(f"Saved your choices ({', '.join(sorted(res['manifest']['packages']))}; "
              f"reviewer = {'a team' if tier == 'team' else 'on your own'}).")
        return 0
    if argv and argv[0] == "apply":
        decisions = _parse_apply_flags(argv)
        res = apply(consent=lambda kind: decisions.get(kind, False))
        _print_ledger_plain(res)
        return 1 if res.get("refused") else 0
    if argv and argv[0] == "verify":
        # The consistency check. A hard finding pauses (exit 1) with a plain explanation + the two next
        # actions; clean is exit 0. The standing review-gate surfacing is boot's, so verify run on its own
        # leaves the gate status to the start-of-session check rather than re-checking GitHub here.
        res = verify()
        return 1 if res.get("paused") else 0
    if argv and argv[0] == "retire":
        # The tidy-up: refuses (exit 1) on an inconsistent setup — the irreversible self-delete never runs on
        # a broken setup; otherwise removes the one-time setup files, re-derives the saved information, and
        # confirms completion (exit 0).
        res = retire()
        return 1 if res.get("refused") else 0
    if argv and argv[0] == "collision-check":
        # The overlap check runs when the engine is ADDED to a project that already has its own files. In this
        # construction repo (engine == product) there is nothing to detect — running it here would read every
        # engine file as a project file colliding with itself — so the verb short-circuits, read-only. The
        # detection logic (collision_check) is exercised by `collision-demo` and the tests; its live caller is
        # the deferred brownfield-arrival path.
        print("This is the workshop where the engine is built — the overlap check runs when the engine is "
              "added to a project that already has its own files. Nothing to detect here.")
        return 0
    # `show` (or no argument): the read-only gather walkthrough. In an already-set-up repo (this one), it
    # short-circuits rather than re-offering setup.
    if is_provisioned():
        print(_ALREADY_SET_UP)
        return 0
    print(present_gather())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
