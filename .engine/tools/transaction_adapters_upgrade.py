#!/usr/bin/env python3
"""Updating the engine, and undoing an update, as typed transactions.

WHAT CONSENT MEANS HERE, AND WHAT IT DOES NOT. The operator's ruling: an upgrade gets the digest handle
and no new start-gate machinery, because an upgrade can be rolled back and a start gate would be friction
for what amounts to paperwork stamping. So the handle's job is narrow and worth stating plainly — it binds
WHAT is applied to what was previewed, so an update cannot quietly become a different update between
reading and applying. It says nothing about WHO started it.

The start protections are the ones that already exist and are named honestly in the runbook: the
`/engine-upgrade` skill the harness itself will not let a session invoke, and — under everything — the fact
that applying only ever opens a pull request the operator merges.

This adapter WRAPS `module_manager.upgrade_preview` / `.upgrade` / `.rollback`. Every refusal reason, every
capability disclosure and every migration rule stays in the domain; the envelope reports what it said.
"""
from __future__ import annotations

import module_manager
import transaction
import transaction_handoff as handoff


class UpgradeEngine(transaction.Adapter):
    operation = "engine-upgrade"

    def _ref(self, args):
        rest = [a for a in (getattr(args, "rest", None) or []) if not a.startswith("-")]
        return rest[0] if rest else None

    def inspect(self, args) -> dict:
        preview = module_manager.upgrade_preview(self._ref(args))
        current = preview.get("current") or preview.get("from_version")
        available = preview.get("available") or preview.get("target")
        return {
            "summary": "On {0}; {1}.".format(
                current or "an unreadable version",
                "update available: {0}".format(available) if available else "no newer version available"),
            "fingerprints": {
                "current_version": str(current or ""),
                "target_version": str(available or ""),
                "head": handoff.working_tree_state()["head"],
            },
        }

    def plan(self, args, facts: dict) -> dict:
        preview = module_manager.upgrade_preview(self._ref(args))
        if preview.get("refused"):
            raise transaction.TransactionRefused(
                "upgrade-refused", preview.get("reason", "This update cannot proceed."),
                ["Resolve what the reason above names, then check again."])
        if not (preview.get("available") or preview.get("target")):
            raise transaction.TransactionRefused(
                "already-current", "This engine is already on the newest version its update home offers.",
                ["Nothing to do."], retryable=True)

        consequences = ["Moves this engine to {0}.".format(preview.get("available") or preview.get("target")),
                        "Your own settings and saved data are kept; the engine's own files are replaced."]
        for key, label in (("capabilities_removed", "Retires a capability you have now: {0}"),
                           ("modules_added", "Turns on a capability new in this version: {0}")):
            for item in preview.get(key) or []:
                consequences.append(label.format(item if isinstance(item, str) else item.get("id", item)))
        if preview.get("data_migration"):
            consequences.append("Changes saved data, which is backed up first — the update refuses that "
                                "step if no backup is set up.")

        effects = [{"kind": "tracked-files", "description": "the engine's own files are replaced"},
                   {"kind": "shared-settings", "description": "shared-file settings are brought into line"},
                   {"kind": "review-artifact", "description": "a pull request is opened for review"}]
        if preview.get("data_migration"):
            effects.append({"kind": "saved-data", "description": "stored data is reshaped, after a backup",
                            "reversible": True})

        return {
            "inputs": {"release": preview.get("available") or preview.get("target")},
            "consequences": consequences,
            "effects": effects,
            "reversibility": "reverted-pull-request",
            "manual_steps": [
                "Type `/engine-upgrade` — the engine cannot start an update on its own.",
                "Merge the pull request it opens; until then nothing about the running engine has changed.",
            ],
        }

    def apply(self, args, plan: dict) -> dict:
        result = module_manager.upgrade(self._ref(args))
        if result.get("refused"):
            raise transaction.TransactionRefused(
                "upgrade-refused", result.get("reason", "The update could not be applied."),
                ["Resolve what the reason above names, then run this again."])
        return result

    def verify(self, args, applied: dict) -> list:
        receipts = []
        receipts.append({
            "check": "engine consistency after the update",
            "result": "passed" if applied.get("applied") and not applied.get("findings") else (
                "failed" if applied.get("findings") else "unavailable"),
            "detail": ("the rebuilt tree checks out" if applied.get("applied") and not applied.get("findings")
                       else "; ".join(str(f.get("message", ""))
                                      for f in (applied.get("findings") or []))[:300]
                       or "the update did not report a consistency result, so it is unverified"),
        })
        receipts.append({
            "check": "update proposed for review",
            "result": "passed" if applied.get("pr") else "failed",
            "detail": ("opened as a pull request" if applied.get("pr")
                       else "the update is staged but no pull request was opened; it can be finished or undone"),
        })
        return receipts

    def handoff(self, args, applied: dict, receipts) -> dict:
        pr = applied.get("pr")
        if not pr:
            return {
                "kind": "manual-follow-up",
                "summary": "The update is staged in your working copy but was not opened for review. You "
                           "can finish it by applying again, or undo it with rollback — nothing was merged.",
            }
        return handoff.pull_request_handoff(pr, "The update is proposed for your review.")


