# Your project runs on an Engine — start here

This project is managed by an **Engine**: a system that holds the project's state, memory, decisions, and
guardrails so every session starts grounded instead of from scratch. New here? Read
**[Getting started with your Engine](.engine/docs/getting-started.md)** — it explains, in plain language, what
the Engine is and how you direct it.

**Where this project's memory lives.** The project's real record — its state, its decisions, and its memory —
lives in the Engine (under `.engine/` and the Engine's own memory tools). Treat that as the source of truth and
consult it before asserting anything about where the project stands. Claude Code's built-in memory is **not**
this project's record and must never be cited as fact about the project.

**The Engine keeps to its own corners.** The Engine's files live in `.engine/`, `.claude/`, and the Engine's own
files under `.github/`; everything else at the root belongs to the project. Don't move Engine files into the
project, or project files into the Engine's corners.

**How to tell the Engine actually started.** When the Engine starts up healthy, the first thing you see is a
card titled **Project status** — those exact words, at the top. If you do **not** see a **Project status** card
above this message, the Engine did not fully start, so it does not actually know where your project stands.
Don't trust anything it says about where the project stands; ask it to re-ground, or quit and reopen Claude
Desktop.
