---
title: Test-cost contracts
---

## What this covers

Test-cost contracts make the work added by tests visible before a change reaches merge review. This guide
explains how to declare a test's purpose and limits, inspect measured work, and recognize unavailable proof.

## What you need to know

New `build-plan.v3` nodes declare the fault they protect, the cheapest meaningful boundary, fixture owner, dependencies and data reads, cadence, bounded resources, mutable state, cache lifetime and added-cost risk. A pure boundary cannot request processes or whole-tree fixtures. Old sealed payloads retain their version and bytes. Every new approval includes the technical-integrity cost review after the Build, including quick depth; quick still has no cold plan review.

Tests can carry an embedded declaration or an exact source-bound entry in `test-cost-declarations.json`. These prospective declarations are separate from measured legacy debt. Changing a named method invalidates its external declaration; a broader budget cannot silently override an embedded declaration.

The existing serial launcher accepts `--cost-path`. It records exclusive counters with case/fixture/unattributed ownership; summing nested timing spans is not a resource count. JSON decoding is a conservative upper bound that includes non-schema JSON. Standard jsonschema metaschema checks, process launches, Git launches, canonical fixture clones and nested test-runner journeys have separate counters. Unsupported descendant work is unknown, never zero. These observations do not authenticate an external effect or create a sandbox.

A baseline is an explicit enrollment, not a learned running maximum. The source commit, original source tree, observer adapter, inventory, environment, policy and contract identities must remain distinct. A disposable adapter can observe an older source only after outcome parity is checked; it must not replace that source's files. Measurements from an incompatible adapter cannot activate a baseline.

Missing, corrupt or incompatible enrollment opens a bounded measurement bootstrap under the existing correctness and static inventory checks. Bootstrap does not grant cost clearance. Activation requires complete observations, exact legacy source identities, an owner, a reason and a revisit condition. Candidate runs cannot add legacy cases, rename them into allowances or ratchet limits upward. Unknown coverage remains visible after enrollment.

The installed enrollment has two tracked artifacts: `test-cost-legacy-baseline.json` stores a bounded compressed document, and `test-cost-activation.json` records its digest, execution identities and parity evidence. Compression avoids making every whole-tree fixture copy a large repeated case census. Inspect the document without decoding it by hand:

```sh
uv run --directory .engine --frozen -- python tools/selftest_cost.py inspect --baseline policies/test-cost-legacy-baseline.json
```

Add `--case` with an exact runtime ID to inspect its occurrences and ceilings. The command reports the largest costs, original source and adapter identities, ownership and remaining unknowns. A valid activation does not itself approve a candidate.

For an explicit bootstrap, preserve a clean immutable source checkout and run its native full launcher with structured outcomes and performance output. Then observe that same source through the retained-source adapter, writing to a new directory outside the source:

```sh
uv run --directory .engine --frozen -- python tools/selftest_cost.py observe-retained --source-root /absolute/pinned-source --output-directory /absolute/new-observation-directory
```

This command measures only; it never writes an activation or changes enrolled limits. Enrollment requires complete passing native/observed outcome parity, the exact static/runtime census, independently resolved identities and explicit review of the resulting debt. A focused `--pattern` is a diagnostic, not full bootstrap evidence. Requalify an incompatible adapter or environment before activating its measurements; a local enrollment cannot qualify a different CI environment.

Initial enrollment observes source `c434b4771efb94bce36894287b8ffeda2555b651` with adapter `2a9874346451096cb890567562b142de6c89c14f`. Both complete runs pass all 10,093 cases with zero skips; the static census has 10,024 definitions, including inherited/runtime mappings and the two separately retained overwritten definitions. Exact raw outcome digests remain in the activation record. Seven source-bound parity rules cover reviewed UUID4 fixture IDs, disposable Git commit IDs, and one test's set iteration. They preserve token equality relationships, substantive inputs, counts and outcomes. Changes to the tree or defining source invalidate those rules; they are not general permission to ignore differing subtests.

