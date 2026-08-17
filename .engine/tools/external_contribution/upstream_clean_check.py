#!/usr/bin/env python3
"""Upstream-clean inspector — the read-only `custom/script` entry for engine/check/upstream-clean
(the external-contribution module's *soft* upstream-clean nudge).

What it does: when the Engine contributes to a product repository the operator does NOT own (an open-source
upstream, or engine-template reached by a fork-native deployment escalating an engine fix), the outgoing contribution must carry only the
product's files — never the Engine's own committed files. This inspector intersects the outgoing diff's
changed paths with the file-precise engine-owned path set and warns, in plain language, if any engine-owned
path is about to ride along into the upstream pull request: it names the offending files, why it matters,
and the fix. A clean contribution (no engine-owned path in the diff) passes with no finding.

Honest tier / blocking: every finding is `soft`, so this never blocks a merge — it is an operator-decidable local nudge,
not a hard gate. The branch is engine-clean by origin (cut from the upstream's engine-free default); this
nudge catches an accidental engine path before submission; and the upstream's own review backstops it where
one exists. Read-only: it inspects path lists only and never writes a file (the read-only mutation firewall).

Where the inputs come from (both injectable, so tests and the demo run fully offline):
  - `changed`: defaults to `work_record.changed_paths(cap=None)` — the branch's outgoing diff paths, read
    UNCAPPED (StarshipSuperjam/engine-template#416). The live caller is the submission flow (`submit.py`), which supplies the
    cross-fork outgoing diff (the product branch against the upstream's default) through `changed`; the
    no-argument default is the local branch diff. The read is uncapped because this is a SAFETY predicate: `changed_paths`
    caps at 50 for orientation, and a cap could let an engine path sort past it and slip the leak intersection
    (a false negative), so every engine-owned hit is seen — the listed set is the complete intersection, not a
    truncated heads-up.
  - `owned`: defaults to `module_coherence.engine_owned_paths(discover_manifests())` — the exact
    file-precise engine-owned set that CODEOWNERS is rendered from, so this nudge and CODEOWNERS share one
    source of truth. A path counts as engine-owned only if a present module's `provides` claims it or it is
    foundation infrastructure (CLAUDE.md, the engine workflows, .github/CODEOWNERS, the tool-runtime
    lockfiles, ...).

Trigger: this rule joins NO validate suite (StarshipSuperjam/engine-template#777 removed it from `pre-close`). It is meaningful only
against an OUTGOING cross-fork contribution: in an ordinary same-repo deployment the Engine's files
legitimately live alongside the work, so a suite-firing version would either warn on every normal engine
change (CI) or compute a finding nothing surfaces — the `pre-close` advisory pass surfaces only `hard`
findings, and this rule is `soft`, so its pre-close output was discarded. The live caller is the submission
flow — `submit.py` runs the predicate (`findings()`) at submit time against the cross-fork diff and, on a
real leak, publishes it via telemetry-on-fire. The check declaration is retained, not deleted: it is the
entitized knowledge-graph surface, the operator-facing message-of-record, and the `params.script` this
module is resolved through; an empty `suites` array is the schema's blessed shape for a rule invoked
directly rather than by a suite.

The no-argument entry (`emit_findings`, below) is now a PURE read-only print for a direct/manual run and for
the falsifiable `demo` self-check (discovered by walking `.engine/tools/**/*.py`, independent of suites); it
deliberately does NOT emit telemetry. The "emits a telemetry finding when it fires" duty is the submission
flow's, at submit time over a real outgoing diff — it lives in `submit.py`, never this entry
(StarshipSuperjam/engine-template#416, rejected as unsafe).

Contract: run with NO arguments (a direct/manual read-only surface — no longer suite-dispatched), it prints a
finding.v1 JSON array to stdout and exits 0. A separate `demo` subcommand runs a falsifiable self-check.
"""
from __future__ import annotations
import json
import os
import sys

