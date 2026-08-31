---
title: Memory migration trial — measuring ClawMem's retrieval against the incumbent, and committing the verdict
---

## Purpose

This is the operator-attended trial that decides whether ClawMem's semantic retrieval is good enough to replace
the Engine's native memory — the go/no-go evidence PR 2 of program `prg_13dc60836f68` needs before any
integration. It reaches a committed verdict: a pre-registered gold-query battery run against BOTH ClawMem and
the Engine's own installed semantic recall, judged against a pass bar fixed in advance, recorded in this file
bound to the export manifest digest, the query-set digest, and the exact ClawMem commit and lockfile. Enter it
once the `clawmem_export.py` exporter (X1) exists and you are ready to spend an attended hour producing that
verdict. It is not a background job: it exports your private history in cleartext and runs local models, so you
run it yourself, at a terminal — the single exception is the incumbent-recall comparison in step 7, which is an
MCP tool call made in a live session, called out there.

## Steps

1. **Understand what you are consenting to, before anything is written.** The export is your conversation
   history in CLEARTEXT — everything recall can reach, rejoined and readable. The capture scrubber masks only
   anchored credential shapes; by deliberate policy it leaves **names, email addresses, and phone numbers
   intact**, and it cannot catch a novel secret shape. Every guarantee is POINT-IN-TIME: the export honours the
   withholds, erasures, and masking rules in force at the moment it is written, and a later withhold, a merged
   erasure, or a new masking rule does NOT reach an export already on disk or any ClawMem store built from it.
   This is portability, not a backup — it is lossy and has no restore path; the memory vault stays your only
   backup. If any of that is not acceptable right now, stop here.

