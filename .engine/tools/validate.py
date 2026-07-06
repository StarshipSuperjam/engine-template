#!/usr/bin/env python3
"""The core validation dispatcher — a thin core over a kind registry.

The check *inventory* is data (.engine/check/*.json) and the check *logic* is a
small registry of kind callables, so adding a check adds a rule file and never
edits this dispatcher (systems/guardrails/validation/README.md). This is the
`core` validation engine the stage-0 seed validator grew into; the engine ships
here, while the engine-self-validation rule *corpus* rides `validators-core`
(decision-log D-090), so only the three grandfathered seed rules are committed
(PR-body completeness, link integrity, and the re-homed protection guard).

The five closed core kinds, plus the `custom/script` escape hatch:
  - presence  — named sections/fields are present and non-empty.
  - schema    — a structured file, or a prose surface's YAML frontmatter, conforms to its
                governing JSON Schema (2020-12), resolved catalog-first (`governing_schema`),
                with a `params.schema` override for the cases the catalog cannot express.
                The loader is chosen by surface class: prose frontmatter is read by the
                YAML `frontmatter` reader, every other target by `load_json`.
  - shape     — a prose instance matches a template's shape-spec (required and
                allowed sections, ordering, a soft length budget).
  - coverage  — referential integrity; `params.mode` selects `links` (every relative
                Markdown link resolves) or `catalog` (every catalogued surface has its
                directory; no orphan surface directory).
  - coherence — the installed module set is consistent (dependency presence, acyclicity,
                version range). A directly-callable library entry the module manager
                invokes after an install; no live consumer until the module system lands
                (slice 6), so it ships built + fixture-tested.
  - custom/script — the escape hatch: run a committed script and map its result to
                findings (the §15 guards re-home onto this kind).
Module-provided kinds bind by presence at a later slice and must NOT extend the hardcoded
REGISTRY below; it holds the closed core set + the `custom/script` escape hatch.

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
  validate.py --check engine/check/<id>       # run ONE rule by id, outside any suite

A check is also a directly-callable unit, not only a trigger-driven one: --check
runs the single rule with that `id` and gates on a hard finding (exit 1), with no
suite involved. This is how a guard that must run from the trusted base — the §15
guardrail-weakening guard — is invoked from its own workflow (engine-guard.yml),
NOT from the head-checkout CI suite, so a pull request cannot run its own edited
guard (decision-log D-051). The by-id path loads only the check rules, never the
suite declarations, so a broken or loosened suites.json cannot strand or alter it.

The PR body is read from --pr-body-file, else from $GITHUB_EVENT_PATH
(.pull_request.body — the safe path: never interpolated into a shell command), else
treated as unavailable (the PR-body presence check fails OPEN locally, evaluates in CI).
"""
from __future__ import annotations
import datetime
import glob as _glob
import json
import os
import re
import subprocess
import sys

# yaml + jsonschema are the engine's ONLY third-party dependencies; they live in the
# uv-managed tool-runtime (.engine/.venv/). They are bound LAZILY (PEP 562 module
# __getattr__) rather than imported at module top, so `import validate` succeeds on the
# Python standard library alone — before that runtime exists. This is load-bearing for
# the first-run instantiator: it is the one engine tool that must run to BOOTSTRAP the
# runtime (it installs uv, then `uv sync`), so it cannot presuppose the packages the
# runtime provides (provisioning README §"Tool-runtime bootstrap"; D-156). When the
# runtime IS present the symbols resolve on first use exactly as a top-level import
# would — every `validate.<symbol>` consumer (e.g. wiring's ontology-entry check, the
# schema-validation tests) and validate's own frontmatter/schema paths are unchanged.
# A genuinely-absent package still raises ImportError at first use (fail-loud), never
# silently. Internal uses below additionally take a local import for the same reason
# (a bare module-global lookup does not trigger this module __getattr__).
_LAZY_THIRD_PARTY = {
    "yaml": ("yaml", None),
    "Draft202012Validator": ("jsonschema", "Draft202012Validator"),
    "SchemaError": ("jsonschema.exceptions", "SchemaError"),
}


def __getattr__(name):                                 # PEP 562: called only for names absent from globals()
    spec = _LAZY_THIRD_PARTY.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    module_name, attr = spec
    mod = importlib.import_module(module_name)          # raises ImportError loudly if the runtime is absent
    value = mod if attr is None else getattr(mod, attr)
    globals()[name] = value                            # cache: later access binds directly, skipping __getattr__
    return value


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


def disclosed_noop(message: str, location: dict | None = None) -> dict:
    """A DISCLOSED not-applicable finding: a check reporting "this doesn't apply in this
    context, nothing to do" — a disclosed no-op, never a silent skip (the design's
    disclosed-not-applicable grammar). Always `soft` (a no-op is by definition non-gating), and
    carries the optional `not_applicable` marker so report() can collapse these dormant notes
    away from the actionable ones. The marker is an additive finding.v1 key (the base fixes no
    closed property set); a finding WITHOUT it defaults to actionable, so the fail-safe is a
    no-op shown in full, never an actionable note hidden."""
    return {"severity": "soft", "message": message, "location": location, "not_applicable": True}


def env_override_path(var: str, default: "str | None" = None) -> "str | None":
    """Resolve an input-substitution env var to a path — the one shared seam the negative-fixture
    meta-check's custom/script units use (#286, D-256…D-260). When `var` is set and non-empty,
    return it resolved under ROOT (an absolute value is used as-is); otherwise return `default`
    unchanged. So when the variable is UNSET — every production run — the caller gets its own
    default and behaviour is byte-unchanged; the seam is inert outside a `run_unit` fixture run,
    which is the only path that sets the variable (around the child, restored after). One helper,
    one relative-to-ROOT resolution rule, so every seam is the same single audit rather than a
    dozen hand-rolled `os.environ`+`join` blocks."""
    value = os.environ.get(var)
    if not value:
        return default
    return value if os.path.isabs(value) else os.path.join(ROOT, value)


def loc(path: str, line: int | None = None) -> dict:
    return {"file": os.path.relpath(path, ROOT), "line": line}


def _is_pos_int(v) -> bool:
    """A positive integer, excluding bool (a Python int subclass) so True/False can
    never pass as a budget or count."""
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


# ---- shared helpers --------------------------------------------------------

def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# A `----- SECTION MARKER -----` prompt fence is two 3+-dash rails around words. To neutralize a line of
# UNTRUSTED text that could forge or prematurely close such a fence, look for the give-away: TWO dash rails
# on ONE line, with a letter present. A linear findall + an alpha scan (no backtracking regex, so no
# pathological input) catches every variant a line-anchored match would miss — text trailing or leading a
# rail, no spaces around the rails, a tab indent — while a SINGLE rail (a horizontal rule, `8<----- cut`),
# a letterless table delimiter row (`| --- | --- |`), an ISO date (`2026-06-01`), or a `--flag` is untouched.
# The rail class is ASCII hyphen plus the look-alike unicode dashes/bars (en/em dash, horizontal bar, minus,
# box-drawing) so a visually-identical forgery in another codepoint cannot slip past — a model reads the
# fence by its shape, not its bytes.
# ASCII hyphen (leading, so literal in the class), the U+2010–U+2015 dash/bar run, the minus sign, and the
# light/heavy box-drawing horizontals — the glyphs a forged rail could be drawn from.
_RAIL_CHARS = "-‐-―−─━"
_PROMPT_FENCE_RAIL_RE = re.compile("[" + _RAIL_CHARS + "]{3,}")


def defang_prompt_fence_markers(text: str) -> str:
    """Neutralize any line of UNTRUSTED `text` that could forge or prematurely close a `----- SECTION
    MARKER -----` prompt fence, so content fed between such markers cannot break out of its region. A line
    carrying TWO-or-more dash rails AND a letter has its dash runs trimmed to two dashes: the words are kept
    (no information is dropped), but the line can no longer read as a fence delimiter. A line with a single
    rail (a horizontal rule), no letters (a table delimiter row), or no 3-dash run at all (an ISO date, a
    `--flag`) is left exactly as it is. Linear in the text length — no regex backtracking."""
    out = []
    for line in text.split("\n"):
        if len(_PROMPT_FENCE_RAIL_RE.findall(line)) >= 2 and any(c.isalpha() for c in line):
            line = _PROMPT_FENCE_RAIL_RE.sub("--", line)   # trim every rail so the line can't be a fence
        out.append(line)
    return "\n".join(out)


