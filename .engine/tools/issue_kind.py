#!/usr/bin/env python3
"""The canonical issue-kind vocabulary — the ONE source of truth for an engine change-request Issue's kind.

WHY THIS EXISTS. An engine-authored Issue that requests a change carries a `Kind:` title prefix — `Feature`,
`Fix`, `Improvement`, `Maintenance`, `Security`, `Removal`. Left as prose, that prefix drifts (`Bug`,
`Engine fault`, `Defect`, `Architecture`, or none), so the Issue list reads inconsistently and needs manual
cleanup. This module makes the kind STRUCTURED DATA and the prefix a PROJECTION of it: the authoring helper
renders `<Kind>: <title>` from a kind and stamps an invisible `<!-- engine-kind: … -->` body marker; the
on:issues reconciler repairs a drifted title back to the marker's authoritative kind. The invariant: the kind
is data; the prefix is presentation — humans and AI do not independently author the prefix.

A STDLIB-ONLY LEAF. Imported by the authoring helper, the on:issues reconciler (a CI hot path), the one-time
backfill, AND release_cut. It imports nothing but `re`, so it never drags the module-manager/coherence stack
onto the reconciler's per-Issue path — the same discipline the reconciler already applies by refusing to import
release_cut. This is the single edit point for the kind vocabulary; release_cut references KINDS for its
release-note grouping rather than spelling the set a second time.

THE MARKER (mirrors telemetry's severity trailer). `kind_trailer` is the ONE place `<!-- engine-kind: … -->`
is built; it validates against KINDS and raises otherwise, so the value is marker-safe by construction (an
enum member carries no `<`, `>`, or `--`) — never free text. `parse_kind` takes the LAST marker so body prose
forged before the genuine trailer cannot hijack it. HONEST RESIDUAL: last-match does not defend a marker
appended AFTER the genuine one by anyone who can edit the body — INCLUDING the Issue's own (untrusted) author,
who can pre-plant a dormant marker before the `engine` label exists and have it honoured once a maintainer
later adds that label. That is bounded because the value is enum-closed: a wrong-but-in-enum kind yields only a
self-healing wrong title prefix + native label — a cosmetic effect, never a consent, merge, or gate effect.

NORMALISED, IDEMPOTENT TITLES (the reconciler's loop-safety). `render_title` emits a normalised string —
exactly one space after the colon, no surrounding whitespace — so its output is a fixed point of GitHub's own
title normalisation (GitHub trims stored titles) AND of this module: `render(k, render(k, d)) == render(k, d)`
— re-rendering an already-rendered title is a no-op, because the output always leads with the canonical `Kind:`
slot. That idempotence is what lets the reconciler write `render(k, live_title)` and be sure a second pass over
the result writes nothing, so an unrelated Issue edit never triggers a spurious re-titling. NOTE: render/split
strip only the SINGLE leading kind slot. A title with STACKED recognised prefixes (`Bug: Feature: x` — an
unusual MANUAL edit; the helper never emits one, since it strips on render) is repaired to a canonical LEADING
prefix with the inner token left as description (`Improvement: Feature: x`); this still converges in one pass.
Recursing into the description is deliberately avoided — it would eat a legitimately-descriptive `Removal:`-style
token (data loss traded for a rare cosmetic edge).

CLI (operator-runnable, falsifiable):
  uv run --directory .engine -- python tools/issue_kind.py demo   # scripted, self-checks the laws above
"""
from __future__ import annotations

import re
import sys

# The canonical kinds — the recognised set an engine change-request Issue may carry. The one place a deployed
# repo edits to change its issue-kind vocabulary. release_cut references this for its release-note grouping.
KINDS = ("Feature", "Fix", "Improvement", "Maintenance", "Security", "Removal")
_CANONICAL_BY_LOWER = {k.lower(): k for k in KINDS}

