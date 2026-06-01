#!/usr/bin/env python3
"""The core validation dispatcher — a thin core over a kind registry.

The check *inventory* is data (.engine/check/*.json) and the check *logic* is a
small registry of kind callables, so adding a check adds a rule file and never
edits this dispatcher (systems/guardrails/validation/README.md). This is the
`core` validation engine the stage-0 seed validator grew into; the engine ships
here, while the engine-self-validation rule *corpus* rides `validators-core`
(decision-log D-090), so only the two grandfathered seed rules are committed.

Closed core kinds (this slice ships three of the five):
  - presence  — named sections/fields are present and non-empty.
  - schema    — a structured file conforms to its governing JSON Schema (2020-12),
                resolved from the ontology catalog (`governing_schema`) or, for the
                cases the catalog cannot express, an explicit rule `params.schema`.
  - shape     — a prose instance matches a template's shape-spec (required and
                allowed sections, ordering, a soft length budget).
  - link-integrity — a transient seed kind; folds into the `coverage` kind at the
                next slice. (coverage / coherence / custom-script are not here yet.)
Module-provided kinds bind by presence at a later slice and must NOT extend the
hardcoded REGISTRY below; it holds the closed core set only.

Each kind callable returns a Result: a pass/fail verdict plus zero or more findings
on the canonical finding.v1 base {severity, message, location}. A check finding's
severity is the rule's tier (`hard` | `soft`) (decision-log D-113).

Suites and triggers: a suite is a thin declaration (.engine/suites.json) — a name,
a trigger, and an execution context — never a list of its rules; a rule self-declares
which suites it joins, so a suite's roster is derived. Only a `blocking-gate` context
(the CI suite) lets a hard finding fail the run; `local-nudge` and `report-only`
contexts never block. A rule whose kind is unregistered, or whose callable errors,
FAILS CLOSED in a gating context, so a hard governance rule can never be silently
un-enforced. A malformed suites.json or check/schema file fails loud.

Usage:
  validate.py --suite CI                      # run the CI suite (default)
  validate.py --suite CI --pr-body-file PATH  # supply the PR body explicitly

The PR body is read from --pr-body-file, else from $GITHUB_EVENT_PATH
(.pull_request.body — the safe path: never interpolated into a shell command), else
treated as unavailable (the PR-body presence check fails OPEN locally, evaluates in CI).
"""
from __future__ import annotations
import glob as _glob
import json
import os
import re
import sys

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

THIS = os.path.abspath(__file__)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(THIS)))  # .engine/tools/validate.py -> repo root
ENGINE_DIR = os.path.join(ROOT, ".engine")
CHECK_DIR = os.path.join(ENGINE_DIR, "check")
SCHEMAS_DIR = os.path.join(ENGINE_DIR, "schemas")
CATALOG_PATH = os.path.join(SCHEMAS_DIR, "surface-catalog.json")
SUITES_PATH = os.path.join(ENGINE_DIR, "suites.json")
SUITES_SCHEMA_PATH = os.path.join(SCHEMAS_DIR, "suites.v1.json")
# The 2020-12 dialect URI. A governing_schema of this value means "this file IS a
# schema; check its well-formedness against the bundled meta-schema" rather than
# "validate this file against a sibling schema". Matched, never fetched (offline).
META_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"

LINK_RE = re.compile(r"\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^##\s+(.*?)\s*$")          # a level-2 (## ) heading; ### does not match
PLACEHOLDER_RE = re.compile(r"^<[^>]*>$")             # a prompt token (decoration stripped), e.g. <why this exists>
LIST_MARKER_RE = re.compile(r"^[-*+]\s+")             # a leading unordered-list bullet marker
EMPHASIS_RE = re.compile(r"^(\*\*|__|\*|_)(.+?)\1$")  # a surrounding bold/italic emphasis wrapper


# ---- finding.v1 ------------------------------------------------------------

def finding(severity: str, message: str, location: dict | None = None) -> dict:
    """A finding on the canonical finding.v1 base {severity, message, location}."""
    return {"severity": severity, "message": message, "location": location}


