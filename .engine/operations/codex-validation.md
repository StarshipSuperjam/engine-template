---
title: Validate the Codex adapter live — qualification and hook re-trust
---

## Purpose

Prove the adapter behavior that requires a running Codex host: hook firing, agent discovery, effective
permissions and context delivery. Repository checks prove coherence; live observations qualify only the
host and version actually tested. Enter after adapter changes or when hooks stop running.

## Steps

1. Record `codex --version`, OS, host kind and host version (or why unavailable). Hooks require a
   supported build (around v0.114 or later). CLI evidence never certifies Desktop or Windows.
2. Approve Engine hooks with the CLI's `/hooks` browser. Do not direct a Desktop user to type that
   command in Desktop or assume their build has a Hooks settings screen. Project-folder trust and
   approval of each hook's current hash are separate. New or changed entries in `.codex/hooks.json`
   require approval. Preserve saved trust during isolated qualification; verify actual events afterward.
3. Start fresh: verify `AGENTS.md` and the opening **Project status** block. If the briefing is absent,
   disclose that automation is off and ground manually with
   `uv run --directory .engine --frozen -- python tools/engine_status.py` before continuing.
4. In a disposable git worktree only, request an edit and a shell `git commit` without Build authority.
   Both must be denied with the exploring explanation. Inspect the actual worktree afterward; a failed
   hook can perform the offered mutation, so never target the real project. Discard the fixture.
5. Check explicit Build entry with `$engine-start`; importing or casually discussing a plan grants no
   Build authority. Payloadless entry and concurrent-session stance acceptance have their own owner.
6. Check the exact deferred-helper procedure from `boot.py`'s `MCP_AVAILABILITY_CHECK_CODEX`. In a fresh
   session, record whether each helper was initially omitted; an initially visible helper does not prove
   deferred discovery. Healthy exact discovery/health produces no warning. In isolated copies, exercise:
   both pass; either one passes while the other's discovery misses; either one passes while the other's
   discovered call fails; both miss; both calls fail. Omit only a fixture registration to cause a miss;
   use a temporary server registering the exact health operation but returning an MCP error to cause a
   call failure. Passing helpers stay silent. Misses advise trust/restart; registered failures do not
   blame trust. Never damage real servers or trust; if isolation is unavailable, record not-verified.
7. After turns, check memory capture with `$engine-status`; a capture warning is a defect. Confirm
   `$engine-help` uses the `$` prefix. Run incident replays via `codex-incident-replay.md` separately.
8. Confirm all ten rendered personas are discoverable. From separate Read Only and Workspace Write
   parents, launch the same configured child. Record requested model/effort/read-only intent separately
   from actual child runtime metadata, shell observation and disposable-file outcomes. A parent override
   may replace the child sandbox. If that changes, reopen the exception and `codex-settings.md`.
9. Test spawn enforcement and compact delivery as below. Keep child identity separate from hook session
   identity; never substitute a live-session marker or use the hook's parent model as child evidence.
10. Preserve the scheduled-Routine retirement gate in `codex-settings.md`: inspect the UI's shared default
    and lack of per-Automation permission profiles; changed platform facts reopen the policy. In Scheduled,
    pause/delete every `$engine-routine` task and verify none remains Active. Invoke the refusal skill;
    it must point to supported interactive Codex or Claude Desktop. This is a pre-merge migration gate,
    including the upgrade notice when crossing routine-mode 0.2.0; code cannot prove scheduler origin.
11. Likewise verify the audit retirement instructions in `.engine/audits/self-review-setup.md`: identify
    recurring audits by prompt, remove them from Active, and retain interactive Read Only Codex plus
    durable GitHub/Claude replacements. Upgrades crossing audit-library 0.3.0 must disclose the migration.

### Vary the real policy owners

These replays can fail, but do not prove a live hook fired. They are construction demonstrations covered
by permanent regression tests, not an additional standing command. From the Engine root:

```sh
printf '%s\n' '{"tool_name":"collaborationspawn_agent","tool_input":{"agent_type":"explorer","model":"gpt-6-astra"}}' | uv run --directory .engine --frozen -- python tools/session_economy.py hook
printf '%s\n' '{"source":"compact"}' | uv run --directory .engine --frozen -- python tools/build_coordinator.py reground-hook
```

