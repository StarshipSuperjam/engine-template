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
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import module_coherence  # noqa: E402
import wiring            # noqa: E402

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

    def test_wires_must_be_fully_formed_per_seam(self):
        # The closed-seam gate is retained: an unknown type is still rejected (no escape hatch).
        self.assertTrue(_errors(MODULE_SCHEMA, {**VALID_MODULE, "wires": [{"type": "custom"}]}),
                        "an unknown seam type must be rejected (no wiring escape hatch)")
        # A fully-formed wire of each seam passes.
        full = {
            "hook": {"type": "hook", "event": "PreToolUse", "matcher": "Bash",
                     "hook": {"type": "command", "command": "${CLAUDE_PROJECT_DIR}/.engine/x.py"}},
            "mcp": {"type": "mcp", "name": "engine-x", "definition": {"command": "uv", "args": []}},
            "ontology-entry": {"type": "ontology-entry", "name": "widget", "record": {"a": 1}},
            "permission": {"type": "permission", "value": "Read(./src/**)"},
            "gitignore": {"type": "gitignore", "key": "core", "lines": [".engine/.venv/"]},
        }
        for seam, wire in full.items():
            self.assertEqual(_errors(MODULE_SCHEMA, {**VALID_MODULE, "wires": [wire]}), [],
                             f"a fully-formed {seam} wire must pass")
        # A BARE wire (type only, missing the seam's required fields) is now REJECTED — the gap this
        # slice closes (a {type:mcp} with no definition used to pass the hard CI module-manifest gate).
        for seam in ("hook", "mcp", "ontology-entry", "permission", "gitignore"):
            self.assertTrue(_errors(MODULE_SCHEMA, {**VALID_MODULE, "wires": [{"type": seam}]}),
                            f"a bare {seam} wire (missing required fields) must be rejected")
        # An extra key beyond the seam's vocabulary is rejected (additionalProperties:false).
        self.assertTrue(_errors(MODULE_SCHEMA, {**VALID_MODULE,
                        "wires": [{"type": "mcp", "name": "engine-x", "definition": {}, "extra": 1}]}),
                        "an unexpected key on a wire must be rejected")

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

    def test_malformed_wire_is_flagged_via_the_rule(self):
        # The end-to-end CI path for defect (a): a manifest whose wire is missing its seam's
        # required fields ({type:mcp} with no definition) fails the hard module-manifest gate.
        rule = self._rule("module-manifest.json")
        with tempfile.TemporaryDirectory() as d:
            bad = _write(d, "manifest.json", {**VALID_MODULE, "wires": [{"type": "mcp"}]})
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


class TestWiringFindings(unittest.TestCase):
    """The pure forward wiring leg: a not-applied directive flags HARD (uniform across all five
    seams — mcp too, because the carve-out is approval-blindness, not a soft tier), an applied one
    does not. The applied flag is supplied by the caller, so the leg is pure (no filesystem here)."""

    def test_applied_directives_produce_no_finding(self):
        declared = [("core", "mcp", ".mcp.json", True), ("core", "gitignore", ".gitignore", True)]
        self.assertEqual(validate.wiring_findings(declared, "hard", "m"), [])

    def test_not_applied_is_a_hard_finding_uniformly(self):
        for seam in ("hook", "mcp", "ontology-entry", "permission", "gitignore"):
            found = validate.wiring_findings([("core", seam, ".target", False)], "hard", "fix it")
            self.assertEqual(len(found), 1, f"a not-applied {seam} wire must flag")
            self.assertEqual(found[0]["severity"], "hard",
                             f"{seam} flags HARD (mcp too — approval-blind, not a soft tier)")
            self.assertIn(seam, found[0]["message"])
            self.assertIn("not applied", found[0]["message"])


