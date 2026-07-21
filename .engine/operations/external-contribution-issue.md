---
title: File an issue on a project you contribute to — following that project's own conventions
---

## Purpose

How the engine opens an **issue** on a project you contribute to — an open-source project you've forked, or (the
special case) engine-template itself, which the engine-mechanic contributes to. GitHub only fills an issue's
title prefix (`Bug: `, `Feature: `, …) on its web "New issue" form; the engine files programmatically, which
skips that — so without this, a contributed issue lands with no prefix and doesn't match the project's own
conventions. This reads the **target project's** own issue templates and follows them, the same way the
pull-request path follows the project's PR template. **Opening the issue is always your call.** The tool is
`tools/external_contribution/contribute_issue.py`. **Heads-up:** the final file-the-issue step reaches the
network only on a real filing — see Notes.

## Steps

1. **Say what the issue is.** You give the engine a one-line summary and, when the project has more than one
   kind of issue, which kind it is (a bug, a feature request, and so on).
2. **The engine reads the project's own kinds.** It looks at the target project's issue templates and takes each
   one's heading — the prefix the project puts on that kind of issue. **If the kind you named doesn't match one
   of the project's kinds, the engine asks you which to use rather than guessing** — and if the project has no
   issue templates at all, it files a plain title (there's no convention to follow), and says so.
3. **Review the prepared issue.** The engine shows you the exact title — carrying that project's own prefix —
   and the body, assembled to the project's template when it has one. Nothing is filed yet.
4. **Authorize it — your call.** The engine files the issue **only on your go-ahead**; without it, the prepared
   issue just waits. Filing puts a public record on a project you're contributing to, so it's yours to approve.

## Done when

The engine reports the issue is **filed** and prints its link — or, if you haven't authorized it yet, that it is
**prepared and waiting** for your go-ahead — or, if the project couldn't be reached, that it is **drafted** (the
exact title and body) for you to file once it's reachable. In every case nothing is lost, and the engine never
guessed which kind of issue this is.

## Notes

- **The engine follows the project's conventions, it doesn't impose its own.** The title prefix and the body
  shape come from the **target project's** issue templates — not the engine's own issue format (which is for the
  engine's own housekeeping items in your repo, and would read wrong on a contribution to someone else's
  project).
- **It never guesses the kind.** A project usually has several kinds of issue, each with its own heading. When
  what you asked for doesn't clearly match one, the engine lists the project's kinds and asks — it never picks
  one for you.
- **The engine only proposes.** It files an issue and nothing more — it never changes the project's settings and
  never acts as its maintainer.
- **If the project is unreachable,** nothing is lost: the engine drafts the exact title and body so it can be
  filed once the project is reachable (or you can open it yourself with your own `gh`), and best-effort notes the
  stalled contribution in your own repo so it isn't forgotten.
- **The live step runs when you file.** Every part of this except the final file-the-issue step is tested offline;
  the live `gh issue create` runs the first time you file a real one, the way any released feature's live path runs
  the first time it's used. Treat your first contribution as the shake-out of that last step.
