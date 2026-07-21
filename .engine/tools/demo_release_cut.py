#!/usr/bin/env python3
"""Behavioural demonstration of the release-cut classifier + writer (release_cut.py).

Run it and watch the REAL version-decision and manifest-writing logic act on a throwaway engine
tree — nothing here reimplements the tool, and every step asserts an outcome that can FAIL:

  1. FIRST CUT — no prior release => no derived floor; the initial version is chosen, not derived.
  2. DIFF — a module added, a module removed, a new migration => the mechanical floor
     (removal => a major bump), with a plain-language change inventory.
  3. RAISE-ONLY — a version not strictly higher than the current one is REFUSED, never lowered.
  4. ATOMIC WRITE — a real cut records the versions and leaves the home_repository line
     byte-identical (so a version-only cut never trips the guard on the update home).
  5. ROLLBACK — a write whose validation fails leaves every file untouched (no split-brain).
  6. RELEASE-PR BODY — the propose + apply results render the maintainer's evidence bundle: the version
     move, a legible readiness line (sub-bar, since no benchmark measures a release), the confirm/raise/reject
     guidance, and three readiness states that read distinct (the legibility invariant).

  uv run --directory .engine -- python tools/demo_release_cut.py
"""
import json
import os
import shutil
import tempfile

import validate
import module_coherence
import release_cut as rc


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")


def _module(mid, ver="0.0.0-dev", migrations=None):
    m = {"id": mid, "version": ver, "status": "required", "provides": {}}
    if migrations:
        m["migrations"] = migrations
    return m


def _tree(modules, home="acme/engine-home", engine_release="0.0.0-dev"):
    root = tempfile.mkdtemp()
    _write(os.path.join(root, ".engine", "engine.json"),
           {"engine_release": engine_release,
            "packages": {mid: m["version"] for mid, m in modules.items()},
            "identity": "solo", "home_repository": home})
    for mid, m in modules.items():
        _write(os.path.join(root, ".engine", "modules", mid, "manifest.json"), m)
    return root


def _baseline(modules):
    root = tempfile.mkdtemp()
    for mid, m in modules.items():
        _write(os.path.join(root, ".engine", "modules", mid, "manifest.json"), m)
    return root


