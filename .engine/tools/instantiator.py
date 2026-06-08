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
            engine_release: str | None = None) -> dict:
    """CONFIRM — write the engine manifest, the resumability checkpoint (D-024). Records the engine release,
    the identity tier, and the kept package set: the always-present required spine plus the optional features
    the operator kept (an unkept optional is simply left out of the manifest — its files are removed later, in
    the apply phase, not here). This is the single committing step; before it, nothing is written. Returns the
    written path and the manifest. `root`/`engine_release` are injectable for tests and the demo."""
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
    path = _engine_manifest_path(root)
    _write_json(path, written)
    return {"path": path, "manifest": written}


def _existing_release(root: str | None = None) -> str | None:
    """The engine release recorded in an existing manifest, if any — so a re-run keeps the same release
    rather than resetting it. None when there is no readable manifest."""
    try:
        with open(_engine_manifest_path(root), encoding="utf-8") as fh:
            return json.load(fh).get("engine_release")
    except Exception:
        return None


# ---- demo (mutation-free, real logic, fixture boundary) ---------------------------------------

@contextlib.contextmanager
def _redirect_root(root: str):
    """Point every ROOT-derived path at a throwaway fixture tree, restore on exit (the demo/test idiom)."""
    saved = (validate.ROOT, validate.ENGINE_DIR)
    validate.ROOT = root
    validate.ENGINE_DIR = os.path.join(root, ".engine")
    try:
        yield
    finally:
        (validate.ROOT, validate.ENGINE_DIR) = saved


def _build_fixture(root: str) -> None:
    """A throwaway 'freshly generated' repo: the required spine, one planted optional add-on with a catalog
    entry, and NO engine manifest yet (so it reads as not-set-up, the state a real first run starts from)."""
    eng = os.path.join(root, ".engine")
    os.makedirs(os.path.join(eng, "modules", "core"))
    os.makedirs(os.path.join(eng, "modules", "extras-demo"))
    os.makedirs(os.path.join(eng, "provisioning"))
    _write_json(os.path.join(eng, "modules", "core", "manifest.json"),
                {"id": "core", "version": "1.0.0", "status": "required", "provides": {}, "depends": {}})
    _write_json(os.path.join(eng, "modules", "extras-demo", "manifest.json"),
                {"id": "extras-demo", "version": "1.0.0", "status": "optional", "provides": {}, "depends": {}})
    _write_json(os.path.join(eng, "provisioning", "module-catalog.json"),
                [{"id": "extras-demo", "verb": "engine-extras", "category": "Verification & Validation",
                  "status": "optional",
                  "description": "A practice add-on for this demonstration — checks and tests your work."}])


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


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    # `show` (or no argument): the read-only gather walkthrough. In an already-set-up repo (this one), it
    # short-circuits rather than re-offering setup.
    if is_provisioned():
        print(_ALREADY_SET_UP)
        return 0
    print(present_gather())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
