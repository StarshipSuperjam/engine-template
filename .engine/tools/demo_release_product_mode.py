#!/usr/bin/env python3
"""Behavioral demo — once DEPLOYED, the release machinery cuts the deployed repo's own PRODUCT release (#516).

Construction evidence, retired at first-run (it never travels into a generated repo). It exercises the REAL
release_cut product-mode logic over a synthetic DEPLOYED tree (a temp repo whose recorded home differs from its
origin, carrying a product-version.json), fully offline — the version decision, the raise-only guard, the atomic
write, the malformed-file refuse, and the product-worded pull-request body are the real functions, faked only at
nothing (there is no network on these paths). It SELF-CHECKS and returns non-zero if any invariant breaks, so it
is a falsification that can fail, not a happy-path showcase.

What it shows:
  * mode detection — a deployed repo (or one carrying product-version.json) is in PRODUCT mode; the construction
    repo is in ENGINE mode;
  * a first product cut with no file yet CREATES product-version.json ("no earlier version");
  * a later cut is raise-only — a lowering is refused, a real bump writes the file atomically;
  * a present-but-malformed product-version.json REFUSES loudly (never an engine cut);
  * the pull-request body speaks of the PRODUCT, carrying no engine vocabulary.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate            # noqa: E402
import release_cut as rc   # noqa: E402


def _deployed_tree(product_version=None) -> str:
    """A temp repo that reads as a DEPLOYED deployment: engine.json home != the origin we set below. When
    `product_version` is given, seed product-version.json at the root (a repo that has already cut a release).
    Carries a minimal pull-request template so the release body's consent preamble (#589) can be lifted."""
    root = tempfile.mkdtemp(prefix="product-release-demo-")
    os.makedirs(os.path.join(root, ".engine"))
    os.makedirs(os.path.join(root, ".github"))
    with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
        json.dump({"engine_release": "0.2.0", "packages": {}, "identity": "solo",
                   "home_repository": "StarshipSuperjam/engine-template"}, fh)
    with open(os.path.join(root, ".github", "pull_request_template.md"), "w", encoding="utf-8") as fh:
        fh.write("> *A green mechanical check below shows this change conforms to the engine's rules — not that "
                 "it is correct. **Your merge is the binding gate.***\n>\n> *A check that could not run leaves "
                 "its area unverified.*\n\n## Purpose\n\n**<why>**\n")
    if product_version is not None:
        with open(os.path.join(root, "product-version.json"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"version": product_version}) + "\n")
    return root


def _run(root: str, own_slug: str):
    saved_root, saved_repo = validate.ROOT, os.environ.get("GITHUB_REPOSITORY")
    validate.ROOT = root
    os.environ["GITHUB_REPOSITORY"] = own_slug
    try:
        return _scenarios(root)
    finally:
        validate.ROOT = saved_root
        if saved_repo is None:
            os.environ.pop("GITHUB_REPOSITORY", None)
        else:
            os.environ["GITHUB_REPOSITORY"] = saved_repo


def _scenarios(root: str) -> bool:
    ok = True
    pv_path = os.path.join(root, "product-version.json")

    # 1. mode — a downstream deployment (home != origin), no file yet, is already PRODUCT mode.
    mode, ctx = rc.release_mode()
    print(f"  mode in a deployed repo (no file yet): {mode!r} (current={ctx['current']!r})")
    ok = ok and mode == "product" and ctx["current"] is None

    # 2. first product cut — no file -> creates it, from "no earlier version".
    first = rc.apply_product("0.1.0", dry_run=False)
    on_disk = json.load(open(pv_path))["version"] if os.path.exists(pv_path) else None
    print(f"  first cut 0.1.0: applied={first.get('applied')}, from={first.get('from_engine')!r}, "
          f"file now={on_disk!r}")
    ok = ok and first.get("applied") and first.get("from_engine") == rc.SENTINEL and on_disk == "0.1.0"

    # 3. raise-only — a lowering is refused; a real bump writes atomically.
    lower = rc.apply_product("0.0.9", dry_run=False)
    bump = rc.apply_product("0.1.1", dry_run=False)
    print(f"  lowering 0.0.9: refused={not lower.get('applied')} ({lower.get('reason')}); "
          f"bump 0.1.1: applied={bump.get('applied')} (file={json.load(open(pv_path))['version']!r})")
    ok = ok and (not lower.get("applied")) and lower.get("reason") == "raise-only" and bump.get("applied")

    # 4. malformed product file -> REFUSE (never an engine cut).
    with open(pv_path, "w", encoding="utf-8") as fh:
        fh.write("{ not json")
    bad_mode = rc.release_mode()[0]
    bad_apply = rc.apply_product("0.2.0", dry_run=False)
    print(f"  malformed file: mode={bad_mode!r}, apply refused={not bad_apply.get('applied')} "
          f"({bad_apply.get('reason')})")
    ok = ok and bad_mode == "refuse" and bad_apply.get("reason") == "malformed-product-file"

    # 5. the product pull-request body speaks of the PRODUCT, not the engine.
    proposal = rc._product_proposal(rc.Baseline(None, True, ""), "0.0.0", [])
    applied = {"applied": True, "engine": "0.1.0", "from_engine": rc.SENTINEL, "targets": {}, "product": True}
    body = rc.render_pr_body(proposal, applied)
    title = body.splitlines()[0]
    engine_vocab = ("engine version" in body.lower()) or ("your instances" in body.lower())
    print(f"  product PR title: {title!r}")
    print(f"  contains engine vocabulary: {engine_vocab}")
    ok = ok and title.startswith("# A new release of your product") and not engine_vocab
    return ok


def main() -> int:
    print("PRODUCT-RELEASE DEMO (#516) — once deployed, the release machinery cuts the PRODUCT's release.\n")

    # A construction repo (home == origin, no product file) stays in ENGINE mode — the guard that keeps this
    # from firing in the workshop.
    eng_root = _deployed_tree()
    saved_root, saved_repo = validate.ROOT, os.environ.get("GITHUB_REPOSITORY")
    validate.ROOT = eng_root
    os.environ["GITHUB_REPOSITORY"] = "StarshipSuperjam/engine-template"   # origin == recorded home
    try:
        eng_mode = rc.release_mode()[0]
    finally:
        validate.ROOT = saved_root
        if saved_repo is None:
            os.environ.pop("GITHUB_REPOSITORY", None)
        else:
            os.environ["GITHUB_REPOSITORY"] = saved_repo
    print(f"  mode in the construction repo (engine IS the product): {eng_mode!r}\n")

    deployed_ok = _run(_deployed_tree(), own_slug="acme/my-product")
    ok = deployed_ok and eng_mode == "engine"
    if not ok:
        print("\nDEMO UNEXPECTED: a product-mode invariant did not hold (mode / cut / raise-only / refuse / "
              f"wording). engine-mode={eng_mode!r}, deployed-checks-passed={deployed_ok}.", file=sys.stderr)
        return 1
    print("\nDEMO PASSED: a deployed repo cuts its own product release (raise-only, atomic, product-worded); a "
          "malformed file refuses; the construction repo still cuts the engine version.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