def main() -> int:
    saved = (validate.ROOT, validate.ENGINE_DIR)
    ok = True
    trees = []
    try:
        # 1. FIRST CUT ----------------------------------------------------------------------
        root = _tree({"core": _module("core"), "qa-review": _module("qa-review")})
        trees.append(root)
        validate.ROOT, validate.ENGINE_DIR = root, os.path.join(root, ".engine")
        p = rc.classify(rc.Baseline(None, True, "no prior release"), None)
        print("1. FIRST CUT")
        print(f"   mode={p['mode']}  floor={p['engine_floor_level']}")
        print(f"   inventory: {p['change_inventory'][0]}")
        ok &= (p["mode"] == "first-cut" and p["engine_floor_level"] == "none")

        # 2. DIFF: add + remove + new migration --------------------------------------------
        live = {"core": _module("core", migrations={"0.2.0": {"description": "d", "run": "r", "kind": "config"}}),
                "product-design": _module("product-design")}
        root2 = _tree(live)
        trees.append(root2)
        validate.ROOT, validate.ENGINE_DIR = root2, os.path.join(root2, ".engine")
        base = _baseline({"core": _module("core"), "legacy": _module("legacy")})
        trees.append(base)
        p2 = rc.classify(rc.Baseline("v0.0.9", False, "diff"), base)
        print("\n2. DIFF (product-design added, legacy removed, core gained a migration)")
        for c in p2["change_inventory"]:
            print(f"   - {c}")
        print(f"   engine floor = {p2['engine_floor_level']}  (a removal forces major)")
        ok &= (p2["engine_floor_level"] == "major" and "core" in p2["package_floor"])

        # 3. RAISE-ONLY --------------------------------------------------------------------
        r = rc.apply("0.0.0-dev", "0.0.0-dev", {}, None, dry_run=True)
        print("\n3. RAISE-ONLY: applying the current version to itself")
        print(f"   applied={r['applied']}  reason={r.get('reason')}  ({len(r.get('violations', []))} refused)")
        ok &= (not r["applied"] and r["reason"] == "raise-only")

        # 4. ATOMIC WRITE + home_repository preserved --------------------------------------
        eng_path = os.path.join(root2, ".engine", "engine.json")
        home_before = [ln for ln in open(eng_path, encoding="utf-8").read().splitlines() if "home_repository" in ln][0]
        r2 = rc.apply("0.1.0", "0.1.0", {}, None, dry_run=False)
        eng_after = json.load(open(eng_path, encoding="utf-8"))
        home_after = [ln for ln in open(eng_path, encoding="utf-8").read().splitlines() if "home_repository" in ln][0]
        print("\n4. ATOMIC WRITE to 0.1.0")
        print(f"   applied={r2['applied']}  engine {r2.get('from_engine')} -> {eng_after['engine_release']}")
        print(f"   core manifest version = {module_coherence.discover_manifests()[0][1].get('version')}")
        print(f"   home_repository line unchanged = {home_before == home_after}")
        ok &= (r2["applied"] and eng_after["engine_release"] == "0.1.0" and home_before == home_after)

        # 5. ROLLBACK on validation failure -------------------------------------------------
        root3 = _tree({"core": _module("core")})
        trees.append(root3)
        validate.ROOT, validate.ENGINE_DIR = root3, os.path.join(root3, ".engine")
        orig = rc._schema_ok
        rc._schema_ok = lambda inst, path: ["forced validation error"]
        try:
            r3 = rc.apply("0.1.0", "0.1.0", {}, None, dry_run=False)
        finally:
            rc._schema_ok = orig
        still = json.load(open(os.path.join(root3, ".engine", "engine.json"), encoding="utf-8"))
        print("\n5. ROLLBACK: a write whose validation fails")
        print(f"   applied={r3['applied']}  reason={r3.get('reason')}  engine still {still['engine_release']}")
        ok &= (not r3["applied"] and still["engine_release"] == "0.0.0-dev")

        # 6. RELEASE-PR BODY: the maintainer's evidence bundle ------------------------------
        root4 = _tree({"core": _module("core"), "qa-review": _module("qa-review")})
        trees.append(root4)
        validate.ROOT, validate.ENGINE_DIR = root4, os.path.join(root4, ".engine")
        p6 = rc.classify(rc.Baseline(None, True, "no prior release"), None)
        a6 = rc.apply("0.1.0", "0.1.0", {}, None, dry_run=False)
        body = rc.render_pr_body(p6, a6)
        states = {rc._gate_path_line(s) for s in ("passed", "sub-bar", "errored")}
        print("\n6. RELEASE-PR BODY (the maintainer's evidence bundle)")
        guidance = "## Review" in body and "Go ahead" in body and "higher version" in body
        print(f"   version move shown, sentinel hidden           = {'no earlier version → 0.1.0' in body and '0.0.0-dev' not in body}")
        print(f"   readiness stated (no automated check ran)     = {'no automated check' in body.lower()}")
        print(f"   confirm/raise/reject guidance present         = {guidance}")
        print(f"   the three readiness states read distinct      = {len(states) == 3}")
        ok &= ("no earlier version → 0.1.0" in body and "0.0.0-dev" not in body
               and "no automated check" in body.lower()
               and guidance and len(states) == 3)

        # 7. ENGINE-ONLY CUT: the engine version moves, no capability changed — the reported failure, fixed.
        # Unchanged capabilities keep their version (not refused as "not strictly greater"); only engine.json
        # is written. (Before the fix this refused with reason=raise-only across every capability.)
        root5 = _tree({"core": _module("core", ver="0.1.0"), "qa-review": _module("qa-review", ver="0.1.0")},
                      engine_release="0.1.0")
        trees.append(root5)
        validate.ROOT, validate.ENGINE_DIR = root5, os.path.join(root5, ".engine")
        r7 = rc.apply("0.2.0", None, {}, {"engine_floor_version": "0.2.0", "package_floor": {}}, dry_run=False)
        eng7 = json.load(open(os.path.join(root5, ".engine", "engine.json"), encoding="utf-8"))
        core_ver = json.load(open(os.path.join(root5, ".engine", "modules", "core", "manifest.json"),
                                  encoding="utf-8"))["version"]
        print("\n7. ENGINE-ONLY CUT (engine moves; unchanged capabilities hold — the reported failure, fixed)")
        print(f"   applied={r7['applied']}  engine 0.1.0 -> {eng7['engine_release']}  capabilities written={list(r7['targets'])}")
        print(f"   core held at its version = {core_ver == '0.1.0'}")
        ok &= (r7["applied"] and eng7["engine_release"] == "0.2.0" and r7["targets"] == {} and core_ver == "0.1.0")

        # 8. CHANGE SUMMARY collates a contract-only change (empty structural inventory + one impact), so the
        # release notes' "what changed" is not blank when a changed contract is what forced the bump.
        summ = rc.change_summary({"change_inventory": [],
                                  "impacts": [{"what": "the contract surface 'eADR-0014-one-history.md' changed",
                                               "why": "read it against consumers"}]})
        print("\n8. CHANGE SUMMARY (contract-only release still lists what changed)")
        for s in summ:
            print(f"   - {s}")
        ok &= (len(summ) == 1 and "eADR-0014-one-history.md" in summ[0])

        # 9. PUBLISHED RELEASE NOTES: a human-readable body with a breaking callout, the pull requests merged
        # since the last release (the actual work) GROUPED under their change-kind, and interface changes WITH
        # descriptions — the same signals as the PR body, formatted for the release. A title's leading `Kind:`
        # prefix sorts it under that kind; the prefix is stripped from the line (the heading carries it) and an
        # unprefixed title (the last one) falls to "Other changes", rendered last. (The PR list is best-effort;
        # here a fixed list stands in.)
        rich = {"engine_floor_level": "major",
                "change_inventory": ["Added the 'routine-mode' capability.", "Removed the 'legacy' capability."],
                "merged_prs": ["Feature: add the routine-mode capability (#41)",
                               "Removal: remove the legacy sync path (#42)",
                               "Maintenance: bump setup-uv from 8.3.0 to 8.3.2 (#43)",
                               "Fix: stop the self-review digest double-posting (#44)",
                               "Reword the onboarding copy (#45)"]}     # no prefix -> "Other changes"
        rich["impacts"] = [{"what": "the contract surface 'eADR-0021-control-plane.md' changed",
                            "why": "a changed contract can be additive or breaking — read it against consumers."}]
        notes = rc.render_release_notes("v1.0.0", rich)
        print("\n9. PUBLISHED RELEASE NOTES (merged PRs grouped by change-kind; unprefixed => 'Other changes')")
        print("\n".join("   " + ln for ln in notes.splitlines()))
        ok &= ("breaking change" in notes.lower()
               and "## What changed since the last release (5 pull requests)" in notes
               and "### Feature" in notes and "- add the routine-mode capability (#41)" in notes  # prefix stripped
               and all(f"### {k}" in notes for k in ("Feature", "Removal", "Fix", "Maintenance"))
               and "### Other changes" in notes and "- Reword the onboarding copy (#45)" in notes
               and notes.index("### Feature") < notes.index("### Maintenance") < notes.index("### Other changes")
               and "## Interface changes to read" in notes and "read it against consumers" in notes)

        # 10. LEGACY / UNPREFIXED TITLES — the state EVERY generated repo starts in. Nothing carries a kind,
        # so there is nothing to contrast: the lone "Other changes" heading is OMITTED and the notes degrade to
        # EXACTLY the old flat list, never worse, never a crash. (A heading reading "other" over a reader's
        # whole release would label all their work as leftovers.) Real v0.2.0 titles, which predate the
        # convention — which is also why re-rendering a historical release proves nothing about the win; the
        # win shows on convention-following titles, in step 9.
        legacy = {"engine_floor_level": "minor", "change_inventory": [], "impacts": [],
                  "merged_prs": ["Fix the release workflow's derive-version path (#487)",  # 'Fix the' — no colon
                                 "Bump astral-sh/setup-uv from 8.3.0 to 8.3.2 (#489)",
                                 "Wire the findings-inbox drain into production (#479)"]}
        lnotes = rc.render_release_notes("v0.2.1", legacy)
        print("\n10. LEGACY UNPREFIXED TITLES (no lone 'other' heading — exactly the old flat list)")
        print("\n".join("   " + ln for ln in lnotes.splitlines()))
        ok &= ("###" not in lnotes and "Other changes" not in lnotes
               and "- Fix the release workflow's derive-version path (#487)" in lnotes)

        # 11. A SECURITY FIX IS NEVER FILED AS UPKEEP. A dependency bot appends "[Security] " AFTER any
        # configured prefix, so in a repo that prefixes its bumps a CVE fix arrives titled
        # "Maintenance: [Security] bump ..." — the marker WINS, or the notes would call a security fix
        # "upkeep that doesn't change what you can do" on the one class a reader most needs to see.
        sec = {"engine_floor_level": "minor", "change_inventory": [], "impacts": [],
               "merged_prs": ["Maintenance: [Security] bump cryptography from 41.0.0 to 41.0.6 (#102)",
                              "Maintenance: bump astral-sh/setup-uv from 8.3.0 to 8.3.2 (#101)"]}
        snotes = rc.render_release_notes("v0.2.2", sec)
        print("\n11. A BOT'S SECURITY FIX OUTRANKS ITS OWN 'Maintenance' PREFIX")
        print("\n".join("   " + ln for ln in snotes.splitlines()))
        ok &= (snotes.index("### Security") < snotes.index("### Maintenance")
               and "- bump cryptography from 41.0.0 to 41.0.6 (#102)" in snotes
               and snotes.split("### Maintenance")[1].count("cryptography") == 0)  # NOT filed as upkeep
    finally:
        validate.ROOT, validate.ENGINE_DIR = saved
        for t in trees:
            shutil.rmtree(t, ignore_errors=True)

    print("\n" + ("DEMO PASSED: the classifier derived the right floors, refused a non-raise, wrote "
                  "atomically preserving the update home, rolled back a failed write, rendered a legible "
                  "release-PR body with three distinct readiness states, applied an engine-only cut while "
                  "holding unchanged capabilities, and collated a contract-only change into the summary."
                  if ok else "DEMO DID NOT BEHAVE AS EXPECTED — see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
