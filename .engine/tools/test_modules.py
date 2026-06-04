#!/usr/bin/env python3
"""Self-tests for slice 6 — the module system: manifest grammar (module.v1 / engine.v1),
the ownership-coherence leg, and the module-coherence consumer.

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

These lock the load-bearing teeth: the manifest schemas bite on each malformed shape, the
ownership leg flags orphans and double-claims, the two committed schema rules resolve their
repo-root-relative schema override and pass the real manifests, and the consumer reports the
real repository as coherent. The deliverable-gate cold review attests each test's assertion
matches its name; CI runs them as a step in engine-ci.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import module_coherence  # noqa: E402

MODULE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "module.v1.json"))
ENGINE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "engine.v1.json"))

VALID_MODULE = {"id": "x", "version": "1.0.0", "status": "optional",
                "provides": {"tool": [".engine/tools/*.py"]}}
VALID_ENGINE = {"engine_release": "0.0.0-dev", "packages": {"core": "0.0.0-dev"},
                "identity": "solo"}


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


def _write(d, name, obj):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh) if isinstance(obj, (dict, list)) else fh.write(obj)
    return p


def _run_kind(kind_fn, rule, files):
    """Run a kind callable with validate.target_files stubbed to `files`."""
    orig = validate.target_files
    validate.target_files = lambda r: list(files)
    try:
        return kind_fn(rule, {})
    finally:
        validate.target_files = orig


class TestModuleSchema(unittest.TestCase):
    def test_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(MODULE_SCHEMA)

    def test_valid_manifest_passes(self):
        self.assertEqual(_errors(MODULE_SCHEMA, VALID_MODULE), [])

    def test_real_core_manifest_conforms(self):
        real = validate.load_json(os.path.join(validate.ROOT, ".engine", "modules", "core",
                                               "manifest.json"))
        self.assertEqual(_errors(MODULE_SCHEMA, real), [])

    def test_missing_required_field_is_flagged(self):
        for drop in ("id", "version", "status", "provides"):
            bad = {k: v for k, v in VALID_MODULE.items() if k != drop}
            self.assertTrue(_errors(MODULE_SCHEMA, bad), f"missing {drop} should fail")

    def test_status_outside_the_closed_set_is_flagged(self):
        self.assertTrue(_errors(MODULE_SCHEMA, {**VALID_MODULE, "status": "bogus"}))

    def test_field_outside_the_grammar_is_flagged(self):
        self.assertTrue(_errors(MODULE_SCHEMA, {**VALID_MODULE, "extra": 1}))

    def test_wires_type_must_be_a_closed_seam(self):
        self.assertEqual(_errors(MODULE_SCHEMA, {**VALID_MODULE, "wires": [{"type": "hook"}]}), [])
        self.assertTrue(_errors(MODULE_SCHEMA, {**VALID_MODULE, "wires": [{"type": "custom"}]}),
                        "an unknown seam type must be rejected (no wiring escape hatch)")

    def test_depends_with_range_is_allowed(self):
        self.assertEqual(_errors(MODULE_SCHEMA, {**VALID_MODULE, "depends": {"core": ">=1.0.0"}}), [])


class TestEngineSchema(unittest.TestCase):
    def test_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(ENGINE_SCHEMA)

    def test_valid_manifest_passes(self):
        self.assertEqual(_errors(ENGINE_SCHEMA, VALID_ENGINE), [])

    def test_real_engine_manifest_conforms(self):
        real = validate.load_json(os.path.join(validate.ROOT, ".engine", "engine.json"))
        self.assertEqual(_errors(ENGINE_SCHEMA, real), [])

    def test_missing_required_field_is_flagged(self):
        for drop in ("engine_release", "packages", "identity"):
            bad = {k: v for k, v in VALID_ENGINE.items() if k != drop}
            self.assertTrue(_errors(ENGINE_SCHEMA, bad), f"missing {drop} should fail")

    def test_identity_outside_the_closed_set_is_flagged(self):
        self.assertTrue(_errors(ENGINE_SCHEMA, {**VALID_ENGINE, "identity": "boss"}))

    def test_field_outside_the_grammar_is_flagged(self):
        self.assertTrue(_errors(ENGINE_SCHEMA, {**VALID_ENGINE, "extra": 1}))


class TestSchemaRulesIntegration(unittest.TestCase):
    """The two committed schema rules resolve their repo-root-relative params.schema override
    and bite. Proves the override path (base = ROOT, not the schemas dir) — the slice-6
    plan-gate's blocking fix — actually validates the manifests it targets."""

    def _rule(self, name):
        return validate.load_json(os.path.join(validate.CHECK_DIR, name))

    def test_rules_are_well_formed_and_join_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        for name in ("module-manifest.json", "engine-manifest.json"):
            rule = self._rule(name)
            self.assertEqual(list(validate.Draft202012Validator(check_schema).iter_errors(rule)), [])
            self.assertIn("CI", rule.get("suites", []))

    def test_real_module_manifest_passes_via_the_rule(self):
        rule = self._rule("module-manifest.json")
        real = os.path.join(validate.ROOT, ".engine", "modules", "core", "manifest.json")
        passed, found = _run_kind(validate.kind_schema, rule, [real])
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_real_engine_manifest_passes_via_the_rule(self):
        rule = self._rule("engine-manifest.json")
        real = os.path.join(validate.ROOT, ".engine", "engine.json")
        passed, found = _run_kind(validate.kind_schema, rule, [real])
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_malformed_manifest_is_flagged_at_tier_via_the_rule(self):
        rule = self._rule("module-manifest.json")
        with tempfile.TemporaryDirectory() as d:
            bad = _write(d, "manifest.json", {"id": "x"})  # missing version/status/provides
            passed, found = _run_kind(validate.kind_schema, rule, [bad])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))


