#!/usr/bin/env python3
"""Unit tests for route_target_existence_check — the route-target guard (ADR 0336).

The `hard_check_bite` meta-check only exercises the ACTIVE-TARGET-RESOLVES leg against one fixture. These tests
cover the legs it doesn't: the PRESENCE leg (a reachable route with no targets), the WELL-FORMED legs (bad
kind/ref/availability/owner), the module-conditional/home-only tolerance (allowed absent), platform-truth
reachability (an OMITTED invocation is model-auto = reachable, so it must still name targets), and the guard's
fail-closed posture on a malformed skill.
"""
from __future__ import annotations
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import route_target_existence_check as rtec  # noqa: E402


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _skill(root: str, slug: str, *, invocation="model-only", targets_yaml: str | None = None) -> None:
    inv = f"invocation: {invocation}\n" if invocation is not None else ""
    ui = "user-invocable: false\n" if invocation == "model-only" else ""
    tgt = f"engine-targets:\n{targets_yaml}" if targets_yaml else ""
    _write(os.path.join(root, ".claude", "skills", slug, "SKILL.md"),
           f"---\nname: {slug}\ndescription: A route.\n{inv}{ui}{tgt}---\n\n## Steps\n\n1. Go.\n")


def _hard(fs) -> list:
    return [f for f in fs if f["severity"] == "hard"]


class RouteTargetExistenceCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rtec-test-")
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        # a real operation file the "active target resolves" cases can point at
        _write(os.path.join(self.root, ".engine", "operations", "real-op.md"), "# real op\n")

    def test_active_target_that_resolves_is_clean(self):
        _skill(self.root, "engine-ok", targets_yaml=
               "  - kind: operation\n    ref: .engine/operations/real-op.md\n    availability: active\n")
        self.assertEqual(rtec.findings("hard", root=self.root), [])

    def test_presence_leg_reachable_route_with_no_targets(self):
        _skill(self.root, "engine-empty")  # model-only, no engine-targets
        fs = _hard(rtec.findings("hard", root=self.root))
        self.assertTrue(any("engine-empty" in f["message"] and "no engine-targets" in f["message"] for f in fs))

    def test_omitted_invocation_route_with_no_targets_fires_presence(self):
        # BLOCKER regression: an invocation-less route is model-auto = reachable, so it must name targets.
        _skill(self.root, "engine-omitted", invocation=None)
        fs = _hard(rtec.findings("hard", root=self.root))
        self.assertTrue(any("engine-omitted" in f["message"] and "no engine-targets" in f["message"] for f in fs))

    def test_active_target_missing_fires(self):
        _skill(self.root, "engine-dangling", targets_yaml=
               "  - kind: operation\n    ref: .engine/operations/nope.md\n    availability: active\n")
        fs = _hard(rtec.findings("hard", root=self.root))
        self.assertTrue(any("does not exist" in f["message"] for f in fs))

    def test_module_conditional_and_home_only_absent_are_tolerated(self):
        _skill(self.root, "engine-cond", targets_yaml=
               "  - kind: operation\n    ref: .engine/operations/absent.md\n    availability: module-conditional\n    owner: some-module\n"
               "  - kind: operation\n    ref: .engine/operations/gone.md\n    availability: home-only\n")
        self.assertEqual(rtec.findings("hard", root=self.root), [],
                         "module-conditional/home-only targets are explicitly allowed to be absent")

    def test_unknown_kind_is_malformed(self):
        _skill(self.root, "engine-badkind", targets_yaml=
               "  - kind: gremlin\n    ref: x\n    availability: active\n")
        fs = _hard(rtec.findings("hard", root=self.root))
        self.assertTrue(any("unknown kind" in f["message"] for f in fs))

    def test_blank_ref_is_malformed(self):
        _skill(self.root, "engine-noref", targets_yaml=
               "  - kind: operation\n    ref: '   '\n    availability: active\n")
        fs = _hard(rtec.findings("hard", root=self.root))
        self.assertTrue(any("no `ref`" in f["message"] for f in fs))

    def test_unrecognized_availability_is_malformed(self):
        _skill(self.root, "engine-badavail", targets_yaml=
               "  - kind: operation\n    ref: .engine/operations/real-op.md\n    availability: someday\n")
        fs = _hard(rtec.findings("hard", root=self.root))
        self.assertTrue(any("unrecognized availability" in f["message"] for f in fs))

    def test_module_conditional_without_owner_is_malformed(self):
        _skill(self.root, "engine-noowner", targets_yaml=
               "  - kind: operation\n    ref: .engine/operations/absent.md\n    availability: module-conditional\n")
        fs = _hard(rtec.findings("hard", root=self.root))
        self.assertTrue(any("no `owner`" in f["message"] for f in fs))

    def test_skill_kind_target_resolves_against_claude_skills(self):
        # a subordinate-skill target resolves to .claude/skills/<ref>/SKILL.md
        _skill(self.root, "engine-target-dst")  # the target skill (its own emptiness doesn't matter here)
        _skill(self.root, "engine-router", targets_yaml=
               "  - kind: skill\n    ref: engine-target-dst\n    availability: active\n")
        # engine-router's active skill target resolves; engine-target-dst itself fires PRESENCE (no targets),
        # so assert specifically that engine-router does NOT get a 'does not exist' finding.
        fs = _hard(rtec.findings("hard", root=self.root))
        self.assertFalse(any("engine-router" in f["message"] and "does not exist" in f["message"] for f in fs))

    def test_operator_typed_skill_without_targets_is_not_required_to_have_them(self):
        _skill(self.root, "engine-op", invocation="operator-typed")  # not reachable → presence not required
        self.assertFalse(any("engine-op" in f["message"] for f in _hard(rtec.findings("hard", root=self.root))))

    def test_malformed_frontmatter_fails_closed(self):
        _write(os.path.join(self.root, ".claude", "skills", "engine-bad", "SKILL.md"),
               "---\ndescription: [unterminated\ninvocation: model-only\n---\n\n## Steps\n\n1. Go.\n")
        with self.assertRaises(Exception):
            rtec.findings("hard", root=self.root)


if __name__ == "__main__":
    unittest.main()
