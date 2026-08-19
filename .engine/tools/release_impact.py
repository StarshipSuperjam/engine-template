#!/usr/bin/env python3
"""The canonical release-impact vocabulary — the ONE source of truth for a pull request's declared SemVer impact.

WHY THIS EXISTS. The next release version is a DERIVED presentation, not a thing a human names. A pull request
DECLARES what it changed for a consumer — `none`, `patch`, `minor`, `major` — as structured data; the release
action folds those declarations across the merged pull requests and computes the version, using the structural
diff only as a floor it can genuinely PROVE. This mirrors the issue-kind design (kind is data; the title prefix
is presentation): here, compatibility impact is data; the version number is presentation. The invariant: humans
and review declare the compatibility class; the engine proves when a declaration is too low where it can; the
release action computes the version; release-safety violations remain their own blocking axis.

WHAT `major` MEANS HERE (for the author-facing guidance that projects from this). `major` is decided by
COMPATIBILITY, not by size, effort, or importance: a change that breaks an existing public behaviour, API, or
contract. That it is rare is a CONSEQUENCE of a healthy codebase, never the test — a small-but-breaking change
is still `major`. `minor` = a backward-compatible added capability or an explicit deprecation; `patch` = a
backward-compatible correction or modification of an existing feature; `none` = no public release impact. This
is independent of the pull request's title KIND (Feature/Fix/…), which answers a different question — release-
note grouping, not SemVer.

A STDLIB-ONLY LEAF. Imported by the release-impact CI check (a per-pull-request hot path), by `release_cut`'s
version fold, and by the tests. It imports nothing but `re`/`sys`, so it never drags the module-manager /
coherence stack onto the check's path — the same leaf discipline `issue_kind` keeps. This is the ONE home for
the four-value vocabulary AND for the ordering `none < patch < minor < major`: `release_cut` consumes `rank`/
`max_impact` here rather than spelling the ladder a second time, because that ordering IS the version math and a
second copy would let the two drift into disagreement (unlike `issue_kind.KINDS` ↔ release_cut's display list,
whose duplication is a deliberate ORDER difference — no such difference exists for a semantic ladder).

THE MARKER (mirrors telemetry's severity trailer and issue_kind's). `impact_trailer` is the ONE place
`<!-- engine-release-impact: … -->` is built; it validates against RELEASE_IMPACTS and raises otherwise, so the
value is marker-safe by construction (an enum member carries no `<`, `>`, or `--`). `parse_impact` takes the
LAST marker so body prose forged before the genuine trailer cannot hijack it. HONEST RESIDUAL: last-match does
not defend a marker appended AFTER the genuine one by someone who can edit the pull-request body. That is only
PARTLY bounded: a body edited to UNDER-declare is caught by refuse-until-corrected ONLY where the diff proves a
higher STRUCTURAL floor (a contract surface added/removed, a module added/removed) — a behaviour-only break that
touches no such surface has NO mechanical backstop, so a post-merge downgrade of its marker would pass unnoticed.
The residual is disclosed and tracked (a StarshipSuperjam/engine-template#710-style head-bound impact status is the durable fix); the human merge
gate on the release pull request bounds it today.

EXEMPT AUTHORS. Automated pull requests (dependabot, github-actions) do not run the coordinator and cannot
render a marker. They are exempt from the hard CI check (the same authors the PR-body-completeness check
exempts) and, at the cut, an exempt merged pull request with no marker folds as DEFAULT_EXEMPT_IMPACT (patch —
the safe floor for a routine dependency bump), named in the release evidence rather than hidden. EXEMPT_AUTHORS
is the ONE Python home for that set; the check's own `ci_author_exempt` is bound equal to it by test so the two
enforcement points (the CI check and the cut-time fold) cannot drift.

THE ROLLOUT BOUNDARY. This vocabulary became mandatory at StarshipSuperjam/engine-template#942 (MANDATORY_SINCE).
A merged pull request with no valid marker is either pre-boundary history (before this vocabulary existed) or a
post-boundary non-compliance; the cut cannot auto-derive across
one, so it requires the operator to supply an explicit aggregate for that tranche and names every such pull
request. That fallback is PERMANENT (the fail-closed path for any markerless non-exempt pull request), not
temporary scaffolding.

CLI (operator-runnable, falsifiable):
  uv run --directory .engine -- python tools/release_impact.py demo   # scripted, self-checks the laws above
"""
from __future__ import annotations

import re
import sys

