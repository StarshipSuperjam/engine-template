# Setting up the engine's scheduled self-review

## What this is

The engine can look over its own health on a regular schedule — once a week by default — and write up what
it found as a short, plain-language summary you read and approve, like any other change. This page shows you
how to turn that on, how to keep it running, how to change how often it runs or which model does the review,
and — if you'd like — how to run it on Anthropic's cloud instead.

It ships switched **off**: it does nothing until you set it up here. While it's off, or if it ever quietly
stops, the engine tells you on your next start — so a self-review that never ran, or stopped running, is never
invisible. You can come back to this page any time; it stays with the project.

One thing worth knowing up front about what the review can see: it reads your project's **committed files**,
the **open issues the engine has filed about its own health** (the running list of things it has flagged), and
— once you turn it on — your **saved memory**, the notes the engine keeps about your decisions as you work.

Saved memory lives only on your computer and isn't part of the project's files, so the review can see it only
when two things are in place: you've set up a **backup** of your saved memory, and this scheduled review has
been **given read access** to that backup. Both are a deliberate, optional setup — the review normally reaches
only *this* project, while your memory backup is kept in a separate, private place — and **"Optional: let the
review read your saved memory"** below walks you through it. Until both are in place, the review simply tells you
in its summary that it couldn't review your saved decisions this time, and which of the two is missing and how
to fix it — it never pretends your memory is empty.

## Turn it on

Setup is three one-time steps. Do **all three** — skipping any one makes the review quietly fail to run, with
no error you'd notice, so don't stop early.

**Step 1 — create a sign-in token.** This one step uses **Claude Code's command-line tool** — separate from
the Claude Desktop app you run the engine in, so you may need to install it first (it's quick). If you're not
sure how, just **ask me and I'll walk you through it**. Then, in a terminal, run:

```
claude setup-token
```

This opens your browser to sign in to your Claude account and hands you back a token — a long private string
tied to your subscription, good for one year. That token is the only access the review needs; it just has to
come from one of Anthropic's paid plans (Pro, Max, Team, or Enterprise). A token from a free account won't run
the review, and — like a mistyped one — it fails quietly, with no error you'd notice. Keep it handy for Step 2.

**Step 2 — give the token to this project.** On GitHub, open this project's **Settings → Secrets and variables
→ Actions** and add a new repository **secret** — a private value GitHub keeps for this project and never shows
again — named exactly:

```
CLAUDE_CODE_OAUTH_TOKEN
```

Paste the token from Step 1 as its value. The name has to match exactly: a typo here makes the review fail
without any error you'd notice.

**Step 3 — let the engine open the summary for you.** On GitHub, open this project's **Settings → Actions →
General**, scroll to **Workflow permissions**, and turn on the option named
**"Allow GitHub Actions to create and approve pull requests."** Without this, the review still runs but can't
open its summary for you to read — so it would look like nothing happened. This only lets the engine *open* a
summary as an ordinary change for you; it can never merge anything on its own — approving is always your call.

A note for the cautious: that one setting also lets automated actions *approve* changes in general, which you
may not want. On this project that approval power does nothing — a merge always takes your own action, and an
automated approval doesn't count toward it — so it's safe to leave on here. If you later tighten this project
to require an approval before a merge, set it to require *your* review specifically, so an automated approval
can't stand in for yours.

Once all three are done, on the next scheduled day the self-review runs on its own and opens a summary for you
to read and approve.

If you'd like to check it works straight away instead of waiting for the schedule, you can start a run by hand
from the project's **Actions** tab — find **audit-prep** and use **Run workflow**.

## Keeping it running

A few ordinary things can quietly stop the review. In every case the engine tells you on your next start and
names the one thing to do, so you're never left guessing:

- **The sign-in token expires after a year.** When it lapses, the review stops. Run `claude setup-token` again
  and update the `CLAUDE_CODE_OAUTH_TOKEN` secret (Step 2) with the new value.
- **The memory-vault read key can lapse too** — only if you've set up the saved-memory read (above) and either
  gave the key an expiry or your organization caps it. When it does, the review keeps running but stops reading
  your saved memory, and its summary says so. The fix is specific to *that* key: make a new read-only key and
  update the **`MEMORY_VAULT_TOKEN`** secret (steps A–B above). This is **not** `claude setup-token` — that's the
  separate sign-in token that runs the review itself, and re-running it won't restore the memory read.
- **Running it very often uses your subscription, like any other Claude usage.** A too-frequent schedule can run
  into your plan's usage limits and pause the review until they reset — so the fix is to run it *less* often, not
  more (see below).
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

If you're unsure, run it *less* often rather than more — a too-frequent schedule can run into your plan's usage limits.
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

## Optional: let the review read your saved memory

This part is optional. By default the review checks your committed files and the engine's open issues — but
**not** your saved memory (the notes the engine keeps about your decisions), because that lives only on your
computer, outside the project. If you'd like the review to also check whether those saved notes have gone stale
or started to contradict each other, you can give it read access to them. It's a deliberate, one-time setup, and
worth understanding before you do it. Ask me and I'll talk you through each step; here's what it involves.

**Two things have to be true**, and doing only the first leaves the read switched off:

1. **A backup of your saved memory exists.** Your saved memory is backed up to a small, private GitHub
   repository — your *memory vault*. If you haven't set one up, just **ask me to set one up** and I'll create it
   with your consent.
2. **This review is given read access to that vault.** The review normally reaches only *this* project; your
   vault is a separate, private repository, so the review needs its own read-only key to reach it — steps A–C
   below.

**A — make a read-only key for the vault.** On GitHub, create a **fine-grained personal access token** —
GitHub's name for a narrow key you can lock to a single repository and a single kind of access. Scope it to
**only your memory-vault repository**, and give it **just "Contents → Read"** (permission to *read* files and
nothing else — no writing, no deleting, no other repository).

- **Make it not expire, where GitHub lets you.** On a **personal** account you can set the expiry to **"No
  expiration"** — choose that, so the review doesn't silently stop a year later. If your vault lives in an
  **organization** that caps how long a key can live, "No expiration" won't be offered; the key will lapse on
  the organization's limit, and the review will tell you when it does so you can renew it (see *Keeping it
  running*).