def loc(path: str, line: int | None = None) -> dict:
    return {"file": os.path.relpath(path, ROOT), "line": line}


# ---- shared helpers --------------------------------------------------------

def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_json(path: str):
    """Parse a JSON file to a data object. Raises (loud) on a missing or malformed
    file — the halt-on-malformed posture: a broken structured file fails loud rather
    than misleading the AI (schemas/README.md design commitment)."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def target_files(rule: dict) -> list:
    """Repository files a rule's target.path glob selects (absolute paths, sorted).
    A target without a path (e.g. a `context` target) selects no files here."""
    pattern = (rule.get("target") or {}).get("path")
    if not pattern:
        return []
    matched = _glob.glob(os.path.join(ROOT, pattern), recursive=True)
    return sorted(p for p in matched if os.path.isfile(p))


def section_blocks(body: str) -> dict:
    """{heading_text: section_body} for each level-2 heading in the body."""
    blocks, current, buf = {}, None, []
    for line in body.splitlines():
        m = HEADING_RE.match(line)
        if m:
            if current is not None:
                blocks[current] = "\n".join(buf)
            current, buf = m.group(1), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        blocks[current] = "\n".join(buf)
    return blocks


def section_order(body: str) -> list:
    """The level-2 heading texts in the order they appear in the body."""
    return [m.group(1) for line in body.splitlines() if (m := HEADING_RE.match(line))]


def _placeholder_only(line: str) -> bool:
    """True if the line, once its Markdown scaffolding is stripped — a leading list
    marker and any surrounding bold/italic emphasis — is nothing but a single <...>
    prompt token. So a decorated, unfilled template slot (**<summary>**, - <detail>,
    *<Impact: ...>*) still counts as a placeholder, while a line carrying real text
    (even one with an inline <token>) does not reduce to a bare token and is kept."""
    s = LIST_MARKER_RE.sub("", line.strip()).strip()
    prev = None
    while prev != s:                       # peel possibly-nested emphasis wrappers
        prev = s
        m = EMPHASIS_RE.match(s)
        if m:
            s = m.group(2).strip()
    return bool(PLACEHOLDER_RE.match(s))


def is_empty_section(text: str) -> bool:
    """Empty if every line is blank or a placeholder slot (decorated or bare). Any
    line with real content makes the section non-empty, so a filled section passes."""
    for line in text.splitlines():
        if not line.strip() or _placeholder_only(line):
            continue
        return False
    return True


def section_presence_findings(body: str, sections: list, tier: str, message: str, where: str) -> list:
    """For each named section: a finding if it is missing, or present but empty
    (only blank/placeholder lines). `where` names the source for the message."""
    blocks = section_blocks(body)
    findings = []
    for name in sections:
        if name not in blocks:
            findings.append(finding(tier, f"Required section '## {name}' is missing from "
                            f"the {where}. {message}"))
        elif is_empty_section(blocks[name]):
            findings.append(finding(tier, f"Required section '## {name}' in the {where} is "
                            f"empty or only contains the template placeholder. {message}"))
    return findings


# ---- kind: presence --------------------------------------------------------

def kind_presence(rule, ctx):
    """Named sections are present and non-empty. The target is either the
    pull-request body (target.context == 'pull-request-body') or a prose file
    (target.path). A section is empty if, after dropping blank lines and template
    placeholder lines, no substantive content remains — so an auto-populated
    template body does NOT pass on its own. Presence + non-emptiness are gated;
    truthfulness is posture (this cannot judge whether the content is accurate)."""
    tier = rule["tier"]
    sections = (rule.get("params") or {}).get("sections", [])
    target = rule.get("target") or {}
    if target.get("context") == "pull-request-body":
        body = ctx.get("pr_body")
        if body is None:
            return True, [finding("soft", "PR body not available; completeness not "
                                  "evaluated here (the CI run evaluates it).")]
        findings = section_presence_findings(body, sections, tier,
                                             rule["message"], "pull-request body")
        return (len(findings) == 0), findings
    findings = []
    for path in target_files(rule):
        findings.extend(section_presence_findings(read(path), sections, tier,
                        rule["message"], os.path.relpath(path, ROOT)))
    return (len(findings) == 0), findings


# ---- kind: schema ----------------------------------------------------------

def _surface_record_for(rel_path: str) -> dict | None:
    """The catalog surface record whose `location` is a directory prefix of
    rel_path (the longest match wins). None if no surface owns the file."""
    try:
        surfaces = load_json(CATALOG_PATH).get("surfaces", {})
    except Exception:
        return None
    best = None
    for rec in surfaces.values():
        location = rec.get("location", "")
        if location and rel_path.startswith(location) and (best is None
                or len(location) > len(best.get("location", ""))):
            best = rec
    return best


def _governing_schema(rule: dict, rel_path: str):
    """Resolve the governing schema for a target file, then return the loaded
    schema object to validate the file against. Resolution is CATALOG-FIRST
    (schemas/README.md: 'there is no separate routing table'): the surface's
    `governing_schema`. A rule's `params.schema` is an OVERRIDE only — for the two
    cases the catalog cannot express: a well-formedness check, and the catalog's
    own self-governance (surface-catalog.json is governed by its meta-contract, not
    by the meta-schema URL its `schema`-surface record carries). Returns:
      None                       -> file is not schema-governed; nothing to check.
      Draft202012Validator.META_SCHEMA -> the 2020-12 dialect (well-formedness).
      <loaded schema object>     -> validate the file against it.
    Schema-path references resolve on disk, never over the network."""
    params = rule.get("params") or {}
    if params.get("schema"):                       # explicit override (repo-root-relative or the dialect URI)
        ref, base = params["schema"], ROOT
    else:                                          # catalog routing (relative to the schemas dir, or the URI)
        rec = _surface_record_for(rel_path)
        ref = rec.get("governing_schema") if rec else None
        base = SCHEMAS_DIR
    if not ref:
        return None
    if ref == META_SCHEMA_URI or ref.startswith("http"):
        return Draft202012Validator.META_SCHEMA    # bundled offline; never fetched
    return load_json(os.path.normpath(os.path.join(base, ref)))


def kind_schema(rule, ctx):
    """A structured file conforms to its governing JSON Schema (2020-12). Validates
    the file's PARSED data, not its raw text. A malformed file, an unresolvable or
    offline schema reference, or a malformed governing schema is a loud finding (the
    halt-on-malformed posture), never an uncaught error and never a network fetch."""
    tier = rule["tier"]
    findings = []
    for path in target_files(rule):
        rel = os.path.relpath(path, ROOT)
        try:
            data = load_json(path)
        except Exception as exc:
            findings.append(finding(tier, f"'{rel}' is not valid JSON and cannot be "
                            f"schema-checked: {exc}. {rule['message']}", loc(path)))
            continue
        try:
            schema = _governing_schema(rule, rel)
            if schema is None:
                continue                           # not schema-governed; nothing to check
            for err in Draft202012Validator(schema).iter_errors(data):
                where = "/".join(str(p) for p in err.absolute_path) or "(root)"
                findings.append(finding(tier, f"'{rel}' does not match its schema at "
                                f"{where}: {err.message}. {rule['message']}", loc(path)))
        except SchemaError as exc:                 # the governing schema is itself malformed
            findings.append(finding(tier, f"'{rel}' is governed by a schema that is not "
                            f"well-formed: {exc.message}. {rule['message']}", loc(path)))
        except Exception as exc:                   # unresolvable/offline reference, missing schema file, ...
            findings.append(finding(tier, f"'{rel}' could not be schema-checked (its schema "
                            f"is unresolvable offline or missing): {exc}. {rule['message']}", loc(path)))
    return (not any(f["severity"] == "hard" for f in findings)), findings


# ---- kind: shape -----------------------------------------------------------

def kind_shape(rule, ctx):
    """A prose instance matches a template's shape-spec: the required sections are
    present and in the declared order, no section falls outside required+allowed, and
    the body stays within a soft length budget. Section STRUCTURE is the control —
    a missing required or an out-of-allowed section is the rule's tier (hard for a
    governance-critical surface, soft for a lighter one). LENGTH only nudges — over
    the budget is always SOFT, never the rule's hard tier (templates/README.md). The
    shape-spec is read from params (required_sections, allowed_sections, length_budget),
    the template.v1 grammar; reading it from a template file's frontmatter arrives
    with the first authored template (a later slice; it needs frontmatter parsing)."""
    tier = rule["tier"]
    params = rule.get("params") or {}
    required = params.get("required_sections", [])
    allowed = set(required) | set(params.get("allowed_sections", []))
    budget = params.get("length_budget")
    findings = []
    for path in target_files(rule):
        rel = os.path.relpath(path, ROOT)
        body = read(path)
        present = section_order(body)
        present_set = set(present)
        # required present
        for name in required:
            if name not in present_set:
                findings.append(finding(tier, f"'{rel}' is missing the required section "
                                f"'## {name}'. {rule['message']}", loc(path)))
        # required ordering: the required sections that ARE present must keep their order
        seen = [n for n in present if n in required]
        if seen != [n for n in required if n in present_set]:
            findings.append(finding(tier, f"'{rel}' has its required sections out of order; "
                            f"expected the order {required}. {rule['message']}", loc(path)))
        # no section outside required+allowed
        for name in present:
            if name not in allowed:
                findings.append(finding(tier, f"'{rel}' has section '## {name}', which the "
                                f"template does not allow. {rule['message']}", loc(path)))
        # length budget — a soft nudge only, regardless of the rule's tier
        if budget is not None:
            lines = len(body.splitlines())
            if lines > budget:
                findings.append(finding("soft", f"'{rel}' is {lines} lines, over its "
                                f"{budget}-line budget — a nudge to trim, never a block.", loc(path)))
    return (not any(f["severity"] == "hard" for f in findings)), findings


# ---- kind: link-integrity (transient seed kind; folds into `coverage` next slice) ----

def kind_link_integrity(rule, ctx):
    """Every relative Markdown link must resolve to an existing file. A link that
    resolves OUTSIDE the repo cannot be checked in a CI checkout, so it is a soft
    note, never a hard failure."""
    tier = rule["tier"]
    exclude = set((rule.get("params") or {}).get("exclude_dirs", []))
    findings = []
    for path in markdown_files(exclude):
        text = read(path)
        base = os.path.dirname(path)
        for m in LINK_RE.finditer(text):
            target = m.group(1).strip()
            if not target or target.startswith(("#", "mailto:")) or "://" in target:
                continue
            target = target.split("#", 1)[0].strip()
            if not target:
                continue
            resolved = os.path.normpath(os.path.join(base, target))
            if os.path.exists(resolved):
                continue
            inside = os.path.abspath(resolved).startswith(ROOT + os.sep)
            line_no = text[:m.start()].count("\n") + 1
            sev = tier if inside else "soft"
            findings.append(finding(sev, f"Broken Markdown link to '{target}'. {rule['message']}",
                            loc(path, line_no)))
    return (not any(f["severity"] == "hard" for f in findings)), findings


def markdown_files(exclude_dirs: set) -> list:
    out = []
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d != ".git"
                   and os.path.relpath(os.path.join(dirpath, d), ROOT) not in exclude_dirs]
        out.extend(os.path.join(dirpath, f) for f in files if f.endswith(".md"))
    return out


# The closed core kind registry. Module-provided / custom-script kinds bind by
# presence at a later slice and must NOT be added here.
REGISTRY = {
    "presence": kind_presence,
    "schema": kind_schema,
    "shape": kind_shape,
    "link-integrity": kind_link_integrity,
}


# ---- dispatcher ------------------------------------------------------------

def load_rules() -> list:
    if not os.path.isdir(CHECK_DIR):
        return []
    return [load_json(os.path.join(CHECK_DIR, n))
            for n in sorted(os.listdir(CHECK_DIR)) if n.endswith(".json")]


def load_suites() -> dict:
    """The suite declarations {name: {trigger, context}}, validated against
    suites.v1.json. Raises (loud) if the file is missing, malformed, or does not
    conform — the dispatcher's own config follows the halt-on-malformed posture."""
    data = load_json(SUITES_PATH)
    schema = load_json(SUITES_SCHEMA_PATH)
    errs = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    if errs:
        raise ValueError(".engine/suites.json does not conform to suites.v1.json: "
                         + "; ".join(e.message for e in errs))
    return data["suites"]


