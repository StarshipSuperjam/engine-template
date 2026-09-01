#!/usr/bin/env python3
"""Turning the branch-protection floor on (bootstrap) and binding the engine's checks after a brownfield
arrival merges (finalize), as typed transactions.

WHY THESE TWO ARE DIFFERENT FROM EVERY OTHER TRANSACTION. A control-plane transaction mutates repository
SETTINGS that no pull request can carry, so its truthful terminal is external state a fresh read-back
confirms — never a diff. Forcing the pull-request handoff onto it would fake reviewability. So its honest
handoff is `verified-external-state`, and it is returned ONLY when a post-write read confirms the FULL
protection floor (protection_guard's enumerated contract) is in force; the handoff carries the moment it was
read and marks itself point-in-time, because the live read is the only current answer and this asserts the
world, not a plan.

CHECKLESS IS ITS OWN TERMINAL. Brownfield arrival protects `main` before the engine's own workflows are on
the branch, so it binds a floor MINUS the required checks. That is not a weaker `verified-external-state`; it
is a distinct typed outcome, `checkless-confirmed`, so a reader can never mistake "protected, checks deferred"
for "fully protected". Exactly one caller may ask for it — the checkless arrival bootstrap — and finalize is
the second phase that binds the checks once the workflows land.

THIS ADAPTER WRAPS `bootstrap.ControlPlane.apply` / `.finalize`. It never re-decides a floor rule, a
capability check, or a degraded classification — every one of those stays in `bootstrap`, and the envelope
reports what it said. The OAuth authorization screen `apply` opens stays the real permission-consent event;
this adapter does not stand in front of it.

ACCEPT-UNPROTECTED STAYS OUTSIDE THE PROTOCOL. It is an exceptional operator decision recorded by the
`bootstrap.py accept-unprotected` door. This adapter's refusals may EXPLAIN it and state the concrete
consequence of running unprotected — and only on the platform-limitation cause — but they never select or
run it.

STANDARD LIBRARY ONLY on the 3.9 floor: the checkless arrival bootstrap reaches this module before the
engine's own 3.11 runtime exists, so it imports nothing beyond the standard library and carries
`from __future__ import annotations`.
"""
from __future__ import annotations

import datetime

import boot
import bootstrap
import repo_identity
import transaction


def _utc_now_iso() -> str:
    """A point-in-time read-back stamp, UTC, second precision — the moment the floor was confirmed."""
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


# The degraded-outcome matrix, keyed by the CANONICAL cause taxonomy `bootstrap` owns. Every cause has its
# own disposition and its own operator next-actions, so a distinct degraded outcome keeps its distinct
# recovery instead of collapsing into a generic failure. `disposition`:
#   "refuse"   — the write was rejected or never attempted; the world is untouched, a clean typed refusal.
#   "degraded" — a write was attempted and its result is uncertain or partial; a warning handoff the
#                operator resolves, never a false "protected".
# Derived from bootstrap.CONTROL_PLANE_CAUSES: a new cause with no home here fails `_assert_total_coverage`
# (pinned by test) rather than silently escaping into a generic message.
_RECOVERY = {
    bootstrap.CAUSE_NOT_ADMIN: (
        "refuse",
        ["Sign in with a login that can administer this repository's settings, then run this again."]),
    bootstrap.CAUSE_ORG_POLICY: (
        "refuse",
        ["An organization policy blocked the write. Ask an organization owner to allow the repository "
         "ruleset, or switch to the team identity, then run this again."]),
    bootstrap.CAUSE_DIDNT_SAVE: (
        "refuse",
        ["The authorization screen completed but the added permission did not persist. Run this again and "
         "re-approve the GitHub authorization screen."]),
    bootstrap.CAUSE_UNSUPPORTED_PLATFORM: (
        "refuse",
        ["This repository's GitHub plan cannot host branch rulesets, so the protection floor cannot be "
         "turned on here. Running unprotected means work can reach the default branch without a pull "
         "request or the required checks — the safety gate stays OFF.",
         "If your plan can host rulesets (upgrade it, or make the repository public), run this again.",
         "To deliberately accept running without protection, record it yourself with "
         "`bootstrap.py accept-unprotected` — an explicit operator decision this protocol never makes for you."]),
    bootstrap.CAUSE_WORKFLOWS_ABSENT: (
        "refuse",
        ["The engine's workflows are not on the branch yet, so its required checks cannot be bound without "
         "deadlocking future pull requests. This usually means the arrival pull request has not merged — "
         "merge it, then run this again."]),
    bootstrap.CAUSE_VERIFY_FAILED: (
        "degraded",
        ["The write was accepted but the protection floor is still not fully in force. Check the "
         "repository's branch rules, or run this again."]),
    bootstrap.CAUSE_VERIFY_UNREADABLE: (
        "degraded",
        ["The write was made but the confirming read-back could not be read, so whether the floor is in "
         "force was not established. Run this again to re-check; do not assume it is on."]),
    bootstrap.CAUSE_PRESERVE_FAILED: (
        "degraded",
        ["The engine could not confirm it left your own branch-protection rule exactly as it was, so it "
         "stopped rather than risk changing your protection. Check your repository's rules and tell me if "
         "anything looks off."]),
}


