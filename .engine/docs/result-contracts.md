---
title: Executable persona result contracts
---

# Executable persona result contracts

## What this covers

The Engine binds a versioned result contract to dispatch and validates the entire observed report
before accepting it. JSON syntax alone earns no credit. The registry in
`tools/result_contracts.py` names each schema, producer role, compiler, ingress handler and enforcement level.
The agent-coherence check runs positive and deliberately invalid reports through every structured handler.

## What you need to know

| Producer | Contract | Enforcement |
| --- | --- | --- |
| Plan reviewers | `plan-review-finding.v1` | Canonical ingress and observed-output equality |
| Deliverable and repair reviewers | `pre-submission-review-finding.v1` | Canonical ingress and observed-output equality |
| Workers | `worker-result.v1` | Canonical ingress, claim identity and existing integration checks |
| Audit digest | `audit-finding.v1` | Intentional prose |
| Audit conformance block | `conformance-verdicts.v1` | Whole-block validation only |
| Grounding and validation scouts | `grounding-brief.v1`, `validation-digest.v1` | Intentional prose |

Dispatch carries the canonical schema, its digest (including the selected local-reference closure),
enforcement level and limits. Worker claims and scoped assignments freeze that binding. A missing or
changed binding refuses new acceptance. Native structured formatting is currently unqualified; the
Engine makes no provider-format guarantee and always runs canonical ingress.

### Reviewer reports

Return a complete JSON array:

```json
[{"severity":"serious","message":"The failure path loses evidence.","location":{"file":"tools/example.py","line":42}}]
```

`location` may be null; `line` may be absent or null. New plan findings store that exact object or null,
including the distinction between an absent line and an explicit null line. File paths are not normalized
or parsed for line numbers: `{"file":"a:12"}` remains different from `{"file":"a","line":12}`.
Deliverable and repair findings retain their existing text projection and exact retained semantic report.
Finding IDs, lens identity, dispositions
and receipts are controller-owned and forbidden in model reports. `[]` is a completed empty report.
Null, absent output and partial clarification messages are not completed review.

Project Manager `review record --findings <file>` accepts that array for one lens, or an object mapping
each named lens to its complete array for a panel. A supplied copy must equal the observed output, including
array order and every semantic value. JSON whitespace and key order may differ. Omitting the caller's file
derives findings directly from the observed completed report; it never manufactures an empty review.
`review amend` uses the same boundary for additional lens coverage.

Build `review record --report <file>` accepts a raw deliverable or repair report and compiles finding IDs.
The existing `--finding` ID list or `--findings-from-file` controller batch remains available, but its
coverage must match the observed report. Initial severity and summary must match too, whether the
disposition is recorded before or after its receipt. Later controller corrections remain separate.
Project Manager's explicit `--controller-findings` option accepts a controller projection with chosen IDs;
it must supply the exact observed location as well as count, order, severity and message. A missing location
or a flattened string cannot replace an observed object or null. Raw producer output never enters through
that option.

Old plan records may contain a nonempty string location or no location at all. They remain readable and
are not migrated or reinterpreted. Explicit `finding amend --location` corrections may still use text;
those corrections remain separate from the immutable original report. Export/import and reindex preserve
the authoritative record and seal; a bundle alone still does not transfer fresh execution evidence.
The durable schema references the producer's location definition using bounded local reference resolution
for both complete records and finding fragments. Existing internal recursive record definitions retain
their native validation behavior; remote schema retrieval and references escaping the schema directory
are not enabled.

The producer schema and its complete resolved binding are unchanged by this location repair, so an
already accepted review of an unsealed plan remains verifiable and can finish its existing ceremony.
The producer schema's older descriptive claim that no mechanical check resolves its contract is stale;
the registry and ingress behavior described here are current. That fingerprinted sentence is deliberately
left unchanged until a supported contract-transition design can update it without invalidating old reviews.
Older Engine versions are not promised to read newly structured plan records; upgrade the reader before
opening those records and preserve the original library when considering a downgrade.

The private scoped companion retains validated semantic reports, original output digests and bindings,
linked to the published consumer receipt. `finding amend`, `finding dispose` and Build `finding record`
remain separate controller corrections or adjudications. They do not rewrite the original report.

