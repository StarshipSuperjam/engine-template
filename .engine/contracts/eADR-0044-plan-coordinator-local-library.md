---
id: eADR-0044
title: Planning has a coordinator, and its plans live locally
status: accepted
date: 2026-08-23
---

_Amended 2026-08-25: the component is retitled. It shipped as the **Plan Coordinator**, and that named one duty out of several — it also manages task completion, owns the continuous-improvement workflows, and organizes work across build phases through the program object — so on the operator's decision of 2026-08-24 it is now the **Project Manager**, and this record and everything else that speaks of it in the present tense says so. Nothing the decision below decides changes; only the name of the thing it decides about. The retitle stops at the DATA BOUNDARY, and that line is drawn deliberately: the schema ids (`engine-plan.v1`, `plan-record.v1`, `engine-program.v1`) and the `plan` verb namespace name the ARTIFACTS rather than the component, and renaming a schema id would invalidate every record already stored in every deployed project — no rename is worth an unreadable library. Stored plan-library and program records keep their contents, and history keeps its names: merged pull-request titles, this file's own name, and the decision records that cite it were written under the old title and remain accurate as history. So the old name survives in exactly two kinds of place — this file's name, and prose speaking about the past — and nowhere else._

## Decision

Planning gets a mechanical lifecycle owner of its own, paired with the Build Coordinator: a Project Manager that carries a plan from raw intent through deliberation, approval, one cold review, and a terminal seal. Its plans live in a durable, gitignored, per-instance library at `.engine/plans/` in the project's canonical checkout — the only copy of what they hold — as immutable JSON revisions with a derived status and no stored lifecycle field. The library is the plan's home; the pull request remains the claim, and everything reviewable about a plan still reaches the PR through the composed contract.

## Significance

This fixes where planning state lives and what owns its lifecycle, and it is worth keeping because it is bought with a real exception rather than inherited from precedent.

Two facts forced it. Coordinator state kept in OS temp was observed to vanish across a reboot, taking a session's planning with it. And plans decay silently under revision — three successive drafts of the plan that produced this record each dropped obligations no mechanism noticed, which is why the multi-PR program object exists alongside the library and why its one guarantee is that a declared obligation cannot vanish from its successor without someone saying so.

What later work must respect. The library is the exception eADR-0003 grants in its amendment of this date, scoped to planning state and to nothing else — no later store may reach for it by analogy, and every other store still answers eADR-0003's original question on its own merits. Plan authority is JSON-only: immutable revisions, mutated only by minting a new one, with no YAML working copy and no uncheckpointed-edit state. There is exactly ONE cold plan review per approved revision; folding fixes in afterwards is covered by a single proportional judgment of the reviewed-to-sealed delta, never by another panel, because a plan whose every revision re-triggered a panel would never converge — the same failure BC-16 and BC-17 already removed from the Build side. The seal is terminal and nothing locks before it: a plan carrying blocking findings stays an unsealed, editable draft rather than entering a limbo it cannot leave. Nothing auto-selects a plan, ever — a library is a shelf, not a queue. And the honest cost is binding: recovery is workstation-only, and the PR's account of what was agreed cannot be externally verified against the store, so a session that cannot reach the library must say so rather than reconstruct.

## Rationale

The Build Coordinator becomes authoritative at `plan bind`, which already requires an open draft pull request. Everything upstream of that — grounding, deliberation, authoring the graph, presenting it, deciding it is good enough — was convention a session was trusted to remember, and a Build is only ever as good as the plan it executes. Giving planning the same mechanical owner execution has is the smallest change that closes that gap.

Local rather than published, because the material is different in kind from a Build's. A plan under deliberation is the operator's own working thought: it holds raw intent verbatim, may name things which must not become public, and its usefulness ends when the PR it authorized merges. Publishing it into Issue machine blocks was tried and rejected on the operator's own objection — good plans are far too large for comments, and data blocks create noise only one person can decode. Durable rather than session-held, because the observed failure is loss, and a store that dies with the process is exactly what was lost.

The trade accepted: a gitignored library is a record no reviewer, no CI check, and no second machine can see. That is a real cost and it is paid deliberately, mitigated by keeping the reviewable half — objective, obligations, scope, risks, findings, dispositions, and the reviewed-to-sealed delta — flowing to the PR where the operator's merge still judges it.

## Anti-choice

The strongest alternative was the incumbent position this record carves out from: keep a Build's plan session-held, non-durable, and promoted to a writable Issue only when cold continuation genuinely demands it — eADR-0025's actual answer, chosen against real evidence that a private receipt chain produced recursive audits without improving any pull request.

It lost on two grounds, neither of which is that promotion is bad. First, promotion solves transport and not decay: a promoted plan is still one document with no revision chain, so nothing can tell that revision four quietly dropped an obligation revision two promised — the failure actually observed, three times, in this record's own drafting. Second, promotion is durable only where GitHub is reachable and only for the plans someone remembered to promote, which makes durability a thing an operator must anticipate needing rather than a property planning has.

What survives from the rejected alternative is its warning, and it constrains this design: a private ledger that nobody reads is worse than no ledger, so the library is deliberately not a parallel audit trail. It holds the plan and its revisions; the judgment still lands on the pull request, and the operator's merge is still the only wall.

## Status

accepted