def _assert_total_coverage() -> None:
    """Every canonical cause has a recovery, and no recovery names a cause that is not canonical. Called at
    import so a taxonomy change that outruns this matrix fails loudly here, not silently in a handoff."""
    missing = bootstrap.CONTROL_PLANE_CAUSES - set(_RECOVERY)
    extra = set(_RECOVERY) - bootstrap.CONTROL_PLANE_CAUSES
    if missing or extra:
        raise AssertionError(
            "the control-plane degraded-outcome matrix is out of step with bootstrap.CONTROL_PLANE_CAUSES: "
            "missing {0}, unknown {1}".format(sorted(missing), sorted(extra)))


_assert_total_coverage()


class _ControlPlaneAdapter(transaction.Adapter):
    """Shared shape for the two control-plane transactions. Subclasses set `operation`, the branch default,
    and which ControlPlane method (`apply` vs `finalize`) the run drives."""

    def __init__(self, *, control_plane=None, repo=None, token=None, transport=None, refresh_fn=None,
                 issues=None, tier=None, checkless=False, clock=None):
        # A ready ControlPlane, injected by the arrival path and by tests; otherwise resolved lazily from the
        # environment so a bare registered instance still works from the door. Resolving is deferred to call
        # time so importing/registering this module performs no I/O and stays safe on the arrival floor.
        self._injected = control_plane
        self._repo = repo
        self._token = token
        self._transport = transport
        self._refresh_fn = refresh_fn
        self._issues = issues
        self._tier = tier
        self._checkless = bool(checkless)
        self._clock = clock or _utc_now_iso

    # -- construction / resolution ----------------------------------------------------------------

    def _control_plane(self, args) -> bootstrap.ControlPlane:
        if self._injected is not None:
            return self._injected
        repo = self._repo or boot.repo_slug()
        token = self._token or boot.gh_token()
        if not repo or not token:
            raise transaction.TransactionRefused(
                "control-plane-unreachable",
                "This cannot reach GitHub to read or change branch protection: {0} is not available."
                .format("the repository" if not repo else "a GitHub login"),
                ["Run this from inside the repository's checkout with a GitHub login available, then try again."])
        return bootstrap.ControlPlane(
            repo, token, transport=self._transport, refresh_fn=self._refresh_fn,
            issues=self._issues, tier=self._tier, checkless=self._checkless)

    def _default_branch(self) -> str:
        raise NotImplementedError

    def _branch(self, args, cp) -> str:
        rest = [a for a in (getattr(args, "rest", None) or []) if not a.startswith("-")]
        return rest[0] if rest else self._default_branch()

    def _drive(self, cp: bootstrap.ControlPlane, branch: str):
        raise NotImplementedError

    # -- inspect / plan ---------------------------------------------------------------------------

    def inspect(self, args) -> dict:
        cp = self._control_plane(args)
        branch = self._branch(args, cp)
        try:
            missing = cp.floor_missing(branch)
            floor_state = "in-force" if not missing else "not-in-force"
            floor_missing = "; ".join(missing) if missing else "none"
        except bootstrap.BootstrapError:
            # An unreadable floor is an ordinary state (offline, or a plan that forbids rulesets), never a
            # crash. It is disclosed here; `plan` decides whether it is a platform-limitation refusal.
            floor_state = "unreadable"
            floor_missing = "unreadable"
        return {
            "summary": "Safety gate on {0!r} of {1}: {2}.".format(branch, cp.repo, {
                "in-force": "on", "not-in-force": "off", "unreadable": "could not be read"}[floor_state]),
            "fingerprints": {
                # Bind repository and external state — NEVER the token, and never a raw GitHub error body.
                "repo": str(cp.repo),
                "branch": str(branch),
                "tier": str(cp.tier),
                "checkless": "yes" if cp.checkless else "no",
                "floor_state": floor_state,
                "floor_missing": floor_missing,
            },
        }

    def plan(self, args, facts: dict) -> dict:
        cp = self._control_plane(args)
        branch = self._branch(args, cp)
        # Pre-mutation platform-limitation refusal: the ONE place accept-unprotected is named before any
        # write. If the floor read is unreadable AND the read itself returns GitHub's genuine plan-limitation
        # 403, this plan cannot proceed — refuse, naming the concrete consequence and the operator door.
        if facts["fingerprints"]["floor_state"] == "unreadable":
            try:
                plan_limited = cp._plan_forbids_rulesets(branch)
            except bootstrap.BootstrapError:
                plan_limited = False
            if plan_limited:
                self._refuse(bootstrap.CAUSE_UNSUPPORTED_PLATFORM)
        checkless = bool(cp.checkless)
        consequences = [
            ("Protects {0!r} so work cannot reach it without a pull request that passes the engine's floor."
             .format(branch)) if not checkless else
            ("Protects {0!r} with a pull-request rule but WITHOUT the engine's own required checks yet — "
             "those are bound after this arrival merges.".format(branch)),
            "Changes repository settings on GitHub, not files in your tree; no pull request carries this.",
            "If this login cannot yet manage repository settings, GitHub shows an authorization screen — "
            "approving it is the permission-consent event; the engine cannot grant it to itself.",
        ]
        return {
            "inputs": {
                # The consent handle binds target, branch, tier, mode and the floor state read — and nothing
                # else. No token, no GitHub error body: those never enter the digest.
                "repo": str(cp.repo),
                "branch": str(branch),
                "tier": str(cp.tier),
                "checkless": checkless,
                "floor_before": facts["fingerprints"]["floor_state"],
            },
            "consequences": consequences,
            "effects": [{"kind": "external-settings",
                         "description": "the branch-protection ruleset on GitHub is created or brought up to the floor"}],
            "reversibility": "external-reapply",
        }

    # -- apply / verify / handoff -----------------------------------------------------------------

    def apply(self, args, plan: dict) -> dict:
        cp = self._control_plane(args)
        branch = plan["inputs"]["branch"]
        result = self._drive(cp, branch)
        observed_at = self._clock()   # the moment of the post-write read-back the result rests on
        # A degraded/unverified result is dispatched through the canonical matrix. A "refuse" cause left the
        # world untouched, so it stops cleanly as a typed refusal; a "degraded" cause is uncertain/partial and
        # is carried on to an honest warning handoff.
        if result.status in ("degraded", "unverified"):
            disposition, _ = self._recovery(result.cause)
            if disposition == "refuse":
                self._refuse(result.cause, result=result)
        return {"result": result, "observed_at": observed_at, "branch": branch,
                "checkless": bool(cp.checkless)}

    def verify(self, args, applied: dict) -> list:
        result = applied["result"]
        full_floor = result.status in ("applied", "already") and not result.missing
        receipts = [{
            "check": "full protection floor in force (confirmed by a post-write read)",
            "result": "passed" if full_floor else (
                "unavailable" if result.status == "unverified" else "failed"),
            "detail": ("read back at {0}".format(applied["observed_at"]) if full_floor else
                       ("the confirming read-back could not be read" if result.status == "unverified" else
                        "still not in force: " + ("; ".join(result.missing) if result.missing
                                                  else "the floor was not confirmed"))),
        }]
        receipts.append({
            "check": "engine labels present",
            "result": "passed" if result.labels_ok else "unavailable",
            "detail": ("the engine's guard labels are in place" if result.labels_ok
                       else "the engine's labels could not be confirmed; they are retried next time GitHub is reachable"),
        })
        return receipts

    def handoff(self, args, applied: dict, receipts) -> dict:
        result = applied["result"]
        observed_at = applied["observed_at"]
        checkless = applied["checkless"]
        if result.status in ("applied", "already") and not result.missing:
            if checkless:
                return {
                    "kind": "checkless-confirmed",
                    "summary": ("The branch is protected — a pull request is required — but the engine's own "
                                "checks are NOT bound yet; they are bound after this arrival's pull request "
                                "merges, by `bootstrap.py finalize`. This is the confirmed external state as read."),
                    "observed_at": observed_at,
                    "point_in_time": True,
                }
            return {
                "kind": "verified-external-state",
                "summary": ("The full protection floor is in force on the branch, confirmed by a read-back. "
                            "This is repository settings on GitHub, not a change you merge."),
                "observed_at": observed_at,
                "point_in_time": True,
            }
        # An "applied" result carrying residual missing is augmented-partial: the engine did its part but a
        # floor piece is left open in the operator's own rule. That is NOT the full floor, so it is disclosed
        # as a follow-up, never as verified-external-state.
        if result.status == "applied" and result.missing:
            return {
                "kind": "manual-follow-up",
                "summary": ("The engine added its protection alongside your own rule, but a floor piece your "
                            "rule leaves open remains: {0}. Close it in your repository's rules if you want "
                            "the full floor. Nothing of yours was changed.".format("; ".join(result.missing))),
            }
        # Degraded / unverified with a "degraded" disposition: an honest warning handoff, whose summary is
        # bootstrap's own operator copy for the cause (the matrix guarantees the cause is a known one).
        self._recovery(result.cause)
        return {"kind": "manual-follow-up", "summary": bootstrap.render(result)}

    # -- refusal helpers --------------------------------------------------------------------------

    def _recovery(self, cause):
        if cause not in _RECOVERY:
            # A cause with no home in the matrix must fail loudly, never be smoothed into a generic message —
            # this is the coverage guarantee that keeps a new degraded outcome from escaping its recovery.
            raise AssertionError(
                "unmapped control-plane cause {0!r} — add it to bootstrap.CONTROL_PLANE_CAUSES and to the "
                "recovery matrix".format(cause))
        return _RECOVERY[cause]

    def _refuse(self, cause, result=None):
        _, next_actions = self._recovery(cause)
        explanation = bootstrap.render(result) if result is not None else next_actions[0]
        raise transaction.TransactionRefused(cause, explanation, next_actions,
                                              retryable=cause != bootstrap.CAUSE_UNSUPPORTED_PLATFORM)