def get_pr_body(body_file: str | None) -> str | None:
    if body_file:
        return read(body_file)
    event = os.environ.get("GITHUB_EVENT_PATH")
    if event and os.path.exists(event):
        pr = (load_json(event).get("pull_request") or {})
        return pr.get("body") or ""
    return None


def run(suite: str, ctx: dict) -> int:
    try:
        suites = load_suites()
    except Exception as exc:  # a broken suites.json/schema halts loudly (config error)
        print(f"\nCONFIG ERROR: cannot load the suite declarations: {exc}", file=sys.stderr)
        return 2
    decl = suites.get(suite)
    if decl is None:
        print(f"\nCONFIG ERROR: suite '{suite}' is not declared in .engine/suites.json "
              f"(declared: {', '.join(sorted(suites))}).", file=sys.stderr)
        return 2
    gates = decl.get("context") == "blocking-gate"  # only a blocking-gate context can fail the run
    try:
        rules = load_rules()
    except Exception as exc:  # a broken check rule file halts loudly (config error), in plain language
        print(f"\nCONFIG ERROR: cannot load the check rules: {exc}", file=sys.stderr)
        return 2
    findings = []
    for rule in [r for r in rules if suite in r.get("suites", [])]:
        kind, tier = rule.get("kind"), rule.get("tier", "hard")
        fn = REGISTRY.get(kind)
        if fn is None:  # dangling kind: fail closed (a finding at the rule's tier)
            findings.append(finding(tier, f"Check rule '{rule.get('id')}' names "
                            f"unregistered kind '{kind}'; cannot evaluate (fails closed)."))
            continue
        try:
            _verdict, found = fn(rule, ctx)
        except Exception as exc:  # a kind that errors fails closed
            findings.append(finding("hard", f"Check rule '{rule.get('id')}' (kind "
                            f"'{kind}') errored and could not evaluate: {exc}"))
            continue
        findings.extend(found)
    report(suite, findings, gates)
    # Gate on the authoritative signal — any hard-severity finding — but only where
    # the suite's context is a blocking-gate. A callable's verdict flag is advisory;
    # the rule's tier (carried as the finding severity) and the suite context decide
    # where teeth land, so the exit code and report() can never disagree.
    hard_fired = any(f["severity"] == "hard" for f in findings)
    return 1 if (gates and hard_fired) else 0