class TestOwnershipFindings(unittest.TestCase):
    """The pure ownership leg: orphan (no owner, not exempt), double-claim (>1 owner),
    exempt suppresses an orphan, and a single owner is clean."""

    def test_clean_when_each_file_has_one_owner_or_is_exempt(self):
        inv = [".engine/check/a.json", ".engine/engine.json"]
        claims = {".engine/check/a.json": ["core"]}
        self.assertEqual(
            validate.ownership_findings(inv, claims, {".engine/engine.json"}, "hard", "m"), [])

    def test_orphan_is_flagged(self):
        found = validate.ownership_findings([".engine/extra/thing.json"], {}, set(), "hard", "m")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "hard")
        self.assertIn("orphan", found[0]["message"])
        self.assertEqual(found[0]["location"]["file"], ".engine/extra/thing.json")

    def test_exempt_file_is_not_an_orphan(self):
        self.assertEqual(
            validate.ownership_findings([".engine/uv.lock"], {}, {".engine/uv.lock"}, "hard", "m"), [])

    def test_double_claim_is_flagged(self):
        found = validate.ownership_findings(
            [".engine/x.json"], {".engine/x.json": ["core", "other"]}, set(), "hard", "m")
        self.assertEqual(len(found), 1)
        self.assertIn("more than one module", found[0]["message"])
        self.assertIn("core", found[0]["message"])
        self.assertIn("other", found[0]["message"])


