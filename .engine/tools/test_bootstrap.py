"""Tests for the control-plane bootstrap (core slice 25a).

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

The GitHub network is the ONLY thing faked (an in-memory transport returning (status, json, headers), and a
fake label-ensure boundary); every test exercises the real capability-detection, floor-merge, create/repair,
verify, degrade, and copy-rendering logic. The protection floor the tool writes is checked against the
REAL protection_guard.missing_floor — the same evaluation the committed CI guard uses — so a drift between
what the bootstrap writes and what the guard requires fails here.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootstrap  # noqa: E402
import protection_guard  # noqa: E402

REPO = "you/proj"


class FakeIssues:
    """A stand-in for telemetry.GitHubIssues — records ensure_label, optionally fails."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.ensured = 0

    def ensure_label(self):
        if self.fail:
            raise RuntimeError("GitHub unreachable")
        self.ensured += 1


class FakeGitHub:
    """In-memory GitHub for the bootstrap transport seam. `scopes` is the X-OAuth-Scopes header value
    (or None for a fine-grained token); `floor_met` drives the evaluated per-branch rules; `rulesets`
    is the admin ruleset list. Records every call so writes can be asserted."""

    def __init__(self, scopes="repo", floor_met=False, rulesets=None):
        self.scopes = scopes
        self.floor_met = floor_met
        self.rulesets = [dict(r) for r in (rulesets or [])]
        self.calls = []
        self._next_id = 900

    def _evaluated_rules(self):
        # A floor-meeting branch returns exactly the rules the engine writes; an unprotected one returns [].
        return bootstrap.floor_ruleset()["rules"] if self.floor_met else []

    def transport(self, method, path, body=None):
        self.calls.append((method, path, body))
        headers = {} if self.scopes is None else {"X-OAuth-Scopes": self.scopes}
        if method == "GET" and path == f"/repos/{REPO}":
            return 200, {"full_name": REPO}, headers
        if method == "GET" and path == f"/repos/{REPO}/rules/branches/main":
            return 200, self._evaluated_rules(), headers
        if method == "GET" and path == f"/repos/{REPO}/rulesets":
            return 200, self.rulesets, headers
        if method == "POST" and path == f"/repos/{REPO}/rulesets":
            rid = self._next_id
            self._next_id += 1
            self.rulesets.append({"id": rid, "name": body["name"]})
            self.floor_met = True
            return 201, {"id": rid, "name": body["name"]}, headers
        if method == "PUT" and path.startswith(f"/repos/{REPO}/rulesets/"):
            self.floor_met = True
            return 200, {"id": int(path.rsplit("/", 1)[1])}, headers
        return 404, None, headers

    # convenience assertions
    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "PUT")]

    def names(self):
        return [r["name"] for r in self.rulesets]


def cp(fake, refresh_fn=None, issues=None):
    return bootstrap.ControlPlane(
        REPO, "tok", transport=fake.transport,
        refresh_fn=refresh_fn or (lambda scope: True), issues=issues or FakeIssues())


def quiet(_text):
    """An announce sink so tests don't print operator copy."""


class TestFloorPayload(unittest.TestCase):
    def test_floor_satisfies_the_real_guard(self):
        # The decisive fidelity test: what the bootstrap WRITES must satisfy the SAME evaluation the
        # committed protection_guard / CI guard uses. Any drift (a dropped rule, a wrong param) fails here.
        rules = bootstrap.floor_ruleset()["rules"]
        missing = protection_guard.missing_floor(rules, protection_guard.REQUIRED_CHECKS)
        self.assertEqual(missing, [], f"floor payload does not satisfy the guard: {missing}")

    def test_floor_binds_the_frozen_check_names_from_the_single_home(self):
        rules = bootstrap.floor_ruleset()["rules"]
        rsc = next(r for r in rules if r["type"] == "required_status_checks")
        bound = [c["context"] for c in rsc["parameters"]["required_status_checks"]]
        self.assertEqual(bound, protection_guard.REQUIRED_CHECKS)

    def test_floor_requires_conversation_resolution_and_zero_approvals(self):
        pr = next(r for r in bootstrap.floor_ruleset()["rules"] if r["type"] == "pull_request")
        self.assertTrue(pr["parameters"]["required_review_thread_resolution"])
        self.assertEqual(pr["parameters"]["required_approving_review_count"], 0)


class TestApplyCreatesAndIsIdempotent(unittest.TestCase):
    def test_unprotected_repo_creates_the_engine_ruleset(self):
        fake = FakeGitHub(floor_met=False, rulesets=[])
        issues = FakeIssues()
        result = cp(fake, issues=issues).apply(announce=quiet)
        self.assertEqual(result.status, "applied")
        self.assertTrue(result.is_protected())
        self.assertEqual([c[0] for c in fake.writes()], ["POST"])  # created, not repaired
        self.assertIn(bootstrap.ENGINE_RULESET_NAME, fake.names())
        self.assertEqual(issues.ensured, 1)                        # labels ensured (inherited)

    def test_already_protected_is_a_no_op(self):
        fake = FakeGitHub(floor_met=True, rulesets=[])
        result = cp(fake).apply(announce=quiet)
        self.assertEqual(result.status, "already")
        self.assertEqual(fake.writes(), [])                        # never writes when the floor is met

    def test_existing_engine_ruleset_is_repaired_in_place(self):
        fake = FakeGitHub(floor_met=False,
                          rulesets=[{"id": 42, "name": bootstrap.ENGINE_RULESET_NAME}])
        result = cp(fake).apply(announce=quiet)
        self.assertEqual(result.status, "applied")
        self.assertEqual([c[0] for c in fake.writes()], ["PUT"])   # repaired its own, not a new one
        self.assertEqual(fake.calls[-2][1], f"/repos/{REPO}/rulesets/42")

    def test_verify_after_write_catches_a_silent_no_op(self):
        # A transport whose POST does NOT actually turn protection on -> the verify step degrades.
        fake = FakeGitHub(floor_met=False, rulesets=[])
        orig = fake.transport

        def transport(method, path, body=None):
            status, data, headers = orig(method, path, body)
            if method == "POST":
                fake.floor_met = False  # the write "succeeded" but protection never took
            return status, data, headers
        cpx = bootstrap.ControlPlane(REPO, "tok", transport=transport,
                                     refresh_fn=lambda s: True, issues=FakeIssues())
        result = cpx.apply(announce=quiet)
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.cause, "verify-failed")


