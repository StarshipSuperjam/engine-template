---
title: Add the engine to an existing project — fetch it, check for overlaps, and set it up reviewably
---

## Purpose

How the engine joins a project that **already has its own files** — there is no "Use this template" step to
start from. The engine is fetched from its published releases at one **pinned tag** (a fixed version, never a
moving branch — the supply-chain control), placed in its own namespaced corners alongside the project, checked
for overlaps with what is already there, and set up by the same first-run setup the engine uses everywhere.
Nothing reaches the main branch except through a pull request the owner approves. You run the engine's tools
from the **fetched release**, never a local copy — the first-run tool retires itself once setup is done, so a
further project means fetching a fresh release again. Enter this runbook to understand how an existing project
takes the engine on, where the owner's decisions fall, and what happens after the merge.

Before starting: the project is on GitHub and you are signed in (`gh auth status` — the engine is fetched with
your `gh`); you are on a clean working branch, not main and with nothing uncommitted, so the arrival is a
reviewable set rather than an in-place edit; and you know which pinned release tag to install (default: the
latest). The arrival is a typed lifecycle transaction. Its read-only preview is
`instantiator.py arrive --target <project>` **without** `--accept-all`: it surfaces every overlap and changes
nothing. You then apply it from the extracted release with `instantiator.py arrive --target <project>
--accept-all …`; the arrival lands as a pull request the owner merges, and that merge is the consent. The
post-merge `finalize` is named as a follow-up but never precomputed.

## Steps

The arrival is **read-only until you accept it.** Run without `--accept-all` and it changes nothing — it only
reports, in plain language, each place the engine and the project would overlap: what the engine would do and
what the owner keeps or loses. Any Python 3.9 or newer runs this (macOS system `python3` is fine — the engine
builds its own newer runtime from there); an older interpreter stops before touching the project and prints the
command to re-run with.

Each overlap is the **owner's decision — accept it, leave it as is, or stop.** If the owner keeps something the
engine would place, settle that first (move their file, or choose the team or solo setup); if the check found no
overlaps there is nothing to settle. The reviewer tier and the add-ons are the owner's choices too: the tier is
`--tier team` or `--tier solo` (a team identity also takes `--handle <account>`), and add-ons are named on
(`--keep`) or off (`--decline`) — naming the same one in both is refused before any write, and a declined add-on
can be added later through [module-add](module-add.md), an install rather than a toggle. An existing review team
is noticed and the team setup recommended.

**Accepting** (`--accept-all`, with the reviewer and add-on choices) places the engine's files alongside the
project: its working-guide floor is inserted into **both** CLAUDE.md and AGENTS.md keeping the owner's content;
where it fetches its own updates is recorded; a `SECURITY.md` is seeded only if the project has none; the README
and LICENSE are left exactly as they are; the reviewer is set; and the whole arrival opens as **one pull
request.** The main branch is protected here **when the owner's sign-in can administer the repository** — a
reviewed pull request required, no force-push, no deletion; if it cannot, setup says so plainly and protection
is turned on later instead. Either way — admin or not — the engine's **own** required checks are always bound
*after* the merge by the one-time `finalize`, never at arrival (their workflows aren't on the branch yet; see
the Notes). An overlap left unaccepted stops the run with nothing changed.

**Merging is the owner's consent.** Until the pull request merges, none of the engine's files are on main
(branch protection is a GitHub setting, not something the pull request carries), and reverting it removes the
engine again. After the merge the engine's **first act is the onboarding read** — in Explore mode it reads the
project and saves a durable understanding to memory, so later sessions start grounded rather than cold, then
hands off to the first build. This is a read of the project, not a change to it; follow the onboarding-read
operation.

## Done when

The engine's files are in place alongside the project's, every overlap was surfaced and settled by the owner's
choice, setup ran, and the arrival is open as a pull request the owner can approve — or the arrival stopped
cleanly at an overlap the owner chose to keep, with nothing changed. After the merge, the engine's required
checks were turned on with the one-time `finalize` step and the engine has run the onboarding read, so it starts
grounded on the project it joined.

## Notes

**Surfaced, never silent.** Every overlap is shown before anything changes; the engine never overwrites a shared
file without the owner's choice — on the project's CLAUDE.md it adds only its own marked block and keeps the
rest — and the later consistency check does not re-flag the project's own files, because the overlap check is
the single place overlaps are reported. The project's front page and license stay the project's: no README or
LICENSE is seeded, and an existing `SECURITY.md` (in the root, `.github/`, or `docs/`) is surfaced and kept, not
replaced.

**Branch protection is added to, never replaced.** If the project already protects main with its own rule, the
engine adds its checks to that rule in place — and adds any missing force-push, deletion, or pull-request
protection — rather than creating a second rule, leaving everything else of the rule as it was. Anything it
cannot add without changing a setting the owner chose is reported, not overwritten. The exact additions are
recorded across both the arrival and `finalize`, so a later clean removal takes back exactly what was added. If
more than one rule covers main, the engine adds its own alongside and says so.

**The checks come on in two steps, after the merge.** A required check can only report once its workflow is on
the branch, and the engine's workflows (`engine-ci`, `engine-guard`) arrive *inside* the arrival pull request —
requiring them at arrival would make that very pull request impossible to merge. So the arrival protects the
branch but leaves its own checks un-required, and the one-time `bootstrap.py finalize` turns them on after the
merge: it confirms the workflows are on the branch first (refusing rather than deadlock), and is safe to re-run.
Between the merge and `finalize` the branch is protected (a pull request required, no force-push, no deletion)
but the engine's checks are not yet required — run it promptly; boot keeps reminding until you do. **If a
project reached a stuck state under an older engine** — main already *requires* `engine-ci`/`engine-guard` while
the pull request that adds their workflows cannot merge — clear it by hand once: remove the required-checks rule
(the branch-protection settings, or the engine's de-bootstrap "keep" path), merge the stuck pull request, then
run `finalize`.

**On an older Python, the Codex config wire may defer.** Arrival runs its setup on the system Python (3.9 or
newer). One optional step — registering the engine's Codex helper server in an existing, non-empty
`.codex/config.toml` — needs Python 3.11+ to validate that file before editing it. On 3.9 with a non-empty Codex
config present, the engine leaves the file untouched, says so, and pauses so you finish it under the engine's
own 3.11 runtime (or add the block by hand) rather than risk corrupting the config; a project with no Codex
config, or an empty one, is set up cleanly on 3.9. One consequence to know: a Codex block written this way on a
3.9-only machine also cannot be *removed* by the engine on that machine (the same validation gap) until a 3.11
runtime is available — remove the marked block by hand if needed.

**Consented, reversible, re-enterable.** The arrival lands as a pull request you merge, so it lands only with
your consent and is undone by reverting it; if it stops at an overlap, running it again picks up from the
overlap step, nothing shared having changed.