2. **Choose a safe destination directory.** The exporter refuses a path inside a git working tree that git does
   not already ignore, but it CANNOT see three dangers you must rule out yourself: a cloud-synced folder
   (Dropbox, iCloud, Drive), a network mount, or anything a backup tool sweeps. Pick a local, non-synced,
   non-backed-up scratch folder you have CONFIRMED is outside every sync root — on default macOS and Windows
   setups the home directory, Desktop, and Documents are often themselves iCloud- or OneDrive-synced, so check
   System Settings → iCloud (or your sync client's excluded-folders list) first rather than assuming. Make sure
   it is empty (the exporter refuses a non-empty destination so a stale export cannot survive beside the new one).

3. **Run the exporter at a terminal.** From your own shell:

   ```bash
   uv run --directory .engine -- python tools/memory/clawmem_export.py <destination>
   ```

   It refuses without a real terminal on stdin and stdout, and refuses while any operator-ordered erasure is
   still pending. On success it writes `conversations/<session>.jsonl`, `curated/notes.md`, `meta/pins.jsonl`,
   and `meta/manifest.json`. Read `meta/manifest.json` — its `omission_account` tells you exactly what was left
   out and why (withheld sessions and records by id, injected and scaffolding filtered, scrub-altered count,
   legacy-skipped, bookkeeping kinds dropped). Record the manifest's own SHA-256 (`shasum -a 256
   <destination>/meta/manifest.json`); the verdict binds it.

4. **Install ClawMem in a sandbox, and record exactly what you ran.** Clone ClawMem OUTSIDE this repository
   (nothing is vendored or depended on by the Engine). Check out a specific commit — the exporter's line-format
   contract was verified against `ba09cb8` (v0.37.0), so start there unless you have a reason to move — and run
   `bun update node-llama-cpp` before the first embed — the pinned lockfile ships a llama.cpp too old for
   current Apple Silicon and fails Metal compilation (upstream `yoloshii/ClawMem#26`). Record the resolved
   ClawMem commit SHA and the resolved `node-llama-cpp` version from the lockfile; the verdict binds both.

5. **Import the conversations, and only the conversations.** Point ClawMem's claude-code importer at
   `conversations/` ONLY — that is the ClawMem-ingestible layer. `curated/notes.md` is the disposable curated
   layer for a human to read, not for import; `meta/` is provenance. Confirm the import accepted every session
   file. **An import failure is an EXPORTER defect to fix, never a retrieval verdict** — if a file is rejected,
   stop the trial, fix the exporter, re-export, and start this step again.

6. **Pre-register the gold-query battery, and digest it BEFORE running anything.** Author at least **25**
   paraphrase-only queries — each phrased in words that do NOT appear verbatim in its target, so the battery
   measures meaning-retrieval rather than keyword overlap — and for each name the target session or record you
   expect in the top results. Freeze the set and record its SHA-256 (`shasum -a 256 <query-set-file>`) NOW, so
   the verdict cannot be reverse-fitted to the results. A battery authored after seeing either system's answers
   is not evidence.

7. **Run the same battery against BOTH systems.** Against ClawMem, use its eval harness
   (`clawmem eval run --gold <query-set.jsonl>`), falling back to running each query manually and recording the
   top 3 hits if the harness proves unusable. Against the Engine's installed semantic recall, the query goes
   through `recall-by-meaning` (the semantic add-on the program would replace) — this is an **MCP tool, not a
   shell command**, so you cannot type it at a terminal. Do it in a live Claude Code session: ask me, in chat, to
   call `recall-by-meaning` for each query and report its top 3, then copy those into your record. (This is the
   one step of the trial that is not terminal-only.) Record, per query and per system, whether the known target
   appears in the top 3 (hit@3) and, on a miss, what came back instead.

8. **Apply the pass bar fixed in advance.** ClawMem PASSES only if its hybrid retrieval **meets or beats the
   incumbent semantic recall on hit@3** across the battery. A tie passes; a loss fails. Examine every ClawMem
   miss and say why it missed — a bad query, a genuine retrieval gap, or a corpus artifact — because a pass with
   unexplained misses is not a pass you can defend to PR 2.

9. **Record the verdict in this file and commit it.** Fill the slot below with real values and commit it to the
   repository; that committed artifact IS the go/no-go evidence, and it is worthless unbound.

   ### Verdict (fill in, then commit)

   ```text
   Date:                     <YYYY-MM-DD>
   Export manifest digest:   sha256:<...>            # meta/manifest.json
   Query-set digest:         sha256:<...>            # the pre-registered battery, frozen before step 7
   ClawMem commit:           <sha>                   # the sandboxed clone
   node-llama-cpp resolved:  <version>               # from the resolved lockfile (yoloshii/ClawMem#26)
   Battery size:             <N >= 25>
   ClawMem hit@3:            <k>/<N>
   Incumbent hit@3:          <k>/<N>                 # recall-by-meaning, same battery
   Pass bar met (ClawMem >= incumbent):  <yes|no>
   Misses examined:          <one line per ClawMem miss: query -> what came back -> why>
   Verdict:                  <GO | NO-GO>
   ```

10. **Delete every residue as part of finishing.** The trial is not done until the cleartext is gone. Delete the
    export directory AND the ClawMem store — but the store is more than the clone directory: ClawMem keeps its
    SQLite database and its FTS/vector indexes in a data directory, and caches the downloaded models separately,
    and either can sit OUTSIDE the clone (a global data dir and model cache are common), so an `rm -rf` of the
    clone alone can leave cleartext-derived indexes behind. Before deleting, find the ACTUAL paths from ClawMem's
    own configuration or its startup output (it reports where it reads and writes), then remove the database and
    indexes. Removing the models is optional — they are large and carry no conversation content. The verdict text
    you committed in step 9 carries no conversation content and stays; the raw export and the built store must not
    linger on disk.

## Done when

The Verdict slot in this file is filled with real values, committed to the repository, and binds all five
identities — export manifest digest, query-set digest, ClawMem commit, resolved `node-llama-cpp` version, and
both systems' hit@3 with ClawMem's misses examined — and the export directory and ClawMem store have been
deleted. A committed `GO` is PR 2's authorization to proceed with integration; a committed `NO-GO` sends the
program to its documented substrate-swap fallback. Either way the decision is recorded and reproducible, and no
cleartext residue remains.

## Notes

- **Why pre-registration and the incumbent comparison exist.** A battery that cannot fail is theatre. Freezing
  and digesting the query set before any run, stating the pass bar in advance, and measuring the incumbent on
  the SAME battery are what make a `GO` mean "better than what we have" rather than "looked fine."
- **Duration and disk.** Expect roughly an attended hour end to end. ClawMem downloads about 2.5 GB of models on
  first use; the export itself is a few sequential passes over the ledger. If an embed or query run appears
  stuck, check that `bun update node-llama-cpp` actually resolved a current build (a stale one fails Metal
  compilation silently-ish and falls back to empty results) before assuming a retrieval problem.
- **The scrub is defense-in-depth, not a wall.** Treat every exported and imported file as private material
  regardless of masking; names, emails, and phone numbers are intact by policy, and a novel secret shape can
  pass. This is why steps 2 and 10 (safe destination, delete residue) are not optional.
- **Length.** This runbook carries an explicit `length_budget_overrides` entry in
  `.engine/check/operation-shape.json`: its consent, destination, and residue-deletion content is safety
  material that must not be trimmed to fit a line budget.