### Worker reports

```json
{
  "outcome":"failed",
  "reason":"Cannot complete while full verification fails.",
  "evidence":{
    "changed_paths":[],
    "verification_results":[
      {"command":"focused checks","outcome":"passed","detail":"3 passed"},
      {"command":"full checks","outcome":"failed","detail":"13 failures, 3 errors","exit_code":1}
    ],
    "assumptions":[],
    "unresolved_concerns":["Full verification remains red."]
  }
}
```

All four evidence arrays are required, including when empty. Verification outcomes are `passed`, `failed`,
`not-run` or `blocked`; each entry requires command, outcome and detail. A failed report requires a nonempty
reason and earns no integration. These are preserved self-reports, not proof that checks ran or passed.

In worker-commit mode, a returned report includes the 40-hex `artifact_ref` claim. In accepted-candidate
mode, stage the candidate before `work result`; the Engine observes the staged tree and records its digest.
Do not include `base_sha`, `attempt_id`, `artifact_digest`, receipt fields or a failure class in a report.
The trusted claim supplies attempt, base and route. Existing scope, ancestry, reachability and artifact
checks still run at integration. A rejected report changes neither Build state nor retry count. There are
no automatic retries or format-repair loops. Full worker reports stay in the private canonical snapshot;
public handoffs omit that duplicate and redact the projected evidence prose.

### Limits, refusal and history

Result files and stdin are read with a 1 MiB bound before decoding. JSON must be UTF-8, contain no duplicate
keys or nonfinite numbers, and stay within depth 64, 10,000 values (including object keys), 1,000 items per
array, and 65,536 UTF-8 bytes per string. Schema loading accepts local references only, rejects recursion,
and bounds reference expansion. Validation stops at its first error. Closed semantic objects reject unknown
fields before conversion or enrichment.

Refusals carry `result-rejection.v1` with category, rule, path, contract and a bounded diagnostic. Categories
are syntax, schema, semantic, stale-attempt and authority. The Project Manager and Build CLI emit these
refusals as JSON on stderr with a nonzero exit. Payload values and unknown object keys are not copied into
diagnostics. Oversized native final outputs are replaced by a bounded refusal before hashing or retention;
a later valid completion can recover the same assignment.

Historical records remain readable. Missing bindings and old acceptances never gain newly verified
contract evidence. For an active legacy worker claim, use the existing explicit abandon/retry/new-claim
path; do not edit or restamp history. Fresh review recovery needs a newly bound, observed assignment. The explicit historical adoption
route below can preserve narrowly proven older evidence; it never upgrades a prior seal or reports
historical-unverified evidence as fresh execution.

Audit is narrower: the typed adapter distinguishes absent, rejected and valid full conformance blocks.
The outer body is read in bounds too; an oversized body is refused and its machine tail stripped by a
bounded streaming scan. Only valid divergences are promoted. Rejection is disclosed; the outer audit still strips the block and
continues successfully, and independently derived degradation notices keep their policy. No audit result
receipt or durable retention of every meets/unsure verdict is promised here; issue StarshipSuperjam/engine-template#815 owns that work.

### Falsification demo

Run `uv run --directory .engine --frozen -- python tools/project_manager.py demo-locations` to watch
exact file/line, whole-plan and ambiguous-path locations survive real record/amend commands, and invalid
reports leave records unchanged. `--break-preservation` deliberately restores lossy flattening in memory;
`--break-ingress` deliberately bypasses raw validation. Each broken mode must exit nonzero. The demo
runs permanent regression witnesses in temporary libraries with synthetic transport observations; it
does not use your real plan library or claim to qualify live reviewers.

Run `uv run --directory .engine --frozen -- python tools/demo_result_contracts.py`.
It executes five command-level checks against disposable stores with synthetic transport observations,
including observed-report substitution, unchanged stores on rejection and stale-attempt refusal.
Add `--break-ingress` to bypass validation in memory: the demo must exit nonzero. It does not qualify a
live provider or modify the project's real plan library.

### Frozen reviewer contracts and renewal

