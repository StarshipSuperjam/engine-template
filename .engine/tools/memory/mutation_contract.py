#!/usr/bin/env python3
"""Canonical persistent-mutation inventory and fail-closed request classifier.

This module is intentionally substrate-neutral.  It names effects and exact writer identities without granting
authority to execute them; the execution-context boundary binds these entries into operation-scoped
capabilities. The registry is also an executable census: automatic entry points, attended operations, low-level writers, and degraded-health
writers all have one closed description, and an unknown or understated request is refused.
"""
from __future__ import annotations

import ast
import copy
import os
from types import MappingProxyType


SCHEMA_VERSION = "persistent-mutation-registry.v1"
EFFECT_CLASSES = frozenset({
    "semantic-read", "durable-append", "reversible-mutation", "destructive-irreversible",
})
INVOCATION_MODES = frozenset({"automatic", "attended"})
TARGET_KINDS = frozenset({
    "ledger", "ledger-metadata", "derived-index", "capture-cursor", "lifecycle-marker",
    "degraded-health", "backup-pointer", "remote-vault", "remote-git-ref", "restore-journal",
    "erasure-proposal", "export-artifact", "ephemeral-staging", "semantic-index", "project-repository",
    "tracked-finding",
})
RECOVERY_REQUIREMENTS = frozenset({
    "none", "append-lock", "atomic-replace", "derived-rebuild", "compare-and-set", "durable-journal",
    "backup-snapshot", "operator-merged-consent", "remote-ref-compare-and-set", "best-effort-diagnostic",
})


class MutationContractError(RuntimeError):
    """The requested effect is absent, ambiguous, or wider than the registry declares."""


def _entry(entry_id, writer, target, effect, modes, maximum, unit, recovery, callers, *,
           schema_cutover=False, code_referent=None):
    module, _, function = writer.rpartition(".")
    return {
        "id": entry_id,
        "code_referent": code_referent or f".engine/tools/{module.replace('.', '/')}.py:{function}",
        "writer": writer,
        "target_kind": target,
        "effect_class": effect,
        "declared_cardinality": {"maximum": maximum, "unit": unit},
        "schema_cutover": bool(schema_cutover),
        "recovery_requirement": recovery,
        "allowed_invocation_modes": list(modes),
        "capability_identity": f"persistent-mutation/{entry_id}",
        "callers": list(callers),
    }


_AUTO = ("automatic",)
_ATTENDED = ("attended",)
_BOTH = ("automatic", "attended")