class ControlPlaneBootstrap(_ControlPlaneAdapter):
    operation = "control-plane-bootstrap"

    def _default_branch(self) -> str:
        return repo_identity.resolve_default_branch()

    def _drive(self, cp: bootstrap.ControlPlane, branch: str):
        return cp.apply(branch=branch, announce=lambda _text: None)


class ControlPlaneFinalize(_ControlPlaneAdapter):
    operation = "control-plane-finalize"

    def _default_branch(self) -> str:
        return boot.PROTECTED_BRANCH

    def _control_plane(self, args) -> bootstrap.ControlPlane:
        cp = super()._control_plane(args)
        if cp.checkless:
            # finalize's whole job is to BIND the checks a checkless arrival deferred; a checkless instance
            # would no-op them. This mirrors ControlPlane.finalize's own guard rather than re-deciding it.
            raise transaction.TransactionRefused(
                "finalize-needs-checks",
                "Finalize binds the engine's required checks, so it must run on a control plane that carries "
                "them — not the checkless arrival mode.",
                ["Run finalize with the normal (non-checkless) control plane."])
        return cp

    def _drive(self, cp: bootstrap.ControlPlane, branch: str):
        return cp.finalize(branch=branch, announce=lambda _text: None)


transaction.register(ControlPlaneBootstrap())
transaction.register(ControlPlaneFinalize())