def load_json(path: str):
    """Parse a JSON file to a data object. Raises (loud) on a missing or malformed
    file — the halt-on-malformed posture: a broken structured file fails loud rather
    than misleading the AI (schemas/README.md design commitment)."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _json_model(obj):
    """Render a YAML-loaded object into the JSON data model JSON Schema governs. YAML's
    native types are richer than JSON's: an unquoted ISO-8601 value (e.g. `date: 2026-06-03`)
    loads as a datetime.date, which a `{"type": "string"}` schema would reject — so a
    date/datetime scalar is rendered to its ISO-8601 string form, while numbers, booleans,
    null, and strings stay native (so `persistence: 3` stays a number). Recurses through
    mappings and sequences. This keeps frontmatter authors free of quoting rituals: the
    reader, not the document, reconciles YAML's type system with the schema's."""
    if isinstance(obj, dict):
        return {k: _json_model(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_model(v) for v in obj]
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    return obj


def frontmatter(path: str) -> dict:
    """Parse a prose file's YAML frontmatter (the block between the first two `---`
    fences) to a data object, normalized to the JSON data model (see _json_model). This
    is the frontmatter reader the locked schemas/validation foundation calls for ("parses
    a file or its YAML frontmatter to a data object before validating"; D-090's deferral,
    resolved). A file that does not open with a `---` fence yields {} — the governing
    schema's `required` then catches a frontmatter-less file. Malformed YAML RAISES (loud),
    caught by the caller as a fail-closed finding (the halt-on-malformed posture).
    `safe_load` only, never `load`: no arbitrary object construction from frontmatter."""
    import yaml                                         # lazy: see the module __getattr__ note (tool-runtime dep)
    text = read(path)
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)          # maxsplit=2: a `---` thematic break in the body stays in the body
    if len(parts) < 3:
        return {}
    data = yaml.safe_load(parts[1])
    return _json_model(data) if isinstance(data, dict) else {}


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
            return True, [disclosed_noop("PR body not available; completeness not "
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
    from jsonschema import Draft202012Validator        # lazy: see the module __getattr__ note (tool-runtime dep)
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
    """A structured file — or a prose file's YAML frontmatter — conforms to its governing
    JSON Schema (2020-12). Validates the PARSED data, not raw text. The loader is chosen by
    the target's surface CLASS (catalog-resolved, the same routing _governing_schema uses):
    a `prose` surface's frontmatter is read (and normalized to the JSON data model) by
    `frontmatter`; every other target — structured surfaces, and the override-schema targets
    that carry no surface record at all — is read by `load_json`, exactly as before. A
    malformed file, an unresolvable or offline schema reference, or a malformed governing
    schema is a loud finding (the halt-on-malformed posture), never an uncaught error and
    never a network fetch."""
    from jsonschema import Draft202012Validator        # lazy: see the module __getattr__ note (tool-runtime dep)
    from jsonschema.exceptions import SchemaError
    tier = rule["tier"]
    findings = []
    for path in target_files(rule):
        rel = os.path.relpath(path, ROOT)
        rec = _surface_record_for(rel)                 # None for override-schema targets (engine.json, state.json, manifests)
        is_prose = bool(rec) and rec.get("class") == "prose"
        try:
            data = frontmatter(path) if is_prose else load_json(path)
        except Exception as exc:
            malformed = ("has a malformed settings block (its YAML frontmatter could not be read)"
                         if is_prose else "is not valid JSON")
            findings.append(finding(tier, f"'{rel}' {malformed} and cannot be "
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
    with the first authored template (a later slice; it needs frontmatter parsing).
    An optional params.length_budget_overrides {rel: {budget, why}} carries a recorded,
    consented higher ceiling for one named operation (the override lives in this guarded
    rule, not the operation's own unguarded frontmatter, so raising a budget stays a
    deliberate act needing the operator's sign-off); an entry that is malformed (no integer
    budget, no recorded why) or names a file that no longer exists fails at the rule's tier,
    so a stale or unexplained override cannot rot into a silent grant."""
    tier = rule["tier"]
    params = rule.get("params") or {}
    required = params.get("required_sections", [])
    allowed = set(required) | set(params.get("allowed_sections", []))
    budget = params.get("length_budget")
    overrides = params.get("length_budget_overrides") or {}
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
        # length budget — a soft nudge only, regardless of the rule's tier. A per-file
        # override (a recorded, consented higher ceiling for one named operation) replaces
        # the rule-wide budget for its file; absent or malformed, the rule-wide budget
        # applies (a malformed override is caught as a hard finding below).
        ov = overrides.get(rel)
        ov_budget = ov.get("budget") if isinstance(ov, dict) else None
        file_budget = ov_budget if _is_pos_int(ov_budget) else budget
        if file_budget is not None:
            lines = len(body.splitlines())
            if lines > file_budget:
                findings.append(finding("soft", f"'{rel}' is {lines} lines, over its "
                                f"{file_budget}-line budget — a nudge to trim, never a block.", loc(path)))
    # Each override must be well-formed and live: an integer `budget` (the line ceiling) and a
    # recorded `why` (#273's recorded-rationale, made mechanical so a budget cannot be raised
    # without a stated reason), keyed to a file that still exists. A malformed entry, or a key
    # left dangling by a rename, would otherwise sit as inert, consented config that grants
    # nothing while looking like a live budget. Each failure is the rule's hard tier so a dead
    # grant cannot accumulate. Existence is checked on disk directly (not via the iterated file
    # list) so the guard is independent of which subset of files a given run evaluates.
    for key, ov in overrides.items():
        ov_budget = ov.get("budget") if isinstance(ov, dict) else None
        ov_why = ov.get("why") if isinstance(ov, dict) else None
        if not _is_pos_int(ov_budget) or not (isinstance(ov_why, str) and ov_why.strip()):
            findings.append(finding(tier, f"the length-budget override for '{key}' is incomplete — "
                            f"every override must give an integer 'budget' (the line ceiling) and a "
                            f"'why' (the recorded reason for the raise). Add both before merging.", None))
        if not os.path.isfile(os.path.join(ROOT, key)):
            findings.append(finding(tier, f"the length-budget override names '{key}', which is not a "
                            f"file in this project — a stale or mistyped key (often left by a rename). "
                            f"Update length_budget_overrides in this rule: remove the entry or repoint it.",
                            None))
    return (not any(f["severity"] == "hard" for f in findings)), findings


# ---- kind: coverage (referential integrity: links + catalog-coverage) ------

def kind_coverage(rule, ctx):
    """Referential-integrity coverage; `params.mode` selects the check. `links` folds in
    the former link-integrity (every relative Markdown link resolves). `catalog` is
    catalog-coverage (every catalogued surface has its location directory; no orphan
    surface directory). An unrecognized mode fails closed as a finding — `mode` is OPEN,
    not a fixed enum, so a later mode (e.g. knowledge fingerprint-coverage) adds no edit
    here. Per D-090 core ships this kind; the catalog-coverage RULE rides validators-core."""
    mode = (rule.get("params") or {}).get("mode")
    if mode == "links":
        return _coverage_links(rule, ctx)
    if mode == "catalog":
        return _coverage_catalog(rule, ctx)
    if mode == "fingerprint":
        return _coverage_fingerprint(rule, ctx)
    tier = rule["tier"]
    return False, [finding(tier, f"Check rule '{rule.get('id')}' (kind 'coverage') names an "
                   f"unrecognized mode '{mode}'; cannot evaluate (fails closed).")]


def _coverage_fingerprint(rule, ctx):
    """Knowledge fingerprint-coverage (§16 detection relay, decision-log D-090): re-derive the
    knowledge graph from the current surfaces and compare to the committed entities, so a surface
    that changed/was added/was removed without an entity regen is caught at CI. Detection is
    KNOWLEDGE's — this RELAYS to knowledge_gen.check() (the self-map drift-gate model), holding zero
    derivation logic here. The import is DEFERRED (function-local) so the coverage kind loads no
    module at import time; it resolves wherever this rule runs (.engine/tools/ is on sys.path). Any
    failure to load or derive FAILS CLOSED as a hard finding at the rule's tier — the gate cannot
    silently pass when the graph is missing or the generator is broken. The drift finding (when it
    fires) is knowledge's own plain-language message naming the regenerate fix."""
    tier = rule["tier"]
    try:
        import knowledge_gen  # deferred sibling import; resolves in CLI/import/test contexts
        f = knowledge_gen.check(tier=tier)
    except Exception as exc:
        return False, [finding(tier, f"Check rule '{rule.get('id')}' could not verify the knowledge "
                       f"graph (fingerprint coverage): {exc} (fails closed). {rule.get('message', '')}")]
    findings = [f] if f.get("severity") == "hard" else []
    return (len(findings) == 0), findings