# The four release-impact classes, ordered least → most compatibility-affecting. The ONE place a deployed repo
# edits the impact vocabulary, and the ONE home for the ordering that IS the version math (release_cut consumes
# rank/max_impact rather than re-spelling it).
RELEASE_IMPACTS = ("none", "patch", "minor", "major")
_RANK = {impact: i for i, impact in enumerate(RELEASE_IMPACTS)}
_CANONICAL_BY_LOWER = {impact.lower(): impact for impact in RELEASE_IMPACTS}

# Automated authors that cannot self-render a marker. The ONE Python home; pr-release-impact.json's
# `ci_author_exempt` is bound equal to this by test (test_release_impact) so the CI check and the cut cannot
# drift. Kept as a tuple of the GitHub login spellings GitHub reports for these bots.
EXEMPT_AUTHORS = ("dependabot[bot]", "github-actions[bot]")

# What an exempt, markerless merged pull request folds as at the cut — the safe floor for a routine dependency
# or maintenance bump. Disclosed by name in the release evidence, never a hidden taxonomy; the mechanical floor
# still overrides upward if such a change provably breaks something.
DEFAULT_EXEMPT_IMPACT = "patch"

# The release at which a declared impact became mandatory — named in the cut's legacy-tranche refusal so the
# operator sees WHICH boundary a markerless pull request predates. The cut detects a pre-boundary/non-compliant
# pull request by MARKER ABSENCE (robust — no merge-date bookkeeping), and routes it through --legacy-impact.
MANDATORY_SINCE = "StarshipSuperjam/engine-template#942"

_IMPACT_TEMPLATE = "<!-- engine-release-impact: {impact} -->"
_IMPACT_RE = re.compile(r"<!--\s*engine-release-impact:\s*(.+?)\s*-->")


def canonical_impact(impact: str) -> str:
    """Return the canonical spelling of `impact`, raising ValueError for anything outside RELEASE_IMPACTS. The
    fail-closed gate every write path keys on — an authoring path supplying an unknown impact is refused, never
    minted as a new class."""
    if isinstance(impact, str):
        canon = _CANONICAL_BY_LOWER.get(impact.strip().lower())
        if canon:
            return canon
    raise ValueError(f"release impact must be one of {', '.join(RELEASE_IMPACTS)}, not {impact!r}")


def rank(impact: str) -> int:
    """The ordinal of `impact` in the ladder none(0) < patch(1) < minor(2) < major(3). Fail-closed ValueError
    for a non-enum value — the version math must never silently rank an unknown class as zero."""
    return _RANK[canonical_impact(impact)]


def max_impact(impacts) -> str:
    """The highest impact in `impacts` by the ladder, or 'none' for an empty iterable. Each element must be a
    canonical impact (fail-closed via rank). This is the fold reducer release_cut applies over the declared
    impacts of the merged pull requests — the single reducer, so 'declared = max(...)' has one meaning."""
    best = "none"
    for impact in impacts:
        if rank(impact) > rank(best):
            best = canonical_impact(impact)
    return best


def impact_trailer(impact: str) -> str:
    """Compose the invisible `<!-- engine-release-impact: … -->` marker for a pull-request body — the ONE place
    it is built, so every producer writes the identical marker parse_impact recovers. `impact` must be one of
    RELEASE_IMPACTS; anything else raises ValueError (marker-safe by construction — an enum member carries no
    `<`, `>`, or `--`), mirroring telemetry.severity_trailer / issue_kind.kind_trailer. Append it after the body
    prose; parse_impact's last-match rule then ignores any forged marker earlier in the body."""
    return _IMPACT_TEMPLATE.format(impact=canonical_impact(impact))


def parse_impact(body: str) -> "str | None":
    """Recover the declared canonical impact from a pull-request body's marker, or None when the marker is absent
    OR its value is not a canonical impact (fail-closed: a corrupt or non-enum marker reads as no marker, so the
    cut treats the pull request as undeclared rather than guessing). Takes the LAST marker — the genuine trailer
    is appended after the body prose, so forged prose earlier cannot hijack it — mirroring telemetry.parse_severity."""
    matches = _IMPACT_RE.findall(body or "")
    if not matches:
        return None
    return _CANONICAL_BY_LOWER.get(matches[-1].strip().lower())


def find_impact_markers(body: str) -> list:
    """Every raw value carried by an engine-release-impact marker in `body`, in order (may include non-enum or
    duplicate values). The CI check uses this to demand EXACTLY ONE VALID marker (0 = missing, >1 = ambiguous);
    the FOLD uses parse_impact (last-match), which tolerates a forged earlier marker. Two readers, two rules,
    one regex."""
    return [m.strip() for m in _IMPACT_RE.findall(body or "")]