# Make the sibling `.engine/tools/` modules importable whether imported as
# `external_contribution.upstream_clean_check` or run directly as the wired check script (the
# dependency_discipline / projects_sync idiom).
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import validate  # noqa: E402 — the finding.v1 helper
import work_record  # noqa: E402 — changed_paths: the outgoing-diff reader (injectable `run` transport)
import module_coherence  # noqa: E402 — engine_owned_paths: the file-precise CODEOWNERS engine-owned set


def _offending_message(paths: list, *, contributing_to_engine_home: bool = False) -> str:
    """The operator-facing leak message. This string is PUBLISHED verbatim by the submission flow's
    telemetry-on-fire (`submit.py._leak_record`): its FIRST SENTENCE becomes a GitHub Issue title and the whole
    of it is embedded in the body. Two constraints follow and are load-bearing, not cosmetic: (1) it
    interpolates ONLY the offending path list — never a repo slug, branch/ref, or any other
    environment-controlled value — so nothing arbitrary reaches a published Issue; (2) it keeps that path list
    OUT of the first sentence, so the published title stays a fixed, safe string.

    `contributing_to_engine_home` selects FRAMING only and carries no detection meaning (the caller has already
    narrowed the flagged set by home-ness — StarshipSuperjam/engine-template#556). It mirrors the two-branch shape of
    `submit.py._leak_narration` — its framing/tone only, never its interpolation set:
      - the stranger-target branch (default): the files belong to the Engine and would ride into a repository
        the operator does not own;
      - the engine-home branch: the target IS the Engine's own home, so the flagged files are just this copy's
        own saved state/settings/private tuning — they belong here, not in the shared template. It must NEVER
        say "someone else's repository", which would be backwards (the framing `_leak_narration` refuses)."""
    listed = ", ".join(paths)
    if contributing_to_engine_home:
        return (
            "This contribution to the Engine's own home includes files that belong to just this copy of the "
            "Engine — your own saved state, settings, or private tuning — not to the shared template. "
            f"The files are: {listed}. Your engine code and its maps do travel with a contribution like this, "
            "but these files — your own saved state, settings, or private tuning — belong only here. To fix "
            "it, take those files off this branch before you submit — this copy keeps them, nothing is lost. "
            "This is a heads-up, not a block — nothing is stopped."
        )
    return (
        "This contribution branch includes files that belong to the Engine, not to the product you're "
        "contributing to — and the Engine's files should never ride along into someone else's repository. "
        f"The files are: {listed}. They've most likely slipped in by accident (a file added by mistake, or "
        "a merge that pulled your fork's engine branch back in). To fix it, take those files off this branch "
        "before you submit — your fork keeps its copy, nothing is lost. This is a heads-up, not a block — "
        "nothing is stopped. If the project you're contributing to reviews pull requests, its maintainers "
        "would likely turn these files away too; if it doesn't, this is the only thing watching for it, so "
        "it's worth clearing."
    )


def findings(tier: str, *, changed=None, owned=None, contributing_to_engine_home: bool = False) -> list:
    """The upstream-clean findings, as a list of finding.v1 dicts.

    Empty list = a clean contribution (no engine-owned path in the outgoing diff). A single `soft` nudge,
    naming every offending engine-owned path, when the diff touches one or more. Every finding carries
    `tier` severity (`soft`) — never `hard`. `changed` and `owned` are injectable (defaulting to the real
    diff reader and the real engine-owned set) so tests and the demo run fully offline; the submission flow
    supplies the cross-fork diff through `changed` without touching this predicate.

    `contributing_to_engine_home` selects the finding MESSAGE FRAMING only (stranger-target vs the Engine's own
    home) — it does NOT change detection: which paths are flagged is decided entirely by `changed`/`owned`, and
    the caller narrows `owned` by home-ness (StarshipSuperjam/engine-template#556) before calling. The submission flow passes the
    home boolean it already computed so the published telemetry message is truthful on both targets
    (StarshipSuperjam/engine-template#777).
    """
    if changed is None:
        changed = work_record.changed_paths(cap=None)  # StarshipSuperjam/engine-template#416: UNCAPPED — a safety predicate must see
        #                                                 every engine-owned hit, never drop one past a cap
    if owned is None:
        owned = module_coherence.engine_owned_paths(module_coherence.discover_manifests())
    owned_set = set(owned)
    offending = sorted(p for p in changed if p in owned_set)
    if not offending:
        return []
    # Build the location literally from a repo-relative path — `validate.loc()` expects an ABSOLUTE path and
    # would double the `.engine/` prefix on a relpath (the dependency_discipline precedent does the same).
    message = _offending_message(offending, contributing_to_engine_home=contributing_to_engine_home)
    return [validate.finding(tier, message, {"file": offending[0], "line": None})]


