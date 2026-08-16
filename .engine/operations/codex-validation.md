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
   not arrive and grounds manually via `uv run --directory .engine --frozen -- python tools/engine_status.py`).
4. **Check the write-gate only in a disposable git worktree.** Open that throwaway worktree as a separate
   Codex project and, WITHOUT starting a build, ask for a small file edit and then a shell `git commit`.
   Both must be denied with the plain exploring explanation. Inspect and discard the worktree afterward;
   never probe a guardrail's negative path by offering it the real project as the mutation target, because a
   failed hook could perform the action the check meant to deny.
5. Check Build entry: type `$engine-start` — the stance flips to building (and ONLY this typed verb
   does; casual phrasing must not).
6. **Check deferred live-helper discovery — including its failure branches, without disturbing the real
   project.** The exact Codex procedure is emitted by `.engine/tools/boot.py`
   `MCP_AVAILABILITY_CHECK_CODEX`; validate that procedure, never an invented substitute.
   - **Healthy deferred case, in this project:** start a fresh session and record whether the initial tool
     summary omits `mcp__engine_memory.health` and `mcp__engine_knowledge_graph.health`. When it omits them,
     confirm one search per helper discovers the exact tool, each fixed health call returns its exact server
     identity, and the first reply carries no helper-outage warning. If this Codex build surfaces either tool
     initially, that helper's deferred-discovery branch was **not verified** — do not call it a pass; repeat on
     a build/session that actually defers it.
   - **Controlled failure matrix, only in throwaway project copies:** never edit this project's real
     `.codex/config.toml`, trust state, or servers. Exercise these seven fresh-session cases explicitly:
     (1) both pass; (2) memory passes and knowledge discovery misses; (3) memory passes and knowledge is
     discovered but its call fails; (4) knowledge passes and memory discovery misses; (5) knowledge passes and
     memory is discovered but its call fails; (6) both discovery checks miss; (7) both are discovered but both
     calls fail. Produce a miss by omitting only that temporary registration. Produce a call failure by pointing
     only that registration at a temporary MCP fixture which registers the exact `health` operation but returns
     an MCP error — never damage a real store or server. In every mixed case the passing helper stays silent and
     only the failed helper warns; a discovery miss gives the trust-and-restart diagnosis, while a discovered
     call failure says registered-but-not-passing and does not blame trust. Remove temporary copies and fixtures
     afterward. If Codex cannot isolate them from the real project's trust state, record the negative arms as
     **not verified** rather than perturbing the real installation.
7. Check memory capture: after a turn or two, `$engine-status` shows no memory-capture warning (a
   "conversation wasn't saved" line means the transcript reader needs updating — a defect, not a
   deferral).
8. **Check review reach and the parent-override limit.** Confirm the ten personas under
   `.codex/agents/` are visible. From a live **Read Only** parent task, spawn one and confirm it reports
   without editing. Then start a separate live **Workspace Write** parent task, spawn the same persona,
   inspect the child's effective permission, and confirm Codex reapplies the parent's Workspace Write
   override instead of mechanically confining the child to its TOML `sandbox_mode = "read-only"`. The
   second arm is a platform-limit witness, not a desired permission result: if Codex later preserves the
   child's read-only boundary, reopen the provider exception and `codex-settings.md` rather than retaining
   a stale weakness claim.
9. Check help: `$engine-help` renders the commands with the `$` prefix.
10. **Check the retired Codex build-Routine path.** Confirm the scheduling UI still exposes no per-Automation
    permission profile and uses one shared default; if either fact changed, reopen `codex-settings.md` rather
    than retaining the retirement without its premise. In **Scheduled**, find every recurring task whose prompt
    contains `$engine-routine`, pause or delete it, and confirm it no longer appears under Active. Invoke the
    committed `$engine-routine` skill once in a normal Codex task and confirm it refuses to enter Routine and
    points to the supported interactive Codex or Claude Desktop path. This external disable is a **pre-merge
    migration gate**, not merely release follow-up: a shell process cannot prove which scheduler launched it,
    so repository code cannot safely substitute for removing the old task. During an upgrade crossing routine-mode
    0.2.0, confirm the preview and pull-request body carry the same disable-and-replace notice.
11. **Check the retired Codex scheduled self-review path.** Confirm
    `.engine/audits/self-review-setup.md` no longer tells an operator to create a Codex Automation and instead
    tells existing users to open **Scheduled**, identify the recurring audit by its prompt, pause or delete it,
    and then names both supported replacements: an ordinary interactive Read
    Only Codex task, and the durable GitHub/Claude recurring path. During an upgrade that crosses
    audit-library 0.3.0, confirm the upgrade preview and pull-request body carry that same disable-and-replace
    notice. Finally confirm the scheduling UI still has no per-Automation permission profile; if one now exists,
    reopen the audit rather than keeping the retirement on an obsolete platform limit.

## Done when

Every step above passed in a live Codex session — or each failure is recorded as a defect owed an
immediate fix in this line of work (a failure inside this bar is never re-scoped as a follow-up). The Codex
routine twin remains as an actionable refusal surface, not a write backend. Codex build and review Automations
stay retired until Codex and the repository host can preserve the operator-only merge boundary; interactive
Codex work and the durable GitHub/Claude schedules remain available.
The live acceptance record must be complete before the Engine release is cut; a documentation-only answer does
not satisfy the rollout gate.

## Notes

The honest split this runbook exists for: everything above rides the platform's own behavior, which
the repository's checks deliberately do not simulate — they prove the committed files are coherent,
in sync, and parity-complete, and THIS pass proves the platform actually consumes them. The
protected main branch and the operator's merge remain the only wall on every runtime; the hooks are
guardrails (Codex's own documentation says its pre-tool hook is not a complete enforcement
boundary, recorded in the exception ledger). Windows behavior is untested by this project and stays
so until someone runs this pass there.

Incident-derived replays — the boot-briefing leak, agent self-ack, and ready-PR-completion scenarios — live
in a sibling runbook, `codex-incident-replay.md`; this file stays the structural adapter pass.