class TestNeverWeakensProduct(unittest.TestCase):
    def test_product_ruleset_is_left_untouched(self):
        # A pre-existing product ruleset that doesn't meet the floor -> the engine adds its OWN, never
        # mutating the product's (augment-never-weaken; in-place product augment is a deferred brownfield
        # concern). The product ruleset survives unchanged; no PUT touches it.
        fake = FakeGitHub(floor_met=False, rulesets=[{"id": 7, "name": "team protections"}])
        cp(fake).apply(announce=quiet)
        self.assertIn("team protections", fake.names())            # product still present
        self.assertIn(bootstrap.ENGINE_RULESET_NAME, fake.names())  # engine added its own
        self.assertNotIn("PUT", [c[0] for c in fake.calls])        # never edited a product ruleset


class TestCapabilityAndConsent(unittest.TestCase):
    def test_missing_scope_triggers_consent_then_proceeds(self):
        fake = FakeGitHub(scopes="read:org", floor_met=False, rulesets=[])
        announced = []

        def refresh(scope):
            fake.scopes = "read:org, repo"   # the operator approved -> scope now present
            return True
        result = cp(fake, refresh_fn=refresh).apply(announce=announced.append)
        self.assertEqual(result.status, "applied")
        self.assertTrue(any("authorization screen" in a or "manage my repository" in a
                            for a in announced))  # the pre-bootstrap explanation was shown first

    def test_refresh_that_does_not_persist_degrades_didnt_save(self):
        fake = FakeGitHub(scopes="read:org", floor_met=False, rulesets=[])
        result = cp(fake, refresh_fn=lambda s: False).apply(announce=quiet)  # scope never granted
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.cause, "didnt-save")
        self.assertEqual(fake.writes(), [])                        # never attempted the write

    def test_token_scopes_parses_header_case_insensitively(self):
        fake = FakeGitHub(scopes="repo, workflow")
        self.assertEqual(cp(fake).token_scopes(), {"repo", "workflow"})

    def test_fine_grained_token_no_scope_header_is_none_then_write_probes(self):
        fake = FakeGitHub(scopes=None, floor_met=False, rulesets=[])  # fine-grained: no scopes header
        result = cp(fake).apply(announce=quiet)
        self.assertIsNone(cp(fake).token_scopes())
        self.assertEqual(result.status, "applied")                 # capability proven by the write itself


class TestLabelsAndDisclosure(unittest.TestCase):
    def test_label_failure_is_disclosed_not_crashed(self):
        fake = FakeGitHub(floor_met=False, rulesets=[])
        result = cp(fake, issues=FakeIssues(fail=True)).apply(announce=quiet)
        self.assertEqual(result.status, "applied")                 # protection still applied
        self.assertFalse(result.labels_ok)                         # but the label gap is disclosed
        self.assertIn("issue label", bootstrap.render(result))


class TestCopySurface(unittest.TestCase):
    def test_template_carries_every_copy_section(self):
        # The template SURFACE must hold every heading the tool renders -> no silent drift to fallbacks.
        copy = bootstrap.load_copy(bootstrap.TEMPLATE_PATH)
        for key in bootstrap.COPY_HEADINGS:
            self.assertTrue(copy[key].strip(), f"copy section {key!r} missing from the template")
        # And the template body, not the built-in fallback, is what was read.
        self.assertNotEqual(copy["before-you-approve"], "")
        self.assertIn("repo", copy["before-you-approve"])          # the literal is pre-translated

    def test_missing_template_falls_back_not_crashes(self):
        copy = bootstrap.load_copy("/no/such/template.md")
        self.assertEqual(copy["before-you-approve"], bootstrap.FALLBACK_COPY["before-you-approve"])

    def test_render_picks_the_cause_matched_banner(self):
        r = bootstrap.Result("degraded", "main", ["x"], "not-admin")
        self.assertIn("administer", bootstrap.render(r))
        r2 = bootstrap.Result("degraded", "main", [], "didnt-save")
        self.assertIn("didn't save", bootstrap.render(r2))

    def test_copy_has_no_maintainer_jargon(self):
        # Plain-language law: no engine/maintainer vocabulary reaches the operator copy.
        copy = bootstrap.load_copy(bootstrap.TEMPLATE_PATH)
        blob = " ".join(copy.values()).lower()
        for banned in ("ruleset", "idempotent", "venv", "oauth", "endpoint", "wiring", "coherence"):
            self.assertNotIn(banned, blob, f"maintainer term {banned!r} leaked into operator copy")


if __name__ == "__main__":
    unittest.main()
