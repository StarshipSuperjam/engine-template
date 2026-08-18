#!/usr/bin/env python3
"""Protection-detection guard (stage-0 seed).

Reads the EVALUATED per-branch rules for the protected branch and fails loud
until the protected-branch ruleset AND its required-check bindings are actually
in force. The evaluated-rules endpoint omits rules left in 'evaluate' or
'disabled' mode, so a ruleset that protects the branch but does not actually
bite reads as absent here — "is protection on?" is answered by what bites, not
by configuration.

Runs as a `custom/script` check rule in the CI suite,
so an unprotected branch turns engine-ci red. It emits finding.v1 JSON on stdout
(the custom/script machine channel): a hard finding when the gate is not in force,
and a soft witness-deferred note when no token is available (locally — fail open; the
CI run, which has a token, performs the real check). That note is surfaced on the
validator's elevated "not verified in this run — enforces in CI" line (StarshipSuperjam/engine-template#761),
never folded into "nothing to do". The default GITHUB_TOKEN
(Metadata: read) can read this endpoint; it never reads the admin-gated
ruleset-configuration endpoints.

Superseded by the control-plane bootstrap guard once that module lands.
"""
from __future__ import annotations
import json
import os
import sys
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # the sibling tools dir, for github_client
from github_client import get_json  # noqa: E402 — sibling import after the path insert
import repo_identity  # noqa: E402  (resolve_default_branch — the shared, env-authoritative default-branch resolver)

# Frozen required-check names this guard expects the ruleset to bind. These are
# the literal job names of the seed's two required checks; renaming either one,
# anywhere, is a guardrail-weakening change.
REQUIRED_CHECKS = ["engine-ci", "engine-guard"]

UA = "engine-seed-protection-guard"  # this guard's GitHub API User-Agent; boot reuses it for the same protected-branch probe

# The identity-tier vocabulary lives HERE (the floor's home), not in bootstrap: bootstrap imports protection_guard,
# so this is the one module both the ruleset builder and this CI guard can share the tier from without a cycle.
SOLO, TEAM = "solo", "team"  # mirror engine.v1.json's `identity` enum
_ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .engine, two dirs up from tools/