# Unambiguous legacy aliases → canonical kind, for the one-time backfill of MARKER-LESS legacy Issues only. The
# mapping is beyond doubt here (the intent names Bug/Defect/Engine fault → Fix). Ambiguous historical prefixes
# (Architecture, Memory integrity, Docs, Question) are deliberately ABSENT: the backfill leaves them unchanged
# rather than guess a classification.
ALIASES = {
    "bug": "Fix",
    "defect": "Fix",
    "engine fault": "Fix",
}

# The CLOSED set of leading `<Prefix>:` tokens the reconciler recognises as a KIND SLOT to strip when repairing
# a marker-carrying engine Issue's title — the canonical kinds PLUS the named non-canonical kind prefixes seen
# in practice. An invented prefix in this set (`Architecture:`) is repaired to the marker's kind; an
# UNRECOGNISED leading `Word:` (e.g. `parser:`) is preserved untouched (never guessed), so a descriptive token
# that happens to end in a colon is never eaten. Distinct from ALIASES: recognition here only decides "this slot
# is the kind presentation, strip it" — the authoritative kind comes from the marker, so no mapping is needed.
# RESIDUAL (honest): a brand-new invented prefix NOT in this set is preserved as description, not stripped
# (safe over clever) — an operator can add it here or re-author the Issue.
_RECOGNISED_PREFIXES = frozenset(_CANONICAL_BY_LOWER) | {
    "bug", "defect", "engine fault", "fault",
    "architecture", "memory integrity", "docs", "documentation", "question",
}

# The GitHub-native label each canonical kind projects to — a SECONDARY projection OF the canonical kind (the
# kind is the source of truth, never the label). Maps onto only the four labels GitHub ships in every repo
# (eADR-0021: mint nothing). Maintenance and Removal have no fitting native label → None (no label applied),
# matching the legacy applicator's own None for them.
_NATIVE_BY_KIND = {
    "Feature": "enhancement",
    "Improvement": "enhancement",
    "Fix": "bug",
    "Security": "bug",
    "Maintenance": None,
    "Removal": None,
}

# A leading `<token>:` split. `token` is everything up to the FIRST colon (so a multi-word kind like
# `Engine fault` matches); the remainder is everything after it. Case is decided by str.lower() against the
# recognised set, NOT re.I — re.I case-folds wider than str.lower() (Turkish dotless-i, long-s), which would
# make a title's mere spelling flip recognition; str.lower() keeps recognition deterministic and narrow (an
# exotic-cased prefix simply reads as unrecognised and is preserved).
_PREFIX_RE = re.compile(r"^\s*([^:]+?)\s*:\s*(.*)$", re.DOTALL)


def canonical_kind(kind: str) -> str:
    """Return the canonical spelling of `kind` (case-normalised), raising ValueError for anything outside KINDS.
    The fail-closed gate every write path keys on — an authoring path that supplies an unknown kind is refused,
    never minted as a new category."""
    if isinstance(kind, str):
        canon = _CANONICAL_BY_LOWER.get(kind.strip().lower())
        if canon:
            return canon
    raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")


def split_title(title: str) -> "tuple[str | None, str]":
    """Split a title into (recognised_canonical_kind_or_None, descriptive_remainder).

    Strips a single leading `<Prefix>:` ONLY when `<Prefix>` is a recognised kind slot (a canonical kind or a
    named legacy/invented prefix); an unrecognised leading `Word:` is preserved as part of the remainder (never
    guessed). The first element is the CANONICAL kind the leading prefix denotes when that prefix is itself
    canonical (e.g. `Fix:` → `Fix`), else None (an invented-but-recognised prefix like `Architecture:` strips
    but denotes no canonical kind — the marker supplies it). The remainder is what render_title re-prefixes."""
    if not isinstance(title, str):
        return (None, "")
    m = _PREFIX_RE.match(title)
    if m and m.group(1).strip().lower() in _RECOGNISED_PREFIXES:
        return (_CANONICAL_BY_LOWER.get(m.group(1).strip().lower()), m.group(2).strip())
    return (None, title.strip())


