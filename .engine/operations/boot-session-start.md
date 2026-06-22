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
   → silent) and the engine's open self-monitoring findings (read-only, from telemetry's register). Also relay
   a **stranded operator checkout** — the boot-invoked `checkout_health` detector finding the top-level project
   folder stuck off its branch or missing the engine's files — read-only at the open-findings tier, BELOW the
   governance alarms (a stranded local checkout cannot reach the protected branch), and **offer to repair it**.
   Boot only surfaces and offers; it never repairs.
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

**Beyond what this pack pushes, a session can reach the wiring map deliberately.** When a change needs an
impact check — what depends on a part, what checks or governs it — or when a part is unfamiliar, the
session can query the project's own map; when and how is `.engine/operations/knowledge-impact-check.md`.

**The un-stranding repair is operator-consented and lossless.** The assistant **waits for the operator to
say yes** — it never runs the repair un-asked. Only then does it run the un-stranding fix
(`checkout_health.unstrand`), the deployed-floor never-strand-main rule's one sanctioned write to the operator
checkout. It is **lossless-or-it-does-not-run**: it saves anything at risk — work that has drifted off the
branch, or unsaved changes — to a safe point first, then re-attaches the folder to its branch and restores the
missing engine files; if it cannot safely tell where to put the folder, it refuses rather than guess, and the
assistant says so. The assistant relays the plain-language result. (Bringing a merely *behind* folder up to
date is a separate, deferred step — the repair handles the broken states, not ordinary behind-ness.)

**A stranded pull request is the same shape (#136).** When boot surfaces a pull request that can't be merged,
the assistant likewise **waits for the operator's go-ahead**, then runs the reconcile (`pr_reconcile.reconcile`).
The reconcile first checks whether the clash is confined to the engine's two internal index files — the
knowledge graph and the self-map, the only files a clash on which is *spurious* (both sides are regenerations
of one source tree). If so, it reconciles the pull request against the latest default branch, regenerates those
two files from the reconciled tree, and keeps both pieces of work, **lossless-or-it-does-not-run**. If anything
but those two files clashed, it changes nothing, restores the branch exactly as it was, and routes the operator
to a plain-language decision rather than touching a real conflict. The assistant relays the plain result, and
never claims the merge is now guaranteed — a later change can still land first.
