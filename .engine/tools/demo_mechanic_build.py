#!/usr/bin/env python3
"""Operator-runnable demo: the engine-mechanic build preflight VERIFIES a genuine, matching product checkout,
and REFUSES — fail-closed, in plain language — a mismatched one and a look-alike-host one.

Run: uv run --directory .engine -- python tools/demo_mechanic_build.py

This exercises the REAL gate (`mechanic_build.resolve_build_target` and its host-anchored belt), not a
re-implementation: it builds throwaway git checkouts with real `origin` remotes and runs the actual preflight
against them. Read the three blocks by eye:
  [1] a checkout whose origin genuinely IS the committed product, clean to build in, is VERIFIED — the preflight
      emits the checkout path and the product slug the mechanic would build against;
  [2] a checkout whose origin is a DIFFERENT github repo is REFUSED (`origin-mismatch`) — the mechanic will not
      write into a checkout that is not the product it is configured to build;
  [3] a checkout whose origin is a LOOK-ALIKE host (`notgithub.com/…`, which merely CONTAINS "github.com") is
      REFUSED (`origin-untrusted-host`) — this is the safety that matters most, because the mechanic runs the
      matched checkout's OWN tools in place, so a look-alike pass would be local code execution.

Each refusal prints the exact plain-language reason + remedy the operator would see. The demo self-checks and
exits non-zero if the real gate does not behave as narrated (e.g. if the host anchor were removed and block [3]
were verified instead of refused) — it is a falsification that can fail, not a showcase.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mechanic_build  # noqa: E402

_TARGET = "acme/product"   # the committed product_build_target — NOT this repo's own slug


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _repo(tmp: str, name: str) -> str:
    root = os.path.join(tmp, name)
    os.makedirs(os.path.join(root, ".claude"))
    os.makedirs(os.path.join(root, ".engine"))
    with open(os.path.join(root, ".claude", "settings.json"), "w") as fh:
        fh.write("{}")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", "seed", "--allow-empty")
    return root


def _mechanic(tmp: str) -> str:
    root = _repo(tmp, "mechanic")
    with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
        json.dump({"product_build_target": _TARGET}, fh)
    return root


def _product(tmp: str, name: str, origin: str) -> str:
    root = _repo(tmp, name)
    _git(root, "remote", "add", "origin", origin)
    return root


def _resolve(mechanic: str, product: str) -> tuple:
    saved = os.environ.get("ENGINE_PRODUCT_CHECKOUT")
    os.environ["ENGINE_PRODUCT_CHECKOUT"] = product
    try:
        return mechanic_build.resolve_build_target(cwd=mechanic)
    finally:
        if saved is None:
            os.environ.pop("ENGINE_PRODUCT_CHECKOUT", None)
        else:
            os.environ["ENGINE_PRODUCT_CHECKOUT"] = saved


def _verified_checkout_passes(tmp: str, m: str) -> bool:
    p = _product(tmp, "genuine", "git@github.com:acme/product.git")
    path, slug, refusal = _resolve(m, p)
    ok = refusal is None and path == p and slug == _TARGET
    if ok:
        print(f"    VERIFIED. The mechanic would build in: {path}")
        print(f"    and open its pull request against: {slug}")
    else:
        print(f"    UNEXPECTED refusal: {refusal!r} (a genuine matching checkout should verify)")
    return ok


def _mismatched_checkout_is_refused(tmp: str, m: str) -> bool:
    p = _product(tmp, "other", "git@github.com:acme/other.git")
    path, _slug, refusal = _resolve(m, p)
    ok = path is None and refusal == "origin-mismatch"
    print(f"    REFUSED ({refusal}). {mechanic_build._REFUSALS.get(refusal, '')}" if ok
          else f"    UNEXPECTED: path={path!r} refusal={refusal!r} (a mismatched origin should be refused)")
    return ok


def _look_alike_host_is_refused(tmp: str, m: str) -> bool:
    p = _product(tmp, "lookalike", "https://notgithub.com/acme/product.git")
    path, _slug, refusal = _resolve(m, p)
    ok = path is None and refusal == "origin-untrusted-host"
    print(f"    REFUSED ({refusal}). {mechanic_build._REFUSALS.get(refusal, '')}" if ok
          else f"    UNEXPECTED: path={path!r} refusal={refusal!r} (a look-alike host must be refused)")
    return ok


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        print("=" * 78)
        print("The engine-mechanic build preflight: verify a genuine product checkout, refuse the rest.")
        print("The committed product build target for this demo is:", _TARGET)
        print("=" * 78)
        m = _mechanic(tmp)   # one mechanic; each block points it at a different product checkout

        print("\n[1] A checkout whose origin genuinely IS the product, clean to build in. Expect: VERIFIED.")
        print("-" * 78)
        one = _verified_checkout_passes(tmp, m)

        print("\n[2] A checkout whose origin is a DIFFERENT github repo. Expect: REFUSED (origin-mismatch).")
        print("-" * 78)
        two = _mismatched_checkout_is_refused(tmp, m)

        print("\n[3] A checkout whose origin is a LOOK-ALIKE host (notgithub.com). Expect: REFUSED.")
        print("-" * 78)
        three = _look_alike_host_is_refused(tmp, m)

    ok = one and two and three
    print("\n" + "=" * 78)
    print("In plain words: the mechanic writes into your separate product checkout ONLY when that checkout is")
    print("genuinely the product it was told to build, on a real github.com origin, and clean — and it says why,")
    print("in plain language, whenever it refuses. The look-alike-host refusal is the load-bearing one: because")
    print("the mechanic runs the matched checkout's OWN tools, letting a look-alike host pass would be running")
    print("someone else's code on your machine.")
    print("DEMO OK" if ok else "DEMO FAILED -- the preflight did not behave as narrated (a gate may be weakened)")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
