#!/usr/bin/env python3
"""Upstream-clean inspector — the read-only `custom/script` entry for engine/check/upstream-clean
(the external-contribution module's *soft* upstream-clean nudge).

What it does: when the Engine contributes to a product repository the operator does NOT own (an open-source
upstream, or the engine-mechanic building engine-template), the outgoing contribution must carry only the
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
    UNCAPPED (#416). The live caller is `submit.py` (`clean_findings`), which supplies the cross-fork
    outgoing diff (the product branch against the upstream's default) through `changed`; the no-argument
    default is the local branch diff. The read is uncapped because this is a SAFETY predicate: `changed_paths`
    caps at 50 for orientation, and a cap could let an engine path sort past it and slip the leak intersection
    (a false negative), so every engine-owned hit is seen — the listed set is the complete intersection, not a
    truncated heads-up.
  - `owned`: defaults to `module_coherence.engine_owned_paths(discover_manifests())` — the exact
    file-precise engine-owned set that CODEOWNERS is rendered from, so this nudge and CODEOWNERS share one
    source of truth. A path counts as engine-owned only if a present module's `provides` claims it or it is
    foundation infrastructure (CLAUDE.md, the engine workflows, .github/CODEOWNERS, the tool-runtime
    lockfiles, ...).

Suite / trigger: this rule rides the `pre-close` suite only (never CI). In an ordinary same-repo deployment
the Engine's files legitimately live alongside the work, so a CI-firing version would warn on every normal
engine change; it is meaningful only against an OUTGOING cross-fork contribution. The live caller is the
submission flow — `submit.py.clean_findings` runs the predicate (`findings()`) against the cross-fork diff
(#416: no longer dormant — #415 wired it). The no-argument validator surface (`emit_findings`,
below) is a PURE read-only print and deliberately does NOT emit telemetry: the `pre-close` suite is collected
on every clean turn-close (close.py's advisory pass, dispatched by suite membership — `target.context` is not
enforced), and a GitHub write there would break close.py's "a local run reaches no GitHub event" invariant.
So the "emits a telemetry finding when it fires" duty is the submission flow's, at submit time over a real outgoing diff — it lives in
`submit.py`, never this validator entry (#416, rejected as unsafe).

Contract: invoked by the validator with NO arguments, it prints a finding.v1 JSON array to stdout and exits
0. A separate `demo` subcommand runs a falsifiable self-check.
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


def _offending_message(paths: list) -> str:
    listed = ", ".join(paths)
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


def findings(tier: str, *, changed=None, owned=None) -> list:
    """The upstream-clean findings, as a list of finding.v1 dicts.

    Empty list = a clean contribution (no engine-owned path in the outgoing diff). A single `soft` nudge,
    naming every offending engine-owned path, when the diff touches one or more. Every finding carries
    `tier` severity (`soft`) — never `hard`. `changed` and `owned` are injectable (defaulting to the real
    diff reader and the real engine-owned set) so tests and the demo run fully offline; the submission flow
    supplies the cross-fork diff through `changed` without touching this predicate.
    """
    if changed is None:
        changed = work_record.changed_paths(cap=None)  # #416: UNCAPPED — a safety predicate must see
        #                                                 every engine-owned hit, never drop one past a cap
    if owned is None:
        owned = module_coherence.engine_owned_paths(module_coherence.discover_manifests())
    owned_set = set(owned)
    offending = sorted(p for p in changed if p in owned_set)
    if not offending:
        return []
    # Build the location literally from a repo-relative path — `validate.loc()` expects an ABSOLUTE path and
    # would double the `.engine/` prefix on a relpath (the dependency_discipline precedent does the same).
    return [validate.finding(tier, _offending_message(offending), {"file": offending[0], "line": None})]


def emit_findings() -> int:
    """The no-argument path the validator invokes: print the finding.v1 array and return 0."""
    print(json.dumps(findings("soft")))
    return 0


def demo() -> int:
    """Prove the nudge fires on a leaked engine path, passes a clean product-only diff, catches a leaked
    foundation file, and stays quiet on an empty diff — RETURNS NON-ZERO if any invariant is broken (the
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
          "product-only diff, catches a leaked foundation file, and stays quiet on an empty diff.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return demo()
    return emit_findings()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
