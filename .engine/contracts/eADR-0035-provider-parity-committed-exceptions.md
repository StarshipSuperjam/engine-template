---
id: eADR-0035
title: Provider parity holds by check, with committed exceptions that also declare weakness
status: accepted
date: 2026-07-17
---

## Decision

Every capability the engine exposes to one runtime must have a counterpart on the other, and a standing check
enforces it in both directions — hooks, typed commands, reviewer personas, helper servers, and the
instruction floors all compare pairwise. The only sanctioned differences live in one committed exception
ledger, and an entry must state not just that something is missing but when a counterpart exists in a weaker
form: present-but-weaker capabilities (an instruction where the other runtime has mechanical enforcement, a
protection the platform can switch off) are ledger entries with reasons, not footnotes in prose. The ledger
is schema-validated and sits inside the guardrail-weakening floor, so widening an exception is held for the
operator's deliberate acknowledgment exactly like loosening any other protection.

## Significance

This is what keeps the second runtime honest over time: a future feature shipped to one runtime without a
counterpart or a ledger entry turns the merge gate red, in both directions, forever. It also fixes the
meaning of "parity" the operator is told: presence and declared strength, never a silent claim of
equivalence. Later work must treat the ledger as the single home for every deliberate asymmetry — adding an
entry is a guarded, reviewed act, and removing a capability to dodge an entry is the failure mode this
record exists to forbid.

## Rationale

The operator's strongest evidence is a mechanical signal, so the parity promise has to be a check, not a
stated intention. But real platforms differ — one runtime may lack an event, gate a protection behind its
own trust step, or accept only prose where the other accepts enforcement — so an honest check needs a
sanctioned escape valve. Committing the exceptions makes each one visible at review, reasoned, and durable;
guarding the ledger closes the loophole where the escape valve itself becomes the quiet way around the
check.

## Anti-choice

The rejected alternative was a presence-only parity check with platform differences documented in the
support matrix. It lost because prose does not travel to the merge gate: a reader of a green parity check
would conclude the runtimes are equivalent while materially weaker counterparts shipped unflagged, and
nothing would resist the drift of "documented difference" quietly becoming "unreviewed gap".

## Status

accepted
