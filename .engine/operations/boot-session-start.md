---
title: Boot — the SessionStart orientation pack
---

## Purpose

Orient a cold session: assemble a bounded, prioritized, plain-language `Project status` pack from
committed state and the substrates that exist, and inject it before the first prompt so the session
starts grounded instead of blind. This is the heaviest member of the orientation family and runs
automatically — there is no manual "start the engine" step. Enter it whenever a session begins
(`startup`, `resume`, or `clear`); it is **read-only of canonical state** (its one local write is the
gitignored standing-alarm presentation ledger — see Notes) and never blocks. Beneath it, and independent of it,
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
   - **Anti-habituation collapse (D-269).** A standing governance alarm renders on **every** session it is
     live, but one whose underlying condition is **unchanged since last relayed in full** collapses to a
     **terse one-line reminder that still names the consequence and still offers the fix**; a **new, changed,
     or worsened** condition relays in **full**. New-versus-old is carried in words ("still ... (unchanged)"
     vs the full statement / "this has grown"), never length alone. The **present-marker line and the
     all-clear render never collapse** — only the must-push relay payload behind the marker varies. The read,
     the terse-versus-full decision, and the write all run in the deterministic hook (`boot_alarm_ledger`),
     never the model, and are **fail-toward-full**: a missing/unreadable/write-failed ledger, or any
     ambiguity, renders the alarm in full (repetition is the tolerable failure; suppression is not).

To print the assembled briefing by hand (a debug view of what the hook injects): `python tools/boot.py pack`.

## Done when

The assistant opens its first reply with a short titled `Project status` block (the present marker a
grounded session always renders; its absence is how the floor tells the operator the engine did not
ground) and relays any governance alarm in plain words. Any degraded or unverifiable source is named in
plain language rather than silently dropped, and no canonical state was regenerated — boot reads and
surfaces, its sole local write the gitignored standing-alarm presentation ledger.

## Notes

`compact` is deliberately not a trigger: a full re-render after compaction is a deferred enhancement
that must never be depended on, so the reliable post-compaction floor stays the re-injected `CLAUDE.md`
plus the next per-prompt scent. The memory reversible-forgetting readout and the modes stance line
render only once those substrates exist, so on a fresh engine they are simply absent.

**The standing-alarm presentation ledger is boot's one local write (D-269).** It is a small, local,
gitignored, non-canonical marker at `.engine/boot/.cache/standing-alarms.json` (`boot_alarm_ledger`),
recording each surfaced standing alarm's structured condition and that it was shown in full, so the next
session can collapse an unchanged one. It is read and written by boot's own `SessionStart` hook, lives at
a stable per-instance path under the shared clone root (never an ephemeral worktree, so it spans separate
sessions on the one machine), shares **no code path** with memory's consolidation sweep, is never
committed, and is **fail-toward-full** (any loss or ambiguity renders the alarm in full). This refines
boot's read-only law to *read-only of canonical state* — it never regenerates derived or committed state;
its sole write is this presentation ledger.

**Beyond what this pack pushes, a session can reach the wiring map deliberately.** When a change needs an
impact check — what depends on a part, what checks or governs it — or when a part is unfamiliar, the
session can query the project's own map; when and how is `.engine/operations/knowledge-impact-check.md`.

**The un-stranding repair is operator-consented and lossless.** The assistant **waits for the operator to
say yes** — it never runs the repair un-asked. Only then does it run the un-stranding fix
(`checkout_health.unstrand`), the deployed-floor never-strand-main rule's one sanctioned write to the operator
checkout. It is **lossless-or-it-does-not-run**: it saves anything at risk — work that has drifted off the
branch, or unsaved changes — to a safe point first, then re-attaches the folder to its branch and restores the
missing engine files; if it cannot safely tell where to put the folder, it refuses rather than guess, and the
assistant says so. The assistant relays the plain-language result.

**A folder that has merely fallen behind is the same shape (#335).** When boot surfaces that the folder is on
its default branch but missing recent merged work, the assistant likewise **waits for the operator to say**
"bring it up to date", then runs the catch-up (`checkout_health.catch_up`). It is **lossless-or-it-does-not-run**
by construction: it brings the folder current only along a safe fast-forward, keeping any unsaved changes; if
unsaved work is in the way it changes nothing and reports that plainly (a `blocked` result), so nothing is lost.
The assistant relays the plain result — brought current, already up to date, or blocked-with-unsaved-work — and
never forces. (This signal is online-only and consequence-gated: ordinary small drift stays quiet, and a folder
parked on a *non-default* branch is a separate state, not yet handled — [issue #342](https://github.com/StarshipSuperjam/engine-template/issues/342).)

**A stranded pull request is the same shape (#136).** When boot surfaces a pull request that can't be merged,
the assistant likewise **waits for the operator's go-ahead**, then runs the reconcile (`pr_reconcile.reconcile`).
The reconcile first checks whether the clash is confined to the engine's two internal index files — the
knowledge graph and the self-map, the only files a clash on which is *spurious* (both sides are regenerations
of one source tree). If so, it reconciles the pull request against the latest default branch, regenerates those
two files from the reconciled tree, and keeps both pieces of work, **lossless-or-it-does-not-run**. If anything
but those two files clashed, it changes nothing, restores the branch exactly as it was, and routes the operator
to a plain-language decision rather than touching a real conflict. The assistant relays the plain result, and
never claims the merge is now guaranteed — a later change can still land first.
