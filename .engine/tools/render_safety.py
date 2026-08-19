#!/usr/bin/env python3
"""The one render-safety boundary — neutralise an attacker-influenceable identifier before it is rendered
into a comment a person or a model reads.

WHY THIS EXISTS (single home). A repo-native identifier — a file path, a git branch name — is not free
prose, but it IS attacker-influenceable: a session can create a branch or a file whose *name* carries markup,
a code-span-terminating backtick, an autolink, or (inside a fenced JSON block) a fence-closing run of
backticks. Two surfaces now render such identifiers into a GitHub comment: `overlay_disclosure` (the
overwrite-disclosure comment) and `coordination_board` (the advisory coordination notice, StarshipSuperjam/engine-template#939). Rather
than copy a sanitizer into a second, unguarded surface, both call this one — so the injection boundary is
defined once and cannot drift between them.

WHAT IT DOES. `safe_ident` maps every character outside a conservative whitelist (letters, digits, dot,
underscore, slash, hyphen) to '?'. Real engine paths and branch names use only those, so it is lossless for
them; a crafted name is neutralised — no backtick can terminate a code span, no bracket/paren/angle-bracket
can form a link, and no backtick run can close a fenced block. Backslash-escaping is deliberately NOT used —
it has no effect inside a markdown code span (CommonMark). A length cap bounds a single pathological value.
This is a rendering boundary, not an authenticity check: it makes a hostile identifier inert on the page, it
does not decide whether the identifier is genuine (the receiver re-verifies canonical state for that).
"""
from __future__ import annotations

import re

# The conservative whitelist: exactly the characters real engine paths and git branch names use. Anything
# else becomes '?'. Kept deliberately narrow — a wider set to admit some exotic-but-valid path would reopen
# the injection surface this boundary exists to close.
_UNSAFE_IDENT_CHAR = re.compile(r"[^A-Za-z0-9._/-]")

# A single identifier is bounded so one pathological value cannot bloat a rendered line; the caller also caps
# how MANY identifiers it renders. 255 covers any real path/branch (the common filesystem component ceiling)
# with headroom, while refusing a megabyte-long crafted string.
MAX_IDENT_LEN = 255


def safe_ident(value: str, *, replacement: str = "?", max_len: int = MAX_IDENT_LEN) -> str:
    """A render-safe form of `value` (a path or branch name): every character outside the conservative
    whitelist becomes `replacement`, and the result is truncated to `max_len` (with a whitelist-safe
    `...TRUNCATED` marker when it was longer). Lossless for real engine identifiers; neutralising for a
    crafted one.

    `replacement` defaults to '?' — the historical marker for a rendered code span, where a non-whitelist
    placeholder is fine (a '?' cannot break a code span). A caller that ALSO stores the result in a
    whitelist-constrained field (the coordination notice's branch/path charset) passes a replacement that is
    itself in the whitelist (e.g. '_'), so the neutralised value still satisfies that field's pattern. The
    replacement must be exactly one character; picking one appropriate to the target surface is the caller's
    choice (both '?' and '_' are render-safe)."""
    if len(replacement) != 1:
        raise ValueError("replacement must be exactly one character")
    text = value if isinstance(value, str) else str(value)
    safe = _UNSAFE_IDENT_CHAR.sub(replacement, text)
    if len(safe) > max_len:
        # Truncate a pathological value so the RESULT never exceeds max_len — the marker is counted IN the
        # budget, not appended past it, so a caller that also enforces `max_len` as a hard field bound (the
        # coordination-notice branch/path charset) never rejects a value this function claims it made fit.
        # The marker is plain whitelist ASCII (dots + letters), so it carries no markup and stays in-charset.
        marker = "...TRUNCATED"
        keep = max(0, max_len - len(marker))
        safe = safe[:keep] + marker
    return safe


# Backwards-compatible alias for the path-specific caller (overlay_disclosure), whose own docstring speaks of
# "paths": a path is just an identifier under the same whitelist, so the two share this one implementation.
safe_path = safe_ident