def _coverage_links(rule, ctx):
    """Every relative Markdown link must resolve to an existing file. A link that resolves
    OUTSIDE the repo cannot be checked in a CI checkout, so it is a soft note, never hard."""
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


def catalog_coverage_findings(surfaces: dict, present_locations: set, tier: str,
                              message: str, infra=()) -> list:
    """Pure catalog-coverage: given the catalogued surfaces {name: record} and the set of
    surface-location strings present on disk, return findings — a catalogued surface whose
    `location` directory is absent, or a present surface-location no surface claims (an
    orphan), skipping a known non-surface infra location. The 'uncatalogued surface in use'
    leg (a surface referenced with no record) is not mechanically general here — a named
    limitation. Kept pure so it is testable without the live filesystem."""
    catalogued = {rec.get("location") for rec in surfaces.values()}
    findings = []
    for name, rec in surfaces.items():
        if rec.get("location") not in present_locations:
            findings.append(finding(tier, f"Catalogued surface '{name}' has no directory at "
                            f"'{rec.get('location')}'. {message}"))
    for location in sorted(present_locations):
        if location not in catalogued and location not in set(infra):
            findings.append(finding(tier, f"Directory '{location}' exists but no catalogued "
                            f"surface claims it (orphan surface directory). {message}"))
    return findings


def _coverage_catalog(rule, ctx):
    """catalog-coverage over the live surface catalog + filesystem (see the pure
    catalog_coverage_findings); non-surface infra directories are passed via
    params.infra_dirs. The catalog source and the walk root default to the live globals
    (CATALOG_PATH / ROOT — what CI runs); run_unit (#286, D-256…D-260) may override BOTH
    via ctx (coverage_catalog / coverage_root) to point the REAL callable at a seeded
    mini-tree, so the meta-check witnesses this exact entry point. Production callers pass
    neither key, so the behaviour is byte-unchanged."""
    tier = rule["tier"]
    catalog_path = ctx.get("coverage_catalog", CATALOG_PATH)
    base = ctx.get("coverage_root", ROOT)
    try:
        surfaces = load_json(catalog_path).get("surfaces", {})
    except Exception as exc:
        return False, [finding(tier, f"Could not read the surface catalog to check coverage: "
                       f"{exc}. {rule['message']}", loc(catalog_path))]
    infra = set((rule.get("params") or {}).get("infra_dirs", []))
    present = set()
    for root in (".engine", ".claude"):
        abs_root = os.path.join(base, root)
        if os.path.isdir(abs_root):
            for name in sorted(os.listdir(abs_root)):
                if os.path.isdir(os.path.join(abs_root, name)):
                    present.add(f"{root}/{name}/")
    findings = catalog_coverage_findings(surfaces, present, tier, rule["message"], infra)
    return (not any(f["severity"] == "hard" for f in findings)), findings


def markdown_files(exclude_dirs: set) -> list:
    out = []
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d != ".git"
                   and os.path.relpath(os.path.join(dirpath, d), ROOT) not in exclude_dirs]
        out.extend(os.path.join(dirpath, f) for f in files if f.endswith(".md"))
    return out


# ---- kind: coherence (the installed module set is consistent) --------------

def _ver_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")) or (0,)


def _version_in_range(version: str, spec: str) -> bool:
    """A pragmatic version-range check on dotted-integer versions: a space/comma list of
    comparators (>=, >, <=, <, ==/=, ^). The exact manifest range grammar is pinned by the
    module-system manifest schema (slice 6); this is the stable presence/acyclicity/range
    seam the module manager calls — slice 6 may extend the comparator set."""
    vt = _ver_tuple(version)
    for part in re.split(r"[,\s]+", (spec or "").strip()):
        if not part:
            continue
        m = re.match(r"(\^|>=|<=|==|=|>|<)?\s*(.+)", part)
        op, ref = (m.group(1) or "=="), _ver_tuple(m.group(2))
        if op in ("==", "=") and vt != ref:
            return False
        if op == ">=" and vt < ref:
            return False
        if op == ">" and vt <= ref:
            return False
        if op == "<=" and vt > ref:
            return False
        if op == "<" and vt >= ref:
            return False
        if op == "^" and not (vt >= ref and vt[:1] == ref[:1]):
            return False
    return True


