#!/usr/bin/env python3
"""Self-tests for slice 8 — the self-map (surface-level + wiring-graph) + its drift gate.

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

These lock the slice-8 defenses: the map is DERIVED (sorted, deterministic, no volatile content);
it is human-readable Markdown with NO `](` byte-sequence (so link-integrity passes); the fingerprint
gate is regenerate-and-compare (a hand-edit or a stale map is a HARD finding, in sync is a note, an
absent map is HARD); generate/check round-trip and are idempotent; the custom/script entry emits []
in sync and a hard finding on drift; and the META-TEST proves the committed `.engine/self-map.md`
equals what the real sources regenerate — so a forgotten regenerate fails the unit suite, not only CI.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import self_map          # noqa: E402
import self_map_check    # noqa: E402

# The closed seam vocabulary, read live from the schema so the wires render cannot silently diverge.
MODULE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "module.v1.json"))
WIRES_ENUM = set(MODULE_SCHEMA["properties"]["wires"]["items"]["properties"]["type"]["enum"])

# ---- fixtures (the render layer is pure, so these drive it without any IO) --------------------

def _surface(**over):
    base = {"class": "structured", "location": ".engine/x/", "purpose": "a demo surface",
            "authority": "mechanics-and-guidance", "lifecycle": "artifact",
            "governing_schema": None, "template": None}
    base.update(over)  # `class` is a reserved word, so callers set it via over, e.g. _surface(**{"class": "code"})
    return base


CATALOG = {"surfaces": {
    "tool": _surface(location=".engine/tools/", purpose="machinery", **{"class": "code"}),
    "check": _surface(location=".engine/check/", purpose="rules"),
}}

CORE = {"id": "core", "version": "0.0.0-dev", "status": "required",
        "provides": {"tool": [".engine/tools/*.py"], "check": [".engine/check/*.json"]},
        "depends": {}}
WIDGET = {"id": "widget", "version": "1.2.0", "status": "optional",
          "provides": {"skill": [".claude/skills/widget/SKILL.md"]},
          "depends": {"core": ">=0.0.0"},
          "wires": [{"type": "gitignore", "key": "widget", "lines": ["x/"]},
                    {"type": "hook", "event": "PreToolUse", "hook": {"type": "command",
                     "command": ".engine/tools/h.py"}}]}
ENGINE = {"engine_release": "0.0.0-dev", "packages": {"core": "0.0.0-dev"}, "identity": "solo"}


class TestRenderSurfaces(unittest.TestCase):
    def test_every_surface_and_field_rendered_sorted(self):
        out = "\n".join(self_map.render_surfaces(CATALOG["surfaces"]))
        self.assertIn("(2 surfaces)", out)
        for name in ("tool", "check"):
            self.assertIn(f"`{name}`", out)
        # fields present
        self.assertIn("machinery", out)
        self.assertIn("`.engine/tools/`", out)
        self.assertIn("code", out)
        self.assertIn("mechanics-and-guidance", out)
        # sorted: check's row precedes tool's row
        self.assertLess(out.index("`check`"), out.index("`tool`"))

    def test_pipe_in_a_value_is_escaped_not_a_new_column(self):
        cat = {"weird": _surface(purpose="a | b")}
        out = "\n".join(self_map.render_surfaces(cat))
        self.assertIn("a \\| b", out)


class TestRenderModule(unittest.TestCase):
    def test_core_block(self):
        out = "\n".join(self_map.render_module(CORE))
        self.assertIn("### `core` — version `0.0.0-dev` (required)", out)
        self.assertIn("- depends on: nothing", out)
        self.assertIn("- wires: none (this module adds no shared-state edits)", out)
        # provides groups sorted (check before tool)
        self.assertLess(out.index("check:"), out.index("tool:"))

    def test_wired_block_renders_types_only_sorted(self):
        out = "\n".join(self_map.render_module(WIDGET))
        self.assertIn("### `widget` — version `1.2.0` (optional)", out)
        self.assertIn("- depends on: `core` >=0.0.0", out)
        # wire TYPES, sorted, from the closed vocabulary — and NO per-type body
        self.assertIn("- wires: gitignore, hook", out)
        self.assertNotIn("PreToolUse", out)
        self.assertNotIn("lines", out)
        for t in ("gitignore", "hook"):
            self.assertIn(t, WIRES_ENUM)

    def test_version_reads_from_manifest_not_engine_packages(self):
        # render_module takes only the manifest; changing its version changes the render,
        # proving the per-module version source is the manifest's own `version` field.
        m = dict(WIDGET, version="9.9.9")
        self.assertIn("version `9.9.9`", "\n".join(self_map.render_module(m)))


class TestRenderMapDeterminism(unittest.TestCase):
    def test_deterministic_and_order_independent(self):
        a = self_map.render_map(CATALOG, [CORE, WIDGET], ENGINE)
        b = self_map.render_map(CATALOG, [WIDGET, CORE], ENGINE)  # permuted module order
        self.assertEqual(a, b)
        # permuted catalog insertion order
        cat2 = {"surfaces": dict(reversed(list(CATALOG["surfaces"].items())))}
        self.assertEqual(a, self_map.render_map(cat2, [CORE, WIDGET], ENGINE))

    def test_clean_text_shape(self):
        out = self_map.render_map(CATALOG, [CORE, WIDGET], ENGINE)
        self.assertTrue(out.endswith("\n"))
        self.assertFalse(out.endswith("\n\n"))
        self.assertNotIn("\r", out)
        for ln in out.split("\n"):
            self.assertEqual(ln, ln.rstrip(), f"trailing whitespace on: {ln!r}")

    def test_header_carries_release_and_identity(self):
        out = self_map.render_map(CATALOG, [CORE], ENGINE)
        self.assertIn("`0.0.0-dev`", out)
        self.assertIn("`solo`", out)


class TestNoLinkSequence(unittest.TestCase):
    def test_no_bracket_paren_sequence(self):
        out = self_map.render_map(CATALOG, [CORE, WIDGET], ENGINE)
        self.assertNotIn("](", out)  # the exact pattern link-integrity's LINK_RE matches


class TestDriftLogic(unittest.TestCase):
    def test_in_sync_is_a_note(self):
        f = self_map.drift_finding("X", "X", "/r/.engine/self-map.md")
        self.assertEqual(f["severity"], "note")

    def test_drift_is_hard_with_fix(self):
        # real calls pass an absolute path (SELF_MAP_PATH); loc() makes it repo-relative
        f = self_map.drift_finding("X", "X tampered", self_map.SELF_MAP_PATH)
        self.assertEqual(f["severity"], "hard")
        self.assertIn("out of date", f["message"])
        self.assertIn("self_map.py generate", f["message"])
        self.assertEqual(f["location"]["file"], ".engine/self-map.md")

    def test_absent_is_hard(self):
        f = self_map.drift_finding("X", None, ".engine/self-map.md")
        self.assertEqual(f["severity"], "hard")
        self.assertIn("not been generated", f["message"])

    def test_finding_is_finding_v1_shaped(self):
        f = self_map.drift_finding("X", "Y", ".engine/self-map.md")
        self.assertEqual(set(f), {"severity", "message", "location"})


class TestGenerateCheckIO(unittest.TestCase):
    """Real sources (catalog/manifests/engine on disk) + a redirected committed path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "self-map.md")

    def tearDown(self):
        self._tmp.cleanup()

    def test_generate_then_check_is_in_sync(self):
        g = self_map.generate(self.path)
        self.assertEqual(g["severity"], "note")
        self.assertIn("Wrote", g["message"])
        self.assertEqual(self_map.check(self.path)["severity"], "note")

    def test_generate_is_idempotent(self):
        self_map.generate(self.path)
        first = self_map.read_committed(self.path)
        second_finding = self_map.generate(self.path)
        self.assertIn("already up to date", second_finding["message"])
        self.assertEqual(first, self_map.read_committed(self.path))

    def test_hand_edit_is_caught(self):
        self_map.generate(self.path)
        with open(self.path, "a", encoding="utf-8", newline="") as fh:
            fh.write("junk a human typed\n")
        self.assertEqual(self_map.check(self.path)["severity"], "hard")

    def test_absent_committed_file_is_hard_no_traceback(self):
        self.assertFalse(os.path.exists(self.path))
        f = self_map.check(self.path)
        self.assertEqual(f["severity"], "hard")
        self.assertIn("not been generated", f["message"])


