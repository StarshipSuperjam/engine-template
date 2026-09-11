---
title: Scoped native agents — fresh assignments and useful clarification
---
## Purpose

Use native runtime scheduling. A new assignment starts with a fresh context; necessary clarification can
continue that same assignment. Retained conversation history is not evidence of occupied execution capacity.
Local hooks are fallible same-user guardrails. Protected main and the operator's merge remain the boundary.

## Steps

### Prepare and dispatch

For independent review, cut the approved packet through `project_manager.py review packet <plan> --session
<root-session>` or `build_coordinator.py review packet --stage deliverable|repair --plan <payload> --session
<root-session>`. Keep the emitted assignment ID, registered role, immutable packet path and digest. Dispatch
only lenses whose coverage is still missing. Never infer the root session from a recent child or a task title.
For a worker/scout, register its complete bounded packet with `scoped_agents.py register --plan <plan-id>
--session <root-session> --purpose worker|scout --role <native-role> --packet <file>`. Registration grants no
Build authority and no review coverage.

On Codex V2 use native `spawn_agent`, the assignment ID as `task_name`, the registered `agent_type`, and
`fork_turns="none"`. On Claude use a fresh `Agent` with the registered custom `subagent_type`, without resume
or fork, and include its unique immutable packet path in the prompt. Ask the child to read that entire file
through a successful native read tool before working. Registration refuses packets and clarification
supplements over 1 MiB before dispatch; narrow their contents first. A digest repeated in prose, a start event, or a caller's
provider label cannot establish that read. Do not pass sibling findings or the controller's verdict.

Codex registers the compact `engine-review-reader` at project level. The installed 0.153.4 role loader
ignores role-local MCP registration, so availability cannot be restricted through reviewer TOMLs.
Other sessions can discover the same reader; do not claim zero context overhead. Its single `read_file` tool reads complete
UTF-8 files up to 1 MiB from the checkout, or exact registered frozen packets and supplements from the
canonical plan library. Optional zero-based line `offset` and `limit` bound source inspection; omit them
for the required complete packet read. It exposes no writes or commands. A refused, incomplete or truncated response
does not establish a packet read. The existing no-shell instruction remains in force. Claude retains
its native reader. This server's path checks constrain its own calls, not every tool in the host session.

### Clarify within the assignment

A child may ask its owning controller for missing information or access. A blocked/partial turn is not a
completed assignment, even when the native UI says Completed. Rephrase instructions, explain a term or repair
access to the same packet instead of launching replacement after replacement against the same ambiguity.
Keep the original target and obligations fixed. A materially different target needs a new packet and child.

Prepare useful clarification as a private text file, then `scoped_agents.py clarify --plan <plan-id>
--session <root-session> --assignment <id> --input <private-file>`. Send the returned supplement path and ask
the same child to read it. Use Codex V2 `followup_task`, or Claude `SendMessage`; never Codex queue-only
`send_message` to an owned child, whether active, finishing or stopped. Child-to-controller reporting remains
allowed. Do not send thanks, empty nudges, peer conclusions or unrelated work. Supplement bodies stay private.

Check `scoped_agents.py status --plan <plan-id> --session <root-session>` before retrying uncertain delivery.
On Codex, `reconcile --assignment <id> --transcript <actual-child-transcript>` with the same plan/session
matches native sender, recipient and payload observations; it records delivery only, never inventing completion.
On Claude, require the observed successful supplement read; an undocumented transcript envelope is not proof.
A transport error does not prove nondelivery: reconcile actual child observations rather than blindly retrying.
An uncertain dispatch or missing completion stays unverified. A blocked reviewer reports a small JSON status
object such as `{"status":"needs_clarification","question":"..."}`; its eventual valid findings array is the
one completed result. An empty findings array means no findings only after observed valid execution.

### Finish, recover and accept

