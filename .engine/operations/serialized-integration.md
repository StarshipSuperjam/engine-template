---
title: Serialized cross-PR integration — one candidate at a time, never merged by the engine
---

## Purpose

How reviewed pull requests reach the protected branch safely when more than one is in flight. Concurrent
sessions and worktrees make the repository multi-writer (eADR-0042), so integration into the canonical branch
is **serialized**: reviewed candidates enter an ordered path, exactly one is admitted at a time, it is brought
up to date and proven fresh against the current main, and it is surfaced ready for **your** merge. The engine
orders and proves; it never merges — your merge of the protected branch remains the only binding gate. Enter
this runbook to understand or explain how the integration queue is driven and why one candidate integrates at
a time. The mechanism is `.engine/tools/integration_queue.py`; the provider backend seam is
`integration_queue_backend.py` (a GitHub-native merge queue where available — deferred, see
StarshipSuperjam/engine-template#989 — and an engine-controlled serialized fallback everywhere else).

## Steps

1. **A pull request joins the queue when it is reviewed.** Add the `engine-integrate-ready` label and take the
   pull request out of draft. In a team deployment "reviewed" also means an approval that survives the last
   push (GitHub's own `require_code_owner_review` is the binding code-owner gate at merge); in a solo
   deployment there is no distinct reviewer, so the label plus out-of-draft is your *acknowledged readiness*,
   not an independent review. `engine-integrate-priority` promotes a candidate ahead of the pull-request-number
   order.
2. **See the queue.** `integration_queue.py status` lists the ordered reviewed candidates and which pull
   request currently holds the single integration slot; `next` names the one that is next and why.
3. **Prepare the admitted candidate.** `integration_queue.py prepare` admits the next candidate (a singleton
   `engine-integrating` label marks the one slot), and when that candidate is this session's own branch it
   brings the branch up to date against current main and regenerates its derived files — refusing any conflict
   in files a human edited and leaving both branches untouched for your decision. It proves the candidate
   mergeable and reports whether its checks are green against current main; it never merges.
4. **Merge, then advance.** When the candidate is surfaced ready, merge it yourself. Then
   `integration_queue.py advance` releases the slot so the next candidate can be admitted. If a session ever
   leaves the slot stuck on a pull request, closing that pull request drops it from the queue and frees the
   slot — the queue reads GitHub live, so nothing is left permanently wedged.
5. **Freshness is enforced by GitHub, not by the queue.** `prepare`'s readiness report is an advisory
   pre-flight; the binding stale-green blocker is the strict required-status-checks ruleset
   (StarshipSuperjam/engine-template#915) GitHub enforces at your merge click. A missed, stale, or unreachable queue signal never makes the
   result incorrect — the reconcile/merge path still recovers it.

## Done when

A reviewed pull request has been admitted as the single integration candidate, brought up to date against the
current protected branch with its derived files regenerated, surfaced to you as ready with its checks green,
merged by you, and the slot released so the next candidate can be admitted — with no candidate ever merged by
the engine and no authored conflict guessed away.

## Notes

**Advisory coordination rides this lifecycle, it never gates it.** When session coordination is active
(`session-coordination.md`, eADR-0043), admitting or blocking a candidate at `prepare` emits an advisory
notice on the pull request concerned — "a slot opened; prepare and re-check the queue". The *merge-reaction*
signals — "the base advanced; re-prove against the current main" (revalidation), "a merge touched your
declared surface" (dependency-update), and "you're next in the queue" — do NOT ride the `advance` verb, which
is a human afterthought to a merge (and by the "merge, then advance" flow the merged pull request is already
closed by then). They fire deterministically from the merge EVENT: a `pull_request: closed` workflow
(`engine-coordination-postmerge.yml`) runs on merge and posts them to the affected candidates' boards. These
make the lifecycle cheaper and less surprising for concurrent sessions, but they carry no authority: a session
still acts only by re-running `prepare`/`prove_ready` against live state, freshness is still the ruleset's at
your merge, and a missed or forged notice changes nothing (the reconcile/merge path recovers it). The overlap
warning prompts sequencing; it is never a lock.

**Toward the merge-queue outcome without a native merge queue.** The O(n²) re-prove churn the strict
freshness lane (StarshipSuperjam/engine-template#915) can incur across concurrent candidates is what a native GitHub merge queue
(StarshipSuperjam/engine-template#989) would remove. Session coordination offers a second route to the same *outcome*: if sessions order
their own preparation around the singleton admission — each preparing only when it is the admitted candidate,
rather than every candidate re-proving on every advance of main — the queue is walked in O(n) prepares
without any `merge_group` machinery. Whether that makes the strict up-to-date requirement's churn a
worthwhile trade to relax is a decision for when the measurement (`coordination_notice.py`'s ring, read via
the coordination metrics view) shows how often re-prove churn actually survives coordination; until then the
strict lane stays the enforcement floor and StarshipSuperjam/engine-template#989 stays parked.

