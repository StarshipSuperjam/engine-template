---
title: Knowledge impact check — when to query the project's wiring before you change it
---

## Purpose

The project keeps a queryable map of how its own parts are wired — for each part, what it is, what it is
part of, what depends on it, what checks it, and what governs it. The map is reachable any time through
four read-only tools that load in every session automatically, so you can look something up instead of
re-deriving how the project hangs together by hand.

Enter this runbook when you want to:

- **Run an impact check** — before you change something other parts lean on, find out what would be
  affected.
- **Orient on something unfamiliar** — you have landed on a part you do not know and need to see what it
  touches.
- **Trace a connection** — you need to know whether, and through what, two parts are wired together.

The end state is that you reached the map for the part in hand rather than guessing, and confirmed what it
told you against the live files before acting on it.

## Steps

1. **Know what you are looking at.** To get one part's record and the parts it directly connects to, ask
   the map for it by name (`get-entity`). Reach for this first when you have landed somewhere unfamiliar.
2. **Find the part you mean.** When you do not have an exact name, list what matches — by kind, by file
   path, or by which part owns it (`find`) — then pick the one you mean.
3. **Run the impact check.** Before you change a part, ask what sits next to it (`neighbors`) — look in
   both directions — and read each connection in plain words: what it **is part of**, what **depends on**
   it, what **checks** it, and what **governs** it. The connections that point *at* the part — what relies
   on it, what checks it — are the ones a change can break, so they are what the impact check is really
   after. Widen the look a hop or two only when a change reaches further.
4. **Trace a connection.** To learn whether two parts are wired together and through what, ask for the path
   between them (`relate`). An empty answer means they are not connected in the map.
5. **Confirm against the live files before you assert.** The map is built from the committed files and is
   only as fresh as its last rebuild, so treat anything it returns as last-known: open the actual file and
   confirm before you state it as fact or act on it. Put what you found in plain words for the operator;
   never echo a raw internal token.

## Done when

You reached the map for the part in hand instead of re-deriving its wiring; for a change, you know what
depends on it, checks it, and governs it; you confirmed the parts that matter against the live committed
files; and you relayed them to the operator in plain language. Nothing was written — these tools only read.

## Notes

The map answers **structure only** — what is wired to what, never what was decided about it or whether it
is a good idea. For the distilled reasoning behind a part, that is a separate read.

It is **pull-only by itself.** Two things already push the relevant slice without you asking: the
per-prompt scent surfaces pointers when your prompt matches them, and the session-start briefing surfaces
the neighbourhood of the work already in hand. This runbook is the deliberate reach *beyond* what got
pushed — when you have a specific part to check and nothing surfaced it for you.

A change that weakens one of the engine's own guardrails is exactly the kind the impact check exists to
catch before the fact: run it whenever a change reaches into something other parts rely on.