def _dependency_cycle(by_id: dict) -> list:
    """A dependency cycle as a list of module ids, or [] if acyclic (DFS over `depends`)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {k: WHITE for k in by_id}
    stack = []

    def visit(node):
        color[node] = GRAY
        stack.append(node)
        for dep in (by_id.get(node, {}).get("depends") or {}):
            if dep not in by_id:
                continue
            if color.get(dep) == GRAY:
                return stack[stack.index(dep):] + [dep]
            if color.get(dep) == WHITE:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for n in list(by_id):
        if color[n] == WHITE:
            found = visit(n)
            if found:
                return found
    return []


def coherence_findings(manifests: list, tier: str, message: str) -> list:
    """Pure module-set coherence: given installed module manifests
    [{id, version, depends: {id: range}}, ...], return findings for an absent dependency,
    a version outside a declared range, or a dependency cycle. The slice-6 module manager
    imports this directly (a library call, not a suite trigger)."""
    by_id = {m.get("id"): m for m in manifests}
    findings = []
    for m in manifests:
        for dep_id, dep_range in (m.get("depends") or {}).items():
            if dep_id not in by_id:
                findings.append(finding(tier, f"Module '{m.get('id')}' depends on '{dep_id}', "
                                f"which is not installed. {message}"))
            elif dep_range and not _version_in_range(by_id[dep_id].get("version"), dep_range):
                findings.append(finding(tier, f"Module '{m.get('id')}' needs '{dep_id}' {dep_range}, "
                                f"but version {by_id[dep_id].get('version')} is installed. {message}"))
    cycle = _dependency_cycle(by_id)
    if cycle:
        findings.append(finding(tier, f"Module dependency cycle: {' -> '.join(cycle)}. {message}"))
    return findings


def topological_order(manifests: list) -> list:
    """The present manifests in dependency (topological) order — every module appears AFTER the
    modules it `depends` on. Deterministic and INPUT-ORDER-INDEPENDENT: ready modules are emitted in
    alphabetical id order (Kahn's algorithm), so the order is a function of the dependency edges, not
    of the input sequence. A `depends` id not present in the set is ignored (an absent dependency is a
    separate coherence finding, not this function's concern — mirrors `_dependency_cycle`). CYCLE-SAFE:
    if a dependency cycle leaves modules unresolved (a cycle is flagged hard by `coherence_findings`),
    the unresolved modules are appended in alphabetical id order so a caller never crashes — the result
    stays deterministic. Pure (no IO): the self-map (slice 8) renders modules in this order and the
    module manager (slice 25) installs/migrates in it (module-system/README.md §Dependency resolution
    — "the build order is its topological sort")."""
    by_id = {m.get("id"): m for m in manifests}
    indeg = {mid: 0 for mid in by_id}            # count of THIS module's deps present in the set
    dependents: dict = {mid: [] for mid in by_id}  # dep id -> modules that depend on it
    for mid, m in by_id.items():
        for dep in (m.get("depends") or {}):
            if dep in by_id:
                indeg[mid] += 1
                dependents[dep].append(mid)
    ready = sorted(mid for mid, d in indeg.items() if d == 0)
    order: list = []
    while ready:
        node = ready.pop(0)                       # alphabetically-smallest ready id
        order.append(node)
        newly = []
        for child in dependents[node]:
            indeg[child] -= 1
            if indeg[child] == 0:
                newly.append(child)
        if newly:
            ready = sorted(ready + newly)          # keep ready alphabetical -> deterministic
    if len(order) < len(by_id):                    # cycle fallback: emit the rest alphabetically
        emitted = set(order)
        order.extend(sorted(mid for mid in by_id if mid not in emitted))
    return [by_id[mid] for mid in order]


def ownership_findings(inventory: list, claims: dict, exempt, tier: str, message: str) -> list:
    """Pure ownership coherence — the third coherence leg, beside dependency-coherence
    (coherence_findings) in the validation foundation. Given the engine file `inventory`
    (relpaths), a `claims` map {relpath: [module-id, ...]} of which present modules'
    `provides` match each file, and the set of `exempt` relpaths a module legitimately need
    not claim (the named foundation infrastructure artifacts + the module manifests
    themselves), return a finding for every ORPHAN (an engine file no module claims and that
    is not exempt) and every DOUBLE-CLAIM (a file two or more modules claim). The leg is
    kept pure — the filesystem walk and the glob matching that build `inventory`/`claims`,
    and the policy of what is exempt, live in the module-coherence consumer — so it is
    testable without the live filesystem and the module manager (slice 25) reuses it."""
    exempt = set(exempt)
    findings = []
    for rel in inventory:
        owners = claims.get(rel) or []
        if len(owners) > 1:
            findings.append(finding(tier, f"Engine file '{rel}' is claimed by more than one "
                            f"module ({', '.join(sorted(owners))}); exactly one module must "
                            f"own each engine file. {message}", loc(os.path.join(ROOT, rel))))
        elif not owners and rel not in exempt:
            findings.append(finding(tier, f"Engine file '{rel}' belongs to no module (an "
                            f"orphan); add it to a module's 'provides', or remove it. "
                            f"{message}", loc(os.path.join(ROOT, rel))))
    return findings


def wiring_findings(declared: list, tier: str, message: str) -> list:
    """Pure forward wiring coherence (declared -> applied) — the wiring leg of module coherence,
    beside dependency (coherence_findings) and ownership (ownership_findings). Given `declared`, a
    list of (module_id, seam_type, target_label, is_applied: bool) — one per `wires` directive of
    each present manifest, with the applied flag computed live by the module-coherence consumer (so
    this leg stays pure and filesystem-free, exactly like ownership_findings) — return a hard `tier`
    finding for every directive NOT applied in its shared target. Uniform across all five seams: a
    declared wire the engine has not applied, or whose applied entry has drifted, is real coherence
    drift.

    The MCP carve-out is APPROVAL-BLINDNESS, not a soft tier: the applied flag for an `mcp` wire
    reflects the committed `.mcp.json` definition (engine wiring), never the operator's runtime
    approval (operator state, surfaced loudly at boot and in the control-plane PR-Validation section,
    not here) — so a defined-but-unapproved server is simply is_applied=True and never flags
    (module-system/README.md §"MCP registration", §Coherence). FORWARD direction only; the orphan-wire
    REVERSE direction (nothing engine-identified applied that no manifest declares) is the companion
    `orphan_wire_findings` below, over the module-coherence consumer's per-seam applied-wire enumerator."""
    findings = []
    for module_id, seam_type, target_label, applied in declared:
        if not applied:
            findings.append(finding(tier, f"Module '{module_id}' declares a {seam_type} wire that is "
                            f"not applied in {target_label}; re-run the install / wiring step to apply "
                            f"it, then re-check. {message}"))
    return findings


def orphan_wire_findings(applied: list, declared_ids, tier: str, message: str) -> list:
    """Pure REVERSE wiring coherence (applied -> declared) — the orphan-wire leg, the inverse of the
    forward wiring_findings (declared -> applied). Together they are the full bidirectional
    "Declared wiring <-> applied wiring" leg the module system mandates
    (systems/grammar/module-system/README.md §Coherence): "everything a present manifest's `wires`
    declares is applied in the shared files, AND nothing engine-identified is applied that no manifest
    declares."

    Given `applied`, a list of (seam_type, identity_key, target_label) for every ENGINE-IDENTIFIED entry
    currently applied in the platform-shared files (built live by the module-coherence consumer, so this
    leg stays pure and filesystem-free exactly like ownership_findings / wiring_findings), and
    `declared_ids`, the set of (seam_type, identity_key) every present manifest's `wires` declares, return
    a hard `tier` finding for any applied engine entry whose identity NO manifest declares — a stale
    leftover after an incomplete uninstall (Risk R5).

    Two carve-outs make the live seam set the three PLATFORM-SHARED-file seams (hook, mcp, gitignore) —
    the only place an orphan has no other governance:
      - PERMISSION is absent from `applied` by construction: a bare permission string is not
        engine-identifiable, so reversal "errs toward leaving it" and coherence cannot reach that honest
        residue (module-system §"The wiring library"). The consumer's enumerator never emits one.
      - ONTOLOGY-ENTRY is out of scope HERE: its target is the engine-OWNED catalog, already covered by
        the ownership leg (every .engine/ file must be claimed) and the SEPARATE locked catalog-coverage
        gate ("ontology catalog coverage is an already-separate locked gate, so [it is] not part of module
        coherence", module-system §Coherence). The forward leg still checks a declared ontology-entry wire
        is applied; only the reverse (orphan) direction defers to those gates. The asymmetry is sound: the
        reverse leg exists to catch orphans in the platform-shared files that NOTHING ELSE watches; an
        engine-owned-file orphan is already watched twice.

    Drift is reported ONCE, by the forward leg, for the NAME/KEY-identity seams (mcp, gitignore): their
    identity is the server name / fence id, so a content-drifted entry keeps the same identity, still
    matches a declared directive here, and is therefore not an orphan. A HOOK is different by the spec's
    own identity model — a hook's identity is the full {event, matcher, type, command} tuple
    (module-system §"The wiring library") — so editing an engine hook's command is, by that definition,
    the declared hook gone (forward leg: not applied) AND a new undeclared engine hook present (reverse
    leg: orphan). Those are two accurate findings about two real facts, not a double-count of one."""
    declared = set(declared_ids)
    findings = []
    for seam_type, key, target_label in applied:
        if (seam_type, key) not in declared:
            findings.append(finding(tier, f"An engine-identified {seam_type} setting is present in "
                            f"{target_label} that no installed module declares — a leftover from an "
                            f"incomplete removal. Remove it, or re-install the module it belonged to, "
                            f"then re-check. {message}"))
    return findings


def interface_resolution_findings(interfaces: list, present_impls: dict, present_handles, tier: str,
                                  message: str) -> list:
    """Single-active resolution + structural conformance for the declared interfaces — the locked
    `coherence` posture for interfaces (introduces no new check-kind, surfaces never silently picks).
    The third coherence concern beside dependency-coherence (coherence_findings) and ownership
    (ownership_findings). Pure + fixture-testable.

    `interfaces` is the list of interface declaration dicts; `present_impls` maps an interface id to
    the present NON-DEFAULT implementations [{'handle': ..., 'operations': [op-name, ...]}, ...]
    discovered by presence; `present_handles` is the set of all present/answerable implementation
    handles (default fallback and non-default alike). Findings:
      - HARD: more than one non-default implementation present for one interface — exactly one may
        answer (single-active; a silent arbitrary pick is the trust breach the design forbids).
      - HARD: a present implementation that does not provide every operation the interface declares
        (structural conformance; full behavioural equivalence is a test concern, not a check).
      - NOTE: an interface with no non-default implementation whose named fallback is not yet present
        — an expected pending-setup state (e.g. a fallback that ships with a later module), surfaced
        as setup, never as coherence drift.

    No live rule wires this in core: only the shipped fallback is present and the module manager that
    installs richer implementations is a later slice, so the single-active finding has nothing live to
    fire on (the slice-6 coherence_findings/ownership_findings precedent — built + fixture-tested, no
    live rule). The module manager discovers the present set and runs this post-install."""
    present_handles = set(present_handles or [])
    findings = []
    for decl in interfaces:
        iface_id = decl.get("id")
        declared_ops = {op.get("name") for op in (decl.get("operations") or [])}
        fallback_handle = (decl.get("fallback") or {}).get("handle")
        impls = [im for im in (present_impls.get(iface_id) or [])
                 if im.get("handle") != fallback_handle]
        if len(impls) > 1:
            handles = ", ".join(sorted(str(im.get("handle")) for im in impls))
            findings.append(finding(tier, f"Interface '{iface_id}' has more than one implementation "
                            f"present ({handles}); exactly one may answer (single-active). Remove all "
                            f"but one. {message}"))
        for im in impls:
            missing = sorted(declared_ops - set(im.get("operations") or []))
            if missing:
                findings.append(finding(tier, f"Implementation '{im.get('handle')}' for interface "
                                f"'{iface_id}' does not provide the operation(s) {missing} the "
                                f"interface declares. {message}"))
        if not impls and fallback_handle not in present_handles:
            findings.append(finding("note", f"Interface '{iface_id}' is answered by its named "
                            f"fallback '{fallback_handle}', which is not yet present — an expected "
                            f"pending-setup state (its implementation ships with a later module). "
                            f"{message}"))
    return findings


def agent_coherence_findings(agents: list, tier: str, message: str) -> list:
    """Pure persona-set coherence — the agent surface's coherence leg, beside dependency
    (coherence_findings), ownership (ownership_findings), forward wiring (wiring_findings), and
    interface resolution (interface_resolution_findings). Given the present personas' parsed
    frontmatter [{name, role, lens?, model-tier, ...}, ...], return a finding for:

      - a `role` outside the closed set {plan-review, worker, pre-submission-review, audit} (an
        'unknown role' is impossible by construction once caught; agents/README §Coherence),
      - a `model-tier` outside the closed demand set {judgment, mechanical} (D-100),
      - a `lens` declared by a `worker` or `audit` role — the symmetric guard to the closed-role
        check: those two roles carry no lens (agents/README: "a worker or audit instance that
        declares one is a coherence finding"). Scoped to the two KNOWN lensless roles, not "any
        non-review role", so an unknown role carrying a lens yields only the role finding (no
        redundant second finding), and a review role's lens is valid.
      - a `permissions: read-only` persona that does not actually BLOCK the authoritative-write
        tools (Edit, Write, NotebookEdit) — the realization of the design's "permissions maps to the
        Claude Code tool/permission restrictions the platform enforces" (agent.v1 `permissions` /
        `tools` / `disallowedTools`; D-272). A read-only persona blocks a write tool iff it lists it
        in `disallowedTools` OR declares a `tools` allowlist (a list) that omits it; a read-only
        persona that declares NEITHER inherits every tool (the inherit-all trap) and is a finding.
        HONEST LIMIT: this enforces only that the native file-writing tools (Edit/Write/NotebookEdit)
        are blocked — it deliberately does NOT police `Bash` (which the execution roles
        pre-submission-review/audit legitimately keep to run the suite in a scratch worktree —
        qa-review/README dry-run) nor any write-capable MCP tools the session may expose; confining
        those tool-/shell-side writes is the orchestration worktree's + the protected-branch merge
        gate's job, not a frontmatter invariant this static leg can see. A STRING-valued
        disallowedTools/tools is treated CONSERVATIVELY (a string denylist blocks nothing here; a
        string `tools` is not a write-excluding allowlist), so blocking must come from the list
        form — this errs toward a false finding, never a false pass.

    It does NOT do the dangling/unconsumed-lens check (an installed review lens nothing in the
    orchestration consumes): that needs build-orchestration's consumed-lens set (which gate
    consumes which lens), deferred to that surface's design (agents/README §Coherence) — the
    build-orchestration slice. The closed sets live HERE (the leg), NOT as agent.v1 enums: agent.v1
    governs role/model-tier/lens as well-formed strings and this leg owns membership, so each set
    is defined in one place (the locked grammar routes membership through the coherence kind, not
    the schema). `lens` stays an OPEN vocabulary — only role/model-tier are closed sets.

    Pure + fixture-testable, mirroring the other legs: the persona frontmatter is parsed by the
    consumer (the build-orchestration roster derivation) and passed in, so this stays
    filesystem-free. No live rule wires this in core: the build roster is derived by
    build-orchestration (a later slice) and no persona instance ships with the grammar (D-066), so
    the leg has nothing live to fire on — the interface_resolution_findings precedent (built +
    fixture-tested, no live rule). That slice discovers the present persona set and runs this
    alongside the deferred dangling-lens check."""
    roles = {"plan-review", "worker", "pre-submission-review", "audit"}
    lensless_roles = {"worker", "audit"}   # the recognized roles that carry no lens
    tiers = {"judgment", "mechanical"}
    write_tools = ("Edit", "Write", "NotebookEdit")   # the authoritative-write tools a read-only persona must block
    findings = []
    for a in agents:
        name = a.get("name", "(unnamed)")
        role = a.get("role")
        if role not in roles:
            findings.append(finding(tier, f"Persona '{name}' declares role '{role}', which is not a "
                            f"recognized role ({sorted(roles)}). {message}"))
        mtier = a.get("model-tier")
        if mtier not in tiers:
            findings.append(finding(tier, f"Persona '{name}' declares model-tier '{mtier}', which is "
                            f"not a recognized demand level ({sorted(tiers)}). {message}"))
        if a.get("lens") and role in lensless_roles:
            findings.append(finding(tier, f"Persona '{name}' has role '{role}', which carries no lens, "
                            f"but declares lens '{a.get('lens')}'; only the review roles carry a "
                            f"lens. {message}"))
        if a.get("permissions") == "read-only":
            allow, deny = a.get("tools"), a.get("disallowedTools")
            allow_list = allow if isinstance(allow, list) else None        # a STRING tools (e.g. "inherit") is not an excluding allowlist
            deny_set = {str(t) for t in deny} if isinstance(deny, list) else set()
            if allow is None and deny is None:
                findings.append(finding(tier, f"Persona '{name}' declares permissions: read-only but "
                                f"declares neither a tools allowlist nor a disallowedTools denylist, so it "
                                f"inherits every tool — including the authoritative-write tools "
                                f"{list(write_tools)}. Block them via disallowedTools (or a write-excluding "
                                f"tools allowlist). {message}"))
            else:
                unblocked = [t for t in write_tools
                             if t not in deny_set and not (allow_list is not None and t not in allow_list)]
                if unblocked:
                    findings.append(finding(tier, f"Persona '{name}' declares permissions: read-only but does "
                                    f"not block the authoritative-write tool(s) {unblocked}: a read-only persona "
                                    f"must block Edit/Write/NotebookEdit via disallowedTools or omit them from a "
                                    f"tools allowlist. {message}"))
    return findings


def skill_coherence_findings(skills: list, tier: str, message: str) -> list:
    """Pure skill-set coherence — the skill surface's coherence leg, beside dependency
    (coherence_findings), ownership (ownership_findings), forward wiring (wiring_findings),
    interface resolution (interface_resolution_findings), and persona coherence
    (agent_coherence_findings). Given the present skills' parsed SKILL.md frontmatter
    [{description, invocation?, disable-model-invocation?, user-invocable?, ...}, ...], return a
    finding for:

      - an `invocation` outside the closed set {model-auto, operator-typed, model-only} (an
        OMITTED invocation is model-auto, the platform default — NOT a finding; skills/README
        §"The invocation axis"),
      - an invocation that DISAGREES with the real platform flags the instance carries — the
        self-election leak-guard, the load-bearing CROSS-FIELD rule: the engine reads
        `invocation`, Claude Code reads the flags (disable-model-invocation: true makes a skill
        operator-typed; user-invocable: false makes it model-only), and the two must agree.
        `operator-typed` without `disable-model-invocation: true` is the safety case — the model
        could still self-invoke. A restricting flag carried under a model-auto (or omitted)
        declaration is the symmetric case — the skill behaves restricted but does not declare it.

    An invocation OUTSIDE the set yields only the one membership finding (the flag-mapping is
    skipped for that instance — the agent-leg precedent: one unknown value, one finding). The
    closed set lives HERE (the leg), NOT as a skill.v1 enum: skill.v1 governs `invocation` as a
    well-formed string and this leg owns both membership AND the cross-field flag-mapping a schema
    enum cannot express, so the set is defined in one place (skills/README §"The invocation
    axis", the platform-flag table).

    Pure + fixture-testable, mirroring the other legs: the SKILL.md frontmatter is parsed by the
    consumer and passed in, so this stays filesystem-free. No live rule wires this in core: the
    consumer (the slice-26 operator verbs, which discover the present skill set and decide the
    engine-vs-operator scope) runs this live and proves the Build-entry verb's self-election
    safety on the live platform — the interface_resolution_findings / agent_coherence_findings
    precedent (built + fixture-tested, no live rule). ZERO skill instances ship with the grammar,
    so the leg has nothing live to fire on yet."""
    invocations = {"model-auto", "operator-typed", "model-only"}
    findings = []
    for s in skills:
        name = s.get("name") or "(unnamed)"
        inv = s.get("invocation")
        if inv is not None and inv not in invocations:
            findings.append(finding(tier, f"Skill '{name}' declares invocation '{inv}', which is not "
                            f"a recognized invocation value ({sorted(invocations)}). {message}"))
            continue   # an unknown value yields only the one finding (skip the flag-mapping)
        effective = inv or "model-auto"   # an omitted invocation is the platform default
        dmi = s.get("disable-model-invocation") is True
        uif = s.get("user-invocable") is False
        # Expected flags per effective invocation: model-auto -> neither; operator-typed -> dmi
        # only; model-only -> uif only. Any mismatch is a self-election / leak-guard finding.
        if effective == "operator-typed":
            if not dmi:
                findings.append(finding(tier, f"Skill '{name}' declares invocation 'operator-typed' "
                                f"but does not carry 'disable-model-invocation: true', so the model "
                                f"could still self-invoke it. {message}"))
            if uif:
                findings.append(finding(tier, f"Skill '{name}' declares invocation 'operator-typed' "
                                f"but also carries 'user-invocable: false' (model-only); the two "
                                f"conflict. {message}"))
        elif effective == "model-only":
            if not uif:
                findings.append(finding(tier, f"Skill '{name}' declares invocation 'model-only' but "
                                f"does not carry 'user-invocable: false', so it is not hidden from the "
                                f"operator's menu. {message}"))
            if dmi:
                findings.append(finding(tier, f"Skill '{name}' declares invocation 'model-only' but "
                                f"also carries 'disable-model-invocation: true' (operator-typed); the "
                                f"two conflict. {message}"))
        elif dmi or uif:   # effective is model-auto (declared or omitted) but a flag restricts it
            carried = "disable-model-invocation: true" if dmi else "user-invocable: false"
            declared = ("declares invocation 'model-auto'" if inv
                        else "carries no invocation (defaulting to model-auto)")
            findings.append(finding(tier, f"Skill '{name}' {declared} but carries '{carried}', which "
                            f"restricts who may invoke it; declare the matching invocation "
                            f"(operator-typed or model-only) so the engine and the platform agree. "
                            f"{message}"))
    return findings


def block_budget_findings(blocks: list, tier: str, message: str) -> list:
    """Pure hook block-budget coherence — the hooks substrate's coherence leg, beside dependency
    (coherence_findings), ownership (ownership_findings), forward wiring (wiring_findings), interface
    resolution (interface_resolution_findings), persona coherence (agent_coherence_findings), and
    skill coherence (skill_coherence_findings). The block-budget law
    (systems/infrastructure/hooks/README.md §"The block-budget law"): only PreToolUse and Stop may
    HARD-BLOCK; every other event nudges or injects. The platform would let PreCompact /
    UserPromptSubmit / SubagentStop block too — the Engine declines (a local hard-block buys friction
    without proportional trust; principles §6).

    Given the present block-eligible registrations [{event, name?, owner?}, ...] — assembled by the
    consumer from the owning systems' declarations and passed in (filesystem-free, the agent/skill
    precedent) — return a finding for any block declared on an event OUTSIDE {PreToolUse, Stop}.

    The block-eligible invariant set STARTS EMPTY: this leg names no invariant itself (hooks owns the
    BUDGET — which events may block — not the invariants). Owning systems register their block
    additively — close's findings-disposition Stop block (slice 22), modes' explore write-gate
    PreToolUse block (slice 21) — so with the set empty (core today) this returns nothing. No live
    rule wires it in core: the registration source is the committed `.claude/settings.json` + the
    owning systems' declarations, born at the first hook-wiring slice (slice 20), which runs this leg
    live — the interface_resolution_findings / agent_coherence_findings precedent (built +
    fixture-tested, no live rule). The closed eligible set lives HERE (the leg) and in the runtime
    harness (hooks.py); the locked hooks README is the single source both cite."""
    eligible = {"PreToolUse", "Stop"}
    findings = []
    for b in blocks:
        event = b.get("event")
        if event not in eligible:
            name = b.get("name") or b.get("owner") or "(unnamed)"
            findings.append(finding(tier, f"The hook block '{name}' is declared on the '{event}' "
                            f"event, but only {sorted(eligible)} may hard-block; every other event "
                            f"nudges or injects. {message}"))
    return findings


def effective_policy_values(default: dict, override: dict, *, structural_keys, tier: str,
                            message: str) -> tuple[dict, list]:
    """Merge a per-deployment operator policy-override over a policy's shipped default tuning values,
    per-key at read time — the core merge mechanism for the operator policy-override (D-167: "the
    merge-mechanism is core"; policies/README §Per-deployment value override). Returns
    (effective, findings): the effective value map a consumer reads, plus a finding per refused key.

    Given the shipped `default` value map (read from the policy frontmatter by the consumer) and a sparse
    `override` map (committed operator config — its file path/format belong to the authoring slice, NOT
    this function), merge each override key over the default, EXCEPT:

      - a key in `structural_keys` — a value that encodes a structural LAW an override may never retune
        (for attention, the partition precedence + trim order, so "blocking-debt-first holds by
        construction") — is REFUSED and surfaced; the shipped default value stands.
      - a key absent from `default` — a STALE key (a knob the policy no longer carries after an upgrade) —
        falls back to the default (it is simply not present in the result) and is surfaced (the
        freshly-stale catch at the merge; a lingering one is the audit's job, audits/README).

    An unset eligible key (a partial override) silently keeps the default — the normal case, no finding.
    Eligibility is the CALLER'S parameter (`structural_keys`), so one mechanism serves attention (structural
    = precedence + trim) and telemetry's triage-threshold (its own set, empty) without re-derivation — and a
    consumer never imports another substrate's tool to merge, so there is no layering inversion.

    PURE + fixture-testable: the merged value is static data, so a deterministic consumer (attention's
    ranking function) stays deterministic — the merge adds another recorded input; no clock, no IO. The
    override is taken as DATA: this function does not read a file — the consumer (or the authoring slice)
    loads it. Findings are ordered by key (sorted) for a reproducible result. No live rule wires this in
    core yet: the `custom/script` stale-key rule that runs it on a committed override file needs that file's
    path (the authoring slice's leaf), so the leg is built + fixture-tested here and consumed live later
    (the interface_resolution_findings / agent_coherence_findings precedent)."""
    structural = set(structural_keys)
    effective = dict(default)
    findings = []
    for key in sorted(override):
        if key in structural:
            findings.append(finding(tier, f"Override key '{key}' sets a structural-law value that is not "
                            f"override-eligible; it is refused and the shipped default stands. {message}"))
        elif key not in default:
            findings.append(finding(tier, f"Override key '{key}' is not carried by the policy's shipped "
                            f"default; it is ignored and falls back to the default. {message}"))
        else:
            effective[key] = override[key]
    return effective, findings


def kind_coherence(rule, ctx):
    """The installed module set is consistent. A directly-callable library entry the
    slice-6 module manager invokes right after an install; the manifests it reads land with
    the module system, so in core it ships built + fixture-tested with no live rule. As a
    kind callable it reads the manifest set from ctx['manifests'] (empty until slice 6)."""
    tier = rule["tier"]
    findings = coherence_findings(ctx.get("manifests") or [], tier, rule.get("message", ""))
    return (not any(f["severity"] == "hard" for f in findings)), findings


# ---- kind: custom/script (the escape hatch; runs a committed script) -------

def kind_custom_script(rule, ctx):
    """Run a committed, in-repo script (params.script, resolved under ROOT) with the engine
    interpreter (sys.executable, so it inherits the engine venv). The rule's tier is passed
    via ENGINE_RULE_TIER; the repo token (GITHUB_TOKEN) is passed ONLY if the rule opts in
    with params.pass_token, so the secret reaches only the scripts that need it. CONTRACT:
      exit 0 + stdout a parseable finding.v1 JSON array -> those findings pass through (the
        script sets each severity: the rule tier for a real finding, `soft` for a
        could-not-evaluate note — the fail-open-locally pattern). An empty array = pass.
      non-zero exit OR unparseable stdout -> ONE hard fail-closed finding regardless of the
        rule's tier, so a crashing or uninstalled guard can never silently pass.
    stdout is the machine channel (JSON only); human prose lives in each finding's message."""
    tier = rule["tier"]
    script = (rule.get("params") or {}).get("script")
    if not script:
        return False, [finding("hard", f"Check rule '{rule.get('id')}' (kind 'custom/script') "
                       f"names no params.script; cannot evaluate (fails closed).")]
    path = os.path.normpath(os.path.join(ROOT, script))
    if not (os.path.abspath(path) == ROOT or os.path.abspath(path).startswith(ROOT + os.sep)):
        return False, [finding("hard", f"Check rule '{rule.get('id')}' (kind 'custom/script') names "
                       f"a script outside the repository ('{script}'); refusing to run it (fails "
                       f"closed). A custom/script must be a committed, reviewed, in-repo file.")]
    if not os.path.isfile(path):
        return False, [finding("hard", f"Check rule '{rule.get('id')}' (kind 'custom/script') script "
                       f"'{script}' does not exist; cannot evaluate (fails closed).")]
    # Scope the child environment: a custom/script does NOT inherit the CI repository token
    # unless its rule opts in (params.pass_token), so the secret reaches only the scripts that
    # need it (the protection guard) rather than every script the suite runs.
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
    env["ENGINE_RULE_TIER"] = tier
    if (rule.get("params") or {}).get("pass_token") and os.environ.get("GITHUB_TOKEN"):
        env["GITHUB_TOKEN"] = os.environ["GITHUB_TOKEN"]
    try:
        proc = subprocess.run([sys.executable, path], capture_output=True, text=True,
                              env=env, timeout=120)
    except Exception as exc:
        return False, [finding("hard", f"Check '{rule.get('id')}' could not run '{script}': "
                       f"{exc} (fails closed).")]
    if proc.returncode != 0:
        detail = (proc.stdout or proc.stderr or f"exit {proc.returncode}").strip()
        return False, [finding("hard", f"Check '{rule.get('id')}' could not verify — its script "
                       f"exited with an error: {detail[:300]} (fails closed).")]
    try:
        raw = json.loads(proc.stdout or "[]")
        if not isinstance(raw, list):
            raise ValueError("expected a JSON array of findings")
    except Exception:
        return False, [finding("hard", f"Check '{rule.get('id')}' produced unreadable output; "
                       f"cannot verify (fails closed).")]
    findings = []
    for f in raw:
        if not isinstance(f, dict):
            return False, [finding("hard", f"Check '{rule.get('id')}' produced a malformed finding "
                           f"(not an object); cannot verify (fails closed).")]
        # Reconstruct on the finding.v1 base with an EXPLICIT allow-list — severity, message,
        # location, plus the optional `not_applicable` disclosed-no-op marker — so a script's
        # disclosed_noop() survives re-ingestion (report() can collapse it) while no other
        # author-controllable key leaks through this trust boundary.
        rebuilt = finding(f.get("severity", tier), f.get("message", ""), f.get("location"))
        if f.get("not_applicable"):
            rebuilt["not_applicable"] = True
        findings.append(rebuilt)
    return (not any(f["severity"] == "hard" for f in findings)), findings


# The closed core kind registry: the five closed kinds + the `custom/script` escape hatch.
# Module-provided kinds bind by presence at a later slice and must NOT be added here.
REGISTRY = {
    "presence": kind_presence,
    "schema": kind_schema,
    "shape": kind_shape,
    "coverage": kind_coverage,
    "coherence": kind_coherence,
    "custom/script": kind_custom_script,
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
    from jsonschema import Draft202012Validator        # lazy: see the module __getattr__ note (tool-runtime dep)
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


def get_pr_author() -> str | None:
    """The PR author's login from the trusted event context (.pull_request.user.login —
    GitHub-stamped from the real author, not fork-forgeable), or None when unavailable: a
    local run, a --pr-body-file invocation, or a malformed/partial event. NEVER github.actor
    (a re-run attributes that to the re-runner — the documented spoof vector). Read only; the
    sole consumer is run()'s ci_author_exempt honoring in the merge-gating suite. Degrades to
    None — and therefore to ENFORCING the rule — on any doubt, never to a falsely-exempt author."""
    event = os.environ.get("GITHUB_EVENT_PATH")
    if event and os.path.exists(event):
        try:
            pr = (load_json(event).get("pull_request") or {})
            return (pr.get("user") or {}).get("login")
        except (OSError, ValueError, AttributeError, TypeError):
            return None                    # unreadable / malformed / type-confused event → no author
    return None


def get_pr_labels() -> list:
    """The PR's label names from the trusted event context (.pull_request.labels[].name), or an
    EMPTY list when unavailable: a local run, a --pr-body-file invocation, or a malformed/partial
    event. Read only; the sole consumer is _evaluate()'s ci_label_exempt honoring in the merge-gating
    suite. Degrades to [] — and therefore to ENFORCING the rule — on any doubt (a non-list labels
    field, a label without a string name, an unreadable event), never to a falsely-exempt label.
    Mirrors get_pr_author()'s fail-safe posture: empty means 'no exemption', never 'skip the check'."""
    event = os.environ.get("GITHUB_EVENT_PATH")
    if event and os.path.exists(event):
        try:
            labels = (load_json(event).get("pull_request") or {}).get("labels")
            if not isinstance(labels, list):
                return []                  # absent / type-confused labels → no exemption (enforce)
            return [lbl["name"] for lbl in labels
                    if isinstance(lbl, dict) and isinstance(lbl.get("name"), str)]
        except (OSError, ValueError, AttributeError, TypeError):
            return []                      # unreadable / malformed / type-confused event → no labels
    return []


def _exemption_note(rule: dict, ctx: dict) -> "str | None":
    """The disclosed not-applicable note when a merge-gating rule does not bind for THIS pull
    request — waived by its author (ci_author_exempt) or by a label it carries (ci_label_exempt) —
    or None when the rule binds normally and its kind must run. Called by _evaluate ONLY in the
    blocking-gate suite (so the waiver lands exactly where a rule would otherwise block a merge);
    the by-id run_check() path never reaches here, so the §15 guardrail-weakening guard is never
    exempt. Exact-match only (no case-folding — silent widening is a spoof concern). Author is
    checked first; both forms emit a stated pass that names WHY the rule did not apply, never a
    silent green."""
    author = ctx.get("pr_author")
    if author in (rule.get("ci_author_exempt") or []):
        return (f"NOT APPLICABLE — check '{rule.get('id')}' does not bind for pull requests "
                f"authored by {author} in the merge gate, so it was not evaluated "
                f"here (a disclosed not-applicable pass — not a verification). This narrative "
                f"check is waived for this author only; any guardrail-touching change in the pull "
                f"request is still gated by the guardrail-ack label the maintainer applies.")
    matched = sorted(set(ctx.get("pr_labels") or []) & set(rule.get("ci_label_exempt") or []))
    if matched:
        return (f"NOT APPLICABLE — check '{rule.get('id')}' does not bind for pull requests "
                f"labelled '{matched[0]}' in the merge gate, so it was not evaluated here (a "
                f"disclosed not-applicable pass — not a verification). This narrative check is "
                f"waived for this single-purpose pull-request class only, which carries its own "
                f"deliberate plain-language body; any guardrail-touching change in the pull "
                f"request is still gated by the guardrail-ack label the maintainer applies.")
    return None


def _evaluate(rules: list, suite: str, gates: bool, ctx: dict, with_source: bool = False) -> list:
    """Dispatch every rule that joins `suite` through its kind and return the collected
    findings. The shared core behind both run() (which prints + computes an exit code) and
    collect() (which returns the data). `gates` is the suite's blocking-gate context — it
    decides only where ci_author_exempt waives, never what is collected.

    With `with_source`, each finding is annotated with the rule that emitted it — `source_rule`
    (the rule id) and `source_kind` (its kind) — so a programmatic consumer can tell, say, a
    soft length-budget nudge (kind `shape`) apart from another soft finding firing in the same
    suite. The finding.v1 base allows these extra keys (it fixes no closed property set), and the
    default (off) leaves run()'s and the existing feed's findings byte-for-byte unchanged."""
    findings = []
    for rule in [r for r in rules if suite in r.get("suites", [])]:
        kind, tier = rule.get("kind"), rule.get("tier", "hard")
        # Honor ci_author_exempt / ci_label_exempt at the engine layer — before any check-kind
        # runs, so the closed kinds stay author- and label-agnostic. They bind ONLY in the
        # merge-gating (blocking-gate) suite (`gates`, derived from the suite's context, not its
        # name): the exemption waives exactly where a rule would otherwise block a merge. A matched
        # author OR a matched label yields a DISCLOSED not-applicable pass (a soft note, never
        # gating), never a silent green and never a workflow skip that would leave the required
        # check pending. Exact-match only (no case-folding — silent widening is a spoof concern).
        # The by-id run_check() path carries no suite and so never reaches here: the §15
        # guardrail-weakening guard is never exempt.
        exempt_note = _exemption_note(rule, ctx) if gates else None
        if exempt_note is not None:
            # NOT a disclosed_noop: this is a check WAIVED in the merge-gating context (an exempt
            # author/label), a consequential disclosure that must stay prominent in the CI log — never
            # collapsed into the dormant "nothing to do" summary. It also never fires on a clean local
            # run, so it is outside the soft-note noise #322 targets.
            found = [finding("soft", exempt_note)]
        else:
            fn = REGISTRY.get(kind)
            if fn is None:  # dangling kind: fail closed (a finding at the rule's tier)
                found = [finding(tier, f"Check rule '{rule.get('id')}' names "
                         f"unregistered kind '{kind}'; cannot evaluate (fails closed).")]
            else:
                try:
                    _verdict, found = fn(rule, ctx)
                except Exception as exc:  # a kind that errors fails closed
                    found = [finding("hard", f"Check rule '{rule.get('id')}' (kind "
                             f"'{kind}') errored and could not evaluate: {exc}")]
        if with_source:
            for f in found:
                f["source_rule"] = rule.get("id")
                f["source_kind"] = kind
        findings.extend(found)
    return findings


def collect(suite: str, ctx: dict, *, with_source: bool = False) -> list:
    """The machine-readable seam behind run(): evaluate `suite` and RETURN its findings
    (each {severity, message, location}) as data, rather than printing a human report. A
    programmatic consumer — the audit soft-findings feed — reads the report-only findings
    here instead of scraping run()'s stdout. RAISES (ValueError / the loader's exception)
    on a config error (undeclared suite, unloadable suites/rules); the caller decides how
    to surface it (run() turns it into the loud exit-2 path, the feed into an honest marker).

    `with_source` annotates each finding with its emitting rule (`source_rule`/`source_kind`) —
    off by default, so the existing feed reads the bare base shape unchanged."""
    suites = load_suites()
    decl = suites.get(suite)
    if decl is None:
        raise ValueError(f"suite '{suite}' is not declared in .engine/suites.json "
                         f"(declared: {', '.join(sorted(suites))}).")
    gates = decl.get("context") == "blocking-gate"
    return _evaluate(load_rules(), suite, gates, ctx, with_source=with_source)


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
    # with_source so report() can name the checks whose disclosed-no-op notes it collapses;
    # the extra source_rule/source_kind keys are inert for the hard/actionable render paths.
    findings = _evaluate(rules, suite, gates, ctx, with_source=True)
    report(suite, findings, gates)
    # Gate on the authoritative signal — any hard-severity finding — but only where
    # the suite's context is a blocking-gate. A callable's verdict flag is advisory;
    # the rule's tier (carried as the finding severity) and the suite context decide
    # where teeth land, so the exit code and report() can never disagree.
    hard_fired = any(f["severity"] == "hard" for f in findings)
    return 1 if (gates and hard_fired) else 0


def run_check(check_id: str, ctx: dict) -> int:
    """Run ONE check rule, selected by its `id` field, directly — the "a check is a
    directly-callable unit, not only trigger-driven" path the validation README blesses.
    It loads ONLY the check rules (NOT suites.json), so a broken or loosened suite
    declaration can never strand or alter a directly-invoked guard — the isolation the
    §15 weakening guard relies on (D-051). It dispatches via the same kind registry as
    run(), and a dangling kind or an erroring callable FAILS CLOSED (a hard finding),
    exactly as in run(). A by-id run always gates (it is invoked deliberately, outside a
    suite's context): exit 1 on any hard finding, 0 if clean, 2 on an unknown id (a loud
    config error, like run()'s undeclared-suite path)."""
    try:
        rules = load_rules()
    except Exception as exc:  # a broken check rule file halts loudly (config error), in plain language
        print(f"\nCONFIG ERROR: cannot load the check rules: {exc}", file=sys.stderr)
        return 2
    matches = [r for r in rules if r.get("id") == check_id]
    if not matches:
        print(f"\nCONFIG ERROR: no check rule has id '{check_id}'.", file=sys.stderr)
        return 2
    findings = []
    for rule in matches:
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
    report(check_id, findings, True)  # a by-id run always gates
    hard_fired = any(f["severity"] == "hard" for f in findings)
    return 1 if hard_fired else 0


def run_unit(unit, target=None, ctx=None):
    """Run ONE check-logic unit against a caller-substituted target and return its
    (passed, findings) exactly as production would — the target-substitution affordance the
    negative-fixture meta-check (#286, D-256…D-260) needs to witness that each hard check
    actually BITES a seeded bad input. It is NOT a production entry point: run()/run_check()/
    --check never call it, so those paths and every existing finding are byte-unchanged.

    `unit` is the rule to run — its `kind` selects the REAL REGISTRY callable (a custom/script
    unit carries params.script). For the closed kinds it is a transient rule the caller crafts;
    for a custom/script it is the real committed rule. `target` (a dict) substitutes the input
    by unit class, overlaying only the keys that class reads and leaving the rest of `ctx`:
      - path:               presence/schema/shape — the fixture glob, set as target.path
                            (target_files resolves it under ROOT); the real callable is unchanged.
      - coverage_catalog,
        coverage_root:      coverage — the catalog source + walk root the real callable reads.
      - manifests:          coherence — the manifest set the real callable reads (ctx['manifests']).
      - env:                custom/script — env vars set around the child and restored after, so a
                            script reading a substituted GITHUB_EVENT_PATH (or another agreed
                            variable the fixture's script honours) sees the seeded input. (No edit
                            to kind_custom_script — it already builds its child env from os.environ.)
    NOTE on `env`: it mutates the process-global os.environ for the duration of the call (restored
    in a finally), so it is meaningful ONLY for the custom/script kind — which builds its child env
    from os.environ; the in-process closed kinds never read the environment. It is therefore not
    safe to call run_unit with `env` from multiple threads at once (no current/planned caller does).
    Dispatch mirrors run()/run_check(): an unregistered kind or an erroring callable FAILS CLOSED
    (a hard finding), so the meta-check witnesses exactly what the gate does."""
    target = target or {}
    ctx = dict(ctx or {})
    rule = dict(unit)
    if "path" in target:
        rule["target"] = {**(rule.get("target") or {}), "path": target["path"]}
    for key in ("coverage_catalog", "coverage_root", "manifests"):
        if key in target:
            ctx[key] = target[key]
    fn = REGISTRY.get(rule.get("kind"))
    if fn is None:  # dangling kind: fail closed, exactly as run()/run_check()
        return False, [finding(rule.get("tier", "hard"), f"run_unit: rule '{rule.get('id')}' names "
                       f"unregistered kind '{rule.get('kind')}'; cannot evaluate (fails closed).")]

    def _call():
        try:
            return fn(rule, ctx)
        except Exception as exc:  # an erroring callable fails closed, exactly as run()/run_check()
            return False, [finding("hard", f"Check rule '{rule.get('id')}' (kind "
                           f"'{rule.get('kind')}') errored and could not evaluate: {exc}")]

    env = target.get("env")
    if not env:
        return _call()
    saved = {k: os.environ.get(k) for k in env}      # set the substituted target in the child env...
    try:
        os.environ.update({k: str(v) for k, v in env.items()})
        return _call()
    finally:                                          # ...and restore os.environ no matter what
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def fmt(f: dict) -> str:
    where = ""
    if f.get("location"):
        l = f["location"]
        where = f"  [{l.get('file')}" + (f":{l['line']}" if l.get("line") else "") + "]"
    return f["message"] + where


def report(suite: str, findings: list, gates: bool) -> None:
    hard = [f for f in findings if f["severity"] == "hard"]
    soft = [f for f in findings if f["severity"] != "hard"]
    # Partition soft notes so an actionable one stands out from the dormant "nothing to do" ones.
    # Only a no-op we can NAME (it carries a source_rule) is collapsed into the summary line; an
    # actionable note — OR a marked no-op with no source rule to name (the by-id `--check` path does
    # not set one, and a single deliberately-invoked check is never noise) — renders in full. A
    # finding WITHOUT the marker defaults to actionable (`.get`, never `[]`), so the fail-safe is a
    # note shown in full (harmless), never an actionable note hidden. The collapsed no-ops stay
    # DISCLOSED (named + counted, never a silent skip); only their boilerplate prose folds away.
    collapsible, shown = [], []
    for f in soft:
        (collapsible if f.get("not_applicable") and f.get("source_rule") else shown).append(f)
    displayed = len(shown) + (1 if collapsible else 0)
    if displayed:
        print(f"\nnotes ({displayed}):")
        for f in shown:
            print("  - " + fmt(f))
        if collapsible:
            names = list(dict.fromkeys(str(f["source_rule"]) for f in collapsible))
            print(f"  - {len(names)} check(s) not applicable here (nothing to do): " + ", ".join(names))
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
    suite, body_file, check_id, i = "CI", None, None, 0
    while i < len(argv):
        if argv[i] == "--suite" and i + 1 < len(argv):
            suite, i = argv[i + 1], i + 2
        elif argv[i] == "--pr-body-file" and i + 1 < len(argv):
            body_file, i = argv[i + 1], i + 2
        elif argv[i] == "--check" and i + 1 < len(argv):
            check_id, i = argv[i + 1], i + 2
        else:
            print(f"unknown argument: {argv[i]}", file=sys.stderr)
            return 2
    ctx = {"pr_body": get_pr_body(body_file),     # the same ctx both entry points build
           "pr_author": get_pr_author(),           # honored by run() for ci_author_exempt (CI gate only)
           "pr_labels": get_pr_labels()}           # honored by run() for ci_label_exempt (CI gate only)
    if check_id is not None:
        return run_check(check_id, ctx)
    return run(suite, ctx)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
