#!/usr/bin/env python3
"""Brownfield engine arrival, as a typed transaction.

WHY ARRIVAL IS THE HARD ONE. Arrival runs where the engine does not yet exist — on the operator's system
interpreter, the Python 3.9 floor, before the engine's own 3.11 runtime is built. So every module this
adapter and the arrival machinery it drives can reach must import nothing beyond the standard library and
carry `from __future__ import annotations`; an evaluated `X | None` is a TypeError on 3.9. That is a
load-bearing rule, enforced here by `audit_arrival_floor`, not a style preference.

CONSENT IS GATED ON RESOLVED CHOICES. Arrival is not a one-shot apply: it surfaces file collisions the
operator must decide (accept / leave-as-is / abort) and module choices they own (keep / decline). NO consent
handle is minted while any required collision or module choice is unresolved — the plan REFUSES and names the
unresolved choice instead, because a handle over an undecided arrival would consent to a change the operator
never saw whole. The handle, once minted, binds the pinned release identity, the target repository, its
default branch and HEAD, the collision set and its decisions, the reviewer tier, the module choices, and the
target fingerprints — so a release or target that moved between plan and run invalidates it.

ONE STEP STAYS MANUAL, AND STAYS PROSE. Obtaining and running the pinned engine release before the target
contains any engine code cannot be automated by an engine that is not there yet. That step is a plan
`manual_step`, not a code path.

THE SEAM. This adapter's inspect and plan are the read-only surface and the consent gate. Its apply, verify
and handoff — running the overlay/setup/verify/retire/index-regen/checkless-bootstrap sequence, enumerating
every degraded outcome, opening the verified pull request, and returning control-plane-finalize as a
post-merge follow-up — are provided by node b3-arrival-execution. It wraps `instantiator.arrive`; it never
re-decides a collision class, a module resolution, or an arrival outcome.

STANDARD LIBRARY ONLY on the 3.9 floor, and `from __future__ import annotations` at the top: this module is
one of the arrival-floor files its own audit checks.
"""
from __future__ import annotations

import os

import instantiator
import transaction
import transaction_handoff


# ── The 3.9 arrival floor: declared checked data, and the audit that reads it ────────────────────────
# The arrival entry point plus the two new typed-transaction adapter files are the ROOTS of the module
# closure that must hold the 3.9 floor. The audit walks their import graph (restricted to the engine's own
# tools) and refuses any reachable module that lacks `from __future__ import annotations` or pulls in a
# standard-library module that does not exist on 3.9. A NEW import that widens the floor therefore fails the
# audit rather than silently breaking a real arrival — the guarantee the arrival adapter's whole existence
# rests on.
ARRIVAL_FLOOR_ROOTS = ("instantiator", "transaction_adapters_arrival", "transaction_adapters_controlplane")

# Standard-library modules that do not exist on the 3.9 floor: reaching one from the arrival closure would
# import-error on the operator's system interpreter before the engine's own runtime exists.
FLOOR_FORBIDDEN_STDLIB = frozenset({"tomllib", "graphlib"})

# The one reachable module that carries no future-import because it declares no deferred-evaluation
# annotations at all (a hashlib-only leaf). Named explicitly so the exemption is a reviewed decision, never a
# silent hole the audit forgets to close.
FLOOR_FUTURE_IMPORT_EXEMPT = frozenset({"license_seeds"})

# The snapshot of the reachable set, as checked data. `test_instantiator` asserts the live closure equals
# this, so a module entering (or leaving) the arrival floor is a deliberate, reviewed change to this list —
# never a silent drift. Kept sorted for a legible diff.
ARRIVAL_REACHABLE_MODULES = (
    "accepted_hook_dispatch", "attention", "attention_rank", "audit_digest", "boot", "boot_alarm_ledger",
    "boot_slice", "bootstrap", "checkout_auto_update", "checkout_health", "close", "derived_state",
    "engine_write", "execution_environment", "first_run_health", "github_client", "greenfield_intake",
    "hooks", "hooks_path_health", "instantiator", "integration_queue_backend", "issue_author", "issue_gate",
    "issue_kind", "issue_label_client", "knowledge_gen", "knowledge_index", "knowledge_query",
    "license_health", "license_seeds", "modes", "module_catalog", "module_coherence", "module_manager",
    "moment", "mutation_guards", "operator_overrides", "pr_reconcile", "protection_guard", "providers",
    "release_source", "render_safety", "repo_behavior", "repo_identity", "security_floor", "self_map",
    "session_relay", "standing_situation", "telemetry", "transaction", "transaction_adapters_arrival",
    "transaction_adapters_controlplane", "transaction_envelope", "transaction_handoff", "tune", "validate",
    "weakening_guard", "wiring", "work_record",
)


