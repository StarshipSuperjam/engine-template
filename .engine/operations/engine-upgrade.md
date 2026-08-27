---
title: Update the engine — what an update promises, what it refuses, and how to undo one
---

## Purpose

What an engine update means for you, and the judgment around it that no command can carry: the three
promises it makes, the refusals that are honest stops rather than failures, and how to get back if one goes
wrong or you change your mind. The mechanics are the transaction's —
`transaction.py plan engine-upgrade` shows exactly what an update would change, and asking changes nothing.

The three promises. An update is **reviewed** — it arrives as a pull request you approve, never an in-place
change. It is **reversible** — undoing that pull request undoes the update. And it **degrades** — if the
newer version cannot be reached, the engine stays on the one it has and keeps working.

## Steps

1. **See what an update would change.** `/engine-upgrade`, or `transaction.py plan engine-upgrade`. Either
   only checks: the version you are on, whether a newer one exists, whether a previous update looks
   unfinished, and what the update would change — the files, the settings, any stored-data change and
   whether a backup is set up for it, any capability it retires, and any new capability it brings in.
2. **Apply it yourself.** `/engine-upgrade` is a command you type; the engine cannot start it for you. That
   limit is real, and it is worth knowing exactly how far it goes — see the note below. Applying takes the
   consent handle from the plan you were shown, so what runs is what you read; if the world moved in
   between, it refuses and hands you a fresh plan rather than applying your consent to a different change.
3. **Review and merge.** The update lands as a pull request with the engine's checks. Merging it is your
   approval; reverting it undoes the update. Until you merge, nothing about the running engine has changed.

## Done when

The engine is on the new version with your settings and saved data preserved and the update's pull request
merged — or the update refused, and you were told plainly which refusal it was, with nothing changed.

## Notes

**How far the typed-command limit actually goes.** Three layers, named honestly, because a limit you
misjudge is worse than one you know the shape of. The `/engine-upgrade` command **cannot be started by the
engine on its own** — that is enforced by the harness, not by an instruction. Beneath it,
`module_manager.py upgrade --confirm` is an ordinary command with no such lock, so the layer that matters
there is different: applying takes the consent handle from a plan you were shown, so an update cannot
quietly become a different update. And under both, applying only ever opens a pull request — nothing about
the running engine changes until you merge it. So even if the middle layer were slipped, the worst outcome
is a pull request you can reject.

**The refusals, and why each is a stop rather than a failure.** An update refuses, changing nothing, when:
no update home is recorded (the engine asks you to record one rather than guessing); the home has no such
release, or was renamed or removed (it names the home so you can check); the network cannot be reached (it
stays on the current version); a needed change to saved data cannot be backed up first; a module you have
has vanished from the release without being recorded as an intentional removal (a broken release); or your
engine is below the release's **clean-upgrade floor** — the oldest version proven to update to it in one
clean pass — where it names both versions and says plainly to stay put. Each of those is the engine
declining to guess, which is what makes an update safe to try.

**Saved data is backed up before it changes, or the update stops.** Most updates only replace the engine's
code, which a reverted pull request restores on its own. When one also needs to change saved data, it makes
a backup first so the change can be undone; with no backup set up it refuses that step rather than risk
data it cannot restore. If an update is undone *after* it changed saved data, the engine notices at the
next start and gives you the exact command to restore the backup.

**If an update stops half-applied.** The working copy is changed but nothing was opened for review or
merged — safe either way, and you have two clean choices. **Finish it**: run the apply again; the second
run uses the just-installed version's own logic to complete the stalled step. **Undo it**:
`module_manager.py rollback --confirm` puts the engine back, saving a recovery point of your current state
first, refusing if you have unrelated unsaved work of your own, and putting back any saved memory the update
changed. A bare check reports a half-finished tree as *unfinished* rather than "up to date", so you can
always tell where you stand.

**Undoing an update you have already merged.** This cannot be undone locally — the engine never rewrites
your main line. Its pull request is reverted instead, as a normal reviewed change you merge. Once the code
is back, the memory from before the update is put back too; that last step needs your backup reachable, and
if it is not, your memory is left unchanged and the engine offers again later. Your code is safely back
either way.

**What an update replaces, and what it keeps.** It refreshes the engine's **own** files — its tools, checks,
and the templates that shape your pull request and issue descriptions — while keeping **your** settings and
saved data untouched. So an engine template you edited yourself is replaced with the new version's wording;
you can see and undo that in the update's pull request, like any other change.

**Where updates come from.** Your engine is detached from the repository it was created from, so updates are
fetched from its **update home** — the repository whose published releases it updates from, recorded once in
your engine's own record. Because that home decides where your engine's code comes from, changing it to a
different home is treated as a change to a safety setting: it is flagged at review and takes your deliberate
acknowledgment, like any change that could weaken a safety gate. An engine with no home recorded says so at
session start and offers to record one; updates simply wait until it is set.

**New capabilities a release adds** are never silently left off and never resurrected against your wishes. A
**required** capability is installed automatically — the version needs it, and if it cannot be installed the
whole update refuses rather than open a broken pull request. A **default** add-on new to your deployment is
turned on unless you decline it before merging. An **optional** one is only offered. What an update never
does is turn back on a module you deliberately declined or removed: that is treated as declined and only
offered again. Each is disclosed in the preview and the pull request, so you weigh it at the merge.

**The required safety checks keep their names across versions**, so an update can never break the review
gate that protects the project.
