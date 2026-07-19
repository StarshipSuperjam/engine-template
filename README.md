<!-- engine-template:landing-front -->

<div align="center">
  <img src="assets/engine_banner.jpg" width="700" alt="Engine" />

  <p><strong>Scaffolding that lets you direct AI on real engineering work — and stay the person who decides — without reading a line of code.</strong></p>
</div>

The Engine is not a faster, more capable autonomous agent. It's the opposite bet: a way to keep a human who *can't* read code firmly in charge of AI that can. Its job is to make what the AI does **legible** and **consentable** to that person — so you can direct serious work on a real project, and approve it, on evidence you can actually judge.

## This is for the person who can't verify the work — and has to govern it anyway

You direct the work, and you approve every change, from the very first commit. You are not expected to read code, debug a failure, or take the AI's word that it did the right thing. Most AI tooling quietly assumes a power user who can check the output. The Engine assumes you can't — and is built, end to end, so that you don't have to.

## You approve the work without reading the code

Every change the AI proposes arrives as a pull request, and nothing reaches your main branch until you say so. But your approval never rests on reading or trusting the code. It rests on evidence you *can* weigh:

- a demonstration you run yourself, and vary, to see the behavior with your own eyes;
- independent checks that have to pass before a change is even offered to you;
- a plain-language account of what changed, why, and what it could put at risk — including an honest statement of how sure anyone can be.

That bundle — not a code review — is the gate. It is the whole point of the Engine.

## What you get to stand on

Every AI session starts cold: it remembers nothing. Left there, you'd re-explain your project every time and hope for the best. Instead, the Engine gives the work a durable footing — committed to the repository itself, and open to you:

- where things stand right now, and what's still unfinished;
- the decisions made so far and *why* — written down where you can read them, not buried in a chat log only the AI ever saw;
- a current map of how the project fits together;
- a sense of what matters next, so a fresh session picks up instead of starting over.

This isn't a catalog of the AI's abilities — it's the ground *you* direct from: committed, inspectable, and yours. The AI builds faithfully not because it's clever, but because the ground under it is solid and in plain sight.

## Constrained autonomy is the feature

Slow, gated, and verifiable beats fast and opaque. The Engine deliberately trades raw autonomy for trust: work is proposed before it lands, lands only behind your approval, and can always be undone; what governs the work is written down and citable; and if a supporting service goes down, it falls back to plain files in git rather than stranding you. You give up the thrill of an agent that simply runs — and you get work you can actually stand behind.

## Get started

1. Click **Use this template** above to create your own repository.
2. Open it in [Claude Code](https://claude.com/claude-code), or in [Codex](https://openai.com/codex/) — the Engine runs natively in either.
3. A guided first-run setup walks you through your choices and stands up the Engine for your project. If it doesn't start on its own that first session — some tools ask you to approve the Engine's session hooks first — just say **set up my project**, or type `/engine-setup`, to begin.

## What's inside

The Engine externalizes the cognition and controls it runs on — each one a committed, inspectable subsystem rather than a black box:

- **Memory** — a git-committed, append-only memory ledger with a full-text search index: it captures decisions, pushback, and lessons per session and recalls them by relevance, with AI-judged consolidation and frecency-scored retention, so signal compounds and noise decays across sessions.
- **State** — an externalized, committed state cursor: the standing-situation pointers and open-debt count a cold session reads first to orient deterministically, before it touches anything.
- **Knowledge** — a knowledge graph derived from the repository's own surfaces and regenerated on change — entities, relationships, and neighbors, queryable over MCP — so a session maps how the project actually fits together from source, not from guesswork.
- **Attention** — a committed prioritization policy plus a deterministic ranking function that budgets what surfaces at boot and orders the work queue: explicit and inspectable, not an opaque heuristic.
- **Guardrails & the review gate** — a deterministic validation suite (presence, coverage, shape, and coherence checks) gating every pull request, a protected `main`, and a guardrail-weakening classifier that forces an explicit, logged acknowledgment for any change that relaxes a check.
- **Explore / Build modes** — an enforced write-gate: read-only investigation and planning by default, file edits only after a deliberate build transition, every change landing as a reviewable pull request — autonomy bounded by construction, not by good behavior.
- **One-shot provisioning** — an instantiator that runs gather → confirm → apply → verify → retire on first use: it installs only the modules you select, wires them in, verifies coherence, and self-deletes the setup scaffolding.
- **Native in Claude Code and Codex** — one canonical Engine core with a native adapter per AI runtime: wired into Claude Code (its hooks, skills, agents, and MCP control plane) and into Codex (its hooks, skills, agents, and project-scoped MCP), with a parity check that keeps every capability paired across the two and a committed ledger for the few sanctioned differences. Built to degrade to plain git-tracked files when an out-of-repo substrate is unavailable, so a broken service never strands the work.

## Runtime support

The same Engine serves both runtimes from one core; the differences worth knowing:

| Capability | Claude Code | Codex |
|---|---|---|
| Instruction floor | `CLAUDE.md` (conduct auto-imported) | `AGENTS.md` (conduct by required reading — an instruction, not a mechanism) |
| Session hooks (boot, write-gate, memory, status) | Native, on by default | Native; **requires your one-time approval** (`/hooks`), and re-approval after any Engine update that changes them — the Engine tells you when |
| Explore / Build write-gate | PreToolUse gate | Same gate; Codex's own docs call its hook a guardrail, not a complete boundary — the protected branch and your merge remain the wall on both |
| Build entry | `/engine-start` or plan approval | `$engine-start` only (Codex has no plan-approval signal) |
| Typed commands | `/engine-…` (10) | `$engine-…` (10) |
| Review personas | 10 native agents | The same 10, rendered natively (read-only sandbox) |
| Memory & knowledge servers | `.mcp.json` | `.codex/config.toml` (trusted projects only) |
| Session-memory capture | Native transcripts | Dedicated reader; Codex's transcript format is not a stable interface, so a format change degrades **loudly** ("memory not captured"), never silently |
| Minimum version | Current | A 2026 build with hooks support (~v0.114+) |
| Windows | Supported | Untested by this project — the hook launcher carries the standard fallbacks, but no Windows/Codex run has verified them |

## Running unattended (routines)

The Engine can advance a *planned* build on a schedule while you're away — each run does one planned chunk,
adds its commits to an open pull request, and **never merges**; your review at the merge stays the only gate.

**First, plan the build (a normal, interactive session).** A routine doesn't plan — it *advances* a plan. So
before you schedule anything, work with the Engine in a normal session to produce what it will follow: a build
Issue holding the ordered checklist and the files each step may touch, and an open **draft pull request** on a
branch. The routine reads that Issue and adds commits to that pull request, so point the schedule at that
build's branch (below), not a fresh copy of your default branch. With no plan to find, the first run has
nothing to do and says so.

Then set up the schedule in your runtime, running `/engine-routine` (Claude Code) or `$engine-routine` (Codex):

**On Claude Code — a Claude Desktop routine.** Create a routine and choose when it runs; put `/engine-routine`
in its Instructions; turn on **"Work in an isolated copy of the repo"** (worktree mode — the Engine refuses to
write unless the run is in a dedicated worktree, so this is required) and make sure that copy is on your build's
branch; and set the **permission mode** to the one that lets the session act without pausing to ask you — the
most permissive / "allow everything" option in Claude Desktop's permission settings (the same place you approve
or restrict what Claude can do).

