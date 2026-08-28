#!/usr/bin/env python3
"""Adding and removing one module, as typed transactions.

WHY THESE DO NOT OPEN A PULL REQUEST. The operator's ruling: the review gate is a safety gate protecting
product code, not a hermetic seal on the repository. The engine changing its own installation is not
product code, and routing it through review would break a flow that works today — a session offers a
capability, the operator says yes, and the capability is usable in that same conversation. Under
pull-request ceremony that becomes a two-visit errand for every offer, every re-add of a declined add-on,
and every mid-update decline.

What was actually wrong was narrower: the change landed as uncommitted sprawl in whatever state the
checkout happened to be in. So these transactions make the apply CLEAN rather than ceremonial — exactly
the declared file set, one labelled commit, revertable as a unit — and leave the reviewed handoff to the
transactions that genuinely change the engine's own code.

These adapters WRAP `module_manager.add` / `.remove`. They re-decide nothing: dependency rules, wiring
symmetry, permission residue, dependency-group derivation, release notices and coherence all stay where
they are, and the envelope reports what the domain returned.
"""
from __future__ import annotations

import module_manager
import transaction
import transaction_handoff as handoff
import wiring


def _module_paths(module_id: str) -> list:
    """The files this transaction claims — its manifest and whatever its `provides` names.

    Declared up front because the commit stages exactly this set: what a transaction may not name, it may
    not quietly carry.
    """
    paths = [".engine/modules/{0}/manifest.json".format(module_id)]
    for path, manifest in module_coherence_manifests():
        if manifest.get("id") != module_id:
            continue
        for _kind, provided in (manifest.get("provides") or {}).items():
            paths.extend(provided if isinstance(provided, list) else [provided])
        paths.append(path)
    # The engine manifest records the present set, so every add or remove touches it.
    paths.append(".engine/engine.json")
    return sorted(set(p for p in paths if isinstance(p, str)))


def module_coherence_manifests():
    import module_coherence
    return module_coherence.discover_manifests()


class _ModuleAdapter(transaction.Adapter):
    """Shared shape. The two verbs differ in what the domain does, not in how they are typed."""

    verb = ""

    def _module_id(self, args) -> str:
        rest = [a for a in (getattr(args, "rest", None) or []) if not a.startswith("-")]
        if not rest:
            raise transaction.TransactionRefused(
                "module-id-missing",
                "This needs the id of the module to {0}.".format(self.verb),
                ["Run `module_manager.py status` to see the module ids, then name one."])
        return rest[0]

    def inspect(self, args) -> dict:
        module_id = self._module_id(args)
        installed = {m.get("id") for _p, m in module_coherence_manifests()}
        return {
            "summary": "{0!r} is {1}installed.".format(module_id, "" if module_id in installed else "not "),
            "fingerprints": {
                "installed_modules": ",".join(sorted(i for i in installed if i)),
                "head": handoff.working_tree_state()["head"],
            },
        }

    def verify(self, args, applied: dict) -> list:
        """Report what the domain's own coherence check found. A check that could not run says so."""
        receipts = []
        findings = applied.get("findings")
        if findings is None:
            receipts.append({"check": "module coherence", "result": "unavailable",
                             "detail": "The coherence check did not report, so wiring and ownership are "
                                       "unverified for this change."})
        else:
            hard = [f for f in findings if f.get("severity") == "hard"]
            receipts.append({
                "check": "module coherence",
                "result": "failed" if hard else "passed",
                "detail": ("; ".join(f.get("message", "") for f in hard)[:400] if hard
                           else "wiring, ownership and dependencies agree"),
            })
        return receipts


class AddModule(_ModuleAdapter):
    operation = "module-add"
    verb = "add"

    def plan(self, args, facts: dict) -> dict:
        module_id = self._module_id(args)
        # The domain's own resolution, dry-run: same home, ref, fetch, candidate and refusal rules.
        preview = module_manager.preview_add(module_id)
        if preview.get("refused"):
            raise transaction.TransactionRefused(
                "add-refused", preview.get("reason", "This module cannot be added."),
                ["Resolve what the reason above names, then run this again."])
        consequences = ["Adds the {0!r} capability to this engine.".format(module_id)]
        if preview.get("version"):
            consequences.append("At engine release {0}.".format(preview["version"]))
        for note in preview.get("notes") or []:
            consequences.append(note)
        return {
            "inputs": {"module": module_id},
            "consequences": consequences,
            "effects": [
                {"kind": "capability", "description": "{0} becomes available".format(module_id)},
                {"kind": "tracked-files", "description": "the module's files and the engine manifest",
                 "paths": sorted(set(_module_paths(module_id)) | set(preview.get("would_provide") or [])),
                 "reversible": True},
            ],
            "reversibility": "local-recovery",
        }

    def apply(self, args, plan: dict) -> dict:
        module_id = plan["inputs"]["module"]
        declared = [p for effect in plan["effects"] for p in (effect.get("paths") or [])]
        handoff.refuse_unless_ready(declared)
        result = module_manager.add(module_id)
        if result.get("refused"):
            raise transaction.TransactionRefused(
                "add-refused", result.get("reason", "This module could not be added."),
                ["Resolve what the reason above names, then run this again."])
        # Re-derive the claimed paths: the domain knows what it actually wrote.
        written = sorted(set(declared) | set(result.get("copied") or []))
        committed = handoff.commit_in_tree(written, "Add the {0} module".format(module_id))
        result["committed"] = committed.get("committed")
        result["note"] = committed.get("note")
        return result

    def handoff(self, args, applied: dict, receipts) -> dict:
        return handoff.in_tree_handoff(
            applied, "Added the {0!r} capability.".format(applied.get("module_id")))


class RemoveModule(_ModuleAdapter):
    operation = "module-remove"
    verb = "remove"

    def plan(self, args, facts: dict) -> dict:
        module_id = self._module_id(args)
        preview = module_manager.plan_remove(module_id)  # already read-only
        if preview.get("refused"):
            raise transaction.TransactionRefused(
                "remove-refused", preview.get("reason", "This module cannot be removed."),
                ["Resolve what the reason above names, then run this again."])
        consequences = ["Removes the {0!r} capability from this engine — you will no longer be able to "
                        "ask for what it provides.".format(module_id)]
        for note in preview.get("notes") or []:
            consequences.append(note)
        return {
            "inputs": {"module": module_id},
            "consequences": consequences,
            "effects": [
                {"kind": "capability", "description": "{0} is retired".format(module_id)},
                {"kind": "tracked-files", "description": "the module's files and the engine manifest",
                 "paths": _module_paths(module_id)},
            ],
            "reversibility": "local-recovery",
        }

    def apply(self, args, plan: dict) -> dict:
        module_id = plan["inputs"]["module"]
        declared = _module_paths(module_id)
        handoff.refuse_unless_ready(declared)
        result = module_manager.remove(module_id)
        if result.get("refused"):
            raise transaction.TransactionRefused(
                "remove-refused", result.get("reason", "This module could not be removed."),
                ["Resolve what the reason above names, then run this again."])
        committed = handoff.commit_in_tree(declared, "Remove the {0} module".format(module_id))
        result["committed"] = committed.get("committed")
        result["note"] = committed.get("note")
        return result

    def handoff(self, args, applied: dict, receipts) -> dict:
        return handoff.in_tree_handoff(
            applied, "Removed the {0!r} capability.".format(applied.get("module_id")))


transaction.register(AddModule())
transaction.register(RemoveModule())
