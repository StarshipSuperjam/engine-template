<!-- The operator-facing copy for the first-run setup APPLY phase, the FINISH phase (the consistency check and
the tidy-up that end setup), and the brownfield OVERLAP check (shown only when the engine joins a project that
already has its own files). The tool `tools/instantiator.py` reads these sections by heading and renders them;
built-in fallbacks keep it working if this file is ever missing, and a parity test holds the two in step. This
is the single review surface for what the operator is told as the engine installs their choices, turns on its
guardrails, checks the result fits together, cleans up the one-time setup files, and reports any overlap with a
project's existing files. Plain language only — keep the engine's internal machinery out of what the operator
reads: name each thing by what it does for them, not by its engine/maintainer term. That is a relevance
judgment made in the writing and the review, never a banned-word list — the right word for the operator's need
is the right word, and no word-list is kept (a list invites list-growth and teaches that word-banning is a
writing function). Whether a render leans on engineer-shorthand is judged by the `audit` self-review's prose
probe and the per-change review, not a filter. The copy for the GitHub authorization screen, and for a review gate that couldn't be turned on,
lives with the review-gate tool itself, so it is not repeated here. Edit the wording here; the section HEADINGS
are stable keys the tool matches, so don't rename them. -->

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

## Your stance came with this project

This project came set up with a starting set of codes of conduct — short notes on how you like me to work with
you (for example, speaking plainly, and explaining choices before you make them). They're here from the first
session, and they're yours: change, add, or remove any of them any time with /engine-conduct. I didn't put
them in place silently — this note is me telling you they're here.

## A security-contact file came with this project

I added a short file called `SECURITY.md` at the top of your project. It tells anyone who finds a security
problem — a bug that could let someone get in, or get at your data — how to report it to you privately,
instead of posting it in the open where it could be misused. You own this file and can edit it any time; if
your project already had one, I left yours exactly as it is. I didn't add it silently — this note is me
telling you it's there.

## Your project's front page is now yours

This project started from a template, and its front page — the `README.md` at the top — was the Engine's own
landing page, the page that advertises the Engine to people deciding whether to use it. I replaced it with a
short starter for **your** project, so your repository's front door is about your work, not the Engine. This
was intentional setup of your project's front page, not a change to anything you wrote — and the starter is
yours to edit freely. I didn't do it silently — this note is me telling you.

## Your project starts without a license — and that's normal

A brand-new project normally starts with no license file at all — and that's a safe, normal place to begin: your
code is yours, and stays yours, until you choose to share it on your own terms. This project started from a
template, and that template carried its own license file — a `LICENSE` at the top naming the template's author and
their copyright, not you. Left in place it would have set the terms for **your** project under someone else's name,
so I removed it — and I added nothing in its place, because which license to use is your decision to make, not
mine. GitHub treats a project with no license file as exactly this normal starting state, and may show a small
"No license" note on the project page; that's expected, not a problem. When you're ready to pick one, GitHub's
choosealicense.com walks through the common options in plain language — and I can explain what a license file is
and help you add the one you choose, though I can't tell you which terms are right for you; for anything that really
matters legally, ask a person. I didn't do this silently — this note is me telling you.

## Your project's working guide

This project started from a template, and its working guide — the `CLAUDE.md` at the top — arrived carrying the
template's own setup notes, which are about building the template itself, not your project. I've replaced it with
the engine's working guide for **your** project, kept inside a clearly marked block I keep current as the engine
updates — so if you open that file and see the marker lines around it, that part is mine to maintain, not
something you need to edit. The part that's yours to shape — how you like me to work with you — lives in your
codes of conduct instead: change it any time with `/engine-conduct`. I didn't do this silently — this note is me
telling you.

## If I couldn't set up file ownership for reviews

I couldn't read your account name just now, so I haven't yet set up who owns the engine's own files for
review — the part that routes any change to those files to you for approval. It isn't blocking the rest of
setup. Once I can read your account name — for example after you sign in to GitHub from the command line —
I'll set it; or just tell me your account name and I'll set it now.

## If I couldn't reach your project on GitHub

I couldn't find this project on GitHub or sign in just now, so I couldn't turn on the review gate that
protects your main branch. The rest of setup is unaffected. Once you're signed in to GitHub from the command
line and the project is connected, I can turn it on — just ask me to finish setup.

## If something needs fixing before finishing

Before finishing, I check that everything fits together — and something doesn't line up yet, so I've paused
rather than carry on with a setup that isn't right. Here's what I found:

## Your two ways forward

Neither choice loses anything you've already decided. You can fix what's listed above and run setup again — it
picks up right here. Or, if this looks like something you can't sort out yourself, stop here and report it
(copy the lines above so someone can help). I won't carry on with a setup that isn't consistent.

## Setup checks out

Everything fits together — your setup is consistent and ready to use.

## Your review gate is on

Your branch review gate is on: every change to your main branch now goes through approval.

## Your review gate isn't on yet

Your branch review gate isn't on yet — but nothing else is held up by it. I'll remind you each time I start,
and you can turn it on any time by asking me to finish setup.

## Setup is complete

Setup is complete. I've cleaned up the one-time setup files — the walkthrough, its notes, and the setup helper
itself — now that they've done their job. Everything your project needs to keep running stays in place, and all
your choices are saved. You're ready to start.

## Your project looks like it already has a team

Your project looks like it already has a team — others review changes here. For that, the team setup fits best:
the engine commits under a separate name and a teammate approves its changes, so the way work is reviewed here
stays the way it already works. It's a suggestion for you to weigh, not a switch I throw — on-your-own stays
available, and the choice is yours.

## Before I add the engine, here's what I found in your project

Your project already has files and settings of its own. I'm adding the engine alongside them, so first I'll
show you anywhere the two would overlap — what I'd do, and what you'd keep or lose — and let you decide. I
never change anything without showing you first.

## A file of yours sits where the engine keeps its own

You already have a file here: {paths}. This is a spot the engine normally keeps to itself. If you let it, the
engine would put its own file here in place of yours — so you'd only lose your version if you choose that. Your
choices: let the engine use this spot (your file here is replaced) · keep your file and have the engine skip it
· stop, and decide later. Nothing is replaced until you choose.

## The engine and your project both use the same file

You already have your own entries in {paths}. The engine adds its own clearly-marked section here and leaves
everything else of yours exactly as it is. Your choices: add the engine's section and keep all of yours · leave
this file untouched for now · stop, and decide later.

## One of your review rules also covers the engine's files

You have a rule that decides which teammate is asked to review changes to particular files — `{rule}` — and it
also covers the engine's own files. The engine adds its own such rule so changes to its files are always sent
to you; yours keeps covering everything else. I'm pointing this out so the overlap is no surprise. Your
choices: add the engine's rule (so review requests for the engine's files always come to you) · leave your
rules as they are · stop, and decide later.

## Nothing of yours is in the way

Good news — none of your files or settings overlap with what the engine adds. I can set it up alongside your
project cleanly.

## I couldn't safely read one of your files

I couldn't make sense of {paths} just now, so I've left it completely untouched rather than risk changing it
wrongly. Take a look at it, then run setup again — I'll pick up from here.
