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

Known ambient Git-configuration reads are reported separately from launch counts; a pure declaration also refuses an observed socket connection. Explicit Git isolation removes that ambient-read finding. Exact unchanged legacy effects remain visible as enrolled debt; missing enrollment reports unavailable coverage. New declared tests and newly observed effects cannot borrow a legacy allowance. Other effects and unobserved descendants remain unavailable coverage, and these facts do not provide general network or filesystem containment.

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

The initial resource enrollment observes independently pinned pre-feature main `d5b0c6d55490354b7f50ed91d88306588e7624b8` with adapter `9f793bb684eb0e26a7a41613b586f905f684211d`. Complete native and observed runs pass all 10,270 cases with zero skips; the current source census has 10,201 definitions. Seven source-bound parity rules retain their unchanged defining ASTs and are explicitly bound to this source tree. They preserve substantive inputs, token relationships, counts and outcomes; they do not authorize general subtest redaction.

| Observed resource | Initial count on current main |
| --- | ---: |
| Process launches | 69,135 |
| Git launches (included in processes) | 68,010 |
| JSON decodes (conservative schema-work proxy) | 735,796 |
| Metaschema validations | 119 |
| Classified whole-tree fixtures | 49 |
| Classified nested journeys | 88 |

The enrollment retains 135 ambient Git-configuration facts and 11 network-connection facts with their original owners. These expose existing debt and coverage limits; they do not permit new declared tests to acquire those effects. Candidate work was not used to establish legacy identities or learn ceilings. The original static duplicate enrollment remains bound to source `c434b4771efb94bce36894287b8ffeda2555b651`; both overwritten definitions remain unchanged and reserved for the later relevance audit.

The native child interval is 2,455.878 seconds and the observed interval is 2,442.808 seconds. They ran concurrently on separate immutable checkouts, so their elapsed times are not an overhead comparison. Both recorded environments remain in the activation. This evidence makes no CI-platform, causal-PR or 15-minute program claim.

The initial static census covers all recursively discovered test modules. Existing overwritten definitions have separate source/AST-bound enrollment; they are not runtime cases and are not a completed relevance audit. Generated runtime identities need explicit source mappings and fault-preservation rationale. Line numbers aid diagnosis but do not grant an allowance.

Exceptions are bounded to a source, case occurrence, resource and ceiling. They name the supported fault and its preservation evidence, as well as an owner, reason and revisit condition. Their UTC issuance/expiry interval must be positive and at most 30 days. Every consumer checks current time through `moment.py`; old green evidence cannot extend permission. Expiry removes the allowance, while otherwise compatible raw observations can be re-evaluated against the unwaived rules.

Timing remains advisory. The program's 900-second target and independent 1200-second concern remain separate from deterministic resource ceilings. Comparable samples retain a measured noise envelope; small local calibration samples are not CI speed qualification. Observer overhead must be measured with alternating identical workloads before release.

Current observer qualification at clean source `9f793bb684eb0e26a7a41613b586f905f684211d` uses the operator-approved 3% median wall-time cap. One warm-up pair and three alternating scored pairs preserve all outcomes for the 16-case results workload and 235-case plan lifecycle workload. The expensive workload measures +2.5545% median launcher overhead, within the approved 3% target, after exceeding the original 2% target. Filtering unrelated interpreter audit events reduced the preceding observer's overhead but did not meet the original cap.

The initial cheap samples measure +5.2920% launcher overhead with substantial child-process variation. A fixed 15-pair confirmation preserves all 32 complete outcomes; its reported worktree population changes during collection, so no aggregate matched-environment qualification is claimed. The two matching groups retain all seven and eight pairs and measure +0.2564% and +2.0116% respectively. Earlier 7c, 4b and a32 measurements remain historical evidence, not qualification of this implementation. The operator explicitly approved a 3% cap after these measurements and their tradeoff were explained. Both matching cheap groups and the expensive group meet that revised cap. The original 2% seal and failed measurements remain retained. Resource ceilings, outcome parity and the program target are unchanged.

For a prospective v3 Build, the coordinator measures each committed node through `work verify` before
integration, then measures the complete candidate separately. Node evidence binds the claim base, attempt,
approved contract and actual artifact. Candidate evidence cannot stand in for a node observation. A cached
candidate preserves its raw measurements but consumes current exception permission again.

Actual-base comparison uses an original retained node observation or a verified successful full push receipt for that exact base. CI publishes full push receipts through the existing receipt artifact. Missing, expired, incompatible or inaccessible receipts leave comparison unavailable; they do not trigger a comparison-only full run. Receipt eligibility conservatively rechecks current permission. Base discovery uses disposable runner control files and strips allowance and CI-token variables before importing tests.

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

Every newly approved v3 plan includes test-cost contracts and the technical-integrity cost review, at every
review depth. Project Manager shows that roster before approval; historical sealed v1/v2 plans retain their
approved roster unless explicitly renewed. For these new approvals, the reviewer returns its bound versioned envelope
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
completion promise. Qualification of observer overhead uses the explicitly approved <=3% median target and alternating
comparable samples. Do not create hard wall-clock assertions or claim the broader relevance/cadence audit
is complete. See the baseline evidence and limitations above.
