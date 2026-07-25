---
title: Memory recall — finding what this project already knows
---

## Purpose

How to answer "what did we decide about X?", "why did we do it that way?", or "have we hit this before?" from
this project's saved memory. Enter it whenever a request leans on something from an earlier session — a past
decision, the reasoning behind it, a lesson learned, or something the operator said they prefer — and before
relying on your own recollection of this project, which does not survive between sessions.

The point of the procedure is that memory's search is a **keyword** tool, not a meaning-aware one. It matches
words, so a question worded differently from the original conversation finds nothing. The meaning is supplied
here, by you, in this session: you rephrase the question several ways, search each, then read and judge the
results. Skipping the rephrasing is what makes recall fail.

## Steps

1. **Decide whether memory is the right source at all.** Canonical project artifacts outrank remembered
   narrative: a merged pull request, a decision record under `.engine/contracts/`, an issue, or the code itself
   is stronger evidence than a memory of it. Use memory for the *narrative* — why a choice was made, what was
   rejected, what went wrong last time, what the operator prefers. If the answer belongs in a canonical
   artifact, read that instead, and use memory only to find *which* artifact to read.
2. **Turn the question into several short search phrases.** Write three to six, and make them differ from each
   other — this is the step that does the real work:
   - Use the words the conversation itself would have used, not the words of the question you were just asked.
   - Include at least one phrase using different vocabulary for the same idea (a synonym set), because the
     original wording may share no words with the question.
   - Include project anchors where they apply — a file or subsystem name, an issue or decision-record id, a
     person, a feature name.
   - **Keep each phrase to roughly two to four words.** Search requires *every* word in a phrase to appear in
     the same record, so a long natural-language sentence reliably matches nothing.
3. **Search each phrase separately** with the memory search tool (`mcp__engine-memory__search`), and **set a
   limit on each call** (10 is a reasonable default) — the tool returns *every* match when no limit is given,
   which on a large store floods the session. Narrow with the optional role filter when the question has a
   clear shape (`decision`, `rationale/pushback`, `lesson`, `dead-end`, `preference`, `intent`, `observation`),
   and with the tag filter when you have an entity id such as a decision-record id.
4. **Merge the results and drop duplicates.** Pool the hits from every phrase and de-duplicate by record id.
   Judge the pooled set, not each search in isolation: a record that surfaced for two different phrasings is
   usually a better answer than one that topped a single search.
5. **Read the conversation behind the promising hits.** A search result is a summary written after the fact.
   For the few that look like they answer the question, read the real conversation with the window tool
   (`mcp__engine-memory__recall-window`), passing the hit's `session_id`. Anchor on the hit's position and widen
   only if the answer is not there. If a hit's session field is a cluster key rather than a real session (a
   summary folded from several sessions), follow that record's source references to real sessions first, then
   read those.
6. **Judge by meaning and answer.** Rank what you found by whether it actually answers the question, not by
   the order search returned it — that ordering is keyword relevance, which is exactly what you are correcting
   for. Say plainly where the answer came from and how confident it is. If nothing genuinely answers, say that;
   a confident answer assembled from near-misses is worse than "I did not find it."
7. **Offer the exact wording when it matters.** Summaries are paraphrases. When wording is load-bearing — what
   the operator actually asked for, a commitment, a specific phrasing — offer the verbatim conversation from
   step 5 rather than relying on the summary of it.

## Done when

The question is answered from what the project actually recorded, with its source named — or you have said
plainly that memory does not hold it. Every promising hit was read in its real conversation rather than trusted
as a summary, and where exact wording mattered it was offered. Nothing was written: recall only reads.

## Notes

**Why the rephrasing is not optional.** Memory's search is a keyword floor by deliberate design — the process
that answers it holds no language model, so it cannot understand that two differently-worded questions mean the
same thing. Measured on this engine's own recall benchmark, the single-query path found the answer to **none**
of twenty-two reworded questions. The rephrasing in step 2 is the entire fix; the tools cannot supply it.

**What the window can and cannot promise.** It returns the conversation as stored. Long messages were saved in
pieces and are rejoined on read, and machine-inserted text (continuation summaries, notifications) is left out
so it is never mistaken for something the operator said. What it cannot prove is that no piece of a long message
was permanently erased later — so treat the wording as faithful but not certified, and say so if a fine
distinction in phrasing is carrying weight.

**A known gap, until raw conversation becomes searchable.** Search reaches the curated summaries, not the raw
turns themselves. So something said once and never summarized cannot be *found* by step 3, even though step 5
could read it if you knew which session to look in. When you know roughly when a conversation happened, reading
the session directly is the way around it.
