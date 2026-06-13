---
title: Boot — the SessionStart orientation pack
---

## Purpose

Orient a cold session: assemble a bounded, prioritized, plain-language `Project status` pack from
committed state and the substrates that exist, and inject it before the first prompt so the session
starts grounded instead of blind. This is the heaviest member of the orientation family and runs
automatically — there is no manual "start the engine" step. Enter it whenever a session begins
(`startup`, `resume`, or `clear`); it is read-only and never blocks. Beneath it, and independent of it,
sits the floor: the root `CLAUDE.md` the platform always loads, which grounds the session even if this
pack does not run.

## Steps

The mechanism is `.engine/tools/boot.py`, wired as a `SessionStart` hook in `.claude/settings.json`; it
runs these steps in order and degrades over any source that is absent — one failure costs that line
only, never the whole pack, and the session never halts.

1. Read the committed state cursor (`.engine/state/state.json`). If it is unreadable or not a
   schema-version-1 cursor, say so plainly ("I couldn't read where the project stands") and treat
   project status as unknown — never halt.
2. Detect the governance-critical alarms to pin at the top of the status dashboard: the protected-branch signal
   (relayed from `protection_guard`; off → a nag, unverifiable → an honest "don't assume it's on", on
   → silent) and the engine's open self-monitoring findings (read-only, from telemetry's register).
3. Consume the attention ranking (`attention.rank_live`) in its given precedence order — never re-rank —
   and resolve each ranked item to a plain-language line under "Needs your attention".
4. Read the integration-debt readout (offline count from state, rendered loud-if-stale) and the
   recently-shipped digest (from merged pull requests — there is no changelog).
5. Assemble the AI-facing **briefing** and inject it as `additionalContext`. The briefing reaches the
   model, never the operator's screen, so it instructs the assistant to render a short titled `Project
   status` block first (all-clear, or a `⚠` line when something fired), relay the governance-critical
   alarms to the operator in plain words, and surface a brief needs-attention headline; the operator-toned
   status dashboard follows for grounding. The present-marker line and the must-push set are a fixed relay
   over the signals the substrates already detected — boot computes nothing new.

To print the assembled briefing by hand (a debug view of what the hook injects): `python tools/boot.py pack`.

## Done when

The assistant opens its first reply with a short titled `Project status` block (the present marker a
grounded session always renders; its absence is how the floor tells the operator the engine did not
ground) and relays any governance alarm in plain words. Any degraded or unverifiable source is named in
plain language rather than silently dropped, and nothing was regenerated — boot only reads and surfaces.

## Notes

`compact` is deliberately not a trigger: a full re-render after compaction is a deferred enhancement
that must never be depended on, so the reliable post-compaction floor stays the re-injected `CLAUDE.md`
plus the next per-prompt scent. The memory reversible-forgetting readout and the modes stance line
render only once those substrates exist, so on a fresh engine they are simply absent.