def emit_findings() -> int:
    """The no-argument path the validator invokes: print the finding.v1 array and return 0."""
    print(json.dumps(findings("soft")))
    return 0


def demo() -> int:
    """Prove the nudge fires on a leaked engine path, passes a clean product-only diff, catches a leaked
    foundation file, stays quiet on an empty diff, and frames the engine-home case truthfully (naming this
    copy's own state, never "someone else's repository") — RETURNS NON-ZERO if any invariant is broken (the
    falsification can fail). Fully offline: every case injects `changed`/`owned`, so no git runs and the real
    working tree is never touched."""
    owned = [
        ".engine/check/upstream-clean.json",
        ".engine/tools/external_contribution/upstream_clean_check.py",
        "CLAUDE.md",
        ".github/CODEOWNERS",
    ]
    cases = []  # (label, kwargs for findings(), predicate over the findings list)
    cases.append(("an engine path in the outgoing diff fires one soft nudge naming it",
                  {"changed": ["src/feature.py", ".engine/check/upstream-clean.json"], "owned": owned},
                  lambda fs: len(fs) == 1 and fs[0]["severity"] == "soft"
                  and ".engine/check/upstream-clean.json" in fs[0]["message"]))
    cases.append(("a product-only diff passes clean (no finding)",
                  {"changed": ["src/feature.py", "README.md"], "owned": owned},
                  lambda fs: fs == []))
    cases.append(("a leaked foundation file (CLAUDE.md) is caught, the product file is not named",
                  {"changed": ["CLAUDE.md", "src/feature.py"], "owned": owned},
                  lambda fs: len(fs) == 1 and "CLAUDE.md" in fs[0]["message"]
                  and "src/feature.py" not in fs[0]["message"]))
    cases.append(("an empty diff passes clean (no finding)",
                  {"changed": [], "owned": owned},
                  lambda fs: fs == []))
    # StarshipSuperjam/engine-template#777: the engine-home framing names this copy's own state and NEVER the stranger
    # "someone else's repository" wording (which would be backwards when the target IS the Engine's home).
    cases.append(("the engine-home framing names this copy's own state, not someone else's repository",
                  {"changed": ["src/feature.py", ".engine/check/upstream-clean.json"], "owned": owned,
                   "contributing_to_engine_home": True},
                  lambda fs: len(fs) == 1
                  and ".engine/check/upstream-clean.json" in fs[0]["message"]
                  and "just this copy of the Engine" in fs[0]["message"]
                  and "someone else's repository" not in fs[0]["message"]))

    failures = []
    for label, kw, ok in cases:
        result = findings("soft", **kw)
        if any(f.get("severity") == "hard" for f in result):
            failures.append(f"{label}: an upstream-clean finding must never be hard, got {result}")
        elif not ok(result):
            failures.append(f"{label}: invariant broken, got {result}")

    if failures:
        print("DEMO FAILED — the upstream-clean nudge broke an invariant:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO PASSED — the upstream-clean nudge fires on a leaked engine path, passes a clean "
          "product-only diff, catches a leaked foundation file, stays quiet on an empty diff, and frames the "
          "engine-home case as this copy's own state rather than someone else's repository.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return demo()
    return emit_findings()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