def render_title(kind: str, descriptive: str) -> str:
    """Render the normalised canonical title `<Kind>: <descriptive>`. `kind` must be one of KINDS (fail-closed
    ValueError via canonical_kind). Output is normalised — exactly one space after the colon, no surrounding
    whitespace — so it is a fixed point of GitHub's own title trim and IDEMPOTENT under this function
    (`render(k, render(k, x)) == render(k, x)`, since the output leads with the canonical slot). `descriptive`
    is the bare remainder; the SINGLE leading recognised kind prefix it carries is stripped first (defensive: a
    caller passing an already-prefixed title never yields `Fix: Fix: …`), while an unrecognised leading `Word:`
    — and any SECOND stacked recognised prefix — is preserved as description. An empty remainder renders the
    bare `<Kind>:`."""
    canon = canonical_kind(kind)
    _, remainder = split_title(descriptive if isinstance(descriptive, str) else "")
    return f"{canon}: {remainder}" if remainder else f"{canon}:"


_KIND_TEMPLATE = "<!-- engine-kind: {kind} -->"
_KIND_RE = re.compile(r"<!--\s*engine-kind:\s*(.+?)\s*-->")


def kind_trailer(kind: str) -> str:
    """Compose the invisible `<!-- engine-kind: … -->` marker for an Issue body — the ONE place it is built, so
    every producer writes the identical marker parse_kind recovers. `kind` must be one of KINDS; anything else
    raises ValueError (marker-safe by construction — an enum member carries no `<`, `>`, or `--`), mirroring
    telemetry.severity_trailer's fail-closed discipline. Append it after the body prose; parse_kind's last-match
    rule then ignores any forged marker earlier in the body."""
    return _KIND_TEMPLATE.format(kind=canonical_kind(kind))


def parse_kind(body: str) -> "str | None":
    """Recover the authoritative canonical kind from an Issue body's engine-kind marker, or None when the marker
    is absent OR its value is not a canonical kind (fail-closed: a corrupt or non-enum marker reads as no marker,
    so the reconciler no-ops rather than guessing). Takes the LAST marker — the genuine trailer is appended after
    the body prose, so forged prose earlier cannot hijack it — mirroring telemetry.parse_severity."""
    matches = _KIND_RE.findall(body or "")
    if not matches:
        return None
    return _CANONICAL_BY_LOWER.get(matches[-1].strip().lower())


def alias_target(title: str) -> "str | None":
    """The canonical kind an UNAMBIGUOUS legacy alias prefix maps to (ALIASES: Bug/Defect/Engine fault → Fix),
    or None when the title's leading prefix is not an unambiguous alias — already canonical, ambiguous
    (Architecture/Memory integrity/Docs/Question), or no prefix at all. The one-time backfill's mapping,
    single-sourced here so the tool and this module can never disagree; it never guesses a classification."""
    if not isinstance(title, str):
        return None
    m = _PREFIX_RE.match(title)
    if not m:
        return None
    return ALIASES.get(m.group(1).strip().lower())


def native_label_for_kind(kind: str) -> "str | None":
    """The GitHub-native label a canonical kind projects to (a secondary projection of the kind), or None for
    Maintenance/Removal or a non-canonical input. Total and never raises, so the reconciler's CI hot path cannot
    crash on an odd value — an unknown kind simply projects to no label."""
    canon = _CANONICAL_BY_LOWER.get(kind.strip().lower()) if isinstance(kind, str) else None
    return _NATIVE_BY_KIND.get(canon)


# ---- the operator-runnable demo (self-checks the laws the reconciler depends on) --------------

