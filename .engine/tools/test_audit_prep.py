"""The audit-prep scheduled self-review workflow (audit-library): a committed workflow that runs the
read-only audit persona on a schedule and commits the digest it produces. These tests attest the
load-bearing facts a non-engineer cannot read off the YAML: the file exists, is engine-owned (travels on
upgrade + renders into CODEOWNERS), runs on a schedule, gates cleanly when unarmed, pins the
operator-configurable model knob's default, and wires the tested seal/refresh plumbing. The end-to-end run
itself is disclosed-unrun (no token in this construction repo); its grammar is covered by the actionlint
workflow. Mirrors test_actionlint.py / test_secret_scan.py."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate            # noqa: E402
import module_coherence    # noqa: E402
import module_manager      # noqa: E402

WORKFLOW_REL = ".github/workflows/audit-prep.yml"


class TestAuditPrepIsAnEngineOwnedTraveler(unittest.TestCase):
    """The workflow is a FOUNDATION_INFRA member, so it travels on upgrade (FOUNDATION_CODE) and is owned in
    CODEOWNERS (foundation_infra_paths) — the same treatment as the other engine workflows."""

    def test_workflow_is_present_in_the_tree(self):
        self.assertTrue(os.path.isfile(os.path.join(validate.ROOT, WORKFLOW_REL)),
                        f"{WORKFLOW_REL} must exist")

    def test_is_a_foundation_infra_member(self):
        self.assertIn(WORKFLOW_REL, module_coherence.FOUNDATION_INFRA)

    def test_travels_on_upgrade_via_foundation_code(self):
        self.assertIn(WORKFLOW_REL, module_manager.FOUNDATION_CODE)

    def test_renders_into_codeowners_via_foundation_infra_paths(self):
        owned = module_coherence.foundation_infra_paths()
        self.assertIn(WORKFLOW_REL, owned)
        self.assertFalse(any("*" in p for p in owned), "paths are concrete, never bare globs")


class TestAuditPrepShape(unittest.TestCase):
    """The load-bearing grammar a non-engineer cannot read off the YAML."""

    def _text(self):
        return validate.read(os.path.join(validate.ROOT, WORKFLOW_REL))

    def test_runs_on_a_schedule(self):
        text = self._text()
        self.assertIn("schedule:", text)
        self.assertIn("cron:", text)

    def test_runs_the_audit_persona_read_only_via_direct_claude(self):
        # Direct `claude` (not an action that would let the agent commit) keeps the persona read-only — the
        # workflow's own later steps commit.
        text = self._text()
        self.assertIn("claude -p", text)
        self.assertIn("--agent audit", text)

    def test_gates_cleanly_when_unarmed(self):
        # The skip-when-unarmed pattern: a gate job's output the real job is conditioned on, so a repo with
        # no token never accumulates failing runs.
        text = self._text()
        self.assertIn("needs.gate.outputs.armed", text)

    def test_model_knob_defaults_to_opus(self):
        # The operator-configurable judgment model, defaulted so an unset variable never drops the run to a
        # platform default. Pinned here so the default cannot silently change.
        self.assertIn("vars.AUDIT_MODEL || 'opus'", self._text())

    def test_seals_the_digest_and_refreshes_the_cache(self):
        text = self._text()
        self.assertIn("audit_digest.py", text)
        self.assertIn("--body-file", text)
        self.assertIn("telemetry.py refresh", text)

    def test_seal_path_is_engine_relative_not_doubled(self):
        # Regression (#176): the Seal step runs with `--directory .engine` (cwd becomes .engine), so its
        # positional path must be RELATIVE to .engine — `seal audits/audit-digest.md`, which resolves to the
        # canonical .engine/audits/audit-digest.md that the boot staleness read, the fingerprint check, and the
        # later `git add` all key on. A `.engine/`-prefixed path nested to `.engine/.engine/audits/...` — the
        # doubled-path bug that wrote the digest where nothing could find it (the `git add` then aborted with
        # exit 128, and boot could never see it). Pin the `seal `-prefixed form so the trap cannot return.
        text = self._text()
        self.assertIn("seal audits/audit-digest.md", text)
        self.assertNotIn("seal .engine/audits/audit-digest.md", text)

    def test_branch_name_is_collision_proof(self):
        # Regression (round-2 finding 4): the first real run pushed `audit-prep/<date>`, and a same-day re-run
        # reused that exact name — the push failed non-fast-forward, so a leftover branch from a failed run
        # blocked the next run. The branch now carries the unique-per-run id (and attempt), so no leftover
        # branch can ever collide; uniqueness sidesteps a force-push and the protected-branch deletion guard
        # (which scopes to the default branch only) entirely. The id/attempt are passed via env (RUN_ID /
        # RUN_ATTEMPT) rather than interpolated into the shell, so pin both the wiring and the branch form.
        text = self._text()
        # Both must be wired in: the run id (unique across distinct runs) AND the run attempt (the only thing
        # that differs when the SAME run is re-run — which is the original same-day-re-run failure). Dropping
        # `-${RUN_ATTEMPT}` would silently re-break the re-run case, so pin both and the full branch form.
        self.assertIn("github.run_id", text)
        self.assertIn("github.run_attempt", text)
        self.assertIn("audit-prep/${stamp}-${RUN_ID}-${RUN_ATTEMPT}", text)  # the full collision-proof form
        self.assertNotIn('branch="audit-prep/${stamp}"', text)               # the collision-prone form must not return

    def test_fetches_the_issue_backlog_and_feeds_the_read_only_persona(self):
        # #183 / Option 1: the persona never reaches GitHub. The workflow grants only `issues: read`, fetches
        # the engine-labelled backlog through the telemetry boundary, and feeds it into the persona's prompt —
        # so concern #2 has its data without the read-only persona ever holding a GitHub token.
        text = self._text()
        self.assertIn("issues: read", text)                         # the only new scope, read-only
        self.assertIn("telemetry.py engine-issues", text)           # the workflow fetches the backlog
        self.assertIn("BEGIN OPEN ENGINE-LABELLED ISSUES", text)    # …and feeds it into the persona's prompt

    def test_persona_step_stays_token_less(self):
        # The read-only persona must never receive a GitHub token — the fetch/refresh/PR steps are the only
        # holders. Slice the persona step (its own `claude -p` block) and assert no GitHub token reaches it,
        # so a future edit can't quietly hand the persona write-capable creds.
        text = self._text()
        start = text.index("Run the read-only self-review")
        persona_step = text[start:text.index("- name:", start)]     # up to the next step (Seal)
        self.assertIn("claude -p", persona_step)                    # sanity: this is the persona step
        self.assertNotIn("GITHUB_TOKEN", persona_step)
        self.assertNotIn("GH_TOKEN", persona_step)


if __name__ == "__main__":
    unittest.main()
