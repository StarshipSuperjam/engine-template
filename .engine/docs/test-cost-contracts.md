# Test-cost contracts

New `build-plan.v3` nodes declare the fault they protect, the cheapest meaningful boundary, fixture owner, dependencies and data reads, cadence, bounded resources, mutable state, cache lifetime and added-cost risk. A pure boundary cannot request processes or whole-tree fixtures. Old sealed payloads retain their version and bytes.

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
