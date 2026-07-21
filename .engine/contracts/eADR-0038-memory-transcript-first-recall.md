---
id: eADR-0038
title: Memory is a transcript-first archive, with meaning spent at read time
status: accepted
date: 2026-07-20
---

## Decision

The memory substrate is a transcript-first archive: its canonical record is the exact user and assistant messages of each session, normalized per session and scrubbed of secret-shaped content at capture — not a curated pyramid of AI summaries built over that record. Durable operator intent that has no better canonical home is held as a small set of explicit pins, created the moment the operator asks and carrying their own wording and source session; a pin is a record-type within the one substrate, never a second store. Meaning at recall time is supplied by the session's own reasoning model working ABOVE a lexical retrieval seam: the model expands a question into paraphrases and project anchors, calls the keyword search interface (the `search` interface — a lexical keyword tool in the required core), groups the hits into transcript windows, and reranks them by meaning in its own context — the search process itself holds no model and writes nothing on a read. Semantic embeddings are not part of the required core; they are admissible only behind that same seam, as an optional module, and only once a labeled benchmark shows the read-time model workflow leaves a material gap. No background model work maintains the store: there is no automated consolidation, summary-to-gist roll-up, frecency, reinforcement-on-read, or tiered demotion.

## Significance

This fixes what memory holds and where its intelligence lives, and it retires the curation lifecycle as the substrate's shape. Because the canonical record is the raw conversation, exact wording is always recoverable and nothing load-bearing rests on a later model's summary of an earlier one. The intelligence that makes recall meaning-aware is spent at read time by the model already in the session, not accumulated as mutable scored state — which keeps memory inside the cognitive-substrate law that forbids a hand-authored scored working-set, since search now writes nothing and no per-record score survives a read. The lexical seam stays the one stable retrieval contract: a richer engine, if it is ever earned, is substituted behind it without changing its callers. Later work must route recall through that seam and must not reintroduce a background summariser, a per-record score, or an embedding dependency in the required core; a deployment that needs embeddings installs them as an optional module behind the seam, never as a precondition, and never as a reason a live substrate becomes required. That capture scrubs secrets before storage is fixed here; how the per-prompt consultation reflex is delivered belongs to the cognition and orientation contracts, and how forgetting is gated belongs to the control plane — this record does not restate either.

## Rationale

The prior substrate inverted the effort: it spent heavily on curating, summarising, and scoring a store whose actual recall — the only moment that matters — still failed on a paraphrased question while firing noise on a broad one. Preserving the exact transcript and moving meaning to read time is the smaller, more honest design: it keeps the one irreplaceable thing, what was actually said, verbatim, and it borrows intelligence from the reasoning model already present rather than building a second one out of scores and summaries that rot. Placing the model above a lexical seam rather than behind it is a constraint, not a preference — the retrieval process is a plain tool with no model to call; MCP sampling is not currently offered in the target runtimes, and baking an outbound model call into every generated repo would break git-native offline degradation and open a secret surface. Deferring embeddings until a benchmark proves them keeps a heavy dependency, and its privacy and migration costs, out of the required core until evidence, not intuition, pays for it.

## Anti-choice

The strongest rejected alternative was to keep the curation lifecycle and make recall smarter underneath it — better clustering for roll-up, a semantic index feeding the summariser. It was rejected because it doubles down on the layer that was not the problem: the summaries duplicate stronger canonical authorities — merged pull requests, decision records, specifications — in a weaker, staler copy, and no amount of clustering repairs a recall path that the exact transcript answers directly. A second rejected alternative placed the meaning engine behind the search seam as required core — embeddings or a vector store answering every query. It was rejected on feasibility and cost: the retrieval process has no in-session model, sampling is not currently available in the target runtimes, and an outbound API call bakes a key, a network dependency, and a secret surface into every deployment while breaking offline degradation. Embeddings remain admissible, but only behind the seam, only as an option, and only after a labeled benchmark shows the read-time workflow leaves a gap worth their cost.

## Status

accepted
