#!/usr/bin/env python3
"""Self-tests for slice 10 — the knowledge graph: the knowledge.v1 schema, the generic
catalog-driven generator, the committed graph, and the coverage/fingerprint relay gate.

Run: uv run --directory .engine -- python -m unittest discover -s tools -p 'test_*.py'

These lock the load-bearing teeth: the pure derivation is deterministic and produces well-shaped,
schema-conforming, referentially-intact entities (every predicate target resolves to an entity, ids
are unique, id == type:slug); coverage is TOTAL (every owned engine file under a catalogued surface
has an entity) and the derived-observational graph.json is NOT itself an entity (no recursion); the
conformance strips hold (no debt/finding/session types, no hand-authored predicates); generate->check
round-trips and a hand-edit/absence is REFUSED as a hard finding; the committed graph is in sync with
the live sources (so a forgotten regen fails this suite, not only CI); and the committed
`coverage`/`mode:fingerprint` rule, via validate.kind_coverage, passes the real graph, bites on a
stale/missing graph, fails closed on a broken generator, and the unknown-mode tail is intact.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import knowledge_gen     # noqa: E402
import hooks             # noqa: E402  (slice 23: the run_hook harness the commit-boundary regen rides)

KNOWLEDGE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "knowledge.v1.json"))
RULE_PATH = os.path.join(validate.CHECK_DIR, "knowledge-coverage.json")
ID_RE = r"^(contract|policy|conduct|schema|check|tool|operation|skill|agent|interface|doc|state|module):[A-Za-z0-9._-]+$"


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


def _live_entities():
    return knowledge_gen.derive_entities(*knowledge_gen.load_sources())


class TestHelpers(unittest.TestCase):
    def test_slug_is_the_filename_stem(self):
        self.assertEqual(knowledge_gen._slug(".engine/check/state-cursor.json"), "state-cursor")
        self.assertEqual(knowledge_gen._slug(".engine/schemas/check.v1.json"), "check.v1")
        self.assertEqual(knowledge_gen._slug(".engine/tools/validate.py"), "validate")

    def test_surface_for_longest_prefix(self):
        surfaces = {"check": {"location": ".engine/check/"}, "schema": {"location": ".engine/schemas/"}}
        self.assertEqual(knowledge_gen._surface_for(".engine/check/x.json", surfaces), "check")
        self.assertEqual(knowledge_gen._surface_for(".engine/schemas/x.json", surfaces), "schema")
        self.assertIsNone(knowledge_gen._surface_for(".engine/engine.json", surfaces))
        self.assertIsNone(knowledge_gen._surface_for(".engine/knowledge/graph.json", surfaces))


class TestSchema(unittest.TestCase):
    def test_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(KNOWLEDGE_SCHEMA)


class TestLiveDerivation(unittest.TestCase):
    """derive_entities over the real catalog/inventory/manifests/claims."""

    def setUp(self):
        self.entities = _live_entities()
        self.by_id = {e["id"]: e for e in self.entities}

    def test_canonical_graph_conforms_to_its_schema(self):
        graph = json.loads(knowledge_gen.canonical_graph())
        self.assertEqual(_errors(KNOWLEDGE_SCHEMA, graph), [])

    def test_ids_are_unique(self):
        ids = [e["id"] for e in self.entities]
        self.assertEqual(len(ids), len(set(ids)), "entity ids must be unique")

    def test_id_equals_type_colon_slug(self):
        for e in self.entities:
            self.assertEqual(e["id"], f'{e["type"]}:{e["slug"]}', e["id"])

    def test_referential_integrity_every_edge_target_resolves(self):
        """The gate cannot catch a consistently-wrong edge (committed == re-derived); this can."""
        ids = set(self.by_id)
        for e in self.entities:
            for kind, targets in (e.get("predicates") or {}).items():
                for t in targets:
                    self.assertIn(t, ids, f"{e['id']} {kind} -> dangling target {t}")

    def test_total_coverage_every_owned_surface_file_has_an_entity(self):
        catalog, manifests, inventory, claims = knowledge_gen.load_sources()
        surfaces = catalog.get("surfaces", {})
        expected = {rel for rel in inventory
                    if knowledge_gen._surface_for(rel, surfaces) and claims.get(rel)}
        got = {e["source"]["path"] for e in self.entities if e["type"] != "module"}
        self.assertEqual(got, expected)

    def test_source_fingerprints_are_sha256(self):
        import re
        for e in self.entities:
            self.assertRegex(e["source"]["fingerprint"], r"^sha256:[0-9a-f]{64}$", e["id"])

    def test_graph_json_is_not_itself_an_entity(self):
        for e in self.entities:
            self.assertNotEqual(e["source"]["path"], ".engine/knowledge/graph.json")

    def test_no_stripped_entity_types_or_predicates(self):
        for e in self.entities:
            self.assertNotIn(e["type"], {"integration-debt", "audit-finding", "session-claim", "feature"})
            self.assertNotIn("pushback_drawers", e.get("predicates") or {})

    def test_known_edges_for_the_state_cursor_check(self):
        # the committed state-cursor check is governed by check.v1 and owned by validators-core (one of
        # the corpus rules). Its target — the state cursor — is a FOUNDATION, not a catalogued surface
        # (issue #24), so the cursor is not a knowledge entity and the check carries no `targets` edge.
        chk = self.by_id.get("check:state-cursor")
        self.assertIsNotNone(chk, "expected a check:state-cursor entity")
        self.assertEqual(chk["predicates"].get("governed_by"), ["schema:check.v1"])
        self.assertEqual(chk["predicates"].get("provided_by"), ["module:validators-core"])
        self.assertNotIn("targets", chk["predicates"])  # the cursor is a foundation, not a surface entity
        # no state:state surface entity exists (state left the catalog); its schema state.v1.json
        # remains a schema-surface entity, governed by the external dialect (no governed_by edge)
        self.assertNotIn("state:state", self.by_id)
        self.assertIn("schema:state.v1", self.by_id)
        # both module entities exist (validators-core was stood up alongside core)
        self.assertIn("module:core", self.by_id)
        self.assertIn("module:validators-core", self.by_id)

    def test_catalog_coverage_rule_and_provisioned_surface_homes(self):
        # issue #30: the catalog-coverage gate is a validators-core corpus rule; the provisioned .engine/
        # surface homes appear as core-owned entities. The `doc` surface gained its grammar at slice 19
        # (governing_schema null -> doc.v1) and landed its first instance, so its .gitkeep was removed and
        # the doc home is now the governed orientation instance; the `operation` surface gained its grammar
        # at slice OG, so operation:.gitkeep is now governed by schema:operation.v1.
        cov = self.by_id.get("check:catalog-coverage")
        self.assertIsNotNone(cov, "expected a check:catalog-coverage entity")
        self.assertEqual(cov["predicates"].get("provided_by"), ["module:validators-core"])
        # the doc grammar flip (slice 19): the placeholder doc:.gitkeep is gone (the first operator doc
        # landed), and the doc home is now the core-provided orientation instance, governed by doc.v1.
        self.assertNotIn("doc:.gitkeep", self.by_id)
        doc_home = self.by_id.get("doc:getting-started")
        self.assertIsNotNone(doc_home, "expected a doc:getting-started entity")
        self.assertEqual(doc_home["predicates"].get("provided_by"), ["module:core"])
        self.assertEqual(doc_home["predicates"].get("governed_by"), ["schema:doc.v1"])
        # the operation surface (slice 20 landed the first instance, boot's SessionStart pack; slice 21
        # added the modes operation; slice 22 adds close's turn-close operation): the placeholder
        # operation:.gitkeep is gone, and every core-provided lifecycle operation is governed by operation.v1.
        self.assertNotIn("operation:.gitkeep", self.by_id)
        for op_id in ("operation:boot-session-start", "operation:operating-modes", "operation:close-turn"):
            op_home = self.by_id.get(op_id)
            self.assertIsNotNone(op_home, f"expected an {op_id} entity")
            self.assertEqual(op_home["predicates"].get("provided_by"), ["module:core"], op_id)
            self.assertEqual(op_home["predicates"].get("governed_by"), ["schema:operation.v1"], op_id)

    def test_known_edges_for_the_interfaces_and_their_declaration_check(self):
        # slices 11a/11b: each interface declaration is governed by interface.v1 (the catalog
        # governing_schema flip) and provided by core; the interface-declaration check targets BOTH.
        # The non-fingerprint correlate for edge correctness — the fingerprint gate proves the graph
        # MATCHES the surfaces, never that the derived edges are right; this asserts the edges.
        for iid in ("interface:knowledge-retrieval", "interface:search"):
            iface = self.by_id.get(iid)
            self.assertIsNotNone(iface, f"expected an {iid} entity")
            self.assertEqual(iface["predicates"].get("governed_by"), ["schema:interface.v1"], iid)
            self.assertEqual(iface["predicates"].get("provided_by"), ["module:core"], iid)
        self.assertIn("schema:interface.v1", self.by_id)
        chk = self.by_id.get("check:interface-declaration")
        self.assertIsNotNone(chk, "expected a check:interface-declaration entity")
        # the check globs .engine/interfaces/*.json, so its derived `targets` are BOTH declarations
        # (sorted) — adding search.json widened this edge, the 11b graph delta the operator eyeballs.
        self.assertEqual(chk["predicates"].get("targets"),
                         ["interface:knowledge-retrieval", "interface:search"])
        self.assertEqual(chk["predicates"].get("governed_by"), ["schema:check.v1"])

    def test_schema_surface_files_have_no_governed_by(self):
        """A schema file's governing authority is the external 2020-12 dialect (a URI), not an in-repo
        schema entity, so it carries no governed_by edge — by design, not a gap."""
        for e in self.entities:
            if e["type"] == "schema":
                self.assertNotIn("governed_by", e.get("predicates") or {}, e["id"])

    def test_catalog_data_files_are_schema_entities(self):
        """A deliberate, pinned choice: the catalog data file and its meta-schema live under the
        schema surface location, so they appear as schema entities (mechanical/total coverage),
        not silently excluded."""
        self.assertIn("schema:surface-catalog", self.by_id)
        self.assertIn("schema:surface-catalog.schema", self.by_id)

    def test_expected_entities_are_present(self):
        """Concrete spot-checks, independent of the _surface_for oracle the total-coverage test
        reuses — so a classification bug cannot pass both tests."""
        for eid in ("tool:validate", "tool:knowledge_gen", "tool:modes", "tool:close", "schema:check.v1",
                    "schema:knowledge.v1",
                    "check:knowledge-coverage", "check:catalog-coverage", "module:core"):
            self.assertIn(eid, self.by_id, eid)


class TestAttributeHarvesters(unittest.TestCase):
    """The D-203 declared-attribute harvesters — pure (IO-free), tested directly on parsed dicts so the
    'declared, not interpreted / structure, not belief' gates are locked independently of any source tree."""

    def test_status_is_modules_and_contracts_only_else_active(self):
        kg = knowledge_gen
        self.assertEqual(kg._status_for("module", {}, {"status": "required"}), "required")
        self.assertEqual(kg._status_for("module", {}, {"status": "experimental"}), "experimental")
        self.assertEqual(kg._status_for("contract", {"status": "superseded"}, None), "superseded")
        # every OTHER surface is 'active' (D-203 'else active'), even one that declares a status of its own
        for st in ("policy", "operation", "doc", "conduct", "interface", "check", "schema", "tool"):
            self.assertEqual(kg._status_for(st, {"status": "deprecated"}, None), "active", st)
        # a missing status on the two declaring surfaces degrades to 'active' (never a crash)
        self.assertEqual(kg._status_for("contract", {}, None), "active")   # the .gitkeep trap
        self.assertEqual(kg._status_for("module", {}, {}), "active")

    def test_tier_is_checks_only(self):
        kg = knowledge_gen
        self.assertEqual(kg._tier_for("check", {"tier": "hard"}), "hard")
        self.assertEqual(kg._tier_for("check", {"tier": "soft"}), "soft")
        self.assertIsNone(kg._tier_for("check", {}))                    # malformed check -> None
        self.assertIsNone(kg._tier_for("check", {"tier": "posture"}))   # not a check bite tier
        self.assertIsNone(kg._tier_for("policy", {"tier": "hard"}))     # never from a policy

    def test_title_identity_only_no_slug_fallback(self):
        kg = knowledge_gen
        self.assertEqual(kg._title_for("policy", {"title": "Contract threshold"}), "Contract threshold")
        self.assertEqual(kg._title_for("interface", {"title": "Memory recall"}), "Memory recall")
        self.assertEqual(kg._title_for("skill", {"name": "engine-start"}), "engine-start")
        # excluded surfaces never get a title even when they declare one (purpose/decision clauses)
        self.assertIsNone(kg._title_for("operation", {"title": "Boot the session"}))
        self.assertIsNone(kg._title_for("doc", {"title": "Getting started"}))
        self.assertIsNone(kg._title_for("contract", {"title": "A decision"}))
        # absent / empty -> omit (NO slug fallback)
        self.assertIsNone(kg._title_for("policy", {}))
        self.assertIsNone(kg._title_for("policy", {"title": "   "}))

    def test_title_shape_guard_rejects_purpose_clauses_and_imperatives(self):
        kg = knowledge_gen
        for bad in ("Operating modes — the session stance", "Knowledge graph - retrieval",
                    "Start building now", "Set up your project", "Do this. Then that",
                    "Scope: the worker role", "Shape how I work with you"):
            self.assertIsNone(kg._title_for("policy", {"title": bad}), bad)
        for ok in ("Attention", "Contract threshold", "Knowledge graph retrieval", "Finding disposition"):
            self.assertEqual(kg._title_for("policy", {"title": ok}), ok)

    def test_discriminators_per_surface_with_sorted_lists(self):
        kg = knowledge_gen
        self.assertEqual(kg._discriminators_for("check", {}, {"kind": "shape", "suites": ["pr", "CI"]}, None),
                         {"kind": "shape", "suites": ["CI", "pr"]})            # suites sorted
        self.assertEqual(kg._discriminators_for("interface", {},
                         {"operations": [{"name": "neighbors"}, {"name": "find"}],
                          "fallback": {"handle": "engine-x"}}, None),
                         {"operations": ["find", "neighbors"], "fallback": "engine-x"})  # op names sorted
        self.assertEqual(kg._discriminators_for("agent", {"role": "worker", "model-tier": "judgment"}, {}, None),
                         {"role": "worker", "model-tier": "judgment"})
        self.assertEqual(kg._discriminators_for("skill", {"invocation": "operator-typed"}, {}, None),
                         {"invocation": "operator-typed"})
        self.assertEqual(kg._discriminators_for("module", {}, {}, {"version": "1.4.0"}), {"version": "1.4.0"})
        self.assertEqual(kg._discriminators_for("policy", {"title": "x"}, {}, None), {})  # none for a policy


class TestSupersedesEdges(unittest.TestCase):
    """The supersedes edge guard (D-203 / the D-169 canon invariant): contract->contract, DEPLOYMENT-STREAM
    only — no edge may EVER reach a canon eADR. Canon-ness is provides-membership (modelled as canon_ids)."""

    @staticmethod
    def _contract(eid):
        return {"id": eid, "type": "contract", "name": eid, "slug": eid.split(":", 1)[1],
                "source": {"path": f"x/{eid}.md", "fingerprint": "sha256:" + "0" * 64},
                "owner": "core", "predicates": {}}

    def _pair(self):
        a, b = self._contract("contract:eADR-0002"), self._contract("contract:eADR-0001")
        fm = {"contract:eADR-0002": {"id": "eADR-0002", "supersedes": "eADR-0001"},
              "contract:eADR-0001": {"id": "eADR-0001"}}
        return [a, b], fm

    def test_deployment_to_deployment_emits_the_edge(self):
        ents, fm = self._pair()
        self.assertEqual(knowledge_gen._supersedes_edges(ents, fm, canon_ids=set()),
                         {"contract:eADR-0002": ["contract:eADR-0001"]})

    def test_a_canon_target_is_never_reached(self):
        ents, fm = self._pair()
        self.assertEqual(knowledge_gen._supersedes_edges(ents, fm, canon_ids={"contract:eADR-0001"}), {})

    def test_a_canon_source_emits_nothing(self):
        ents, fm = self._pair()
        self.assertEqual(knowledge_gen._supersedes_edges(
            ents, fm, canon_ids={"contract:eADR-0001", "contract:eADR-0002"}), {})

    def test_dangling_or_self_target_emits_nothing(self):
        a = self._contract("contract:eADR-0002")
        self.assertEqual(knowledge_gen._supersedes_edges(
            [a], {"contract:eADR-0002": {"id": "eADR-0002", "supersedes": "eADR-9999"}}, set()), {})
        self.assertEqual(knowledge_gen._supersedes_edges(
            [a], {"contract:eADR-0002": {"id": "eADR-0002", "supersedes": "eADR-0002"}}, set()), {})


class TestLiveDerivationAttributes(unittest.TestCase):
    """The D-203 attributes on the REAL derived graph — the non-fingerprint correlate that the harvest is
    RIGHT (the gate proves the committed graph MATCHES the sources, never that the values are correct)."""

    def setUp(self):
        self.by_id = {e["id"]: e for e in _live_entities()}

    def test_check_carries_tier_kind_suites_status_and_no_title(self):
        c = self.by_id["check:catalog-coverage"]
        self.assertEqual((c.get("tier"), c.get("kind"), c.get("suites"), c.get("status")),
                         ("hard", "coverage", ["CI"], "active"))
        self.assertNotIn("title", c)
        self.assertEqual(self.by_id["check:conduct-weakening-guard"].get("tier"), "soft")

    def test_policy_and_interface_carry_title_and_discriminators(self):
        self.assertEqual(self.by_id["policy:attention"].get("title"), "Attention")
        i = self.by_id["interface:knowledge-retrieval"]
        self.assertEqual(i.get("title"), "Knowledge graph retrieval")
        self.assertEqual(i.get("operations"), ["find", "get-entity", "neighbors", "relate"])
        self.assertEqual(i.get("fallback"), "engine-knowledge-graph")

    def test_module_carries_status_and_version(self):
        m = self.by_id["module:core"]
        self.assertEqual((m.get("status"), m.get("version")), ("required", "0.0.0-dev"))

    def test_every_entity_has_status_and_tier_and_title_are_well_scoped(self):
        for e in self.by_id.values():
            self.assertIn("status", e, e["id"])
            if "tier" in e:
                self.assertEqual(e["type"], "check", e["id"])
            if "title" in e:
                self.assertIn(e["type"], ("policy", "interface"), e["id"])  # skills/agents not entitized here

    def test_supersedes_is_dormant_and_gitkeep_trap_holds(self):
        self.assertFalse(any("supersedes" in e["predicates"] for e in self.by_id.values()),
                         "supersedes is provably dormant in v1 (every contract entity is owned == canon)")
        self.assertEqual([e["id"] for e in self.by_id.values() if e["type"] in ("skill", "agent")], [])
        gk = self.by_id["contract:.gitkeep"]
        self.assertEqual(gk.get("status"), "active")
        self.assertNotIn("title", gk)
        self.assertNotIn("supersedes", gk["predicates"])


class TestCommittedGraph(unittest.TestCase):
    """The committed .engine/knowledge/graph.json — conforms to its schema and is in sync with the
    live sources (so a forgotten regenerate fails THIS suite, not only CI)."""

    def test_committed_graph_conforms(self):
        committed = knowledge_gen.read_committed(knowledge_gen.GRAPH_PATH)
        self.assertIsNotNone(committed, "the knowledge graph must be generated and committed")
        self.assertEqual(_errors(KNOWLEDGE_SCHEMA, json.loads(committed)), [])

    def test_committed_graph_is_in_sync_with_sources(self):
        f = knowledge_gen.check()
        self.assertEqual(f["severity"], "note",
                         "the committed graph is stale — run knowledge_gen.py generate and commit")


class TestGenerateCheckIO(unittest.TestCase):
    """Real sources + a redirected committed path in a temp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "graph.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_generate_then_check_is_in_sync(self):
        g = knowledge_gen.generate(self.path)
        self.assertEqual(g["severity"], "note")
        self.assertIn("Wrote", g["message"])
        self.assertEqual(knowledge_gen.check(self.path)["severity"], "note")

    def test_generate_is_idempotent(self):
        knowledge_gen.generate(self.path)
        again = knowledge_gen.generate(self.path)
        self.assertIn("already up to date", again["message"])

    def test_hand_edit_is_caught_as_drift(self):
        knowledge_gen.generate(self.path)
        with open(self.path, "a", encoding="utf-8", newline="") as fh:
            fh.write("junk a human typed\n")
        self.assertEqual(knowledge_gen.check(self.path)["severity"], "hard")

    def test_absent_graph_is_caught(self):
        self.assertEqual(knowledge_gen.check(os.path.join(self._tmp.name, "nope.json"))["severity"], "hard")

    def test_drift_tier_is_the_callers_tier(self):
        knowledge_gen.generate(self.path)
        with open(self.path, "a", encoding="utf-8", newline="") as fh:
            fh.write("junk\n")
        self.assertEqual(knowledge_gen.check(self.path, tier="soft")["severity"], "soft")


