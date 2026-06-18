<!-- engine-template:landing-front -->

<div align="center">
  <img src="assets/engine_banner.jpg" width="700" alt="The Engine" />

  <h1>The Engine</h1>

  <p><strong>Scaffolding that lets you direct AI on real engineering work — and stay the person who decides — without reading a line of code.</strong></p>
</div>

The Engine is not a faster, more capable autonomous agent. It's the opposite bet: a way to keep a human who *can't* read code firmly in charge of AI that can. Its job is to make what the AI does **legible** and **consentable** to that person — so you can direct serious work on a real project, and approve it, on evidence you can actually judge.

## This is for the person who can't verify the work — and has to govern it anyway

You direct the work, and you approve every change, from the very first commit. You are not expected to read code, debug a failure, or take the AI's word that it did the right thing. Most AI tooling quietly assumes a power user who can check the output. The Engine assumes you can't — and is built, end to end, so that you don't have to.

## You approve the work without reading the code

Every change the AI proposes arrives as a pull request, and nothing reaches your main branch until you say so. But your approval never rests on reading or trusting the code. It rests on evidence you *can* weigh:

- a demonstration you run yourself, and vary, to see the behavior with your own eyes;
- independent checks that have to pass before a change is even offered to you;
- a plain-language account of what changed, why, and what it could put at risk — including an honest statement of how sure anyone can be.

That bundle — not a code review — is the gate. It is the whole point of the Engine.

## What you get to stand on

Every AI session starts cold: it remembers nothing. Left there, you'd re-explain your project every time and hope for the best. Instead, the Engine gives the work a durable footing — committed to the repository itself, and open to you:

- where things stand right now, and what's still unfinished;
- the decisions made so far and *why* — written down where you can read them, not buried in a chat log only the AI ever saw;
- a current map of how the project fits together;
- a sense of what matters next, so a fresh session picks up instead of starting over.

This isn't a catalog of the AI's abilities — it's the ground *you* direct from: committed, inspectable, and yours. The AI builds faithfully not because it's clever, but because the ground under it is solid and in plain sight.

## Constrained autonomy is the feature

Slow, gated, and verifiable beats fast and opaque. The Engine deliberately trades raw autonomy for trust: work is proposed before it lands, lands only behind your approval, and can always be undone; what governs the work is written down and citable; and if a supporting service goes down, it falls back to plain files in git rather than stranding you. You give up the thrill of an agent that simply runs — and you get work you can actually stand behind.

## Get started

1. Click **Use this template** above to create your own repository.
2. Open it in [Claude Code](https://claude.com/claude-code).
3. A guided first-run setup walks you through your choices and stands up the Engine for your project.

## Status

The Engine is pre-1.0 and under active construction toward its first milestone. Expect rapid change.