class TestModuleCoherenceConsumer(unittest.TestCase):
    """The consumer over the REAL repository: discovery reads the present set, and both
    coherence legs report the committed tree as coherent (the clean baseline the demo
    starts from)."""

    def test_discover_manifests_finds_core(self):
        ids = [m.get("id") for _path, m in module_coherence.discover_manifests()]
        self.assertIn("core", ids)

    def test_load_engine_manifest_reads_solo_tier(self):
        em = module_coherence.load_engine_manifest()
        self.assertIsNotNone(em)
        self.assertEqual(em.get("identity"), "solo")
        self.assertIn("core", em.get("packages", {}))

    def test_provides_claims_map_real_files_to_core(self):
        claims = module_coherence.provides_claims(module_coherence.discover_manifests())
        self.assertEqual(claims.get(".engine/suites.json"), ["core"])  # the foundation group
        self.assertEqual(claims.get(".engine/tools/validate.py"), ["core"])

    def test_check_corpus_split_core_two_guards_validators_core_fourteen(self):
        # The locked engine/corpus boundary (D-089/D-090; validators-core README; validation README):
        # core ships the validation engine and owns ZERO rules EXCEPT the two §15 frozen-named guards;
        # the self-validation corpus is validators-core's (14 rules: the 12 after catalog-coverage/issue #30,
        # plus the policy- and contract-frontmatter live schema rules, issue #26).
        # The files stay under .engine/check/ — ownership is a `provides` claim, not a location. This
        # test pins that exact split so a future wildcard re-introduction (which would double-claim the
        # corpus) cannot pass silently.
        manifests = module_coherence.discover_manifests()
        ids = {m.get("id") for _path, m in manifests}
        self.assertIn("validators-core", ids, "validators-core must be a present module")

        claims = module_coherence.provides_claims(manifests)
        check_owner = {rel: owners for rel, owners in claims.items()
                       if rel.startswith(".engine/check/") and rel.endswith(".json")}
        # every check file is owned by exactly one module (no orphan, no double-claim)
        for rel, owners in check_owner.items():
            self.assertEqual(len(owners), 1, f"{rel} must have exactly one owner, got {owners}")

        core_checks = sorted(r for r, o in check_owner.items() if o == ["core"])
        vc_checks = sorted(r for r, o in check_owner.items() if o == ["validators-core"])
        self.assertEqual(core_checks, [
            ".engine/check/guardrail-weakening.json",
            ".engine/check/protection.json",
        ], "core owns exactly the two §15 guards")
        self.assertEqual(vc_checks, [
            ".engine/check/catalog-coverage.json",
            ".engine/check/contract-frontmatter.json",
            ".engine/check/contract-shape.json",
            ".engine/check/contract-threshold.json",
            ".engine/check/engine-manifest.json",
            ".engine/check/interface-declaration.json",
            ".engine/check/knowledge-coverage.json",
            ".engine/check/link-integrity.json",
            ".engine/check/module-manifest.json",
            ".engine/check/policy-frontmatter.json",
            ".engine/check/policy-shape.json",
            ".engine/check/pr-body-completeness.json",
            ".engine/check/self-map-drift.json",
            ".engine/check/state-cursor.json",
        ], "validators-core owns exactly the 14 corpus rules")
        # the split partitions ALL committed check files — nothing left unclaimed
        all_checks = sorted(r for r in module_coherence.engine_file_inventory()
                            if r.startswith(".engine/check/") and r.endswith(".json"))
        self.assertEqual(sorted(core_checks + vc_checks), all_checks,
                         "every .engine/check/*.json is claimed by exactly one of core / validators-core")
        # validators-core depends on core (presence assertion, any version)
        vc = next(m for _p, m in manifests if m.get("id") == "validators-core")
        self.assertEqual(vc.get("depends"), {"core": ""})

    def test_real_repository_is_coherent(self):
        # The whole point of the slice: the committed tree has no unowned engine file and no
        # broken dependency. This is the green baseline the operator's fail-then-pass demo
        # perturbs (add a stray file -> orphan; add a ghost dependency -> unsatisfied).
        findings = module_coherence.check_coherence()
        hard = [f for f in findings if f["severity"] == "hard"]
        self.assertEqual(hard, [], f"unexpected coherence findings: {[f['message'] for f in hard]}")

    def test_main_exit_zero_on_clean_tree(self):
        self.assertEqual(module_coherence.main([]), 0)

    def test_malformed_manifest_is_a_plain_config_error_not_a_traceback(self):
        # A non-JSON manifest must surface as a plain CONFIG ERROR (exit 2), never a raw
        # Python traceback to the non-engineer — the validator's halt-on-malformed posture.
        import contextlib
        import io
        orig = module_coherence.discover_manifests
        module_coherence.discover_manifests = lambda: (_ for _ in ()).throw(
            ValueError("manifest.json is not valid JSON"))
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                rc = module_coherence.main([])
        finally:
            module_coherence.discover_manifests = orig
        self.assertEqual(rc, 2)
        self.assertIn("CONFIG ERROR", err.getvalue())


if __name__ == "__main__":
    unittest.main()