class TestOrphanWireFindings(unittest.TestCase):
    """The pure REVERSE wiring leg (orphan-wire, slice 25b): an applied engine entry no manifest
    declares flags HARD; a declared one does not; a drifted same-identity entry is NOT double-flagged
    (the forward leg owns drift). permission and ontology-entry are excluded by construction —
    declared_wire_identity returns None and the enumerator never emits them."""

    def test_undeclared_applied_entry_is_flagged_per_shared_seam(self):
        for seam, key in (("hook", ("PreToolUse", "", "command", ".engine/x.py")),
                          ("mcp", "engine-x"), ("gitignore", "x-cache")):
            found = validate.orphan_wire_findings([(seam, key, ".target")], set(), "hard", "fix it")
            self.assertEqual(len(found), 1, f"an undeclared applied {seam} must flag")
            self.assertEqual(found[0]["severity"], "hard")
            self.assertIn(seam, found[0]["message"])
            self.assertIn("no installed module declares", found[0]["message"])

    def test_declared_applied_entry_is_not_flagged(self):
        applied = [("mcp", "engine-x", ".mcp.json")]
        self.assertEqual(validate.orphan_wire_findings(applied, {("mcp", "engine-x")}, "hard", "m"), [])

    def test_drifted_name_identity_entry_is_not_an_orphan(self):
        # mcp/gitignore identity is the name/key (not content), so a content-drifted entry keeps its
        # identity, still matches a declared directive, and is reported ONCE by the forward leg — not
        # double-flagged here.
        applied = [("mcp", "engine-knowledge-graph", ".mcp.json")]
        self.assertEqual(
            validate.orphan_wire_findings(applied, {("mcp", "engine-knowledge-graph")}, "hard", "m"), [])

    def test_hook_with_a_changed_command_is_an_orphan_by_the_identity_model(self):
        # A hook's identity is the full (event, matcher, type, command) tuple (module-system §"The wiring
        # library"), so an applied engine hook whose command differs from every declared one IS an orphan
        # here — while the declared hook is separately reported not-applied by the forward leg. Two
        # accurate findings about two real facts, by design (documented on orphan_wire_findings).
        applied = [("hook", ("Stop", "", "command", ".engine/EDITED.py"), ".claude/settings.json")]
        declared = {("hook", ("Stop", "", "command", ".engine/orig.py"))}
        found = validate.orphan_wire_findings(applied, declared, "hard", "m")
        self.assertEqual(len(found), 1)
        self.assertIn("hook", found[0]["message"])

    def test_declared_wire_identity_is_single_homed_and_excludes_permission_and_ontology(self):
        self.assertIsNone(wiring.declared_wire_identity({"type": "permission", "value": "Bash(x)"}))
        self.assertIsNone(wiring.declared_wire_identity({"type": "ontology-entry", "name": "x"}))
        self.assertEqual(wiring.declared_wire_identity({"type": "mcp", "name": "engine-x"}),
                         ("mcp", "engine-x"))
        self.assertEqual(wiring.declared_wire_identity(
            {"type": "hook", "event": "Stop", "matcher": "",
             "hook": {"type": "command", "command": ".engine/c"}}),
            ("hook", ("Stop", "", "command", ".engine/c")))


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

    def test_check_corpus_split_core_two_guards_validators_core_thirtythree(self):
        # The locked engine/corpus boundary (D-089/D-090; validators-core README; validation README):
        # core ships the validation engine and owns ZERO rules EXCEPT the two §15 frozen-named guards;
        # the self-validation corpus is validators-core's (33 rules: the 31 prior plus the two
        # audit-digest gates (the audit-library module's seal/fingerprint gate and its staleness signal,
        # validated HERE — the detection-relay shape, audit-library owns the digest machinery while
        # validators-core owns the rules that verify it, exactly as the first-run reference-closure gate
        # enforces provisioning's invariant from here), and before them the audit
        # checklist schema gate (the audit-library module's concern-list, validated HERE because
        # engine-self-validation consolidates in validators-core, not the surface's owner — README
        # "Why a separate required package"), and before that the first-run
        # reference-closure gate (issue #150; engine-planning D-219/D-220), and before it the
        # knowledge-vocabulary parity guard (issue #131) — the 20 after the
        # operation grammar (slice OG) and the skill grammar (slice SG) plus the doc-frontmatter and
        # doc-shape grammar rules (slice 19) and the uv-group-drift gate (slice 25c) and the
        # skill-coherence self-election guard (slice 26a) and the policy-override-stale rule
        # (slice 26c) — plus the conduct-frontmatter, conduct-shape, and conduct-weakening-guard rules
        # (slice CD), the grammar plus the soft §15 guard for the new codes-of-conduct surface).
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
            ".engine/check/agent-frontmatter.json",
            ".engine/check/agent-shape.json",
            ".engine/check/audit-concern-list.json",
            ".engine/check/audit-digest-fingerprint.json",
            ".engine/check/audit-digest-staleness.json",
            ".engine/check/catalog-coverage.json",
            ".engine/check/conduct-frontmatter.json",
            ".engine/check/conduct-shape.json",
            ".engine/check/conduct-weakening-guard.json",
            ".engine/check/contract-frontmatter.json",
            ".engine/check/contract-shape.json",
            ".engine/check/contract-threshold.json",
            ".engine/check/doc-frontmatter.json",
            ".engine/check/doc-shape.json",
            ".engine/check/engine-manifest.json",
            ".engine/check/first-run-reference-closure.json",
            ".engine/check/interface-declaration.json",
            ".engine/check/knowledge-coverage.json",
            ".engine/check/knowledge-vocabulary.json",
            ".engine/check/link-integrity.json",
            ".engine/check/module-manifest.json",
            ".engine/check/operation-frontmatter.json",
            ".engine/check/operation-shape.json",
            ".engine/check/policy-frontmatter.json",
            ".engine/check/policy-override-stale.json",
            ".engine/check/policy-shape.json",
            ".engine/check/pr-body-completeness.json",
            ".engine/check/self-map-drift.json",
            ".engine/check/skill-coherence.json",
            ".engine/check/skill-frontmatter.json",
            ".engine/check/skill-shape.json",
            ".engine/check/state-cursor.json",
            ".engine/check/uv-group-drift.json",
        ], "validators-core owns exactly the 33 corpus rules")
        # the split partitions ALL committed check files — nothing left unclaimed
        all_checks = sorted(r for r in module_coherence.engine_file_inventory()
                            if r.startswith(".engine/check/") and r.endswith(".json"))
        self.assertEqual(sorted(core_checks + vc_checks), all_checks,
                         "every .engine/check/*.json is claimed by exactly one of core / validators-core")
        # validators-core depends on core (presence assertion, any version)
        vc = next(m for _p, m in manifests if m.get("id") == "validators-core")
        self.assertEqual(vc.get("depends"), {"core": ""})

    def test_audit_library_owns_persona_and_concern_list(self):
        # audit-library (required, L3) ships the static self-audit artifacts: the audit persona and the
        # seeded concern-list (audits-owned data). It owns NO check or schema this slice — the concern-list
        # check is validators-core's (engine-self-validation consolidates there) and the schema rides core's
        # schema glob — so its provides is exactly persona + concern-list, and it depends on core +
        # validators-core (the semantic audit assumes the mechanical floor).
        manifests = module_coherence.discover_manifests()
        al = next((m for _p, m in manifests if m.get("id") == "audit-library"), None)
        self.assertIsNotNone(al, "audit-library must be a present module")
        self.assertEqual(al.get("status"), "required")
        self.assertEqual(al.get("wires"), [])
        self.assertEqual(al.get("depends"), {"core": "", "validators-core": ""})
        self.assertEqual(al.get("provides"), {
            "agent": [".claude/agents/audit.md"],
            "audits": [".engine/audits/concern-list.json"],
        }, "audit-library owns exactly the persona and the seeded concern-list")

    def test_seed_concern_list_conforms_to_its_schema(self):
        # the committed seed concern-list is well-formed against concern-list.v1 — the same schema + dialect
        # the audit-concern-list check validates it with at the merge. Pins the seed (every entry carries its
        # required justification) and that the schema is itself usable.
        from jsonschema import Draft202012Validator
        schema = validate.load_json(os.path.join(validate.ROOT, ".engine/schemas/concern-list.v1.json"))
        data = validate.load_json(os.path.join(validate.ROOT, ".engine/audits/concern-list.json"))
        errors = [e.message for e in Draft202012Validator(schema).iter_errors(data)]
        self.assertEqual(errors, [], f"seed concern-list must conform: {errors}")
        self.assertTrue(data.get("concerns"), "the seed ships with concerns")
        for c in data["concerns"]:
            self.assertTrue(c.get("justification"), "every seed concern carries its justification")

    def test_real_repository_is_coherent(self):
        # The whole point of the slice: the committed tree has no unowned engine file and no
        # broken dependency. This is the green baseline the operator's fail-then-pass demo
        # perturbs (add a stray file -> orphan; add a ghost dependency -> unsatisfied).
        findings = module_coherence.check_coherence()
        hard = [f for f in findings if f["severity"] == "hard"]
        self.assertEqual(hard, [], f"unexpected coherence findings: {[f['message'] for f in hard]}")

    def test_real_repository_is_wiring_coherent_and_approval_blind(self):
        # The committed tree's declared wires are ALL applied -> the forward wiring leg is silent.
        # The mcp leg is APPROVAL-BLIND: it reports the engine-knowledge-graph server applied from the
        # committed .mcp.json DEFINITION, never from the operator's runtime client-approval (which the
        # repo does not record) -> the MCP-pending-setup carve-out, shown positively. (Slice 20 BORNed
        # .claude/settings.json for the SessionStart hook wiring; that is the engine's own hook config,
        # not an operator MCP approval, so it is irrelevant to approval-blindness — the point is that the
        # mcp wire is green on the committed definition alone.)
        status = module_coherence.wiring_status(module_coherence.discover_manifests())
        mcp = [s for s in status if s[1] == "mcp"]
        self.assertTrue(mcp, "core declares an mcp wire to exercise")
        self.assertTrue(all(applied for _id, _seam, _t, applied in mcp),
                        "the mcp wire is applied from the committed .mcp.json definition (approval-blind)")
        self.assertTrue(all(applied for _id, _seam, _t, applied in status),
                        f"every committed wire must be applied: {status}")
        self.assertEqual(validate.wiring_findings(status, "hard", "m"), [])

    def test_unapplied_declared_wires_are_hard_findings(self):
        # Redirect the wiring library's shared targets to empty temp files -> the wires core declares
        # are no longer applied -> check_coherence reports them as HARD drift (the half-applied/stale
        # catcher). Dependency + ownership stay green (they don't read the wiring targets).
        saved = (wiring.GITIGNORE_PATH, wiring.MCP_PATH, wiring.CATALOG_PATH, wiring.SETTINGS_PATH)
        with tempfile.TemporaryDirectory() as d:
            wiring.GITIGNORE_PATH = os.path.join(d, ".gitignore")
            wiring.MCP_PATH = os.path.join(d, ".mcp.json")
            wiring.CATALOG_PATH = os.path.join(d, "surface-catalog.json")
            wiring.SETTINGS_PATH = os.path.join(d, "settings.json")
            try:
                findings = module_coherence.check_coherence()
            finally:
                (wiring.GITIGNORE_PATH, wiring.MCP_PATH, wiring.CATALOG_PATH,
                 wiring.SETTINGS_PATH) = saved
        msgs = " ".join(f["message"] for f in findings if f["severity"] == "hard")
        self.assertIn("mcp wire that is not applied", msgs)
        self.assertIn("gitignore wire that is not applied", msgs)

    def test_drifted_mcp_definition_is_a_hard_wiring_finding(self):
        # Binds leg (c) to fix (b): a committed .mcp.json whose engine-knowledge-graph definition has
        # DRIFTED from the manifest -> the full-content is_applied reads it not-applied -> a HARD
        # wiring finding. (Name-only is_applied would have stayed green here.) Only MCP_PATH is
        # redirected, so the gitignore wire stays applied and silent.
        saved = wiring.MCP_PATH
        with tempfile.TemporaryDirectory() as d:
            wiring.MCP_PATH = os.path.join(d, ".mcp.json")
            with open(wiring.MCP_PATH, "w", encoding="utf-8") as fh:
                json.dump({"mcpServers": {"engine-knowledge-graph":
                                          {"command": "DRIFTED", "args": []}}}, fh)
            try:
                findings = module_coherence.check_coherence()
            finally:
                wiring.MCP_PATH = saved
        hard = [f for f in findings if f["severity"] == "hard"]
        self.assertTrue(any("mcp wire that is not applied" in f["message"] for f in hard),
                        "a drifted mcp definition must flag a hard wiring finding")

    def test_real_repository_has_no_orphan_wires(self):
        # The slice-25b REVERSE leg adds zero findings over the committed tree: every applied
        # engine-identified hook / mcp / gitignore entry is declared by a present manifest, and the
        # foundation .venv ignore is a plain line (not a fence) so it is never enumerated (D-156).
        manifests = module_coherence.discover_manifests()
        orphans = validate.orphan_wire_findings(
            wiring.applied_engine_wires(), module_coherence.declared_wire_identities(manifests),
            "hard", "m")
        self.assertEqual(orphans, [], f"unexpected orphan wires: {[f['message'] for f in orphans]}")

    def test_permission_and_venv_plain_line_are_never_enumerated(self):
        applied = wiring.applied_engine_wires()
        self.assertNotIn("permission", {seam for seam, _k, _t in applied})  # not engine-identifiable
        fences = {key for seam, key, _t in applied if seam == "gitignore"}
        self.assertNotIn(".engine/.venv/", fences)         # the plain line is not a fence id

    def test_injected_undeclared_engine_hook_is_an_orphan_wire(self):
        # Redirect only SETTINGS_PATH to a faithful COPY + an engine hook NO manifest declares -> the
        # reverse leg flags it; the other shared files stay real (and fully declared), adding nothing.
        saved = wiring.SETTINGS_PATH
        with tempfile.TemporaryDirectory() as d:
            copy = os.path.join(d, "settings.json")
            shutil.copyfile(saved, copy)
            wiring.SETTINGS_PATH = copy
            try:
                wiring.apply({"type": "hook", "event": "PostToolUse", "matcher": "",
                              "hook": {"type": "command",
                                       "command": ".engine/.venv/bin/python .engine/tools/boot.py --x"}})
                findings = module_coherence.check_coherence()
            finally:
                wiring.SETTINGS_PATH = saved
        hard = [f for f in findings if f["severity"] == "hard"]
        self.assertTrue(any("hook" in f["message"] and "no installed module declares" in f["message"]
                            for f in hard),
                        "an applied engine hook no manifest declares must be an orphan-wire finding")

    def test_injected_orphan_gitignore_fence_is_flagged(self):
        saved = wiring.GITIGNORE_PATH
        with tempfile.TemporaryDirectory() as d:
            copy = os.path.join(d, ".gitignore")
            shutil.copyfile(saved, copy)
            wiring.GITIGNORE_PATH = copy
            try:
                wiring.apply({"type": "gitignore", "key": "ghost-cache",
                              "lines": [".engine/ghost/.cache/"]})
                findings = module_coherence.check_coherence()
            finally:
                wiring.GITIGNORE_PATH = saved
        hard = [f for f in findings if f["severity"] == "hard"]
        self.assertTrue(any("gitignore" in f["message"] and "no installed module declares" in f["message"]
                            for f in hard),
                        "an undeclared engine-managed fence must be an orphan-wire finding")

    def test_main_exit_zero_on_clean_tree(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):  # main() prints its operator report; keep tests quiet
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


