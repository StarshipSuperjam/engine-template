---
id: eADR-0036
title: Codex capability that cannot be proven is disclosed as degraded, never claimed
status: accepted
date: 2026-07-17
---

## Decision

Where the Codex platform withholds a guarantee, the engine degrades loudly instead of pretending: the Codex
conversation-transcript format is treated as unstable, so session-memory capture parses it through a
dedicated recognizer that captures fully or not at all — an unrecognized transcript records a visible
"memory not captured" status, and Codex transcripts never fall through to the tolerant Claude parser.
Because Codex skips new or changed project hooks until the operator re-trusts them, every engine change to
its hook registrations tells the operator at the moment it happens that re-trust is needed, and the status
readout detects and names a session whose hooks did not run. Enforcement the platform cannot mirror is a
ledgered exception (eADR-0035), never a quiet claim of coverage, and the acceptance step for Codex behavior
is a live post-merge validation session against a written pass bar — a failure inside that bar is a defect
owed a fix, never a scope removal.

## Significance

This fixes the engine's posture on a runtime it cannot fully verify from inside a repository: capability
claims are bounded by what a check or a demonstration actually proved, and everything else is named as
unproven. Later work must keep the fail-loud shape — a new Codex integration point inherits the duty to
detect its own silent-off state and disclose it in plain words, and no green signal may be built that
conflates "did not run" with "passed".

## Rationale

The write-gate, memory capture, and boot orientation all ride hooks the operator can leave untrusted and a
transcript format the platform explicitly reserves the right to change. A gate that silently stopped running
would be worse than no gate, because the operator's consent would rest on protection that is not there. The
cost of loud degradation — an occasional notice the operator must act on — is small against the cost of a
single silently-corrupted memory store or a session that edited files with the write-gate off and no one
told.

## Anti-choice

The rejected alternative made capture resilient by letting the existing tolerant parser try any transcript
it was handed. It lost because tolerance is the wrong virtue against an unstable format: a
partially-recognized transcript would write fragments into the project's long-term memory looking like
captured conversation, and a corrupted memory is strictly worse than an honestly empty one.

## Status

accepted
