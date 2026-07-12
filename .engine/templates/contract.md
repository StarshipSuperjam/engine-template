---
required_sections: ["Decision", "Significance", "Rationale", "Anti-choice", "Status"]
allowed_sections: ["Supersedes"]
length_budget: 120
---

<!-- Two eADR homes, told apart by folder (repository-topology law 5 / D-169): the engine's own founding
canon lives here in .engine/contracts/ and is replaced wholesale on an engine update; a DEPLOYMENT's own
engine-decision eADRs live in .engine/contracts/instance/ and are preserved across every update. If you are
recording a decision this project made about ITS OWN engine, author it under instance/ (see
.engine/contracts/instance/README.md). This guidance is not part of the record — do not copy it into the eADR. -->

## Decision

<State the single decision in one or two sentences — what was chosen, in plain words. Name the thing decided, not the discussion that led to it.>

## Significance

<Explain why this decision is worth keeping forever: what it locks in, and what later work now has to respect. If it does not constrain future choices, it probably does not need its own record.>

## Rationale

<Give the reasoning in two to five sentences — the forces that mattered and the trade-off that was made — with enough background that a reader a year from now understands why this was chosen, not just what.>

## Anti-choice

<Name the strongest alternative that was seriously weighed and turned down, and say plainly why it lost. Every decision worth recording had a real alternative; if none comes to mind, reconsider whether this needs a contract.>

## Status

<One word — proposed (drafted, not yet in force), accepted (in force now), or superseded (replaced by a later decision). Keep it the same as the status in the top block above.>

## Supersedes

<Include this section only when this decision replaces an earlier one: name the eADR it replaces (for example, eADR-0003) and add one line on what changed. Delete this whole section for a first-of-its-kind decision.>