Approval retains one `reviewer-contract.v1` envelope for both plan and Build reviewers. It records the
approved plan referent, depth and roster, original source bytes, resolved provider models and complete
result-schema bindings. Exact source provenance is separate from semantic identity: editorial text or
path changes do not spend review credit. A changed mandate version, model, allowed effects, result schema
or enforcement descriptor does. Reviewer effort remains harness-controlled, with no fixed value or floor.

Persona authors keep `reviewer-contract` stable and bump `reviewer-contract-version` when the mandate
changes. Structured semantic changes are detected even if the author forgets that bump. The Engine cannot
infer whether arbitrary prose changed the mandate; declaration discipline remains part of review.

Use `project_manager.py review-contract preview PLAN --action retain|adopt --output PREVIEW` and
`review-contract apply PLAN --input PREVIEW --reason REASON --operator-decided` for explicit
per-lens retain/adopt decisions before sealing. The Build owns subsequent changes through `review
contract-preview --plan PAYLOAD --action retain|adopt --output PREVIEW` and `review contract-apply
--plan PAYLOAD --input PREVIEW --reason REASON --operator-decided`. Apply consumes the reviewed preview, a reason and
`--operator-decided`; stale previews refuse. Retain keeps the original obligation. Adopt adds the changed
obligation and requires its review; unaffected lenses keep credit. The original roster cannot silently
shrink. Unavailable old mandates require a decision, not a substituted fresh assignment.

To mix decisions, add repeatable `--adopt-lens ROLE:LENS` options to an `--action adopt` preview.
For example, `--adopt-lens pre-submission-review:technical-integrity` adopts that changed lens while
retaining the other obligations. Plan reviewers use `plan-review:architecture` and the corresponding
lens names. Omit the option to adopt all proposed changes, or use `--action retain` to retain all.
The preview and durable decision list each changed lens's action. A retained decision covers only the
exact semantic change that was shown; a later change requires another decision.

Plan `show` and Build status/PR output disclose editorial source changes using reviewer identities and
old/current hashes. They publish no source paths or reviewer prose. PLAN.md lists retained source hashes
and points to `show` for the current comparison. Comparing provenance never rewrites the approved packet
or spends coverage.

Supplemental findings join the original findings, dispositions and presentation lineage. New findings
must be settled and presented before sealing. Build renewals and finding changes invalidate prior PR
contract/preflight evidence. A Build model renewal retains the original plan panel’s resolving policy as provenance; repeated renewals
keep that original policy and validate its resolved models. Semantic credit does not prove a reviewer read new authored changes: Git
range coverage is checked independently, including after reconciliation.
Replacing a receipt cannot drop an undispositioned or still-blocking finding. Its original receipt
continues to require explicit resolution even when the replacement reports no new findings.

### Historical adoption and its limits

The original plan/Build backup, original review packets and retained source Git objects are the recovery
inputs. The source SHA must already be named in original review authority. Dates, an asserted old version,
a current installation, or a digest alone are insufficient. The locator JSON has exactly `source_root`,
`source_commit`, `backup` and `packets` (a list of packet paths). The source code is read as data, never run.

For an unbound plan, use `project_manager.py review-contract historical-preview PLAN --input LOCATOR
--output PREVIEW`, inspect the recovered roster, cohort, receipts and gaps, then `review-contract
historical-apply PLAN --input PREVIEW --reason REASON --operator-decided`. A bound plan directs you to its
Build: `review historical-preview --plan PAYLOAD --input LOCATOR --output PREVIEW`, then `review
historical-apply --plan PAYLOAD --input PREVIEW --reason REASON --operator-decided`, with the usual Build
owner and revision arguments. The locked apply rechecks the preview. Exact retry is idempotent.

Observed pre-envelope reviews retain their original collector companion and original result binding.
Deleting that companion still blocks submission. Only sources demonstrably predating the collector can
use the separate **historical-unverified** cohort, and only for the exact retained receipt keys, owner,
generation, findings and original read ranges. Missing original deliverable receipts, packets or findings
cannot be repaired by adopting a later repair panel. An actually unreviewed observed-era Build can adopt
its recovered contract with zero execution credit. Closed history remains unchanged; unapproved plans use
ordinary approval; a modern envelope cannot downgrade into this route.