The first must emit `deny`. Change the model to the central mechanical choice (`gpt-5.6-luna` here):
expect no denial. Remove the model: denial. A `default` or unknown role is outside the search rule;
unknown means unclassified, not verified compliant. Changing the top-level parent model cannot satisfy
this requirement. Master and model-specific escape switches still govern newly launched sessions.
The second prints a bounded plan/PR/work pointer for one bound Build, or an explicit absence message.
Adding `"cwd":"/a-different-worktree"` must disclose mismatch and assume no pointer. No replay mutates state.

For live witnesses, use an isolated qualification project with only reviewed fixture hooks. Inventory
all enabled hooks first and abort if anything unrelated is present. Never bypass trust on an uninventoried
project. Record exact commands and fixture sources in Build evidence; never change saved installation trust.
Launch explicit cheap, strong and missing-model explorers with `fork_turns=none`. Capture actual spawn
inputs and SubagentStart: cheap creates a child; strong/missing are denied before creation. Record actual
child metadata separately. Unknown launch fields stay unknown; task prose never establishes a role.
For mid-turn compact delivery, run the isolated CLI with `-c model_auto_compact_token_limit=5000`, request
one output-only command printing numbers 1 through 12000, then ask for the hook's plan/work pointer without
reading files. Use an isolated plan-library fixture with the real lookup and reminder handler. Capture
SessionStart `source=compact`, bounded additionalContext and the following model response. Repeat without
a binding. Keep CLI and Desktop results separate; a replay or self-report alone is not the live event.

For multipart delivery, follow [live provider qualification](../docs/result-contracts.md#qualify-multipart-review-delivery).

## Done when

Retain a `codex-qualification.v1` JSON record in existing Build verification evidence and run:

```sh
uv run --directory .engine --frozen -- python tools/codex_qualification.py <record.json>
```

The read-only checker requires a timestamp with timezone, full base/head commits, official sources,
reproduction argv arrays, and host kind/OS/CLI/host-version values or explicit unavailable reasons.
It requires exactly 18 unique cells: both parent modes × custom-agent-model, reasoning-effort,
parent-child-sandbox, shell-availability, file-writes, hook-session-identity, compact-context,
agent-observation and spawn-payload. Each has requested settings and pass/fail/not-verified status.
Pass/fail needs observed results and evidence references; not-verified needs a reason. Omitted fields,
duplicates, invalid statuses and missing evidence accounting fail completeness. A complete record can
still contain failures: the checker never executes probes, certifies a host, validates evidence truth,
promotes unknowns or changes `.engine/state/execution.json`. Reviewers judge evidence separately.
Required capability failures/unknowns hold dependent implementation. After material repairs, renew live
witnesses against the final candidate and check its final record before submission. Every applicable
live arm must pass before release; defects inside the agreed bar require a fix, not silent deferral.
Routine/audit migration and Desktop acceptance remain separate gates where those changes apply.

## Notes

September 9 qualification used macOS Desktop 26.901.51231 (8109) and Codex 0.153.4. Native
completion reclaimed capacity; queue-only child mail reproduced allocation pressure, and useful
continuation restored it. Candidate hooks denied that mail both during work and after a partial stop.
Six fresh workers ran across two batches of three; one relative-read attribution failed and stayed
unverified, then a fresh case passed after repair. Full stdout with exit 7 earned no read credit;
a later successful read recovered the same assignment without erasing the failed attempt.

Planning reviewers initially lacked a usable reader. Codex rust-v0.153.4 `agent/role.rs` ignores
role-local MCP configuration, despite current documentation. The operator approved project-level
registration of one mechanically read-only reader, preserving no-shell instructions. The same stuck
child then read its original packet and supplement, completed without shell use, and earned PM
acceptance. Reuse for a different packet was refused; a fresh replacement completed after two useful
clarifications. Actual Build fixture acceptance also passed. Partial results earned no coverage.

The reader was absent from the test root's initial explicit tool list; discovery returned one
351-character definition including a 153-character declaration. Exact billed-token overhead remains
unknown. Fourteen reader tests and 232 reader/generator/wiring/provider tests passed. Failed witnesses
remain retained; fixture receipts cannot satisfy the actual Build's independent deliverable review.
See [Scoped native agents](scoped-agent-orchestration.md) for the operator-run demonstration. The CLI
fixture is optional validation, not setup every Engine adopter must repeat. Claude documented-contract
and real hook-runner tests passed; live Claude remains the operator-approved September 12 follow-up.
