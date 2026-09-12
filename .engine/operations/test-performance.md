---
title: Test performance — complete observations and comparable CI evidence
---

## Purpose

Measure complete selected-case outcomes and named required workflow attempts. Neither artifact is an
`engine-ci-receipt`, and neither grants merge authority.

## Steps

### Observe a serial run

The supported full CI command is:

```sh
uv run --directory .engine --frozen -- python tools/selftest.py --start-dir tools --pattern 'test_*.py' --results-path /tmp/selftest-results.json --performance-path /tmp/selftest-performance.json
```

Choose distinct output paths outside the source tree, as CI does. `--results-path` retains `selftest-results.v1`;
when omitted, the launcher checks a private temporary result. `--performance-path` observes advisory timing. The existing
`--run-record-path` output keeps its v1 shape. Its executed count is now observed starts, including individual
skips and excluding cases prevented from starting by module/class fixtures.

Discovery and selected identities are child-owned. Duplicate IDs have occurrence indices. Outcomes include
expected failures, unexpected successes, skip reasons, fixture errors/skips and parent-linked subtests.
An unexecuted selected case is incomplete even when `unittest.wasSuccessful()` says success. A clean early
stop and an empty full run therefore fail. Legitimate fixture and subtest skips preserve unittest success.
Missing, corrupt, oversized or unfinished required observations also prevent a successful launcher result.
Existing child failures remain failures. Optional timing failure is unknown and cannot make a failing run pass.

Outcome and timing files are limited to 64 MiB each, 200,000 cases, 4,096-byte required strings, and 1,000
subtests per parent. Unsupported phase hooks remain unknown. Timing uses monotonic spans: case durations
include their setup, body, teardown, cleanups and nested journeys. Fixture spans can nest; use their interval
union, never add inclusive spans together. Collection, child elapsed, parent elapsed and unallocated time are
separate. Platform, Python, uv, resources and worktree-count facts are included when available; cache state
remains unknown unless independently established by a later qualification procedure.

### Read the complete required CI interval

Choose the actual run and attempt for **each** required context, naming each additional workflow file.

```sh
uv run --directory .engine --frozen -- python tools/selftest_performance.py report-ci --repository OWNER/REPO --head FULL_HEAD_SHA --run CI_RUN_ID --attempt 1 --context engine-guard=GUARD_RUN_ID:1:.github/workflows/engine-guard.yml --output /tmp/ci-report.json
```

The command uses GET-only GitHub calls with `GITHUB_TOKEN`, `GH_TOKEN`, or the existing `gh` login.
`--base BRANCH` supplies a missing base. Required contexts come from both active branch rules and
classic protection. Their snapshot is the policy observed at report time, **not** a claim to reconstruct
historical branch requirements. Permission errors, exhausted pagination and unsupported required-workflow
policy remain incomplete. Check identity includes the requested head, context, app binding, workflow file,
run and attempt. A PR-target workflow's base checkout is distinct from its verified head-associated check.

`ci-test-performance.v1` reports:

- elapsed time from the first named workflow creation to the last selected required-job completion;
- the interval union of active selected jobs, and queue-only time that does not overlap active required work;
- queue-excluded elapsed time, including gaps that cannot truthfully be assigned to a queue;
- per-job and per-step durations, observed workflow finalization gaps, and summed runner-minutes;
- every attempt of each explicitly named run up to the supplied attempt, including failed attempts and
  cumulative runner cost. Separate workflow runs require their own reports; this is not a PR-wide search
  across earlier commits or unmentioned metadata events.

Manual approval time and dependency critical paths remain unknown. Main-push, full PR, metadata reuse and project-only runs are
distinct classes. A main-push report covers its explicitly supplied reference workflows and earns no PR-path
qualification. Timing can be complete while missing test artifacts make the overall report incomplete;
the measured interval remains visible with the reason, even on failed runs.

