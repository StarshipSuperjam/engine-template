#!/usr/bin/env python3
"""Coverage and refusal tests for the persistent mutation registry (issue StarshipSuperjam/engine-template#1151 S02)."""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import accepted_hook_dispatch
from memory import mutation_contract as contract
from memory import execution_context as _execution_context


ROOT = Path(__file__).resolve().parents[3]
MEMORY = Path(__file__).resolve().parent
TOOLS = ROOT / ".engine/tools"
SCHEMA = ROOT / ".engine/schemas/persistent-mutation-registry.v1.json"

_CROSS_CUTTING_SURFACES = (
    "accepted_hook_dispatch", "boot", "boot_alarm_ledger", "checkout_auto_update", "close",
    "first_run_health", "hooks", "modes", "providers",
)


def _production_mutation_surfaces():
    surfaces = []
    for path in MEMORY.rglob("*.py"):
        if path.name.startswith("test_") or path.name in {"__init__.py", "mutation_contract.py"}:
            continue
        module = ".".join(path.relative_to(TOOLS).with_suffix("").parts)
        surfaces.append((str(path), module))
    surfaces.extend((str(TOOLS / f"{module}.py"), module) for module in _CROSS_CUTTING_SURFACES)
    return surfaces


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


class TestMutationRegistryShape(unittest.TestCase):
    def test_schema_is_well_formed_and_the_canonical_registry_conforms(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(contract.document(), schema)

    def test_ids_writers_and_capability_identities_are_unique(self):
        for key in ("id", "writer", "capability_identity"):
            values = [entry[key] for entry in contract.REGISTRY]
            self.assertEqual(len(values), len(set(values)), key)

    def test_every_code_referent_names_a_real_function_in_the_declared_file(self):
        for entry in contract.REGISTRY:
            rel, function = entry["code_referent"].split(":", 1)
            path = ROOT / rel
            self.assertTrue(path.is_file(), entry["id"])
            if path.suffix == ".py":
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                functions = {node.name for node in ast.walk(tree)
                             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
                self.assertIn(function, functions, entry["id"])
            else:
                self.assertIn(function, path.read_text(encoding="utf-8"), entry["id"])

    def test_closed_vocabularies_match_the_schema(self):
        self.assertEqual(contract.EFFECT_CLASSES, {
            "semantic-read", "durable-append", "reversible-mutation", "destructive-irreversible"})
        self.assertEqual(contract.INVOCATION_MODES, {"automatic", "attended"})
        for entry in contract.REGISTRY:
            self.assertIn(entry["target_kind"], contract.TARGET_KINDS)
            self.assertIn(entry["effect_class"], contract.EFFECT_CLASSES)
            self.assertIn(entry["recovery_requirement"], contract.RECOVERY_REQUIREMENTS)
        self.assertEqual({entry["effect_class"] for entry in contract.REGISTRY}, contract.EFFECT_CLASSES)


class TestConfiguredMutationCoverage(unittest.TestCase):
    def test_dispatcher_and_registry_have_the_same_automatic_mutator_roster(self):
        self.assertEqual(set(contract.AUTOMATIC_ENTRYPOINTS), set(accepted_hook_dispatch.AUTOMATIC_MUTATORS))
        self.assertEqual(contract.automatic_coverage_failures(accepted_hook_dispatch.AUTOMATIC_MUTATORS), [])
        entries = {entry["id"]: entry for entry in contract.REGISTRY}
        referenced = set(contract.AUTOMATIC_COMMON_EFFECTS)
        for operation_ids in contract.AUTOMATIC_ENTRYPOINTS.values():
            referenced.update(operation_ids)
        for entry_id in referenced:
            with self.subTest(entry_id=entry_id):
                self.assertIn(entry_id, entries)
                self.assertIn("automatic", entries[entry_id]["allowed_invocation_modes"])

    def test_each_automatic_mutator_is_live_on_both_provider_surfaces(self):
        claude_docs = [
            json.loads((ROOT / ".claude/settings.json").read_text(encoding="utf-8")),
            json.loads((ROOT / ".engine/modules/memory-substrate-sqlite-fts5/manifest.json").read_text(
                encoding="utf-8")),
        ]
        codex_docs = [
            json.loads((ROOT / ".codex/hooks.json").read_text(encoding="utf-8")),
            claude_docs[1],
        ]
        claude = "\n".join(_strings(claude_docs))
        codex = "\n".join(_strings(codex_docs))
        for script in contract.AUTOMATIC_ENTRYPOINTS:
            with self.subTest(script=script):
                self.assertIn(script, claude)
                self.assertIn(script, codex)

    def test_composite_and_read_shaped_boundaries_have_closed_transitive_inventories(self):
        entries = {entry["id"]: entry for entry in contract.REGISTRY}
        writers = {entry["writer"] for entry in contract.REGISTRY}
        for boundary, entry_ids in contract.TRANSITIVE_BOUNDARIES.items():
            with self.subTest(boundary=boundary):
                self.assertIn(boundary, writers)
                self.assertTrue(entry_ids)
                self.assertEqual(len(entry_ids), len(set(entry_ids)))
                self.assertFalse(set(entry_ids) - entries.keys())

    def test_an_unregistered_hook_mutator_fails_coverage(self):
        configured = set(accepted_hook_dispatch.AUTOMATIC_MUTATORS) | {".engine/tools/memory/new_writer.py"}
        self.assertEqual(contract.automatic_coverage_failures(configured),
                         [".engine/tools/memory/new_writer.py"])


class TestLowLevelWriterCoverage(unittest.TestCase):
    def test_every_statically_visible_production_write_is_registered(self):
        self.assertEqual(contract.coverage_failures(_production_mutation_surfaces()), [])
        self.assertIn("hook-runner.runtime-health-marker", {entry["writer"] for entry in contract.REGISTRY})

    def test_an_unregistered_low_level_or_health_writer_fails_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "synthetic.py"
            path.write_text(
                "def new_writer(path):\n"
                "    with open(path, 'w', encoding='utf-8') as handle:\n"
                "        handle.write('x')\n",
                encoding="utf-8",
            )
            self.assertEqual(contract.coverage_failures([str(path)]), ["memory.synthetic.new_writer"])

    def test_os_open_and_sqlite_writes_cannot_hide_from_static_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "synthetic.py"
            path.write_text(
                "import os\n"
                "def flag_writer(path):\n"
                "    return os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)\n"
                "def database_writer(conn):\n"
                "    conn.execute('INSERT INTO entries VALUES (1)')\n",
                encoding="utf-8",
            )
            self.assertEqual(
                contract.coverage_failures([str(path)]),
                ["memory.synthetic.database_writer", "memory.synthetic.flag_writer"],
            )


class TestReadShapedMutationCoverage(unittest.TestCase):
    def test_keyword_and_semantic_public_search_boundaries_are_registered_as_mutations(self):
        by_writer = {entry["writer"]: entry for entry in contract.REGISTRY}
        expected = {
            "memory.mcp_server.search": "derived-index",
            "memory.index.search": "derived-index",
            "memory.mcp_server.recall_by_meaning": "semantic-index",
            "memory.semantic.store.search": "semantic-index",
        }
        for writer, target in expected.items():
            with self.subTest(writer=writer):
                self.assertEqual(by_writer[writer]["target_kind"], target)
                self.assertEqual(by_writer[writer]["effect_class"], "reversible-mutation")
                self.assertTrue(by_writer[writer]["schema_cutover"])

    def test_keyword_search_rebuilds_a_stale_derived_index(self):
        from memory import index, ledger

        if not index.fts5_available():
            self.skipTest("SQLite FTS5 is unavailable")
        with tempfile.TemporaryDirectory() as td:
            ledger_file = os.path.join(td, "ledger.ndjson")
            index_file = os.path.join(td, "index.sqlite3")
            ledger.append({"body": "qualified hook context"}, path=ledger_file)
            index.rebuild(ledger_file=ledger_file, index_file=index_file)
            with sqlite3.connect(index_file) as conn:
                conn.execute("UPDATE meta SET schema_version = 0 WHERE rowid = 1")
                conn.commit()
            result = index.search("qualified", ledger_file=ledger_file, index_file=index_file)
            self.assertTrue(result.records)
            with sqlite3.connect(index_file) as conn:
                current = conn.execute("SELECT schema_version FROM meta WHERE rowid = 1").fetchone()[0]
            self.assertEqual(current, index.INDEX_SCHEMA_VERSION)


class TestFailClosedClassification(unittest.TestCase):
    def test_exact_registered_shape_resolves_one_entry(self):
        entry = contract.classify(
            writer="memory.ledger.append", target_kind="ledger", effect_class="durable-append",
            invocation_mode="automatic", measured_cardinality=1,
        )
        self.assertEqual(entry["id"], "ledger-append")

    def test_unknown_writer_target_effect_or_mode_refuses(self):
        base = dict(writer="memory.ledger.append", target_kind="ledger", effect_class="durable-append",
                    invocation_mode="automatic", measured_cardinality=1)
        cases = (
            {**base, "writer": "memory.ledger.unknown"},
            {**base, "target_kind": "remote-vault"},
            {**base, "effect_class": "reversible-mutation"},
            {**base, "invocation_mode": "attended-only-if-convenient"},
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(contract.MutationContractError):
                contract.classify(**case)

    def test_understated_cardinality_and_undeclared_schema_cutover_refuse(self):
        base = dict(writer="memory.ledger.append", target_kind="ledger", effect_class="durable-append",
                    invocation_mode="automatic")
        with self.assertRaises(contract.MutationContractError):
            contract.classify(**base, measured_cardinality=2)
        with self.assertRaises(contract.MutationContractError):
            contract.classify(**base, measured_cardinality=1, schema_cutover=True)

    def test_every_registry_entry_has_a_classifiable_boundary_witness(self):
        for entry in contract.REGISTRY:
            maximum = entry["declared_cardinality"]["maximum"]
            measured = 1 if maximum is None else min(maximum, 1)
            for mode in entry["allowed_invocation_modes"]:
                with self.subTest(entry=entry["id"], mode=mode):
                    request = dict(
                        writer=entry["writer"], target_kind=entry["target_kind"],
                        effect_class=entry["effect_class"], invocation_mode=mode,
                        measured_cardinality=measured, schema_cutover=entry["schema_cutover"],
                    )
                    if mode == "automatic" and contract._needs_attendance(entry):
                        # Registered for automatic invocation, but destroying the record needs a person:
                        # the witness here is the refusal, and it must name why.
                        with self.assertRaises(contract.MutationContractError) as caught:
                            contract.classify(**request)
                        self.assertIn("attending", str(caught.exception))
                        continue
                    resolved = contract.classify(**request)
                    self.assertEqual(resolved["capability_identity"], entry["capability_identity"])



class TestDegradedTiering(unittest.TestCase):
    """What an UNQUALIFIED session may still do. StarshipSuperjam/engine-template#1151's rule is that
    candidate code never AUTHORS canonical memory; StarshipSuperjam/engine-template#1153 read it as
    "touches nothing" and took reads, diagnostics and Build entry down with it."""

    def test_every_registered_effect_lands_in_exactly_one_tier(self):
        tiers = contract.degraded_tiering()
        ids = [entry["id"] for entry in contract.REGISTRY]
        self.assertEqual(sorted(tiers["allow"] + tiers["refuse"]), sorted(ids))
        self.assertEqual(set(tiers["allow"]) & set(tiers["refuse"]), set())
        self.assertEqual(len(ids), len(set(ids)))

    def test_nothing_that_writes_the_record_is_allowed_unqualified(self):
        record_targets = {"ledger", "ledger-metadata", "capture-cursor", "restore-journal",
                          "backup-pointer", "remote-vault", "remote-git-ref", "erasure-proposal",
                          "export-artifact", "project-repository"}
        leaked = [entry["id"] for entry in contract.REGISTRY
                  if contract.degraded_disposition(entry) == "allow"
                  and entry["target_kind"] in record_targets
                  and entry["effect_class"] != "semantic-read"]
        self.assertEqual(leaked, [], "an unqualified session could write canonical state")

    def test_no_destructive_effect_is_allowed_outside_diagnostics_and_markers(self):
        leaked = [entry["id"] for entry in contract.REGISTRY
                  if contract.degraded_disposition(entry) == "allow"
                  and entry["effect_class"] == "destructive-irreversible"
                  and entry["target_kind"] not in {"degraded-health", "lifecycle-marker"}]
        self.assertEqual(leaked, [])

    def test_the_effects_availability_depends_on_are_allowed(self):
        # Each of these going dark is a failure mode StarshipSuperjam/engine-template#1153 actually produced: no recall, no health record,
        # no crash diagnostics, no findings, and no way into Build.
        for entry_id in ("read-memory-health", "read-recall-window", "read-pins", "read-withheld",
                         "attended-memory-mcp", "attended-keyword-mcp-search", "attended-semantic-mcp-search",
                         "hook-crash-debug", "hook-fail-open-promote",
                         "close-findings-record", "telemetry-finding-emit", "alarm-ledger-write",
                         "automatic-boot-operation"):
            with self.subTest(entry_id):
                self.assertEqual(contract.degraded_disposition(contract.entry_by_id(entry_id)), "allow")

    def test_index_writers_are_refused_because_the_index_holds_a_second_copy_of_the_record(self):
        """The tier's one correction after the deliverable review.

        These were allowed on the reasoning that the index is "regenerated from the ledger by construction",
        so corrupting it costs nothing. The index contradicts that: it stores each record's whole JSON body
        and `index.query` hydrates results straight out of that column. Letting unqualified code rewrite it
        would let candidate code put invented text in front of every future session's recall without ever
        touching the ledger — the rule defeated through the accelerator rather than the record.

        Refusing costs slower recall and nothing else, which is what keeps this compatible with
        availability-first: a refused rebuild is swallowed, the index reads stale, and both query paths fall
        through to the full ledger scan.
        """
        for entry_id in ("index-rebuild", "index-extend", "index-schema", "index-stale-heal",
                         "attended-semantic-sync", "semantic-store-reconcile", "semantic-passages-drop",
                         "semantic-store-connect"):
            with self.subTest(entry_id):
                entry = contract.entry_by_id(entry_id)
                self.assertEqual(contract.degraded_disposition(entry), "refuse")
        # …while READING through them is untouched. Each recall boundary is registered against an index target
        # only because it MAY heal a stale index on the way past; the rebuilding writers above are nested and
        # re-tiered on their own entries, so they refuse, `_heal_if_stale` swallows that, and the read falls
        # through to the ledger scan. Refusing these by target instead takes recall down — #1153 again, and
        # the launch-contract test caught exactly that when this list was missing `memory.index.search`.
        for entry_id in ("attended-keyword-mcp-search", "attended-keyword-search-heal",
                         "attended-semantic-mcp-search", "attended-semantic-search-reconcile"):
            with self.subTest(entry_id):
                self.assertEqual(contract.degraded_disposition(contract.entry_by_id(entry_id)), "allow")

    def test_the_stance_marker_is_not_in_the_registry_at_all(self):
        writers = {entry["writer"] for entry in contract.REGISTRY}
        self.assertNotIn("modes.set_stance", writers)
        self.assertNotIn("modes.clear_stance", writers)
        self.assertEqual(contract.SESSION_EPHEMERAL_WRITERS,
                         {"modes.set_stance", "modes.clear_stance", "modes._harden_marker_write"})

    def test_refusals_name_the_effect_and_what_makes_it_stick(self):
        for entry_id in ("attended-pin-add", "attended-withhold", "attended-restore-withheld"):
            with self.subTest(entry_id):
                reply = contract.degraded_refusal(contract.entry_by_id(entry_id))
                self.assertNotIn("execution context", reply)      # no internal vocabulary
                self.assertIn("qualif", reply)                    # says why
                self.assertIn("session start", reply)             # says what makes it stick
        self.assertIn("erase", contract.degraded_refusal(contract.entry_by_id("attended-withhold")))


class TestAttendedOnlyRecordRewrites(unittest.TestCase):
    """Rewriting the record is attended-only EVEN WHEN QUALIFIED. PR StarshipSuperjam/engine-template#1148's near-loss cleared every other
    safeguard; what was missing was a person."""

    def _classify(self, entry_id, mode):
        entry = contract.entry_by_id(entry_id)
        return contract.classify(writer=entry["writer"], target_kind=entry["target_kind"],
                                 effect_class=entry["effect_class"], invocation_mode=mode,
                                 measured_cardinality=1, schema_cutover=entry["schema_cutover"])

    def test_an_automatic_wholesale_rescrub_is_refused_even_with_everything_else_in_order(self):
        with self.assertRaises(contract.MutationContractError):
            self._classify("attended-rescrub", "automatic")
        # …and the ATTENDANCE rule is what refuses it independently of its declared invocation modes, which
        # is the property this class is about rather than the mode declaration.
        self.assertTrue(contract._needs_attendance(contract.entry_by_id("attended-rescrub")))

    def test_the_same_rescrub_is_permitted_when_attended_and_returns_its_entry(self):
        entry = self._classify("attended-rescrub", "attended")
        self.assertEqual(entry["id"], "attended-rescrub")

    def test_compaction_is_NOT_attendance_gated_so_a_merged_erasure_is_actually_enacted(self):
        """The deliverable review's finding, pinned so the symmetry argument cannot quietly return.

        Requiring attendance here looked like the same rule as the rescrub and was a regression: compaction
        is the only production executor of physical erasure, its only automatic trigger is the PreCompact
        hook, and gating that meant an erasure the operator had consented to by merging was never carried
        out — not deleted, and not hidden either. The operator's role in `operator-merged-consent` is to
        consent, and the merge already was that.
        """
        entry = self._classify("automatic-compaction", "automatic")
        self.assertEqual(entry["id"], "automatic-compaction")
        self.assertEqual(entry["recovery_requirement"], "operator-merged-consent")

    def test_appending_stays_automatic_because_capture_must_keep_working(self):
        for entry_id in ("ledger-append", "automatic-capture", "capture-transaction"):
            entry = contract.entry_by_id(entry_id)
            with self.subTest(entry_id):
                self.assertFalse(contract._needs_attendance(entry))

    def test_attendance_is_required_for_record_destruction_backed_by_a_person(self):
        needing = sorted(e["id"] for e in contract.REGISTRY if contract._needs_attendance(e))
        # Exactly the record-destroying effect whose declared recovery is a snapshot someone takes first —
        # the shape of PR StarshipSuperjam/engine-template#1148's near-loss, where a background pass rewrote
        # live records with every other safeguard satisfied. Compaction is deliberately NOT here; see
        # `test_compaction_is_NOT_attendance_gated_so_a_merged_erasure_is_actually_enacted`.
        self.assertEqual(needing, ["attended-rescrub"])

    def test_recovery_and_index_rebuilds_stay_automatic(self):
        # Refusing these would break real things: SessionStart cannot finish an interrupted restore, and
        # recall has no index. Each is named so a later widening of the rule has to argue with this test.
        for entry_id in ("automatic-restore-reconcile", "restore-prior-set", "ledger-replace",
                         "compaction-temp-write", "compaction-temp-reap", "index-rebuild",
                         "automatic-compaction", "automatic-erasure-observer"):
            entry = contract.entry_by_id(entry_id)
            with self.subTest(entry_id):
                self.assertFalse(contract._needs_attendance(entry))



class TestPreCompactBoundedWarning(unittest.TestCase):
    """PreCompact cannot inject context and fires constantly, so its disclosure is one line to the hook log —
    emitted only when compaction actually had work it declined to do."""

    def _warn(self, report):
        from memory import compact
        return compact.unenacted_warning(report)

    def test_a_refusal_produces_one_bounded_sentence(self):
        warning = self._warn({"status": "skipped", "folded": 0, "pruned": 0,
                              "reason": "memory.compact.compact needs this session to be qualified to write "
                                        "memory, and it isn't yet"})
        self.assertIsNotNone(warning)
        self.assertLess(len(warning), 300)
        # Bounded, not fixed at two. The sentence now carries cause, state and remedy, because reporting a
        # failing backup push with the qualification remedy sent the operator to wait for the wrong thing.
        # Length is the constraint that matters here — a paragraph on every squash teaches a reader to skip.
        self.assertLessEqual(warning.count("."), 3)
        self.assertIn("Nothing was changed", warning)
        self.assertNotIn("registry", warning)
        self.assertNotIn("execution context", warning)

    def test_an_ordinary_skip_says_nothing(self):
        self.assertIsNone(self._warn({"status": "skipped", "reason": "below the compaction threshold"}))
        self.assertIsNone(self._warn({"status": "ok", "folded": 3, "pruned": 1}))
        self.assertIsNone(self._warn(None))

    def test_a_missing_qualification_also_warns(self):
        self.assertIsNotNone(self._warn(
            {"status": "skipped", "reason": "compaction faulted, skipped: memory.compact.compact needs this "
                                            "session to be qualified to write memory"}))


def _attended_ctx(registry_id):
    return {"operation": {"invocation_mode": "attended", "registry_id": registry_id}}


def _automatic_ctx(script):
    return {"operation": {"invocation_mode": "automatic"}, "invocation": {"script": script}}


def _closure(ctx):
    """The real closure function against the real contract — the same call the guard makes at mint time."""
    return _execution_context._allowed_registry_ids(ctx, contract)


class TestSanctionedWriteLaneClosures(unittest.TestCase):
    """Every sanctioned write lane must reach the exact nested store operations its code path consumes.

    The withhold/restore lanes (and the CLI pin-remove lane that inherits withhold) reach their ledger
    writers through an UNREGISTERED helper — `memory.forget._write_control` — so the closure's callers-edge
    leg cannot bridge them the way it bridges pin-add (whose nested entries name `memory.pins.add` outright).
    Before the boundary keys these lanes closed over just their own root and refused the very first nested
    write with the raw "outside this invocation's closed transitive boundary" error."""

    _WITHHOLD_NESTED = frozenset({"capture-lock-create", "ledger-append", "ledger-index-epoch"})

    def test_mcp_withhold_reaches_lock_epoch_and_append(self):
        self.assertLessEqual(self._WITHHOLD_NESTED, _closure(_attended_ctx("attended-withhold")))

    def test_mcp_restore_reaches_lock_epoch_and_append(self):
        self.assertLessEqual(self._WITHHOLD_NESTED, _closure(_attended_ctx("attended-restore-withheld")))

    def test_cli_pin_removal_inherits_the_withhold_lane(self):
        # engine-drop-operator-pin installs attended-pin-remove; its closure reaches attended-withhold across
        # the callers edge (`memory.pins.remove` is a caller of attended-withhold), and thence the nested writers.
        closure = _closure(_attended_ctx("attended-pin-remove"))
        self.assertIn("attended-withhold", closure)
        self.assertLessEqual(self._WITHHOLD_NESTED, closure)

    def test_the_already_working_pin_add_lane_is_undisturbed(self):
        self.assertLessEqual(self._WITHHOLD_NESTED, _closure(_attended_ctx("attended-pin-add")))

    def test_automatic_erasure_observer_reaches_lock_and_append(self):
        closure = _closure(_automatic_ctx(".engine/tools/memory/erasure_observer.py"))
        self.assertLessEqual({"capture-lock-create", "ledger-append"}, closure)

    def test_the_explicit_erasure_grant_holds_without_the_shared_harness_cascade(self):
        # The erasure lane's ids are ALSO reachable through the automatic shared-harness cascade
        # (AUTOMATIC_COMMON_EFFECTS -> hooks.run_hook -> boot/close boundaries -> capture writers), so on
        # today's tree this explicit key is defensive rather than the thing that unbreaks the lane — the
        # observer was never actually refused. This test pins the key's real value: were that cascade ever
        # tightened toward least privilege, the explicit grant still delivers EXACTLY what enact_erasure
        # consumes, and nothing more.
        original = contract.AUTOMATIC_COMMON_EFFECTS
        try:
            contract.AUTOMATIC_COMMON_EFFECTS = ()
            closure = _closure(_automatic_ctx(".engine/tools/memory/erasure_observer.py"))
        finally:
            contract.AUTOMATIC_COMMON_EFFECTS = original
        self.assertEqual({eid for eid in closure if eid != "automatic-erasure-observer"},
                         {"capture-lock-create", "ledger-append"})

    def test_the_boundary_keys_declare_exactly_the_consumed_ids(self):
        self.assertEqual(contract.TRANSITIVE_BOUNDARIES["memory.forget.withhold"],
                         ("capture-lock-create", "ledger-append", "ledger-index-epoch"))
        self.assertEqual(contract.TRANSITIVE_BOUNDARIES["memory.forget.restore"],
                         ("capture-lock-create", "ledger-append", "ledger-index-epoch"))
        self.assertEqual(contract.TRANSITIVE_BOUNDARIES["memory.erasure_observer.enact_from_merged_prs"],
                         ("capture-lock-create", "ledger-append"))


# The full allowed closure of every attended write root, pinned. Attended closures start from the single
# operation and never fold in the automatic shared-harness cascade, so each set is small and each entry is a
# genuine reachable write — which is exactly why a writer-through-helper gap (the withhold/restore bug) shows
# up here as a MISSING id rather than being masked. Regenerate deliberately when the registry legitimately
# changes; an unexplained diff is a lane that quietly gained or lost reach.
_ATTENDED_WRITE_ROOT_CLOSURES = {
    'accepted-lock-create': ('accepted-lock-create',),
    'accepted-metadata-write': ('accepted-metadata-write',),
    'attended-accepted-activation': ('accepted-lock-create', 'accepted-metadata-write', 'attended-accepted-activation'),
    'attended-backup-setup': ('attended-backup-setup', 'backup-pointer-write', 'project-pointer-commit', 'vault-destination-bind', 'vault-readme-seed'),
    'attended-clawmem-export': ('attended-clawmem-export',),
    'attended-clawmem-export-teardown': ('attended-clawmem-export', 'attended-clawmem-export-teardown'),
    'attended-erasure-request': ('attended-erasure-request', 'erasure-pr-open', 'erasure-proposal-write'),
    'attended-export': ('attended-export',),
    'attended-first-run-marker-stage': ('attended-first-run-marker-stage',),
    'attended-keyword-mcp-search': ('attended-keyword-mcp-search', 'attended-keyword-search-heal', 'index-rebuild', 'index-schema', 'index-stale-heal'),
    'attended-keyword-search-heal': ('attended-keyword-search-heal',),
    'attended-migration-snapshot': ('attended-migration-snapshot', 'migration-stamp-write', 'vault-blob-create', 'vault-commit-build', 'vault-tag-create'),
    'attended-pin-add': ('attended-pin-add', 'capture-lock-create', 'ledger-append', 'ledger-index-epoch'),
    'attended-pin-remove': ('attended-pin-remove', 'attended-withhold', 'capture-lock-create', 'ledger-append', 'ledger-index-epoch'),
    'attended-rescrub': ('attended-migration-snapshot', 'attended-rescrub', 'capture-lock-create', 'index-rebuild', 'index-schema', 'ledger-index-epoch', 'ledger-replace', 'migration-stamp-write', 'semantic-passages-drop', 'vault-blob-create', 'vault-commit-build', 'vault-tag-create'),
    'attended-restore-now': ('attended-restore-now', 'automatic-restore-reconcile', 'capture-lock-create', 'index-rebuild', 'index-schema', 'ledger-generation-set', 'ledger-replace', 'restore-apply', 'restore-journal-complete', 'restore-journal-write', 'restore-orphan-cleanup', 'restore-prior-set', 'restore-quiet-remove'),
    'attended-restore-pre-migration': ('attended-restore-pre-migration', 'automatic-restore-reconcile', 'capture-lock-create', 'index-rebuild', 'index-schema', 'ledger-generation-set', 'ledger-replace', 'migration-stamp-clear', 'restore-apply', 'restore-journal-complete', 'restore-journal-write', 'restore-orphan-cleanup', 'restore-prior-set', 'restore-quiet-remove'),
    'attended-restore-withheld': ('attended-restore-withheld', 'capture-lock-create', 'ledger-append', 'ledger-index-epoch'),
    'attended-saved-memory-projection': ('attended-saved-memory-projection', 'restore-quiet-remove', 'saved-belief-temp-projection'),
    'attended-semantic-mcp-search': ('attended-semantic-mcp-search', 'attended-semantic-search-reconcile', 'semantic-store-connect', 'semantic-store-reconcile'),
    'attended-semantic-search-reconcile': ('attended-semantic-search-reconcile', 'semantic-store-connect', 'semantic-store-reconcile'),
    'attended-semantic-sync': ('attended-semantic-sync', 'semantic-store-connect', 'semantic-store-reconcile'),
    'attended-withhold': ('attended-withhold', 'capture-lock-create', 'ledger-append', 'ledger-index-epoch'),
    'automatic-backup': ('automatic-backup', 'backup-status-write', 'vault-blob-create', 'vault-commit-build', 'vault-files-push', 'vault-snapshot-publish'),
    'automatic-compaction': ('automatic-compaction', 'capture-lock-create', 'compaction-temp-reap', 'compaction-temp-write', 'index-rebuild', 'index-schema', 'ledger-generation-bump', 'ledger-replace', 'migration-window-reap'),
    'automatic-restore-reconcile': ('automatic-restore-reconcile', 'restore-orphan-cleanup', 'restore-prior-set'),
    'backup-pointer-write': ('backup-pointer-write',),
    'backup-status-write': ('backup-status-write',),
    'capture-lock-create': ('capture-lock-create',),
    'checkout-preference-write': ('checkout-preference-write',),
    'close-findings-clear': ('close-findings-clear',),
    'close-findings-record': ('close-findings-record',),
    'compaction-temp-reap': ('compaction-temp-reap',),
    'compaction-temp-write': ('compaction-temp-write',),
    'erasure-pr-open': ('erasure-pr-open',),
    'erasure-proposal-write': ('erasure-proposal-write',),
    'index-rebuild': ('index-rebuild', 'index-schema'),
    'index-schema': ('index-schema',),
    'index-stale-heal': ('index-rebuild', 'index-schema', 'index-stale-heal'),
    'ledger-append': ('ledger-append',),
    'ledger-generation-bump': ('ledger-generation-bump',),
    'ledger-generation-set': ('ledger-generation-set',),
    'ledger-index-epoch': ('ledger-index-epoch',),
    'ledger-replace': ('ledger-replace',),
    'migration-stamp-clear': ('migration-stamp-clear',),
    'migration-stamp-write': ('migration-stamp-write',),
    'migration-window-close': ('migration-window-close',),
    'migration-window-open': ('migration-window-open',),
    'migration-window-reap': ('migration-window-reap',),
    'project-pointer-commit': ('project-pointer-commit',),
    'restore-apply': ('capture-lock-create', 'index-rebuild', 'index-schema', 'ledger-generation-set', 'ledger-replace', 'restore-apply', 'restore-journal-complete', 'restore-journal-write', 'restore-orphan-cleanup', 'restore-quiet-remove'),
    'restore-journal-complete': ('restore-journal-complete', 'restore-journal-write'),
    'restore-journal-write': ('restore-journal-write',),
    'restore-orphan-cleanup': ('restore-orphan-cleanup',),
    'restore-prior-set': ('restore-prior-set',),
    'restore-quiet-remove': ('restore-quiet-remove',),
    'resurrection-finding': ('resurrection-finding',),
    'saved-belief-temp-projection': ('restore-quiet-remove', 'saved-belief-temp-projection'),
    'semantic-passages-drop': ('semantic-passages-drop',),
    'semantic-store-connect': ('semantic-store-connect',),
    'semantic-store-reconcile': ('semantic-store-reconcile',),
    'stranding-log-append': ('stranding-log-append',),
    'stranding-log-export': ('stranding-log-export',),
    'telemetry-finding-emit': ('telemetry-finding-emit',),
    'vault-blob-create': ('vault-blob-create',),
    'vault-commit-build': ('vault-blob-create', 'vault-commit-build'),
    'vault-destination-bind': ('vault-destination-bind',),
    'vault-files-push': ('vault-blob-create', 'vault-commit-build', 'vault-files-push'),
    'vault-readme-seed': ('vault-readme-seed',),
    'vault-snapshot-publish': ('vault-blob-create', 'vault-commit-build', 'vault-files-push', 'vault-snapshot-publish'),
    'vault-tag-create': ('vault-tag-create',),
    'vault-tag-delete': ('vault-tag-delete',),
}


class TestClosureCompletenessCensus(unittest.TestCase):
    """A completeness census over every write lane, so a future writer-through-helper gap or registry edit
    fails a test DELIBERATELY instead of shipping a silently-broken lane.

    Two shapes, matched to where the gap class actually bites:

    * Attended write roots get their FULL closure pinned (`_ATTENDED_WRITE_ROOT_CLOSURES`). These closures do
      not fold in the automatic shared-harness cascade, so a missing nested writer is visible rather than
      masked — this is the census that would have caught the withhold/restore bug on the day it landed.
    * Automatic entrypoints get an INVARIANT (each reaches the core store writers) rather than a pinned
      golden. Their closures are large and legitimately volatile as the control plane evolves, and the shared
      cascade already MASKS a helper gap there (as it did for erasure), so a full golden would be high-noise,
      low-signal, and would not detect the gap class anyway."""

    def test_every_attended_write_root_matches_its_pinned_closure(self):
        roots = sorted(entry["id"] for entry in contract.REGISTRY
                       if "attended" in entry["allowed_invocation_modes"]
                       and entry["effect_class"] != "semantic-read")
        # The golden and the live registry name exactly the same roster — a new or removed write root shows here.
        self.assertEqual(roots, sorted(_ATTENDED_WRITE_ROOT_CLOSURES))
        for registry_id in roots:
            with self.subTest(root=registry_id):
                self.assertEqual(tuple(sorted(_closure(_attended_ctx(registry_id)))),
                                 _ATTENDED_WRITE_ROOT_CLOSURES[registry_id])

    def test_every_automatic_entrypoint_reaches_the_core_store_writers(self):
        for script in contract.AUTOMATIC_ENTRYPOINTS:
            with self.subTest(script=script):
                closure = _closure(_automatic_ctx(script))
                self.assertIn("ledger-append", closure)
                self.assertIn("capture-lock-create", closure)

    def test_the_census_detects_a_removed_boundary_grant(self):
        # The deliberately-broken fixture: strip the withhold boundary key and confirm the census would fail
        # — proof the golden is a live tripwire, not a tautology re-derived from the same data it checks.
        from types import MappingProxyType
        broken = MappingProxyType({k: v for k, v in contract.TRANSITIVE_BOUNDARIES.items()
                                   if k != "memory.forget.withhold"})
        original = contract.TRANSITIVE_BOUNDARIES
        try:
            contract.TRANSITIVE_BOUNDARIES = broken
            closure = tuple(sorted(_closure(_attended_ctx("attended-withhold"))))
        finally:
            contract.TRANSITIVE_BOUNDARIES = original
        self.assertNotEqual(closure, _ATTENDED_WRITE_ROOT_CLOSURES["attended-withhold"])
        self.assertEqual(closure, ("attended-withhold",))  # collapses to just the root, the pre-fix bug


if __name__ == "__main__":
    unittest.main()
