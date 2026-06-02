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
  - schema    — a structured file conforms to its governing JSON Schema (2020-12),
                resolved catalog-first (`governing_schema`), with a `params.schema`
                override for the cases the catalog cannot express.
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
import glob as _glob
import json
import os
import re
import subprocess
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
    tier = rule["tier"]
    return False, [finding(tier, f"Check rule '{rule.get('id')}' (kind 'coverage') names an "
                   f"unrecognized mode '{mode}'; cannot evaluate (fails closed).")]


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
    params.infra_dirs."""
    tier = rule["tier"]
    try:
        surfaces = load_json(CATALOG_PATH).get("surfaces", {})
    except Exception as exc:
        return False, [finding(tier, f"Could not read the surface catalog to check coverage: "
                       f"{exc}. {rule['message']}", loc(CATALOG_PATH))]
    infra = set((rule.get("params") or {}).get("infra_dirs", []))
    present = set()
    for root in (".engine", ".claude"):
        abs_root = os.path.join(ROOT, root)
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
        findings.append(finding(f.get("severity", tier), f.get("message", ""), f.get("location")))
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
    ctx = {"pr_body": get_pr_body(body_file)}  # the same ctx both entry points build
    if check_id is not None:
        return run_check(check_id, ctx)
    return run(suite, ctx)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