| Observed resource | Initial count |
| --- | ---: |
| Process launches | 67,410 |
| Git launches (included in processes) | 66,317 |
| JSON decodes (conservative schema-work proxy) | 9,469,084 |
| Metaschema validations | 119 |
| Classified whole-tree fixtures | 49 |
| Classified nested journeys | 88 |

The 101-operation recovery test alone records 6,868 Git launches. This is measured legacy debt for the later fixture/relevance work, not an acceptable new-test template. Descendant work, alternate process boundaries, background-thread ownership and unclassified tree-copy paths remain explicit gaps; the table is not a complete operating-system trace.

The native child interval is 2,391.692 seconds and the observed interval is 2,448.129 seconds. Timing qualification is unavailable: only one sequential pair exists, cache state is unknown, and the shared worktree population changed from 29 to 30. The activation retains both environments and binds budgets to the observed one; it makes no 2% overhead, CI-platform, causal-PR or 15-minute program claim.

The initial static census covers all recursively discovered test modules. Existing overwritten definitions have separate source/AST-bound enrollment; they are not runtime cases and are not a completed relevance audit. Generated runtime identities need explicit source mappings and fault-preservation rationale. Line numbers aid diagnosis but do not grant an allowance.

Exceptions are bounded to a source, case occurrence, resource and ceiling. They name the supported fault and its preservation evidence, as well as an owner, reason and revisit condition. Their UTC issuance/expiry interval must be positive and at most 30 days. Every consumer checks current time through `moment.py`; old green evidence cannot extend permission. Expiry removes the allowance, while otherwise compatible raw observations can be re-evaluated against the unwaived rules.

Timing remains advisory. The program's 900-second target and independent 1200-second concern remain separate from deterministic resource ceilings. Comparable samples retain a measured noise envelope; small local calibration samples are not CI speed qualification. Observer overhead must be measured with alternating identical workloads before release.

Representative overhead measurements at clean source `7c2f8957fe8e05e1b5d6e41fd145c88f2e74e9bb`
used the existing serial launcher, one discarded warm-up pair and three scored pairs per workload,
ordered off/on, on/off, off/on. All eight runs per workload preserved complete outcome parity through
the canonical comparator: 16 cases for `test_selftest_results.py`, 235 for `test_project_manager.py`,
with no skips or failures. Each child starts with fresh process caches; one checkout and runtime keep
reported environment and topology identical. The warm-up does not prove operating-system cache state.

| Workload | Off child samples (seconds) | On child samples (seconds) | Median child overhead | Median launcher overhead |
| --- | --- | --- | ---: | ---: |
| Cheap results controls | 1.416, 1.506, 1.666 | 1.357, 1.473, 1.458 | -3.22% | +0.14% |
| Expensive plan lifecycle | 87.683, 82.398, 82.507 | 84.776, 83.460, 83.193 | +1.16% | +1.22% |

Both observed medians are within the 2% overhead target. The negative cheap child delta is measurement
variation, not a speedup claim. The retained sample manifest has SHA-256
`c7cdcdce8e3d9fb8ba00e94b7e9757b8df941272ff6834cd84db3459b0f875ac`; it records exact source/tree,
environment, raw outcome/performance hashes, warm-ups and measured launcher intervals. These bounded
local observations neither qualify Linux nor establish a full-PR performance improvement.

For a prospective v3 Build, the coordinator measures each committed node through `work verify` before
integration, then measures the complete candidate separately. Node evidence binds the claim base, attempt,
approved contract and actual artifact. Candidate evidence cannot stand in for a node observation. A cached
candidate preserves its raw measurements but consumes current exception permission again.

The full `engine-ci` arm retains resource observations alongside outcomes and timing. A blocking assessment
uses the same consumer as the coordinator; the immutable merge receipt includes its evidence digest.
Metadata reuse retains the originally measured source identity and reconsumes the evidence at completion.
The project-only arm keeps its existing Engine-health scope. No new final selector or parallel runner is
introduced. The required wiring check rejects consumer command, shell and environment substitutions.

