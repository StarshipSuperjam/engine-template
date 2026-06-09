<!-- The operator-facing copy for the first-run setup APPLY phase (the tool `tools/instantiator.py` reads
these sections by heading and renders them; built-in fallbacks keep it working if this file is ever missing,
and a parity test holds the two in step). This is the single review surface for what the operator is told as
the engine installs their choices and turns on its guardrails. Plain language only — no engine/maintainer
vocabulary reaches these words (so the private tool folder is never called a "tool-runtime", "uv", a "venv",
or a "sync"; the review gate is never called a "control-plane" or a "ruleset"; nothing is an "override" or a
"manifest"). The copy for the GitHub authorization screen, and for a review gate that couldn't be turned on,
lives with the review-gate tool itself, so it is not repeated here. Edit the wording here; the section
HEADINGS are stable keys the tool matches, so don't rename them. -->

## Before I set up the engine's own tools

To do its work the engine needs a small set of its own programs, kept in a private folder inside this
project — separate from anything already on your computer, and never touching your own setup. With your
go-ahead I'll download them from their official source, at a fixed version I can name for you, and place them
only in that folder. Nothing is downloaded or installed until you say yes.

## If the engine's tools couldn't be set up

I couldn't finish setting up the engine's own programs just now — most often that's a brief network problem.
Nothing is broken and nothing was left half-done; I just can't run the rest of setup until those programs are
in place, and I'll never quietly use a different setup on your computer instead. Let's try again in a moment,
or once you're back online — I'll pick up exactly where I left off.

## Your safer default is on

You're set to start in planning mode by default in this project: I'll lay out what I'm about to do and wait
for your go-ahead before making any change — the safer way to work here. This is only a convenience setting
for this project; it changes nothing about your own setup elsewhere, and it removes no safety check (you still
review and approve every change). Change it any time with /config.

## Your editing default — keep yours, or use the safer one

You already have your own editing default set, so I've left it alone. If you'd like, this project can start in
planning mode instead — I'd lay out my plan and wait for your go-ahead before changing anything, which is a
little safer. Use planning mode for this project, or keep your own default? Keeping yours changes nothing.
Either way, nothing about your setup elsewhere changes and no safety check is removed.

## About automatic secret scanning

A quick note on automatic secret scanning — the check that warns you if a password or key is committed by
accident. If your project's hosting doesn't include it, I can't switch it on for you, so the project is on the
basic level for now. To get automatic scanning you can make the project public, or add GitHub's Advanced
Security if your plan offers it. I'm telling you so you can decide — I won't switch anything on or off without
you.

## If I couldn't set up file ownership for reviews

I couldn't read your account name just now, so I haven't yet set up who owns the engine's own files for
review — the part that routes any change to those files to you for approval. It isn't blocking the rest of
setup. Once I can read your account name — for example after you sign in to GitHub from the command line —
I'll set it; or just tell me your account name and I'll set it now.

## If I couldn't reach your project on GitHub

I couldn't find this project on GitHub or sign in just now, so I couldn't turn on the review gate that
protects your main branch. The rest of setup is unaffected. Once you're signed in to GitHub from the command
line and the project is connected, I can turn it on — just ask me to finish setup.
