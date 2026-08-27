---
title: Remove the engine — the choice you make, and the gap while it happens
---

## Purpose

How the engine removes **itself**, leaving a project that still works without it. Two things about this are
judgment rather than mechanics, and this runbook exists for them: the choice you own about your main
branch's safety rule, and the window between the protection change and your merge, when the branch is in a
state neither the old world nor the new one. Everything else is the transaction's:
`transaction.py plan engine-remove --keep-protection|--remove-protection` shows exactly what would happen.

## Steps

1. **Choose what happens to your main-branch safety rule.** The engine set up a rule requiring a pull
   request and passing checks before anything reaches your main branch, and removal takes the engine's
   checks out of it. Decide whether to **keep** the rule (protected, minus the engine's checks) or
   **remove** it entirely. Keep it unless you are sure you want it gone. The engine never removes that
   protection without you choosing.
2. **Start it yourself.** `module_manager.py remove-engine --confirm` with your choice. This one is not
   something the engine will start for you: an update can be rolled back, but an engine that has removed
   itself is a harder recovery, so beginning it stays your deliberate act. Running the plan first changes
   nothing.
3. **Review and merge.** The deletions arrive as a pull request. Until you merge it, the engine's files are
   still present. Merging is your approval; reverting brings them back.

## Done when

The engine's files are gone, your shared setup files keep only your own entries, your safety rule reflects
the choice you made, and the removal pull request is merged — or the removal could not start and you have
been told plainly why, with nothing changed.

## Notes

**The protection change is not in the pull request, and it happens first.** A branch rule is a repository
setting; no pull request can carry one. The engine takes its checks off the rule **before** opening the
deletion pull request — it has to, because a required check whose files are being deleted would block that
very pull request forever. So from that moment until you finish: if you chose to **keep** the rule, your
main branch is still protected but without the engine's checks; if you chose to **remove** it, your main
branch is no longer protected. This is the one part of removal your merge does not gate, which is why it is
stated here rather than left to be inferred from a diff that does not show it.

**Reverting the removal does not turn the safety rule back on.** Reverting the pull request brings the files
back; re-creating the rule means running the engine's setup again.

**When the safety rule was yours, not the engine's.** If the engine arrived on a repository that already
protected its main branch and added its checks to *your* rule rather than creating its own, removal takes
back exactly what it added — its checks, and any force-push, deletion or pull-request protection it had to
add — and leaves the rest of your rule as it was. There is no keep-or-remove choice in that case: the rule
is yours, and the engine never deletes a rule it did not make.

**Shared things the engine leaves alone.** In your shared setup files the engine removes only its own
entries. Where something might also be yours — a permission you granted and may still want — it is left and
named for you, so you can remove it by hand if you no longer need it. The cost of never wrongly removing
something of yours is that a little may be left behind, always disclosed.
