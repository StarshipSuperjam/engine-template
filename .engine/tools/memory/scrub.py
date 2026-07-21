"""Capture-time secret redaction — the one sanctioned mutation of an otherwise-verbatim archive.

Memory is a transcript-first archive whose whole value is the EXACT conversation (eADR-0038); recall
depends on it. So this redactor is deliberately PRECISION-BIASED, not recall-biased: it redacts only
credential shapes that anchor on a structural constant a normal sentence essentially never contains —
a vendor prefix, a fixed delimiter, a rigid multi-segment grammar. It never uses an entropy heuristic,
because a bare high-entropy string is indistinguishable from a git SHA, a UUID, a checksum, or a nonce
we legitimately discuss, and redacting those would shred everyday engineering prose. A false positive
here PERMANENTLY destroys unrecoverable memory (capture is the only record), so under-redacting a rare
exotic token is the correct trade against corrupting real conversation.

Deliberately OUT of scope (a surfaced boundary, not an oversight): bare high-entropy strings; a generic
`password=`/`secret=` sitting in prose (no vendor anchor — "the database password lives in the vault"
must survive verbatim); personal names; and PII — EMAILS and PHONE NUMBERS are intentionally left
intact. The operator asked for password/key/token-shaped content; eADR-0038 says "secret-shaped." An
email is not a credential, is extremely common in legitimate conversation, and is often the very anchor
a later session searches on — redacting it trades a large recall loss for a benefit no one requested.
PII redaction, if ever wanted, is a separate opt-in decision with its own record, never folded in here.

This module is pure, offline, and standard-library-only (eADR-0004: no outbound calls, no third-party
deps). `scrub_text` is deterministic, idempotent (`scrub_text(scrub_text(t)) == scrub_text(t)`), and
NEVER raises (capture is fail-soft, capture.py:756) — on any internal fault it returns the input
unchanged, biasing to under-redaction over corruption or a crash. It is defense-in-depth, not a wall:
a novel secret shape can pass un-redacted, and the real protection stays the gitignored local store and
the private backup vault.
"""

import re

# Each entry: (compiled pattern, replacement). Order matters where one shape is a prefix of another —
# the more specific pattern (Anthropic sk-ant-, Stripe sk_live_) is listed BEFORE the generic sk-, so
# it wins the first redaction and the generic pattern then finds nothing to match. The PEM multiline
# block is applied first so its inner base64 can never be half-caught by a single-line pattern.
_PATTERNS = [
    # PEM private-key armor — redact the WHOLE block as one unit (the paired BEGIN/END lines are a
    # globally reserved format; non-greedy body bounded by the END armor, no catastrophic backtracking).
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
                re.DOTALL), "[redacted:private-key]"),
    # JWT — anchored on TWO `eyJ` segments (base64url of `{"`), which a random dotted token won't have.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
     "[redacted:jwt]"),
    # AWS access key id — the AKIA-family prefix + exactly 16 more upper/digit chars = a minted shape.
    (re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[0-9A-Z]{16}\b"), "[redacted:aws-key]"),
    # GitHub fine-grained PAT (literal prefix) — before the generic ghp_ family for clarity.
    (re.compile(r"\bgithub_pat_[0-9A-Za-z_]{22,}\b"), "[redacted:github-token]"),
    # GitHub tokens — vendor prefix + underscore + long base62.
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{36,}\b"), "[redacted:github-token]"),
    # Anthropic — literal sk-ant-, BEFORE the generic sk- so it wins.
    (re.compile(r"\bsk-ant-[0-9A-Za-z_-]{20,}\b"), "[redacted:anthropic-key]"),
    # Stripe — the _live_/_test_ infix is unmistakable; BEFORE the generic sk-.
    (re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{16,}\b"), "[redacted:stripe-key]"),
    # OpenAI / generic sk- — prefix + >=20 token chars (a short "sk-" word won't match).
    (re.compile(r"\bsk-(?:proj-)?[0-9A-Za-z_-]{20,}\b"), "[redacted:openai-key]"),
    # Slack — xox + kind letter + dashes.
    (re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"), "[redacted:slack-token]"),
    # Google OAuth client id — literal domain suffix (before the AIza key so both are distinct).
    (re.compile(r"\b[0-9]+-[0-9a-z]{20,}\.apps\.googleusercontent\.com\b"), "[redacted:google-oauth]"),
    # Google API key — AIza + exactly 35 chars = Google's fixed shape.
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[redacted:google-key]"),
    # Authorization header — keep the scheme scaffold, redact only the credential.
    (re.compile(r"(?i)(\bauthorization\s*:\s*(?:bearer|basic|token)\s+)[0-9A-Za-z._+/=~-]{8,}"),
     r"\1[redacted:auth-credential]"),
    # Credentials embedded in a URL authority — redact only the `user:pass@` userinfo, keep proto+host.
    (re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/:@]+:[^\s/:@]+@"), r"\1[redacted:url-credential]@"),
]


def scrub_text(text):
    """Redact high-confidence secret/credential shapes from a captured turn's text, replacing each with
    a typed, idempotent placeholder (`[redacted:<kind>]`).

    PRECISION-BIASED: only anchored, vendor/format-specific shapes are redacted; bare high-entropy
    strings, generic `password=` in prose, names, emails, and phone numbers are DELIBERATELY left intact
    — a false positive permanently destroys unrecoverable verbatim memory (eADR-0038). Pure, offline,
    stdlib-only, no I/O, deterministic. Idempotent: `scrub_text(scrub_text(t)) == scrub_text(t)` (the
    `[redacted:...]` placeholder matches none of the patterns). NEVER raises — on any internal fault the
    input is returned unchanged, biasing to under-redaction over corruption or a crash."""
    if not text:
        return text
    try:
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text
    except Exception:  # noqa: BLE001 — capture is fail-soft; never corrupt or crash on a redaction fault
        return text