Adoption appends a decision and discloses historical gaps in status and the PR contract. It preserves the
original approval, seal, packets, receipts and companion. It neither stamps today's schema onto old reports
nor turns an old receipt into a fresh observed review. Keep the original artifacts when changing readers;
older Engine versions are not promised to understand these records.

### Reviewer contract demonstration

Run `uv run --directory .engine --frozen -- python tools/project_manager.py demo-review-contracts`.
The two acceptance matrices cover StarshipSuperjam/engine-template#1087 (identity and per-lens renewal) and StarshipSuperjam/engine-template#1127 (frozen approval and
historical continuity). Disposable Git histories exercise a real rebase, packet refresh, candidate
commands, preflight and submission without a new reviewer launch. They also prove refusal after evidence
loss or a new unread authored delta. GitHub responses and transport observations are synthetic; this
witness does not claim live reviewer qualification or a real CI run.

Each of `--break-identity-preservation`, `--break-envelope-validation` and
`--break-fresh-legacy-separation` deliberately breaks one safeguard in memory and must exit nonzero
because its acceptance assertion fails. Nothing touches the real plan library.

### Cumulative review coverage demonstration

Run `uv run --directory .engine --frozen -- python tools/demo_review_coverage.py` to exercise an
initial five-lens review, four repairs by one lens, packet refresh/retry, a clean target merge and a
later authored edit. It checks immutable originals, surviving findings, zero redundant repair
assignments, a successful production submission preview, and refusal when a middle read is missing.
`--lose-coverage` deliberately removes retained reads; `--overcredit-gap` deliberately credits unread
work. Each must exit nonzero because a behavioral assertion fails.

The Git history and coordinator coverage, receipt, finding and submission owners are real. Native
event transport, CI/preflight and GitHub observations are disposable synthetic fixtures. This evidence
does not replace live reviewer qualification or final CI. The demo's permanent fate is regression
coverage in `TestCumulativeReviewScenario` and `test_review_economy.ReviewCoverageDemo`.

The regression suite also alternates repair lenses. Each lens retains its original deliverable
obligation and the repair ranges assigned to it; other recorded proportional repairs are excluded,
not described as reads. A later edit without such a decision remains unread, including after a
reverified rebase. Refreshing the deliverable packet asks its new whole scope. Execution disclosure
includes retained original receipts, so a later nonexecuting review cannot hide an earlier execution.
Historical receipts without an execution declaration remain unchanged and are disclosed as unknown;
a fresh current receipt supplies its own declaration without claiming what the earlier reviewer did.


### Fix-first repair completion demonstration

Run from the repository root:

```sh
uv run --directory .engine --frozen -- python tools/demo_repair_completion.py
```

The real-Git scenario accepts a finding, shows that accepted-fixed at its own original review commit
cannot submit, lands a fix, completes an independent repair panel with another finding, lands that
repair, and reaches production submit preview through an explicit terminal direct-verification decision.
Original receipts and findings remain intact. The decision names its exact range, approved authority,
candidate evidence, rationale and concrete checks; it does not claim independent review of that range.
A same-commit decision or reference change requires fresh PR disclosure and preflight. New candidate
evidence requires reassessment. A later authored edit requires another proportional judgment.

Run the deliberate controls separately; each must exit nonzero with an `AssertionError`, not a harness error:

```sh
uv run --directory .engine --frozen -- python tools/demo_repair_completion.py --bypass-own-commit-hold
uv run --directory .engine --frozen -- python tools/demo_repair_completion.py --lose-originals
uv run --directory .engine --frozen -- python tools/demo_repair_completion.py --omit-decision-freshness
```

The controls suppress the unchanged-commit hold, discard retained originals, or omit decision freshness.
The normal scenario runs once in the permanent suite; fault controls reuse it and stop at their violated
assertions. Native reviewer transport, GitHub and candidate/final CI inputs are explicitly synthetic.
This demonstration does not qualify live review execution or replace the Build's real validation and QA.
