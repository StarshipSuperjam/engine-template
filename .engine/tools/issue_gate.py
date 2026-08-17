#!/usr/bin/env python3
"""The engine-Issue reroute gate — the matcher (modes registers it; this holds the logic).

WHAT THIS IS. A pure-logic matcher the Explore/Build PreToolUse hook (modes.handler) consults on every tool
call: when a session makes a DIRECT creation of an `engine`-labelled GitHub Issue — a Bash `gh`/API command, or
a connector issue-creation tool — this returns a plain redirect reason; modes wraps it in
hooks.decide("deny", reason) so the platform blocks the call and feeds the reason back to the session, which
re-files through the issue-authoring helper's `create` CLI. An unlabelled or non-engine Issue, every read /
list / view / comment / close, and anything the matcher cannot parse all return None → the call proceeds.

WHY EVERY ENGINE-LABELLED CREATION, NOT JUST A MALFORMED ONE. The helper now offers a supported create path
(`issue_author.py preview/create`) that resolves the correct TARGET repository from trusted config, applies the
`engine` label by construction, and renders the body in the engine's format. So the gate routes ALL direct
engine-Issue creation onto that one path — not only bodies that happen to look malformed. Three properties come
free once the filing goes through the helper: an input cannot steer the Issue off the engine's own channel
(trusted-target resolution), the label cannot be dropped by accident (applied by construction), and the body is
always legible. A hand-rolled `gh issue create --label engine` gets none of those, so it is rerouted regardless
of how its body reads.

THE CI BACKSTOP KEEPS THE BODY-SHAPE JOB. This in-session gate is best-effort and fail-open (below); the
fail-loud catch-all is the `on:issues` conformance workflow (`issue_conformance_ci.py`), which checks the
landed body against the contract MARKERS. Those markers live HERE as the single source that backstop imports
(`CONTRACT_MARKERS`), coupled to issue_author's real output by test_issue_conformance_ci — so an operator-facing
copy change to the framing or the headers breaks that test, never the backstop silently. This gate no longer
inspects the body itself (it reroutes on the creation + label alone); the markers remain the backstop's contract.

LABEL DETECTION IS PRECISE. Only a real `--label`/`-l`/`--label=`/`labels[]=` field carrying `engine`, never a
loose "any token containing both 'label' and 'engine'" (which would false-deny an innocent Issue whose body
merely says e.g. "relabel the engine room"). The connector arm reads the tool's structured `labels` field.

A NUDGE, NOT A WALL — best-effort and fail-open, stated honestly. The shell-string check is incomplete: an
alias / eval / substitution / a body assembled in a variable all evade it and resolve to None → ALLOW. It also
recognizes Issue CREATION only, not a later label edit: `gh issue edit <n> --add-label engine` adds the engine
label to an already-created Issue and is not routed (the Issue already exists and is scoped to the current
repo, so there is no target-redirection risk; its body is caught after the fact by the `on:issues` backstop).
The connector arm covers only GitHub issue-creation tools (a name ending `create_issue` and containing
`github`). The catch-all for everything the gate misses is the `on:issues` CI backstop; the only unbypassable
guarantee is the protected-branch merge. The helper's OWN create path files through a Python GitHub boundary
(not Bash, not a connector), so it is never caught by this gate.

SELF-CONTAINED RUNTIME. No network, no label application, no import of the helper at runtime (it holds no
producer roster).

CLI (operator-runnable demo; the live gate is what modes' wired hook invokes):
  uv run --directory .engine -- python tools/issue_gate.py demo   # a scripted allow/deny demonstration
"""
from __future__ import annotations

import re
import shlex
import sys

# The engine-domain label marking the channel the gate governs (telemetry.ENGINE_DOMAIN_LABEL). An Issue
# without it is ordinary backlog or a human/operator Issue, and is never gated.
ENGINE_LABEL = "engine"

# The body-contract markers the issue-authoring helper always emits (issue_author.py: the framing floor + the
# two required section headers). SINGLE SOURCE: the on:issues CI backstop imports these to check the LANDED
# body's shape, and test_issue_conformance_ci pins them to issue_author's real output. This in-session gate no
# longer inspects the body, but keeps the constant as the backstop's single source of truth.
CONTRACT_MARKERS = (
    "The engine opened this item",
    "**What this is.**",
    "**What happens next.**",
)

# The in-repo helper the redirect points at.
HELPER = ".engine/tools/issue_author.py"

