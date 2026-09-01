---
title: Bootstrap the control plane — turn the protected-branch safety gate on
---

## Purpose

How the engine turns on the **branch protection** that makes a non-engineer's merge gate real — the number-one
trust dependency every other guardrail sits downstream of. The branch ruleset is a GitHub *setting*, not a file,
so it does not travel with the template and must be applied once per repository by an operator-privileged actor.
This runbook is the permanent, re-runnable mechanism, and a typed lifecycle transaction:
`transaction.py plan control-plane-bootstrap` previews it, changing nothing. Two doors apply it, and both
trigger the same GitHub authorization screen you approve in your browser — the consent gate: the command you
type yourself (`tools/bootstrap.py apply`, or the `engine-enable-protection` skill), and the transaction's own
`transaction.py run control-plane-bootstrap --consent-handle <handle>`, which re-derives the previewed plan
before applying. (The post-merge `control-plane-finalize` binds the checks through the same two doors —
`bootstrap.py finalize` or `transaction.py run control-plane-finalize`.) The
operator-facing copy is the `control-plane-bootstrap` template (`.engine/templates/control-plane-bootstrap.md`). Enter this
runbook to understand or explain how protection is turned on, why it needs the operator, and what happens when
it cannot be turned on.

## Steps

The operation is **idempotent and safe to run any time** — it never weakens protection already in place, and a
re-run when the gate is already on is a clean no-op. The engine **cannot grant itself** the permission to set
protection rules; the GitHub authorization screen is the consent gate, the same merge-as-consent model the rest
of the engine rests on.

The **protection floor** is the thing being put in force: a pull request before merging, the engine's required
checks bound, resolved review conversations, no force-push, no deletion (reused from the committed
`protection_guard`). If that floor is already fully in force — by any ruleset, the engine's own or the
product's — there is nothing to do but ensure the engine's label exists and report the no-op.

Whether the operator's login can administer the repository is read from the token's scopes. When it cannot, the
operator first sees a **pre-bootstrap explanation** — plain language pre-translating the permission they are
about to grant — then the authorization screen (`gh auth refresh`); the engine then **verifies the permission
actually persisted**, because some sign-in flows complete without saving it. Applying the floor **creates the
engine's own named ruleset, or repairs it in place** — *augmented, never weakened*: it never removes or loosens
a product's existing rules. (The reverse — de-bootstrapping the engine's binding on clean removal — shipped in
core; in-place augment of a pre-existing *product* ruleset is a later brownfield step; both are named in the
tool's header.) The engine then **re-reads to confirm the floor is genuinely in force — never assuming the write
took** — and ensures the engine-domain label exists.

Where the permission genuinely cannot be obtained, the engine **degrades, never fakes**: it discloses in plain
language the concrete risk ("branch protection is not active — work can reach the branch without the required
checks or a pull request"), never reports the gate on when it is not, never auto-deletes or weakens protection,
and gives a next action matched to the cause — if the operator does not administer the repository, forward the
one-time setup to whoever does; if an org policy blocks the permission, point the operator at their org admin
(**team mode is NOT an escape** — its identity is deliberately non-admin, so it cannot hold the blocked
branch-protection permission); if the approval did not save, retry. Never a dead-end. This runbook runs the
**single first-run attempt and surfaces its own outcome**; the **standing** "your safety gate is off" reminder
across every later session is [boot](boot-session-start.md)'s, rendered from the same evaluation.

## Done when

The protection floor is confirmed in force on the protected branch (a pull request, the engine's required
checks, resolved conversations, no force-push, no deletion) — or, where the permission could not be obtained,
the operator has been told plainly that protection is off, why, and the one concrete next step, with no silent
green. The engine-domain label exists. A re-run when the gate is already on changes nothing.

## Notes

**The skeleton is posture; the only wall is the protected-branch merge.** This operation *establishes* that
wall. Until it runs successfully, the committed CI guard fails loud on every pull request and boot surfaces the
unprotected state every session, so an unprotected repo is never silently the operating baseline — but nothing
mechanically forces the operator to complete it. That honest limit is the same one the other lifecycle
operations carry; the structural close for the residual (an engine that holds the operator's credentials in solo
and *could* act on the ruleset) is the operator's choice of the team identity tier.

**The token-handling detail is the corrected build-spec leaf.** The locked design illustrates the required
permission as `admin:repo_ruleset`; that is not a real GitHub scope (verified against GitHub's live
documentation), so this operation uses the standard `repo` permission (or a fine-grained "Administration"
permission), which a normal GitHub login already carries. The locked *contract* — operator-privileged actor,
consent at the authorization screen, verify-after, degrade-never-fake, the protection floor — is unchanged; only
the inaccurate scope name is corrected, and the design prose is flagged for amendment.

**Brownfield arrival binds protection in two phases (`checkless` → `finalize`).** A required status check can
only report once the workflow that emits it is on the branch — but on a brownfield arrival the engine's own
workflows (`engine-ci`, `engine-guard`) arrive *inside* the arrival pull request, so binding those checks at
arrival would make that pull request unmergeable: the same deadlock, in reverse, that `de_bootstrap` avoids by
stripping checks *before* the workflow-deleting removal. So the arrival applies the floor in **checkless** mode
(`ControlPlane(checkless=True)` ⇒ an empty `required_checks`), writing a *tier-aware* floor minus the
required-checks rule (`checkless_floor_ruleset`, **not** the SOLO-pinned de-bootstrap `remainder_ruleset`, so a
team arrival keeps its team protections) and augmenting only wholly-missing floor rule types. The branch is
protected (pull request required, no force-push, no deletion) and the arrival pull request can merge. After it
merges, the one-time **`finalize`** verb — a permanent `bootstrap.py` primitive that survives the instantiator's
self-retirement — binds the checks: it confirms both workflows are on the branch first (refusing fail-closed
rather than re-create the deadlock), runs a normal non-checkless apply, re-emits the Actions-enablement reminder,
and **unions** its reversal marker with the arrival's so a later `de_bootstrap` reverses exactly what arrival
*and* finalize added. `protection_guard`'s standing check keeps reporting honestly throughout, always evaluating
against the full frozen `REQUIRED_CHECKS`; its `missing_floor` skips the checks gap only when handed an empty
required-checks set, and the checkless arrival's internal verify is the **one caller** allowed to pass one — the
standing check never does. Honesty never becomes a deadlock.