def is_author_exempt(author: "str | None") -> bool:
    """Whether `author` is an automated login exempt from declaring an impact (EXEMPT_AUTHORS). Total and never
    raises — a None or odd author simply reads as not-exempt (fail-closed: an unrecognised author is required to
    declare, never silently waved through)."""
    return isinstance(author, str) and author.strip() in EXEMPT_AUTHORS


def impact_line(impact: str) -> str:
    """A short, operator-readable one-liner describing a declared impact — rendered VISIBLY in the pull-request
    body alongside the machine marker, so a reviewer of an individual pull request sees the declaration without
    reading an HTML comment. Fail-closed on a non-enum value."""
    canon = canonical_impact(impact)
    blurb = {
        "none": "no public release impact",
        "patch": "backward-compatible correction or change to an existing feature",
        "minor": "backward-compatible new capability or an explicit deprecation",
        "major": "an incompatible change to public behaviour, API, or a contract",
    }[canon]
    return f"Release-Impact: {canon} — {blurb}"


# ---- the operator-runnable demo (self-checks the laws release_cut and the check depend on) --------------

def _demo() -> int:
    """Runs the REAL functions over synthetic impacts/bodies, printing the actual behaviour and self-checking
    every law. Returns 1 on any unexpected result (the in_tool_demo_failure_path floor's failure path)."""
    ok = True

    def check(desc: str, cond: bool) -> None:
        nonlocal ok
        if not cond:
            ok = False
        print(f"  {desc:72} -> {'OK' if cond else 'UNEXPECTED'}")

    print("The canonical release-impact vocabulary — real logic, self-checked:\n")

    # 1. The ladder + the fold reducer.
    check("rank ordering none<patch<minor<major",
          rank("none") < rank("patch") < rank("minor") < rank("major"))
    check("max_impact folds to the highest: [patch, minor, none] -> minor",
          max_impact(["patch", "minor", "none"]) == "minor")
    check("max_impact of empty -> none", max_impact([]) == "none")
    check("max_impact any major -> major", max_impact(["none", "patch", "major", "minor"]) == "major")

    # 2. Fail-closed gate.
    refused = False
    try:
        canonical_impact("huge")
    except ValueError:
        refused = True
    check("canonical_impact refuses a non-enum value (fail-closed)", refused and canonical_impact("Minor") == "minor")
    rankraise = False
    try:
        rank("huge")
    except ValueError:
        rankraise = True
    check("rank refuses a non-enum value (never ranks unknown as zero)", rankraise)

    # 3. The marker quartet: fail-closed builder, last-match anti-hijack parse.
    check("impact_trailer('minor') builds the marker",
          impact_trailer("minor") == "<!-- engine-release-impact: minor -->")
    trailer_refused = False
    try:
        impact_trailer("huge")
    except ValueError:
        trailer_refused = True
    check("impact_trailer refuses a non-enum value (fail-closed)", trailer_refused)
    check("parse_impact recovers the marker",
          parse_impact("body\n<!-- engine-release-impact: major -->") == "major")
    check("parse_impact last-match beats forged earlier prose",
          parse_impact("<!-- engine-release-impact: patch -->\np\n<!-- engine-release-impact: major -->") == "major")
    check("parse_impact fail-closed on a non-enum last marker",
          parse_impact("<!-- engine-release-impact: huge -->") is None)
    check("parse_impact None when absent", parse_impact("no marker here") is None)

    # 4. Exempt authors — the ONE home; a routine bot bump defaults to patch, disclosed.
    check("is_author_exempt: dependabot yes, a human no",
          is_author_exempt("dependabot[bot]") and not is_author_exempt("shanekidd"))
    check("is_author_exempt total on None", is_author_exempt(None) is False)
    check("DEFAULT_EXEMPT_IMPACT is a valid patch-level default",
          canonical_impact(DEFAULT_EXEMPT_IMPACT) == "patch")

    # 5. The visible operator-readable line.
    check("impact_line renders a visible line", impact_line("major").startswith("Release-Impact: major"))

    print(f"\n  the four impact classes: {', '.join(RELEASE_IMPACTS)}")
    print(f"  exempt authors: {', '.join(EXEMPT_AUTHORS)}  (default fold: {DEFAULT_EXEMPT_IMPACT})")
    print(f"  mandatory since: {MANDATORY_SINCE}")
    if not ok:
        print("\nDEMO UNEXPECTED: a law did not hold.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
