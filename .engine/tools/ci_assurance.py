#!/usr/bin/env python3
"""Generate the committed, human-readable account of what engine-ci verifies.

The catalogue is evidence about the declared and exercised CI surface, not a quality score.  Its inputs are
the workflow, CI check declarations, the checker-of-checkers proof roster, module manifests, and the test
modules selected by the workflow.  Test files are parsed with ``ast`` and never imported or executed.

  uv run --directory .engine --frozen -- python tools/ci_assurance.py show
  uv run --directory .engine --frozen -- python tools/ci_assurance.py generate
  uv run --directory .engine --frozen -- python tools/ci_assurance.py check
"""
from __future__ import annotations

import ast
import fnmatch
import glob
import os
import re
import shlex
import sys
from collections import defaultdict

import yaml

import engine_write
import hard_check_bite_check
import module_coherence
import validate


CATALOGUE_PATH = os.path.join(validate.ENGINE_DIR, "docs", "ci-assurance.md")
WORKFLOW_REL = ".github/workflows/engine-ci.yml"


class _WorkflowLoader(yaml.SafeLoader):
    """Safe YAML with YAML 1.2-like booleans, so GitHub's ``on`` remains a string key."""


_WorkflowLoader.yaml_implicit_resolvers = {
    first: list(resolvers) for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for first, resolvers in list(_WorkflowLoader.yaml_implicit_resolvers.items()):
    _WorkflowLoader.yaml_implicit_resolvers[first] = [
        item for item in resolvers if item[0] != "tag:yaml.org,2002:bool"
    ]
_WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.IGNORECASE), list("tTfF")
)


