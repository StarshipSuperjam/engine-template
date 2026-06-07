---
title: Update the engine — fetch the newer released version and install it, reviewably and reversibly
---

## Purpose

How the engine updates itself to a newer released version, with three promises that make the update safe to
trust: it is **reviewed** (the update arrives as a pull request you approve, never an in-place change), it is
**reversible** (undoing that pull request undoes the update), and it **degrades** (if the newer version can't
be reached, the engine simply stays on the one it has and keeps working). The engine is detached from where
it came from, so an update is not a normal pull — it is fetched from the engine's published releases, pinned
to one tagged version. The update replaces the engine's **own** code while keeping **your** settings and your
saved data; reshapes any saved data that the new version needs in a different form — **backing it up first**;
re-checks that everything still fits together; and opens the result for your review. Enter this runbook to
understand or perform an engine update. The tool is `tools/module_manager.py`. The update refuses cleanly,
changing nothing, whenever it cannot proceed — so it is safe to try.

## Steps

1. **See your current version.** `module_manager.py status` lists the installed modules and the version the
   engine is on, so it is clear what an update would move from.
2. **Update.** `module_manager.py upgrade` (optionally name a specific version) fetches the newer released
   version, replaces the engine's own files with it while **keeping your settings and saved data untouched**,
   turns shared-file settings on or off to match the new version, rebuilds the engine's tools, reshapes any
   saved data the new version needs in a new form, re-checks that everything fits together, and opens the
   change as a pull request. It is refused, in plain language, and **nothing is changed**, if the release
   can't be reached (the engine stays on its current version), if a needed change to saved data can't be
   backed up first (see Notes), or if a required module is missing from the release.
3. **Review and merge.** The update lands as a pull request with the engine's checks. Merging it is your
   approval; reverting it undoes the update. Until you merge, nothing about the running engine has changed.

## Done when

The engine is on the new version, your settings and saved data are preserved, and the update's pull request is
merged — or, if the update was refused, you have been told plainly why (the release couldn't be reached, a
needed saved-data change had no backup set up, or a module was missing), with nothing changed.

## Notes

**Saved data is backed up before it is changed — or the update stops.** Most updates only replace the
engine's code, which a reverted pull request restores on its own. When an update also needs to change saved
data, it makes a backup first, so the change can be undone. If no backup is set up yet, the update **refuses**
that step rather than risk data it can't restore — set up a backup, then update again. And if an update is
undone *after* it already changed saved data, the engine notices on its next start and tells you, in plain
language, the exact command to restore the backup so your data and the engine match again.

**Reviewed and reversible.** The update is never applied in place — it always arrives as a pull request behind
the same review gate as any other change, so you approve it and can undo it.

**Degrades, never pretends.** If the newer version can't be reached, the engine stays on the version it has
and keeps working; the update is simply not applied, and it says so.

**The required safety checks keep their names across versions**, so an update can never break the review gate
that protects the project.
