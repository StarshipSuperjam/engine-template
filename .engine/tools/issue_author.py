#!/usr/bin/env python3
"""The shared issue-authoring helper — assembles every engine-authored Issue body to the one
control-plane body contract.

WHY THIS EXISTS. The engine creates Issues programmatically (telemetry health findings, promoted build
plans for cold continuation, tracked debt). Those bypass the human web issue templates entirely — templates populate only the web
"New issue" form, while the REST / gh creation path sets the body directly. GitHub cannot gate Issue
*creation* the way a required check gates a merge, so the body contract is enforced **by
construction**: every producer assembles its body through this one helper, which builds the contract's
parts from required arguments — a producer that authors through it cannot omit a part. Authoring *via*
the helper is posture; a producer that bypasses it emits a less-legible body, which
costs legibility, never a guardrail (so the weakening guard does not bite).

THE BODY CONTRACT — a loose structural skeleton, in plain language (control-plane):
  (1) what the Issue is and why it is here                  -> `what_this_is`  (required)
  (2) what the operator must decide, or what happens next   -> `whats_next`    (required)
  (3) any backstage references, as plain links a person can follow, never a bare id dump
                                                            -> `references`    (optional)
Item (1) is bound to the operator-communication law directly: the helper prepends a fixed plain
framing every engine-authored Issue carries, so a producer not yet written inherits a plainness floor
rather than only the example of the contracts it fills. The shape's presence is the floor; its
truthfulness is posture (the PR-contract tiering, carried over).

READABLE FORMATTING (guidance, not a mandate). The contract is a loose skeleton that must accommodate
plain prose, so this helper never *forces* structure — but a non-trivial part reads as a wall of text
unless the producer shapes it. For anything beyond a sentence or two: lead the part with a one-line
summary, then break the detail into markdown bullets — the structured-artifacts convention, mirroring
the [PR template](../../.github/pull_request_template.md)'s summary->bullets shape. A short finding may
stay plain prose (audits' pinned exemplar). The helper renders whatever markdown a part contains
verbatim, so bulleted detail renders as bullets; `_demo` below models the readable shape.

PASSIVE FORMATTER, NOT A REGISTRY. This is shared code each producer *calls*; it
makes no network calls, applies no label, and holds no roster of producers. The engine-domain label is
applied by each producer's own GitHub boundary (an explicit `labels` value at creation, or a label
call right after — never a web-only issue-template default, which the programmatic path bypasses). Its
literal string is `engine` (`telemetry.ENGINE_DOMAIN_LABEL`), never `engine-domain` or a look-alike a
descriptive phrase might suggest — a look-alike label is read by no machinery, so the Issue silently drops out
of the debt register and the boot counts. The producer-side rule: whoever files an Issue about the engine's
OWN health applies `--label engine` AT creation, regardless of who asked for it. The
product-design spec Issue is the named exception: its body is a plain-prose specification, a
different realization of the same channel, not authored through this helper.

CLI (operator-runnable):
  uv run --directory .engine -- python tools/issue_author.py demo
  uv run --directory .engine -- python tools/issue_author.py preview --input <file|->
  uv run --directory .engine -- python tools/issue_author.py create  --input <file|-> --confirm

THE preview/create CLI. `preview` reads a structured input (engine-issue-input.v1 shape), validates it,
resolves the TRUSTED target repository from engine config, and prints the repository, the `engine` label
(applied by construction), the title, and the rendered body — WITHOUT any network call. `create` does the
same, then (only with `--confirm`) files the Issue through the supported GitHub boundary
(`telemetry.GitHubIssues` → `github_client.json_request`), applying the `engine` label by construction, and
prints the link.

AUTHORITY BOUNDARY (why the input's `repository` cannot steer the filing). The input NAMES an intended
repository, but the create path RESOLVES the actual target from trusted config (this checkout's own
identity — `GITHUB_REPOSITORY` in CI, else the git origin slug) and REFUSES to file when the two do not
match. So an input whose `repository` was influenced by observed content cannot redirect an engine Issue
off the engine's own channel onto a repository chosen by that content. The label is applied by construction,
never read from the input.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_kind  # noqa: E402  (stdlib-only leaf: the canonical kind vocabulary + marker + normalised title)

# The input schema governing the preview/create structured input (engine-issue-input.v1). Loaded from disk at
# call time (never fetched); issue_author stays import-light (telemetry imports it at load), so jsonschema and
# the schema file are only touched inside the CLI functions, not at module scope.
_INPUT_SCHEMA_REL = os.path.join(os.path.dirname(__file__), "..", "schemas", "engine-issue-input.v1.json")

# The plainness floor: the one fixed, plain line every engine-authored Issue carries for contract
# part (1), so a future producer inherits a plain framing by construction (control-plane). It states
# only what is universally true of an engine-authored Issue — the engine opened it, the operator did
# not — and carries no backstage vocabulary.
_FRAMING = "*The engine opened this item itself — you didn't create it.*"

# The verified-head provenance marker (StarshipSuperjam/engine-template#957). A session that files an engine
# Issue whose premise is committed repository state records the exact commit it verified the claim against, so
# the body carries machine-recoverable proof the claim was checked at a fresh default-branch head — not on a
# stale worktree. The value is `owner/repo@sha` (the repo, because a bare sha is ambiguous across repositories
# the engine may file into), rendered as an invisible HTML-comment trailer mirroring telemetry's severity
# marker: the ONE place this trailer is built, so any future reader recovers the identical form. `_VALUE_RE`
# is the marker-safety gate — only `owner/repo@hex` passes, so no `<`, `>`, or `-->` can enter the comment. It
# mirrors `engine-issue-input.v1.json`'s `verified_head.pattern` (the CLI-boundary gate); keep the two in sync,
# as the urgency enum and telemetry's severity classes already are — a drift would let a value pass one gate and
# raise at the other.
_VERIFIED_HEAD_TEMPLATE = "<!-- verified-head: {value} -->"
_VERIFIED_HEAD_RE = re.compile(r"<!--\s*verified-head:\s*(.+?)\s*-->")
_VERIFIED_HEAD_VALUE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*@[0-9a-fA-F]{7,40}$")


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


def verified_head_trailer(value: str) -> str:
    """Compose the invisible verified-head marker for an issue body — the ONE place the
    `<!-- verified-head: … -->` trailer is built (StarshipSuperjam/engine-template#957), so every producer that
    records the checked commit writes the identical marker `parse_verified_head` recovers. `value` is
    `owner/repo@sha` (the repo the sha belongs to plus the 7–40 char hex commit the repository-state claim was
    verified against). Anything else raises ValueError — the value is marker-safe by construction (no `<`, `>`,
    or comment-closer can enter), never interpolated free text, mirroring telemetry.severity_trailer's
    fail-closed discipline. Callers append it BEFORE any severity marker, so severity stays the last trailer."""
    if not isinstance(value, str) or not _VERIFIED_HEAD_VALUE_RE.match(value):
        raise ValueError(
            f"verified_head must be 'owner/repo@<7-40 hex sha>', not {value!r}")
    return _VERIFIED_HEAD_TEMPLATE.format(value=value)


def parse_verified_head(body: str) -> str | None:
    """Recover the `owner/repo@sha` a tracked Issue's premise was verified against, from its invisible
    verified-head marker — the read a future freshness/provenance check uses. Takes the LAST marker (the
    genuine trailer is appended after the body prose, so forged prose earlier in the body cannot hijack it),
    mirroring telemetry.parse_severity's anti-hijack rule. None when the marker is absent."""
    matches = _VERIFIED_HEAD_RE.findall(body or "")
    return matches[-1] if matches else None


def render_engine_issue_body(*, what_this_is: str, whats_next: str, references=None,
                             urgency: str | None = None, verified_head: str | None = None,
                             kind: str | None = None) -> str:
    """Assemble an engine-authored Issue body to the control-plane body contract.

    Keyword-only and required: omitting `what_this_is` or `whats_next` raises TypeError at the call
    boundary (the by-construction enforcement — a producer cannot omit a part); a present-but-blank
    value raises ValueError. `references` is an optional list of (label, url) pairs rendered as plain
    markdown links.

    `kind` (optional here; REQUIRED on the preview/create input path — see body_from_input) records the
    authoritative canonical issue kind (StarshipSuperjam/engine-template#937) as the invisible
    `<!-- engine-kind: … -->` marker via `issue_kind.kind_trailer` (the single source; a non-canonical value
    raises there). The on:issues reconciler reads this marker to keep the title's `Kind:` prefix correct, so a
    body that carries it is self-healing. None (the default) leaves a producer's body byte-for-byte unchanged —
    telemetry's self-observation genre and other direct callers pass no kind and are intentionally out of the
    self-healing set (a health finding is a report, not a change-kind). Appended before any severity marker.

    `verified_head` (optional) lets a session that files an engine Issue about committed repository state
    record the exact commit it verified the claim against (StarshipSuperjam/engine-template#957): an
    `owner/repo@sha` string, rendered as the invisible `<!-- verified-head: … -->` trailer via
    `verified_head_trailer` (the single source; a malformed value raises there). None (the default) leaves
    every existing producer's body byte-for-byte unchanged. It is appended BEFORE any severity marker, so
    severity stays the last trailer.

    `urgency` (optional) lets a session that files an engine Issue grade it at creation: one of telemetry's
    two severity classes (`trust-critical`, `persistent-but-benign`), or None (unrated — the default, which
    leaves every existing producer's body byte-for-byte unchanged). When set, the same invisible
    `<!-- engine-severity: … -->` marker telemetry writes is appended LAST (so `telemetry.parse_severity`
    recovers it) via `telemetry.severity_trailer` — the single source for that marker; any other value raises
    ValueError there. Returns the body string; the calling producer still applies the engine-domain label and
    appends any OTHER producer-specific trailer (e.g. a tracking marker) itself — this helper never calls
    GitHub and never applies a label."""
    what_this_is = _require("what_this_is", what_this_is)
    whats_next = _require("whats_next", whats_next)
    body = (
        f"{_FRAMING}\n\n"
        f"**What this is.** {what_this_is}\n\n"
        f"**What happens next.** {whats_next}"
        f"{_render_references(references)}\n"
    )
    if verified_head is not None:
        # Appended BEFORE the severity marker so severity remains the last trailer parse_severity's last-match
        # rule expects; verified_head_trailer validates the value (fail-closed) and owns the marker's shape.
        body += f"\n{verified_head_trailer(verified_head)}\n"
    if kind is not None:
        # The authoritative kind marker (the reconciler's source of truth). Appended before the severity marker
        # so severity stays last; issue_kind.kind_trailer validates against the six-kind enum (fail-closed) and
        # owns the marker's shape, so a non-canonical kind is refused here, never minted.
        body += f"\n{issue_kind.kind_trailer(kind)}\n"
    if urgency is not None:
        # telemetry owns the severity marker; import it lazily HERE (not at module scope) because telemetry
        # imports issue_author at load time — a top-level `import telemetry` would close that cycle and crash
        # the boot path. At call time telemetry is fully initialised, so the local import is safe.
        import telemetry  # noqa: E402  (function-local: breaks the telemetry<->issue_author import cycle)
        try:
            body += f"\n{telemetry.severity_trailer(urgency)}\n"
        except ValueError as exc:
            # telemetry's message speaks its own vocabulary ("severity"); re-raise naming the argument the
            # CALLER actually passed, so the error points at a parameter that exists at this call site.
            raise ValueError(str(exc).replace("severity", "urgency", 1)) from None
    return body


# ---- the preview/create CLI (structured for offline unit testing) ---------------------------

class IssueInputError(ValueError):
    """A structured-input failure the CLI reports as a plain refusal (schema violation, unreadable input,
    trusted-target mismatch, or a missing confirmation/token) — distinct from a network failure."""


def load_input(source: str, *, _stdin=None) -> dict:
    """Read and JSON-parse the structured input from a file path or `-` (stdin). Raises IssueInputError on an
    unreadable source or non-object JSON. `_stdin` is an injectable stream for offline tests."""
    try:
        if source == "-":
            raw = (_stdin if _stdin is not None else sys.stdin).read()
        else:
            with open(os.path.expanduser(source), "r", encoding="utf-8") as fh:
                raw = fh.read()
    except OSError as exc:
        raise IssueInputError(f"could not read the input from '{source}': {exc}") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IssueInputError(f"the input is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise IssueInputError("the input must be a JSON object (engine-issue-input.v1 shape)")
    return data


def validate_input(data: dict) -> dict:
    """Validate `data` against engine-issue-input.v1 and return it unchanged. Raises IssueInputError naming the
    first violation. jsonschema and the schema file are loaded lazily here so the module stays import-light."""
    from jsonschema import Draft202012Validator  # lazy: tool-runtime dep, not needed to import this module
    with open(_INPUT_SCHEMA_REL, "r", encoding="utf-8") as fh:
        schema = json.load(fh)
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.path) or "(root)"
        raise IssueInputError(f"the input does not match engine-issue-input.v1 at {where}: {first.message}")
    return data


def resolve_trusted_targets(*, env=None, root: "str | None" = None) -> list:
    """The repositories this engine may file its OWN engine Issues into, resolved from TRUSTED config only —
    never from the input. Always the engine's own checkout: `GITHUB_REPOSITORY` (the CI-provided identity) when
    set, otherwise the checkout's git `origin` slug (read offline from disk). For an engine-MECHANIC it ALSO
    includes the owned product it delivers into — the manifest's committed `product_build_target`
    (checkout_health.recorded_product_build_target), which is None for a normal self-building engine — so an
    owned-product engine Issue reaches the product it builds. Both sources are trusted config, so neither can be
    steered by observed content. Deduped case-insensitively; empty when nothing resolves (create then refuses)."""
    import repo_identity  # lazy: pulls in validate; only needed at CLI runtime
    environ = os.environ if env is None else env
    targets = []
    declared = environ.get("GITHUB_REPOSITORY")
    if declared and declared.strip():
        targets.append(declared.strip())
    else:
        origin = repo_identity.origin_slug(root)
        if origin:
            targets.append(origin)
    try:
        import checkout_health  # lazy
        product = checkout_health.recorded_product_build_target(root)
        if product:
            targets.append(product)
    except Exception:  # noqa: BLE001 — the owned-product target is additive; its absence never blanks the set
        pass
    deduped: list = []
    for target in targets:
        if not any(repo_identity.slug_eq(target, seen) for seen in deduped):
            deduped.append(target)
    return deduped


def title_from_input(data: dict) -> str:
    """The canonical Issue title from the validated input: `<Kind>: <title>`, rendered from the REQUIRED `kind`
    (structured data) and the descriptive `title`. issue_kind.render_title normalises and strips any prefix the
    author mistyped, so the filed title always carries exactly one canonical kind prefix — the prefix is a
    projection of the kind, never independently authored (StarshipSuperjam/engine-template#937)."""
    return issue_kind.render_title(data["kind"], data["title"])


def body_from_input(data: dict) -> str:
    """Render the Issue body from the validated input through the one body contract (render_engine_issue_body),
    stamping the authoritative `<!-- engine-kind: … -->` marker from the REQUIRED `kind` so the filed Issue is
    self-healing. References carry (label, link) fields (engine-issue-input.v1) — mapped to the renderer's
    (label, url) pairs."""
    references = [(ref["label"], ref["link"]) for ref in data.get("references", [])] or None
    return render_engine_issue_body(
        what_this_is=data["what_this_is"], whats_next=data["whats_next"], references=references,
        urgency=data.get("urgency"), verified_head=data.get("verified_head"), kind=data["kind"])


def _matched_target(requested: str, trusted_targets: list) -> "str | None":
    """The trusted target the requested repository matches (case/normalization-insensitive), or None when it
    matches none — an input naming an untrusted repository is never treated as a match (fail closed)."""
    import repo_identity  # lazy
    for target in trusted_targets:
        if repo_identity.slug_eq(requested, target):
            return target
    return None


def preview_text(data: dict, trusted_targets: list) -> str:
    """The operator-facing preview string: requested repository, the trusted target set and whether the request
    matches one of them, the engine label (by construction), the title, and the rendered body. Pure — no
    network, nothing filed."""
    import telemetry  # lazy: for the label constant (issue_author is imported by telemetry at load)
    requested = data["repository"]
    matched = _matched_target(requested, trusted_targets)
    if not trusted_targets:
        agree = ("  ✗ no trusted target could be resolved from engine config — `create` will refuse to file "
                 "until one can (fail closed).")
    elif matched:
        agree = f"  ✓ the requested repository matches the trusted target {matched}."
    else:
        agree = ("  ✗ the requested repository does NOT match any trusted target — `create` will refuse to file "
                 "(an input cannot steer the filing off the engine's own channel).")
    return (
        "ENGINE ISSUE — PREVIEW (nothing has been filed)\n\n"
        f"Repository (requested in the input): {requested}\n"
        f"Trusted targets (where create MAY file): {', '.join(trusted_targets) or '(none resolved)'}\n"
        f"{agree}\n"
        f"Label applied by construction: {telemetry.ENGINE_DOMAIN_LABEL}\n"
        f"Kind (structured): {data['kind']}\n"
        f"Title (rendered from the kind): {title_from_input(data)}\n\n"
        "--- rendered body ---\n"
        f"{body_from_input(data)}\n"
        "---------------------\n"
        "To file it, re-run with `create --input <same input> --confirm`."
    )


def create_issue(data: dict, *, env=None, root: "str | None" = None, issues_factory=None) -> str:
    """File the engine Issue and return its link. Resolves the trusted target SET and REFUSES (IssueInputError)
    if the input's repository matches none of it, or if no target/token can be resolved. The Issue is filed into
    the trusted target the input MATCHED (never a repository named only by the input). The `engine` label is
    applied by construction (telemetry.GitHubIssues' default). `issues_factory(repo, token)` is injectable so
    offline tests exercise the whole path without a network; production uses telemetry.GitHubIssues."""
    environ = os.environ if env is None else env
    trusted = resolve_trusted_targets(env=environ, root=root)
    if not trusted:
        raise IssueInputError(
            "refusing to file: no trusted target could be resolved from engine config "
            "(no GITHUB_REPOSITORY, no git origin, no recorded product build target) — the target cannot be verified.")
    matched = _matched_target(data["repository"], trusted)
    if matched is None:
        raise IssueInputError(
            f"refusing to file: the input names '{data['repository']}' but this engine's trusted targets are "
            f"{trusted}. An engine Issue is filed only into the engine's own repository (or, for a mechanic, the "
            "owned product it builds); correct the input's `repository` to one of those (an input cannot redirect "
            "the filing elsewhere).")
    token = environ.get("GITHUB_TOKEN")
    if not token or not token.strip():
        raise IssueInputError("refusing to file: no GITHUB_TOKEN is set, so the Issue cannot be filed.")
    if issues_factory is None:
        import telemetry  # lazy
        issues_factory = telemetry.GitHubIssues
    issues = issues_factory(matched, token.strip())
    created = issues.open_issue(title_from_input(data), body_from_input(data))
    return created.get("html_url") or f"https://github.com/{matched}/issues/{created.get('number', '')}"


def _cli_preview(source: str) -> int:
    try:
        data = validate_input(load_input(source))
    except IssueInputError as exc:
        print(f"Refused — {exc}", file=sys.stderr)
        return 2
    print(preview_text(data, resolve_trusted_targets()))
    return 0


def _cli_create(source: str, confirm: bool) -> int:
    if not confirm:
        print("Refused — `create` files a GitHub Issue, so it needs explicit confirmation. Re-run with "
              "`--confirm` (use `preview` first to see exactly what will be filed).", file=sys.stderr)
        return 2
    try:
        data = validate_input(load_input(source))
        link = create_issue(data)
    except IssueInputError as exc:
        print(f"Refused — {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # a network / GitHub failure (e.g. telemetry.DegradedReadError) — report plainly
        print(f"Could not file the Issue: {exc}", file=sys.stderr)
        return 1
    print(f"Filed: {link}")
    return 0


def _parse_cli(argv: list) -> "tuple[str, bool]":
    """Extract `--input <value>` and `--confirm` from a subcommand's args. Raises IssueInputError when
    `--input` is missing or has no value (the one required flag both subcommands share)."""
    source, confirm = None, False
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--input" and i + 1 < len(argv):
            source = argv[i + 1]
            i += 2
            continue
        if tok.startswith("--input="):
            source = tok.split("=", 1)[1]
        elif tok == "--confirm":
            confirm = True
        i += 1
    if not source:
        raise IssueInputError("this command needs `--input <file|->`.")
    return source, confirm


def _demo() -> int:
    print("ISSUE-AUTHORING HELPER DEMO — one body assembled from the contract's parts.\n")
    body = render_engine_issue_body(
        what_this_is=(
            "The engine noticed one of its own checks has been unable to run for the last few sessions.\n\n"
            "- **What it is:** an item about the engine's own machinery, not your project.\n"
            "- **Why it's here:** so the problem stays visible until it is fixed."
        ),
        whats_next=(
            "Usually nothing right now.\n\n"
            "- The engine will propose a fix in a later session, under the review-and-merge step you already use.\n"
            "- Once the cause is gone, this item closes itself.\n"
            "- If it lingers and you want it resolved sooner, you can ask for the fix to be prioritised."
        ),
        references=[("The check's last run", "https://github.com/owner/repo/actions/runs/123")],
    )
    print(body)
    refused = 0
    print("--- leaving out a required part stops the call ---")
    try:
        render_engine_issue_body(what_this_is="only one part supplied")  # type: ignore[call-arg]
    except TypeError as exc:
        refused += 1
        print(f"Refused — a required part was missing: {exc}")
    print("\n--- a present-but-blank part stops the call ---")
    try:
        render_engine_issue_body(what_this_is="   ", whats_next="x")
    except ValueError as exc:
        refused += 1
        print(f"Refused — a required part was blank: {exc}")
    print("\n--- a reference without a label and a link is refused (never a bare id) ---")
    try:
        render_engine_issue_body(what_this_is="x", whats_next="y", references=[("", "rule:abc")])
    except ValueError as exc:
        refused += 1
        print(f"Refused — a reference needs a label and a link: {exc}")
    print("\n--- an optional urgency grades the Issue at creation (invisible severity marker, appended last) ---")
    graded = render_engine_issue_body(what_this_is="x", whats_next="y", urgency="trust-critical")
    graded_ok = "<!-- engine-severity: trust-critical -->" in graded
    print(f"Marker present: {graded_ok}")
    print("\n--- an urgency outside telemetry's two classes is refused (never free text) ---")
    try:
        render_engine_issue_body(what_this_is="x", whats_next="y", urgency="high")
    except ValueError as exc:
        refused += 1
        print(f"Refused — urgency must be one of telemetry's two classes: {exc}")
    print("\n--- an optional kind stamps the authoritative kind marker (the reconciler's self-healing source) ---")
    kinded = render_engine_issue_body(what_this_is="x", whats_next="y", kind="Fix")
    kind_ok = "<!-- engine-kind: Fix -->" in kinded
    print(f"Kind marker present: {kind_ok}")
    print("\n--- a kind outside the six canonical kinds is refused (never minted) ---")
    try:
        render_engine_issue_body(what_this_is="x", whats_next="y", kind="Bug")
    except ValueError as exc:
        refused += 1
        print(f"Refused — kind must be one of the six canonical kinds: {exc}")
    # Self-check: a complete call renders a body, each of the five contract violations is refused, and the
    # graded / kinded bodies carry their markers.
    ok = bool(body) and refused == 5 and graded_ok and kind_ok
    if not ok:
        print(f"\nDEMO UNEXPECTED: body/marker did not render or a refusal did not fire "
              f"({refused}/5 refused, severity={graded_ok}, kind={kind_ok}).", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    verb = argv[0] if argv else None
    if verb == "demo":
        return _demo()
    if verb in ("preview", "create"):
        try:
            source, confirm = _parse_cli(argv[1:])
        except IssueInputError as exc:
            print(f"Refused — {exc}", file=sys.stderr)
            return 2
        return _cli_preview(source) if verb == "preview" else _cli_create(source, confirm)
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
