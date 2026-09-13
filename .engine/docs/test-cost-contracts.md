# Test-cost contracts

New `build-plan.v3` nodes declare the fault they protect, the cheapest meaningful boundary, fixture owner, dependencies and data reads, cadence, bounded resources, mutable state, cache lifetime and added-cost risk. A pure boundary cannot request processes or whole-tree fixtures. Old sealed payloads retain their version and bytes.

The existing serial launcher accepts `--cost-path`. It records exclusive counters with case/fixture/unattributed ownership; summing nested timing spans is not a resource count. JSON decoding is a conservative upper bound that includes non-schema JSON. Standard jsonschema metaschema checks, process launches, Git launches, canonical fixture clones and nested test-runner journeys have separate counters. Unsupported descendant work is unknown, never zero. These observations do not authenticate an external effect or create a sandbox.

A baseline is an explicit enrollment, not a learned running maximum. The source commit, original source tree, observer adapter, inventory, environment, policy and contract identities must remain distinct. A disposable adapter can observe an older source only after outcome parity is checked; it must not replace that source's files. Measurements from an incompatible adapter cannot activate a baseline.

Missing, corrupt or incompatible enrollment opens a bounded measurement bootstrap under the existing correctness and static inventory checks. Bootstrap does not grant cost clearance. Activation requires complete observations, exact legacy source identities, an owner, a reason and a revisit condition. Candidate runs cannot add legacy cases, rename them into allowances or ratchet limits upward. Unknown coverage remains visible after enrollment.

The initial static census covers all recursively discovered test modules. Existing overwritten definitions have separate source/AST-bound enrollment; they are not runtime cases and are not a completed relevance audit. Generated runtime identities need explicit source mappings and fault-preservation rationale. Line numbers aid diagnosis but do not grant an allowance.

Exceptions are bounded to a source, case occurrence, resource and ceiling. Their UTC issuance/expiry interval must be positive and at most 30 days. Every consumer checks current time through `moment.py`; old green evidence cannot extend permission. Expiry removes the allowance, while otherwise compatible raw observations can be re-evaluated against the unwaived rules.

Timing remains advisory. The program's 900-second target and independent 1200-second concern remain separate from deterministic resource ceilings. Comparable samples retain a measured noise envelope; small local calibration samples are not CI speed qualification. Observer overhead must be measured with alternating identical workloads before release.
