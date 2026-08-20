#!/usr/bin/env python3
"""Hard CI check: every pull request declares EXACTLY ONE VALID release-impact marker, so the release action can
fold a durable, reviewed impact per pull request (StarshipSuperjam/engine-template#942) instead of inferring SemVer from the diff at cut
time — catching a missing/invalid declaration at the pull request, not in a batch at the release gate.

A custom/script (not the `presence` kind) because the declaration is a hidden HTML-comment marker, not a `##`
section. The exempt-author / exempt-label handling is applied by validate.py BEFORE this script runs (declared in
the check's `ci_author_exempt`), so automated pull requests that cannot self-render a marker (dependabot,
github-actions) are waved through here — their markerless pull requests fold to a conservative default at the cut,
disclosed there. So this script only judges the pull requests that ARE required to declare.

Emits a finding.v1 JSON array on stdout — the custom/script contract. Reads the body from the trusted event
context ($GITHUB_EVENT_PATH); with NO body available (a local rehearsal, no event) OR an unreadable event, it
emits a DISCLOSED not-applicable no-op rather than a false hard block — so a CI-infra failure can never turn this
into a fail-closed wall. In CI the body is present and the check runs for real."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (the trusted body reader)
import release_impact  # noqa: E402  (the marker vocabulary — the ONE source, shared with the fold)

TIER = os.environ.get("ENGINE_RULE_TIER", "hard")

_VALUES = ", ".join(release_impact.RELEASE_IMPACTS)
_HOWTO = (f"Add one hidden HTML-comment marker of the form  engine-release-impact: VALUE  where VALUE is one of "
          f"{_VALUES}, chosen by COMPATIBILITY (not size): none = no public impact; patch = a backward-compatible "
          f"change to an existing feature; minor = a new capability or a deprecation; major = an incompatible "
          f"change to public behaviour, an API, or a contract. Engine-built pull requests get it rendered for you; "
          f"see the pull-request template. Change kind (Feature/Fix/…) is separate — it does not set the impact.")


def _read_pr_body() -> "str | None":
    """The pull-request body from the trusted event context ($GITHUB_EVENT_PATH .pull_request.body), resolving a
    ROOT-RELATIVE event path against validate.ROOT (only the negative fixture uses a relative path — GitHub sets
    GITHUB_EVENT_PATH absolute in production, so this is a no-op there). Mirrors release_integrity_check's
    ROOT-join so the seeded fixture body is found regardless of the check's working directory. None when no event
    is available (a local rehearsal); raises only on a genuinely malformed event."""
    event = os.environ.get("GITHUB_EVENT_PATH")
    if not event:
        return None
    if not os.path.isabs(event):
        event = os.path.join(validate.ROOT, event)
    if not os.path.exists(event):
        return None
    return (validate.load_json(event).get("pull_request") or {}).get("body") or ""


def findings_for_body(body: str) -> list:
    """Run the real release-impact rule against an already-rendered pull-request body.

    The normal script entrypoint obtains its body from GitHub's trusted event context. The engine updater
    has the rendered body before a pull request exists, so it calls this pure sibling rather than pretending
    an absent event is a pass. Keeping the marker parsing and all failure messages here means the pre-open
    and CI paths are one rule, not two look-alikes.
    """
    markers = release_impact.find_impact_markers(body)
    if not markers:
        return [{"severity": TIER, "location": None,
                 "message": "This pull request declares no release impact. " + _HOWTO}]
    if len(markers) > 1:
        return [{"severity": TIER, "location": None,
                 "message": f"This pull request declares {len(markers)} release-impact markers "
                            f"({', '.join(markers)}) — there must be exactly one, so the recorded impact is "
                            f"unambiguous. Remove the extra marker(s) so a single valid one remains "
                            f"(VALUE is one of {_VALUES})."}]
    value = markers[0]
    try:
        release_impact.canonical_impact(value)             # the module's public, fail-closed gate (not its private dict)
    except ValueError:
        return [{"severity": TIER, "location": None,
                 "message": f"The release-impact marker value '{value}' is not one of {_VALUES}. " + _HOWTO}]
    return []


def findings() -> list:
    try:
        body = _read_pr_body()
    except Exception as exc:  # noqa: BLE001
        # Surface the diagnostic (type + message) rather than masking it — a bare, fixed no-op message would let
        # a real future bug in _read_pr_body silently downgrade this gate to "not applicable" forever, with
        # nothing in the CI log to tell an operator the check is broken rather than legitimately skipping (QA).
        return [{"severity": "soft", "not_applicable": True,
                 "message": f"Could not read the pull-request body ({type(exc).__name__}: {exc}); the "
                            "release-impact declaration was not evaluated. In CI the body is present, so if this "
                            "persists there the check may be BROKEN (not merely skipping) — investigate."}]
    if body is None:
        return [{"severity": "soft", "not_applicable": True,
                 "message": "PR body not available (no event context); the release-impact declaration was not "
                            "evaluated. In CI the body is present and the check runs."}]
    return findings_for_body(body)


def main() -> int:
    print(json.dumps(findings()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