def _demo() -> int:
    """Runs the REAL functions over synthetic titles/bodies, printing the actual behaviour and self-checking
    every law. Returns 1 on any unexpected result (the in_tool_demo_failure_path floor's failure path)."""
    ok = True

    def check(desc: str, cond: bool) -> None:
        nonlocal ok
        if not cond:
            ok = False
        print(f"  {desc:70} -> {'OK' if cond else 'UNEXPECTED'}")

    print("The canonical issue-kind vocabulary — real logic, self-checked:\n")

    # 1. render_title normalises and prefixes.
    check("render_title('Fix', 'quote the hook path') == 'Fix: quote the hook path'",
          render_title("Fix", "quote the hook path") == "Fix: quote the hook path")
    check("render_title normalises odd spacing to one space, no trailing ws",
          render_title("Fix", "  x  ") == "Fix: x" and render_title("Fix", "Fix:   x") == "Fix: x")

    # 2. THE IDEMPOTENCE LAW — render(k, render(k, d)) == render(k, d) over adversarial remainders (the
    # reconciler's convergence guarantee; holds even for stacked recognised prefixes, single-stripped).
    for d in ["x", "  x  ", "", "Feature: do X", "parser: handle nested", "a  b", "Fıx: exotic", "café: x",
              "Bug: Feature: stacked", "Fix: Fix: doubled"]:
        once = render_title("Improvement", d)
        check(f"idempotence holds for remainder {d!r}", render_title("Improvement", once) == once)

    # 3. split_title strips a recognised prefix, PRESERVES an unrecognised one (never eats a descriptive token).
    check("split strips a recognised invented prefix: 'Architecture: example' -> 'example'",
          split_title("Architecture: example") == (None, "example"))
    check("split strips a canonical prefix and reports it: 'Fix: x' -> (Fix, 'x')",
          split_title("Fix: x") == ("Fix", "x"))
    check("split PRESERVES an unrecognised prefix: 'parser: handle' stays whole",
          split_title("parser: handle") == (None, "parser: handle"))

    # 4. The reconciler's one-call repair: render_title(marker_kind, current_title).
    check("repair invented prefix: (Improvement, 'Architecture: example') -> 'Improvement: example'",
          render_title("Improvement", "Architecture: example") == "Improvement: example")
    check("repair missing prefix: (Improvement, 'example') -> 'Improvement: example'",
          render_title("Improvement", "example") == "Improvement: example")
    check("already-canonical is a no-op fixed point: (Improvement, 'Improvement: example') unchanged",
          render_title("Improvement", "Improvement: example") == "Improvement: example")
    check("never eats a descriptive colon-token: (Fix, 'parser: handle') -> 'Fix: parser: handle'",
          render_title("Fix", "parser: handle") == "Fix: parser: handle")

    # 5. The marker quartet: fail-closed builder, last-match anti-hijack parse.
    check("kind_trailer('Fix') builds the marker", kind_trailer("Fix") == "<!-- engine-kind: Fix -->")
    refused = False
    try:
        kind_trailer("Architecture")   # not a canonical kind
    except ValueError:
        refused = True
    check("kind_trailer refuses a non-canonical kind (fail-closed)", refused)
    check("parse_kind recovers the marker", parse_kind("body\n<!-- engine-kind: Security -->") == "Security")
    check("parse_kind last-match beats forged earlier prose",
          parse_kind("<!-- engine-kind: Removal -->\nprose\n<!-- engine-kind: Fix -->") == "Fix")
    check("parse_kind fail-closed on a non-enum last marker", parse_kind("<!-- engine-kind: bogus -->") is None)
    check("parse_kind None when absent", parse_kind("no marker here") is None)

    # 6. native projection is a secondary view of the kind (eADR-0021: only the four natives; None for two).
    check("native_label_for_kind: Fix->bug, Feature->enhancement, Maintenance->None, Removal->None",
          native_label_for_kind("Fix") == "bug" and native_label_for_kind("Feature") == "enhancement"
          and native_label_for_kind("Maintenance") is None and native_label_for_kind("Removal") is None)

    # 7. canonical_kind is the fail-closed gate.
    okraise = False
    try:
        canonical_kind("nope")
    except ValueError:
        okraise = True
    check("canonical_kind refuses an unknown kind", okraise and canonical_kind("fix") == "Fix")

    print(f"\n  the six canonical kinds: {', '.join(KINDS)}")
    print(f"  unambiguous backfill aliases: {', '.join(f'{a} -> {k}' for a, k in ALIASES.items())}")
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
