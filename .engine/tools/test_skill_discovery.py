"""Tests for the shared skill-discovery helper.

Verifies the discovery contract every caller now depends on: the directory-slug identity rule (a `SKILL.md`
is named for its directory; a legacy command for its filename stem); engine-prefix scoping (un-prefixed
product skills ignored); both provider trees (.claude/skills and .agents/skills); the RAW-parse contract
(records expose the unmodified frontmatter — no invocation defaulting, no filtering, so each caller keeps its
own posture); the strict toggle (non-strict drops a malformed skill, strict lets the parse raise — the guard
posture); the legacy-command inclusion (Claude only); and the fixture-dir seam.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skill_discovery as sd  # noqa: E402


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


_SKILL = "---\nname: a-display-label\ndescription: Do a thing.\ninvocation: operator-typed\n---\n\nbody\n"
_NO_INVOCATION = "---\ndescription: An auto one.\n---\n\nbody\n"
_MALFORMED = "---\ndescription: [unclosed\n---\n\nbody\n"


class TestSlugIdentity(unittest.TestCase):
    def test_skill_slug_is_the_directory(self):
        self.assertEqual(sd.slug("/x/.claude/skills/engine-start/SKILL.md"), "engine-start")

    def test_legacy_command_slug_is_the_filename_stem(self):
        self.assertEqual(sd.slug("/x/.claude/commands/engine-legacy.md"), "engine-legacy")


class TestDiscovery(unittest.TestCase):
    def test_skill_files_scoped_to_engine_prefix_sorted(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/skills/engine-zed/SKILL.md"), _SKILL)
            _write(os.path.join(d, ".claude/skills/engine-abc/SKILL.md"), _SKILL)
            _write(os.path.join(d, ".claude/skills/my-product/SKILL.md"), _SKILL)   # un-prefixed → ignored
            slugs = [sd.slug(p) for p in sd.skill_files("claude", d)]
            self.assertEqual(slugs, ["engine-abc", "engine-zed"], "engine-prefixed only, sorted")

    def test_skill_dirs_are_the_parents(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/skills/engine-one/SKILL.md"), _SKILL)
            dirs = sd.skill_dirs("claude", d)
            self.assertEqual([os.path.basename(x) for x in dirs], ["engine-one"])

    def test_codex_provider_reads_agents_tree(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".agents/skills/engine-twin/SKILL.md"), _SKILL)
            self.assertEqual([sd.slug(p) for p in sd.skill_files("codex", d)], ["engine-twin"])


class TestRecords(unittest.TestCase):
    def test_records_expose_raw_frontmatter(self):
        # No semantic normalization: an omitted invocation stays ABSENT (the caller applies its own default).
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/skills/engine-auto/SKILL.md"), _NO_INVOCATION)
            rec = sd.records("claude", root=d)[0]
            self.assertEqual(rec["slug"], "engine-auto")
            self.assertEqual(rec["provider"], "claude")
            self.assertNotIn("invocation", rec["frontmatter"], "the raw parse is not defaulted here")

    def test_non_strict_drops_a_malformed_skill(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/skills/engine-ok/SKILL.md"), _SKILL)
            _write(os.path.join(d, ".claude/skills/engine-bad/SKILL.md"), _MALFORMED)
            slugs = [r["slug"] for r in sd.records("claude", root=d)]   # must NOT raise
            self.assertEqual(slugs, ["engine-ok"], "the malformed skill is skipped, not crashing discovery")

    def test_strict_lets_the_parse_raise(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/skills/engine-bad/SKILL.md"), _MALFORMED)
            with self.assertRaises(Exception):
                sd.records("claude", root=d, strict=True)

    def test_include_commands_adds_legacy_claude_only(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/skills/engine-skill/SKILL.md"), _SKILL)
            _write(os.path.join(d, ".claude/commands/engine-legacy.md"), _SKILL)
            with_cmds = sorted(r["slug"] for r in sd.records("claude", root=d, include_commands=True))
            self.assertEqual(with_cmds, ["engine-legacy", "engine-skill"])
            without = [r["slug"] for r in sd.records("claude", root=d, include_commands=False)]
            self.assertEqual(without, ["engine-skill"], "commands excluded when not requested")

    def test_skills_dir_fixture_seam(self):
        # The negative-fixture seam globs engine-*/SKILL.md directly under a literal dir (not a .claude tree),
        # so a seeded fixture is never loaded as a real skill by the platform.
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "fixtures/engine-fixture/SKILL.md"), _SKILL)
            slugs = [r["slug"] for r in sd.records("claude", skills_dir=os.path.join(d, "fixtures"))]
            self.assertEqual(slugs, ["engine-fixture"])


if __name__ == "__main__":
    unittest.main()