def _load_manifest(engine_dir: str | None = None) -> dict | None:
    """The engine manifest (engine.json) as a dict, or None when it is absent/unreadable/not-an-object — the
    single committed-manifest reader this module shares (resolve_tier, resolve_labeler_authority, and
    recorded_posture all call it, so none opens the file independently). Deliberately robust; never raises."""
    engine_dir = engine_dir if engine_dir is not None else _ENGINE_DIR
    try:
        with open(os.path.join(engine_dir, "engine.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None  # a list/string/number honors the never-raises contract


def resolve_tier(engine_dir: str | None = None) -> str:
    """Resolve the repo's identity tier from its committed manifest — the SINGLE place the tier is read, so no
    ruleset/verify call site defaults it independently (a defaulted tier spread across sites is fail-open: an
    omission silently builds or verifies the weaker floor). Returns SOLO for an absent/missing/unreadable manifest
    or an absent/unknown `identity` (the documented default; a malformed manifest is caught loudly by the engine.v1
    schema check, an intentional team->solo downgrade by the weakening guard's identity detector — neither is this
    read's job). Returns TEAM only when the manifest explicitly records it. Deliberately robust; never raises."""
    manifest = _load_manifest(engine_dir)
    if manifest is None:
        return SOLO
    # TEAM is real only when the distinct identity that makes it real is ALSO recorded. This is the deadlock
    # guard: the team floor (1 required approval) is unsatisfiable without a distinct identity to author the PRs
    # (a sole owner cannot approve their own PR), so any team-WITHOUT-identity state — a first-run tier preference
    # recorded before the switch, or a half-completed switch — fail-safes to the SOLO floor here rather than
    # applying an unsatisfiable ruleset. The team-switch operation writes `identity` and `engine_identity`
    # together, so a genuinely-switched repo resolves TEAM.
    if manifest.get("identity") == TEAM and (manifest.get("engine_identity") or {}).get("login"):
        return TEAM
    return SOLO


# Authority tiers for a guardrail-ack LABEL event, returned by resolve_labeler_authority. Kept beside the
# identity-tier vocabulary (SOLO/TEAM) so, if this stage-0 module is ever superseded, the whole authority
# vocabulary migrates as a unit rather than stranding one half.
AUTH_TEAM, AUTH_SOLO, AUTH_REFUSE = "team", "solo", "refuse"


def resolve_labeler_authority(sender_login, sender_type, engine_dir: str | None = None) -> "tuple[str, str]":
    """Decide whether a `guardrail-ack` LABEL event applied by `sender` is an authorized acknowledgment — the
    SINGLE home of that judgment (StarshipSuperjam/engine-template#958), consumed by the head-binding writer
    `ack_status.py`. Derived from ONE read of the committed base manifest, so the tier decision and the
    identity comparand can never desync (two independent reads could, if resolve_tier's TEAM condition ever
    changed). Returns `(decision, detail)`:

      - AUTH_TEAM  ("team")   — team tier, and a DISTINCT operator identity applied it: mint the head-bound
                                success. `detail` is a short non-secret audit phrase for the status description.
      - AUTH_SOLO  ("solo")   — solo tier (the documented default whenever a READABLE manifest records solo or
                                no distinct identity): accept, preserving one-step consent — but the
                                acknowledgment proves head-binding and a deliberate gesture, NOT WHO applied it
                                (a session holding the same single credential could have). That limit is
                                disclosed to the operator by the guard; this decision does not hide it.
      - AUTH_REFUSE ("refuse")— the event must NOT mint a success. Every branch that cannot PROVE authority
                                fails closed here: an absent/unreadable/malformed manifest (a team repo with a
                                corrupt BASE manifest must never silently drop to solo-accept), team tier with
                                no distinct engine identity to compare against (an empty comparand would accept
                                any sender), or — in team tier — a sender that is not a distinct user account
                                (a Bot, a missing sender, or the engine's OWN identity).

    Deliberately robust; never raises. NOTE ON SCOPE (StarshipSuperjam/engine-template#914 seam): team
    acceptance is "a distinct User whose login is not the engine identity", NOT a bind to a single recorded
    operator `handle`. GitHub's own label ACL (only triage+ collaborators can apply a label) is the allowlist;
    this subtracts the engine's own machine account from it. Multi-operator teams are legitimate, and `handle`
    is optional/stale-able, so a positive operator roster is left as StarshipSuperjam/engine-template#914's
    territory — the swap point is this
    one function. The residual is: any collaborator the operator granted triage+ can acknowledge, not only the
    operator; the threat this closes is the engine acknowledging its OWN change under a shared credential."""
    # NOTE — this inspects `identity`/`engine_identity` directly rather than delegating to resolve_tier(), on
    # purpose and NOT as a duplication oversight: resolve_tier() collapses a TEAM-recorded-but-no-identity
    # manifest to SOLO (its safe default for the ruleset floor), but for an ACK that collapse is fail-OPEN — it
    # would accept a self-applied label under the weaker solo rule. Here that same state must fail CLOSED
    # (AUTH_REFUSE below). The two functions therefore agree on the POSITIVE team/solo classification (pinned by
    # a parity test in test_protection_guard.py) but diverge, deliberately, on the ambiguous case. Both read the
    # manifest ONCE.
    manifest = _load_manifest(engine_dir)
    if manifest is None:
        return (AUTH_REFUSE, "the engine manifest could not be read, so the label applier's authority is unknown")
    login = (manifest.get("engine_identity") or {}).get("login")
    engine_login = login.strip() if isinstance(login, str) and login.strip() else None
    if manifest.get("identity") == TEAM:
        if not engine_login:
            # Defense in depth: resolve_tier only returns TEAM with a truthy login, but the writer must not
            # rest on that internal invariant — were it ever relaxed, an empty comparand below would make
            # `sender != ""` trivially true and accept any labeler. Fail closed instead.
            return (AUTH_REFUSE, "team mode is recorded but no distinct engine identity is on record")
        if sender_type != "User" or not (isinstance(sender_login, str) and sender_login.strip()):
            return (AUTH_REFUSE, "the acknowledgment was not applied by a user account")
        if sender_login.strip().casefold() == engine_login.casefold():
            return (AUTH_REFUSE, "the acknowledgment was applied by the engine's own identity, not a distinct operator")
        return (AUTH_TEAM, f"by @{sender_login.strip()} [operator]")
    # A readable manifest that is not team tier resolves to solo — the documented default for an absent or
    # unknown `identity`, matching resolve_tier. Accept (one-step consent preserved); the annotation states the
    # shared-credential limit plainly so a solo operator is never misled into reading it as identity-verified.
    who = f"@{sender_login.strip()}" if isinstance(sender_login, str) and sender_login.strip() else "an unrecorded actor"
    return (AUTH_SOLO, f"by {who} [shared credential]")


def recorded_posture(engine_dir: str | None = None) -> dict | None:
    """The operator-consented protection posture recorded in engine.json, or None. Returns the posture dict
    ONLY when it is well-formed and records the unsupported-platform status; anything else reads as no posture
    (fail toward the HARD check, never toward a false soften). Written solely by `bootstrap.py
    accept-unprotected` after it re-verifies the platform limitation; its mere presence never softens the gate —
    the standing check also demands a live plan-limitation 403 (platform_forbids_rulesets), so a stale or
    hand-forged posture is inert on any repo whose plan can host protection. Deliberately robust; never raises."""
    manifest = _load_manifest(engine_dir)
    if manifest is None:
        return None
    posture = manifest.get("protection_posture")
    if isinstance(posture, dict) and posture.get("status") == "unsupported-platform":
        return posture
    return None


def _forbidden_body(err: urllib.error.HTTPError) -> dict:
    """Best-effort parse of an HTTPError's JSON body (GitHub returns an object with a `message`). Returns a
    dict, or {} when the body is absent/unreadable/not-JSON. Never raises — a body we cannot read simply
    can't match the plan-limitation signature, so the gate stays HARD."""
    try:
        raw = err.read()
    except Exception:  # noqa: BLE001 — an unreadable error body must not crash the gate
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, AttributeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def platform_forbids_rulesets(status: int, body, headers=None) -> bool:
    """The SINGLE definition of "this repository's GitHub PLAN cannot host branch rulesets at all" — the one
    403 that is a permanent platform limitation rather than a transient or a permission failure. It is the
    load-bearing gate on the whole unsupported-platform posture: the standing check softens to a warning, boot
    reports it calmly, and the accept-unprotected verb records it, ONLY when this returns True. Every other 403
    — a rate-limit/secondary-limit throttle, a service incident, an ordinary not-admin or org-policy block —
    stays a HARD, unresolved failure, because treating any of those as an accepted limitation would silence the
    safety gate on a repo that genuinely CAN be protected (a repo whose plan hosts rulesets returns 200 for any
    token; only writes 403 there). Shared by the standing guard, boot's signal, and bootstrap's arrival/verb so
    the recognition lives in exactly one place and cannot drift between them.

    Grounded in GitHub's real response: on a plan that cannot host rulesets the rules read returns 403 with an
    upgrade-oriented message ('Upgrade to GitHub Team/Enterprise to enable this feature', 'rulesets won't be
    enforced on this private repository until you upgrade …'). A transient rate-limit 403 instead carries
    rate-limit headers/message, excluded FIRST so an induced or coincidental throttle can never masquerade as a
    plan limit. When the wording is unrecognizable we return False — the safe direction is a red gate the
    operator can re-file, never a silently softened one."""
    if status != 403:
        return False
    msg = ""
    if isinstance(body, dict):
        msg = (body.get("message") or "").lower()
    elif isinstance(body, str):
        msg = body.lower()
    hdrs = {}
    try:
        hdrs = {str(k).lower(): str(v).lower() for k, v in dict(headers or {}).items()}
    except (TypeError, ValueError):
        hdrs = {}
    # Exclude the transient/abuse 403s first — these are NOT plan limitations, and are the inducible cases a
    # forged posture would try to ride to a false soften.
    if "retry-after" in hdrs or hdrs.get("x-ratelimit-remaining") == "0":
        return False
    if "rate limit" in msg or "secondary rate" in msg or "abuse" in msg:
        return False
    # Positive plan-limitation signature: an upgrade-to-a-paid-tier message about rulesets / this feature.
    return "upgrade" in msg and any(
        token in msg for token in ("ruleset", "team", "enterprise", "feature", "private repositor"))


def http_error_forbids_rulesets(err: urllib.error.HTTPError) -> bool:
    """platform_forbids_rulesets for the raising read model (get_json raises HTTPError unwrapped) — used by
    the standing check's main() and by boot's protected_branch_signal so both branch on the genuine
    plan-limitation 403 through exactly one recognition. Never raises."""
    return platform_forbids_rulesets(err.code, _forbidden_body(err), err.headers)


def missing_floor(rules: list, required_checks: list, *, tier: str = SOLO) -> list:
    """Pure evaluation of the protection floor against the EVALUATED per-branch rules (which already omit rules in
    evaluate/disabled mode), for the given identity `tier`. Returns the list of floor pieces not in force — empty
    means the gate fully bites. The floor requires FRESHNESS — the required checks must have passed against the
    then-current base — enforced as `strict_required_status_checks_policy` on the required_status_checks rule
    (eADR-0021, amended by StarshipSuperjam/engine-template#915). A merge queue is an OPTIONAL second mechanism
    for the same invariant where GitHub offers one (never the floor); if it is ever recognized here it ships
    together with its workflow plumbing (StarshipSuperjam/engine-template#989), never detection alone.
    In TEAM the floor additionally requires a code-owner approval that survives the last
    push — the distinct-identity review the tier is sold on. The default is SOLO: the ENFORCEMENT paths (the standing
    CI check `main()` and bootstrap's apply/verify) resolve the real tier once via resolve_tier and pass it
    explicitly, so team protection is continuously verified; the default only serves an un-migrated informational
    caller (boot's orientation card — a tracked follow-up to make tier-aware), and under-reports team-specific rules
    there rather than mis-enforcing them."""
    types = {r.get("type") for r in rules}
    bound: set[str] = set()
    strict_checks = False  # freshness: does the required_status_checks rule require the branch to be up to date?
    pr_thread_resolution = False
    pr_params: dict = {}
    for r in rules:
        p = r.get("parameters") or {}
        if r.get("type") == "required_status_checks":
            for c in p.get("required_status_checks", []):
                if c.get("context"):
                    bound.add(c["context"])
            # FRESHNESS: strict_required_status_checks_policy makes GitHub require the head to be up to date with
            # the base before the required checks authorize a merge — so a green proven against an older base
            # cannot merge stale. The evaluated-rules endpoint surfaces this flag inside the rule's parameters
            # (confirmed live). A MISSING/unreadable flag reads as False here — fail toward not-fresh (RED),
            # never toward a false green, matching this module's fail-closed posture. ACCUMULATE with `or`, the
            # same union `bound` uses above: the evaluated response aggregates rules from EVERY ruleset that
            # targets the branch, so two required_status_checks rules can appear (the engine's own strict
            # ruleset created alongside an operator's non-strict one — bootstrap's ambiguous-arrival path). If
            # ANY applicable rule requires up-to-date, GitHub gates the branch on it (most-restrictive wins), so
            # freshness is satisfied when any of them is strict — never last-write-wins on GitHub's array order.
            strict_checks = strict_checks or bool(p.get("strict_required_status_checks_policy"))
        elif r.get("type") == "pull_request":
            pr_thread_resolution = bool(p.get("required_review_thread_resolution"))
            pr_params = p

    missing: list[str] = []
    if "pull_request" not in types:
        missing.append("a pull request is not required before merging")
    elif tier == TEAM:
        # The team floor's whole point: a distinct non-admin identity authors the engine's commits, so the operator
        # is the enforced code-owner reviewer — and that approval must not be bypassable by a post-approval push.
        if int(pr_params.get("required_approving_review_count") or 0) < 1:
            missing.append("in team mode, a change can merge without anyone's review approval")
        if not pr_params.get("require_code_owner_review"):
            missing.append("in team mode, a change can merge without a code-owner's approval")
        if not pr_params.get("require_last_push_approval"):
            missing.append("in team mode, a commit pushed after approval can merge without a fresh approval")
    # The required-checks floor is conditional on there being checks to require. In the brownfield-arrival
    # CHECKLESS bootstrap (required_checks == []), the engine deliberately binds no checks until its workflows
    # are on the branch (finalize), so an ABSENT required_status_checks rule is the intended state, not a floor
    # gap — reporting it would make a checkless apply falsely read as degraded. The enforcement paths (the
    # standing CI check and steady-state bootstrap/verify) always pass the frozen REQUIRED_CHECKS (non-empty),
    # so this stays fully enforced there; only the checkless bootstrap passes an empty set.
    if required_checks:
        if "required_status_checks" not in types:
            missing.append("status checks are not required to pass")
        else:
            for name in required_checks:
                if name not in bound:
                    missing.append(f"the required check '{name}' is not bound")
            # FRESHNESS is gated HERE, inside `if required_checks:` and only when the required_status_checks rule
            # is actually present — exactly like the check-binding floor above. A change with no required checks
            # (the checkless brownfield-arrival window, which strips the whole rule) has nothing to be fresh
            # about, so asserting freshness there would false-fail the arrival the checkless path exists to allow.
            if not strict_checks:
                missing.append("a change can merge without first being brought up to date with the base branch, "
                               "so a check that passed against an older base can still merge — turn on "
                               "'Require branches to be up to date before merging' in the branch rule to require it")
    if not pr_thread_resolution:
        missing.append("unresolved review conversations do not block merging")
    if "non_fast_forward" not in types:
        missing.append("force-pushes are not blocked")
    if "deletion" not in types:
        missing.append("branch deletion is not restricted")
    return missing


def emit(findings: list) -> int:
    """Write the finding.v1 array to stdout (the custom/script machine channel) and return
    0 — a successful evaluation, whatever it found. Each finding carries its own severity;
    the dispatcher's custom/script kind decides where the teeth land. Human-readable prose
    lives inside each finding's `message`, so stdout stays pure JSON."""
    print(json.dumps(findings))
    return 0


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    # The branch this merge-gate verifies: the workflow sets PROTECTED_BRANCH from the repo's AUTHORITATIVE
    # live default (github.event.repository.default_branch), which the resolver reads first; recorded ->
    # origin/HEAD -> "main" are the local/degraded fallbacks. Never raises (fail-soft) so the gate can only
    # emit a finding, never crash to an ambiguous disposition.
    branch = repo_identity.resolve_default_branch()
    token = os.environ.get("GITHUB_TOKEN", "")
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")  # the FINDING SEVERITY (hard/soft), passed by the kind
    identity_tier = resolve_tier()  # the repo's solo/team IDENTITY tier — DISTINCT from the severity above; decides
    #                                 which floor the standing CI check verifies, so a team repo's stronger floor is
    #                                 continuously enforced (not just the solo baseline).
    if not repo or not token:
        # Local / no credentials: FAIL OPEN with a soft note — a soft finding never blocks,
        # and the CI run (which has a token) performs the real check. Mirrors the presence
        # kind's fail-open-locally posture; never a false local block.
        # WITNESS-DEFERRED, not merely not-applicable: this check DOES enforce in CI, it just had no
        # witness (a repository token) in this run — so the validator lifts it onto its elevated
        # "not verified in this run — enforces in CI" line, never folding it into "nothing to do"
        # (StarshipSuperjam/engine-template#761). The markers ride through the custom/script boundary's allow-list
        # (validate.witness_deferred is the canonical shape; mirrored here since this tool does not
        # import validate). not_applicable stays set so every prior fail-safe path still holds.
        return emit([{"severity": "soft", "location": None, "not_applicable": True,
                      "witness_deferred": True, "missing_witness": ["GITHUB_REPOSITORY", "GITHUB_TOKEN"],
                      "message": "Branch protection was not checked in this run — no repository "
                      "access token is available, which is normal on your own machine. The "
                      "check that can actually block a bad merge runs in CI."}])
    posture = recorded_posture()  # an operator-consented 'this plan can't host protection' acceptance, or None
    try:
        rules = get_json(f"/repos/{repo}/rules/branches/{urllib.parse.quote(branch, safe='')}",
                         token, user_agent=UA)
    except urllib.error.HTTPError as e:
        # The read failed with an HTTP status. Softens to an honest WARNING ONLY when BOTH the operator
        # recorded an unsupported-platform posture AND this 403 genuinely carries GitHub's plan-limitation
        # signature (platform_forbids_rulesets excludes rate-limit/incident/permission 403s). Any other
        # failure — no posture, or a 403 that isn't a plan limit — stays HARD, exactly as before.
        if posture and http_error_forbids_rulesets(e):
            when = posture.get("recorded_on") or "an earlier date"
            who = posture.get("operator_login") or "the operator"
            return emit([{"severity": "soft", "location": None,
                          "message": f"Branch protection isn't available on this repository's GitHub plan, so "
                          f"the safety gate can't be enforced on '{branch}'. Running without it was accepted "
                          f"on {when} (recorded by {who}) — a known, accepted limitation, not a failure to "
                          f"fix. If your plan later supports branch rulesets, run `python "
                          f".engine/tools/bootstrap.py apply` and this note stops applying."}])
        return emit([{"severity": tier, "location": None,
                      "message": f"Branch protection could not be verified for '{branch}' "
                      f"({e}); treating it as not in force until confirmed."}])
    except Exception as e:  # token present but the API could not be read (network, etc.) -> fail closed in CI
        return emit([{"severity": tier, "location": None,
                      "message": f"Branch protection could not be verified for '{branch}' "
                      f"({e}); treating it as not in force until confirmed."}])
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        # A 200 with an unexpected body is NOT a confirmation that protection is in force — fail CLOSED
        # (mirrors boot's twin guard). This checks BOTH the outer container AND the elements: a list of
        # non-dicts (e.g. [1, 2, 3]) would otherwise crash missing_floor's `r.get("type")` into an uncaught
        # exception (missing_floor runs below, outside the read's try) and an ambiguous disposition.
        return emit([{"severity": tier, "location": None,
                      "message": f"Branch protection could not be verified for '{branch}' (the rules "
                      "response was not in the expected form); treating it as not in force until confirmed."}])
    missing = missing_floor(rules, REQUIRED_CHECKS, tier=identity_tier)
    if missing:
        # The read SUCCEEDED, which proves this plan CAN host rulesets — so a posture recorded here is now
        # stale (e.g. the plan was upgraded) and must NOT soften anything: this stays a HARD finding, and we
        # nudge the operator to clear the stale record. This is the "should be available but missing" case the
        # design preserves as red.
        stale = ""
        if posture:
            stale = (" (This repository also carries a recorded 'protection unavailable on this plan' "
                     "acceptance, but its plan now supports branch protection — that record is stale; turning "
                     "protection on with the command above clears it.)")
        return emit([{"severity": tier, "location": None,
                      "message": f"The protected-branch safety gate on '{branch}' is not fully "
                      "in force: " + "; ".join(missing) + ". Until this is on, a change can reach "
                      "the protected branch without the required checks or a pull request. If the engine was just added to "
                      "this project, run `python .engine/tools/bootstrap.py finalize` to turn its "
                      "required checks on now that their workflows are on the branch; otherwise "
                      "complete the branch-protection setup you were handed, then re-run." + stale}])
    return emit([])  # protection is fully in force


if __name__ == "__main__":
    sys.exit(main())