def fmt(f: dict) -> str:
    where = ""
    if f.get("location"):
        l = f["location"]
        where = f"  [{l.get('file')}" + (f":{l['line']}" if l.get("line") else "") + "]"
    return f["message"] + where


def report(suite: str, findings: list, gates: bool) -> None:
    hard = [f for f in findings if f["severity"] == "hard"]
    soft = [f for f in findings if f["severity"] != "hard"]
    if soft:
        print(f"\nnotes ({len(soft)}):")
        for f in soft:
            print("  - " + fmt(f))
    if hard and gates:
        print(f"\nFAIL ({len(hard)} hard finding(s)) [suite: {suite}] — blocks the merge:")
        for f in hard:
            print("  - " + fmt(f))
    elif hard:
        print(f"\n{len(hard)} hard finding(s) [suite: {suite}] — advisory here, not blocking:")
        for f in hard:
            print("  - " + fmt(f))
    else:
        print(f"\nOK — suite '{suite}' passed, no hard findings.")


def main(argv: list) -> int:
    suite, body_file, i = "CI", None, 0
    while i < len(argv):
        if argv[i] == "--suite" and i + 1 < len(argv):
            suite, i = argv[i + 1], i + 2
        elif argv[i] == "--pr-body-file" and i + 1 < len(argv):
            body_file, i = argv[i + 1], i + 2
        else:
            print(f"unknown argument: {argv[i]}", file=sys.stderr)
            return 2
    return run(suite, {"pr_body": get_pr_body(body_file)})


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
