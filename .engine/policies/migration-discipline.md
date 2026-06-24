---
title: Migration discipline
status: accepted
date: 2026-06-23
---

## Rule

When your project changes the shape of its database — a *migration*, the script that adds a table, drops a
column, changes a type, or moves data — the engine holds three standing expectations:

- **Look before it runs.** A migration is read and understood before it is applied, never run blind.
- **Prefer changes that can be undone.** Favour additive, reversible steps over destructive ones — for
  example *expand-contract*: add the new column, copy the data across, and only remove the old one once
  nothing needs it, instead of dropping it in a single irreversible step.
- **Back up before anything destructive.** If a change would delete or overwrite data, the data is backed up
  first, so a mistake can be recovered.

And the expectation that matters most: a migration that is **destructive or cannot be undone** — dropping a
table or column, a type change that loses data, deleting rows — is treated as a moment to **stop and bring you
in**, surfaced for your decision rather than run on the engine's own judgement. In a session with you it stops
and asks; in an unattended run it halts that work and records it for you to see at the next start. You can
always approve it — it is a pause for your decision, never a veto.

## Scope

This governs **your project's own** database migrations — the schema and data changes your application ships,
in whatever tool you use (Rails/ActiveRecord, Django, Alembic, Prisma, Flyway, Liquibase, golang-migrate, or
plain SQL such as Supabase). It is **not** about the engine's own internal upgrades, which are handled
separately. The bar stands continuously, not only at one review step, and the engine never runs a migration
for your application — it reads, recognises, and routes.

## Rationale

A database migration is one of the few changes that can lose data for good. Drop the wrong column, or run a
destructive change without a backup, and there may be no way back — especially with tools like Supabase, where
migrations only ever go forward and there is no built-in "undo" to fall back on. That is exactly where a
non-engineer is most exposed: the change reads as routine, but its effect is permanent. So when the engine
sees a destructive migration, its job is to stop and hand you a plain-language account of what it would do,
the risk, and at least one safer way to reach the same result (back up first, or an expand-contract step) — so
the decision is yours, made with enough to judge it, not a blank "are you sure?".

This protection is only as good as the engine *recognising* the risk in the moment — there is no automatic
scanner watching your migrations. Its real backstop is the pull request you review and approve: nothing
reaches your main branch without passing through you.

## Enforcement-tier

- **Posture.** This is a standing habit the engine is trusted to follow and to surface honestly — not, today,
  an automatic block. There is **no mechanical detector** that decides whether a migration is destructive; the
  stop-and-ask is the engine recognising the risk itself and bringing anything hard to undo to you — the same
  standing habit it follows before any risky, hard-to-reverse step.
- **No check is live yet.** A gentle, never-blocking reminder about undo/rollback steps is planned for a later
  step of this module, for migration tools that have an undo concept; it will not apply to forward-only tools
  like Supabase, and when it lands the policy will say plainly what it does and does not catch. Until then,
  this is posture only.
- **Your backstop, always:** every migration still appears in the pull request you review and approve — the
  final human gate nothing merges without.
