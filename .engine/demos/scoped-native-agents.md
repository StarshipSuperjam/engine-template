# Demonstrate fresh native assignments and useful clarification

This is an optional validation procedure for an assistant operating an Engine project. It is not a
per-repository installation requirement or an Engine-owned model runner. Use only an explicitly authorized
isolated test task. Never change saved trust automatically; verify actual events after any required approval.

Use the installed runtime's native agent tools and the existing commands in
[Scoped native agents](../operations/scoped-agent-orchestration.md). Give every new assignment a fresh
context and unique packet. Do not run this against production review receipts or held work.

On Codex, confirm the project's installed `engine-review-reader` exposes its single `read_file` tool.
Use it to read complete packets and supplements without shell commands; partial source reads are
useful for inspection but cannot establish a complete packet read. The project-level registration
also makes the reader discoverable to other sessions. Record actual catalog exposure when measuring
context overhead; definition size alone cannot establish billed tokens.

## Prepare variable inputs

Create a private temporary directory. Generate a new random witness for every packet and choose different
base values on each run. Record the runtime version, root session ID, source commit and hashes of any
uncommitted candidate files. Each packet must contain its full task, witness and completion contract.

Register workers with `scoped_agents.py register --plan <test-plan> --session <actual-root> --purpose worker
--role <installed-worker-role> --packet <absolute-packet-file>`. Use that returned assignment ID and immutable
packet path for the native dispatch. For reviewers, use the existing Project Manager or Build Coordinator
packet command; do not register caller-invented coverage in a real Build. The test plan must belong to the
isolated fixture, and fixture evidence must never be imported into a production plan.

## Run the native cases

1. **Normal completion and capacity:** run more successive fresh assignments than the runtime's exposed
   capacity limit (record whether it is concurrency or residency; do not guess an unexposed limit), with independently generated witnesses. Record distinct actual child IDs, native starts, successful
   full-packet reads and finals. Finish each valid worker through its existing control before moving on.
   Retained conversation history is expected; do not infer resident occupancy from history listings.
2. **Terminal partial response:** give a calculation packet with an unspecified adjustment. Require a
   `needs_clarification` response. After the actual read and partial stop are observed, attempt one queue-only
   child message containing a unique denial sentinel. Require a genuine candidate hook refusal before enqueue.
   If it is delivered, record failure and consume it through one legitimate continuation; never leave it queued.
   Stage a private clarification supplement specifying the adjustment, then use the native wake-and-deliver
   continuation once. Require the same actual child to read the supplement and return the correct witness and
   result. A partial response must not earn independent review coverage.
3. **Active clarification and completion race:** use a packet that reads its obligations and then performs a
   short bounded wait before responding. Confirm the packet read before sending necessary clarification.
   Vary the wait and ordering between runs. Require observed delivery and final useful work under the same
   assignment. Try the one queue-only denial during active work too; native active status is no exemption.
4. **Independent review:** use a frozen review packet with one genuinely ambiguous term, clarified without
   changing its target, obligations or verdict. A valid final findings array after that clarification may
   satisfy the original assignment once. Attempt to reuse the old child for a different packet; require
   refusal or unverified acceptance, then show a fresh child on the new packet succeeds. Do not pass peer
   findings or coach a verdict. Exercise both Project Manager and Build acceptance using fixture records.
5. **Negative evidence:** distinguish cancelled/errored launches, missing starts, failed reads, partial finals,
   wrong child/turn/call IDs and missing or disabled hooks. None may become verified completion by default.
   For Codex plain shell output, compare an exit-0 and nonzero command producing identical output; only the
   exact successful native completion may establish a successful read.

On capacity refusal, inspect owned work once, complete or clarify legitimate pending work, and retry once
only after observed progress. Do not increase limits, invent a close tool or repeatedly wake an unchanged
blocked assignment. On uncertain delivery, reconcile the native evidence before retrying.

## Report and clean up

For every case record pass, fail or unverified, actual root/child/call identities, packet and source hashes,
observed event locations, final output and any delivery uncertainty. Verify the candidate evidence through
its existing acceptance helper, not by editing JSON facts. Record every failure and its disposition.
Finish native work and consume any accidentally queued fixture messages before deleting disposable copies.
Keep only the private evidence needed for review; do not publish packet bodies or private transcripts.

Run the offline Claude contracts through the real Engine hook runner before merge. The separately agreed
live Claude smoke test checks fresh and concurrent reviewers, same-assignment clarification, cross-assignment
resume rejection, worker continuation and completion attribution. Documentation fixtures are not live model
proof. Local observations remain fallible provenance; protected main and the operator's merge are the boundary.
