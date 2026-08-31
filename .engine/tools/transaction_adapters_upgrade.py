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


def _pull_request_handoff_from_record(record: dict) -> dict:
    """The handoff for an update whose pull request is already open, built from the durable record.

    `checkout_health` accepts a `pr-opened` record carrying ANY ONE of `number`, `url` or `html_url`, so
    reading only `url` produced "proposed for your review" with nothing to click -- and an empty
    `reference`, which the envelope schema forbids. The key is omitted rather than emptied: an absent
    reference is honest, a blank one is a value that says nothing.
    """
    pr = record.get("pull_request") or {}
    reference = pr.get("url") or pr.get("html_url") or (str(pr["number"]) if pr.get("number") else "")
    result = {"kind": "pull-request",
              "summary": "The update is already proposed for your review. Merging it is what applies it; "
                         "nothing here needs re-running."}
    if reference:
        result["reference"] = str(reference)
    return result


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
                # NEVER BLANK. The envelope requires a non-blank fingerprint, and "no newer version" is an
                # ordinary answer -- already current, offline, mid-transaction -- not an error. Writing ""
                # here made `inspect engine-upgrade` die with an unhandled EnvelopeError in exactly the
                # states the summary line beside it composes prose for, so the method contradicted itself.
                "current_version": str(current or "unreadable"),
                "target_version": str(available or "none-available"),
                # `head` is "" whenever `git rev-parse HEAD` cannot answer — no git binary, an unborn
                # HEAD, a non-repository deployment. The NEVER-BLANK rule above applies to it too; the
                # previous version guarded the two version fields and let this one through, so the
                # comment overclaimed and the test written to prove it failed in a non-git tree.
                "head": handoff.working_tree_state()["head"] or "unknown",
            },
        }

    def plan(self, args, facts: dict) -> dict:
        preview = module_manager.upgrade_preview(self._ref(args))
        if preview.get("refused"):
            raise transaction.TransactionRefused(
                "upgrade-refused", preview.get("reason", "This update cannot proceed."),
                ["Resolve what the reason above names, then check again."])
        # DRIVEN OFF `status`, ENUMERATED — not off "is there a target". The previous shape asked
        # `not (available or target)` and called everything else already-current, which was wrong twice
        # over. It called the states where the preview COULD NOT LOOK "already current"; and it could
        # never fire for an engine that genuinely IS current, because `up-to-date` sets
        # `available = target_ref`, so that operator was handed a real plan and a consent handle whose
        # first consequence read "Moves this engine to <the version it is already on>".
        #
        # This is the third time in this build that a defect was "fixed" for the sites a reviewer named
        # rather than for every site, so the map below is the complete set `upgrade_preview` and
        # `plan_upgrade` can return; anything outside it refuses as unrecognised rather than being
        # guessed at.
        _CANNOT_LOOK = {
            "no-home": ("no-update-home",
                        ["Tell me the repository your engine updates from, then check again."], False),
            "transaction-incomplete": ("unfinished-update",
                                       ["See where the interrupted update got to: "
                                        "`transaction.py resume engine-upgrade`.",
                                        "Or undo it: `module_manager.py rollback --confirm`."], False),
            "inconsistent": ("engine-inconsistent",
                             ["Resolve what the reason above names, then check again."], False),
            "unreachable": ("update-home-unreachable",
                            ["Check again when the update home can be reached."], True),
            "missing-release": ("no-such-release",
                                ["Check the release name, or plan the latest instead."], False),
        }
        status = preview.get("status")
        if status in _CANNOT_LOOK:
            code, next_actions, retryable = _CANNOT_LOOK[status]
            raise transaction.TransactionRefused(
                code, preview.get("reason") or "This update could not be planned.",
                next_actions, retryable=retryable)
        if status == "up-to-date":
            raise transaction.TransactionRefused(
                "already-current", "This engine is already on the newest version its update home offers.",
                ["Nothing to do. Check again when a new version has been released."],
                # Re-running now returns this identical answer; a new release later is what changes it.
                retryable=False)
        if not (preview.get("available") or preview.get("target")):
            # A shape this version does not recognise. Refusing as unknown is the honest answer; the old
            # code reached here and asserted the engine was up to date, which it had not established.
            raise transaction.TransactionRefused(
                "preview-unrecognised",
                "The update check returned something this version does not know how to read, so no "
                "update was planned and nothing was changed.",
                ["Report this: it is a defect in the engine, not something you did wrong."])

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
        # BASE CURRENCY FIRST, before any mutation: refuse a wrong, behind-origin or diverged base, and
        # otherwise carry the (current | unverified) note onto the result for the envelope and the handoff.
        currency = handoff.refuse_if_stale_base()
        # APPLY WHAT THE PLAN NAMED. This used to pass the raw command-line operand -- `None` for the
        # ordinary "update me" case -- so `upgrade()` resolved "latest" a SECOND time, after the consent
        # handle had already been checked against the concretely resolved tag the plan recorded. A release
        # landing between those two moments meant consent given for X applied Y: the exact substitution
        # this transaction exists to prevent, on the route the runbook points at. The plan was handed to
        # this method and ignored.
        release = (plan.get("inputs") or {}).get("release")
        result = module_manager.upgrade(
            release or self._ref(args),
            base_currency_note=handoff.currency_summary_line(currency))
        if result.get("refused"):
            raise transaction.TransactionRefused(
                "upgrade-refused", result.get("reason", "The update could not be applied."),
                ["Resolve what the reason above names, then run this again."])
        result["base_currency"] = currency
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
        currency_line = handoff.currency_summary_line(applied.get("base_currency"))
        pr = applied.get("pr")
        if not pr:
            summary = ("The update is staged in your working copy but was not opened for review. You "
                       "can finish it by applying again, or undo it with rollback — nothing was merged.")
            return {"kind": "manual-follow-up",
                    "summary": summary + (" " + currency_line if currency_line else "")}
        what = "The update is proposed for your review."
        if currency_line:
            what += " " + currency_line
        return handoff.pull_request_handoff(pr, what)

    def resume(self, args):
        """Upgrade is the one adapter with a DURABLE progress record, so it does not re-plan blindly.

        `module_manager._diagnose_undo()` reads back how far an interrupted update actually got — a
        staged overlay in the working copy, a git-level transaction still in flight, or saved data left
        ahead of the code. When it names one of those, the remaining effects are NOT the whole update:
        the overlay is already written, so re-planning and applying again would re-derive work that has
        already landed. This returns what was read back plus the named recovery instead.

        When nothing is recorded (`state == "none"`) this returns None and the generic re-inspect-and-
        re-plan runs — which is the truthful answer, because then there is genuinely no interrupted
        attempt to continue.
        """
        diagnosis = module_manager._diagnose_undo()
        state = diagnosis.get("state")
        # The `staged` reading comes from the deliberately GENEROUS dirty-only predicate, which is right
        # where it is asked (offering an undo must not miss a real one) and wrong here. Resume WITHHOLDS
        # the fresh plan when it claims progress, so on an ordinary dirty working copy that generosity
        # would tell an operator with nothing staged not to apply -- StarshipSuperjam/engine-template#948's failure shape in a new
        # place. Here the narrow marker-gated reading is the honest one; every other state stands as read.
        if state == "staged" and not module_manager.staged_upgrade_announced():
            state = "none"
        if state == "none":
            return None

        # A COMPLETED update whose bookkeeping did not get cleared is NOT mid-flight. `upgrade` tolerates
        # `finish_upgrade_transaction` failing AFTER the pull request is open ("the next invocation will
        # finalize or report it"), and `_diagnose_undo` checks that record first -- so without this the
        # operator whose update actually succeeded is told applying again "would compound it".
        record = ((diagnosis.get("transaction") or {}).get("record") or {})
        if state == "transaction" and record.get("phase") == "pr-opened":
            return transaction._envelope(
                self.operation, "resume", ["inspect"], "ok",
                facts={"summary": "The interrupted update had already finished.",
                       "fingerprints": {"undo_state": str(state), "phase": "pr-opened",
                                        "current_version": str(diagnosis.get("current") or "")}},
                verification=[{
                    "check": "prior progress",
                    "result": "passed",
                    "detail": ("This update completed and its pull request is open; only the internal "
                               "bookkeeping was left behind. There is nothing to resume."),
                }],
                handoff=_pull_request_handoff_from_record(record))

        remaining = {
            "staged": ("An update is already written into this working copy but was never opened for "
                       "review. The overlay is applied; what remains is your decision to finish it or "
                       "undo it -- not re-applying it."),
            "transaction": ("An update is mid-flight at the git level. What remains is finishing or "
                            "unwinding that transaction; applying again on top of it would compound it."),
            "memory-ahead": ("The code is back on an older version while your saved data is still on the "
                             "newer one. What remains is putting the saved data back, not re-applying."),
        }.get(state)

        if remaining is None:
            # UNKNOWN STATE. The contract is explicit: a check that could not run is `unavailable`, never
            # quietly `passed`. Reporting a state this version cannot interpret as a green check is the
            # precise failure the envelope was built to prevent, so it is reported as unread.
            return transaction._envelope(
                self.operation, "resume", ["inspect"], "ok",
                facts={"summary": "Interrupted update found in an unrecognised state: {0}.".format(state),
                       "fingerprints": {"undo_state": str(state),
                                        "current_version": str(diagnosis.get("current") or "")}},
                verification=[{
                    "check": "prior progress",
                    "result": "unavailable",
                    "detail": ("This copy records an interrupted update in a state this version does not "
                               "know how to read, so how far it got could not be determined."),
                }],
                handoff={"kind": "local-recovery",
                         "summary": "Do not apply again on top of this. Undo it "
                                    "(`module_manager.py rollback --confirm`) and start from a fresh plan."})

        return transaction._envelope(
            self.operation, "resume", ["inspect"], "ok",
            facts={
                "summary": "Interrupted update found: {0}.".format(state),
                "fingerprints": {
                    "undo_state": str(state),
                    "current_version": str(diagnosis.get("current") or "unreadable"),
                    # Never blank here either — see `inspect`.
                    "head": handoff.working_tree_state()["head"] or "unknown",
                },
            },
            verification=[{
                "check": "prior progress",
                "result": "passed",
                "detail": remaining,
            }],
            handoff={
                "kind": "local-recovery",
                "summary": ("Run the undo (`transaction.py plan engine-upgrade-rollback`) to see how this "
                            "is unwound, or finish the update by opening it for review. Do not apply the "
                            "update again on top of this state."),
            })


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
        # BASE CURRENCY FIRST — an undo mutates the working tree too, so a wrong, behind-origin or diverged
        # base refuses here before anything is touched; the non-refusing note rides on for the handoff.
        currency = handoff.refuse_if_stale_base()
        result = module_manager.rollback(confirm=True)
        if result.get("refused"):
            raise transaction.TransactionRefused(
                "rollback-refused", result.get("reason", "The undo could not complete."),
                ["Resolve what the reason above names, then run this again."])
        result["base_currency"] = currency
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
        currency_line = handoff.currency_summary_line(applied.get("base_currency"))
        suffix = (" " + currency_line) if currency_line else ""
        if not applied.get("undone"):
            return {"kind": "manual-follow-up",
                    "summary": "Nothing was undone locally. If the update was already merged, revert its "
                               "pull request — the engine never rewrites your main line." + suffix}
        return {"kind": "local-recovery",
                "summary": "The engine is back the way it was, with a recovery point saved first." + suffix,
                "reference": str(applied.get("recovery_point") or "")}


transaction.register(UpgradeEngine())
transaction.register(RollbackUpgrade())