# Cardinality ``None`` means the operation is intentionally unbounded/dynamic and must be represented that way
# in a future capability.  A finite integer is a hard maximum; measured work above it is always refused.
REGISTRY = (
    # Configured automatic operation entry points.
    _entry("automatic-capture", "memory.capture.capture_turn_delta", "ledger", "durable-append", _AUTO,
           None, "records", "append-lock", ["close._trigger_ambient_capture"]),
    _entry("capture-transaction", "memory.capture._capture", "ledger", "durable-append", _AUTO, None,
           "records", "append-lock", ["memory.capture.capture_turn_delta"]),
    _entry("automatic-compaction", "memory.compact.compact", "ledger", "destructive-irreversible", _BOTH,
           None, "records", "operator-merged-consent",
           ["memory.compact._pre_compact_handler", "memory.compact.run"]),
    _entry("automatic-erasure-observer", "memory.erasure_observer.enact_from_merged_prs", "ledger",
           "durable-append", _AUTO, None, "records", "operator-merged-consent",
           ["memory.erasure_observer._session_start_handler"]),
    _entry("automatic-backup", "memory.backup_vault.push_now", "remote-vault", "reversible-mutation", _BOTH,
           None, "files", "remote-ref-compare-and-set",
           ["memory.backup_vault._session_start_handler", "memory.backup_vault.main"]),
    _entry("automatic-restore-reconcile", "memory.restore_vault.reconcile_interrupted_restore",
           "restore-journal", "reversible-mutation", _BOTH, None, "files", "durable-journal",
           ["boot.handler", "memory.restore_vault.restore_now", "memory.restore_vault.restore_pre_migration"],
           schema_cutover=True),

    # Other persistent effects reached by the automatic hook handlers and accepted dispatcher.  These are
    # not ledger payloads, but they are exactly the sidecars, control state, caches, and diagnostics whose
    # omission would let a hook remain write-capable outside the eventual S03 capability boundary.
    _entry("automatic-checkout-catch-up", "checkout_auto_update.automatic_catch_up",
           "project-repository", "reversible-mutation", _AUTO, None, "files", "compare-and-set",
           ["boot.handler"]),
    _entry("automatic-live-session", "providers.write_live_session", "lifecycle-marker",
           "reversible-mutation", _AUTO, 1, "status-records", "atomic-replace", ["boot.handler"]),
    _entry("automatic-alarm-presentation", "boot_alarm_ledger.decide", "degraded-health",
           "reversible-mutation", _AUTO, None, "status-records", "atomic-replace", ["boot._relay_lines"]),
    _entry("automatic-first-run-marker-consume", "first_run_health.clear_first_run_marker",
           "lifecycle-marker", "destructive-irreversible", _AUTO, 1, "files", "none",
           ["boot._relay_lines"]),
    _entry("attended-first-run-marker-stage", "first_run_health.mark_first_run_applied",
           "lifecycle-marker", "reversible-mutation", _ATTENDED, 1, "files",
           "best-effort-diagnostic", ["instantiator.retire"]),
    _entry("accepted-tree-materialize", "accepted_hook_dispatch._materialize", "ephemeral-staging",
           "reversible-mutation", _AUTO, None, "files", "derived-rebuild",
           ["accepted_hook_dispatch.dispatch"]),
    _entry("accepted-dispatch-operation", "accepted_hook_dispatch.dispatch", "ephemeral-staging",
           "reversible-mutation", _AUTO, None, "files", "derived-rebuild",
           ["accepted_hook_dispatch.main"]),
    _entry("automatic-boot-operation", "boot.handler", "lifecycle-marker", "reversible-mutation", _AUTO,
           None, "files", "durable-journal", ["hooks.run_hook"]),
    _entry("automatic-close-operation", "close.handler", "ledger", "durable-append", _AUTO, None,
           "records", "append-lock", ["hooks.run_hook"]),
    _entry("automatic-hook-harness", "hooks.run_hook", "degraded-health", "semantic-read", _AUTO,
           None, "status-records", "best-effort-diagnostic", ["automatic provider hook launchers"]),
    _entry("attended-accepted-activation", "accepted_hook_dispatch.activate", "lifecycle-marker",
           "reversible-mutation", _ATTENDED, 1, "status-records", "compare-and-set",
           ["accepted_hook_dispatch.main"]),

    # Public attended operations (CLI, MCP, setup, or maintenance).
    _entry("attended-pin-add", "memory.pins.add", "ledger", "durable-append", _ATTENDED, 1, "records",
           "append-lock", ["memory.mcp_server.pin", "memory.pins.main"]),
    _entry("attended-pin-remove", "memory.pins.remove", "ledger", "reversible-mutation", _ATTENDED, 1,
           "records", "append-lock", ["memory.pins.main"]),
    _entry("attended-withhold", "memory.forget.withhold", "ledger", "reversible-mutation", _ATTENDED, 1,
           "records", "append-lock", ["memory.mcp_server.withhold", "memory.pins.remove"]),
    _entry("attended-restore-withheld", "memory.forget.restore", "ledger", "reversible-mutation", _ATTENDED,
           1, "records", "append-lock", ["memory.mcp_server.restore"]),
    _entry("attended-backup-setup", "memory.backup_vault.setup", "project-repository",
           "reversible-mutation", _ATTENDED, None, "repositories", "compare-and-set",
           ["memory.backup_vault.main"]),
    _entry("attended-migration-snapshot", "memory.backup_vault.snapshot_for_migration", "remote-vault",
           "reversible-mutation", _ATTENDED, None, "files", "remote-ref-compare-and-set",
           [".engine/tools/module_manager.py", "memory.rescrub.run"]),
    _entry("attended-restore-now", "memory.restore_vault.restore_now", "ledger", "reversible-mutation",
           _ATTENDED, None, "records", "durable-journal", ["memory.restore_vault.main"], schema_cutover=True),
    _entry("attended-restore-pre-migration", "memory.restore_vault.restore_pre_migration", "ledger",
           "reversible-mutation", _ATTENDED, None, "records", "durable-journal",
           ["module_manager.main"], schema_cutover=True),
    _entry("attended-erasure-request", "memory.erase.request", "erasure-proposal", "reversible-mutation",
           _ATTENDED, None, "records", "operator-merged-consent", ["memory.erase.main"]),
    _entry("attended-rescrub", "memory.rescrub.run", "ledger", "destructive-irreversible", _ATTENDED, None,
           "records", "backup-snapshot", ["memory.rescrub.main"]),
    _entry("attended-export", "memory.export.write", "export-artifact", "destructive-irreversible",
           _ATTENDED, 1, "files", "backup-snapshot", ["memory.export.main"]),
    # The ClawMem migration exporter (a substrate-owned migration instrument, retired with the module at PR 4).
    # `_render` writes the export tree (conversations/, curated/, meta/); `export_all` tears a half-written
    # export down fail-closed on a scrub fault. Both are terminal-gated in `main`, like the export above.
    _entry("attended-clawmem-export", "memory.clawmem_export._render", "export-artifact",
           "destructive-irreversible", _ATTENDED, None, "files", "backup-snapshot",
           ["memory.clawmem_export.export_all"]),
    _entry("attended-clawmem-export-teardown", "memory.clawmem_export.export_all", "export-artifact",
           "destructive-irreversible", _ATTENDED, None, "files", "none",
           ["memory.clawmem_export.main"]),
    _entry("attended-keyword-search-heal", "memory.index.search", "derived-index",
           "reversible-mutation", _ATTENDED, None, "rows", "derived-rebuild",
           ["memory.mcp_server._recall"], schema_cutover=True),
    _entry("attended-keyword-mcp-search", "memory.mcp_server.search", "derived-index",
           "reversible-mutation", _ATTENDED, None, "rows", "derived-rebuild",
           ["memory.mcp_server.main"], schema_cutover=True),
    _entry("attended-semantic-search-reconcile", "memory.semantic.store.search", "semantic-index",
           "reversible-mutation", _ATTENDED, None, "rows", "derived-rebuild",
           ["memory.mcp_server.recall_by_meaning"], schema_cutover=True),
    _entry("attended-semantic-mcp-search", "memory.mcp_server.recall_by_meaning", "semantic-index",
           "reversible-mutation", _ATTENDED, None, "rows", "derived-rebuild",
           ["memory.mcp_server.main"], schema_cutover=True),
    _entry("attended-semantic-sync", "memory.semantic.store.sync", "semantic-index",
           "reversible-mutation", _ATTENDED, None, "rows", "derived-rebuild",
           ["memory.semantic.store.main"], schema_cutover=True),
    _entry("attended-memory-mcp", "memory.mcp_server.main", "ledger", "semantic-read", _ATTENDED,
           1, "servers", "none", ["configured MCP launchers"]),
    _entry("read-memory-health", "memory.mcp_server.health", "degraded-health", "semantic-read", _ATTENDED,
           1, "status-records", "none", ["memory.mcp_server.main"]),
    _entry("read-recall-window", "memory.mcp_server.recall_window", "ledger", "semantic-read", _ATTENDED,
           None, "records", "none", ["memory.mcp_server.main"]),
    _entry("read-pins", "memory.mcp_server.list_pins", "ledger", "semantic-read", _ATTENDED, None,
           "records", "none", ["memory.mcp_server.main"]),
    _entry("read-withheld", "memory.mcp_server.list_withheld", "ledger", "semantic-read", _ATTENDED, None,
           "records", "none", ["memory.mcp_server.main"]),
    _entry("attended-list-withheld", "memory.forget.main", "ledger", "semantic-read", _ATTENDED, None,
           "records", "none", ["engine-restore-operator-pin fallback"]),
    _entry("read-backup-status", "memory.backup_vault.status", "backup-pointer", "semantic-read", _ATTENDED,
           1, "status-records", "none", ["memory.backup_vault.main"]),
    _entry("read-restore-status", "memory.restore_vault.status", "restore-journal", "semantic-read", _ATTENDED,
           1, "status-records", "none", ["memory.restore_vault.main"]),
    _entry("read-rescrub-plan", "memory.rescrub.plan", "ledger", "semantic-read", _ATTENDED, None,
           "records", "none", ["memory.rescrub.main"]),
    _entry("read-remote-vault", "memory.restore_vault.test_read", "remote-vault", "semantic-read", _ATTENDED,
           None, "files", "none", ["memory.restore_vault.main"]),
    _entry("attended-saved-memory-projection", "memory.restore_vault.read_saved_memory", "ephemeral-staging",
           "reversible-mutation", _ATTENDED, 1, "files", "none", ["memory.restore_vault.main"]),

    # Ledger and derived-index low-level writers.
    _entry("ledger-append", "memory.ledger.append", "ledger", "durable-append", _BOTH, 1, "records",
           "append-lock", ["memory.capture._recover_capture_transaction", "memory.compact.enact_erasure", "memory.forget._write_control",
                           "memory.pins.add"]),
    _entry("ledger-index-epoch", "memory.ledger.bump_index_epoch", "ledger-metadata",
           "reversible-mutation", _BOTH, 1, "files", "atomic-replace",
           ["memory.index.extend", "memory.forget._write_control", "memory.pins.add", "memory.rescrub.run"]),
    _entry("ledger-generation-bump", "memory.ledger.bump_generation", "ledger-metadata",
           "reversible-mutation", _BOTH, 1, "files", "atomic-replace", ["memory.compact.compact"]),
    _entry("ledger-generation-set", "memory.ledger.set_generation", "ledger-metadata", "reversible-mutation",
           _BOTH, 1, "files", "atomic-replace", ["memory.restore_vault._apply_restore"]),
    _entry("ledger-replace", "memory.ledger.replace_ledger", "ledger", "destructive-irreversible", _BOTH, None,
           "records", "atomic-replace", ["memory.compact.compact", "memory.restore_vault._apply_restore",
                                                "memory.rescrub.run"]),
    _entry("index-schema", "memory.index._build_schema", "derived-index", "reversible-mutation", _BOTH, None,
           "rows", "derived-rebuild", ["memory.index.rebuild"], schema_cutover=True),
    _entry("index-rebuild", "memory.index.rebuild", "derived-index", "reversible-mutation", _BOTH, None,
           "rows", "derived-rebuild", ["memory.compact.compact", "memory.restore_vault._apply_restore",
                                               "memory.rescrub.run", "memory.index._heal_if_stale",
                                               "memory.capture._recover_capture_transaction"],
           schema_cutover=True),
    _entry("index-extend", "memory.index.extend", "derived-index", "reversible-mutation", _AUTO, None, "rows",
           "derived-rebuild", ["memory.capture._recover_capture_transaction"]),
    _entry("index-stale-heal", "memory.index._heal_if_stale", "derived-index", "reversible-mutation",
           _ATTENDED, None, "rows", "derived-rebuild", ["memory.index._ranked"], schema_cutover=True),
    _entry("semantic-store-connect", "memory.semantic.store._connect", "semantic-index",
           "reversible-mutation", _ATTENDED, None, "rows", "derived-rebuild",
           ["memory.semantic.store.search", "memory.semantic.store.sync"], schema_cutover=True),
    _entry("semantic-store-reconcile", "memory.semantic.store._reconcile", "semantic-index",
           "reversible-mutation", _ATTENDED, None, "rows", "derived-rebuild",
           ["memory.semantic.store.search", "memory.semantic.store.sync"]),

    # Capture cursors, lifecycle markers, and degraded-health persistence.
    _entry("capture-cursor-write", "memory.capture._write_cursor", "capture-cursor", "reversible-mutation",
           _AUTO, 1, "files", "atomic-replace", ["memory.capture._recover_capture_transaction"]),
    _entry("capture-journal-write", "memory.capture._write_capture_transaction", "restore-journal",
           "reversible-mutation", _AUTO, 1, "files", "durable-journal", ["memory.capture._capture"]),
    _entry("capture-journal-clear", "memory.capture._clear_capture_transaction", "restore-journal",
           "reversible-mutation", _AUTO, 1, "files", "durable-journal",
           ["memory.capture._recover_capture_transaction"]),
    _entry("capture-journal-recover", "memory.capture._recover_capture_transaction", "ledger",
           "durable-append", _AUTO, None, "records", "durable-journal", ["memory.capture._capture"]),
    _entry("capture-lock-create", "memory.capture._acquire_lock", "lifecycle-marker",
           "reversible-mutation", _BOTH, 1, "files", "compare-and-set",
           ["memory.capture._capture", "memory.compact.compact", "memory.forget._write_control",
            "memory.pins.add", "memory.restore_vault._apply_restore", "memory.rescrub.run"]),
    _entry("migration-window-open", "memory.capture.open_migration_window", "lifecycle-marker",
           "reversible-mutation", _ATTENDED, 1, "files", "atomic-replace", [".engine/tools/module_manager.py"]),
    _entry("migration-window-close", "memory.capture.close_migration_window", "lifecycle-marker",
           "reversible-mutation", _ATTENDED, 1, "files", "none", [".engine/tools/module_manager.py"]),
    _entry("migration-window-reap", "memory.capture.clear_orphaned_migration_locked", "lifecycle-marker",
           "reversible-mutation", _BOTH, 1, "files", "none", ["memory.compact.compact"]),
    _entry("capture-failure-history", "memory.capture._append_failure_history", "degraded-health",
           "reversible-mutation", _AUTO, 20, "status-records", "best-effort-diagnostic",
           ["memory.capture._write_capture_status"]),
    _entry("capture-status", "memory.capture._write_capture_status", "degraded-health", "reversible-mutation",
           _AUTO, 1, "status-records", "best-effort-diagnostic",
           ["memory.capture._capture", "memory.capture._recover_capture_transaction"]),
    _entry("close-findings-record", "close._write_record", "degraded-health", "reversible-mutation", _BOTH,
           None, "status-records", "atomic-replace", ["close.record_finding", "close.dispose", "close._bump_blocks"]),
    _entry("close-findings-clear", "close.clear", "degraded-health", "destructive-irreversible", _BOTH, 1,
           "files", "best-effort-diagnostic", ["close.handler", "close.main"]),
    _entry("close-findings-promote", "close._promote", "tracked-finding", "reversible-mutation", _AUTO, 1,
           "status-records", "best-effort-diagnostic", ["close.handler"]),
    _entry("boot-refused-cursor-finding", "boot.emit_refused_cursor_finding", "tracked-finding",
           "durable-append", _AUTO, 1, "status-records", "best-effort-diagnostic", ["boot.read_state"]),
    _entry("hook-runtime-health-marker", "hook-runner.runtime-health-marker", "degraded-health",
           "reversible-mutation", _AUTO, 1, "status-records", "best-effort-diagnostic",
           [".engine/tools/hook-runner.sh"],
           code_referent=".engine/tools/hook-runner.sh:runtime-health.marker"),
    _entry("hook-crash-debug", "hooks._record_crash_debug", "degraded-health", "durable-append", _AUTO, 1,
           "status-records", "best-effort-diagnostic", ["hooks.run_hook"]),
    _entry("hook-fail-open-promote", "hooks._do_promote_fail_open", "tracked-finding",
           "reversible-mutation", _AUTO, 1, "status-records", "best-effort-diagnostic",
           ["hooks._promote_fail_open"]),
    _entry("telemetry-finding-emit", "telemetry.emit_finding", "tracked-finding", "reversible-mutation",
           _BOTH, 1, "status-records", "best-effort-diagnostic",
           ["boot.emit_refused_cursor_finding", "hooks._do_promote_fail_open"]),
    _entry("alarm-ledger-write", "boot_alarm_ledger._write", "degraded-health", "reversible-mutation",
           _AUTO, None, "status-records", "atomic-replace", ["boot_alarm_ledger.decide"]),
    _entry("alarm-ledger-lock-create", "boot_alarm_ledger._acquire", "lifecycle-marker",
           "reversible-mutation", _AUTO, 1, "files", "compare-and-set", ["boot_alarm_ledger.decide"]),
    _entry("accepted-metadata-write", "accepted_hook_dispatch._atomic_json", "lifecycle-marker",
           "reversible-mutation", _BOTH, 1, "status-records", "atomic-replace",
           ["accepted_hook_dispatch.activate", "accepted_hook_dispatch._materialize"]),
    _entry("accepted-lock-create", "accepted_hook_dispatch._exclusive_lock", "lifecycle-marker",
           "reversible-mutation", _BOTH, 1, "files", "compare-and-set",
           ["accepted_hook_dispatch.activate", "accepted_hook_dispatch._materialize"]),
    _entry("checkout-preference-write", "checkout_auto_update._atomic_write", "project-repository",
           "reversible-mutation", _ATTENDED, 1, "files", "atomic-replace",
           ["checkout_auto_update.set_preference"]),
    # Backup pointer, lifecycle metadata, and remote vault writers.
    _entry("backup-pointer-write", "memory.backup_vault.write_pointer", "backup-pointer",
           "reversible-mutation", _ATTENDED, 1, "files", "atomic-replace", ["memory.backup_vault.setup"]),
    _entry("backup-status-write", "memory.backup_vault._record_state", "degraded-health",
           "reversible-mutation", _BOTH, 1, "status-records", "best-effort-diagnostic",
           ["memory.backup_vault.push_now"]),
    _entry("migration-stamp-write", "memory.backup_vault.write_migration_stamp", "lifecycle-marker",
           "reversible-mutation", _ATTENDED, 1, "files", "atomic-replace",
           ["memory.backup_vault.snapshot_for_migration"]),
    _entry("migration-stamp-clear", "memory.backup_vault.clear_migration_stamp", "lifecycle-marker",
           "reversible-mutation", _ATTENDED, 1, "files", "none",
           ["module_manager.main", "memory.restore_vault.restore_pre_migration"]),
    _entry("vault-files-push", "memory.backup_vault._push_files", "remote-vault", "reversible-mutation", _BOTH,
           None, "files", "remote-ref-compare-and-set", ["memory.backup_vault._publish_snapshot"]),
    _entry("vault-blob-create", "memory.backup_vault._create_blob", "remote-vault", "reversible-mutation",
           _BOTH, 1, "files", "remote-ref-compare-and-set", ["memory.backup_vault._build_commit"]),
    _entry("vault-commit-build", "memory.backup_vault._build_commit", "remote-vault", "reversible-mutation",
           _BOTH, None, "files", "remote-ref-compare-and-set", ["memory.backup_vault._push_files",
                                                                  "memory.backup_vault.snapshot_for_migration"]),
    _entry("vault-snapshot-publish", "memory.backup_vault._publish_snapshot", "remote-vault",
           "reversible-mutation", _BOTH, None, "files", "remote-ref-compare-and-set",
           ["memory.backup_vault.push_now"]),
    _entry("vault-tag-create", "memory.backup_vault._create_tag", "remote-git-ref", "reversible-mutation",
           _ATTENDED, 1, "files", "remote-ref-compare-and-set", ["memory.backup_vault.snapshot_for_migration"]),
    _entry("vault-tag-delete", "memory.backup_vault._delete_tag", "remote-git-ref",
           "destructive-irreversible", _ATTENDED, 1, "files", "backup-snapshot",
           ["memory.backup_vault._prune_snapshots"]),
    _entry("project-pointer-commit", "memory.backup_vault.commit_pointer_to_project", "project-repository",
           "reversible-mutation", _ATTENDED, 1, "files", "compare-and-set", ["memory.backup_vault.setup"]),
    _entry("vault-destination-bind", "memory.backup_vault._bind_destination", "remote-vault",
           "reversible-mutation", _ATTENDED, 1, "repositories", "compare-and-set", ["memory.backup_vault.setup"]),
    _entry("vault-readme-seed", "memory.backup_vault._seed_readme", "remote-vault", "reversible-mutation",
           _ATTENDED, 1, "files", "compare-and-set", ["memory.backup_vault.setup"]),

    # Restore journal, rollback, cleanup, and actual multi-artifact restore.
    _entry("restore-journal-write", "memory.restore_vault._write_restore_transaction", "restore-journal",
           "reversible-mutation", _BOTH, 1, "files", "durable-journal",
           ["memory.restore_vault._apply_restore", "memory.restore_vault._complete_restore_transaction"]),
    _entry("restore-journal-complete", "memory.restore_vault._complete_restore_transaction", "restore-journal",
           "reversible-mutation", _BOTH, 1, "files", "durable-journal", ["memory.restore_vault._apply_restore"]),
    _entry("restore-prior-set", "memory.restore_vault._restore_prior_set", "ledger",
           "destructive-irreversible", _BOTH, None, "files", "durable-journal",
           ["memory.restore_vault.reconcile_interrupted_restore"], schema_cutover=True),
    _entry("restore-orphan-cleanup", "memory.restore_vault._cleanup_orphan_restore_staging_locked",
           "restore-journal", "destructive-irreversible", _BOTH, None, "files", "durable-journal",
           ["memory.restore_vault.reconcile_interrupted_restore", "memory.restore_vault._apply_restore"]),
    _entry("restore-apply", "memory.restore_vault._apply_restore", "ledger", "destructive-irreversible",
           _ATTENDED, None, "records", "durable-journal", ["memory.restore_vault.restore_now",
                                                             "memory.restore_vault.restore_pre_migration"],
           schema_cutover=True),
    _entry("resurrection-finding", "memory.restore_vault.surface_resurrection", "tracked-finding",
           "durable-append", _BOTH, 1, "status-records", "best-effort-diagnostic",
           ["memory.restore_vault.detect_restore_offer", "memory.restore_vault.detect_migration_revert"]),
    _entry("saved-belief-temp-projection", "memory.restore_vault._project_beliefs", "ephemeral-staging",
           "reversible-mutation", _BOTH, 1, "files", "none", ["memory.restore_vault.read_saved_memory"]),
    _entry("restore-quiet-remove", "memory.restore_vault._quiet_remove", "restore-journal",
           "destructive-irreversible", _BOTH, 1, "files", "durable-journal",
           ["memory.restore_vault._project_beliefs", "memory.restore_vault._apply_restore"]),

    # Erasure proposal and compaction staging writers.
    _entry("erasure-proposal-write", "memory.erase.write_proposal", "erasure-proposal",
           "reversible-mutation", _ATTENDED, 1, "files", "operator-merged-consent", ["memory.erase.request"]),
    _entry("erasure-pr-open", "memory.erase._open_erasure_pr", "project-repository", "reversible-mutation",
           _ATTENDED, None, "files", "operator-merged-consent", ["memory.erase.request"]),
    _entry("compaction-temp-write", "memory.compact._write_compacted_temp", "ledger",
           "destructive-irreversible", _BOTH, None, "records", "atomic-replace", ["memory.compact.compact"]),
    _entry("compaction-temp-reap", "memory.compact._reap_temps", "ledger", "destructive-irreversible",
           _BOTH, None, "files", "atomic-replace", ["memory.compact.compact"]),
    _entry("semantic-passages-drop", "memory.rescrub._drop_semantic_passages", "semantic-index",
           "destructive-irreversible", _ATTENDED, None, "rows", "derived-rebuild", ["memory.rescrub.run"]),
)