class TestCoverageFingerprintMode(unittest.TestCase):
    """The committed rule, dispatched through validate.kind_coverage (no _run_kind stub needed — the
    fingerprint mode ignores target_files and re-derives from the catalog)."""

    def _rule(self):
        return validate.load_json(RULE_PATH)

    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        rule = self._rule()
        self.assertEqual(_errors(check_schema, rule), [])
        self.assertIn("CI", rule.get("suites", []))
        self.assertEqual(rule["params"]["mode"], "fingerprint")
        self.assertEqual(rule["target"], {"context": "knowledge-fingerprint"})

    def test_real_graph_passes_via_the_mode(self):
        passed, found = validate.kind_coverage(self._rule(), {})
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_stale_committed_graph_is_hard(self):
        # mock.patch.object guarantees GRAPH_PATH is restored even on an unexpected path.
        with tempfile.TemporaryDirectory() as d:
            stale = os.path.join(d, "graph.json")
            knowledge_gen.write_graph('{"schema_version": 1, "entities": []}\n', stale)
            with mock.patch.object(knowledge_gen, "GRAPH_PATH", stale):
                passed, found = validate.kind_coverage(self._rule(), {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))
        self.assertTrue(any("out of date" in f["message"] for f in found))

    def test_missing_committed_graph_is_hard(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(knowledge_gen, "GRAPH_PATH", os.path.join(d, "nope.json")):
                passed, found = validate.kind_coverage(self._rule(), {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_broken_generator_fails_closed(self):
        def boom(*a, **k):
            raise RuntimeError("simulated generator failure")

        with mock.patch.object(knowledge_gen, "check", boom):
            passed, found = validate.kind_coverage(self._rule(), {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_unrecognized_mode_still_fails_closed(self):
        passed, found = validate.kind_coverage(
            {"id": "x", "tier": "hard", "params": {"mode": "bogus"}}, {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))


class TestCommitBoundaryRegen(unittest.TestCase):
    """Slice 23: the PreToolUse commit-boundary regen hook (knowledge/README §Regeneration). On a
    `git commit` it refreshes the graph best-effort and ALWAYS proceeds — it never blocks, never injects,
    fails open, and is reached via the `hook` verb (whose absence/mistyping would BLOCK the commit: the
    no-arg default is `show`, an stdout dump, and an unknown verb returns exit 2)."""

    _COMMIT = {"tool_name": "Bash", "tool_input": {"command": "git commit -m 'x'"}}
    _STATUS = {"tool_name": "Bash", "tool_input": {"command": "git status"}}

    def test_is_git_commit_true_on_commit_amend_and_compound(self):
        for cmd in ("git commit -m 'x'", "git commit --amend", "git add -A && git commit -m y"):
            p = {"tool_name": "Bash", "tool_input": {"command": cmd}}
            self.assertTrue(knowledge_gen._is_git_commit(p), cmd)

    def test_is_git_commit_false_on_non_commit_non_bash_and_malformed(self):
        self.assertFalse(knowledge_gen._is_git_commit(self._STATUS))
        self.assertFalse(knowledge_gen._is_git_commit(
            {"tool_name": "Bash", "tool_input": {"command": "git log --oneline"}}))
        # a non-Bash tool never fires, even if its input text contains the words
        self.assertFalse(knowledge_gen._is_git_commit(
            {"tool_name": "Read", "tool_input": {"file_path": "git commit"}}))
        self.assertFalse(knowledge_gen._is_git_commit({"tool_name": "Bash"}))   # no tool_input
        self.assertFalse(knowledge_gen._is_git_commit(None))                    # malformed
        self.assertFalse(knowledge_gen._is_git_commit({}))
        # a non-string command degrades safe (no TypeError, no spurious finding)
        for bad in (["git", "commit"], 123, {"x": 1}, None):
            self.assertFalse(knowledge_gen._is_git_commit(
                {"tool_name": "Bash", "tool_input": {"command": bad}}), repr(bad))

    def test_handler_regenerates_on_commit_and_proceeds(self):
        with tempfile.TemporaryDirectory() as d:
            scratch = os.path.join(d, "graph.json")
            with mock.patch.object(knowledge_gen, "GRAPH_PATH", scratch), \
                    contextlib.redirect_stderr(io.StringIO()):
                decision = knowledge_gen._regen_handler(self._COMMIT)
            self.assertEqual(decision, hooks.proceed())             # ALWAYS proceed
            self.assertTrue(os.path.exists(scratch))                # the graph was refreshed...
            with open(scratch, encoding="utf-8") as fh:
                json.loads(fh.read())                               # ...and it is valid JSON

    def test_handler_does_not_regenerate_on_non_commit(self):
        with tempfile.TemporaryDirectory() as d:
            scratch = os.path.join(d, "graph.json")                 # never created
            with mock.patch.object(knowledge_gen, "GRAPH_PATH", scratch):
                decision = knowledge_gen._regen_handler(self._STATUS)
            self.assertEqual(decision, hooks.proceed())
            self.assertFalse(os.path.exists(scratch))               # no regen on a non-commit

    def test_handler_fails_open_on_generate_error(self):
        err = io.StringIO()
        with mock.patch.object(knowledge_gen, "generate", side_effect=OSError("boom")), \
                contextlib.redirect_stderr(err):
            decision = knowledge_gen._regen_handler(self._COMMIT)
        self.assertEqual(decision, hooks.proceed())                 # fail-open: proceed, never block
        self.assertIn("could not run", err.getvalue())              # not silent
        self.assertIn("commit was not affected", err.getvalue())

    def test_handler_returns_proceed_on_every_path(self):
        # PreToolUse is block-eligible and is NOT downgraded the way a forced Stop is, so a block/deny
        # return here would BLOCK the commit. The handler must return proceed on every path.
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(knowledge_gen, "GRAPH_PATH", os.path.join(d, "g.json")), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(knowledge_gen._regen_handler(self._COMMIT), hooks.proceed())
            self.assertEqual(knowledge_gen._regen_handler(self._STATUS), hooks.proceed())
        with mock.patch.object(knowledge_gen, "generate", side_effect=ValueError("x")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(knowledge_gen._regen_handler(self._COMMIT), hooks.proceed())

    def test_end_to_end_via_run_hook_proceeds_exit_zero_no_stdout(self):
        with tempfile.TemporaryDirectory() as d:
            scratch = os.path.join(d, "graph.json")
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(knowledge_gen, "GRAPH_PATH", scratch):
                code = hooks.run_hook("PreToolUse", knowledge_gen._regen_handler,
                                      stdin=io.StringIO(json.dumps(self._COMMIT)), stdout=out, stderr=err)
            self.assertEqual(code, hooks.EXIT_PROCEED)              # exit 0 — the commit proceeds
            self.assertEqual(out.getvalue(), "")                   # no structured-output corruption
            self.assertTrue(os.path.exists(scratch))               # the graph was refreshed

    def test_main_hook_verb_routes_to_run_hook(self):
        # The `hook` verb MUST route to run_hook; its absence would fall through to the usage error
        # (exit 2 = a PreToolUse BLOCK) and the no-arg default `show` would dump the graph to stdout.
        with mock.patch.object(hooks, "run_hook", return_value=0) as run:
            self.assertEqual(knowledge_gen.main(["hook"]), 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], "PreToolUse")
        self.assertIs(run.call_args.args[1], knowledge_gen._regen_handler)

    def test_wired_command_passes_the_hook_arg(self):
        # The footgun guard: the wired command MUST end in ` hook`. Dropping the arg re-enables the
        # no-arg `show` -> stdout dump on every PreToolUse. Assert it in both the manifest and settings.
        manifest = validate.load_json(os.path.join(validate.ROOT, ".engine/modules/core/manifest.json"))
        kg_wires = [w for w in manifest["wires"] if w.get("type") == "hook"
                    and "knowledge_gen.py" in w.get("hook", {}).get("command", "")]
        self.assertEqual(len(kg_wires), 1, "exactly one knowledge_gen hook wire")
        self.assertEqual(kg_wires[0]["event"], "PreToolUse")
        self.assertTrue(kg_wires[0]["hook"]["command"].rstrip().endswith(" hook"))
        settings = validate.load_json(os.path.join(validate.ROOT, ".claude", "settings.json"))
        kg_cmds = [h["command"] for grp in settings["hooks"].get("PreToolUse", [])
                   for h in grp.get("hooks", []) if "knowledge_gen.py" in h.get("command", "")]
        self.assertEqual(len(kg_cmds), 1)
        self.assertTrue(kg_cmds[0].rstrip().endswith(" hook"))


if __name__ == "__main__":
    unittest.main()
