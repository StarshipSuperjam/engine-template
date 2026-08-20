#!/usr/bin/env python3
"""Generator for the per-module setup routes (ADR 0336).

Each offerable module gets one automatic `engine-setup-<module-id>` route — a Claude `model-only` skill that
recognizes intent to set the add-on up. The routes are DERIVED-COMMITTED, core-owned provisioning surfaces:
generated from the offerable manifests' `presentation` (the `setup_trigger` becomes the route's description,
and a `setup_operation`, when present, becomes its canonical target), committed and shipped, and regenerated
rather than hand-edited. They are CORE-owned on purpose — declining or removing the module deletes the
module's own files, but never its offer route, so an operator can always ask to add it back.

A generated route never installs, removes, or grants authority because its trigger matched: its body checks
installation state, offers the add-on (with consent) when absent, and enters the module's setup operation
(or reports the active capability) when present. `derive()` is pure over the discovered manifests; `check()`
compares the committed routes to the derived set for the drift gate; `generate()` writes them.
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

SKILLS_DIR = os.path.join(validate.ROOT, ".claude", "skills")
# The offerable statuses — the opt-out-able set that carries a `presentation`. Mirrors module_catalog._OFFERABLE
# (a required or internal module is never offered and carries no presentation).
_OFFERABLE = ("optional", "default-on", "experimental")
_NAME_PREFIX = "engine-setup-"


def _route_name(module_id: str) -> str:
    return _NAME_PREFIX + module_id


def _body(module_id: str, setup_operation: str | None) -> str:
    """The generated thin-recognizer body: check install state, offer on absence (with consent), enter the
    setup operation (or report the capability) on presence."""
    if setup_operation:
        installed_step = (f"If it is installed, enter its setup procedure in `{setup_operation}`.")
    else:
        installed_step = ("If it is installed, report the active capability and route the operator's request "
                          "to the add-on's own workflow.")
    return "\n".join([
        "## Steps",
        "",
        f"1. Check whether the `{module_id}` add-on is installed in this project.",
        "2. If it is not installed, explain in plain language what it does and offer to add it through the "
        "normal setup step — never install it because this route matched; adding it is the operator's decision.",
        f"3. {installed_step}",
        "",
    ])


def _render(module_id: str, presentation: dict) -> str:
    """One setup route's SKILL.md text. The description is the manifest's concise `setup_trigger`. Every setup
    route funnels into the permanent `engine-setup` dispatcher, so it always names that skill as an active
    target; when the module additionally declares a `setup_operation`, that operation is a second target,
    module-conditional on the module itself (absent on a deployment that declined it)."""
    trigger = presentation.get("setup_trigger") or ""
    if not trigger:
        raise ValueError(f"module '{module_id}' presentation has no setup_trigger")
    if len(trigger) > 120:
        raise ValueError(f"module '{module_id}' setup_trigger is {len(trigger)} > 120 chars")
    setup_operation = presentation.get("setup_operation")
    lines = [
        "---",
        f"name: {_route_name(module_id)}",
        f"description: {trigger}",
        "invocation: model-only",
        "user-invocable: false",
        "engine-targets:",
        "  - kind: skill",
        "    ref: engine-setup",
        "    availability: active",
    ]
    if setup_operation:
        lines += [
            "  - kind: operation",
            f"    ref: {setup_operation}",
            "    availability: module-conditional",
            f"    owner: {module_id}",
        ]
    lines += ["---", "", _body(module_id, setup_operation)]
    return "\n".join(lines)


def derive(root: str | None = None) -> dict:
    """{relative SKILL.md path: rendered text} for every offerable module carrying a presentation, keyed by the
    route's committed path. Pure over the discovered manifests — the single source is each manifest's
    `presentation`, never a hand-authored route. `root` overrides which tree the manifests are read from
    (default validate.ROOT): the negative fixture seeds its own module there, so this gate stays witnessable
    in a deployment whose real offerable set is reduced or empty. Every live caller reads the real tree."""
    import module_coherence  # lazy: imports validate; keep out of import time
    out: dict = {}
    for _rel, manifest in module_coherence.discover_manifests(root):
        if not isinstance(manifest, dict):
            continue
        mid = manifest.get("id")
        pres = manifest.get("presentation")
        if not mid or manifest.get("status") not in _OFFERABLE:
            continue
        if not isinstance(pres, dict) or not pres.get("setup_trigger"):
            continue
        rel = os.path.join(".claude", "skills", _route_name(mid), "SKILL.md")
        out[rel] = _render(mid, pres)
    return out


def declined_route_owner(route_name: str, root: str | None = None) -> "str | None":
    """The owning module id when `route_name` (an `engine-setup-<mid>` directory) belongs to a REAL module NOT
    installed in this checkout — so its surviving route is a legitimate decline, not orphan generation — else
    None. Mirrors `module_surfaces.declined_surface_owner`, and reads the same two authorities: the committed
    module-surfaces registry for what modules REALLY exist (it ships complete, while a deployment carries only
    a subset of manifests — StarshipSuperjam/engine-template#646), and `engine.json`'s `packages` for what is
    installed here. Delegates to `module_surfaces.declined_surface_owner` — the engine's ONE authority for
    "this path belongs to a module that is not installed" — so this gate and the link-integrity check can
    never disagree, and the fail-closed rule (an unreadable roster tolerates nothing) has a single home. A
    name the registry does not know — renamed, retired from source, hand-created — is never tolerated.
    Deliberately NOT keyed on the module catalog: `module_catalog.derive` merge-preserves a prior entry for any
    manifest-less module, so a module RETIRED from source keeps its entry forever and is indistinguishable
    there from one declined in a deployment — the discriminator this gate needs."""
    root = root or validate.ROOT
    mid = route_name[len(_NAME_PREFIX):]
    if not mid:
        return None
    import module_surfaces  # lazy: keep the import cost off the generate/derive path
    owner = module_surfaces.declined_surface_owner(os.path.join(root, ".engine", "modules", mid), root)
    return owner if owner == mid else None


def declined_route_names(root: str | None = None) -> set:
    """The `engine-setup-*` route directory names this checkout SHOULD carry for its declined modules. The
    committed module-surfaces registry ships complete (a deployment carries the full registry but a subset of
    manifests), so it names every real module — which is what lets `check` assert a declined module's route is
    still PRESENT rather than only tolerating it when it happens to be. Empty when the registry or the
    installed roster cannot be read: undecidable means assert nothing, never invent a missing-route finding."""
    root = root or validate.ROOT
    import module_surfaces
    registry = module_surfaces.load(root)
    known = {owner for owners in registry.values() for owner in owners}
    return {_route_name(mid) for mid in known if declined_route_owner(_route_name(mid), root)}


def check(tier: str = "hard", root: str | None = None) -> list:
    """Findings when a committed setup route is missing or diverges from its derived text, or a stray
    `engine-setup-*` route exists with no offerable module behind it. The drift gate for the derived-committed
    routes (mirrors codex_gen's render-equality contract). BOTH sides are rooted at `root` (default
    validate.ROOT) — the seam the negative-fixture meta-check uses to point the check at a seeded tree. The
    derivation is rooted too (it once always read the real manifests): a deployment that declined its add-ons
    has NO offerable manifests, so a real-tree derivation there is empty and the fixture's aimed bite could
    never be witnessed — the gate would report itself unproven in exactly the shape it must protect."""
    base = root or validate.ROOT
    findings = []
    expected = derive(base)
    for rel, text in sorted(expected.items()):
        path = os.path.join(base, rel)
        if not os.path.isfile(path):
            findings.append(validate.finding(tier, f"The setup route {rel} is missing; regenerate the setup "
                            f"routes with `setup_route_gen.py generate`."))
            continue
        with open(path, encoding="utf-8") as fh:
            if fh.read() != text:
                findings.append(validate.finding(tier, f"The setup route {rel} is out of date — it no longer "
                                f"matches its module's presentation. Regenerate with "
                                f"`setup_route_gen.py generate`."))
    # A stray engine-setup-<id> route whose module is not an offerable present module is orphan generation —
    # EXCEPT where the module is real but DECLINED here. The routes are core-owned and survive a decline on
    # purpose (see this module's docstring; ADR 0336: the derived-committed surfaces "travel with a deployment,
    # including its declined-module memory"), so in a deployment that declined a module its route is expected,
    # not stale. The tolerance reads `declined_route_owner`, which fails CLOSED: an unreadable installed roster
    # tolerates nothing, and a module the registry does not know (renamed, retired from source, a typo) is never
    # tolerated — that leftover route stays a hard finding, which is this branch's whole purpose at home.
    # The tolerance is DISCLOSED, not silent (the same shape validate.py uses for a declined module's link),
    # and it is two-way: a declined module's route must still be PRESENT. Its absence was invited by this
    # check's own former advice ("Remove it"), and once removed the operator can no longer be offered the
    # add-on back, which is exactly what these core-owned routes exist to keep possible.
    # RESIDUAL, stated plainly: a declined module's route cannot be content-verified — its text is derived
    # from a manifest this checkout no longer has — so this gate proves such a route EXISTS, never that it is
    # unaltered. An installed module's route is still content-verified above.
    expected_names = {os.path.basename(os.path.dirname(rel)) for rel in expected}
    skills_dir = os.path.join(base, ".claude", "skills")
    present = set()
    if os.path.isdir(skills_dir):
        for name in sorted(os.listdir(skills_dir)):
            if not name.startswith(_NAME_PREFIX) or name in expected_names:
                continue
            present.add(name)
            owner = declined_route_owner(name, base)
            if owner:
                findings.append(validate.finding("soft", f"The setup route '{name}' is kept for the declined "
                                f"module '{owner}' (present here because declining an add-on removes the "
                                f"module's own files but never its offer route, so it can be added back) — "
                                f"not a stale route. Its text is not verified here: the manifest it derives "
                                f"from is absent while the module is declined."))
                continue
            findings.append(validate.finding(tier, f"The setup route '{name}' has no offerable module "
                            f"behind it — a stale generated route. Remove it or restore its module."))
    for name in sorted(declined_route_names(base) - present):
        findings.append(validate.finding(tier, f"The setup route '{name}' is missing for the declined module "
                        f"'{name[len(_NAME_PREFIX):]}'. These routes are core-owned and survive a decline so "
                        f"the add-on can be offered back; restore it from the engine (a re-run of "
                        f"`engine-upgrade` overlays it) rather than leaving the offer unreachable."))
    return findings


def generate() -> list:
    """Write every derived setup route; return the relative paths written."""
    written = []
    for rel, text in sorted(derive().items()):
        path = os.path.join(validate.ROOT, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        written.append(rel)
    return written


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        result = generate()
        print(f"wrote {len(result)} setup route(s):")
        for rel in result:
            print(f"  {rel}")
    elif len(sys.argv) > 1 and sys.argv[1] == "check":
        found = check()
        print(f"{len(found)} finding(s)")
        for f in found:
            print("  ", f["message"][:140])
    else:
        print("usage: setup_route_gen.py {generate|check}", file=sys.stderr)
        sys.exit(2)