CI allowances come from the maintainer-owned `ENGINE_TEST_COST_APPROVED_EXCEPTIONS` repository variable,
a JSON array of `test-cost-exception.v1` records. The workflow passes it only to the evidence consumers.
Committed candidate content and previously saved permission cannot grant a current allowance. A local
operator-decided `cost exception` record does not update that repository variable. An expired allowance
is refused without treating another full run of identical code as a remedy.

Prospective approvals include technical-integrity review at every depth. Its versioned report carries the
assessment digest, full identity, status and rationale. The controller records its own disposition
separately; neither judgment can waive deterministic violations or turn missing timing into qualified
performance. Existing frozen array-shaped review obligations remain readable and dispatchable under their
original binding, including Builds sealed before this capability.

Run the bounded behavioral demonstration from either supported runtime:

```sh
uv run --directory .engine --frozen -- python tools/demo_test_cost_contracts.py --scenario shared-helper
```

The unchanged test body calls a helper that launches a real child process. The zero-work baseline rejects
the increase, then a fresh observation after repair passes. Other scenarios cover JSON decoding, metaschema
validation, canonical fixture cloning, a nested journey, ambient Git, duplicate definitions and stale
identity. These controls use explicit synthetic fixture identities and grant no project performance credit.
The registered hard-check-bite fixture exercises all eight through the actual required check; permanent
regressions also protect standalone imports, workflow wiring and historical reviewer dispatch.

This capability addresses the program's `efficient-tests-at-creation` obligation. The child retains
`fifteen-minute-feedback`, `preserve-failure-boundaries` and `justified-test-placement-and-selection`:
the complete legacy relevance audit, cadence decisions, qualified selection and broader optimization still
belong to their subsequent work. `PROFILE-STAGES`, `ARTIFACT-IDENTITY` and `EVIDENCE-OUTCOME` are applied at
the existing node/candidate/full producers. `ENV-EXECUTION-HANDOFF` remains limited to observed environment
facts; counters do not establish sandbox containment, authentic external effects or future product outcomes.

### Cost evidence and live permission

Whole-Build candidate validation produces a distinct cost assessment after all nodes integrate. Full CI
still executes every test; it acquires costs from that same execution, evaluates them before emitting the
existing merge receipt, and retains the original observation beside it. Reuse and terminal completion
re-evaluate permission without rerunning still-eligible observations. Candidate cache acceptance, final
proof import and submission preview/apply also recheck expiry through `moment.py`.

For new cost-applicable approvals, the technical-integrity reviewer returns its bound versioned envelope
with an explicit assessment digest, candidate identity, status and rationale. Record the actual observed
report through native ingress, then use `cost dispose --input <json>` for the separate controller object
`{assessment_digest, decision, rationale}`. Only qualified acceptable evidence permits `accept`; concerns
or unavailable coverage require `accept-with-limitations`. Deterministic violations require repair or a
live explicitly approved bounded exception. Missing, stale or invented evidence cannot be disposed away.
Historical array-bound approvals remain arrays and do not silently acquire a new obligation.

Exceptions name exact case, resource, immutable source, ceiling, owner, supported-fault proof, reason,
revisit and issue/expiry times (at most 30 days). `cost exception --input <json> --operator-decided --reason
<decision>` records actual operator authority locally. CI receives the current list from the maintainer-owned
repository variable `ENGINE_TEST_COST_APPROVED_EXCEPTIONS`, injected only into evidence-consumption steps;
never put waiver authority into candidate Git or a test-controlled job environment. A maintainer explicitly
updates that variable when approving CI permission. Withdrawal/expiry invalidates permission, not raw counts.
No exception was granted merely because an old receipt was green.

A missing or incompatible legacy baseline uses bounded measurement bootstrap and grants no qualified cost
success. New declarations, duplicate controls and correctness still apply. Unavailable actual-base resource
comparisons, descendant coverage and noisy timing remain visible for disposition. Every observed required
path of at least 1,200 seconds needs investigation; 900 seconds remains the program target, not this child's
completion promise. Qualification of observer overhead retains the <=2% median target and alternating
comparable samples. Do not create hard wall-clock assertions or claim the broader relevance/cadence audit
is complete. See the baseline evidence and limitations above.
