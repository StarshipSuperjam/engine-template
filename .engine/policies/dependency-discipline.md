---
title: Dependency discipline
status: accepted
date: 2026-06-23
---

## Rule

Your project's dependencies — the outside code it pulls in to run — are held to three standing expectations:

- **Locked versions.** Every dependency is recorded at an exact version, so the project builds the same way every time instead of quietly picking up whatever is newest.
- **A review step on changes.** When a change adds or updates a dependency, it should be examined for known security problems and license conflicts (a dependency whose license clashes with your project's), and those surfaced to you in plain language — so a risky one is something you see and weigh, not something that slips in unnoticed.
- **Regular updates.** Dependencies are refreshed on a predictable schedule, so security fixes don't pile up unattended.

## Scope

This is about the dependencies your project declares and ships — the outside packages it needs to run — not the engine's own internal tooling, which is held separately. The review step concerns the moment a dependency is added or changed; the locked-versions and regular-updates expectations stand continuously. It does not govern which packages you choose to use: that is your project's call, and the engine's part is to keep whatever you choose locked, reviewed, and current.

## Rationale

Left alone, dependencies drift. The same project starts installing different code on different days, a known security fix can ship without anyone noticing, and a problem one machine sees never shows up on another. Locking versions makes every install identical. A review step means a dependency with a known vulnerability or an incompatible license is caught and shown to you before it reaches your project, not discovered afterward. Keeping dependencies current on a schedule stops small risks from accumulating into a large one. Together they let a non-engineer stay in control of what the project depends on, in plain sight.

## Enforcement-tier

- **Posture, with teeth now arriving.** These three expectations are standing rules the engine is trusted to follow and to surface honestly. Two of the checks that back them are now in place; the rest stays posture.
- **A gentle nudge when a version is left unlocked** — live now, and never blocking: it points out a missing lock file but never stops a merge.
- **A merge-blocking check when a change adds a dependency with a known security vulnerability** — live now. It blocks the merge and shows you the problem in plain language, with a next step. It has full effect where GitHub provides the data — free on public projects, and on private projects with GitHub's paid code-security feature; where that data isn't available the check says so plainly and explains the cost and benefit, never a silent pass. Checking a dependency's **licence** for conflicts is the part of this step still to come — it arrives in a later step of this module; until then, a license conflict is not checked here, and your protection for that is the pull request you review.
- **Regular updates stay posture** — the refresh schedule is the engine's always-on practice, not a check of this module.
- **Your backstop, always:** whatever a check does or doesn't yet cover, every dependency change still appears in the pull request you review and approve — the final human gate nothing merges without.