**On Codex — a Codex Automation.** Create an Automation with a schedule (or an RFC-5545 recurrence); put
`$engine-routine` in its prompt; and choose **"run on a dedicated background worktree"** (the same isolation
requirement), on your build's branch. Two settings live in your **Codex settings/config** (`.codex/config.toml`),
not on the Automation's own screen: the sandbox must be workspace-write (`sandbox_mode = "workspace-write"`) and
the approval posture non-interactive (`approval_policy = "never"`) — subject to your organization's policy. If
that policy won't allow the non-interactive posture, the run pauses for an approval no one gives, so it can't
run unattended; confirm it's in effect before you rely on it. Finally, **grant the run network access** so it
can push, open the pull request, and file Issues.

**Both runtimes — confirm before you rely on it.**
- Keep the computer on and the app running during the scheduled time — a local run only works while your machine
  is awake.
- The version must support scheduling (Codex Automations need a 2026 build).
- git/GitHub credentials must be reachable to a scheduled run **without** an interactive prompt — otherwise it
  can't push or even leave an Issue.
- **On Codex, after any Engine update that changes its hooks, run one normal interactive session and re-approve
  the hooks (`/hooks`) before the routine runs again.** Codex turns changed hooks off until you re-trust them,
  and an unattended run can't do that itself. With its hooks off, the run is *designed* to notice the missing
  start-of-session briefing and refuse to write — but that is the run following its own procedure, not a
  mechanical lock, so your review at the merge is the real guarantee. (Claude Code keeps its hooks on, so this
  step doesn't apply there.)

You'll see each run in your scheduling app's history and its progress on the pull request. If a run can't safely
start — hooks not running, or not isolated — it reports why in that run's output and stops. It files a GitHub
Issue only for something it hits mid-build that needs you; when GitHub itself is unreachable it can't file one,
so the run's own history in the app is the only record of that. **The routine never finishes the pull request** —
when you're back, open a normal session and ask the Engine to wrap it up, review it for cohesion, and submit it
for your merge.

## Status

The Engine is pre-1.0 and under active construction toward its first milestone. Expect rapid change.

## Contributing

A contribution model for the Engine is not defined yet. One behavior to revisit when it is: a repository
**forked** from this one is currently treated as a contributor's fork, so the Engine does not prompt it at
session start to run first-run setup (the boot setup offer is suppressed for forks of the Engine's own home).
The sanctioned way to *adopt* the Engine is **Use this template**, not a fork — but anyone who does adopt by
forking won't see that start-up prompt (they can still run `/engine-setup` directly). When contribution is
defined, decide whether a fork-based adopter needs a distinct signal.

## License

Source-available under the [Apache License 2.0 with the Commons Clause](LICENSE). You may use, modify, fork, and
redistribute the Engine (subject to the license's attribution terms). What the license does not grant is the
right to *Sell* the Engine: under the Commons Clause, "Sell" covers providing a product or service whose value
derives substantially from the Engine — and expressly includes charging for hosting, consulting, or support
around it, not only reselling the code. This is **not** an OSI-approved open-source license, so GitHub shows it
as "Other" rather than a named license. See [LICENSE](LICENSE) for the governing terms.