AUTOMATIC_ENTRYPOINTS = MappingProxyType({
    ".engine/tools/close.py": (
        "automatic-close-operation", "automatic-capture", "capture-transaction", "close-findings-record", "close-findings-clear",
        "close-findings-promote",
    ),
    ".engine/tools/boot.py": (
        "automatic-boot-operation", "automatic-restore-reconcile", "automatic-checkout-catch-up",
        "automatic-live-session", "automatic-alarm-presentation", "automatic-first-run-marker-consume",
        "boot-refused-cursor-finding",
    ),
    ".engine/tools/memory/compact.py": ("automatic-compaction",),
    ".engine/tools/memory/erasure_observer.py": ("automatic-erasure-observer",),
    ".engine/tools/memory/backup_vault.py": ("automatic-backup",),
})

# The accepted dispatcher and shared hook harness wrap every configured automatic target, so their durable
# cache/lock/health effects are one common layer rather than duplicated under all five scripts.
AUTOMATIC_COMMON_EFFECTS = (
    "accepted-dispatch-operation", "accepted-tree-materialize", "accepted-metadata-write",
    "accepted-lock-create", "automatic-hook-harness", "hook-runtime-health-marker", "hook-crash-debug",
    "hook-fail-open-promote", "telemetry-finding-emit",
)

