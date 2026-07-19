<!-- engine-template:landing-front -->

<div align="center">
  <img src="assets/engine_banner.jpg" width="700" alt="Engine — direct AI on real engineering work, and stay the person who decides, without reading a line of code" />

  <p><strong>Direct real engineering work — and stay the person who decides — without reading a line of code.</strong></p>

  <p>
    <a href="LICENSE"><img alt="License: Source-available (Apache-2.0 + Commons Clause)" src="https://img.shields.io/static/v1?label=license&message=Source-available%20%28Apache-2.0%20%2B%20Commons%20Clause%29&color=0b7285" /></a>
    <a href="#status"><img alt="Status: pre-1.0, approaching v1" src="https://img.shields.io/static/v1?label=status&message=pre-1.0%20%C2%B7%20approaching%20v1&color=e8590c" /></a>
    <a href="#runtime-support"><img alt="Runtime: Claude Code" src="https://img.shields.io/static/v1?label=runtime&message=Claude%20Code&color=6f42c1" /></a>
  </p>

  <p><a href="#get-started"><strong>Jump to Get started ↓</strong></a></p>
</div>

The Engine keeps a human director in charge of AI that writes the code. It does that by making every change
**legible** and **safe to approve** — so you can direct serious work on a real project and approve it on
evidence you can actually judge, rather than on trust or a code review you'd have to perform yourself.

## Why the Engine

Most AI coding tools assume a power user who will read the diff and catch what's wrong. The Engine makes the
opposite bet: **you shouldn't have to read code to stay in control of it.** You remain
the decision-maker by governing on evidence, not by reviewing source. "Build real software without reading
code" isn't a limitation the Engine works around; it's the workflow the whole thing is built to support.

## How you stay in control — evidence, not code review

Every change the AI proposes arrives as a pull request, and nothing reaches your protected `main` until you
merge it. Your approval never rests on reading the code. It rests on an **evidence bundle** you can weigh:

- **A demonstration you run yourself** — and vary — to see the behavior with your own eyes.
- **Deterministic checks that must pass** before a change is even offered to you.
- **A plain-language account** of what changed, why, and what it could put at risk — including an honest
  statement of how sure anyone can be.

Those automatic checks are a floor: they mechanically hold a pull request until they pass. But passing checks
don't *approve* anything — your merge does, and it's the only thing that can. That bundle, not a code review,
is the gate. And because every change lands as a reviewable, revertible pull request, a decision you regret is
one you can undo.

## What's inside

The Engine keeps its thinking and its safety controls as files in your own repository — open to inspection, not
a black box. Every Engine ships with all of these:

**Externalized cognition**

- **Memory** — a committed, searchable record of decisions, pushback, and lessons that survives across cold
  sessions. It captures as you work, distills itself over time, lets noise decay, and can back itself up to a
  private repo.
- **State** — a small committed "where things stand" pointer a fresh session reads first to orient, and the
  floor it falls back to when GitHub is unreachable.
- **Knowledge** — a queryable map of how your project's parts actually connect, generated from the code itself
  rather than guessed, and refreshed as the project changes.
- **Attention** — a deterministic, inspectable prioritizer for what to do next, with a built-in guarantee that
  blocking problems surface ahead of new features — a structural rule, not a dial anyone has to calibrate.

**Controls & gates**

- **Guardrails & the review gate** — a suite of automatic checks, a protected `main` the AI cannot merge to
  on its own, and a detector that forces a deliberate, logged sign-off before any change can weaken a safety
  check.
- **Explore / Build modes** — sessions are read-only by default; the AI can change files only after a
  deliberate switch into build, and every change still lands as a reviewable pull request.

**Lifecycle**

- **Boot briefing & status** — a plain-language orientation at the start of every session, and an on-demand
  dashboard of where things stand, what shipped, and what needs you.
