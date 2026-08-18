"""Protection-detection guard: the local (no-token) note is a WITNESS-DEFERRED no-op.

The guard runs as a `custom/script` check. With no token it fails open with a soft "not checked in this run
— the real check runs in CI" note. That note is witness-deferred (StarshipSuperjam/engine-template#761): it enforces in
CI but had no repository-token witness here, so the validator LIFTS it onto its elevated "not verified in
this run — enforces in CI" line rather than collapsing it into "nothing to do" — never left to masquerade
as the one note needing action, and never falsely read as checked-and-passed. Run in a subprocess with a
scrubbed env so the no-token branch is deterministic and never touches the network."""
import json
import os
import subprocess
import sys
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import protection_guard  # noqa: E402
import repo_identity     # noqa: E402


class TestLocalNoteIsWitnessDeferred(unittest.TestCase):
    """The local no-token branch emits a WITNESS-DEFERRED no-op (StarshipSuperjam/engine-template#761) — a hard check
    that enforces in CI but had no repository token here. Since protection_guard.py inlines the marker
    shape (it does not import validate), this pins the FULL inline key-set so a later drift from
    validate.witness_deferred()'s shape is caught, not silently under-carried across the ingestion boundary."""

    def _run_without_token(self) -> list:
        env = {k: v for k, v in os.environ.items()
               if k not in ("GITHUB_TOKEN", "GITHUB_REPOSITORY")}
        proc = subprocess.run([sys.executable, os.path.join(HERE, "protection_guard.py")],
                              cwd=HERE, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_local_no_token_note_is_witness_deferred(self):
        findings = self._run_without_token()
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["severity"], "soft")
        self.assertIn("Branch protection was not checked in this run", f["message"])
        # not_applicable stays set (every pre-#761 fail-safe path still holds) AND the distinct
        # witness_deferred marker + its named missing witnesses are present, so report() lifts it
        # onto the elevated "enforces in CI, not verified here" line rather than the collapse line.
        self.assertIs(f.get("not_applicable"), True)
        self.assertIs(f.get("witness_deferred"), True)
        self.assertIsInstance(f.get("missing_witness"), list)
        self.assertTrue(all(isinstance(x, str) for x in f["missing_witness"]))


class TestMainProbesTheResolvedBranch(unittest.TestCase):
    """The CI merge-gate script (`main()`, the literal check engine-ci runs with a token) probes the RESOLVED
    default branch — never a hard-coded 'main' — and URL-quotes it before the token-bearing rules API path."""

    def _probe(self, branch: str):
        seen = {}
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"}, clear=False), \
             mock.patch.object(repo_identity, "resolve_default_branch", return_value=branch), \
             mock.patch.object(protection_guard, "resolve_tier", return_value="solo"), \
             mock.patch.object(protection_guard, "missing_floor", return_value=[]), \
             mock.patch.object(protection_guard, "get_json",
                               side_effect=lambda path, token, **kw: (seen.__setitem__("path", path) or [])):
            rc = protection_guard.main()
        return rc, seen.get("path")

    def test_probes_the_resolved_default_branch(self):
        rc, path = self._probe("master")
        self.assertEqual(rc, 0)
        self.assertEqual(path, "/repos/o/r/rules/branches/master")

    def test_url_quotes_a_slash_containing_branch(self):
        _, path = self._probe("release/1.0")
        self.assertEqual(path, "/repos/o/r/rules/branches/release%2F1.0")