# Public/read-shaped/composite entry points often reach a writer through one or more helpers.  A direct-write
# AST census cannot see those paths, so keep their closed transitive inventory as data too.  S03 consumes the
# registry ids, not Python call names; changing a helper chain without updating this map therefore fails the
# coverage tests before authority can be minted for an incomplete operation.
TRANSITIVE_BOUNDARIES = MappingProxyType({
    "accepted_hook_dispatch.dispatch": (
        "accepted-tree-materialize", "accepted-metadata-write", "accepted-lock-create",
    ),
    "boot.handler": (
        "automatic-checkout-catch-up", "automatic-restore-reconcile",
        "automatic-live-session", "automatic-alarm-presentation", "automatic-first-run-marker-consume",
        "boot-refused-cursor-finding",
    ),
    "close.handler": (
        "automatic-capture", "capture-transaction", "close-findings-record", "close-findings-clear",
        "close-findings-promote",
    ),
    "hooks.run_hook": (
        "hook-crash-debug", "hook-fail-open-promote", "telemetry-finding-emit",
    ),
    "memory.mcp_server.main": (
        "attended-pin-add", "attended-withhold", "attended-restore-withheld",
        "attended-keyword-mcp-search", "attended-semantic-mcp-search",
        "read-memory-health", "read-recall-window", "read-pins", "read-withheld",
    ),
    "memory.mcp_server.search": (
        "attended-keyword-mcp-search", "attended-keyword-search-heal", "index-stale-heal", "index-rebuild",
        "index-schema",
    ),
    "memory.mcp_server.recall_by_meaning": (
        "attended-semantic-mcp-search", "attended-semantic-search-reconcile", "semantic-store-connect",
        "semantic-store-reconcile",
    ),
    "memory.restore_vault.read_saved_memory": (
        "attended-saved-memory-projection", "saved-belief-temp-projection", "restore-quiet-remove",
    ),
    # Operator withhold/restore and the merged-erasure observer each reach their ledger writers through an
    # UNREGISTERED helper (`memory.forget._write_control`, `memory.compact.enact_erasure`), so the callers-edge
    # leg of the closure — which bridges only when a nested entry names the ROOT writer directly — never fires
    # for them the way it does for pin-add (whose nested entries name `memory.pins.add` outright). Naming the
    # exact ids each root consumes keeps the closure honest without widening it: withhold and restore both run
    # `_write_control`, which takes the capture lock and writes the marker under a bumped index epoch; the
    # erasure observer runs `enact_erasure`, which takes the capture lock and appends (no epoch bump).
    "memory.forget.withhold": (
        "capture-lock-create", "ledger-append", "ledger-index-epoch",
    ),
    "memory.forget.restore": (
        "capture-lock-create", "ledger-append", "ledger-index-epoch",
    ),
    "memory.erasure_observer.enact_from_merged_prs": (
        "capture-lock-create", "ledger-append",
    ),
})


