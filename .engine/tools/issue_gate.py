#!/usr/bin/env python3
"""The engine-Issue reroute gate — the matcher (modes registers it; this holds the logic).

WHAT THIS IS. A pure-logic matcher the Explore/Build PreToolUse hook (modes.handler) consults on every tool
call: when a session makes a recognized direct GitHub Issue creation for an Engine-labelled Issue or a trusted
Engine repository — a Bash `gh`/API command, or a connector issue-creation tool — this returns a plain redirect reason; modes wraps it in
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

SELF-CONTAINED RUNTIME. No network and no label application occur here. The matcher reads the helper's trusted
repository configuration from the session checkout only; command text and a command-selected checkout never
extend that set.

CLI (operator-runnable demo; the live gate is what modes' wired hook invokes):
  uv run --directory .engine -- python tools/issue_gate.py demo   # a scripted allow/deny demonstration
"""
from __future__ import annotations

import re
import shlex
import sys
import os

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
    "This directly creates an Engine-scoped Issue. Route it through the Issue helper; preview first, then "
    "create with the explicit confirmation:\n\n"
    f"    uv run --directory .engine --frozen -- python {HELPER} preview --input <file|->\n"
    f"    uv run --directory .engine --frozen -- python {HELPER} create --input <file|-> --confirm\n\n"
    "Use the issue-submission-input.v1 envelope. Engine scope requires its assessed request, including kind, "
    "submission_id and assessment. Product scope uses ordinary request fields and must not carry the `engine` "
    "label. The helper validates the target and does not use a connector fallback when credentials are missing."
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


def _commands(tokens: list[str]):
    """Yield command-local token groups. Flags never cross a shell boundary."""
    current = []
    for token in tokens:
        if token in _SEPARATORS:
            if current:
                yield current
            current = []
        else:
            current.append(token)
    if current:
        yield current


def _shell_tokens(command: str) -> list[str]:
    """Tokenize shell punctuation separately while retaining quoted arguments as one token."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    return list(lexer)


def _option_value(tokens: list[str], names: tuple[str, ...]) -> str | None:
    for i, token in enumerate(tokens):
        if token in names and i + 1 < len(tokens):
            return tokens[i + 1]
        for name in names:
            if token.startswith(name + "="):
                return token.split("=", 1)[1]
    return None


def _is_dynamic(value: str) -> bool:
    return any(mark in value for mark in ("$", "`", "$(`", "${"))


def _parse_repo(value: str) -> str | None:
    if _is_dynamic(value):
        return None
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|github\.com[:/])?"
        r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:\.git)?/?", value)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def _explicit_repo(tokens: list[str]) -> tuple[bool, str | None]:
    value = _option_value(tokens, ("-R", "--repo"))
    present = any(token in ("-R", "--repo") or token.startswith("--repo=") for token in tokens)
    if not present:
        return False, None
    return True, _parse_repo(value) if value else None


_ISSUE_COLLECTION = re.compile(r"^/?repos/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/?$")


def _api_issue_target(tokens: list[str]) -> str | None:
    """Read gh api's endpoint positional argument, never an option value such as --input."""
    skip_next = False
    for token in tokens[2:]:
        if skip_next:
            skip_next = False
            continue
        if token in ("-X", "--method", "-f", "-F", "--field", "--raw-field", "--input", "-H"):
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        match = _ISSUE_COLLECTION.match(token)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
        return None
    return None


def _is_post_creation(tokens: list[str]) -> bool:
    method = _option_value(tokens, ("-X", "--method"))
    if method is not None:
        return method.upper() == "POST"
    return any(token in ("-f", "-F", "--field", "--raw-field", "--input")
               or token.startswith(("--field=", "--raw-field=", "--input=")) for token in tokens)


def _gh_subcommand(tokens: list[str]) -> list[str] | None:
    """Return gh's subcommand words after its supported command-directory prefix."""
    if tokens[:1] != ["gh"]:
        return None
    index = 1
    # Preserve `gh -C checkout issue create`: its directory is considered only for target
    # resolution later, never as a source of trusted repositories.
    if index + 1 < len(tokens) and tokens[index] == "-C":
        index += 2
    return tokens[index:]


def _command_creation(tokens: list[str]):
    """Return `(target, labelled, unresolved_target)` for one supported creation, else None.

    A missing target is deliberate: its caller may resolve the effective checkout offline.
    """
    subcommand = _gh_subcommand(tokens)
    if subcommand is None:
        return None
    if subcommand[:2] == ["issue", "create"]:
        explicit, target = _explicit_repo(tokens)
        return target, _has_engine_label(tokens), explicit and target is None
    if subcommand[:1] == ["api"]:
        target = _api_issue_target(tokens)
        if target and _is_post_creation(tokens):
            return target, _has_engine_label(tokens), False
    return None


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
    api = (_gh_subcommand(tokens) or [])[:1] == ["api"]
    for i, tok in enumerate(tokens):
        if tok in ("--label", "-l") and i + 1 < len(tokens) and _label_value_carries_engine(tokens[i + 1]):
            return True
        if tok.startswith("--label=") and _label_value_carries_engine(tok.split("=", 1)[1]):
            return True
        if api and tok in ("-f", "-F", "--field", "--raw-field") and i + 1 < len(tokens):
            m = _API_LABEL_FIELD.match(tokens[i + 1])
            if m and _label_value_carries_engine(m.group(2)):
                return True
        if api and tok.startswith(("--field=", "--raw-field=")):
            m = _API_LABEL_FIELD.match(tok.split("=", 1)[1])
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


