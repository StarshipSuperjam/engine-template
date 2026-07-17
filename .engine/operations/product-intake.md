---
title: Describe, settle, and hand off a product spec — the engine-design intake
---

## Purpose

How the engine helps the operator turn "here is what I want to build" into a written, checked, settled
description of the product, kept as plain files under `docs/spec/`. Enter this when the operator runs
`engine-design`, or asks to describe, plan, or write up what they want built. The end state is a description
the operator has read and accepted, with every part present and well-formed — the ground the build works
from — and, when the operator is ready, the tracked build work that follows from it. It writes only the
operator's own project files, and it never decides whether the design is *right* — that stays the operator's call.

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
   you can capture just enough to get moving, or take the time now to write each piece down more fully so there
   are fewer surprises later. For a product meant to last, the fuller path can also write up the guiding
   principles behind it, an overview of how it fits together (with a simple diagram), and guides for the people
   who will use it. **Most projects start light; default to that unless the operator asks for more.** Be plain
   that these fuller write-ups are authored *for* the operator but are theirs to get right: the engine drafts
   them from a starting shape, but — unlike the description under `docs/spec/` — it does not check them, so
   never imply that it did.
4. **Write the description from the scaffold.** Author the documents into `docs/spec/`, starting from the
   templates in `.engine/modules/product-design/scaffold/`: a master index at `docs/spec/index.md` that lists
   every capability, its stage, and a link to its document, plus one document per capability. Fill in the real
   content and strip the templates' own guidance as you go — the bracketed placeholders and the comment blocks
   are written for you, not the operator, and must not survive into the operator's files. Write each document's
   acceptance criteria as a table — each row is what must be true, how it is checked, and who checks it (the
   operator themselves, or the engine). Mark each document's stage in its frontmatter. When the operator chose
   the fuller path in step 3, also author the deeper documents from their starting shapes in the same
   `scaffold/` folder — the guiding principles (`principles.md` → `docs/principles.md`), an architecture
   overview with a simple diagram (`architecture.md` → `docs/architecture.md`), and the user guides
   (`diataxis-*.md` → `docs/tutorials|how-to|reference|explanation/`) — writing only the ones this product
   actually needs and stripping the guidance as you go. These are the operator's own documents to get right: the
   engine drafts them from the starting shape but does not check them, so never imply that it did.
5. **Check it, and report what was — and was not — checked, and how "done" will be judged.** Run the form
   check: `uv run --directory .engine --frozen -- python tools/validate.py --check engine/check/product-spec-form`.
   Tell the operator plainly what it found, and state the bound: it checked that every part is present and
   well-formed; it did **not** check that the design is *right* — that is their call. Fix anything flagged and
   re-run until it is clean. Then, for each described capability, show the operator how its acceptance criteria
   split between what they can confirm themselves and what rests on the engine's account — run
   `uv run --directory .engine --frozen -- python tools/spec_referent.py acceptance-split --doc docs/spec/<capability>.md`
   and read its plain-language count back. Do this **before** they settle the description (step 7), while they
   can still add a check they would rather run with their own eyes; and never fold the two into one "it all
   checks out" — seeing the split up front is how they judge, honestly, how much of "done" they can verify
   themselves and how much rests on the engine's account.
6. **Offer a deeper, advisory review before they settle — when it is available.** If the engine's optional
   design reviews are installed, offer the operator the same four independent reviews it runs on a plan before
   building, now reading the **description itself**: whether it is the right thing, whether it is sound, whether
   it can be built, and whether it is safe. They **only advise** — nothing they raise blocks the document, and
   the operator's own go-ahead is still what settles it. This is the **second** place those four reviews are
   offered (the first is before a build). When they are not installed, the form check above plus the operator's
   own read is the bar — say that plainly, never imply a review ran.
7. **Record the go-ahead and mark it settled.** When the operator is satisfied with a document, record their
   acceptance and mark that document settled in its frontmatter. A settled document is the ground the build
   adapts to; the engine never settles one on its own initiative.
8. **Tell them where it lives, and how a settled document is protected.** Point the operator to `docs/spec/` as
   the home of their description, and say plainly that a settled document is not frozen forever — it can be
   changed or reopened later, but not quietly: when a pull request changes a settled document, the engine asks
   the operator to confirm the change on that pull request (by applying the `guardrail-ack` label) before it can
   merge, so the record always shows the change was deliberate, never a silent edit.
9. **Record the significant choices — what was decided, and what was ruled out.** A few moments call for a short
   record: **settling a choice where you weighed real alternatives**, **reopening something already settled**
   (the reopen in step 8), and **adding or dropping a whole capability** — but never the routine first layout in
   step 2, when nothing has been rejected yet. Author it under `docs/adr/` from the starting shape in
   `.engine/modules/product-design/scaffold/adr.md`, numbered in the project's own sequence (0001, 0002, …,
   unrelated to any numbering the engine uses for its own machinery), and fill in what was decided, why, and —
   the part that matters most for a later session — the alternatives weighed and turned down, and why each lost:
   that ruled-out part is what stops a future session from re-opening ground already walked. Strip the guidance
   as you write, as with the other documents. The engine checks that each record it wrote still names what was
   ruled out (present, with something in it), never whether the reasons are good — the operator's call; a record
   kept in some other style is left untouched. This is distinct from the architecture overview's short "key
   decisions" note — that is an at-a-glance summary inside one document; these are the per-decision records.
10. **Turn the settled description into tracked build work.** When a description — or a newly-settled part of
   it — is settled, offer to turn it into work a build can pick up. Keep two moments distinct. *Now:* the engine
   writes a **build order** at `docs/spec/build-plan.md` — the settled capabilities grouped into ordered,
   plainly-named phases (e.g. "Foundation", "Core flows") — and opens a **list of things to build**, one tracked
   item per capability, each **linking to its description** (the document under `docs/spec/`) and naming the
   parts it must satisfy — the link is what lets a later build check the work against that description. *Later:*
   when a build
   actually runs, that order is what groups the work into **visible phases you can watch progress against** — the
   phases are not created just by settling. Say the consequence plainly: once a build order exists, settled work
   left out of it will hold a merge until it is added, so nothing settled is quietly dropped. And reassure: the
   build order is a committed file, written even with no GitHub connection — only the tracked items wait for the
   connection; re-running is safe, updating the order and adding only items not already tracked. Picking "build
   this" on an item is the deliberate act that starts a build.

## Done when

`docs/spec/` holds a master index and one document per capability, the form check
(`engine/check/product-spec-form`) reports no problems, and the operator has seen the result: its bound stated,
and for each capability the two-tier split of how "done" is judged — what they can confirm themselves versus
what rests on the engine's account. They have given their go-ahead on every document they consider settled. When the operator has chosen to hand
the settled work to a build, a build order at `docs/spec/build-plan.md` groups it into phases and there is a
tracked item to build for each settled capability.

## Notes

- **Degrade, do not dead-end.** Almost everything lands as committed files, so a missing or unreachable GitHub
  connection defers only the tracked items in the last step — the description, its checks, the settled state, and
  the build order are all written from files. Tell the operator what is waiting on the connection rather than
  stopping.
- **Plain words only, on screen.** The operator never sees the short stage markers the files carry or any
  internal engine vocabulary — only plain renders like "not yet described / in progress / settled", "build
  order", "phases", and "things to build". The same holds for how you report what the check found.