class TestMetaCommittedInSync(unittest.TestCase):
    """The committed .engine/self-map.md must equal what the REAL sources regenerate."""

    def test_committed_map_matches_real_sources(self):
        committed = self_map.read_committed(self_map.SELF_MAP_PATH)
        self.assertIsNotNone(committed, "the self-map is not committed")
        self.assertEqual(committed, self_map.canonical_map(),
                         "committed self-map is out of date — run self_map.py generate")

    def test_committed_map_has_no_link_sequence_and_clean_shape(self):
        committed = self_map.read_committed(self_map.SELF_MAP_PATH)
        self.assertNotIn("](", committed)
        self.assertTrue(committed.endswith("\n"))
        self.assertFalse(committed.endswith("\n\n"))
        self.assertNotIn("\r", committed)


class TestCheckEntry(unittest.TestCase):
    """self_map_check.py — the thin custom/script entry."""

    def test_emits_empty_array_when_in_sync(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self_map_check.main()
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), [])

    def test_emits_hard_finding_on_drift(self):
        saved = self_map.SELF_MAP_PATH
        tmp = tempfile.TemporaryDirectory()
        try:
            drifted = os.path.join(tmp.name, "self-map.md")
            with open(drifted, "w", encoding="utf-8", newline="") as fh:
                fh.write("not the canonical map\n")
            self_map.SELF_MAP_PATH = drifted  # check() resolves the path at call time
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = self_map_check.main()
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["severity"], "hard")
        finally:
            self_map.SELF_MAP_PATH = saved
            tmp.cleanup()


class TestCLI(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self_map.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_show_prints_the_readout(self):
        rc, out, _ = self._run(["show"])
        self.assertEqual(rc, 0)
        self.assertIn("What this engine is made of", out)

    def test_default_command_is_show(self):
        rc, out, _ = self._run([])
        self.assertEqual(rc, 0)
        self.assertIn("What this engine is made of", out)

    def test_check_in_sync_returns_zero(self):
        rc, out, _ = self._run(["check"])
        self.assertEqual(rc, 0)
        self.assertIn("in sync", out)

    def test_check_absent_path_returns_one(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out, _ = self._run(["check", os.path.join(d, "nope.md")])
        self.assertEqual(rc, 1)

    def test_generate_to_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.md")
            rc, out, _ = self._run(["generate", p])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(p))

    def test_demo_runs_clean_and_never_touches_the_real_file(self):
        before = self_map.read_committed(self_map.SELF_MAP_PATH)
        rc, out, _ = self._run(["demo"])
        after = self_map.read_committed(self_map.SELF_MAP_PATH)
        self.assertEqual(rc, 0)
        self.assertEqual(before, after, "demo must not touch the committed self-map")
        self.assertIn("in sync", out)
        self.assertIn("out of date", out)
        self.assertIn("never touched", out)

    def test_unknown_command_is_config_error(self):
        rc, _out, err = self._run(["bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown command", err)


if __name__ == "__main__":
    unittest.main()