def _tools_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _scan_module(module_name: str, tools_dir: str):
    """Parse `module_name` and return (import_time_local_modules, forbidden_stdlib_hits, has_future_import).

    ONLY import-time imports count toward the floor: a module's top-level imports (including those inside
    top-level try/if/with blocks) are what execute when it loads on 3.9. Imports buried inside a function or
    method body are deferred — they run only if that code path runs, which is why the existing arrival
    machinery reaches tomllib/graphlib lazily in non-arrival paths without breaking the floor. Descending
    into def/class bodies would conflate the two and flag imports that never load during an arrival."""
    import ast
    path = os.path.join(tools_dir, module_name + ".py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    present = {n[:-3] for n in os.listdir(tools_dir) if n.endswith(".py")}
    local, forbidden, has_future = set(), set(), False

    def record(top: str, guarded: bool):
        # A forbidden-stdlib import GUARDED by a try/except (the `try: import tomllib except
        # ModuleNotFoundError: tomllib = None` compat pattern) is 3.9-safe — the except catches it and the
        # code degrades. Only an UNGUARDED load-time reach breaks the floor, so only that is flagged.
        if top in FLOOR_FORBIDDEN_STDLIB and not guarded:
            forbidden.add(top)
        if top in present:
            local.add(top)

    def scan(body, guarded=False):
        nonlocal has_future
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # deferred — not import-time, so not on the load-time floor
            if isinstance(node, ast.Import):
                for alias in node.names:
                    record(alias.name.split(".")[0], guarded)
            elif isinstance(node, ast.ImportFrom):
                if node.module == "__future__" and any(a.name == "annotations" for a in node.names):
                    has_future = True
                if node.level == 0 and node.module:
                    record(node.module.split(".")[0], guarded)
            elif isinstance(node, ast.Try):
                # Imports in the try body are guarded; the handlers/else/finally are not the compat shield.
                scan(node.body, guarded=True)
                for attr in ("orelse", "finalbody"):
                    scan(getattr(node, attr, []) or [], guarded)
                for handler in node.handlers:
                    scan(handler.body, guarded)
            elif isinstance(node, (ast.If, ast.With, ast.For, ast.While)):
                # Other top-level compound blocks still run at import time; descend, keeping the guard state.
                for attr in ("body", "orelse", "finalbody"):
                    scan(getattr(node, attr, []) or [], guarded)

    scan(tree.body)
    return local, forbidden, has_future


def arrival_floor_closure(tools_dir: str | None = None):
    """The transitive set of engine-tool modules reachable from the arrival roots at IMPORT TIME — the live
    reachable set the declared manifest is checked against."""
    tools_dir = tools_dir or _tools_dir()
    seen, stack = set(), list(ARRIVAL_FLOOR_ROOTS)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        local, _forbidden, _future = _scan_module(name, tools_dir)
        stack.extend(local - seen)
    return seen


def audit_arrival_floor(tools_dir: str | None = None):
    """Language-level audit of the arrival floor. Returns a list of plain-language findings; empty is clean.
    Covers the future-import rule on every import-time-reachable module (the new adapter files among them),
    and any import-time reach into a standard-library module that does not exist on 3.9."""
    tools_dir = tools_dir or _tools_dir()
    findings: list = []
    for name in sorted(arrival_floor_closure(tools_dir)):
        _local, forbidden, has_future = _scan_module(name, tools_dir)
        for bad in sorted(forbidden):
            findings.append("{0}.py imports {1} at load time, which does not exist on the Python 3.9 "
                            "arrival floor".format(name, bad))
        if name not in FLOOR_FUTURE_IMPORT_EXEMPT and not has_future:
            findings.append("{0}.py is on the arrival floor but is missing "
                            "`from __future__ import annotations` (an evaluated `X | None` is a TypeError on 3.9)"
                            .format(name))
    return findings


# ── The typed arrival transaction ────────────────────────────────────────────────────────────────────

def _collision_id(collision: dict) -> str:
    """A stable identifier for one surfaced overlap, so a decision can be bound to it: its class and the
    concrete project paths it names (the collision dict carries no id of its own)."""
    return "class{0}:{1}".format(collision.get("klass"), ",".join(sorted(collision.get("paths") or [])))


class EngineArrival(transaction.Adapter):
    operation = "engine-arrival"

    def __init__(self, *, target_root, release_tree, engine_release=None, keep=None, declined=None,
                 tier=None, decisions=None, opener=None, gh_api=None, home_reader=None, settings_path=None,
                 uv_present=None, uv_installer=None, uv_runner=None, consent=None, control_transport=None,
                 gh_refresh=None, control_issues=None, control_repo=None, control_token=None,
                 version_info=None, gate=None, handle=None, default_branch=None, clock=None):
        self._target_root = target_root
        self._release_tree = release_tree
        self._engine_release = engine_release
        self._keep = list(keep or [])
        self._declined = list(declined or [])
        self._tier = tier or "solo"
        # decisions: collision id -> "accept" | "leave-as-is" | "abort". The operator's per-collision choices.
        self._decisions = dict(decisions or {})
        # Every arrival boundary, injectable so the real flow runs with nothing real touched (tests, the demo,
        # and the live door all pass these). Held for b3's apply to thread into instantiator.arrive.
        self._boundaries = dict(
            opener=opener, gh_api=gh_api, home_reader=home_reader, settings_path=settings_path,
            uv_present=uv_present, uv_installer=uv_installer, uv_runner=uv_runner, consent=consent,
            control_transport=control_transport, gh_refresh=gh_refresh, control_issues=control_issues,
            control_repo=control_repo, control_token=control_token, version_info=version_info, gate=gate,
            handle=handle, default_branch=default_branch)
        self._clock = clock

    # -- read-only surface --------------------------------------------------------------------------

    def _surface(self) -> dict:
        """The read-only arrival surface: the collision check and the team-tier recommendation, writing
        nothing. This is instantiator.arrive's own apply_changes=False mode, so the adapter never re-derives
        what the machinery already decides."""
        return instantiator.arrive(
            target_root=self._target_root, release_tree=self._release_tree,
            engine_release=self._engine_release, tier=self._tier, apply_changes=False,
            announce=lambda _text: None,
            gh_api=self._boundaries["gh_api"], control_repo=self._boundaries["control_repo"],
            version_info=self._boundaries["version_info"])

    def _release_identity(self) -> dict:
        """The pinned release the arrival would install: its recorded release string and the module id set it
        would deliver. Read with ROOT at the release tree, never the target."""
        engine_release = self._engine_release or instantiator._existing_release(self._release_tree) or "unknown"
        try:
            with instantiator._redirect_root(self._release_tree):
                ids = sorted(m.get("id") for _rel, m in instantiator.module_coherence.discover_manifests()
                             if m.get("id"))
        except Exception:  # noqa: BLE001 — an unreadable release is disclosed as such, never a crash
            ids = []
        return {"engine_release": str(engine_release), "module_ids": ids}

    def inspect(self, args) -> dict:
        surface = self._surface()
        release = self._release_identity()
        slug = self._boundaries["control_repo"] or instantiator._target_slug(self._target_root) or "unknown"
        target = transaction_handoff.working_tree_state(self._target_root)
        collisions = surface.get("collisions") or []
        team = surface.get("team") or {}
        # A read-only surface digest that MOVES with the collision set and the target, so a target whose files
        # changed between plan and run invalidates the handle.
        surface_digest = "|".join(sorted(_collision_id(c) for c in collisions)) or "no-collisions"
        return {
            "summary": "Arrival of engine {0} into {1}: {2} overlap(s) to decide; team tier {3}.".format(
                release["engine_release"], slug, len(collisions),
                "recommended" if team.get("detected") else "not indicated"),
            "collisions": [{"id": _collision_id(c), "klass": c.get("klass"), "paths": c.get("paths"),
                            "consequence": c.get("consequence")} for c in collisions],
            "team_recommendation": bool(team.get("detected")),
            "stopped_on": surface.get("stopped_on"),
            "fingerprints": {
                # Bind release identity, target, and the surface — never a token or a raw GitHub error body.
                "release": "{0}:{1}".format(release["engine_release"], ",".join(release["module_ids"]) or "empty"),
                "target_repo": str(slug),
                "target_branch": str(target.get("branch") or "unknown"),
                "target_head": str(target.get("head") or "unknown"),
                "surface": surface_digest,
            },
        }

    def plan(self, args, facts: dict) -> dict:
        # A surface that could not even be read (a too-old interpreter, an empty release) cannot be planned:
        # refuse with the machinery's own reason rather than minting a handle over nothing.
        stopped = facts.get("stopped_on")
        if stopped:
            surface = self._surface()
            raise transaction.TransactionRefused(
                "arrival-surface-" + str(stopped),
                surface.get("reason") or "The arrival surface could not be read, so nothing was planned.",
                ["Resolve what the reason above names, then plan the arrival again."])
        # A module cannot be both kept and declined — the machinery's own pre-write invariant, surfaced here
        # as a typed refusal before any handle is minted.
        contradictory = sorted(set(self._keep) & set(self._declined))
        if contradictory:
            raise transaction.TransactionRefused(
                "contradictory-module-choice",
                "A module cannot be both kept and declined: {0}.".format(", ".join(contradictory)),
                ["Decide each of those modules once — keep it or decline it — then plan again."])
        # NO handle while a required collision is undecided. Every surfaced overlap must carry an explicit,
        # valid decision; an unresolved or unrecognized one refuses and names it.
        unresolved, invalid = [], []
        for collision in facts.get("collisions") or []:
            choice = self._decisions.get(collision["id"])
            if choice is None:
                unresolved.append(collision["id"])
            elif choice not in instantiator._COLLISION_CHOICES:
                invalid.append("{0}={1}".format(collision["id"], choice))
        if unresolved or invalid:
            detail = []
            if unresolved:
                detail.append("undecided: " + ", ".join(unresolved))
            if invalid:
                detail.append("not a valid choice: " + ", ".join(invalid))
            raise transaction.TransactionRefused(
                "unresolved-choice",
                "The arrival cannot be consented to while a file overlap is undecided ({0}). Every overlap "
                "must be accepted, left as-is, or aborted first.".format("; ".join(detail)),
                ["Decide each overlap ({0}) with accept / leave-as-is / abort, then plan again."
                 .format(", ".join(sorted(instantiator._COLLISION_CHOICES)))])

        decisions = {c["id"]: self._decisions[c["id"]] for c in (facts.get("collisions") or [])}
        consequences = [
            "Overlays the engine onto {0} and opens ONE pull request for your review; nothing merges until you do."
            .format(facts["fingerprints"]["target_repo"]),
            "Protects the default branch with a pull-request rule now, WITHOUT the engine's own required "
            "checks — those bind after this arrival's pull request merges (control-plane-finalize).",
            "Applies your module choices: {0} kept, {1} declined.".format(
                len(self._keep) or "the defaults", len(self._declined)),
        ]
        return {
            "inputs": {
                # The consent handle binds the pinned release, the target and its branch, the collision set and
                # its decisions, the reviewer tier and the module choices — and nothing else. No token, no error body.
                "release": facts["fingerprints"]["release"],
                "target_repo": facts["fingerprints"]["target_repo"],
                "target_branch": facts["fingerprints"]["target_branch"],
                "target_head": facts["fingerprints"]["target_head"],
                "tier": self._tier,
                "keep": sorted(self._keep),
                "declined": sorted(self._declined),
                "collision_decisions": decisions,
            },
            "consequences": consequences,
            "choices": [{"id": "tier", "chosen": self._tier, "options": ["solo", "team"]}]
            + [{"id": cid, "chosen": choice, "options": list(instantiator._COLLISION_CHOICES)}
               for cid, choice in sorted(decisions.items())],
            "effects": [
                {"kind": "tracked-files", "description": "the engine's files are overlaid onto the project"},
                {"kind": "external-settings",
                 "description": "the default branch is protected (checkless until the arrival pull request merges)"},
                {"kind": "review-artifact", "description": "a pull request is opened for review"}],
            "reversibility": "reverted-pull-request",
            "manual_steps": [
                "You obtain and run the pinned engine release yourself, before this project contains any "
                "engine code — the engine cannot start its own arrival before it exists.",
                "Merge the pull request it opens; then bind the engine's checks — `transaction.py plan "
                "control-plane-finalize` shows what that would do, and you apply it yourself with "
                "`bootstrap.py finalize`.",
            ],
        }

    # -- apply / verify / handoff (arrival execution) ------------------------------------------------

    def apply(self, args, plan: dict) -> dict:
        # Run the SAME arrival machinery, now committing: the consent handle has already been verified, and
        # each collision carries the decision the plan bound. The verified selective opener is injected here
        # (the caller/door supplies it); arrive drives overlay -> setup -> verify -> retire -> index regen ->
        # checkless control-plane bootstrap, and opens the reviewed pull request. Every degraded outcome is
        # returned in the result rather than raised.
        decisions = plan["inputs"]["collision_decisions"]

        def decide(collision):
            # The bound decision for this overlap; a somehow-unseen one aborts rather than guessing a write.
            return decisions.get(_collision_id(collision), "abort")

        b = self._boundaries
        result = instantiator.arrive(
            target_root=self._target_root, release_tree=self._release_tree,
            engine_release=self._engine_release, keep=self._keep, declined=self._declined, tier=self._tier,
            handle=b["handle"], default_branch=b["default_branch"], decide=decide, apply_changes=True,
            opener=b["opener"], gh_api=b["gh_api"], home_reader=b["home_reader"],
            settings_path=b["settings_path"], uv_present=b["uv_present"], uv_installer=b["uv_installer"],
            uv_runner=b["uv_runner"], consent=b["consent"], control_transport=b["control_transport"],
            gh_refresh=b["gh_refresh"], control_issues=b["control_issues"], control_repo=b["control_repo"],
            control_token=b["control_token"], version_info=b["version_info"], gate=b["gate"])
        return {"result": result}

    @staticmethod
    def _hard_gate_findings(result: dict) -> list:
        return [f for f in (result.get("gate_findings") or []) if f.get("severity") == "hard"]

    @staticmethod
    def _control_plane_step(result: dict):
        return next((s for s in (result.get("steps") or []) if s.get("step") == "control-plane"), None)

    def verify(self, args, applied: dict) -> list:
        result = applied["result"]
        receipts = [{
            "check": "engine files overlaid onto the project",
            "result": "passed" if result.get("overlaid") else (
                "unavailable" if not result.get("proceeded") else "failed"),
            "detail": "{0} file(s) overlaid".format(len(result.get("overlaid") or [])),
        }]
        # The embedded control-plane outcome surfaced VERBATIM — its own step status and cause, not a
        # re-derived verdict — so a reader sees exactly what the checkless bootstrap reported.
        cp = self._control_plane_step(result)
        if cp is not None:
            receipts.append({
                "check": "control-plane checkless protection (embedded outcome)",
                "result": "passed" if cp.get("protected") else "failed",
                "detail": "control-plane step: {0}".format(
                    {k: cp[k] for k in ("status", "cause", "protected") if k in cp} or cp),
            })
        hard = self._hard_gate_findings(result)
        receipts.append({
            "check": "generated knowledge index consistent with the deployed sources",
            "result": "failed" if hard else ("passed" if result.get("proceeded") else "unavailable"),
            "detail": ("; ".join((f.get("message") or "") for f in hard)[:300] if hard
                       else "no index drift"),
        })
        receipts.append({
            "check": "arrival opened for review as a pull request",
            "result": "passed" if result.get("pr") else "failed",
            "detail": ("opened" if result.get("pr")
                       else (result.get("reason") or "the pull request was not opened")),
        })
        return receipts

    def handoff(self, args, applied: dict, receipts) -> dict:
        result = applied["result"]
        pr = result.get("pr")
        hard = self._hard_gate_findings(result)
        # THE CLEAN TERMINAL — a reviewed pull request with the finalize follow-up. Forbidden while a hard
        # finding is outstanding: a pull-request handoff must never claim satisfied local postconditions over
        # an inconsistency (arrive() already returns before the opener on a hard finding, so pr is None then;
        # this is the defensive restatement of that invariant).
        if pr and not hard:
            reference = (pr.get("url") or pr.get("html_url") or (str(pr.get("number")) if pr.get("number")
                         else "")) if isinstance(pr, dict) else str(pr)
            handoff = {
                "kind": "pull-request",
                "summary": ("The engine's arrival is proposed for your review. Nothing about the project "
                            "changes until you merge it; the branch is protected now, but the engine's own "
                            "checks are bound only after the merge."),
                # The finalize follow-up names the operation and WHEN — never a precomputed consent handle,
                # because the merge changes the state finalize must inspect, so its plan is made fresh then.
                "follow_up": {"operation": "control-plane-finalize",
                              "when": "after you merge this pull request, so it inspects the merged state; "
                                      "run `bootstrap.py finalize`"},
            }
            if reference:
                handoff["reference"] = str(reference)
            return handoff
        if hard:
            return {
                "kind": "manual-follow-up",
                "summary": (result.get("reason")
                            or "The engine is installed but its generated knowledge index is inconsistent "
                               "with the deployed sources, so the arrival was NOT opened for review."),
            }
        # Every other degraded/stopped outcome, carried honestly. A run that PROCEEDED (the engine is
        # installed) but stopped short of the pull request is a local recovery the operator resumes; a run
        # that stopped BEFORE installing anything is a clean stop to act on. arrive()'s own reason is the
        # operator-facing text in both cases — including the tool-runtime consent halt, the retire-refused
        # terminals, and the typed 3.9 codex deferral.
        summary = result.get("reason") or "The arrival did not complete; nothing was opened for review."
        kind = "local-recovery" if result.get("proceeded") else "manual-follow-up"
        return {"kind": kind, "summary": summary}


transaction.register(EngineArrival(target_root=".", release_tree="."))