def _read(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _rel(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _link(relpath: str, label: str | None = None) -> str:
    # The document lives at .engine/docs/; all governed sources are linked from there.
    return f"[{label or relpath}](../../{relpath})"


def _cell(value) -> str:
    """Collapse authored prose into one deterministic, Markdown-table-safe cell."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    # Authored source prose may itself contain Markdown links or home-repo shorthand. In a generated table,
    # those constructs would be reinterpreted as links from THIS document or as downstream-local issue ids.
    text = re.sub(r"(?:StarshipSuperjam/)?engine-template\s+#(\d+)",
                  r"StarshipSuperjam/engine-template#\1", text)
    text = re.sub(r"/#(\d+)", r" and StarshipSuperjam/engine-template#\1", text)
    text = re.sub(r"(?<![\w/#])#(\d+)", r"StarshipSuperjam/engine-template#\1", text)
    text = text.replace("](", "] (")
    return (text.replace("\\", "\\\\").replace("|", "\\|")
            .replace("[", "\\[").replace("]", "\\]"))


def load_workflow(root: str = validate.ROOT) -> dict:
    path = os.path.join(root, WORKFLOW_REL)
    try:
        data = yaml.load(_read(path), Loader=_WorkflowLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"{WORKFLOW_REL} is not valid safe YAML: {exc}") from exc
    if not isinstance(data, dict) or "on" not in data or not isinstance(data.get("jobs"), dict):
        raise ValueError(f"{WORKFLOW_REL} must declare 'on' and 'jobs'")
    job = data["jobs"].get("engine-ci")
    if not isinstance(job, dict) or not isinstance(job.get("steps"), list):
        raise ValueError(f"{WORKFLOW_REL} must contain jobs.engine-ci.steps")
    return data


def workflow_facts(data: dict) -> tuple[list[dict], list[dict]]:
    """Return normalized triggers and executable steps, rejecting shapes we cannot describe fully."""
    unknown_top = set(data) - {"name", "on", "permissions", "jobs"}
    if unknown_top:
        raise ValueError(f"unsupported engine-ci workflow keys: {sorted(unknown_top)}")
    job = data["jobs"]["engine-ci"]
    unknown_job = set(job) - {"name", "runs-on", "steps"}
    if unknown_job:
        raise ValueError(f"unsupported jobs.engine-ci keys: {sorted(unknown_job)}")
    if not isinstance(data.get("permissions"), dict) or not data.get("permissions"):
        raise ValueError("engine-ci workflow permissions must be a non-empty mapping")
    if not isinstance(job.get("runs-on"), str) or not job.get("runs-on"):
        raise ValueError("jobs.engine-ci.runs-on must be a non-empty string")
    on = data["on"]
    if isinstance(on, str):
        on = {on: None}
    elif isinstance(on, list):
        on = {name: None for name in on}
    if not isinstance(on, dict):
        raise ValueError("engine-ci workflow 'on' must be a string, list, or mapping")
    triggers = []
    for event, config in on.items():
        if config is None:
            detail = "all supported event activity"
        elif isinstance(config, dict):
            unknown = set(config) - {"branches", "branches-ignore", "paths", "paths-ignore", "types"}
            if unknown:
                raise ValueError(f"unsupported {event} trigger keys: {sorted(unknown)}")
            detail = "; ".join(f"{key}: {', '.join(map(str, value if isinstance(value, list) else [value]))}"
                               for key, value in config.items()) or "all supported event activity"
        else:
            raise ValueError(f"unsupported configuration for workflow event {event!r}")
        triggers.append({"event": str(event), "detail": detail})

    steps = []
    for number, step in enumerate(data["jobs"]["engine-ci"]["steps"], 1):
        if not isinstance(step, dict):
            raise ValueError(f"engine-ci step {number} is not a mapping")
        action_keys = [key for key in ("uses", "run") if key in step]
        if len(action_keys) != 1:
            raise ValueError(f"engine-ci step {number} must declare exactly one of uses/run")
        unknown = set(step) - {"name", "uses", "run", "with", "env", "if", "continue-on-error", "timeout-minutes", "working-directory", "shell"}
        if unknown:
            raise ValueError(f"unsupported engine-ci step {number} keys: {sorted(unknown)}")
        steps.append({
            "number": number,
            "name": step.get("name") or f"Step {number}",
            "kind": action_keys[0],
            "command": step[action_keys[0]],
            "if": step.get("if", "always when prior steps succeed"),
            "continue": step.get("continue-on-error", False),
            "timeout": step.get("timeout-minutes"),
            "details": _execution_details(step),
        })
    return triggers, steps


def _execution_details(step: dict) -> str:
    """Render every supported step field not carried by the primary action/condition columns."""
    parts = []
    for key in ("with", "env"):
        value = step.get(key)
        if value is not None:
            if not isinstance(value, dict):
                raise ValueError(f"engine-ci step {key} must be a mapping")
            parts.append(f"{key}: " + ", ".join(f"{name}={value[name]}" for name in sorted(value)))
    for key in ("working-directory", "shell"):
        if key in step:
            parts.append(f"{key}: {step[key]}")
    return "; ".join(parts) or "none"


def discover_test_modules(root: str, steps: list[dict]) -> list[dict]:
    """Mirror the workflow's unittest discovery without importing any discovered Python module."""
    discoveries = []
    for step in steps:
        if step["kind"] != "run":
            continue
        for line in str(step["command"]).splitlines():
            if "unittest discover" not in line:
                continue
            tokens = shlex.split(line)
            try:
                marker = tokens.index("discover")
            except ValueError as exc:
                raise ValueError("unittest discovery command could not be parsed") from exc
            args = tokens[marker + 1:]
            start, pattern = ".", "test*.py"
            for flag, dest in (("-s", "start"), ("--start-directory", "start"),
                               ("-p", "pattern"), ("--pattern", "pattern")):
                if flag in args:
                    idx = args.index(flag)
                    if idx + 1 >= len(args):
                        raise ValueError(f"unittest discovery {flag} has no value")
                    if dest == "start":
                        start = args[idx + 1]
                    else:
                        pattern = args[idx + 1]
            discoveries.append((start, pattern))
    if len(discoveries) != 1:
        raise ValueError(f"expected exactly one unittest discovery command, found {len(discoveries)}")

    start, pattern = discoveries[0]
    base = os.path.join(root, ".engine", start)
    if not os.path.isdir(base):
        raise ValueError(f"unittest start directory does not exist: .engine/{start}")
    found = []
    for directory, subdirs, files in os.walk(base):
        if directory != base and not os.path.isfile(os.path.join(directory, "__init__.py")):
            subdirs[:] = []  # unittest only recurses into importable packages
            continue
        subdirs[:] = sorted(subdirs)
        for name in sorted(files):
            if not fnmatch.fnmatch(name, pattern):
                continue
            path = os.path.join(directory, name)
            try:
                tree = ast.parse(_read(path), filename=path)
            except SyntaxError as exc:
                raise ValueError(f"cannot parse discovered test module {_rel(path, root)}: {exc}") from exc
            doc = ast.get_docstring(tree, clean=True)
            if not doc:
                raise ValueError(f"discovered test module {_rel(path, root)} has no module docstring")
            found.append({"path": _rel(path, root), "description": _cell(doc)})
    if not found:
        raise ValueError("unittest discovery selected no test modules")
    return found


def _owners(root: str) -> tuple[dict[str, dict], dict[str, str]]:
    manifests = module_coherence.discover_manifests(root)
    by_id = {manifest.get("id"): manifest for _path, manifest in manifests}
    if root == validate.ROOT:
        claims = module_coherence.provides_claims(manifests)
    else:
        claims = defaultdict(list)
        for _path, manifest in manifests:
            for patterns in (manifest.get("provides") or {}).values():
                for pattern in patterns:
                    for path in glob.glob(os.path.join(root, pattern), recursive=True):
                        if os.path.isfile(path):
                            claims[_rel(path, root)].append(manifest.get("id"))
    owners = {}
    for path, mids in claims.items():
        unique = sorted(set(mids))
        if len(unique) != 1:
            raise ValueError(f"{path} must have exactly one module owner; found {unique}")
        owners[path] = unique[0]
    return by_id, owners


def _proof_rows(root: str) -> tuple[dict[str, dict], dict[str, dict]]:
    inventory = hard_check_bite_check.proof_inventory(
        root=root,
        check_dir=os.path.join(root, ".engine", "check"),
        fixture_root=os.path.join(root, ".engine", "_fixtures"),
    )
    kinds = {row["kind"]: row for row in inventory if row["scope"] == "kind"}
    checks = {row["key"]: row for row in inventory if row["scope"] == "check"}
    return kinds, checks


def classify_rule(rule: dict, kind_proofs: dict, check_proofs: dict) -> tuple[str, str | None]:
    """Describe the proof carrier without claiming that a declaration or fixture has passed this run."""
    if rule.get("tier") != "hard":
        return "Soft check — outside the hard-check proof roster", None
    if rule.get("kind") == "custom/script":
        proof = check_proofs.get(rule.get("id"))
        label = "Dedicated hard-check bite proof"
    else:
        proof = kind_proofs.get(rule.get("kind"))
        label = "Shared check-kind bite proof"
    if not proof:
        raise ValueError(f"hard rule {rule.get('id')} has no proof-roster entry")
    carrier = proof["carrier"]
    if carrier == "declared-not-applicable":
        label += " — disclosed exception"
    elif carrier == "missing":
        label += " — missing carrier (CI will refuse)"
    else:
        label += " — negative fixture"
    return label, proof.get("fixture_dir")


def canonical_catalogue(root: str = validate.ROOT) -> str:
    workflow = load_workflow(root)
    triggers, steps = workflow_facts(workflow)
    tests = discover_test_modules(root, steps)
    modules, owners = _owners(root)
    rules = []
    for path in sorted(glob.glob(os.path.join(root, ".engine", "check", "*.json"))):
        rule = validate.load_json(path)
        if "CI" in (rule.get("suites") or []):
            rules.append((rule, _rel(path, root)))
    kind_proofs, check_proofs = _proof_rows(root)

    grouped_rules = defaultdict(list)
    for rule, rel in rules:
        owner = owners.get(rel)
        if owner not in modules:
            raise ValueError(f"CI rule {rule.get('id')} has no unique installed-module owner ({rel})")
        proof, fixture = classify_rule(rule, kind_proofs, check_proofs)
        grouped_rules[owner].append((rule, rel, proof, fixture))
    grouped_tests = defaultdict(list)
    for test in tests:
        owner = owners.get(test["path"])
        if owner not in modules:
            raise ValueError(f"test module {test['path']} has no unique installed-module owner")
        grouped_tests[owner].append(test)

    hard = sum(rule.get("tier") == "hard" for rule, _relpath in rules)
    soft = len(rules) - hard
    dedicated = sum(rule.get("tier") == "hard" and rule.get("kind") == "custom/script"
                    for rule, _relpath in rules)
    exception_count = sum(row["carrier"] == "declared-not-applicable" for row in [*kind_proofs.values(), *check_proofs.values()])
    out = [
        "---", "title: What Engine CI verifies", "---", "",
        "# What Engine CI verifies", "",
        "## What this covers", "",
        "The `engine-ci` badge reports the latest `main` push run of the workflow described here. On a "
        "**main push**, green means the checked revision completed every non-optional workflow step: it materialized "
        "the pinned Engine runtime, ran the declared CI validator suite, and ran the discovered self-test modules. "
        "Checks that require pull-request context can disclose that their live witness is unavailable on a push; "
        "green does not turn that absence into pull-request evidence.", "",
        "On a **pull request**, the same workflow runs against the proposed revision and supplies pull-request event "
        "context. Green means its hard findings were clear and its self-tests passed for that run. Branch protection, "
        "other workflows, and the operator's merge decision are separate controls; this catalogue documents only "
        f"{_link(WORKFLOW_REL, 'engine-ci')}.", "",
        "### The assurance claim", "",
        "This page shows the **declared and exercised CI surface**: workflow triggers and steps, validator rules, "
        "their enforcement tiers and proof carriers, installed-module ownership, and statically discovered self-test "
        "modules. A negative fixture is evidence that a hard check catches its deliberately broken example. A shared "
        "check-kind fixture supports rules implemented by the same validator kind. A disclosed exception records why "
        "that proof shape is not applicable. A test-module docstring is shown as its authors' **declared test intent**.", "",
        "It does **not** establish exhaustive correctness, every possible failure mode, Python line or branch coverage, "
        "or the quality of every assertion. It publishes no coverage percentage or quality score. Standard review of "
        "this change checks whether a reader can trace a safeguard from its purpose to where it runs, the bad example "
        "it catches, and the residual limit of that evidence.", "",
        "## What you need to know", "",
        "### Generated totals", "",
        "| Surface | Total |", "| --- | ---: |",
        f"| Workflow triggers | {len(triggers)} |",
        f"| Executable workflow steps | {len(steps)} |",
        f"| CI validator rules | {len(rules)} ({hard} hard, {soft} soft) |",
        f"| Dedicated hard custom-check proofs | {dedicated} |",
        f"| Disclosed proof exceptions | {exception_count} |",
        f"| Discovered self-test modules | {len(tests)} |", "",
        "### When it runs", "",
        f"Workflow: `{_cell(workflow.get('name'))}` · job: `{_cell(workflow['jobs']['engine-ci'].get('name'))}` "
        f"· runner: `{_cell(workflow['jobs']['engine-ci']['runs-on'])}`", "",
        "Permissions: " + ", ".join(
            f"`{_cell(name)}: {_cell(workflow['permissions'][name])}`" for name in sorted(workflow["permissions"])
        ) + ".", "",
        "| Event | Scope |", "| --- | --- |",
    ]
    out.extend(f"| `{_cell(row['event'])}` | {_cell(row['detail'])} |" for row in triggers)
    out.extend(["", "### What the workflow executes", "",
                "A step is gating unless `continue-on-error` is true. The condition column records when GitHub evaluates it.", "",
                "| # | Step | Action or command | Execution details | Condition | Failure semantics |",
                "| ---: | --- | --- | --- | --- | --- |"])
    for step in steps:
        failure = "non-gating" if step["continue"] else "gating"
        if step["timeout"] is not None:
            failure += f"; timeout {step['timeout']} minutes"
        out.append(f"| {step['number']} | {_cell(step['name'])} | `{_cell(step['command'])}` | "
                   f"{_cell(step['details'])} | {_cell(step['if'])} | {failure} |")

    out.extend(["", "### Verification by Engine module", ""])
    for module_id in sorted(set(grouped_rules) | set(grouped_tests)):
        out.extend([f"#### `{module_id}`", ""])
        rows = sorted(grouped_rules[module_id], key=lambda row: row[0]["id"])
        if rows:
            out.extend(["##### Validator rules", "",
                        "| Rule | Tier | Kind | Purpose | Proof classification |",
                        "| --- | --- | --- | --- | --- |"])
            for rule, rel, proof, fixture in rows:
                proof_text = proof
                if fixture:
                    proof_text += f" ({_link(fixture, 'carrier')})"
                rule_label = f"`{rule['id']}`"
                out.append(f"| {_link(rel, rule_label)} | `{_cell(rule.get('tier'))}` | "
                           f"`{_cell(rule.get('kind'))}` | {_cell(rule.get('message'))} | {proof_text} |")
            out.append("")
        module_tests = sorted(grouped_tests[module_id], key=lambda row: row["path"])
        if module_tests:
            out.extend(["##### Self-test modules", "",
                        "These summaries are parsed from module docstrings and report declared test intent; the workflow "
                        "executes the modules, while this prose is not itself proof that each assertion is sufficient.", "",
                        "| Module | Declared test intent |", "| --- | --- |"])
            for test in module_tests:
                test_label = f"`{test['path']}`"
                out.append(f"| {_link(test['path'], test_label)} | {test['description']} |")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def _read_committed(path: str):
    return _read(path) if os.path.isfile(path) else None


def generate(path: str | None = None) -> dict:
    supplied = path is not None
    path = path or CATALOGUE_PATH
    canonical = canonical_catalogue()
    changed = _read_committed(path) != canonical
    reason = engine_write.write_through_symlink_reason(
        path, os.path.dirname(os.path.abspath(path)) if supplied else validate.ROOT)
    if reason:
        raise engine_write.EngineWriteRefused(reason)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(canonical)
    message = ("Wrote" if changed else "The") + f" CI assurance catalogue ({_rel(path, validate.ROOT)})"
    if not changed:
        message += " was already up to date"
    return validate.finding("note", message + ".", validate.loc(path))


def check(path: str | None = None) -> dict:
    path = path or CATALOGUE_PATH
    try:
        canonical = canonical_catalogue()
        committed = _read_committed(path)
    except Exception as exc:  # fail loud as a hard finding, never emit a clean-looking partial report
        return validate.finding("hard", f"The CI assurance catalogue could not be derived: {exc}", validate.loc(path))
    if committed == canonical:
        return validate.finding("note", "The CI assurance catalogue is up to date.", validate.loc(path))
    state = "is missing" if committed is None else "is out of date"
    return validate.finding(
        "hard", f"The CI assurance catalogue {state}. Regenerate it with `uv run --directory .engine "
        "--frozen -- python tools/ci_assurance.py generate` and commit the result.", validate.loc(path))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else "show"
    path = argv[1] if len(argv) > 1 else None
    if command == "show":
        sys.stdout.write(canonical_catalogue())
        return 0
    if command == "generate":
        print(generate(path)["message"])
        return 0
    if command == "check":
        finding = check(path)
        print(finding["message"])
        return 1 if finding["severity"] == "hard" else 0
    print("usage: ci_assurance.py {show|generate|check} [path]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