# The redirect reason, surfaced to the session by modes.handler via hooks.decide. Names why the call was held,
# the supported create path (with its preview companion), AND the escape hatch (drop the label) — so a
# legitimate non-engine note that tripped the gate is never stranded.
DENY_REASON = (
    f"This directly creates an engine Issue — it carries the `{ENGINE_LABEL}` label. Engine Issues are filed "
    "through the engine's Issue helper, which resolves the correct target repository, applies the label by "
    "construction, and renders the body in the engine's format. File it through the helper instead:\n\n"
    f"    uv run --directory .engine -- python {HELPER} preview --input <file|->   # see exactly what will be filed\n"
    f"    uv run --directory .engine -- python {HELPER} create  --input <file|-> --confirm\n\n"
    "The input is the engine-issue-input.v1 shape (repository, title, what_this_is, whats_next, optional "
    "references/urgency). If you actually meant a plain personal note rather than an engine Issue, drop the "
    f"`{ENGINE_LABEL}` label and re-run."
)


# Shell command separators (as shlex emits them) after which a NEW command begins — so a verb counts only at
# the start of the command or just after one of these, never inside an echoed / grepped argument.
_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "("})


def _find_command(tokens: list[str], seq: tuple[str, ...]) -> bool:
    """True if `seq` appears as consecutive tokens AT COMMAND POSITION — the first token, or just after a
    shell separator — so `cd x && gh issue create …` matches but `echo gh issue create …` (the verb inside an
    argument) does not. Mirrors the write-gate's command-position discipline (modes._CMD_START)."""
    n = len(seq)
    for i in range(len(tokens) - n + 1):
        if tuple(tokens[i:i + n]) == seq and (i == 0 or tokens[i - 1] in _SEPARATORS):
            return True
    return False


def _is_issue_creation(tokens: list[str]) -> bool:
    """`gh issue create …`, or `gh api …/issues` with a write method or fields (an Issue body write). The
    `gh api` arm also matches a PATCH that SETS a body on an existing engine Issue — also a non-conforming-body
    write worth rerouting — so it is intentionally not POST-only."""
    if _find_command(tokens, ("gh", "issue", "create")):
        return True
    if _find_command(tokens, ("gh", "api")):
        joined = " ".join(tokens)
        if "/issues" in joined and re.search(
            r"(-X\s+POST|--method\s+POST|(?:^|\s)-[fF](?:\s|$)|--field|--raw-field|--input)", joined
        ):
            return True
    return False


def _label_value_carries_engine(value: str) -> bool:
    """A `--label`/field value is a single label or a comma-separated list; the engine label must be one of its
    members (so `--label engine` and `--label engine,bug` match, but `--label engineering` does not)."""
    return ENGINE_LABEL in [part.strip() for part in value.split(",")]


# The `gh api` field form, e.g. `-f 'labels[]=engine'` (shlex yields the token `labels[]=engine`) or
# `-f labels=engine`. Matches `label=`/`labels=`/`label[]=`/`labels[]=` and captures the value list. It does NOT
# match `--label=…` (that starts with `--`, handled by its own branch).
_API_LABEL_FIELD = re.compile(r"^labels?(\[\])?=(.*)$")


def _has_engine_label(tokens: list[str]) -> bool:
    """True iff the command carries the engine-domain label at a REAL label flag/field — never a loose substring
    match on body/title text (a `"label" in tok and "engine" in tok` clause would false-deny an innocent Issue
    whose prose merely mentioned both words)."""
    for i, tok in enumerate(tokens):
        if tok in ("--label", "-l") and i + 1 < len(tokens) and _label_value_carries_engine(tokens[i + 1]):
            return True
        if tok.startswith("--label=") and _label_value_carries_engine(tok.split("=", 1)[1]):
            return True
        m = _API_LABEL_FIELD.match(tok)
        if m and _label_value_carries_engine(m.group(2)):
            return True
    return False


def _connector_carries_engine(tool_input) -> bool:
    """True iff a connector issue-creation tool's structured input carries the engine label. The label field is
    a list of strings (`{"labels": ["engine", …]}`) or, defensively, a comma-string — mirroring the precise
    membership test the Bash arm uses, never a loose substring match on the title/body."""
    if not isinstance(tool_input, dict):
        return False
    labels = tool_input.get("labels")
    if isinstance(labels, str):
        return _label_value_carries_engine(labels)
    if isinstance(labels, (list, tuple)):
        return ENGINE_LABEL in [str(x).strip() for x in labels]
    return False


