---
title: Executable persona result contracts
---

# Executable persona result contracts

The Engine binds a versioned result contract to dispatch and validates the entire observed report
before accepting it. JSON syntax alone earns no credit. The registry in
`tools/result_contracts.py` names each schema, producer role, compiler, ingress handler and enforcement level.
The agent-coherence check runs positive and deliberately invalid reports through every structured handler.

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

## Reviewer reports

Return a complete JSON array:

```json
[{"severity":"serious","message":"The failure path loses evidence.","location":{"file":"tools/example.py","line":42}}]
```

`location` may be null; `line` may be absent or null. These distinctions survive in the retained semantic
report even though the finding display renders locations as text. Finding IDs, lens identity, dispositions
and receipts are controller-owned and forbidden in model reports. `[]` is a completed empty report.
Null, absent output and partial clarification messages are not completed review.

Project Manager `review record --findings <file>` accepts that array for one lens, or an object mapping
each named lens to its complete array for a panel. A supplied copy must equal the observed output, including
array order and every semantic value. JSON whitespace and key order may differ. Omitting the caller's file
derives findings directly from the observed completed report; it never manufactures an empty review.
`review amend` uses the same boundary for additional lens coverage.

Build `review record --report <file>` accepts a raw deliverable or repair report and compiles finding IDs.
The existing `--finding` ID list or `--findings-from-file` controller batch remains available, but its
coverage must match the observed report. A controller batch's initial severity and summary must match too.
Project Manager's explicit `--controller-findings` option accepts a controller projection with chosen IDs;
it cannot replace the observed count, order, severity or message. Raw producer output never enters through
that option.

The private scoped companion retains validated semantic reports, original output digests and bindings,
linked to the published consumer receipt. `finding amend`, `finding dispose` and Build `finding record`
remain separate controller corrections or adjudications. They do not rewrite the original report.

## Worker reports

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
no automatic retries or format-repair loops.

## Limits, refusal and history

Result files and stdin are read with a 1 MiB bound before decoding. JSON must be UTF-8, contain no duplicate
keys or nonfinite numbers, and stay within depth 64, 10,000 values (including object keys), 1,000 items per
array, and 65,536 UTF-8 bytes per string. Schema loading accepts local references only, rejects recursion,
and bounds reference expansion. Validation stops at its first error. Closed semantic objects reject unknown
fields before conversion or enrichment.

Refusals carry `result-rejection.v1` with category, rule, path, contract and a bounded diagnostic. Categories
are syntax, schema, semantic, stale-attempt and authority. Payload values are not copied into diagnostics.

Historical records remain readable. Missing bindings and old acceptances never gain newly verified
contract evidence. For an active legacy worker claim, use the existing explicit abandon/retry/new-claim
path; do not edit or restamp history. Review recovery needs a newly bound, observed assignment under the
existing plan/Build lifecycle. A prior seal is not silently upgraded.

Audit is narrower: the typed adapter distinguishes absent, rejected and valid full conformance blocks.
Only valid divergences are promoted. Rejection is disclosed; the outer audit still strips the block and
continues successfully, and independently derived degradation notices keep their policy. No audit result
receipt or durable retention of every meets/unsure verdict is promised here; issue #815 owns that work.

## Falsification demo

Run `uv run --directory .engine --frozen -- python tools/demo_result_contracts.py`.
It executes real command handlers against disposable stores with synthetic transport observations.
Add `--break-ingress` to bypass validation in memory: the demo must exit nonzero. It does not qualify a
live provider or modify the project's real plan library.