class RollbackUpgrade(transaction.Adapter):
    """Undoing an update. Three states, and only one of them is something a machine can do locally."""

    operation = "engine-upgrade-rollback"

    def inspect(self, args) -> dict:
        diagnosis = module_manager.rollback()   # bare rollback is read-only by contract
        return {
            "summary": "Undo state: {0}.".format(diagnosis.get("state", "unknown")),
            "fingerprints": {"state": str(diagnosis.get("state", "")),
                             "current_version": str(diagnosis.get("current") or "")},
        }

    def plan(self, args, facts: dict) -> dict:
        diagnosis = module_manager.rollback()
        state = diagnosis.get("state")
        if state == "none":
            raise transaction.TransactionRefused(
                "nothing-to-undo",
                "There is no half-finished update to undo here. An update you already MERGED is undone by "
                "reverting its pull request, which is a normal reviewed change — not a local reset.",
                ["If you meant a merged update, revert its pull request, then run this again to put your "
                 "saved memory back."])
        consequences = {
            "staged": ["Discards a half-finished update and puts the engine back the way it was.",
                       "Saves a recovery point of the current state first, so nothing is lost.",
                       "Refuses if you have unrelated unsaved work of your own."],
            "memory-ahead": ["Puts your saved memory back to the copy from before the update, now that the "
                             "code is back at the older version."],
            "transaction": ["Completes or reports an interrupted update-recovery transaction."],
        }.get(state, ["Undoes what can be undone locally."])
        return {
            "inputs": {"state": state},
            "consequences": consequences,
            "effects": [{"kind": "tracked-files", "description": "the engine's files return to their "
                                                                  "pre-update state", "reversible": True},
                        {"kind": "saved-data", "description": "saved memory is put back where it moved"}],
            "reversibility": "local-recovery",
        }

    def apply(self, args, plan: dict) -> dict:
        result = module_manager.rollback(confirm=True)
        if result.get("refused"):
            raise transaction.TransactionRefused(
                "rollback-refused", result.get("reason", "The undo could not complete."),
                ["Resolve what the reason above names, then run this again."])
        return result

    def verify(self, args, applied: dict) -> list:
        return [{
            "check": "engine returned to its pre-update state",
            "result": "passed" if applied.get("undone") else "unavailable",
            "detail": ("recovery point: {0}".format(applied.get("recovery_point"))
                       if applied.get("recovery_point")
                       else "the undo did not report a recovery point, so this is unverified"),
        }]

    def handoff(self, args, applied: dict, receipts) -> dict:
        if not applied.get("undone"):
            return {"kind": "manual-follow-up",
                    "summary": "Nothing was undone locally. If the update was already merged, revert its "
                               "pull request — the engine never rewrites your main line."}
        return {"kind": "local-recovery",
                "summary": "The engine is back the way it was, with a recovery point saved first.",
                "reference": str(applied.get("recovery_point") or "")}


transaction.register(UpgradeEngine())
transaction.register(RollbackUpgrade())