def document() -> dict:
    """A JSON-schema-ready copy; callers cannot mutate the canonical tuple."""
    return {"schema_version": SCHEMA_VERSION, "entries": copy.deepcopy(list(REGISTRY))}


def entry_by_id(entry_id: str) -> MappingProxyType:
    matches = [entry for entry in REGISTRY if entry["id"] == entry_id]
    if len(matches) != 1:
        raise MutationContractError(f"unknown or ambiguous mutation registry entry: {entry_id}")
    return MappingProxyType(matches[0])


def classify(*, writer: str, target_kind: str, effect_class: str, invocation_mode: str,
             measured_cardinality: int, schema_cutover: bool = False) -> MappingProxyType:
    """Resolve exactly one declared writer and refuse any widening before authority is minted."""
    matches = [entry for entry in REGISTRY if entry["writer"] == writer]
    if len(matches) != 1:
        raise MutationContractError(f"unknown or ambiguous persistent writer: {writer}")
    entry = matches[0]
    if target_kind != entry["target_kind"]:
        raise MutationContractError(f"writer {writer} is not registered for target {target_kind}")
    if effect_class != entry["effect_class"]:
        raise MutationContractError(f"writer {writer} is not registered for effect {effect_class}")
    if invocation_mode not in entry["allowed_invocation_modes"]:
        raise MutationContractError(f"writer {writer} is not registered for {invocation_mode} invocation")
    if (not isinstance(measured_cardinality, int) or isinstance(measured_cardinality, bool)
            or measured_cardinality < 0):
        raise MutationContractError("measured cardinality must be a non-negative integer")
    maximum = entry["declared_cardinality"]["maximum"]
    if maximum is not None and measured_cardinality > maximum:
        raise MutationContractError(
            f"writer {writer} measured cardinality {measured_cardinality} exceeds declared maximum {maximum}"
        )
    if schema_cutover and not entry["schema_cutover"]:
        raise MutationContractError(f"writer {writer} is not registered for a schema cutover")
    if invocation_mode == "automatic" and _needs_attendance(entry):
        # ATTENDED-ONLY EVEN WHEN QUALIFIED. In PR StarshipSuperjam/engine-template#1148 a background lifecycle hook classified 99.9% of live
        # records as retired, and every ordinary safeguard was satisfied — the code ran, the state was
        # consistent, the effect was registered. What was missing was a person. A rewrite of the record itself
        # is not something a hook gets to do while nobody is looking, so the automatic caller must degrade to
        # a warning and let an attended run enact it.
        raise MutationContractError(
            f"writer {writer} rewrites canonical memory and runs only when someone is attending; "
            f"this automatic invocation must proceed without mutating"
        )
    return MappingProxyType(entry)


