#!/usr/bin/env python3
"""Demo — first-run provisioning creates the COMPLETE set of labels the engine's guards depend on.

What this checks, in plain words: when a new repository is stood up from the template, GitHub copies none of the
engine's custom issue labels. The engine's own machinery relies on four of them — the one that marks its health
issues, the one you add to approve a flagged safety change, the flag for a mis-formatted engine issue, and the
marker on a "forget this from memory" request. This shows, on the REAL provisioning step (`ControlPlane.
ensure_labels`) driven against a stand-in GitHub that owns no labels yet, that first-run setup creates all four —
so none is left to be made lazily (and mis-coloured) later, or to block a first safety gate.

It runs the real provisioning path — the same GET-then-create each label goes through — with only the network
faked (a stand-in that reports every label missing and records what gets created). Nothing real is touched.

Run: uv run --directory .engine -- python tools/demo_control_plane_labels.py
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootstrap        # noqa: E402  (ControlPlane.ensure_labels — the real provisioning step under test)
import telemetry        # noqa: E402  (GitHubIssues — the real label-ensure boundary + the engine label name)
import weakening_guard  # noqa: E402  (ACK_LABEL — the guardrail-ack name)
import issue_conformance_ci  # noqa: E402  (NEEDS_REAUTHORING_LABEL — the malformed-issue flag's canonical name)
from memory import erasure_observer  # noqa: E402  (ERASURE_LABEL — the erasure marker's canonical name)

REPO = "you/your-new-repo"

# What a working engine needs present, named at their canonical homes (never re-typed here) so this demo also
# cross-checks that provisioning agrees with each owning subsystem.
EXPECTED = {
    telemetry.ENGINE_DOMAIN_LABEL: "marks issues the engine opens about its own health",
    weakening_guard.ACK_LABEL: "you add this to approve a change the engine flags as weakening a safety gate",
    issue_conformance_ci.NEEDS_REAUTHORING_LABEL: "flags an engine issue not yet in the engine's standard format",
    erasure_observer.ERASURE_LABEL: "marks a pull request whose merge forgets remembered notes",
}


def _bare_repo_transport(created: dict):
    """A stand-in GitHub that owns NO labels yet: every 'does this label exist?' read says no (404), and every
    create is recorded. This is the exact seam telemetry.GitHubIssues uses, so the real GET-then-create runs."""

    def transport(method, path, body=None):
        if "/labels/" in path and method == "GET":      # existence check for one label -> absent
            return 404, None
        if path.endswith("/labels") and method == "POST":  # create a repo label -> record and accept
            created[(body or {}).get("name")] = body or {}
            return 201, {}
        return 404, None

    return transport


def main(_argv=None) -> int:
    print("What this checks: first-run provisioning creates every issue label the engine's guards depend on,")
    print("on a fresh repo that starts with none of them.\n")

    created: dict = {}
    issues = telemetry.GitHubIssues(REPO, "token", transport=_bare_repo_transport(created))
    # Drive the REAL provisioning step. tier is passed so the demo needs no committed manifest.
    ok = bootstrap.ControlPlane(REPO, "token", issues=issues, tier=bootstrap.SOLO).ensure_labels()

    if not ok:
        print("Provisioning reported a failure (ensure_labels returned False). On a real repo this stays SAFE —")
        print("the affected guard simply keeps blocking rather than passing silently — but this offline demo")
        print("expected a clean run, so something is wrong with the provisioning path. Your project was not touched.")
        return 1

    missing = [name for name in EXPECTED if name not in created]
    bad = []
    for name, why in EXPECTED.items():
        body = created.get(name)
        if body is None:
            print(f"  [MISSING] {name:20} — NOT created ({why})")
            continue
        color = body.get("color") or ""
        desc = body.get("description") or ""
        cap_ok = 0 < len(desc) <= 100          # GitHub's label-description hard limit, and never blank
        color_ok = bool(color)
        mark = "OK" if (cap_ok and color_ok) else "WRONG"
        if not (cap_ok and color_ok):
            bad.append(name)
        print(f"  [{mark:5}] {name:20} #{color or '??????'}  — {why}")
        print(f"          \"{desc}\" ({len(desc)} chars)")

    # A label created but not expected would mean provisioning grew a label this demo doesn't know about.
    unexpected = [name for name in created if name not in EXPECTED]

    print()
    if not missing and not bad and not unexpected:
        print(f"In plain words: all {len(EXPECTED)} labels the engine needs were created on a repo that began with")
        print("none — so a generated repo's first safety gate, first mis-formatted issue, and first memory-erasure")
        print("never wait on (or lazily mis-create) a label the operator was told they never make by hand.\n")
        print("Vary it yourself: remove one row from REQUIRED_LABELS in bootstrap.py and re-run — this demo will")
        print("report that label MISSING and exit non-zero (that is the falsification it exists to catch).")
        return 0

    print("This run did NOT confirm complete provisioning:")
    for name in missing:
        print(f"  - {name}: expected but never created")
    for name in bad:
        print(f"  - {name}: created with a blank colour or an out-of-range description")
    for name in unexpected:
        print(f"  - {name}: created but not in this demo's expected set (provisioning gained a label — update the demo)")
    print("That is a real signal worth investigating, not a pass. Your project was not touched.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
