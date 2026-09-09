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

### Scoped-agent baseline, September 9, 2026

macOS Desktop 26.901.51231 (build 8109), with Codex 0.153.4, passed bounded native lifecycle probes.
Ten distinct fresh children ran across twelve launches; two capacity refusals were expected. Three
actual running children saturated the available slots; completion permitted a fresh launch without a
close tool or limit change. Queue-only messages to completed children reproduced failed allocation;
same-assignment followups consumed the messages and restored fresh allocation. A message in the
original incident was sent before completion and remained pending afterward: a completed-only ban
would miss that race. Retained conversations are not proof of resident capacity consumption.

A separate disposable Desktop fixture observed `collaborationspawn_agent`,
`collaborationsend_message` and `collaborationfollowup_task`. `PreToolUse` denied the queue-only
send before delivery. Two actual child IDs had distinct immutable packets and successful `Bash`
packet-read `PostToolUse` responses. Child hook `session_id` remained the parent's ID; `agent_id`
identified the child. `SubagentStart` and `SubagentStop` were observed, but a start by itself does
not correlate a launch to a packet, and a stop is a turn boundary, not proof an assignment succeeded.
Both children first needed clarification, then completed their original assignment through followup.
Parent and child transcripts corroborated packet reads, denial and delivery. A separate live probe
delivered followup during a running tool; an empty followup woke a child without useful progress,
and an unknown recipient was rejected. Do not infer exactly-once transport from these bounded cases.

The fixture's first controls produced no hook events: project trust alone was insufficient. After
the operator approved all five hook hashes through CLI `/hooks`, the already-running Desktop task
still produced no events. Archiving and restoring that same task loaded the fixture configuration;
the subsequent Desktop control and native probes passed. Preserve both failed controls and the
passing run. This is an observed recovery, not a universal hot-reload guarantee. CLI trust approval
does not certify Desktop execution. The disposable fixture and its runner are test equipment, not
requirements for repositories that install the Engine.