**B — give the key to this project as a named secret.** On GitHub, open this project's **Settings → Secrets and
variables → Actions** and add a new repository **secret** (a private value GitHub stores and never shows again)
named **exactly**:

```
MEMORY_VAULT_TOKEN
```

Paste the key from step A as its value. The name has to match exactly — a typo here makes the read fail with no
error you'd notice (the review's summary will say it wasn't given access).

- **Worth knowing before you paste:** if you keep **one shared vault for all your projects** (the default), this
  one key lets *this* project's scheduled review read **every** project's saved memory in that shared vault — not
  only this project's. For your own private projects that's usually fine, but if one project's memory is
  sensitive and you'd rather wall it off, set *that* project up with its **own private vault** — that's the way
  to keep it out of the shared key's reach. Ask me and I'll help you choose.

**C — test it.** When the secret is set, **ask me to test the read** — I'll do a one-shot read of your vault with
that key and tell you, in plain words, that it worked or the exact thing to fix (the key is scoped to the wrong
repository, has the wrong permission, the secret name doesn't match, or the vault's location isn't recorded in
this project yet). That way you learn it works now, not a week later when the review next runs.

- One honest limit: that test reads the vault with the key *you hand me*, which proves the key itself is good. It
  can't confirm that GitHub will hand the *same* key to the scheduled run under the right secret name — only the
  first real scheduled run does that. If that first run reports it wasn't given access, the fix is in *Keeping it
  running*.

**Honest about maturity:** this cross-project read — a scheduled review reaching a *separate* private vault — is
**new in this version of the engine and hasn't been run end-to-end while building it**. The pieces are tested in
isolation and the test read above exercises the real read path, but the full scheduled cross-repository read is
written from the design, not yet proven in a live run here. Treat it as a capability to try, with the test read
as your check that it's working.

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