class TestPlatformForbidsRulesets(unittest.TestCase):
    """The single load-bearing predicate: only GitHub's genuine plan-limitation 403 counts — every transient
    or permission 403 stays a hard failure, so a stale/forged posture cannot ride a rate-limit blip to a soft."""

    def test_plan_limitation_message_matches(self):
        for msg in ("Upgrade to GitHub Team to enable this feature.",
                    "Upgrade to GitHub Enterprise to enable this feature.",
                    "Your rulesets won't be enforced on this private repository until you upgrade this "
                    "organization account to GitHub Team."):
            self.assertTrue(protection_guard.platform_forbids_rulesets(403, {"message": msg}), msg)

    def test_rate_limit_403_is_excluded_by_message(self):
        self.assertFalse(protection_guard.platform_forbids_rulesets(
            403, {"message": "You have exceeded a secondary rate limit. Please wait a few minutes."}))
        self.assertFalse(protection_guard.platform_forbids_rulesets(
            403, {"message": "API rate limit exceeded for user."}))

    def test_rate_limit_403_is_excluded_by_header(self):
        # A throttle whose body somehow said 'upgrade' is still excluded by its rate-limit headers.
        self.assertFalse(protection_guard.platform_forbids_rulesets(
            403, {"message": "Upgrade to GitHub Team feature."}, {"Retry-After": "60"}))
        self.assertFalse(protection_guard.platform_forbids_rulesets(
            403, {"message": "Upgrade to GitHub Team feature."}, {"X-RateLimit-Remaining": "0"}))

    def test_ordinary_not_admin_403_does_not_match(self):
        self.assertFalse(protection_guard.platform_forbids_rulesets(
            403, {"message": "Resource not accessible by personal access token"}))

    def test_non_403_never_matches(self):
        self.assertFalse(protection_guard.platform_forbids_rulesets(404, {"message": "Upgrade to GitHub Team"}))
        self.assertFalse(protection_guard.platform_forbids_rulesets(500, {"message": "Upgrade to GitHub Team"}))

    def test_unreadable_body_does_not_match(self):
        # An unrecognizable/empty body fails toward HARD (the safe direction), never a false soften.
        self.assertFalse(protection_guard.platform_forbids_rulesets(403, {}))
        self.assertFalse(protection_guard.platform_forbids_rulesets(403, None))


