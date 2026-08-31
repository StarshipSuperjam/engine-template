#!/usr/bin/env python3
"""Removing the engine entirely, as a typed transaction.

THE ORDERING IS THE WHOLE DESIGN, and it is unusual for a reason: this transaction deletes the code that
is running it. `remove_engine` takes protection off the branch, reverses every wire, deletes `.engine/`
including this file, and only then opens the pull request that proposes the deletion — because the
deletion has to be IN the commit the pull request carries.

So everything the phases after the delete will need must be resident in memory BEFORE it. Python keeps
imported modules; it does not keep deleted files. The envelope loader is built for exactly this (it reads
its schema once at import), and this module touches it before the delete so the receipt can still be
validated and rendered afterwards.

WHY `run` REFUSES HERE. The operator's ruling: an upgrade can be rolled back, an engine deletion is a
harder recovery, so its start stays a deliberate act of theirs rather than something a session can reach.
`transaction.py run engine-remove` refuses unconditionally and names the typed command; this adapter's
phases are driven by that command. The digest handle still binds what was previewed.

AND WHAT THE PULL REQUEST CANNOT CARRY. The protection change is a repository setting, applied during
apply and outside any pull request. That is disclosed here in plain words rather than left for a reader to
infer from the absence of a diff.
"""
from __future__ import annotations

import module_manager
import transaction
import transaction_envelope as envelope  # imported HERE so its schema is resident before the delete
import transaction_handoff as handoff


class RemoveEngine(transaction.Adapter):
    operation = "engine-remove"

    def _protection_choice(self, args) -> str:
        flags = [a for a in (getattr(args, "rest", None) or []) if a.startswith("--")]
        # The domain's own vocabulary (`keep` / `drop`), not a second set of words for the same choice.
        if "--remove-protection" in flags:
            return "drop"
        if "--keep-protection" in flags:
            return "keep"
        raise transaction.TransactionRefused(
            "protection-choice-required",
            "Removing the engine changes your branch protection, and which way is your decision: keep the "
            "protection rule in place, or remove it with the engine.",
            ["Re-run naming one: `--keep-protection` or `--remove-protection`."])

    def inspect(self, args) -> dict:
        state = handoff.working_tree_state()
        engine = None
        try:
            import module_coherence
            engine = (module_coherence.load_engine_manifest() or {}).get("engine_release")
        except Exception:  # noqa: BLE001 — an unreadable manifest is reported, never fatal to a read
            engine = None
        return {
            "summary": "This engine ({0}) would be removed from the project entirely."
                       .format(engine or "version unreadable"),
            "fingerprints": {"head": state["head"], "engine_release": engine or ""},
        }

    def plan(self, args, facts: dict) -> dict:
        choice = self._protection_choice(args)
        consequences = [
            "Removes the engine from this project entirely — its files, its settings, and everything it "
            "could do for you.",
            "Your own project files, code and content are untouched.",
        ]
        if choice == "drop":
            consequences.append(
                "Removes your main branch's protection rule along with the engine. Until you set up "
                "protection yourself, changes can reach that branch without review.")
        else:
            consequences.append(
                "Leaves your main branch's protection rule in place, but the engine's own checks come off "
                "it — the rule stays, the engine's part of it does not.")
        consequences.append(
            "The protection change happens when this runs, NOT when you merge: a repository setting "
            "cannot ride in a pull request. Between that moment and your merge, the branch is in the "
            "state described above while the deletion is still only proposed.")
        return {
            "inputs": {"protection": choice},
            "choices": [{
                "id": "protection",
                "chosen": choice,
                "options": ["keep", "drop"],
                "consequence": "whether your branch keeps its protection rule after the engine is gone",
            }],
            "consequences": consequences,
            "effects": [
                {"kind": "external-settings",
                 "description": "branch protection is {0}".format(
                     "removed" if choice == "drop" else "kept, minus the engine's checks"),
                 "reversible": True},
                {"kind": "tracked-files", "description": "the entire .engine tree and the engine's "
                                                          "entries in shared files", "reversible": True},
                {"kind": "capability", "description": "every engine capability is retired"},
            ],
            "reversibility": "reverted-pull-request",
            "manual_steps": [
                "Merge the pull request this opens — until you do, the engine's files are still on your "
                "main branch.",
                "If you removed protection and want it back, set it up again yourself.",
            ],
        }

    def apply(self, args, plan: dict) -> dict:
        # BASE CURRENCY FIRST, before this deletes anything: a wrong, behind-origin or diverged base refuses
        # here — and refusing costs nothing, since nothing has been touched yet. The non-refusing note is
        # carried onto the result for the handoff. (This adapter's `apply` is belt-and-braces: production
        # removal runs through `module_manager.remove_engine`, wired at the operator-typed door; `transaction
        # .py run engine-remove` refuses outright. Both operator-facing entries carry the same check.)
        currency = handoff.refuse_if_stale_base()
        # PRELOAD, then delete. Everything the later phases need must be resident now: the envelope schema
        # (already read at import), this module, and the handoff renderers. Nothing below may read a file
        # under .engine/ — it will not be there.
        if not envelope.SCHEMA:
            # Not an `assert`: assertions are stripped under `python -O`, and this guard has to hold in
            # every run — it is the one thing standing between a deleted tree and an unrenderable receipt.
            raise transaction.TransactionRefused(
                "envelope-not-resident",
                "The receipt machinery is not loaded, so this removal could not report what it did after "
                "deleting the engine. Nothing was changed.",
                ["Report this: it is a defect in how the removal transaction loads its own schema."])
        result = module_manager.remove_engine(choice=plan["inputs"]["protection"])
        if result.get("refused"):
            raise transaction.TransactionRefused(
                "remove-engine-refused", result.get("reason", "The engine could not be removed."),
                ["Resolve what the reason above names, then run this again."])
        result["base_currency"] = currency
        return result

    def verify(self, args, applied: dict) -> list:
        """Receipts built from what apply returned. No file is read: there is no .engine to read."""
        receipts = []
        deleted = applied.get("deleted")
        receipts.append({
            "check": "engine files removed",
            "result": "passed" if deleted else "unavailable",
            "detail": ("removed: {0}".format(", ".join(sorted(deleted))[:300]) if deleted
                       else "the removal did not report what it deleted, so this is unverified"),
        })
        protection = applied.get("de_bootstrap")
        receipts.append({
            "check": "branch protection change",
            "result": "passed" if protection else "unavailable",
            "detail": ("applied before deletion, outside the pull request" if protection
                       else "the protection change did not report back, so its outcome is unverified"),
        })
        pr = applied.get("pr")
        receipts.append({
            "check": "removal proposed for review",
            "result": "passed" if pr else "failed",
            "detail": ("the deletion is proposed as a pull request" if pr
                       else "the deletion was made locally but no pull request was opened"),
        })
        return receipts

    def handoff(self, args, applied: dict, receipts) -> dict:
        currency_line = handoff.currency_summary_line(applied.get("base_currency"))
        pr = applied.get("pr") or {}
        if not pr:
            summary = ("The engine was removed from your working copy, but the pull request that "
                       "proposes it could not be opened. The change is committed on its branch — open "
                       "the pull request yourself to complete the removal.")
            return {"kind": "manual-follow-up",
                    "summary": summary + (" " + currency_line if currency_line else "")}
        what = "The engine's removal is proposed for your review."
        if currency_line:
            what += " " + currency_line
        return handoff.pull_request_handoff(pr, what)


transaction.register(RemoveEngine())