- **First-run setup** — a guided walkthrough that installs what you choose, wires it in, verifies it, and then
  removes its own scaffolding (see [Get started](#get-started)).
- **Unattended routines** — advance a *planned* build on a schedule while you're away; it never merges (see
  [Running unattended](#running-unattended)).
- **Periodic self-review** — a cold, independent audit of the Engine's own health that reports what has
  drifted or outlived its use, and never changes anything itself.

**Runtime & resilience**

- **Native in Claude Code and Codex** — one canonical core, wired into both runtimes (see
  [Runtime support](#runtime-support)).
- **Degrades to plain git files** — every index and cache rebuilds from what's committed, so a downed service
  slows the work but never strands it.

## Optional modules

At setup you choose from optional add-ons, grouped by the part of the work they support. Leave out anything you
don't need — you can add it later, and each one is removable.

**Product management**

- **Product design** (`/engine-design`) — describe what you want to build in plain words, and the Engine helps
  you write it down clearly, checks it's complete and well-formed, and settles it as the description to build
  from. It checks the description, never whether the idea is a good one — that stays your call.
- **Project board** (`/engine-board-setup`) — a GitHub Projects board showing what's next, what needs your
  review, and known issues. Never required: the Engine works the same from your issues and pull requests, and
  you can delete the board later without losing anything.

**Software configuration management**

- **Migration discipline** — before a database change that could lose data or can't be undone, the Engine
  stops and brings you in — in plain language, with a safer option — instead of pressing ahead. A habit it
  follows, backed by your review; it never runs a migration for you.
- **Dependency discipline** — an automatic check on the outside libraries your project pulls in. It can block
  a change that brings in a known security hole or a risky license, with gentler nudges toward pinned
  versions; a genuinely unavoidable case can pass with a recorded decision.
- **Upstream contribution** — offer your changes to a project you don't own (an open-source project you've
  forked) as a pull request from your fork, carrying only your files and never the Engine's own.

**Verification & validation**

- **Plan review** — before a change is built, a fresh set of reviewers checks the plan: is it the right
  problem, is the design sound, can it be built and run safely.
- **Finished-work review** — before a built change is submitted, fresh reviewers check the finished work: does
  it do what was asked, is it soundly built, is it safe to release.

The two review packs are the cold reviewers that **strengthen** your evidence bundle. They only advise — they
never block, and your merge stays the only approval. Leave them out and that review step is simply disclosed as
not running, never passed off as a silent green.

## Get started

### Before you begin

- A **GitHub account**, and a repository created with **Use this template** (not a fork — see
  [Contributing](#contributing)).
- **Claude Code** (current version), or **Codex** (a 2026 build with hooks support, around v0.114 or later).
- The **GitHub CLI (`gh`) signed in** — otherwise assigning who reviews your changes, and the review gate
  itself, quietly defer until it is.

### The steps

1. **Create your repo.** Click **Use this template** at the top of this page.
2. **Open it in Claude Code or Codex.** *On Codex only:* approve the Engine's session hooks first, or setup won't start on its own.
3. **Run setup.** Say **"set up my project"** — or type `/engine-setup` (`$engine-setup` on Codex). The Engine
   walks you through a couple of choices: **how it commits on your behalf** (on your own, or with a team — a
   bigger setup with a security trade-off it explains), and **which optional add-ons to include**. Then it
   installs, wires, and verifies your selection.
4. **Two GitHub steps only you can do.**
   - Approve the **one-time authorization screen** that lets the Engine turn on branch protection. During
     setup, `gh` opens this screen in your browser — approve it there. Be aware the permission it asks for
     covers **all of your GitHub repositories, not just this one**; that breadth is real, though it only
     reaches repositories you already have access to, and it's what lets the Engine set up the review gate.
   - **Enable GitHub Actions** on the new repo (its **Actions** tab). Until you do, the automatic checks can't
     run — and *no* pull request can pass them, including setup's own.
5. **Turn on the two live helpers.** Approve the Engine's **memory** and **knowledge** servers — on Claude
   Code, when the app prompts (or in its settings); on Codex, by trusting the project in its settings — then
   **fully quit and reopen** the app. Until then the Engine runs on its committed-file fallback: it works, but
   can be out of date.

When setup finishes it removes its own walkthrough files and tells you it's done — that's your signal the Engine
is live. From there, just make your first request, or ask for a status readout to see where things stand.

### What the Engine handles for you

So the steps above don't read as more work than they are — here's what setup does on its own:

- Turns on the protected-`main` review gate and creates the labels it needs (both need `gh` signed in).
- Installs its own tool runtime into a private, git-ignored folder — never touching your system Python or your
  `PATH`.
- Swaps the template's README, instruction files, and license for your project's own starters. Your new repo
  will then show **"No license"** on GitHub — that's expected: the template's license shouldn't bind your
  project.
- Turns on **GitHub's own** secret scanning, push protection, and code scanning where your repo's plan supports
  them — best-effort and advisory, never a required merge check.
- Offers a private, off-repo backup of its memory, and removes its own setup scaffolding once it's done.

<details>
<summary>More on how first-run setup runs</summary>

Setup follows a fixed sequence — **gather → confirm → apply → verify → retire**. It reads your repo's
coordinates, asks only the choices it can't derive (how it commits, which add-ons), and writes those down as a
checkpoint **before it changes anything**. If it's interrupted after that point, the next session **resumes
from the checkpoint** rather than starting over or re-asking.

Apply is ordered and idempotent, and each step degrades with a plain-language reason rather than failing hard —
with one exception: if the one-time tool download can't complete (for example, you're offline), setup **stops
safely** and never falls back to a guess. It then verifies the result is coherent, pauses in plain words if
anything doesn't fit, and only removes its own setup files once that verify is clean — the setup tool's absence
afterward is the signal that setup is done.

Arriving into an existing ("brownfield") project is handled too: setup only replaces the Engine's *own*
traveled files and repairs an existing protection ruleset in place, so it never clobbers work you already have.

</details>

## Runtime support

Claude Code is the primary, most-exercised runtime. The Engine also serves Codex natively from the same core —
but that path is newer and not yet stress-tested: genuinely supported, though less proven, with the differences
and rough edges worth knowing below.

| Capability | Claude Code | Codex |
|---|---|---|
| Instruction floor | `CLAUDE.md` (conduct auto-imported) | `AGENTS.md` (conduct by required reading — an instruction, not a mechanism) |
| Session hooks (boot, write-gate, memory, status) | Native, on by default | Native; **requires your one-time approval in Codex settings**, and re-approval after any Engine update that changes them — the Engine tells you when |
| Explore / Build write-gate | A session hook that blocks writes until you build | Same gate; Codex's own docs call its hook a guardrail, not a complete boundary — the protected branch and your merge remain the wall on both |
| Build entry | `/engine-start` or plan approval | `$engine-start` only (Codex has no plan-approval signal) |
| Typed commands | `/engine-…` (10) | `$engine-…` (10) |
| Review personas | 10 native agents | The same 10, rendered natively (read-only sandbox) |
| Memory & knowledge servers | `.mcp.json` | `.codex/config.toml` (trusted projects only) |
| Session-memory capture | Native transcripts | Dedicated reader; Codex's transcript format is not a stable interface, so a format change degrades **loudly** ("memory not captured"), never silently |
| Minimum version | Current | A 2026 build with hooks support (~v0.114+) |
| Windows | Supported | Untested by this project — the hook launcher carries the standard fallbacks, but no Windows/Codex run has verified them |

## Running unattended

The Engine can advance a *planned* build on a schedule while you're away — each run does one planned chunk,
adds its commits to an open pull request, and **never merges**. Your review at the merge stays the only gate.

The short version: first plan the build in a normal, interactive session (a routine *advances* a plan, it
doesn't make one), then schedule `/engine-routine` (Claude Code) or `$engine-routine` (Codex) pointed at that
build's branch, in an isolated copy of the repo. When you're back, open a normal session and ask the Engine to
wrap the pull request up for your merge — a routine never finishes it for you.

<details>
<summary>Full setup for unattended routines</summary>

**First, plan the build (a normal, interactive session).** Before you schedule anything, work with the Engine
in a normal session to produce what it will follow: a build Issue holding the ordered checklist and the files
each step may touch, and an open **draft pull request** on a branch. The routine reads that Issue and adds
commits to that pull request, so point the schedule at that build's branch, not a fresh copy of your default
branch. With no plan to find, the first run has nothing to do and says so.

**On Claude Code — a Claude Desktop routine.** Create a routine and choose when it runs; put `/engine-routine`
in its Instructions; turn on **"Work in an isolated copy of the repo"** (the Engine refuses to write unless the
run is in a dedicated worktree, so this is required) and make sure that copy is on your build's branch; and set
the **permission mode** to the one that lets the session act without pausing to ask you.

**On Codex — a Codex Automation.** Create an Automation with a schedule; put `$engine-routine` in its prompt;
and choose **"run on a dedicated background worktree"**, on your build's branch. Two settings live in your Codex
config (`.codex/config.toml`), not on the Automation's screen: the sandbox must be workspace-write
(`sandbox_mode = "workspace-write"`) and the approval posture non-interactive (`approval_policy = "never"`) —
subject to your organization's policy. If that policy won't allow the non-interactive posture, the run pauses
for an approval no one gives, so confirm it's in effect before you rely on it. Finally, **grant the run network
access** so it can push, open the pull request, and file Issues.

**Both runtimes — confirm before you rely on it.**

- Keep the computer on and the app running during the scheduled time — a local run only works while your
  machine is awake.
- The version must support scheduling (Codex Automations need a 2026 build).
- git/GitHub credentials must be reachable to a scheduled run **without** an interactive prompt — otherwise it
  can't push or even leave an Issue.
- **On Codex, after any Engine update that changes its hooks, run one normal interactive session and re-approve
  the hooks (`/hooks`) before the routine runs again.** Codex turns changed hooks off until you re-trust them,
  and an unattended run can't do that itself. With its hooks off, the run is *designed* to notice the missing
  start-of-session briefing and refuse to write — but that's the run following its own procedure, not a
  mechanical lock, so your review at the merge is the real guarantee. (Claude Code keeps its hooks on, so this
  step doesn't apply there.)

You'll see each run in your scheduling app's history and its progress on the pull request. If a run can't
safely start — hooks not running, or not isolated — it reports why and stops. It files a GitHub Issue only for
something it hits mid-build that needs you; when GitHub itself is unreachable it can't file one, so the run's
own history in the app is the only record of that. **The routine never finishes the pull request** — when
you're back, open a normal session and ask the Engine to wrap it up, review it for cohesion, and submit it for
your merge.

</details>

The Engine's periodic **self-review** — its own health check — can also run unattended on a schedule, including
from Codex; that's set up separately. See
[Setting up the engine's scheduled self-review](.engine/audits/self-review-setup.md).

## Status

The Engine is **approaching its first release (v1)** and under active construction toward it. It is still
pre-1.0 until that release is cut — expect rapid change until then.

## Contributing

A contribution model for the Engine is not defined yet. One behavior to revisit when it is: a repository
**forked** from this one is currently treated as a contributor's fork, so the Engine does not prompt it at
session start to run first-run setup (the boot setup offer is suppressed for forks of the Engine's own home).
The sanctioned way to *adopt* the Engine is **Use this template**, not a fork — but anyone who does adopt by
forking won't see that start-up prompt (they can still run `/engine-setup` directly). When contribution is
defined, we'll decide whether a fork-based adopter needs a distinct signal.

## License

Source-available under the [Apache License 2.0 with the Commons Clause](LICENSE). You may use, modify, fork, and
redistribute the Engine (subject to the license's attribution terms). What the license does not grant is the
right to *Sell* the Engine: under the Commons Clause, "Sell" covers providing a product or service whose value
derives substantially from the Engine — and expressly includes charging for hosting, consulting, or support
around it, not only reselling the code. This is **not** an OSI-approved open-source license, so GitHub shows
this template repository's license as "Other" rather than a named license. See [LICENSE](LICENSE) for the
governing terms.