These are baseline observations, not validation of a later implementation. Renew candidate witnesses
after changing hooks; cover active and near-completion denial, parent-directed reporting, clarification,
missing hooks and fresh review acceptance. Offline fixtures derived from Anthropic's
[hook reference](https://code.claude.com/docs/en/hooks) and
[subagent reference](https://code.claude.com/docs/en/sub-agents) are a separate evidence class:
Claude uses `Agent` and `SendMessage`; fresh custom agents, forks and resumed contexts differ.
For issue 1269 the operator deferred live Claude 2.1.185 confirmation to September 12 afternoon.
Documented-contract tests must run before merge; the live result remains unverified until recorded.

**2026-09-08, macOS / CLI 0.153.4:** the same child retained configured `gpt-5.6-luna`/low effort under
both parent modes, while inheriting the parent's effective sandbox. Read Only permitted shell invocation
but prevented the fixture write; Workspace Write permitted it despite the child's read-only default.
No per-tool shell prohibition or mechanical child isolation from Workspace Write was established.
The live spawn spelling was `collaborationspawn_agent`; `^Agent$` missed it and the exact matcher saw it.
Inputs included agent_type/task_name/fork_turns/message; the custom child's configured model was absent,
while the common hook model identified the parent. Start/stop and child hooks retained the parent's exact
session_id and separate agent_id. Both parent modes delivered a compact-only witness to the next response.
H1 then exercised the candidate gate: cheap explorer completed; strong/missing model was denied before
child creation. The real reminder resolved the isolated library and delivered `pln_L49_COMPACT_POINTER`
and `H1_DEMO` during actual compaction; the next response repeated both. An absent fixture was disclosed.
The record retains commands, fixture sources and bounded observations. Invocation-scoped fixture trust
was used only after an exact inventory check; saved trust was unchanged. Desktop and Windows remain
unverified. The operator's protected-branch merge remains the wall; hooks are fallible guardrails.

Final candidate probes renewed cheap/strong explorer and compact witnesses after the gate repair.
Five documented owner replays passed, including the real bound Build pointer and mismatched-worktree case.
The nested-cwd reminder repair was followed by another real compact-pointer witness. Executable shim
tests now round-trip stdin and target exit status. Provider tests retain a redacted Claude transcript
launch projection with provenance, explicitly distinct from a newly observed hook envelope.

### Candidate shell-result qualification — September 9, 2026

The N5 diagnostic in the same authorized Desktop task
`01a086fe-0081-7010-9865-f0508d76430d` found that native Bash PostToolUse carries
`tool_response` as plain output text, with no exit status or error flag in the hook envelope.
Two commands printing the same public marker, one exiting 0 and one exiting 7, produced identical
hook response strings. The candidate's structured-response-only packet-read check consequently
rejected the successful native read too. This was a candidate defect, not proof that hooks failed.

The witness then inspected the bounded native transcript tail **during the hook**. It found the
exact `event_msg/item_completed/CommandExecution` records for both calls:

- `exec-7be67ebd-1d1f-4c2f-91c6-51a8f938122c`: exit 0, status `completed`;
- `exec-7035d3bc-a81f-43ea-b90b-f6cef8c23c70`: exit 7, status `failed`.

Both matched the actual task and turn (`01a087bc-9161-7ba0-9e85-d170cc690e7e`),
and each native stdout and aggregated output exactly matched its hook response. This supports a
Codex adapter correction that joins the observed response to one exact native completion by actor,
turn and call ID, requiring successful status and exit 0. Missing, ambiguous, contradictory or failed
completion must remain unverified. Child execution additionally retains the existing parent/child
metadata join. Plain text alone must never be upgraded to successful evidence. The correction now requires that
exact native join. The original failed diagnostic remains evidence of the defect, not acceptance.

The diagnostic modified only the disposable witness, preserving its baseline copy. Its hook definitions
and saved trust were not changed. These probe additions are not adopter setup requirements. Claude's
structured Read contract is separate; live Claude validation remains the agreed September 12 follow-up.

### Candidate verification and remaining limits — September 9, 2026

The repaired candidate completed a native blocked-worker assignment on child
`01a087cd-da8a-72f1-9c58-1e79c05640e5`: the actual candidate hook denied a queue-only send,
then one necessary supplement and one followup produced the correct result and original witness on
the same child. The shared acceptance helper verified the observed execution under the plan lock.
This was disposable worker evidence, not production review coverage.

Six further fresh workers launched in two batches of three, without retries, messages, reused children
or limit changes. All returned correct variable results. Five passed evidence verification; the sixth
used a relative `cat` path that the first adapter missed. Its failed companion remains unchanged.
The adapter now resolves a simple relative `cat` against the exact observed command completion's cwd;
computed paths and arbitrary shell directory changes remain unsupported. The exposed limit was four
concurrent slots including the controller. Six distinct children demonstrate successive reuse beyond
that exposed limit, not knowledge of an unexposed separate resident allocator limit.

All 860 focused provider, scoped-agent, Project Manager, Build and storage tests passed in a fresh,
hash-matched disposable copy. These include Claude documented-payload cases through the real Engine
hook runner, interrupted acceptance, stale generation, missing evidence and partial completion.
Offline fixtures do not establish live Claude behavior. A separate old/new-reader experiment used
base `a31ecfae453e2c77dd4cb31979293a7cfed531b6`: its actual plan and Build readers round-tripped
sanitized fixture records while preserving companion and packet bytes. Re-upgrade retained verified
receipt provenance; removing the companion made verification fail, and restoring its original bytes
restored verification. The older code has no scoped-agent enforcement. Storage compatibility must not
be presented as enforcement surviving rollback. The experiment's first script invocation used an
incorrect storage constructor and failed; the corrected invocation is the passing result.

The fresh relative-path rerun passed on child `01a087e7-d9b9-78a0-9993-ce3bcd312c5e`,
assignment `sa_53321ccbc69d4141b65e7505ca8d6f25`: native command
`exec-5ab2fc13-010c-4e56-80f5-a4ff582043e0` read the relative packet, exited 0, and the same
child returned 79 with its new witness. The acceptance helper verified it. The preliminary full-CLI
registration attempt correctly refused the earlier minimal lower-level fixture; using the existing
valid disposable plan resolved this harness error without changing source or old evidence.
A further permanent planning crash regression passed with its four related tests after correcting
the test's expectation that filesystem interruption propagates. Receipt publication was interrupted,
legacy history remained unchanged, and retry accepted the original evidence once.

The active clarification case also passed on child `01a087eb-f722-7441-874b-6858e4da6271`.
The candidate denied one queue-only call during its native 45-second shell wait. One necessary
followup was dispatched during that wait, delivered when the tool completed, and read before the
same child's final result of 89 with its original witness. Verification passed with no evidence
faults. Delivery occurred at the tool boundary, not during the sleeping shell command. The denied
message was absent from the child mailbox; no queued fixture traffic needed cleanup.

Native reviewer-gate qualification, candidate/CI validation and the independent deliverable review
remain separate obligations until their results are recorded. The
operator and assisting session own the agreed live Claude follow-up on September 12 afternoon:
fresh and concurrent reviews, same-assignment clarification, cross-assignment resume rejection,
worker continuation and correct completion attribution on the merged candidate. Record the actual
version and results; address any incompatibility in a focused follow-up PR. This deferral is approved
and is not a merge prerequisite. No local probe establishes adversarial isolation or exactly-once delivery.


The first actual planning-review probe did not pass. Fresh architecture child
`01a087ee-a882-7912-a181-95b448dbb32d` could not read its frozen packet: the generated Codex
role prohibited shell commands, and that task exposed no other local-file reader. It returned
`needs_clarification` without reading the packet. The real Project Manager gate refused credit and
left the plan record and companion unchanged. This is an adapter usability gap; a blocked response
is not an independent review. The Build-review fixture can be tested separately with its existing
shell-capable QA role.

A proposed Codex-only planning-role fallback would permit simple cat/rg file reads when no native
local-file reader exists, while retaining the read-only sandbox request and prohibiting execution
of project code, scripts, tests, shell substitutions, pipelines, redirection and writes. Claude's
actual Bash denylist and other roles would stay as they are. Automatic approval review rejected
applying this proposal as a persistent weakening of the planning roles' shell prohibition without
explicit operator authorization. That fallback was not applied. The operator subsequently chose a
mechanically read-only MCP reader while retaining the planning roles' shell prohibition.


The unaffected Build gate passed with fresh QA child `01a087f6-ad64-76a1-b0f8-7e3dfab03793`,
which read the full immutable fixture packet and returned its actual findings array. The real Build
record callback and fixture transaction accepted it. A subsequent attempted reuse of that accepted
child for the planning packet was denied before delivery; repeating planning acceptance still
refused without changing the plan record. This proves the fixture transaction, not a production
Build or CI result. All eight verified worker assignments were finished through the real CLI;
failed historical evidence was preserved and no accepted queue traffic remained pending.

A separate offline access-recovery check exposed another defect: a failed packet read permanently
faulted the assignment, even after a later good read. The repair retains failed-read attempts in an
optional `read_failures` field, separate from fatal identity or immutable-digest faults. An exact
observed child identity can receive access clarification before a successful read, including a
Codex blocked stop whose native metadata matches the registered root, task name and role. This is
identity evidence only: acceptance still requires the full successful original packet read and
complete continuation evidence. No prior fault or missing observation is backfilled. Fifty focused
scoped/plan/Build acceptance tests passed; the expanded 37-test scoped suite then passed, including
a failed Claude Read, clarification, successful original read and final through the real hook runner.
The native access-recovery witness passed on fresh worker `01a08800-f047-7e92-9fc5-da643a30e473`,
assignment `sa_5dc80f1a63974c2ea5652ae6a2f98cdf`. Its controlled `cat` followed by exit 7
printed the full packet but earned no read credit. One necessary supplement and same-child followup
produced successful supplement and original-packet reads, followed by result 126 with the original
witness. Verification passed with the failed attempt retained; the real finish command completed
the assignment. Prior assignments and reports stayed unchanged and no pending traffic remained.
This repair does not grant shell permission to the planning role.

The dedicated reader passed 14 protocol and boundary tests, including an actual stdio handshake,
bounded UTF-8 reads, registered external packets, escaping links, special files, changed digests and
line-range reads. A whole packet remains mandatory for evidence credit; partial responses do not count.
The initial reviewer-local registration failed on actual Desktop child
`01a08870-64cf-73a0-8134-27c8a6102f53`: it received the new instructions but no reader tool, and the
companion retained `read: null`. Codex tag `rust-v0.153.4`,
`codex-rs/core/src/agent/role.rs`, applies a bounded `AgentRoleOverrides` without `mcp_servers`; this
explains the observation despite the broader current documentation example. No read was backfilled.

The operator then explicitly approved project-level registration and measuring its context overhead.
The core manifest now registers one `read_file` tool through the existing Codex MCP wire; Claude's
configuration is unchanged, and the ineffective per-role server blocks were removed. Other Codex
sessions can discover the same reader: do not describe this as reviewer-only availability or zero
context overhead. The adjusted reader/generator/wiring/provider suite passed 232 tests and module
coherence passed. After project registration, the same previously blocked child read its original
packet and clarification through native `mcp__engine_review_reader__read_file` calls. The recorded
continuation was dispatched and delivered; the child completed an architecture review without shell
commands, and the actual Project Manager accepted its observed receipt. This proves recovery of the
original assignment, not another independent review. The earlier absence of a read remains recorded.

In this Desktop test, the reader definition was absent from the root's initial explicit tool list
and discoverable through `ALL_TOOLS`. Discovery returned one definition totaling 351 characters,
including a 153-character TypeScript declaration. This measures catalog size and observed deferred
exposure, not billed tokens or every runtime's initial context. Exact billing overhead was unavailable.