def _is_connector_issue_creation(tool_name) -> bool:
    """True for a connector GitHub issue-creation tool. Matches a name that ENDS in `create_issue` and carries
    `github` somewhere — so the real MCP GitHub server's `mcp__github__create_issue` (double-underscore harness
    naming defeats a literal `github_create_issue` suffix), a Composio `mcp__composio__github_create_issue`, and
    a bare `github_create_issue` all match, while an unrelated `jira_create_issue` does not."""
    if not isinstance(tool_name, str):
        return False
    lowered = tool_name.lower()
    return lowered.endswith("create_issue") and "github" in lowered


def reroute_reason(tool_name, tool_input) -> str | None:
    """The reroute decision for one tool call. Returns the redirect REASON string when the call is a DIRECT
    engine-labelled Issue creation (a Bash `gh`/API form, or a connector issue-creation tool); otherwise None
    (out of scope, not engine-labelled, or not inspectable → fail-open ALLOW). Pure and side-effect-free;
    modes.handler wraps a returned reason in hooks.decide("deny", reason)."""
    if _is_connector_issue_creation(tool_name):
        return DENY_REASON if _connector_carries_engine(tool_input) else None
    if tool_name != "Bash":
        return None
    command = ""
    if isinstance(tool_input, dict):
        command = tool_input.get("command") or ""
    if not isinstance(command, str) or not command:  # a non-str / absent command is not inspectable → allow
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None  # unparseable shell string (unbalanced quotes, etc.) — fail open
    if _is_issue_creation(tokens) and _has_engine_label(tokens):
        return DENY_REASON
    return None


# ---- the operator-runnable demo (the live gate is the wired modes hook) ----------------------


def _demo() -> int:
    """A scripted demonstration over the REAL reroute_reason: an engine-labelled creation (Bash inline, Bash
    heredoc, `gh api`, and a connector tool) is rerouted regardless of body shape; an unlabelled or
    different-labelled creation, a mere mention of "engine", and a non-creation are allowed. Self-checks and
    returns 1 on any unexpected verdict (the failure path)."""
    def verdict(tool_name: str, tool_input) -> str:
        return "REROUTE" if reroute_reason(tool_name, tool_input) else "ALLOW"

    heredoc = "gh issue create --label engine --body-file - <<'EOF'\njust some free text\nEOF"
    conforming = (
        "*The engine opened this item itself — you didn't create it.*\n\n"
        "**What this is.** A demo item.\n\n**What happens next.** Nothing.")
    cases = [
        ("engine label + free-text body (Bash inline)", "Bash", {"command": 'gh issue create --label engine -b "free text"'}, "REROUTE"),
        ("engine label + CONFORMING body (still rerouted)", "Bash", {"command": f'gh issue create --label engine -b {shlex.quote(conforming)}'}, "REROUTE"),
        ("engine label via gh api field", "Bash", {"command": "gh api repos/o/r/issues -f 'labels[]=engine' -f title=x"}, "REROUTE"),
        ("engine label (heredoc on stdin)", "Bash", {"command": heredoc}, "REROUTE"),
        ("NO engine label", "Bash", {"command": 'gh issue create -b "free text"'}, "ALLOW"),
        ("a different label", "Bash", {"command": 'gh issue create --label bug -b "free text"'}, "ALLOW"),
        ("body merely MENTIONS engine", "Bash", {"command": 'gh issue create -b "please relabel the engine room"'}, "ALLOW"),
        ("not a creation (gh issue comment)", "Bash", {"command": "gh issue comment 5 --body whatever"}, "ALLOW"),
        ("connector create_issue + engine label", "mcp__github__github_create_issue", {"title": "x", "labels": ["engine"]}, "REROUTE"),
        ("connector create_issue, no engine label", "mcp__github__github_create_issue", {"title": "x", "labels": ["bug"]}, "ALLOW"),
    ]
    print("The engine-Issue reroute gate — what it decides for each call (this runs the real matcher):\n")
    ok = True
    for label, tool_name, tool_input, expected in cases:
        got = verdict(tool_name, tool_input)
        flag = "" if got == expected else "  <- UNEXPECTED"
        if got != expected:
            ok = False
        print(f"  {label:50} -> {got}{flag}")
    print("\nA REROUTE feeds the session this redirect (it is NOT shown to the operator):\n")
    print("    " + DENY_REASON.replace("\n", "\n    "))
    if not ok:
        print("\nDEMO UNEXPECTED: a call did not get the verdict the gate's contract promises.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