def _connector_repo(tool_input) -> tuple[bool, str | None]:
    """Read only structured connector repository fields; bodies are never classification input."""
    if not isinstance(tool_input, dict):
        return False, None
    value = tool_input.get("repository_full_name") or tool_input.get("repository")
    if not value and tool_input.get("owner") and tool_input.get("repo"):
        value = f"{tool_input['owner']}/{tool_input['repo']}"
    if not isinstance(value, str):
        return False, None
    return True, _parse_repo(value.strip())


def _trusted_repositories(cwd, trusted_targets):
    if trusted_targets is not None:
        return list(trusted_targets)
    if not isinstance(cwd, str) or not cwd:
        return []
    try:
        import issue_author
        return issue_author.resolve_issue_repositories(env=os.environ, root=cwd)
    except Exception:
        # The gate cannot establish target trust when the checkout is unavailable; retain fail-open.
        return []


def _same_repo(left: str | None, targets) -> bool:
    if not left:
        return False
    try:
        import repo_identity
        return any(repo_identity.slug_eq(left, target) for target in targets)
    except Exception:
        return any(left.casefold() == str(target).casefold() for target in targets)


def _origin_for_directory(path: str | None) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    try:
        import repo_identity
        return repo_identity.origin_slug(path)
    except Exception:
        return None


def _effective_directory(tokens: list[str], session_cwd: str | None) -> str | None:
    """A local `cd`/`gh -C` only resolves the request target; it never expands trusted targets."""
    base = session_cwd
    if tokens[:1] == ["cd"] and len(tokens) > 1:
        if _is_dynamic(tokens[1]):
            return None
        return tokens[1] if os.path.isabs(tokens[1]) else (os.path.join(base, tokens[1]) if base else None)
    value = _option_value(tokens, ("-C",))
    if value:
        if _is_dynamic(value):
            return None
        return value if os.path.isabs(value) else (os.path.join(base, value) if base else None)
    return base


CLASSIFICATION_LIMITATION = (
    "This looks like a direct GitHub Issue creation, but the routing check could not classify this call. "
    "Normal stance checks still apply; use an explicit owner/repo or GitHub URL, or run from a checkout with "
    "a known session cwd if you intend Engine routing."
)


def classification_limitation(tool_name, tool_input, *, cwd=None) -> str | None:
    """A pure per-call diagnostic for recognized creates whose unlabelled target is unresolved."""
    if _is_connector_issue_creation(tool_name):
        present, target = _connector_repo(tool_input)
        return CLASSIFICATION_LIMITATION if not _connector_carries_engine(tool_input) and (
            not present or target is None or not isinstance(cwd, str) or not cwd) else None
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return None
    try:
        groups = list(_commands(_shell_tokens(command)))
    except ValueError:
        return None
    effective_cwd = tool_input.get("workdir") if isinstance(tool_input.get("workdir"), str) else cwd
    for group in groups:
        if group[:1] == ["cd"]:
            effective_cwd = _effective_directory(group, effective_cwd)
            continue
        creation = _command_creation(group)
        if creation is None:
            continue
        target, labelled, unresolved = creation
        if not labelled:
            if unresolved or not isinstance(cwd, str) or not cwd:
                return CLASSIFICATION_LIMITATION
            if target is None and _origin_for_directory(_effective_directory(group, effective_cwd)) is None:
                return CLASSIFICATION_LIMITATION
    return None


def reroute_reason(tool_name, tool_input, *, cwd=None, trusted_targets=None) -> str | None:
    """Return the helper redirect for a recognized Engine or trusted unlabelled creation.

    `trusted_targets` is an offline test seam. Production derives it exclusively from the session
    checkout, never from command text or a command-selected checkout.
    """
    targets = _trusted_repositories(cwd, trusted_targets)
    if _is_connector_issue_creation(tool_name):
        if _connector_carries_engine(tool_input):
            return DENY_REASON
        _present, target = _connector_repo(tool_input)
        return DENY_REASON if _same_repo(target, targets) else None
    if tool_name != "Bash":
        return None
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command:
        return None
    try:
        groups = list(_commands(_shell_tokens(command)))
    except ValueError:
        return None
    effective_cwd = tool_input.get("workdir") if isinstance(tool_input.get("workdir"), str) else cwd
    for group in groups:
        if group[:1] == ["cd"]:
            effective_cwd = _effective_directory(group, effective_cwd)
            continue
        creation = _command_creation(group)
        if creation is None:
            continue
        target, labelled, unresolved = creation
        if labelled:
            return DENY_REASON
        if not unresolved and target is None:
            target = _origin_for_directory(_effective_directory(group, effective_cwd))
        if _same_repo(target, targets):
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
