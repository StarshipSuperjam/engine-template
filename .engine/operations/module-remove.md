---
title: Remove an engine module — what you lose, and what is deliberately left behind
---

## Purpose

What removing one capability means, and the two things about it that are judgment rather than mechanics:
the residue removal will not touch, and the notice a released engine owes downstream operators. The
transaction carries the rest — `transaction.py plan module-remove <module>` reports whether it would be
refused and why, before anything changes.

## Steps

1. **Ask the tool what it would do.** `transaction.py plan module-remove <module>` names what the operator
   would lose and whether the removal is refused — another installed module still needs this one, or it is
   foundational and cannot be removed on its own. Nothing is changed by asking.
2. **Apply what was shown.** `transaction.py run module-remove <module> --consent-handle <handle>` reverses
   the module's shared-file settings, deletes its files, drops it from the engine's record, updates the
   tool-runtime's dependency groups, and commits exactly that as one labelled commit.
3. **Read what it left in place, and disclose it.** Anything the removal could not prove was the engine's
   alone is left and named — see the residue note below. Pass this on to the operator; it is the one part of
   the result they may need to act on by hand.

**If this engine publishes releases**, author the removal notice at removal time (`--removal-notice "…what
an operator could ask for before and no longer can…"`). A local operator uninstalling in their own
deployment omits it — no release is cut there.

## Done when

The capability is gone, its files are one revertable commit, the remaining set is reported consistent, and
any residue the removal deliberately left has been named to the operator — or the removal was refused,
plainly, with nothing changed.

## Notes

**Not gated by review, deliberately.** Like adding one, removing a module is the engine changing its own
installation rather than your product code, so it takes effect immediately and lands as one labelled commit
you can revert.

**Ordinary removal touches no branch-protection setting.** A module's checks flow in and out of the engine's
stable required check by which check files are present, so removing one changes only what runs inside that
check, not its name. Removing the *whole engine* is different — it must also unbind the engine's required
check, an operator-privileged step, so a leftover binding to a deleted check cannot deadlock the
repository's own pull requests. That, and adding a module back, are separate capabilities with their own
runbooks.

**The honest residue.** A bare permission a module added cannot be proven to belong to the engine alone, so
removal leaves it rather than risk removing one the operator wanted. That is the accepted cost of never
removing the wrong thing, and it is always disclosed, never silently left.

**Dropping a module from the product needs a removal notice.** When a *release* drops a whole module, a
downstream engine that still has it reconciles it away on update and shows its operator, in plain language,
what they can no longer ask for. That line lives in the release's own record (`engine.json`
`removed_capabilities`); `--removal-notice` is how it is authored at removal time. Forget it and the release
cut refuses until it is added by hand — the module is gone by then, so that is an edit, not another removal.
This is the whole-module sibling of a module's own in-place retirement notice.