class TestSeededRootFileOwnership(unittest.TestCase):
    """The seeded root SECURITY.md is operator-owned product territory, and its template seed is operator config.
    (Re-homed from the retired test_security_seed.py — these assert permanent module_coherence constants that
    govern the upgrade overlay, so they must survive first-run retirement; issue #150.)"""
    def test_seeded_root_security_md_is_not_engine_owned(self):
        # product territory, in no `provides`, NO carve-out: it must NOT be a FOUNDATION_INFRA member
        # (that set is overlay-REPLACED on upgrade, which would clobber the operator's edited disclosure).
        self.assertNotIn("SECURITY.md", module_coherence.FOUNDATION_INFRA)

    def test_security_seed_source_is_carved_out_in_operator_config(self):
        self.assertIn(".engine/provisioning/security-seed.md", module_coherence.OPERATOR_CONFIG)


class TestTopologicalOrder(unittest.TestCase):
    """The pure validate.topological_order — dependency-first, deterministic, input-order-independent,
    cycle-safe. The self-map (slice 8) renders in this order; the module manager (slice 25) installs
    in it."""

    @staticmethod
    def _m(mid, *deps):
        return {"id": mid, "depends": {d: "" for d in deps}}  # empty-string range, as on disk

    def _ids(self, manifests):
        return [m["id"] for m in validate.topological_order(manifests)]

    def test_linear_chain_orders_dependencies_first(self):
        # a depends-on b depends-on c  ->  c, b, a
        ms = [self._m("a", "b"), self._m("b", "c"), self._m("c")]
        self.assertEqual(self._ids(ms), ["c", "b", "a"])

    def test_topological_differs_from_alphabetical(self):
        # alpha depends-on zebra -> [zebra, alpha], the REVERSE of alphabetical [alpha, zebra]
        self.assertEqual(self._ids([self._m("alpha", "zebra"), self._m("zebra")]),
                         ["zebra", "alpha"])

    def test_independent_roots_break_ties_alphabetically(self):
        self.assertEqual(self._ids([self._m("c"), self._m("a"), self._m("b")]), ["a", "b", "c"])

    def test_diamond_orders_base_first_dependents_last(self):
        # d depends-on b,c ; b,c depend-on a  ->  a, b, c, d
        ms = [self._m("d", "b", "c"), self._m("b", "a"), self._m("c", "a"), self._m("a")]
        self.assertEqual(self._ids(ms), ["a", "b", "c", "d"])

    def test_input_order_independent(self):
        import itertools
        ms = [self._m("d", "b", "c"), self._m("b", "a"), self._m("c", "a"), self._m("a")]
        outs = {tuple(self._ids(list(p))) for p in itertools.permutations(ms)}
        self.assertEqual(len(outs), 1, "topological order must not depend on input sequence")

    def test_cycle_is_deterministic_and_lossless(self):
        # a<->b cycle (separately flagged hard by coherence_findings): no crash, all ids present,
        # deterministic (alphabetical fallback), independent of input order.
        ms = [self._m("b", "a"), self._m("a", "b")]
        out = self._ids(ms)
        self.assertEqual(sorted(out), ["a", "b"])
        self.assertEqual(out, self._ids(list(reversed(ms))))

    def test_absent_dependency_is_ignored(self):
        # a depends-on a module not in the set -> treated as a root, no crash (mirrors _dependency_cycle)
        self.assertEqual(self._ids([self._m("x", "not-present")]), ["x"])


if __name__ == "__main__":
    unittest.main()