_RECORD_TARGETS = frozenset({"ledger", "ledger-metadata"})
# An effect that declares this as its recovery story is saying, in the registry, that what stands behind it is
# a snapshot someone took first — so someone has to be there to take it.
#
# `operator-merged-consent` deliberately is NOT here, and the deliverable review is why. Requiring attendance
# for it looked symmetrical and was a regression: compaction is the ONLY thing that physically enacts an
# erasure the operator approved, its only automatic trigger is the PreCompact hook, and refusing that trigger
# meant a merged erasure was never carried out — not deleted, and (since nothing filters erased records out of
# recall) not even hidden. Consent honoured but not executed is its own defect, and it is worse than the one
# the rule was closing. The operator's role in `operator-merged-consent` is to CONSENT, and they already did
# that at the merge; standing over the enactment adds nothing. What actually protects that rewrite is
# `compact._recovery_not_ready`, which refuses the destructive pass unless the recovery copy is current.
_ATTENDED_RECOVERIES = frozenset({"backup-snapshot"})


def _needs_attendance(entry) -> bool:
    """Whether this effect may be enacted only by an attended invocation, qualified or not.

    Narrow on purpose. It is the intersection of three things already declared in the registry: the target is
    the record itself, the effect is destructive-irreversible, and the recovery story is a snapshot someone
    took first. That is the wholesale rescrub — the exact shape of PR StarshipSuperjam/engine-template#1148's
    near-loss, where a background lifecycle pass rewrote live records and every other safeguard was satisfied.

    Deliberately NOT included, because each has its own answer and refusing them would break something real:
    appends (capture must keep working unattended), journal-driven recovery such as reconciling an interrupted
    restore (a repair back to a known-good state, and stalling it strands the writer-pause marker),
    derived-index rebuilds (regenerated from the ledger, and refusing them means no recall), and compaction
    (see `_ATTENDED_RECOVERIES` — attendance there stopped a merged erasure from ever being enacted).
    """
    return (entry["target_kind"] in _RECORD_TARGETS
            and entry["effect_class"] == "destructive-irreversible"
            and entry["recovery_requirement"] in _ATTENDED_RECOVERIES)


