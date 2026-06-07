#!/usr/bin/env python3
"""The shared issue-authoring helper — assembles every engine-authored Issue body to the one
control-plane body contract (control-plane/README §"Engine Issues" -> "Every engine-authored Issue
carries a body contract").

WHY THIS EXISTS. The engine creates Issues programmatically (telemetry health findings, build Issues,
tracked debt). Those bypass the human web issue templates entirely — templates populate only the web
"New issue" form, while the REST / gh creation path sets the body directly. GitHub cannot gate Issue
*creation* the way a required check gates a merge, so the body contract is enforced **by
construction**: every producer assembles its body through this one helper, which builds the contract's
parts from required arguments — a producer that authors through it cannot omit a part. Authoring *via*
the helper is posture (principles §6/§7); a producer that bypasses it emits a less-legible body, which
costs legibility, never a guardrail (so §15 does not bite).

THE BODY CONTRACT — a loose structural skeleton, in plain language (control-plane):
  (1) what the Issue is and why it is here                  -> `what_this_is`  (required)
  (2) what the operator must decide, or what happens next   -> `whats_next`    (required)
  (3) any backstage references, as plain links a person can follow, never a bare id dump
                                                            -> `references`    (optional)
Item (1) is bound to the operator-communication law directly: the helper prepends a fixed plain
framing every engine-authored Issue carries, so a producer not yet written inherits a plainness floor
rather than only the example of the contracts it fills. The shape's presence is the floor; its
truthfulness is posture (the PR-contract tiering, carried over).

PASSIVE FORMATTER, NOT A REGISTRY (principles §14/§16). This is shared code each producer *calls*; it
makes no network calls, applies no label, and holds no roster of producers. The engine-domain label is
applied by each producer's own GitHub boundary (an explicit `labels` value at creation, or a label
call right after — never a web-only issue-template default, which the programmatic path bypasses). The
product-design spec Issue is the named exception: its body is the D-141 plain-prose specification, a
different realization of the same channel, not authored through this helper.

CLI (operator-runnable demo):
  uv run --directory .engine -- python tools/issue_author.py demo
"""
from __future__ import annotations

import sys

# The plainness floor: the one fixed, plain line every engine-authored Issue carries for contract
# part (1), so a future producer inherits a plain framing by construction (control-plane). It states
# only what is universally true of an engine-authored Issue — the engine opened it, the operator did
# not — and carries no backstage vocabulary.
_FRAMING = "*The engine opened this item itself — you didn't create it.*"


def _require(name: str, value: str) -> str:
    """A required contract part must be a present, non-blank string. Omitting the argument entirely
    already raises TypeError at the call boundary (the parameters are keyword-only with no default);
    this guards the present-but-empty case so the contract cannot be satisfied with whitespace."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"engine-authored Issue body part '{name}' must be a non-empty string")
    return value.strip()


def _render_references(references) -> str:
    """Part (3): backstage references as plain markdown links a person can follow — never a bare id
    dump. Each reference is a (label, url) pair; both must be non-blank, so no naked id or unlabelled
    URL is emitted. Absent/empty -> no references block (the part is optional)."""
    if not references:
        return ""
    lines = []
    for ref in references:
        # A reference is a (label, url) PAIR — explicitly a 2-element tuple/list, never a bare string
        # (a 2-char string would otherwise unpack to two characters and emit a malformed link).
        if isinstance(ref, str) or not isinstance(ref, (tuple, list)) or len(ref) != 2:
            raise ValueError("each reference must be a (label, url) pair")
        label, url = ref
        if not str(label).strip() or not str(url).strip():
            raise ValueError("a reference needs both a human label and a url (never a bare id dump)")
        lines.append(f"- [{str(label).strip()}]({str(url).strip()})")
    return "\n\n**More detail.**\n" + "\n".join(lines)


def render_engine_issue_body(*, what_this_is: str, whats_next: str, references=None) -> str:
    """Assemble an engine-authored Issue body to the control-plane body contract.

    Keyword-only and required: omitting `what_this_is` or `whats_next` raises TypeError at the call
    boundary (the by-construction enforcement — a producer cannot omit a part); a present-but-blank
    value raises ValueError. `references` is an optional list of (label, url) pairs rendered as plain
    markdown links. Returns the body string; the calling producer applies the engine-domain label and
    appends any producer-specific trailer (e.g. a tracking marker) itself — this helper never calls
    GitHub and never applies a label."""
    what_this_is = _require("what_this_is", what_this_is)
    whats_next = _require("whats_next", whats_next)
    return (
        f"{_FRAMING}\n\n"
        f"**What this is.** {what_this_is}\n\n"
        f"**What happens next.** {whats_next}"
        f"{_render_references(references)}\n"
    )


def _demo() -> None:
    print("ISSUE-AUTHORING HELPER DEMO — one body assembled from the contract's parts.\n")
    body = render_engine_issue_body(
        what_this_is=("The engine noticed one of its own checks has been unable to run for the last "
                      "few sessions. This is about the engine's machinery, not your project."),
        whats_next=("Usually nothing right now — the engine will propose a fix in a later session "
                    "under the same review-and-merge step you already use."),
        references=[("The check's last run", "https://github.com/owner/repo/actions/runs/123")],
    )
    print(body)
    print("--- a call omitting a required part cannot run ---")
    try:
        render_engine_issue_body(what_this_is="only one part supplied")  # type: ignore[call-arg]
    except TypeError as exc:
        print(f"TypeError (by construction): {exc}")
    print("\n--- a present-but-blank part cannot run ---")
    try:
        render_engine_issue_body(what_this_is="   ", whats_next="x")
    except ValueError as exc:
        print(f"ValueError (by construction): {exc}")
    print("\n--- a bare id (no label/url) is refused as a reference ---")
    try:
        render_engine_issue_body(what_this_is="x", whats_next="y", references=[("", "rule:abc")])
    except ValueError as exc:
        print(f"ValueError (no bare id dump): {exc}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        _demo()
    else:
        print(__doc__)