class TestRecordedPosture(unittest.TestCase):
    """The posture reader honors only a well-formed unsupported-platform record; anything else reads as None
    (fail toward the hard check)."""

    def _write(self, tmp, manifest):
        with open(os.path.join(tmp, "engine.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)

    def test_well_formed_posture_is_returned(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, {"protection_posture": {"status": "unsupported-platform",
                                                     "reason": "x", "operator_login": "me",
                                                     "recorded_on": "2026-08-08"}})
            posture = protection_guard.recorded_posture(engine_dir=tmp)
            self.assertIsNotNone(posture)
            self.assertEqual(posture["operator_login"], "me")

    def test_absent_or_wrong_status_reads_as_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, {"identity": "solo"})
            self.assertIsNone(protection_guard.recorded_posture(engine_dir=tmp))
            self._write(tmp, {"protection_posture": {"status": "something-else"}})
            self.assertIsNone(protection_guard.recorded_posture(engine_dir=tmp))

    def test_missing_manifest_reads_as_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(protection_guard.recorded_posture(engine_dir=tmp))


class TestMainPostureSoftening(unittest.TestCase):
    """main()'s soft/hard decision: soften ONLY on (recorded posture AND a live plan-limitation 403); every
    other outcome stays hard, and a read-success never softens regardless of a recorded posture."""

    def _http_error(self, code, message="", headers=None):
        import email.message
        import io
        hdrs = email.message.Message()
        for k, v in (headers or {}).items():
            hdrs[k] = v
        return urllib.error.HTTPError("https://api.github.com/x", code, message, hdrs,
                                      io.BytesIO(json.dumps({"message": message}).encode()))

    def _run(self, *, posture, get_json_side_effect, missing=None):
        captured = []
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"}, clear=False), \
             mock.patch.object(repo_identity, "resolve_default_branch", return_value="main"), \
             mock.patch.object(protection_guard, "resolve_tier", return_value="solo"), \
             mock.patch.object(protection_guard, "recorded_posture", return_value=posture), \
             mock.patch.object(protection_guard, "missing_floor", return_value=(missing or [])), \
             mock.patch.object(protection_guard, "get_json", side_effect=get_json_side_effect), \
             mock.patch.object(protection_guard, "emit", side_effect=lambda f: captured.append(f) or 0):
            protection_guard.main()
        return captured[0]

    _POSTURE = {"status": "unsupported-platform", "reason": "plan can't host rulesets",
                "operator_login": "owner", "recorded_on": "2026-08-08"}

    def _raise(self, err):
        def _side(path, token, **kw):
            raise err
        return _side

    def test_posture_plus_plan_limitation_403_softens(self):
        findings = self._run(
            posture=self._POSTURE,
            get_json_side_effect=self._raise(self._http_error(403, "Upgrade to GitHub Team to enable this feature.")))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "soft")
        self.assertIn("isn't available on this repository's GitHub plan", findings[0]["message"])
        self.assertIn("2026-08-08", findings[0]["message"])

    def test_missing_floor_message_states_the_mechanical_loss_not_unreviewed(self):
        # #712: the floor-off finding must name the concrete loss (no required checks / no pull
        # request), never "unreviewed" — which imports a code-review framing the gate does not
        # provide (in solo the gate is consent, not a review). A successful read plus a non-empty
        # missing set drives the "not fully in force" message.
        findings = self._run(posture=None, get_json_side_effect=lambda *a, **k: [],
                             missing=["required status checks"])
        self.assertEqual(len(findings), 1)
        self.assertIn("without the required checks or a pull request", findings[0]["message"])
        self.assertNotIn("unreviewed", findings[0]["message"])

    def test_plan_limitation_403_without_posture_stays_hard(self):
        findings = self._run(
            posture=None,
            get_json_side_effect=self._raise(self._http_error(403, "Upgrade to GitHub Team to enable this feature.")))
        self.assertEqual(findings[0]["severity"], "hard")
        self.assertIn("could not be verified", findings[0]["message"])

    def test_transient_rate_limit_403_with_posture_stays_hard(self):
        findings = self._run(
            posture=self._POSTURE,
            get_json_side_effect=self._raise(self._http_error(403, "You have exceeded a secondary rate limit.")))
        self.assertEqual(findings[0]["severity"], "hard")

    def test_read_success_floor_missing_with_posture_stays_hard_and_nudges(self):
        findings = self._run(
            posture=self._POSTURE,
            get_json_side_effect=lambda path, token, **kw: [],
            missing=["a pull request is not required before merging"])
        self.assertEqual(findings[0]["severity"], "hard")
        self.assertIn("stale", findings[0]["message"])

    def test_non_list_200_fails_closed_hard(self):
        findings = self._run(
            posture=self._POSTURE,
            get_json_side_effect=lambda path, token, **kw: {"unexpected": "object"})
        self.assertEqual(findings[0]["severity"], "hard")
        self.assertIn("not in the expected form", findings[0]["message"])

    def test_read_success_floor_present_passes_clean(self):
        findings = self._run(
            posture=self._POSTURE,
            get_json_side_effect=lambda path, token, **kw: [{"type": "pull_request"}],
            missing=[])
        self.assertEqual(findings, [])

    def test_non_dict_list_200_fails_closed_without_crashing(self):
        # A 200 whose body is a list of NON-dict elements must NOT crash missing_floor's r.get("type") into an
        # uncaught exception (missing_floor runs outside the read's try) — the element guard fails it closed to
        # a hard finding. missing_floor is deliberately NOT mocked here, so a regression would raise, not pass.
        captured = []
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"}, clear=False), \
             mock.patch.object(repo_identity, "resolve_default_branch", return_value="main"), \
             mock.patch.object(protection_guard, "resolve_tier", return_value="solo"), \
             mock.patch.object(protection_guard, "recorded_posture", return_value=None), \
             mock.patch.object(protection_guard, "get_json", return_value=[1, 2, "x"]), \
             mock.patch.object(protection_guard, "emit", side_effect=lambda f: captured.append(f) or 0):
            protection_guard.main()   # must not raise
        self.assertEqual(captured[0][0]["severity"], "hard")
        self.assertIn("not in the expected form", captured[0][0]["message"])


