---
title: Validate the Codex adapter live — the post-merge pass bar, and the update re-trust ritual
---

## Purpose

Prove, in a live Codex session, the adapter behavior no check in this repository can prove from
inside (the platform's hook firing, discovery, and sandbox behavior only exist under a running Codex
binary — eADR-0034), and keep Codex sessions healthy across engine updates. Enter this runbook right
after the dual-runtime change merges (the named acceptance step), after any later change to the
Codex adapter surfaces, or when a Codex session reports its hooks are not running.

## Steps

1. **Item zero — version.** Run `codex --version` and confirm the installed Codex is a build with
   hooks support (a 2026 build, around v0.114 or later). On an older build every later step fails
   for that reason alone — upgrade first, or stop here and say so.
2. Open the repository in Codex, run `/hooks`, and approve the engine's hooks (they are skipped
   until trusted; after any engine update that changes `.codex/hooks.json` they need re-approval —
   the engine says so whenever it changes that file).
3. Start a fresh session and check the floor and grounding: the session reads `AGENTS.md`, and its
   first reply opens with the **Project status** block (or plainly discloses that the briefing did
   not arrive and grounds manually via `uv run --directory .engine -- python tools/engine_status.py`).
4. Check the write-gate: ask for a small file edit WITHOUT starting a build — the edit must be
   denied with the plain exploring explanation; a shell `git commit` must be denied the same way.
5. Check Build entry: type `$engine-start` — the stance flips to building (and ONLY this typed verb
   does; casual phrasing must not).
6. Check the helpers: the `mcp__engine-memory__*` and `mcp__engine-knowledge-graph__*` tool families
   are callable — or their absence is plainly disclosed with the trust-then-restart fix.
7. Check memory capture: after a turn or two, `$engine-status` shows no memory-capture warning (a
   "conversation wasn't saved" line means the transcript reader needs updating — a defect, not a
   deferral).
8. Check review reach: the ten personas under `.codex/agents/` are visible to the session and a
   spawned one reports without editing anything.
9. Check help: `$engine-help` renders the commands with the `$` prefix.

## Done when

Every step above passed in a live Codex session — or each failure is recorded as a defect owed an
immediate fix in this line of work (a failure inside this bar is never re-scoped as a follow-up;
the only standing follow-up is the routine adapter and its `engine-routine` command, ledgered in
`.engine/policies/provider-exceptions.json`).

## Notes

The honest split this runbook exists for: everything above rides the platform's own behavior, which
the repository's checks deliberately do not simulate — they prove the committed files are coherent,
in sync, and parity-complete, and THIS pass proves the platform actually consumes them. The
protected main branch and the operator's merge remain the only wall on every runtime; the hooks are
guardrails (Codex's own documentation says its pre-tool hook is not a complete enforcement
boundary, recorded in the exception ledger). Windows behavior is untested by this project and stays
so until someone runs this pass there.
