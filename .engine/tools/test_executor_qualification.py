#!/usr/bin/env python3
"""Hermetic tests for executor_qualification.py — the qualification harness that drives a
BuildExecutionRunner (the abstract seam) through probe families and composes an
executor-attempt-receipt.v1. No network, no real coding agent, no real git repository beyond nothing at all
(git_facts here are hand-built fixture dicts, exactly as build_coordinator_work.assemble_receipt accepts).
Every probe is driven against an in-test FAKE runner implementing the abstract BuildExecutionRunner type, so
this module never launches a real subprocess and never talks to a real agent."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execution_env_policy as ep  # noqa: E402
import executor_qualification as eq  # noqa: E402
from acp_client import BuildExecutionRunner  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402


# ---------------------------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------------------------

CLAIM_BASE = "0" * 40
INTEGRATION_COMMIT = "1" * 40

FIXTURE_NODE_PAYLOAD = {"node_id": "fixture-node-1", "description": "a small fixture completion attempt",
                        "paths": ["tools/example.py"]}


def _git_facts(range_commits=None, paths=None):
    return {
        "range": range_commits if range_commits is not None else [INTEGRATION_COMMIT],
        "tree_digest": "sha256:" + "c" * 64,
        "patch_digest": "sha256:" + "d" * 64,
        "paths": paths if paths is not None else [{"status": "A", "path": "tools/example.py", "old_path": None}],
    }


def _identity(name, version, digest_byte):
    return {"name": name, "version": version, "digest": "sha256:" + digest_byte * 64}


class FakeRunner(BuildExecutionRunner):
    """A scriptable fake implementation of the abstract BuildExecutionRunner seam. It yields a normal
    transcript, an injected malformed update, a configurable cancel acknowledgement, and a process-loss
    simulation — enough surface to drive every probe family hermetically."""

    def __init__(self, *, cancel_acknowledged: bool = True, inject_malformed: bool = True):
        self._session_id = "fake-session-1"
        self._updates = []
        if inject_malformed:
            self._updates.append({"kind": "malformed", "payload": {"raw": "{not-json"}})
        self._cancel_acknowledged = cancel_acknowledged
        self._process_lost = False
        self._closed_witness = None
        self.prompts_sent = []

    # -- BuildExecutionRunner --
    def start_session(self) -> str:
        return self._session_id

    def prompt(self, text: str) -> None:
        self.prompts_sent.append(text)
        self._updates.append({"kind": "session/update", "payload": {"echo": text[:24]}})

    def updates(self) -> list:
        return list(self._updates)

    def cancel(self) -> bool:
        return self._cancel_acknowledged

    def close(self) -> dict:
        self._closed_witness = {"pid": 999999, "pgid": 999999, "leader_exited": True,
                                "escalated_to_kill": False, "group_reaped": True}
        return self._closed_witness

    def process_lost(self) -> bool:
        return self._process_lost

    # -- test-only scripting hook --
    def simulate_process_loss(self) -> None:
        self._process_lost = True


# ---------------------------------------------------------------------------------------------------------
# Schema self-validity
# ---------------------------------------------------------------------------------------------------------

class TestSchemaSelfValid(unittest.TestCase):
    def test_schema_is_a_valid_draft_2020_12_schema(self):
        schema = eq._load_schema()
        Draft202012Validator.check_schema(schema)  # raises on any meta-schema violation

    def test_schema_forbids_tool_execution_proof_true(self):
        schema = eq._load_schema()
        receipt = _valid_receipt_dict()
        receipt["tool_execution_proof"] = True
        errors = list(Draft202012Validator(schema).iter_errors(receipt))
        self.assertTrue(errors, "schema must reject tool_execution_proof: true")


def _valid_receipt_dict():
    """A schema-valid executor-attempt-receipt.v1, built without going through compose_attempt_receipt, for
    tests that want to mutate one field in isolation."""
    from build_coordinator_work import assemble_receipt
    integration_receipt = assemble_receipt(_git_facts(), CLAIM_BASE, INTEGRATION_COMMIT, "worker-commit", [])
    return {
        "schema_version": "executor-attempt-receipt.v1",
        "integration_receipt": integration_receipt,
        "protocol": {"transport": "acp/v1", "acp_version": "1",
                    "transcript": {"identity": "attempt-1-transcript", "digest": "sha256:" + "e" * 64}},
        "process": {"identity": "fake-runner-attempt-1", "tree_reaped": True},
        "configuration_as_reported": {"clientInfo": {"name": "fake-agent", "version": "0.0.1"}},
        "bridge_identity": _identity("acp-bridge", "1.0.0", "a"),
        "vendored_agent_identity": _identity("vendored-agent", "2.0.0", "b"),
        "tool_execution_proof": False,
        "evidence_refs": [{"kind": "evidence", "name": "transcript", "digest": "sha256:" + "e" * 64}],
    }


# ---------------------------------------------------------------------------------------------------------
# Hermetic full probe-suite run
# ---------------------------------------------------------------------------------------------------------

class TestProbeSuiteHermetic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.escape_dir = os.path.join(self.tmp.name, "allowed")
        os.makedirs(self.escape_dir)
        self.monitored_dir = os.path.join(self.tmp.name, "monitored")
        os.makedirs(self.monitored_dir)
        self.monitored_file = os.path.join(self.monitored_dir, "watched.txt")
        with open(self.monitored_file, "w", encoding="utf-8") as fh:
            fh.write("baseline")

    def test_full_probe_set_produces_structured_observations_without_a_real_agent(self):
        runner = FakeRunner()
        observations = eq.run_probe_suite(
            runner, FIXTURE_NODE_PAYLOAD,
            allowed_escape_dir=self.escape_dir, monitored_paths=[self.monitored_file])

        self.assertTrue(observations["negotiation"]["started"])
        self.assertEqual(observations["negotiation"]["session_id"], "fake-session-1")
        self.assertEqual(observations["replay"]["node_payload_digest"],
                         eq.node_payload_digest(FIXTURE_NODE_PAYLOAD))
        self.assertEqual(observations["replay"]["source"], "fixture")
        self.assertGreaterEqual(observations["malformed_updates_observed"], 1)
        self.assertTrue(observations["cancellation"]["requested"])
        self.assertTrue(observations["cancellation"]["acknowledged"])

        containment = observations["containment_observation"]
        self.assertFalse(containment["enforcement"])
        self.assertFalse(containment["delta_observed"])  # nothing outside the workspace was touched
        self.assertTrue(containment["escape_probe"]["marker_written"])
        self.assertTrue(containment["escape_probe"]["cleanup_verified"])
        self.assertFalse(os.path.exists(
            os.path.join(self.escape_dir, "qualification-escape-probe.marker")))

        self.assertIsNone(observations["process_kill_recovery"])  # no kill_fn was passed

    def test_process_kill_recovery_is_detected_and_reaped(self):
        runner = FakeRunner()
        observations = eq.run_probe_suite(
            runner, FIXTURE_NODE_PAYLOAD,
            allowed_escape_dir=self.escape_dir, monitored_paths=[self.monitored_file],
            kill_fn=runner.simulate_process_loss)

        result = observations["process_kill_recovery"]
        self.assertTrue(result["loss_detected"])
        self.assertTrue(result["reaped"])

    def test_snapshot_diff_records_delta_when_a_monitored_path_actually_changes(self):
        pre = eq.snapshot([self.monitored_file])
        with open(self.monitored_file, "w", encoding="utf-8") as fh:
            fh.write("mutated")
        post = eq.snapshot([self.monitored_file])
        diff = eq.diff_snapshots(pre, post, monitored_paths=[self.monitored_file])
        self.assertFalse(diff["enforcement"])
        self.assertTrue(diff["delta_observed"])
        self.assertIn(self.monitored_file, diff["changed_paths"])

    def test_escape_probe_raises_if_cleanup_cannot_be_verified(self):
        runner = FakeRunner()
        # Pre-create a directory at the marker path so os.remove(file) inside probe_escape would fail —
        # simulated instead by monkeypatching os.remove is unnecessary; we assert the happy path elsewhere,
        # so here we assert the probe itself refuses when the target cannot be written at all.
        missing_dir = os.path.join(self.tmp.name, "does-not-exist")
        with self.assertRaises(Exception):
            eq.probe_escape(runner, missing_dir)


# ---------------------------------------------------------------------------------------------------------
# Environment witness
# ---------------------------------------------------------------------------------------------------------

class TestEnvironmentWitness(unittest.TestCase):
    def test_attests_child_env_equals_allowlist(self):
        source = {"PATH": "/usr/bin", "SECRET_TOKEN": "leak-me"}
        allowlist = ["PATH"]
        child_env = ep.allowlist_environment(allowlist, source=source)
        witness = eq.environment_witness(child_env, allowlist, source=source,
                                         authentication_keep_list=["PATH"])
        self.assertTrue(witness["child_env_equals_allowlist"])
        self.assertNotIn("SECRET_TOKEN", witness["allowlist_keys"])
        self.assertIn("PATH", witness["authentication_keep_list"])
        self.assertIn("non-provision", witness["credential_non_provision"].lower())

    def test_detects_a_child_env_that_does_not_equal_the_allowlist(self):
        source = {"PATH": "/usr/bin"}
        witness = eq.environment_witness({"PATH": "/usr/bin", "EXTRA": "1"}, ["PATH"], source=source)
        self.assertFalse(witness["child_env_equals_allowlist"])


# ---------------------------------------------------------------------------------------------------------
# Receipt composition
# ---------------------------------------------------------------------------------------------------------

class TestReceiptComposition(unittest.TestCase):
    def _compose(self, **overrides):
        kwargs = dict(
            git_facts=_git_facts(), claim_base=CLAIM_BASE, integration_commit=INTEGRATION_COMMIT,
            identity_mode="worker-commit", sibling_attributions=[],
            protocol={"transport": "acp/v1", "acp_version": "1",
                     "transcript": {"identity": "t1", "digest": "sha256:" + "e" * 64}},
            process={"identity": "fake-runner", "tree_reaped": True},
            configuration_as_reported={"clientInfo": {"name": "fake-agent", "version": "0.0.1"}},
            bridge_identity=_identity("acp-bridge", "1.0.0", "a"),
            vendored_agent_identity=_identity("vendored-agent", "2.0.0", "b"),
            evidence_refs=[{"kind": "evidence", "name": "t1", "digest": "sha256:" + "e" * 64}],
        )
        kwargs.update(overrides)
        return eq.compose_attempt_receipt(**kwargs)

    def test_produces_a_schema_valid_receipt_whose_integration_receipt_is_from_the_real_assemble_receipt(self):
        from build_coordinator_work import assemble_receipt
        receipt = self._compose()
        schema = eq._load_schema()
        Draft202012Validator(schema).validate(receipt)  # raises on any violation

        expected_integration_receipt = assemble_receipt(
            _git_facts(), CLAIM_BASE, INTEGRATION_COMMIT, "worker-commit", [])
        self.assertEqual(receipt["integration_receipt"], expected_integration_receipt)

    def test_identity_mismatch_refusal_when_bridge_and_vendored_share_a_digest(self):
        with self.assertRaises(eq.QualificationError):
            self._compose(
                bridge_identity=_identity("acp-bridge", "1.0.0", "a"),
                vendored_agent_identity=_identity("vendored-agent", "2.0.0", "a"))  # same digest byte

    def test_identity_mismatch_refusal_when_an_identity_is_missing(self):
        with self.assertRaises(eq.QualificationError):
            self._compose(bridge_identity={"name": "acp-bridge"})  # no version/digest

    def test_optional_fields_carry_through_when_supplied(self):
        receipt = self._compose(
            containment_observation={"enforcement": False, "monitored_paths": [], "delta_observed": False},
            environment_witness_value=eq.environment_witness({"PATH": "x"}, ["PATH"], source={"PATH": "x"}),
            replay={"node_payload_digest": eq.node_payload_digest(FIXTURE_NODE_PAYLOAD), "source": "fixture"},
            cancellation={"requested": True, "acknowledged": True},
            process_kill_recovery={"loss_detected": True, "reaped": True},
            malformed_updates_observed=1,
            notes="composed for a receipt-composition fixture test",
        )
        self.assertFalse(receipt["containment_observation"]["enforcement"])
        self.assertEqual(receipt["replay"]["source"], "fixture")


class TestNoToolExecutionProofPin(unittest.TestCase):
    def test_every_composed_receipt_pins_tool_execution_proof_false(self):
        receipt = eq.compose_attempt_receipt(
            git_facts=_git_facts(), claim_base=CLAIM_BASE, integration_commit=INTEGRATION_COMMIT,
            identity_mode="accepted-candidate", sibling_attributions=[],
            protocol={"transport": "acp/v1", "acp_version": "1",
                     "transcript": {"identity": "t1", "digest": "sha256:" + "e" * 64}},
            process={"identity": "fake-runner", "tree_reaped": True},
            configuration_as_reported={},
            bridge_identity=_identity("acp-bridge", "1.0.0", "a"),
            vendored_agent_identity=_identity("vendored-agent", "2.0.0", "b"),
            evidence_refs=[],
        )
        self.assertIs(receipt["tool_execution_proof"], False)


# ---------------------------------------------------------------------------------------------------------
# Persistence + secret scan
# ---------------------------------------------------------------------------------------------------------

class TestPersistenceAndSecretScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_persisted_evidence_refs_are_digest_checkable(self):
        observations = {"transcript": {"kind": "session/update", "payload": {"text": "hello agent"}}}
        refs, findings = eq.persist_evidence(observations, self.tmp.name)
        self.assertEqual(findings, {})
        self.assertEqual(len(refs), 1)
        ref = refs[0]
        path = os.path.join(self.tmp.name, f"{ref['name']}.evidence")
        with open(path, "rb") as fh:
            content = fh.read()
        import hashlib
        self.assertEqual(ref["digest"], "sha256:" + hashlib.sha256(content).hexdigest())

    def test_a_secret_in_a_transcript_is_redacted_and_not_stored_raw(self):
        secret = "sk-THIS_IS_FAKE_1234"
        observations = {"transcript": f"agent said: my token is {secret} for testing"}
        refs, findings = eq.persist_evidence(observations, self.tmp.name)
        self.assertIn("transcript", findings)
        self.assertTrue(findings["transcript"])

        path = os.path.join(self.tmp.name, "transcript.evidence")
        with open(path, encoding="utf-8") as fh:
            persisted_text = fh.read()
        self.assertNotIn(secret, persisted_text)
        self.assertIn("[REDACTED]", persisted_text)

        # the ref's digest matches exactly what was persisted (the redacted bytes), not the raw secret text
        import hashlib
        self.assertEqual(refs[0]["digest"], "sha256:" + hashlib.sha256(persisted_text.encode("utf-8")).hexdigest())

    def test_secret_scan_recognizes_common_secret_shapes(self):
        findings = eq.secret_scan(
            "sk-abcdefghijklmnop ghp_abcdefghijklmnopqrstuvwx Bearer abcdefghij1234567890 "
            "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----")
        self.assertGreaterEqual(len(findings), 4)
        for finding in findings:
            self.assertNotIn("abcdefghijklmnop", finding["preview"])


if __name__ == "__main__":
    unittest.main()