# ---- what an UNQUALIFIED session may still do ------------------------------------------------------
#
# StarshipSuperjam/engine-template#1151's rule is that candidate code never authors canonical memory. It is
# not "candidate code touches nothing", and StarshipSuperjam/engine-template#1153 shipped the second reading: every registered writer failed
# closed without an accepted execution context, which took reads, health, diagnostics and Build entry down
# with the thing it meant to protect. The tier below is the first reading, enumerated over what each effect
# can actually cost.
#
# A target is DEGRADED-ALLOWED when losing or corrupting it costs no memory:
#   * degraded-health / tracked-finding — status records and diagnostics. Refusing these makes the engine
#     unable to report that it is degraded, which is the worst possible moment to go quiet.
#   * lifecycle-marker / ephemeral-staging — markers, locks and caches. Session-scoped or rebuildable.
#
# derived-index and semantic-index were in this set and are NOT any more. The reasoning that put them here —
# "regenerated from the ledger by construction, so they are only a search accelerator" — is contradicted by
# the index itself: it stores each record's whole JSON body (`entries.record_json`) and `index.query` hydrates
# results straight out of that column whenever the stamped generation matches. So the index is not a pointer
# INTO the record, it is a second copy OF it, and letting unqualified code rewrite it would let candidate code
# put text it invented in front of every future session's recall without ever touching the ledger. That is the
# rule of StarshipSuperjam/engine-template#1151 defeated by the accelerator rather than by the record.
#
# Refusing it costs slower recall and nothing else, which is the trade the availability-first design was
# always willing to make: `_heal_if_stale` swallows a refused rebuild, `_index_is_current` then reads false,
# and both `query` and `_ranked` fall through to the full ledger scan, which computes the same bm25 over the
# same matched set. Reads keep working; a later qualified session rebuilds the fast path.
#
# Everything else is the record itself or the machinery that can destroy it — the ledger, its metadata and
# generation stamp, the capture cursor, the backup pointer and remote vault, erasure proposals, exports, the
# project repository — and stays refused until qualification. Capture loses nothing by that refusal: the
# transcripts are the durable input, and the drain authors from them in qualified code.
#
# restore-journal stays refused, and the deliverable review asked the right question about it: reconciling an
# interrupted restore is what lifts a marker that PAUSES every memory writer, so refusing the repair looks
# like stranding the machine in a state only qualification can leave. It is not, and the reason is that the
# pause has nothing left to block — on an unqualified machine every one of those writers is ALREADY refused by
# this tier. The pause costs nothing until qualification arrives, and qualification lifts it. Allowing the
# repair would not help either: its real work is `restore-prior-set` rewriting the ledger, which is precisely
# what candidate code must never do.
DEGRADED_ALLOWED_TARGETS = frozenset({
    "degraded-health", "tracked-finding", "lifecycle-marker", "ephemeral-staging",
})

# The RECALL boundaries, allowed by id because their target says more about them than they deserve. Each is
# registered against an index target as a reversible mutation — not because the call writes anything, but
# because it MAY heal a stale index on the way past. Refusing them by target takes recall itself down, which
# is exactly StarshipSuperjam/engine-template#1153's failure; the launch-contract test caught precisely that
# when this set named only the two MCP tools and left `memory.index.search` underneath them refused.
#
# Allowing them costs nothing, because every writer that actually REBUILDS the copy is a nested call re-tiered
# on its own registry entry — `index-rebuild`, `index-extend`, `index-schema`, `index-stale-heal`,
# `attended-semantic-sync`, `semantic-store-reconcile` all still refuse, `_heal_if_stale` swallows that, and
# the read falls through to the ledger scan. The read happens; the second copy is not rewritten, which is the
# whole point.
#
# `semantic-store-connect` was in this set and is NOT any more. It was added on the reasoning that "opening
# the store is what a read needs, and refusing it stops nothing" — and that was factually wrong about what
# `_connect` does: on a schema or word-table fingerprint change it runs `DROP TABLE IF EXISTS passages`. So
# unqualified code could DESTROY the passage store and then be refused the only writer that can refill it,
# with an engine upgrade as a plausible non-adversarial trigger, since the first session after one is
# unqualified by construction. The read it was protecting is handled properly instead: `store.search` reports
# itself UNAVAILABLE when it cannot open or reconcile, and the recall tool says so in those words.
DEGRADED_ALLOWED_ENTRY_IDS = frozenset({
    "attended-keyword-mcp-search", "attended-keyword-search-heal",
    "attended-semantic-mcp-search", "attended-semantic-search-reconcile",
})

# Refusals an operator will actually meet, in their own words. Anything not named here gets the generic line.
DEGRADED_REFUSAL_GUIDANCE = MappingProxyType({
    "attended-pin-add": (
        "I can't pin that yet: this session isn't qualified to write memory, and a pin is standing "
        "instruction from you — I won't stash it now and replay it later as if you had said it then. "
        "Qualification converges by itself at a session start that can reach GitHub; ask me again then and "
        "it will stick."
    ),
    "attended-withhold": (
        "I can't set that aside yet: this session isn't qualified to write memory. The note is still "
        "there and still findable, nothing changed, and "
        "nothing was registered — if you asked in order to erase it, not even the first step has happened. "
        "Qualification converges by itself at the next session start that can reach GitHub; ask me again "
        "then and this will stick. Erasing for good is separate and yours end to end: you run it in a "
        "terminal, and it takes effect when you merge the pull request it opens."
    ),
    "attended-restore-withheld": (
        "I can't restore that yet: this session isn't qualified to write memory. The note is still set aside "
        "and nothing was lost; ask again once qualification has converged, which happens by itself at a "
        "session start that can reach GitHub."
    ),
})


# A destructive effect stays refused even on an otherwise degraded-allowed target — with one narrow exception:
# clearing a diagnostic or a one-shot marker, where the "destruction" is deleting a status file. Dropping
# semantic passages is regenerable in principle but is still a bulk delete, and unqualified code has no
# business performing one.
_DESTRUCTIVE_DEGRADED_TARGETS = frozenset({"degraded-health", "lifecycle-marker"})


