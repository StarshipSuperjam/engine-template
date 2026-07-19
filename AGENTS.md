# engine-template — construction governance (read first, every build session)

This is the Codex-side floor of the **construction governance** for the engine-template repository — the
repo where the Engine itself is being built, PR by PR, toward v1. The Claude-side floor is `CLAUDE.md`;
both describe the SAME governance for the same repo, each in its runtime's own terms, and the Engine's own
machinery (the session-start boot briefing, the Explore/Build write-gate, the validation suite, and the
build-orchestration runbook) governs every build session on either runtime.

**Required reading — before anything else.** Open and follow `.engine/conduct/defaults.md` and
`.engine/conduct/operator.md` — the maintainer's standing codes of conduct. They are part of this contract;
this file cannot pull them in automatically here, so reading them is the session's first act. If they cannot
be read, say so plainly and stop.

**Grounding — verify it, every session.** When the Engine's session-start hooks run they hand you an
orientation briefing, and your first reply opens with its titled **Project status** block. Verify the
briefing actually arrived. If it did not, the hooks are not running — on Codex that usually means they await
approval (run `/hooks` and approve the Engine's hooks; they need re-approval whenever the Engine updates
them) — and you must (1) tell the maintainer plainly that the Engine's automation is not active, (2) ground
manually by running `uv run --directory .engine -- python tools/engine_status.py` and presenting its output
before any other work, and (3) treat the write-gate and session-memory capture as OFF: stay read-only until
the maintainer explicitly starts a build.

## What this repo is, and what governs the work

`engine-template` is a GitHub repository template that stands up an AI-driven Engine — the externalized
state, memory, knowledge, attention, guardrails, and control plane a non-engineer needs to direct
cold-booting AI sessions on any project. The Engine builds itself here under its own governance. The
governing design record is the Engine's own decision set — the plain-language eADRs under
`.engine/contracts/` (for the dual-runtime work, start with eADR-0034) — plus the operations
runbooks under `.engine/operations/`. Where a needed rule or grammar genuinely doesn't exist, stop and
raise it with the maintainer and record the decision as an eADR — never invent structure silently.

## The trust model — informed consent on evidence, never code review

The maintainer (Shane) is a **non-engineer and the sole gate-holder.** He directs the build and approves
every merge but cannot read code, so no construction step may rest on code-reading or an engineer's review.
The merge gate is **informed consent over an evidence bundle**: mechanical green (deterministic checks),
independent cold-context cross-checks, behavioral demonstration the maintainer can run and vary himself,
and an honest self-report that names its own tier. Confidence is bounded by how much of a change has a
non-AI correlate — that bound is named, never dressed up.

## The two cold-context audit gates (HARD — every build session)

1. **Plan gate** — every session plan is cold-context audited **before** executing it.
2. **Deliverable gate** — every built PR is cold-context audited **again before merge**.

Each gate launches **≥4 independent agents sharing no session context** with distinct lenses (the installed
review personas — the same personas serve both runtimes). Findings are tagged blocking / serious / nit;
every blocking and serious finding is resolved or explicitly rejected with logged rationale before
proceeding. Ground-truth every concrete finding against the source before recording it.

## Harness invariants (hold through v1)

- **Full spec capability every PR — no milestone licenses an under-build.** A slice ships complete or its
  deferral is an explicitly recorded decision, never a quiet stub.
- Every change is a **pull request against protected `main`**; **validator-green before merge**. Run the
  same checks CI runs: `uv run --directory .engine --frozen -- python tools/validate.py --suite CI` and
  `uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b`. The
  self-test suite runs about 4 minutes (4,000+ tests, varying with machine and cache), so give it a
  generous time limit: a tool whose command
  timeout defaults to ~2 minutes cuts it off mid-run — which reads as a hang, not a failure — so set an
  explicit timeout with headroom (≥ 9 minutes) or run it in the background. While iterating, run just the
  test file(s) for what you changed — narrow the pattern, e.g. `-p 'test_<name>.py'` (seconds); the full
  suite is the pre-submission gate, never the iteration loop, and it can run in the background so it never
  blocks.
- **Plan-first, one step at a time**; each step finished and re-grounded from merged disk before the next.
- **Building starts only by the maintainer's explicit say-so** — `$engine-start` (or an explicitly approved
  plan). Never infer build intent from casual phrasing. The write-gate is a local guardrail; the protected
  branch and the maintainer's merge are the only wall.
- **Guardrail-weakening is always surfaced at the merge** and clears only via the maintainer's own
  deliberate acknowledgment (the `guardrail-ack` label — never applied by the AI).
- **Tests are wired through the review** — a green test name is never trusted alone; behavioral demos must
  be able to fail, and each has a declared fate.
- **Operator-facing copy uses the right word, judged in context — never a banned-word list.** Plain
  language over engineer-shorthand is a writing-and-review judgment.
- **Dual-runtime work follows the render rule**: the Claude-side surfaces (`.claude/skills/`,
  `.claude/agents/`) are canonical; their Codex twins (`.agents/skills/`, `.codex/agents/`) are committed
  renders produced by `tools/codex_gen.py` — edit the source and regenerate, never the render. Every
  deliberate runtime asymmetry is an entry in the provider-exception ledger, never prose.

## Frozen names (a rename of any is a guardrail-weakening change)

- `engine-ci` — required check #1: the validator plus the protection-detection guard.
- `engine-guard` — required check #2: the guardrail-weakening classifier.
- `guardrail-ack` — the label the maintainer applies to deliberately clear a flagged change.

## Resume order for a build session

1. This file — the trust model, the two gates, and the invariants above.
2. `.engine/operations/operating-modes.md` for the session stance, and `.engine/self-map.md` with
   `.engine/operations/knowledge-impact-check.md` for what each part touches and what checks it.
3. The eADRs governing the current work (`.engine/contracts/`).
4. `.engine/operations/build-orchestration.md` — the live build workflow — then plan the one next step, run
   the plan gate, build, run the deliverable gate, and assemble the evidence bundle.
