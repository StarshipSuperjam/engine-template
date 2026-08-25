#!/usr/bin/env python3
"""The demonstration corpus runner — the half of the nightly machinery that SHIPS.

Split from `test_nightly_demos` by provenance, not by topic. That module's subjects — the workflow file
and the first-run retirement machinery — are both removed when a project is set up, so a test importing
them would break the very first automated check in a generated repository. What is here has no such
dependency: the corpus runner is an ordinary operator-runnable tool, and a deployed project's own
demonstrations are worth being able to run.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demonstration_corpus              # noqa: E402


class TheCorpusRunner(unittest.TestCase):

    def test_the_runner_is_not_itself_a_demonstration(self):
        """It globs `demo_*.py`. Named that way it would enumerate and execute itself, and the
        census-completeness guard would count the runner as a demonstration nobody references."""
        self.assertFalse(Path(demonstration_corpus.__file__).name.startswith("demo_"))
        self.assertNotIn(Path(demonstration_corpus.__file__).name,
                         [p.name for p in demonstration_corpus.demos()])

    def test_it_enumerates_from_the_directory_rather_than_a_roster(self):
        with tempfile.TemporaryDirectory() as d:
            tools = Path(d) / ".engine" / "tools"
            tools.mkdir(parents=True)
            (tools / "demo_new.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")
            (tools / "helper.py").write_text("x = 1\n", encoding="utf-8")
            found = [p.name for p in demonstration_corpus.demos(Path(d))]
        self.assertEqual(found, ["demo_new.py"],
                         "a demonstration added without touching the runner must still be in the corpus")

    def test_a_demonstration_that_cannot_run_is_a_failure_not_a_skip(self):
        with tempfile.TemporaryDirectory() as d:
            broken = Path(d) / "demo_broken.py"
            broken.write_text("raise SystemExit(3)\n", encoding="utf-8")
            failure = demonstration_corpus.run_one(broken)
        self.assertEqual(failure["demo"], "demo_broken.py")
        self.assertEqual(failure["exit_code"], 3)

    def test_the_result_records_how_long_the_corpus_took(self):
        """The measurement the workflow exists to make once: without it, 'the nightly run' is a cost
        nobody can weigh against making it more frequent."""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".engine" / "tools").mkdir(parents=True)
            result = demonstration_corpus.run(Path(d))
        self.assertIn("duration_seconds", result)
        self.assertIsInstance(result["duration_seconds"], float)



if __name__ == "__main__":
    unittest.main()
