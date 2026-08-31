"""Tests for executor_acquisition -- pin enforcement, out-of-band digest verification, identity
separation between a bridge and its vendored agent, and the no-credential-handling witness. Fully
hermetic: every acquisition path in these tests is driven by an injected fake runner that populates a
local fixture tree, so no test can reach real npm or the network."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import executor_acquisition as ea  # noqa: E402


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _build_bridge_fixture(root: str, *, vendor_version: str = "0.2.0",
                           vendor_content: str = "vendored agent code") -> None:
    """A fake bridge package tree: its own package.json, plus a vendored agent nested under
    node_modules with its own DIFFERENT package.json."""
    _write(os.path.join(root, "package.json"),
           json.dumps({"name": "@acp/fake-bridge", "version": "1.2.3"}))
    _write(os.path.join(root, "index.js"), "module.exports = {};")
    vendor_dir = os.path.join(root, "node_modules", "@vendor", "fake-agent")
    _write(os.path.join(vendor_dir, "package.json"),
           json.dumps({"name": "@vendor/fake-agent", "version": vendor_version}))
    _write(os.path.join(vendor_dir, "agent.js"), vendor_content)


class TestIsPinned(unittest.TestCase):
    def test_exact_version_pinned(self):
        self.assertTrue(ea.is_pinned("@agentclientprotocol/codex-acp@1.2.3"))
        self.assertTrue(ea.is_pinned("left-pad@1.3.0"))

    def test_prerelease_and_build_metadata_pinned(self):
        self.assertTrue(ea.is_pinned("pkg@1.2.3-beta.1"))
        self.assertTrue(ea.is_pinned("pkg@1.2.3+build.5"))

    def test_no_version_unpinned(self):
        self.assertFalse(ea.is_pinned("left-pad"))
        self.assertFalse(ea.is_pinned("@scope/name"))

    def test_range_unpinned(self):
        for spec in ("pkg@^1.0.0", "pkg@~1.2", "pkg@>=1", "pkg@1.x"):
            self.assertFalse(ea.is_pinned(spec), spec)

    def test_dist_tag_unpinned(self):
        for spec in ("pkg@latest", "pkg@next", "pkg@beta"):
            self.assertFalse(ea.is_pinned(spec), spec)

    def test_url_or_git_spec_unpinned(self):
        for spec in (
            "pkg@https://example.com/pkg.tgz",
            "git+https://example.com/repo.git",
            "pkg@git+ssh://git@example.com/repo.git#1.2.3",
        ):
            self.assertFalse(ea.is_pinned(spec), spec)

    def test_scoped_name_pinned(self):
        self.assertTrue(ea.is_pinned("@agentclientprotocol/claude-agent-acp@0.1.0"))

    def test_non_string_unpinned(self):
        self.assertFalse(ea.is_pinned(None))
        self.assertFalse(ea.is_pinned(123))


class TestRequirePinned(unittest.TestCase):
    def test_pinned_does_not_raise(self):
        ea.require_pinned("pkg@1.0.0")  # no raise

    def test_unpinned_raises(self):
        with self.assertRaises(ea.AcquisitionError):
            ea.require_pinned("pkg@latest")


class TestTreeDigest(unittest.TestCase):
    def test_stable_and_reproducible_for_identical_trees(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            _build_bridge_fixture(a)
            _build_bridge_fixture(b)
            self.assertEqual(ea.tree_digest(a), ea.tree_digest(b))
            # Also stable across repeated calls on the same tree.
            self.assertEqual(ea.tree_digest(a), ea.tree_digest(a))

    def test_differs_when_a_file_changes(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            _build_bridge_fixture(a)
            _build_bridge_fixture(b, vendor_content="DIFFERENT vendored agent code")
            self.assertNotEqual(ea.tree_digest(a), ea.tree_digest(b))

    def test_differs_when_a_file_is_added(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            _build_bridge_fixture(a)
            _build_bridge_fixture(b)
            _write(os.path.join(b, "extra.txt"), "surprise")
            self.assertNotEqual(ea.tree_digest(a), ea.tree_digest(b))

    def test_non_directory_raises(self):
        with self.assertRaises(ea.AcquisitionError):
            ea.tree_digest("/no/such/directory/at/all")


class TestVerifyDigest(unittest.TestCase):
    def test_matching_digest_passes(self):
        with tempfile.TemporaryDirectory() as d:
            _build_bridge_fixture(d)
            ea.verify_digest(d, ea.tree_digest(d))  # no raise

    def test_mismatched_digest_raises(self):
        with tempfile.TemporaryDirectory() as d:
            _build_bridge_fixture(d)
            with self.assertRaises(ea.AcquisitionError):
                ea.verify_digest(d, "sha256:" + "0" * 64)


class TestIdentifyVendoredAgent(unittest.TestCase):
    def test_identity_separation_from_bridge(self):
        with tempfile.TemporaryDirectory() as root:
            _build_bridge_fixture(root)
            bridge_identity = ea._package_identity(root)
            vendored_identity = ea.identify_vendored_agent(
                root, vendor_subpath="node_modules/@vendor/fake-agent")
            self.assertNotEqual(bridge_identity["name"], vendored_identity["name"])
            self.assertNotEqual(bridge_identity["version"], vendored_identity["version"])
            self.assertNotEqual(bridge_identity["digest"], vendored_identity["digest"])
            self.assertEqual(vendored_identity["name"], "@vendor/fake-agent")
            self.assertEqual(vendored_identity["version"], "0.2.0")
            self.assertTrue(vendored_identity["digest"].startswith("sha256:"))

    def test_missing_vendor_subpath_raises(self):
        with tempfile.TemporaryDirectory() as root:
            _build_bridge_fixture(root)
            with self.assertRaises(ea.AcquisitionError):
                ea.identify_vendored_agent(root, vendor_subpath="node_modules/@vendor/does-not-exist")

    def test_describe_returns_both_identities_distinctly(self):
        with tempfile.TemporaryDirectory() as root:
            _build_bridge_fixture(root)
            result = ea.describe(root, vendor_subpath="node_modules/@vendor/fake-agent")
            self.assertIn("bridge_identity", result)
            self.assertIn("vendored_agent_identity", result)
            self.assertNotEqual(result["bridge_identity"]["digest"],
                                 result["vendored_agent_identity"]["digest"])
            self.assertEqual(result["bridge_identity"]["name"], "@acp/fake-bridge")


class TestCredentialNonProvisionWitness(unittest.TestCase):
    def test_clean_allowlist_env_is_non_provision(self):
        result = ea.credential_non_provision_witness({"PATH": "/usr/bin", "HOME": "/home/x"})
        self.assertEqual(result, {"credential_keys_present": [], "non_provision": True})

    def test_flags_known_credential_key(self):
        result = ea.credential_non_provision_witness({"ANTHROPIC_API_KEY": "sk-fake", "PATH": "/usr/bin"})
        self.assertEqual(result["credential_keys_present"], ["ANTHROPIC_API_KEY"])
        self.assertFalse(result["non_provision"])

    def test_flags_multiple_credential_keys_sorted(self):
        result = ea.credential_non_provision_witness({"NPM_TOKEN": "x", "GITHUB_TOKEN": "y"})
        self.assertEqual(result["credential_keys_present"], ["GITHUB_TOKEN", "NPM_TOKEN"])

    def test_non_mapping_raises(self):
        with self.assertRaises(ea.AcquisitionError):
            ea.credential_non_provision_witness(["not", "a", "mapping"])


class TestAcquire(unittest.TestCase):
    def _fake_runner(self, *, vendor_version="0.2.0", vendor_content="vendored agent code"):
        calls = []

        def runner(spec, cache_dir, *, env):
            calls.append({"spec": spec, "cache_dir": cache_dir, "env": dict(env)})
            _build_bridge_fixture(cache_dir, vendor_version=vendor_version, vendor_content=vendor_content)

        return runner, calls

    def test_acquire_with_matching_digest_succeeds_and_uses_injected_runner(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            runner, calls = self._fake_runner()
            # Compute the expected digest out-of-band, from a separately built identical fixture --
            # never derived from the tree the acquire() call is about to produce.
            with tempfile.TemporaryDirectory() as reference:
                _build_bridge_fixture(reference)
                expected = ea.tree_digest(reference)

            result = ea.acquire(
                "@acp/fake-bridge@1.2.3",
                cache_dir=cache_dir,
                expected_digest=expected,
                runner=runner,
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["spec"], "@acp/fake-bridge@1.2.3")
            self.assertEqual(calls[0]["cache_dir"], cache_dir)
            self.assertEqual(result["digest"], expected)
            self.assertTrue(result["scripts_disabled"])

    def test_acquire_refuses_unpinned_spec_without_calling_runner(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            runner, calls = self._fake_runner()
            with self.assertRaises(ea.AcquisitionError):
                ea.acquire(
                    "@acp/fake-bridge@latest",
                    cache_dir=cache_dir,
                    expected_digest="sha256:" + "0" * 64,
                    runner=runner,
                )
            self.assertEqual(calls, [])  # refused before the runner (and therefore npm) ever ran

    def test_acquire_refuses_on_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            # This fake runner populates a tree that will NOT match the expected digest below.
            runner, calls = self._fake_runner(vendor_content="a completely different payload")
            with self.assertRaises(ea.AcquisitionError):
                ea.acquire(
                    "@acp/fake-bridge@1.2.3",
                    cache_dir=cache_dir,
                    expected_digest="sha256:" + "0" * 64,
                    runner=runner,
                )
            self.assertEqual(len(calls), 1)  # runner ran, but the mismatch was still caught and refused

    def test_acquire_does_not_smuggle_ambient_environment_credentials(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            runner, calls = self._fake_runner()
            with tempfile.TemporaryDirectory() as reference:
                _build_bridge_fixture(reference)
                expected = ea.tree_digest(reference)

            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-bogus-ambient-value"}):
                result = ea.acquire(
                    "@acp/fake-bridge@1.2.3",
                    cache_dir=cache_dir,
                    expected_digest=expected,
                    runner=runner,
                    # No env passed through explicitly -- acquire() must not reach into os.environ
                    # on its own and hand the ambient credential to the runner.
                )
            self.assertNotIn("ANTHROPIC_API_KEY", calls[0]["env"])
            self.assertNotIn("sk-bogus-ambient-value", json.dumps(result))


class TestRegistryPresenceDisclaimer(unittest.TestCase):
    def test_disclaimer_constant_is_present_and_says_discovery_only(self):
        self.assertIn("DISCOVERY", ea.REGISTRY_PRESENCE_IS_DISCOVERY_ONLY)
        self.assertIn("never", ea.REGISTRY_PRESENCE_IS_DISCOVERY_ONLY.lower())


if __name__ == "__main__":
    unittest.main()