Full CI publishes an observed launcher-interval summary after success or failure. It is explicitly narrower
than the completed required-CI report. The two artifact names include run ID and attempt and retain data for
30 days without overwriting an earlier attempt. Reuse and project-only runs upload no test observations.
The existing receipt name, overwrite rule, provenance filter and platform-derived verdicts remain separate.
Publication validates and sanitizes fresh upload copies; rejected input becomes safe incomplete outcomes
or absent timing. Uploads require successful staging. Failures remain advisory; all five test-step decoys remain.
Published observations omit tracebacks, full output and environment dumps, redact absolute paths, bound
strings and escape rendered test names. Downloaded archives must contain exactly their named JSON file;
no archive member is extracted into the filesystem.

### Compare without hiding the cost

```sh
uv run --directory .engine --frozen -- python tools/selftest_performance.py compare --baseline /tmp/baseline.json --candidate /tmp/candidate.json --output /tmp/comparison.json
uv run --directory .engine --frozen -- python tools/selftest_performance.py samples /tmp/baseline.json /tmp/candidate.json
```

The comparison keeps raw samples and differences. Missing or mismatched route, runner, OS, architecture,
Python, uv, resources, topology, cache or pattern evidence makes it unqualified. Unknown equals unknown is
not proof of comparable environments. Common, added and removed case identities are separate; an unchanged
test method can become expensive when shared production code changes. Missing per-case baseline data is
unavailable, never zero; changed inventories or outcomes are unqualified. Nested tests remain parent cost.

`TARGET_SECONDS` and `CONCERN_SECONDS` in `selftest_performance.py` own the 900-second target and 1,200-second
concern threshold. A concern calls for investigation; it is not a test timeout. This foundation does not
claim the program has achieved the target. Initial program qualification still owes three alternating
comparable baseline/candidate pairs per representative change class, a candidate median at most 900 seconds,
and investigation of each sample at or above 1,200 seconds. Retain failed and retried samples and report local,
full-reference, nightly and cumulative costs separately. Do not run a full benchmark matrix on every PR.

The samples command prints every value and count; p90 is withheld below 20 samples and remains descriptive above it.
The next program child owns enforced efficient-test authoring and shared-path cost contracts; this child
introduces no selection changes, worker/reviewer rules, production hotspot optimization or cost gate.

### Watch the completeness boundary yourself

These commands run two tiny synthetic cases through the real launcher, with no repository or network fixture:

```sh
uv run --directory .engine --frozen -- python tools/test_selftest_results.py --demonstrate
uv run --directory .engine --frozen -- python tools/test_selftest_results.py --demonstrate --stop-early
```

The first reports two starts, two passes, completeness true and exit 0. `--stop-early` reports one start,
one unexecuted case, completeness false and launcher exit 1, although stock unittest accepts that early stop.
The demonstration exits nonzero on unexpected behavior. Both modes share the permanent outcome regression.

### Foundation qualification evidence

Before the final outcome-guard narrowing, Python 3.12.13 / macOS arm64 pairs over the identical 167-case
`test_selftest*.py` payload measured stock wall times of 31.943, 33.901 and 34.652 seconds and
instrumented times of 33.319, 31.591 and 36.451 seconds. All passed with unchanged source fingerprints.
The ratio of medians is -1.72%; individual pairs range -6.81–5.19%, so a 2% ceiling remains unproved.
Earlier investigation measured warm mandatory validation at 9 ms; all raw samples remain retained.
This is not full-suite or 15-minute qualification. A source-changing preliminary experiment was discarded.

## Done when

An explicit report names its source, run, attempt, current policy and missing facts; any comparison discloses
its qualification limits. Required test outcomes cannot pass with unexecuted selected cases, and timing cannot
substitute for the existing merge proof.

## Notes

The adapter follows GitHub's [workflow-attempt API](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run-attempt),
[attempt job API](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt),
[branch rules API](https://docs.github.com/en/rest/repos/rules#get-rules-for-a-branch) and
[classic status-check protection API](https://docs.github.com/en/rest/branches/branch-protection#get-status-checks-protection).
