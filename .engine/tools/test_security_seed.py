"""Slice 2 of the security floor (issue #124): the `SECURITY.md` seed-then-own. These tests attest the
load-bearing facts: the seed is copy-IF-ABSENT (never overwrites a project's own disclosure file, in any
GitHub-recognized location), it lands on a RESUME after a tool-runtime halt, the file is operator-owned
product territory (NOT in FOUNDATION_INFRA; the seed SOURCE carved out in OPERATOR_CONFIG), and the first-run
disclosure renders from the reviewed template (not the silent fallback). The eyes-on behavior is covered by
demo_security_seed against the real `_seed_security`."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate            # noqa: E402
import instantiator as inst  # noqa: E402
import module_coherence    # noqa: E402
import demo_security_seed  # noqa: E402
import quiet_call          # noqa: E402  (capture the demo walkthrough so it can't bury the summary)
import test_instantiator as ti  # noqa: E402  (reuse the apply fixture harness)

_SEED_REL = os.path.join(".engine", "provisioning", "security-seed.md")


def _plant_seed(root: str, body: str = "# Security Policy\n\nReport privately via the Security tab.\n"):
    os.makedirs(os.path.join(root, ".engine", "provisioning"), exist_ok=True)
    with open(os.path.join(root, _SEED_REL), "w", encoding="utf-8") as fh:
        fh.write(body)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestSeedSecurityFunction(unittest.TestCase):
    def test_seeds_root_from_the_template_seed_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            _plant_seed(d, "# Security Policy\n\nRECOGNIZABLE SEED BODY\n")
            with inst._redirect_root(d):
                outcome = inst._seed_security(lambda _t: None, None)
                target = os.path.join(d, "SECURITY.md")
                self.assertEqual(outcome, "seeded")
                self.assertTrue(os.path.isfile(target))
                self.assertIn("RECOGNIZABLE SEED BODY", _read(target))

    def test_falls_back_to_a_minimal_default_when_seed_absent(self):
        with tempfile.TemporaryDirectory() as d:                 # no seed source planted
            with inst._redirect_root(d):
                outcome = inst._seed_security(lambda _t: None, None)
                body = _read(os.path.join(d, "SECURITY.md"))
            self.assertEqual(outcome, "seeded")
            self.assertEqual(body, inst._DEFAULT_SECURITY_MD, "an absent seed yields the minimal default")

    def test_never_overwrites_an_existing_root_security_md(self):
        self._assert_skip_preserves("SECURITY.md")

    def test_never_overwrites_a_github_security_md_and_makes_no_root_file(self):
        self._assert_skip_preserves(os.path.join(".github", "SECURITY.md"))

    def test_never_overwrites_a_docs_security_md_and_makes_no_root_file(self):
        self._assert_skip_preserves(os.path.join("docs", "SECURITY.md"))

    def _assert_skip_preserves(self, location_rel):
        sentinel = "MY OWN SECURITY FILE -- DO NOT TOUCH"
        with tempfile.TemporaryDirectory() as d:
            _plant_seed(d)                                       # a seed exists, but must NOT be used
            existing = os.path.join(d, location_rel)
            os.makedirs(os.path.dirname(existing), exist_ok=True)
            with open(existing, "w", encoding="utf-8") as fh:
                fh.write(sentinel + "\n")
            with inst._redirect_root(d):
                # a skip discloses NOTHING — pass a real copy + a `say` that fails the test if called,
                # so a wrongful seed (which would disclose) trips self.fail
                outcome = inst._seed_security(self.fail, inst.load_copy())
            self.assertEqual(outcome, "present")
            self.assertEqual(_read(existing).strip(), sentinel,
                             "the project's own disclosure file is left exactly as it was")
            if location_rel != "SECURITY.md":
                self.assertFalse(os.path.isfile(os.path.join(d, "SECURITY.md")),
                                 ".github/ and docs/ take precedence — no stray root file is created")

    def test_resume_is_idempotent_a_second_seed_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            _plant_seed(d)
            with inst._redirect_root(d):
                first = inst._seed_security(lambda _t: None, None)
                body1 = _read(os.path.join(d, "SECURITY.md"))
                second = inst._seed_security(lambda _t: None, None)
                body2 = _read(os.path.join(d, "SECURITY.md"))
            self.assertEqual((first, second), ("seeded", "present"))
            self.assertEqual(body1, body2, "a resumed seed never rewrites the file")


class TestSecuritySeedInApply(unittest.TestCase):
    def test_substrates_step_seeds_and_discloses(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            said = []
            with inst._redirect_root(d):
                ti._confirmed_fixture(d)
                res = ti._fake_apply(d, announce=said.append)
            substrates = next(s for s in res["steps"] if s["step"] == "substrates")
            self.assertEqual(substrates["security"], "seeded")
            self.assertTrue(os.path.isfile(os.path.join(d, "SECURITY.md")))
            heading_body = inst.load_copy()["security-seeded"]
            self.assertTrue(any(heading_body[:30] in line for line in said),
                            "the seed is disclosed in plain language during apply")

    def test_seed_lands_on_resume_after_a_tool_runtime_halt(self):
        with tempfile.TemporaryDirectory() as d:
            inst._build_fixture(d)
            with inst._redirect_root(d):
                ti._confirmed_fixture(d)
                halted = ti._fake_apply(d, uv_present=lambda: None, uv_installer=lambda: None)
                absent = os.path.isfile(os.path.join(d, "SECURITY.md"))
                resumed = ti._fake_apply(d)               # runtime now materializes; step 5 runs
                present = os.path.isfile(os.path.join(d, "SECURITY.md"))
            self.assertTrue(halted["halted"])
            self.assertFalse(absent, "no SECURITY.md while the runtime halt blocks step 5")
            self.assertFalse(resumed["halted"])
            resumed_substrates = next(s for s in resumed["steps"] if s["step"] == "substrates")
            self.assertEqual(resumed_substrates["security"], "seeded", "the seed lands on the resume")
            self.assertTrue(present)


class TestSecuritySeedOwnershipAndDisclosure(unittest.TestCase):
    def test_seeded_root_security_md_is_not_engine_owned(self):
        # product territory, in no `provides`, NO carve-out: it must NOT be a FOUNDATION_INFRA member
        # (that set is overlay-REPLACED on upgrade, which would clobber the operator's edited disclosure).
        self.assertNotIn("SECURITY.md", module_coherence.FOUNDATION_INFRA)

    def test_seed_source_is_carved_out_in_operator_config(self):
        self.assertIn(".engine/provisioning/security-seed.md", module_coherence.OPERATOR_CONFIG)

    def test_disclosure_copy_renders_from_the_template_not_the_fallback(self):
        # the 3rd place of the 3-place copy edit: the `## heading` must exist in first-run.md, or load_copy
        # silently falls back and the operator gets unreviewed wording.
        with open(inst.TEMPLATE_PATH, encoding="utf-8") as fh:
            sections = inst.bootstrap._parse_sections(fh.read())
        heading = inst.COPY_HEADINGS["security-seeded"]
        self.assertIn(heading, sections, "the disclosure heading must be present in first-run.md")
        self.assertTrue(sections[heading].strip(), "the template section must be non-empty")
        self.assertEqual(inst.load_copy()["security-seeded"], sections[heading].strip(),
                         "load_copy must render the template body, not the built-in fallback")


class TestSecuritySeedDemo(unittest.TestCase):
    def test_demo_passes(self):
        self.assertEqual(quiet_call.run(demo_security_seed.main), 0)


if __name__ == "__main__":
    unittest.main()