Use bounded native waits, checking the current assignment rather than polling every retained conversation.
When capacity is exhausted, inspect owned assignments and actual pending deliveries once; finish or clarify
legitimate outstanding work, then retry the fresh launch once after observed progress. If that fails, report
the missing coverage and specific uncertainty. Do not raise limits, manufacture a close tool, or reuse an old
conversation as a new reviewer. V2 completion permits native eviction when eligible; V1 explicit cleanup,
where the runtime supports it, remains separate from preserving conversation history. Historical pending-mail
cleanup earns no review credit and does not authorize new unrelated messages.

Record a review through its existing Project Manager or Build Coordinator `review record` command with
`--session <root-session>` and the current packet identities. The shared validator requires fresh launch,
actual child/role, frozen packet read, complete clarification evidence and a valid final output. Codex also
reconciles the initial launch and every incoming control message against observed native sends, in order. Repeated
observations count once. Missing or contradictory evidence refuses acceptance. For workers/scouts, `finish`
with plan/session/assignment closes the owned assignment after inspecting its result; Build integration still
has its own evidence requirements. `abandon --reason <reason>` forfeits an unaccepted assignment's review
credit, but does not cancel native execution or drain pending messages.

If hooks are absent, untrusted or fail, disclose execution as unverified. Do not silently replace these checks
with attestations. Existing unsealed plans need verified review companions before sealing; restore their
original evidence or clone for fresh review. Already sealed historical records remain readable. Project trust is separate from individual hook approval. `/hooks` is the Codex CLI browser;
do not promise a Desktop slash command or settings screen. The September 9 Desktop fixture needed individual
approval and reopening the same task to load its configuration; verify an actual event afterward. That isolated
qualification harness is not an Engine installation requirement. Claude 2.1.185 contract fixtures are offline
checks; the operator's September 12 live smoke test remains explicitly outstanding.

Reviewer assignments retain approved source and result bindings; dispatch refuses unavailable mandates and acceptance validates observed output against the frozen binding. Preserve companion evidence. Historical adoption is separate from fresh execution, and reviewer effort stays harness-controlled. See [Executable persona result contracts](../docs/result-contracts.md) for renewal, historical recovery, ingress and demonstrations.
## Done when

The observed result satisfies its original assignment, any clarification is accounted for, and the
existing review or worker acceptance command succeeds. Missing evidence remains unverified.

## Notes

### Operator-run demonstration

This optional qualification uses an explicitly authorized isolated test task, not production records.
It is not an adopter installation requirement. Verify actual hooks after any needed approval; never
change saved trust automatically. Record runtime version, root ID and source hashes before starting.

1. Register private worker packets with independently chosen values and random witnesses. Launch more
   successive fresh children than the exposed concurrency limit, respecting that limit at each launch.
   Require distinct child IDs, full packet reads, correct finals and successful worker `finish` calls.
2. Leave one calculation's adjustment unspecified. After its partial stop, attempt one queue-only send
   with a unique sentinel: require native refusal before enqueue. Supply the adjustment through a
   registered clarification and native wake-and-deliver call; require the same child's correct result.
3. Repeat while the child performs a short bounded wait, varying delay and ordering. Record when the
   continuation actually arrives; a tool-boundary delivery is not delivery during the running tool.
4. Use a frozen review packet with one genuinely ambiguous term. Clarify only that term; require its
   complete supplement read and valid final findings. Exercise actual PM and Build fixture acceptance.
   Reuse the accepted child for a new packet: require refusal, then acceptance of a fresh replacement.
5. Vary failed reads, partial output, missing hooks and wrong identities. None may earn coverage. For
   shell reads, compare identical full stdout with exit 0 and exit 7: only successful native completion
   may count. A later correct read can recover the same assignment while retaining the failed attempt.

Record pass/fail/unverified with packet hashes, actual child/call IDs and event locations. On capacity
refusal, inspect owned work once and retry only after observed progress; never raise limits. Preserve
failures and consume accidentally queued fixture mail through legitimate continuation before cleanup.
The operator can vary witnesses, inputs and ordering to challenge the result. Fixture receipts never
satisfy production review. Run Claude documented contracts through the real Engine hook runner before
merge; the separately approved live Claude follow-up remains September 12 afternoon.
