"""Tests for the negative-fixture meta-check (#286, D-256…D-260) — hard_check_bite_check.

The meta-check is dormant in this slice (its rule ships suites: []), so these tests ARE its proof for now: they
drive the testable `evaluate(...)` core against controlled rosters, and drive the whole script through the real
`validate.run_unit` subprocess path for self-coverage. Assertions are by set-membership on (severity, message
token), never order/count.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import hard_check_bite_check as hcb  # noqa: E402

ROOT = validate.ROOT
LIVE_FIXTURES = os.path.join(ROOT, ".engine", "_fixtures")
RULE_PATH = os.path.join(ROOT, ".engine", "check", "hard-check-bite.json")
CLOSED_KINDS = sorted(k for k in validate.REGISTRY if k != "custom/script")


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestLiveFixturesBite(unittest.TestCase):
    """The committed fixtures all bite — the real-repo regression. An empty check_dir keeps roster(b) out (the
    live custom/script instances are backfilled in a later slice), so this proves roster(a) + the custom/script
    fail-closed modes against the shipped fixtures."""

    def test_every_kind_and_failclosed_mode_bites(self):
        with tempfile.TemporaryDirectory() as empty:
            found = hcb.evaluate(check_dir=empty, fixture_root=LIVE_FIXTURES)
        self.assertEqual(found, [], f"a committed fixture failed to bite: {found}")

    def test_each_closed_kind_bites_individually(self):
        # Per-kind, so a regression localizes to the kind whose fixture stopped biting.
        for kind in CLOSED_KINDS:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as empty:
                found = hcb.evaluate(check_dir=empty, fixture_root=LIVE_FIXTURES, kinds=[kind])
                self.assertEqual(found, [], f"the '{kind}' fixture did not bite")

    def test_custom_script_failclosed_modes_bite(self):
        with tempfile.TemporaryDirectory() as empty:
            found = hcb.evaluate(check_dir=empty, fixture_root=LIVE_FIXTURES, kinds=["custom/script"])
        self.assertEqual(found, [])


class TestMissingAndNonBiting(unittest.TestCase):
    """§15 — the two failure modes the meta-check must itself catch: a unit with no fixture, and a unit whose
    fixture does not bite."""

    def test_missing_fixture_fails_closed(self):
        with tempfile.TemporaryDirectory() as empty_fix, tempfile.TemporaryDirectory() as empty_chk:
            found = hcb.evaluate(check_dir=empty_chk, fixture_root=empty_fix, kinds=["presence"])
        self.assertTrue(any(f["severity"] == "hard" and "no negative test fixture" in f["message"]
                            for f in found), found)

    def test_non_biting_fixture_fails_closed(self):
        # A presence fixture whose input is COMPLETE (the check will not fire) but whose expect.json demands a
        # bite → the meta-check must report it did not catch.
        with tempfile.TemporaryDirectory() as fix, tempfile.TemporaryDirectory() as chk:
            fdir = os.path.join(fix, "kind-presence")
            _write(os.path.join(fdir, "rule.json"), json.dumps(
                {"id": "f/p", "kind": "presence", "tier": "hard", "target": {},
                 "params": {"sections": ["Purpose"]}, "message": "m"}))
            _write(os.path.join(fdir, "input.md"), "## Purpose\n\nComplete — nothing missing.\n")
            _write(os.path.join(fdir, "expect.json"),
                   json.dumps({"severity": "hard", "message_contains": "Required section"}))
            found = hcb.evaluate(check_dir=chk, fixture_root=fix, kinds=["presence"])
        self.assertTrue(any(f["severity"] == "hard" and "did NOT catch" in f["message"] for f in found), found)


class TestNotApplicableCarveOut(unittest.TestCase):
    """The only admissible carve-out is the exact bounded property; anything else is rejected (no silent
    self-classification)."""

    def _scenario(self, disclosure: dict):
        fix = tempfile.mkdtemp()
        fdir = os.path.join(fix, "kind-presence")
        _write(os.path.join(fdir, "not-applicable.json"), json.dumps(disclosure))
        return fix

    def test_exact_property_is_honored_as_a_soft_note(self):
        fix = self._scenario({"property": hcb._NA_PROPERTY, "reason": "presence has a CI failure path; this is a test."})
        with tempfile.TemporaryDirectory() as chk:
            found = hcb.evaluate(check_dir=chk, fixture_root=fix, kinds=["presence"])
        self.assertTrue(any(f["severity"] == "soft" and "NOT APPLICABLE" in f["message"] for f in found))
        self.assertFalse(any(f["severity"] == "hard" for f in found))

    def test_wrong_property_is_rejected_hard(self):
        fix = self._scenario({"property": "too-hard-to-write-a-fixture", "reason": "lazy"})
        with tempfile.TemporaryDirectory() as chk:
            found = hcb.evaluate(check_dir=chk, fixture_root=fix, kinds=["presence"])
        self.assertTrue(any(f["severity"] == "hard" and "only admissible reason" in f["message"] for f in found))


class TestRosterBInstances(unittest.TestCase):
    """Roster (b): every custom/script INSTANCE in the check directory is proven, by id-stem fixture."""

    def test_instance_with_no_fixture_fails_closed(self):
        with tempfile.TemporaryDirectory() as chk, tempfile.TemporaryDirectory() as fix:
            _write(os.path.join(chk, "foo.json"), json.dumps(
                {"id": "engine/check/foo", "kind": "custom/script", "tier": "hard", "target": {"context": "x"},
                 "params": {"script": ".engine/tools/validate.py"}, "suites": [], "message": "m"}))
            found = hcb.evaluate(check_dir=chk, fixture_root=fix, kinds=[])
        self.assertTrue(any(f["severity"] == "hard" and "engine/check/foo" in f["message"]
                            and "no negative test fixture" in f["message"] for f in found), found)

    def test_instance_bites_its_fixture(self):
        # A controlled instance whose script is a committed fail-closed fixture script (it exits non-zero), with
        # an id-stem fixture asserting that token → the instance bites. Proves roster(b) witnesses a real bite.
        crash = ".engine/_fixtures/kind-custom-script/nonzero/crash.py"
        with tempfile.TemporaryDirectory() as chk, tempfile.TemporaryDirectory() as fix:
            _write(os.path.join(chk, "bar.json"), json.dumps(
                {"id": "engine/check/bar", "kind": "custom/script", "tier": "hard", "target": {"context": "x"},
                 "params": {"script": crash}, "suites": [], "message": "m"}))
            _write(os.path.join(fix, "bar", "expect.json"),
                   json.dumps({"severity": "hard", "message_contains": "exited with an error"}))
            found = hcb.evaluate(check_dir=chk, fixture_root=fix, kinds=[])
        self.assertEqual(found, [], f"the instance fixture did not bite: {found}")


class TestSelfCoverageThroughSubprocess(unittest.TestCase):
    """The meta-check runs as one more roster entry through the REAL run_unit subprocess path, pointed at its
    committed self-fixture mini-scenario — proving §15 self-falsifiability the way it will run live (S5), and
    that the run terminates (the mini-scenario contains no custom/script instance and not the meta-check itself)."""

    def test_meta_check_bites_its_own_self_fixture_via_run_unit(self):
        rule = validate.load_json(RULE_PATH)
        with tempfile.TemporaryDirectory() as empty_roster:
            target = {"env": {
                "ENGINE_ROSTER_KINDS": "presence",
                "ENGINE_FIXTURE_ROOT": ".engine/_fixtures/hard-check-bite/scenario-fixtures",
                "ENGINE_ROSTER_DIR": empty_roster,
            }}
            passed, found = validate.run_unit(rule, target, {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "did NOT catch" in f["message"] for f in found), found)


class TestRuleIsDormantAndWellFormed(unittest.TestCase):
    """The committed rule is the dormant (suites: []) custom/script meta-check, conforming to check.v1.json."""

    def test_rule_conforms_and_is_dormant(self):
        from jsonschema import Draft202012Validator
        rule = validate.load_json(RULE_PATH)
        schema = validate.load_json(os.path.join(ROOT, ".engine", "schemas", "check.v1.json"))
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(rule)), [])
        self.assertEqual(rule["suites"], [])
        self.assertEqual(rule["kind"], "custom/script")
        self.assertEqual(rule["params"]["script"], ".engine/tools/hard_check_bite_check.py")
        self.assertNotIn("pass_token", rule["params"])  # deferred until a token-needing unit (S6)


if __name__ == "__main__":
    unittest.main()
