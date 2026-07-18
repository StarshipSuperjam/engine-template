---
id: eADR-0034
title: One canonical engine core with a native adapter per AI runtime
status: accepted
date: 2026-07-17
---

## Decision

The engine has exactly one canonical policy home — `.engine/` — and speaks to each AI runtime through a
native adapter that only translates: the Claude Code adapter (`CLAUDE.md`, `.claude/`, `.mcp.json`) and the
Codex adapter (`AGENTS.md`, `.agents/`, `.codex/`). The engine reserves those three Codex namespaces the same
way it reserves the Claude ones. An adapter never carries its own copy of a policy, a workflow, or a gate;
where a runtime needs the material inline (a reviewer persona, a typed command), the adapter file is a
committed render generated from the canonical `.claude/` source and held in sync by check, and the catalog
records it as a render, not a second original. Runtime differences are absorbed at two seams only: a
provider-normalization layer at the hook boundary, and provider-specific wiring seam types that write each
runtime's own registration files.

## Significance

This locks in that adding a runtime is additive: the Claude adapter's files and behavior stay byte-identical
where no change is intended, and that stability is pinned by regression test, not convention. It also locks
the extension grammar — a new runtime gets new wiring seam types and its own adapter files; nothing
multiplexes an existing seam across two targets, and nothing edits another adapter to make room. Later work
must keep provider-specific vocabulary inside the named seams and route every capability through the
canonical core, so a feature built once appears in every adapter or is declared an exception (eADR-0035).

## Rationale

A non-engineer operator governs this engine through one merge gate; two diverging policy copies would give
them two things to trust and no way to tell them apart. Keeping one core and thin adapters means every
runtime enforces the same stance from the same source, and the cheapest honest way to keep required-inline
material single-sourced is to generate it and let a check fail when the render drifts. Normalizing runtime
payloads at the hook boundary keeps every downstream gate and tool provider-blind, which is what lets the
existing enforcement code protect a second runtime without being rewritten.

## Anti-choice

The strongest rejected alternative added a `provider` field to the existing wiring seam types instead of new
seam types. It lost because each existing seam resolves to exactly one target file and is verified by
mirroring that file's full content; one directive writing two different files in two formats would break that
mirror, complicate orphan detection, and — decisively — force edits through the Claude appliers, putting the
byte-stability promise at risk for no gain over two small additive seams.

## Status

accepted