class TestResolveLabelerAuthority(unittest.TestCase):
    """resolve_labeler_authority (#958): the SINGLE-read decision behind the guardrail-ack writer — accept a
    distinct operator in team, accept-and-disclose in solo, and REFUSE every case that cannot prove authority
    (unreadable manifest, team-without-identity, non-user / engine-identity / missing sender in team)."""

    def _dir(self, manifest):
        import json
        import tempfile
        d = tempfile.mkdtemp(prefix="pg-auth-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        if manifest is not None:
            with open(os.path.join(d, "engine.json"), "w", encoding="utf-8") as fh:
                json.dump(manifest, fh)
        return d

    _TEAM = {"identity": "team", "engine_identity": {"login": "engine-bot"}, "home_repository": "o/r"}
    _SOLO = {"identity": "solo", "home_repository": "o/r"}

    def test_solo_accepts_and_annotates_shared_credential(self):
        decision, detail = protection_guard.resolve_labeler_authority("alice", "User", self._dir(self._SOLO))
        self.assertEqual(decision, protection_guard.AUTH_SOLO)
        self.assertIn("[shared credential]", detail)
        self.assertIn("@alice", detail)

    def test_solo_missing_sender_still_accepts(self):
        decision, detail = protection_guard.resolve_labeler_authority(None, None, self._dir(self._SOLO))
        self.assertEqual(decision, protection_guard.AUTH_SOLO)

    def test_team_distinct_operator_accepts_annotated_operator(self):
        decision, detail = protection_guard.resolve_labeler_authority("alice", "User", self._dir(self._TEAM))
        self.assertEqual(decision, protection_guard.AUTH_TEAM)
        self.assertIn("[operator]", detail)

    def test_team_engine_identity_refused_case_insensitive(self):
        for login in ("engine-bot", "Engine-Bot", "ENGINE-BOT"):
            decision, detail = protection_guard.resolve_labeler_authority(login, "User", self._dir(self._TEAM))
            self.assertEqual(decision, protection_guard.AUTH_REFUSE, login)
            self.assertIn("engine's own identity", detail)

    def test_team_bot_sender_refused(self):
        decision, _ = protection_guard.resolve_labeler_authority("app[bot]", "Bot", self._dir(self._TEAM))
        self.assertEqual(decision, protection_guard.AUTH_REFUSE)

    def test_team_missing_sender_refused(self):
        decision, _ = protection_guard.resolve_labeler_authority(None, None, self._dir(self._TEAM))
        self.assertEqual(decision, protection_guard.AUTH_REFUSE)

    def test_team_without_engine_identity_fails_closed(self):
        d = self._dir({"identity": "team", "home_repository": "o/r"})
        decision, detail = protection_guard.resolve_labeler_authority("alice", "User", d)
        self.assertEqual(decision, protection_guard.AUTH_REFUSE)
        self.assertIn("no distinct engine identity", detail)

    def test_absent_manifest_fails_closed(self):
        decision, detail = protection_guard.resolve_labeler_authority("alice", "User", self._dir(None))
        self.assertEqual(decision, protection_guard.AUTH_REFUSE)
        self.assertIn("could not be read", detail)

    def test_malformed_manifest_fails_closed(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="pg-auth-bad-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        with open(os.path.join(d, "engine.json"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        decision, _ = protection_guard.resolve_labeler_authority("alice", "User", d)
        self.assertEqual(decision, protection_guard.AUTH_REFUSE)

    def test_agrees_with_resolve_tier_on_the_positive_classification(self):
        # resolve_labeler_authority re-inspects identity/engine_identity rather than calling resolve_tier (it
        # must be STRICTER on the ambiguous team-without-identity case). This pins that the two still AGREE on
        # the positive team/solo classification, so a future drift in resolve_tier's TEAM condition is caught
        # rather than silently desyncing the ack-authority boundary (#958 review).
        team = self._dir(self._TEAM)
        self.assertEqual(protection_guard.resolve_tier(team), protection_guard.TEAM)
        self.assertEqual(protection_guard.resolve_labeler_authority("alice", "User", team)[0],
                         protection_guard.AUTH_TEAM)
        solo = self._dir(self._SOLO)
        self.assertEqual(protection_guard.resolve_tier(solo), protection_guard.SOLO)
        self.assertEqual(protection_guard.resolve_labeler_authority("alice", "User", solo)[0],
                         protection_guard.AUTH_SOLO)


if __name__ == "__main__":
    unittest.main()
