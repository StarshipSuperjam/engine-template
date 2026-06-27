---
title: Describe and settle a product spec — the engine-design intake
---

## Purpose

How the engine helps the operator turn "here is what I want to build" into a written, checked, settled
description of the product, kept as plain files under `docs/spec/`. Enter this when the operator runs
`engine-design`, or asks to describe, plan, or write up what they want built. The end state is a description
the operator has read and accepted, with every part present and well-formed — the ground the build works
from. It writes only the operator's own project files, and it never decides whether the design is *right* —
that stays the operator's call.

## Steps

Run these with the operator, one at a time, in plain language. The stage words below — a piece that is *not
yet described*, a document *in progress*, or one that is *settled* — are how you speak to the operator; the
short markers the files carry in their frontmatter stay in the files, never on screen.

1. **Check the GitHub connection, and never dead-end.** Run a quick check (for example `gh repo view`). If it
   fails, say so plainly, name the one next action (usually `gh auth login`), and carry on regardless: capture
   what the operator has already told you as a committed file under `docs/spec/` so nothing they said is lost.
   The whole intake works from committed files; the GitHub connection is only needed later, to turn the
   description into tracked work.
2. **Lay out the pieces and confirm the shape — before writing any one document.** From what the operator
   wants, propose the full set of capabilities the product obviously needs, each as a named, not-yet-described
   piece. Ask at the shape level: "does this look like the right pieces, or is something obvious missing?" —
   and let them say yes, add one, or ask you to decide. Settle the shape first, so each piece is written with
   the others in mind rather than as an island.
3. **Agree how much to capture now — and name the trade.** Offer the depth as a consequence, not a setting:
   you can capture just enough to get moving, or take the time now to think each piece through more fully so
   there are fewer surprises later. Most projects start light; default to that unless the operator asks for
   more.
4. **Write the description from the scaffold.** Author the documents into `docs/spec/`, starting from the
   templates in `.engine/modules/product-design/scaffold/`: a master index at `docs/spec/index.md` that lists
   every capability, its stage, and a link to its document, plus one document per capability. Fill in the real
   content and strip the templates' own guidance as you go — the bracketed placeholders and the comment blocks
   are written for you, not the operator, and must not survive into the operator's files. Write each document's
   acceptance criteria as a table — each row is what must be true, how it is checked, and who checks it (the
   operator themselves, or the engine). Mark each document's stage in its frontmatter.
5. **Check it, and report what was — and was not — checked.** Run the form check:
   `uv run --directory .engine --frozen -- python tools/validate.py --check engine/check/product-spec-form`.
   Tell the operator plainly what it found, and state the bound: it checked that every part is present and
   well-formed; it did **not** check that the design is *right* — that is their call. Fix anything flagged and
   re-run until it is clean.
6. **Record the go-ahead and mark it settled.** When the operator is satisfied with a document, record their
   acceptance and mark that document settled in its frontmatter. A settled document is the ground the build
   adapts to; the engine never settles one on its own initiative.
7. **Tell them where it lives, and be honest about the edge.** Point the operator to `docs/spec/` as the home
   of their description, and say plainly that a settled document can be reopened later — it just takes a
   deliberate, recorded change, never a quiet edit. Then name what this does not do yet: turning a settled
   description into a tracked list of things to build, and keeping a settled one from drifting without the
   operator's re-approval, come in later steps. Until then a settled description rests on this recorded
   agreement, not on an automatic guard — say so, so the operator is not left thinking it is enforced for them.

## Done when

`docs/spec/` holds a master index and one document per capability, the form check
(`engine/check/product-spec-form`) reports no problems, and the operator has seen the result with its bound
stated and given their go-ahead on every document they consider settled.

## Notes

- **Degrade, do not dead-end.** Every step lands as committed files, so a missing or unreachable GitHub
  connection only defers the later turning-into-tracked-work step; the description itself is written, checked,
  and settled entirely from files. Tell the operator what is deferred rather than stopping.
- **Plain words only, on screen.** The operator never sees the short stage markers the files carry or any
  internal engine vocabulary — only plain renders like "not yet described / in progress / settled". The same
  holds for how you report what the check found.
- **Deferred to later slices (engine-facing):** the lock-integrity re-acceptance check, and the decomposition
  of a settled spec into a build-plan, ordinary work issues, and milestones, are not built yet; this runbook
  stops at a settled, checked spec.
