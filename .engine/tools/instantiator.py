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
    "security-tier": "About automatic secret scanning",
    "codeowners-degraded": "If I couldn't set up file ownership for reviews",
    "control-plane-unavailable": "If I couldn't reach your project on GitHub",
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
    "security-tier": (
        "A quick note on automatic secret scanning — the check that warns you if a password or key is "
        "committed by accident. If your project's hosting doesn't include it, I can't switch it on for you, so "
        "the project is on the basic level for now. To get automatic scanning you can make the project public, "
        "or add GitHub's Advanced Security if your plan offers it. I'm telling you so you can decide — I won't "
        "switch anything on or off without you."
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


# ---- the seven apply steps (each returns one ledger entry; each idempotent) -------------------

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


def _apply_substrates(say) -> dict:
    """STEP 5 — initialize the kept set's committed substrates (runs AFTER the runtime materializes). Today:
    re-derive the knowledge graph (idempotent) and confirm the state seed is present. The graph path is
    bound at import (knowledge_gen), so the demo redirects it AND we pass it explicitly — a redirected run
    never rewrites the real graph. Memory-backup setup is owed to the memory module (not yet built)."""
    result = {"step": "substrates", "status": "done"}
    try:
        knowledge_gen.generate(path=knowledge_gen.GRAPH_PATH)
        result["knowledge"] = "derived"
    except Exception as exc:  # noqa: BLE001 — degrade-and-disclose, never crash the phase
        result["knowledge"] = f"skipped ({type(exc).__name__})"
        result["status"] = "degraded"
    result["state_present"] = os.path.isfile(os.path.join(validate.ROOT, ".engine", "state", "state.json"))
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


def apply(*, root=None, announce=None, home_reader=None, settings_path=None, uv_present=None,
          uv_installer=None, uv_runner=None, consent=None, control_transport=None, gh_refresh=None,
          control_issues=None, control_repo=None, control_token=None, handle=None) -> dict:
    """The apply phase: run the seven ordered steps against the confirmed manifest. Refuses (no change) when
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
    steps.append(_apply_substrates(say))
    steps.append(_apply_wires(say))
    steps.append(_apply_control_plane(control_transport, gh_refresh, control_issues, say, copy,
                                      repo=control_repo, token=control_token))
    return {"refused": False, "halted": False, "steps": steps}


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
                              "knowledge": [".engine/knowledge/*.json"]},
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
                         ".gitignore", ".github/CODEOWNERS")


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
        if path.startswith("/repos/"):
            return 200, {"full_name": "you/your-project"}, headers
        return 404, None, headers
    return t


def _already_transport():
    """An in-memory GitHub where the branch is ALREADY protected (models a resumed run after the first
    pass turned the gate on) → a clean 'already', no write."""
    def t(method, path, body=None):
        headers = {"X-OAuth-Scopes": "repo"}
        if method == "GET" and path.endswith("/rules/branches/main"):
            return 200, bootstrap.floor_ruleset()["rules"], headers
        if path.startswith("/repos/"):
            return 200, {"full_name": "you/your-project"}, headers
        return 404, None, headers
    return t


def _defer_transport():
    """An in-memory GitHub that denies the protection write (the operator can't administer the repo) → a
    cause-matched degraded banner; the engine never pretends the gate is on."""
    def t(method, path, body=None):
        headers = {"X-OAuth-Scopes": "repo"}
        if method == "GET" and path.endswith("/rules/branches/main"):
            return 200, [], headers
        if method == "GET" and path.endswith("/rulesets"):
            return 200, [], headers
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
}
_GOOD_STATUSES = {"done", "written", "adopted", "already", "materialized", "applied",
                  "kept-operator-default"}


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
        gate_on = bool(res["steps"][-1].get("protected"))
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
        cp = res["steps"][-1]
        ended = (not res["halted"]) and cp["step"] == "control-plane" and len(res["steps"]) == 7
        print(f"    → the review-gate step: {cp['status']} (the engine never pretends it's on: "
              f"protected={cp.get('protected')}).")
        print(f"    → setup still completed every other step and ended cleanly ({ended}).")
        ok &= (ended and cp["status"] == "degraded" and not cp.get("protected"))

    # The isolation guarantee, shown.
    print("\n— ISOLATION CHECK: did any of that touch THIS real project's files?")
    unchanged = _assert_real_files_unchanged(real_before)
    print(f"    → this project's own setup files are byte-for-byte unchanged: {unchanged}.")
    ok &= unchanged

    print("\n" + ("All apply steps behaved." if ok else "AN APPLY STEP DID NOT BEHAVE — see above."))
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
    # `show` (or no argument): the read-only gather walkthrough. In an already-set-up repo (this one), it
    # short-circuits rather than re-offering setup.
    if is_provisioned():
        print(_ALREADY_SET_UP)
        return 0
    print(present_gather())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
