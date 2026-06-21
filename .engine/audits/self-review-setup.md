# Setting up the engine's scheduled self-review

## What this is

The engine can look over its own health on a regular schedule — once a week by default — and write up what
it found as a short, plain-language summary you read and approve, like any other change. This page shows you
how to turn that on, how to keep it running, how to change how often it runs or which model does the review,
and — if you'd like — how to run it on Anthropic's cloud instead.

It ships switched **off**: it does nothing until you set it up here. While it's off, or if it ever quietly
stops, the engine tells you on your next start — so a self-review that never ran, or stopped running, is never
invisible. You can come back to this page any time; it stays with the project.

One limit worth knowing up front: for now this review looks at your project's **committed files**. It can't
yet read your **saved working memory** — the notes the engine keeps about your decisions as you work —
because that needs a backup feature this version of the engine doesn't have yet. That's a planned addition;
until it lands, keep in mind the review covers your committed project, not your saved memory.

## Turn it on

Setup is three one-time steps. Do **all three** — skipping any one makes the review quietly fail to run, with
no error you'd notice, so don't stop early.

**Step 1 — create a sign-in token.** On your own computer, run:

```
claude setup-token
```

This opens your browser to sign in to your Claude account and hands you back a token — a long private string
tied to your subscription, good for one year. Keep it handy for Step 3.

**Step 2 — turn on access for scheduled runs.** This is a *separate* one-time switch in your Claude account,
not part of the command above: the one-time go-ahead that lets an automatic, unattended run draw on your
subscription's monthly allowance. Look for it in your Claude account settings, named for scheduled or developer
("Agent") runs and turn it on. **This step is the easiest to miss, and there is no error if you skip it — the
review simply never runs** — so don't move on until you've turned it on. (If you can't find the switch, ask me
and I'll point you to where it currently lives.)

**Step 3 — give the token to this project.** On GitHub, open this project's **Settings → Secrets and variables
→ Actions** and add a new repository **secret** — a private value GitHub keeps for this project and never shows
again — named exactly:

```
CLAUDE_CODE_OAUTH_TOKEN
```

Paste the token from Step 1 as its value. The name has to match exactly: a typo here makes the review fail
without any error you'd notice. That's it — on the next scheduled day the self-review runs on its own and opens
a summary for you to read and approve.

If you'd like to check it works straight away instead of waiting for the schedule, you can start a run by hand
from the project's **Actions** tab — find **audit-prep** and use **Run workflow**.

## Keeping it running

A few ordinary things can quietly stop the review. In every case the engine tells you on your next start and
names the one thing to do, so you're never left guessing:

- **The sign-in token expires after a year.** When it lapses, the review stops. Run `claude setup-token` again
  and update the `CLAUDE_CODE_OAUTH_TOKEN` secret (Step 3) with the new value.
- **Running too often can use up your monthly allowance.** If that happens, the review pauses until the
  allowance resets — the fix is to run it *less* often, not more (see below).
- **On a public project, GitHub pauses any schedule after 60 days with no activity.** A new commit, or just
  asking me to start it again, brings it back.

You don't have to keep any of this in mind: whenever the review hasn't run in a while, the engine says so the
next time you start it.

## How often it runs

By default the review runs once a week. To change that, open `.github/workflows/audit-prep.yml`, find the line
that sets the schedule — it's the one with five numbers in quotes — and replace it with one of these:

```
# once a week (the default), on Sundays
    - cron: "17 7 * * 0"

# once a month, on the 1st
    - cron: "17 7 1 * *"
```

If you're unsure, run it *less* often rather than more — too-frequent runs can use up your monthly allowance.
One thing to know: if you later update the engine, it puts the default weekly schedule back, so re-apply your
change after an update. (Which model does the review — below — is remembered across updates; the schedule is
the one setting you re-apply.)

## Which model does the review

The review is done by a capable model — by default, the most capable one (currently Opus). You can point it at a
different model — for example a newer one when it's released — by adding a repository **variable** named
`AUDIT_MODEL` (the same **Settings → Secrets and variables → Actions** screen, under **Variables**), set to the
model's name. Two things worth knowing: a cheaper or weaker model gives you a less trustworthy review, and a
misspelled name makes the run fail the same quiet way a mistyped token does — so change this deliberately.
Unlike the schedule, this setting is remembered across engine updates.

## Optional: run it in the cloud instead

This part is optional. The setup above — the scheduled run on GitHub — is the supported way, and you don't need
anything more. If you'd rather the review run on Anthropic's cloud, so it can run even while your computer is
off, you can set up a **Cloud Routine** instead. The engine never depends on this: if a cloud run ever stops,
the engine tells you on your next start, and the normal GitHub schedule is still there to fall back on.

To set one up, in Claude create a **Remote** routine — not a Local one, which only runs while your computer is
awake — on a **recurring** schedule, not a one-time run, pointed at **this project**, and paste this exactly as
the instruction. Don't change a word:

```
Act as this project's audit. Load and follow the instructions in .claude/agents/audit.md, then run the self-review of this project now and output only the plain-language summary — what you looked at, what you found, and what you recommend — with no preamble.
```

Then use **Run now** once and check that a fresh summary appears, so you know it's working.

A few honest notes: a Cloud Routine needs a paid plan with Claude Code on the web turned on; it's a newer,
preview feature that may change; and it counts against your account's daily routine allowance. And in fairness:
this cloud path has not yet been run end-to-end while building this version of the engine — the steps are
written from the design, not yet tried here — so treat it as a convenience to try, not a guarantee, and keep
the standard GitHub schedule as your dependable path.

## Once it's running

Each review opens as an ordinary change for you to read and approve — nothing about your project changes on its
own. You don't need to come back here unless you want to change how often it runs, change the model, or set up
the cloud option. If the engine ever tells you the self-review has stopped, this page is how you start it again.
