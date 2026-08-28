---
title: Add an engine module — what it costs you, and what it never touches
---

## Purpose

What adding one capability to this engine means, and what stays true no matter which module it is. The
mechanics belong to the transaction: `transaction.py plan module-add <module>` shows exactly what it would
do, and `run` applies what you saw. This runbook carries the part a command cannot tell you — when to offer
a module at all, what an add is safe to try against, and where its one boundary sits.

## Steps

1. **Ask the tool what it would do, and read it out.** `transaction.py plan module-add <module>` names the
   capability, the release it comes from, and the files it would add. Nothing is changed by asking.
2. **Apply what was shown.** `transaction.py run module-add <module> --consent-handle <handle>` installs it
   and commits exactly those files as one labelled commit. If the world moved since the plan, it refuses and
   hands back a fresh one rather than applying consent given to a different change.
3. **Trust the refusals.** Every refusal names what is wrong and what clears it — already installed, a
   fetch that does not match, a missing companion module, or an environment that cannot apply one of the
   module's settings. Nothing is changed on any of those paths, so an add is safe to try.

## Done when

The capability is available, its files are one revertable commit on the current branch, and the installed
set is reported consistent — or the add was refused, plainly, with nothing changed.

## Notes

**This is not gated by review, deliberately.** Adding a module is the engine changing its own installation,
not your product code, so it takes effect immediately rather than waiting on a pull request. That is what
lets a capability be offered, accepted and used inside one conversation. What it does not do is leave the
change loose in your checkout: it lands as one labelled commit carrying exactly the module's own files, so
reverting that commit undoes it and nothing of yours is swept in.

**Adding a module touches no branch-protection setting.** A module's checks flow in and out of the engine's
stable required check by which check files are present, so adding one changes only what runs inside that
check, never the check itself — no operator-privileged step is involved. Updating or removing the whole
engine are different capabilities with their own runbooks.

**Where the files come from.** From the engine's current released version, pinned to that exact version —
never an in-progress copy — so an add installs files matching the engine this repository already runs. An
unreachable release is reported plainly and changes nothing.

**Re-adding a module you declined at first run** is this same path: its files were deleted then and are
fetched again now. It is an install, not a toggle.

**When to offer a module — the offer, never a silent install.** Most adds start with the operator asking.
The engine may also *offer* one: when a request maps to a capability that an installed module does not
provide but an uninstalled one would, say what that module turns on and let the operator decide. Their yes
is what installs it, exactly as if they had asked. The threshold is judgment, kept plain so it guides
without becoming brittle: offer when the request **clearly** maps to what an uninstalled module is built
for — a direct ask for its capability, or the same need arising more than once — never on a faint keyword
brush, which would turn every mention into a prompt to install. When unsure, name the capability and ask
rather than either installing or staying silent. Check what is already installed before offering.
