"""Test-cost declarations and evidence at the existing serial selftest boundary.

Static identities are source-bound, not line-bound. Runtime IDs retain occurrence;
explicit mappings cover generated cases without pretending AST count equals discovery.
This module deliberately does not begin with test_: it is a production owner.
"""
from __future__ import annotations

import ast
from collections import Counter
import hashlib
import inspect
import json
from pathlib import Path

RESOURCES = ('processes', 'git_commands', 'schema_decodes', 'metaschema_validations',
             'whole_tree_fixtures', 'nested_journeys')
ROOT = Path(__file__).resolve().parents[2]


def digest(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                                ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def zeros():
    return dict.fromkeys(RESOURCES, 0)


def declaration(contract):
    """Attach one immutable-by-convention declaration to a case method or owning class.

    Validation is once at inventory admission, never on every call of a test helper.
    Each consumer gets a copy so one test cannot mutate a sibling's declared contract.
    """
    saved = json.dumps(contract, sort_keys=True, allow_nan=False)
    def attach(target):
        target.__test_cost_json__ = saved
        return target
    return attach


def declared_contract(case):
    method = getattr(case, getattr(case, '_testMethodName', ''), None)
    saved = getattr(method, '__test_cost_json__', None) or getattr(type(case), '__test_cost_json__', None)
    return json.loads(saved) if saved else None


def static_census(sources, source_commit):
    """Inspect module/class test definitions before import; local fixture classes are not discovery.

    sources maps repository-relative paths to source strings. Duplicate method definitions
    retain their individual normalized ASTs, including the overwritten occurrence.
    """
    definitions, duplicates = [], []
    def visit(body, prefix, path):
        for cls in (n for n in body if isinstance(n, ast.ClassDef)):
            qualified = '.'.join((*prefix, cls.name))
            groups = {}
            for node in cls.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test'):
                    groups.setdefault(node.name, []).append(node)
            for name, nodes in groups.items():
                normal = [ast.dump(node, include_attributes=False) for node in nodes]
                key = {'path': path, 'qualified_name': qualified + '.' + name}
                definitions.append({**key, 'ast_digest': digest(normal[-1]), 'line': nodes[-1].lineno})
                if len(nodes) > 1:
                    duplicates.append({**key, 'source_commit': source_commit, 'occurrences': len(nodes),
                                       'ast_digest': digest(normal)})
            visit(cls.body, (*prefix, cls.name), path)
    for path, source in sorted(sources.items()):
        visit(ast.parse(source, filename=path).body, (), path)
    return {'source_commit': source_commit, 'definitions': definitions, 'duplicates': duplicates}


def duplicate_findings(census, enrollment):
    """An unchanged historical duplicate is allowed only by its exact enrollment identity.

    The enrollment's source commit records where the debt was admitted. Candidate HEAD
    naturally differs; source/AST equality rather than current commit equality proves no growth.
    """
    enrolled = enrollment.get('duplicates', [])
    findings = []
    for item in census['duplicates']:
        matches = [e for e in enrolled if all(e.get(k) == item[k] for k in
                   ('path', 'qualified_name', 'occurrences', 'ast_digest'))]
        if len(matches) != 1 or any(not matches[0].get(k) for k in
                                   ('source_commit', 'owner', 'reason', 'revisit')):
            findings.append('unenrolled duplicate: ' + item['path'] + ':' + item['qualified_name'])
    return findings


def runtime_inventory(cases, root=ROOT):
    """Map real cases to defining functions, including inherited methods; unknown stays explicit."""
    records, occurrences = [], Counter()
    for case in cases:
        name = case.id()
        occurrences[name] += 1
        method = getattr(case, getattr(case, '_testMethodName', ''), None)
        try:
            path = Path(inspect.getsourcefile(method)).resolve().relative_to(Path(root).resolve()).as_posix()
            qualified = method.__qualname__
        except (TypeError, ValueError, AttributeError):
            path = qualified = None
        records.append({'id': name, 'occurrence': occurrences[name], 'path': path,
                        'qualified_name': qualified, 'contract': declared_contract(case)})
    return records


def case_key(case):
    return (case['id'], case['occurrence'])


def inventory_findings(runtime, census, legacy, mappings=()):
    """Classify every runtime occurrence without silently broadening the old source census.

    A generated source mapping needs an exact runtime identity and fault-preservation rationale.
    Removed/renamed historical cases need an explicit mapping too. Orphan mappings refuse.
    """
    from selftest_results import validate_shape
    from build_coordinator_dag import CoordinatorError, validate_test_cost_contracts
    definitions = {(d['path'], d['qualified_name']): d for d in census['definitions']}
    old = {case_key(c): c for c in legacy.get('cases', [])}
    mapped = {case_key(m['target']): m for m in mappings if m.get('target')}
    if len(mapped) != sum(bool(m.get('target')) for m in mappings):
        return ['duplicate runtime mapping']
    findings, seen = [], set()
    for case in runtime:
        key = case_key(case)
        if key in seen:
            findings.append('duplicate runtime occurrence: ' + str(key))
        seen.add(key)
        definition = definitions.get((case.get('path'), case.get('qualified_name')))
        mapping = mapped.get(key)
        if mapping:
            if not mapping.get('reason') or not mapping.get('supported_fault'):
                findings.append('mapping lacks fault-preservation rationale: ' + str(key))
            definition = definitions.get((mapping.get('path'), mapping.get('qualified_name')))
        if not definition:
            findings.append('unmapped runtime case: ' + str(key))
        prior = old.get(key)
        unchanged = prior and definition and prior.get('source_digest') == definition['ast_digest']
        contract = case.get('contract')
        if contract:
            try:
                validate_shape(contract, 'test-cost-contract.v1')
                validate_test_cost_contracts({'work_items': [{'id': case['id'], 'test_cost': contract}]})
            except (ValueError, CoordinatorError) as exc:
                findings.append('invalid declaration: ' + str(key) + ': ' + str(exc))
        elif not unchanged:
            findings.append('new or changed case needs a declaration: ' + str(key))
    for target in mapped:
        if target not in seen:
            findings.append('orphan runtime mapping: ' + str(target))
    removed = set(old) - seen
    for key in removed:
        allowed = [m for m in mappings if m.get('source') and case_key(m['source']) == key
                   and m.get('reason') and m.get('supported_fault')
                   and (m.get('target') is None or case_key(m['target']) in seen)]
        if len(allowed) != 1:
            findings.append('removed case lacks fault-preservation mapping: ' + str(key))
    return findings


def exception_applies(exception, case, resource, source_commit, *, now, max_seconds=2592000):
    """Re-evaluate permission at the consuming boundary; never cache a time-based verdict."""
    import moment
    from selftest_results import validate_shape
    try:
        validate_shape(exception, 'test-cost-exception.v1')
    except ValueError:
        return False
    start, end, at = (moment.parse_z(value) for value in
                      (exception['issued_at'], exception['expires_at'], now))
    return bool(start and end and at and start <= at < end
                and 0 < (end-start).total_seconds() <= max_seconds
                and case_key(exception['case']) == case_key(case)
                and exception['resource'] == resource and exception['source_commit'] == source_commit)


def budget_findings(counts, limits):
    """Deterministic ceilings, including shared-helper amplification with unchanged test AST."""
    return [f'{resource}: {counts[resource]} exceeds {limits[resource]}' for resource in RESOURCES
            if counts[resource] > limits[resource]]
