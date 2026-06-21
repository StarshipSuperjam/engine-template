<!-- The operator-facing copy for the control-plane bootstrap (the tool `tools/bootstrap.py` reads these
sections by heading and renders them; built-in fallbacks keep it working if this file is ever missing).
This is the single review surface for what the operator is told when the safety gate is turned on or can't
be. Plain language only — keep the engine's internal machinery out of what the operator reads: name each thing
by what it does for them, not by its engine/maintainer term. That is a relevance judgment made in the writing
and the review, never a banned-word list (none is kept here or anywhere). The one literal the operator will
see on GitHub's screen (`repo`) is pre-translated here before it appears. Edit the wording here; the section
HEADINGS are stable keys the tool matches, so don't rename them. -->

## Before you approve

I'm about to turn on your safety gate — the branch protection that keeps work from reaching your main
branch without passing checks and your review. To do that I need permission to manage this repository's
settings. GitHub will show an authorization screen asking for `repo` access — the standard "manage my
repository" permission. Approving it lets me set the protection rules; I can't grant it to myself, which is
the point. Nothing changes until you approve.

## When it's on

Your safety gate is on. The main branch now requires a pull request, passing checks, and resolved review
comments before anything merges — and it can't be force-pushed or deleted.

## When it was already on

Your safety gate is already on — nothing to change. (Safe to run any time; it never weakens protection
that's already in place.)

## When it couldn't be confirmed

I set up branch protection but couldn't read back to confirm it actually took (GitHub didn't answer just
now). Don't assume it's on — check your repository's branch settings, or run this again in a moment.

## If it couldn't turn on — you don't administer this repository

I couldn't turn on branch protection — this account doesn't administer the repository. Protection is not
active, so work can merge unreviewed. Next step: ask whoever owns the repository to run this setup and
approve the screen. I'll keep reminding you until it's on.

## If it couldn't turn on — your organization blocks the permission

I couldn't turn on branch protection — your organization's settings blocked the permission it needs.
Protection is not active, so work can merge unreviewed. Two ways forward: ask your org admin to allow it, or
switch to team mode (a separate engine identity that holds this permission). I'll keep reminding you until
it's on.

## If it couldn't turn on — the approval didn't save

The authorization screen completed but the permission didn't save (some sign-in methods do this).
Protection is still off, so work can merge unreviewed. Let's try once more, or sign in again first. I'll
keep reminding you until it's on.

## Removing the engine — keep or remove your safety rule

I set up a safety rule on your main branch that requires checks to pass and a pull request before anything
merges. Removing the engine takes my checks out of that rule. I can keep the rule — your main branch stays
protected, just without my checks — or remove it entirely. Keep it unless you're sure you want it gone; I'll
never remove protection without you choosing.

## When the safety rule is kept

I took my checks out of your main-branch safety rule and kept the rule itself, so your main branch still
requires a pull request and can't be force-pushed or deleted.

## When the safety rule is removed

I removed the main-branch safety rule entirely — the one I had set up. Your main branch is no longer
protected. To turn protection back on later, run the engine setup again.

## When there was no engine safety rule

There was no engine safety rule on your main branch to remove — nothing to change here.