def degraded_disposition(entry) -> str:
    """`allow` or `refuse` for one registry entry running with NO accepted execution context."""
    if entry["effect_class"] == "semantic-read":
        return "allow"
    if entry["id"] in DEGRADED_ALLOWED_ENTRY_IDS:
        return "allow"
    if entry["target_kind"] not in DEGRADED_ALLOWED_TARGETS:
        return "refuse"
    if entry["effect_class"] == "destructive-irreversible":
        return "allow" if entry["target_kind"] in _DESTRUCTIVE_DEGRADED_TARGETS else "refuse"
    return "allow"


def degraded_refusal(entry) -> str:
    """The honest, actionable sentence a refused unqualified writer answers with."""
    named = DEGRADED_REFUSAL_GUIDANCE.get(entry["id"])
    if named:
        return named
    return (
        f"{entry['writer']} needs this session to be qualified to write memory, and it isn't yet. Nothing was "
        f"changed. Qualification converges by itself at a session start that can reach GitHub; reading and "
        f"recall work in the meantime."
    )


def degraded_tiering() -> dict:
    """Every registry id in exactly one tier — the shape a completeness test asserts over."""
    tiers = {"allow": [], "refuse": []}
    for entry in REGISTRY:
        tiers[degraded_disposition(entry)].append(entry["id"])
    return {key: sorted(value) for key, value in tiers.items()}


# Writers the census SEES but the registry deliberately does not govern, named here so the inventory stays
# closed and the exemption stays visible rather than becoming an unexplained census gap.
#
# The session stance marker is a per-session file in the OS temp directory. It is not persistent memory: it
# holds no record, survives no session, and its absence resolves to the safe floor (Explore) by construction.
# Governing it as a persistent mutation is what locked the operator out of Build in StarshipSuperjam/engine-template#1153 — the typed
# `/engine-start` verb writes this marker, so an unqualified session could not enter Build at all, which is a
# refusal that protects nothing and removes the operator's own control. The marker's integrity is instead
# carried by its hardened write in ``modes`` (no symlink, 0600, atomic replace).
SESSION_EPHEMERAL_WRITERS = frozenset({
    "modes.set_stance", "modes.clear_stance", "modes._harden_marker_write",
})

_WRITE_FLAGS = frozenset({"O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND"})
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SQL_WRITE_PREFIXES = frozenset({
    "ALTER", "CREATE", "DELETE", "DROP", "INSERT", "PRAGMA", "REINDEX", "REPLACE", "UPDATE", "VACUUM",
})
_GIT_WRITE_VERBS = frozenset({
    "add", "branch", "checkout", "clean", "commit", "fetch", "merge", "mv", "pull", "push", "rebase",
    "reset", "restore", "rm", "switch", "tag", "update-ref", "worktree",
})
_DEMO_FUNCTION_SUFFIX = "demo"

def _constant_string(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _contains_write_flag(node) -> bool:
    return any(isinstance(part, ast.Attribute) and part.attr in _WRITE_FLAGS for part in ast.walk(node))


def _sql_is_write(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    statement = value.lstrip().split(None, 1)
    return bool(statement and statement[0].upper() in _SQL_WRITE_PREFIXES)


def _constant_strings(node) -> tuple[str, ...]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return ()
    values = tuple(_constant_string(item) for item in node.elts)
    return values if all(value is not None for value in values) else ()


def _call_is_write(call: ast.Call) -> bool:
    name = call.func.id if isinstance(call.func, ast.Name) else (
        call.func.attr if isinstance(call.func, ast.Attribute) else "")
    owner = call.func.value.id if isinstance(call.func, ast.Attribute) and isinstance(
        call.func.value, ast.Name) else ""
    if name in {"replace", "rename", "remove", "unlink"} and owner == "os":
        return True
    if name in {"rmtree", "move", "copy", "copy2", "copyfile"} and owner == "shutil":
        return True
    if name in {"write_text", "write_bytes"}:
        return True
    # Check os.open before the generic open/fdopen branch.  Both have the same terminal name, and the old
    # ordering silently treated an os.open(flags) call as a text open(mode) call and missed every flag writer.
    if name == "open" and owner == "os":
        return len(call.args) > 1 and _contains_write_flag(call.args[1])
    if name == "write" and owner == "os":
        return True
    if name in {"open", "fdopen"}:
        mode = _constant_string(call.args[1]) if len(call.args) > 1 else None
        for keyword in call.keywords:
            if keyword.arg == "mode":
                mode = _constant_string(keyword.value)
        return isinstance(mode, str) and any(flag in mode for flag in "wax+")
    if name in {"execute", "executemany", "executescript"} and call.args:
        return _sql_is_write(_constant_string(call.args[0]))
    if name in {"_send", "_deadline_send"} and len(call.args) > 1:
        return _constant_string(call.args[1]) in _WRITE_METHODS
    if name in {"run", "Popen", "check_call", "check_output"} and owner == "subprocess" and call.args:
        argv = _constant_strings(call.args[0])
        return bool(argv and argv[0] == "git" and any(part in _GIT_WRITE_VERBS for part in argv[1:]))
    return False


def _calls_outside_nested_functions(function):
    stack = list(function.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Call):
            yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            stack.append(child)


def discover_direct_writers(path: str, *, module: str | None = None) -> set[str]:
    """Static coverage leg for direct file/ref writes; demo/test-only functions are deliberately excluded."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    prefix = module or f"memory.{os.path.splitext(os.path.basename(path))[0]}"
    found = set()
    for node in tree.body:
        if (not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                or node.name.startswith(("_demo", "_fixture"))
                or node.name.endswith(_DEMO_FUNCTION_SUFFIX)):
            continue
        if any(_call_is_write(call) for call in _calls_outside_nested_functions(node)):
            found.add(f"{prefix}.{node.name}")
    return found


def coverage_failures(paths) -> list[str]:
    registered = {entry["writer"] for entry in REGISTRY}
    found = set()
    for item in paths:
        path, module = item if isinstance(item, tuple) else (item, None)
        found.update(discover_direct_writers(path, module=module))
    return sorted(found - registered - SESSION_EPHEMERAL_WRITERS)


def automatic_coverage_failures(configured_scripts) -> list[str]:
    """Any configured memory-bearing automatic script with no canonical operation entry."""
    return sorted(set(configured_scripts) - set(AUTOMATIC_ENTRYPOINTS))
