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

**Everything the Engine shows reaches me, the assistant — not your screen.** When a session starts, and at
points during it, the Engine hands me a briefing and notices — where the project stands, and any safety
alarms. Those go to me, not to you; you see only what I actually type. So when something is safety- or
consent-critical — a safety gate is off, a guardrail was weakened, or I could not ground — I must tell you in
plain words, and I must never act as though you already saw something the Engine only handed me ("as the card
above shows"). The real guarantee on any change is still your approval when you merge it — not my relaying. If
I ever fail to pass something on, that is a lapse on my part, not a safe default.

**How to tell I actually grounded.** When the Engine is grounded, the first thing I show you each session is a
short titled status block — like **Project status: all clear**, or **⚠ Protected branch is off** if something
needs your attention — so you can see at a glance that I grounded before I answer. If my first reply jumps
straight into your request with no status block at the top, I did not fully ground — so don't trust what I say
about where your project stands; tell me to re-ground, or quit and reopen Claude Desktop.
