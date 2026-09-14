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

_RUNNER_CONTROL_FILES = ('GITHUB_ENV', 'GITHUB_OUTPUT', 'GITHUB_PATH', 'GITHUB_STATE', 'GITHUB_STEP_SUMMARY')
_COST_EXCEPTION_ENV = 'ENGINE_TEST_COST_APPROVED_EXCEPTIONS'


def inventory_environment(directory, *, inherited=None):
    """Give untrusted inventory discovery private runner controls and no cost permission."""
    import os
    environment = dict(os.environ if inherited is None else inherited)
    controls = Path(directory)
    controls.mkdir(parents=True, exist_ok=True)
    for name in _RUNNER_CONTROL_FILES:
        path = controls / name.lower()
        path.touch(exist_ok=True)
        environment[name] = str(path)
    environment.pop(_COST_EXCEPTION_ENV, None)
    environment.pop('GITHUB_TOKEN', None)
    environment.pop('GH_TOKEN', None)
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    return environment


def digest(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                                ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def zeros():
    return dict.fromkeys(RESOURCES, 0)


def pack_enrollment(document):
    """Compact immutable JSON evidence so whole-tree fixtures do not copy megabytes of repetition."""
    import base64
    import zlib
    data = json.dumps(document, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(data) > 16 * 1024 * 1024:
        raise ValueError('enrollment exceeds expanded byte bound')
    return {'schema_version': 'test-cost-bundle.v1', 'encoding': 'zlib-base64',
            'document_digest': digest(document), 'expanded_bytes': len(data),
            'data': base64.b64encode(zlib.compress(data)).decode('ascii')}


def unpack_enrollment(bundle):
    import base64
    import zlib
    if (not isinstance(bundle, dict) or bundle.get('schema_version') != 'test-cost-bundle.v1'
            or bundle.get('encoding') != 'zlib-base64'
            or type(bundle.get('expanded_bytes')) is not int
            or not 0 <= bundle['expanded_bytes'] <= 16 * 1024 * 1024
            or not isinstance(bundle.get('data'), str) or len(bundle['data']) > 16 * 1024 * 1024):
        raise ValueError('invalid bounded enrollment bundle')
    try:
        decoder = zlib.decompressobj()
        data = decoder.decompress(base64.b64decode(bundle['data'], validate=True), bundle['expanded_bytes'] + 1)
        if not decoder.eof or decoder.unused_data or len(data) != bundle['expanded_bytes']:
            raise ValueError('enrollment expansion or trailing-data mismatch')
        document = json.loads(data)
    except (ValueError, zlib.error) as exc:
        raise ValueError('invalid enrollment encoding') from exc
    if digest(document) != bundle.get('document_digest'):
        raise ValueError('enrollment document digest mismatch')
    return document


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


def scaling_input(family, size):
    """Bind a generated or explicit case to a declared, bounded input-size experiment."""
    if not isinstance(family, str) or not family or type(size) is not int or size < 1:
        raise ValueError('scaling input needs a family and positive integer size')
    def attach(method):
        method.__test_cost_input__ = (family, size)
        return method
    return attach


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
    shadowed = {}
    for definition in definitions:
        shadowed.setdefault((definition['path'], definition['qualified_name']), []).append(definition)
    for (path, name), group in shadowed.items():
        if len(group) > 1:
            duplicates.append({'path': path, 'qualified_name': name, 'source_commit': source_commit,
                               'occurrences': len(group), 'ast_digest': digest([d['ast_digest'] for d in group])})
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
    from selftest_results import text
    records, occurrences = [], Counter()
    for case in cases:
        name = text(case.id())
        occurrences[name] += 1
        method = getattr(case, getattr(case, '_testMethodName', ''), None)
        try:
            # unittest.skip wraps a method in stdlib code while retaining its defining function.
            # Resolve that provenance before comparing the runtime case with the source census.
            definition = inspect.unwrap(method)
            path = Path(inspect.getsourcefile(definition)).resolve().relative_to(Path(root).resolve()).as_posix()
            qualified = definition.__qualname__
        except (TypeError, ValueError, AttributeError):
            path = qualified = None
        records.append({'id': name, 'occurrence': occurrences[name], 'path': path,
                        'qualified_name': qualified, 'contract': declared_contract(case),
                        'family': getattr(method, '__test_cost_input__', (None, None))[0],
                        'input_size': getattr(method, '__test_cost_input__', (None, None))[1]})
    return records


def case_key(case):
    return (case['id'], case['occurrence'])


def effective_contract(case, definition, explicit=None):
    """Resolve embedded or separately reviewed declarations against the actual source."""
    contract = case.get('contract')
    if explicit:
        if (not definition or any(explicit.get(k) != definition[k] for k in ('path', 'qualified_name'))
                or explicit.get('source_digest') != definition['ast_digest']):
            raise ValueError('stale prospective declaration: ' + str(case_key(case)))
        if contract and contract != explicit.get('contract'):
            raise ValueError('conflicting prospective declarations: ' + str(case_key(case)))
        contract = explicit.get('contract')
    return contract


def inventory_findings(runtime, census, legacy, mappings=(), declarations=()):
    """Classify every runtime occurrence without silently broadening the old source census.

    A generated source mapping needs an exact runtime identity and fault-preservation rationale.
    Removed/renamed historical cases need an explicit mapping too. Orphan mappings refuse.
    """
    from selftest_results import validate_shape
    from build_coordinator_dag import CoordinatorError, validate_test_cost_contracts
    definitions = {(d['path'], d['qualified_name']): d for d in census['definitions']}
    old = {case_key(c.get('case', c)): c for c in legacy.get('cases', [])}
    declared = {case_key(c): c for c in declarations}
    if len(declared) != len(declarations):
        return ['duplicate prospective declaration identity']
    mapped = {case_key(m['target']): m for m in mappings if m.get('target')}
    if len(mapped) != sum(bool(m.get('target')) for m in mappings):
        return ['duplicate runtime mapping']
    findings, seen, used_definitions = [], set(), set()
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
        else:
            used_definitions.add((definition['path'], definition['qualified_name']))
        prior = old.get(key)
        unchanged = (prior and definition and prior.get('source_digest') == definition['ast_digest']
                     and prior.get('path') == definition['path']
                     and prior.get('qualified_name') == definition['qualified_name'])
        try:
            contract = effective_contract(case, definition, declared.get(key))
        except ValueError as exc:
            findings.append(str(exc))
            contract = None
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
    for key in declared.keys() - seen:
        findings.append('orphan prospective declaration: ' + str(key))
    for key in definitions.keys() - used_definitions:
        findings.append('orphan test definition: ' + ':'.join(key))
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
    # Reading an older record is compatible; authorizing it still requires proof.
    if any(not exception.get(key, '').strip() for key in
           ('id', 'owner', 'reason', 'revisit', 'supported_fault', 'fault_preservation_evidence')):
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


def observation_problems(observation, expected_identity):
    """Validate both the wire shape and exclusive accounting before consuming evidence."""
    from selftest_results import validate_shape
    try:
        validate_shape(observation, 'test-cost-observation.v1')
    except (ValueError, TypeError):
        return ['resource observation is missing or malformed']
    problems = []
    if observation['identity'] != expected_identity:
        problems.append('resource observation identity differs from the controller identity')
    if not observation['complete']:
        problems.append('resource observation is incomplete')
    owners = {row['owner']: row['counts'] for row in observation['owners']}
    if len(owners) != len(observation['owners']):
        problems.append('resource observation has duplicate owners')
    if any(sum(row[r] for row in owners.values()) != observation['totals'][r] for r in RESOURCES):
        problems.append('resource totals disagree with exclusive ownership')
    keys = [case_key(row['case']) for row in observation['cases']]
    if len(keys) != len(set(keys)):
        problems.append('resource observation has duplicate case occurrences')
    measured_owners = {row['owner'] for row in observation['cases']}
    if any(owner.startswith('case:') and owner not in measured_owners for owner in owners):
        problems.append('resource observation has an unmeasured case owner')
    for row in observation['cases']:
        owner = 'case:' + json.dumps(row['case'], sort_keys=True, separators=(',', ':'))
        if row['owner'] != owner or row['counts'] != owners.get(owner, zeros()):
            problems.append('case counters disagree with their exclusive owner')
            break
    if expected_identity['stage'] in ('full', 'bootstrap') and digest(
            [row['case'] for row in observation['cases']]) != expected_identity['inventory_digest']:
        problems.append('full observation omits or changes inventory occurrences')
    return problems


def timing_assessment(pairs, *, expected_identity, expected_base_identity, minimum_pairs=3,
                      concern_seconds=1200, candidate_duration_seconds=None):
    """Advisory measured noise, never a duration-based correctness assertion.

    Each sample carries its immutable execution identity. Retries and phases remain
    in the source performance report; these samples represent only the named interval.
    """
    import math
    import re
    reasons = []
    if len(pairs) < minimum_pairs:
        reasons.append('insufficient matched timing pairs')
    base, candidate = [], []
    sample_digests = []
    seen_base, seen_candidate = set(), set()
    for pair in pairs:
        if not isinstance(pair, dict):
            reasons.append('timing pair is malformed')
            continue
        sample_digests.append({side: pair.get(side + '_sample_digest') if isinstance(
            pair.get(side + '_sample_digest'), str) and re.fullmatch(r'sha256:[0-9a-f]{64}', pair[side + '_sample_digest'])
            else None for side in ('baseline', 'candidate')})
        for field, seen in (('baseline_sample_digest', seen_base), ('candidate_sample_digest', seen_candidate)):
            value = pair.get(field)
            if not isinstance(value, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
                reasons.append('timing sample identity unavailable')
            elif value in seen:
                reasons.append('one timing sample was reused as multiple independent samples')
            else:
                seen.add(value)
        if (pair.get('baseline_identity') != expected_base_identity
                or pair.get('candidate_identity') != expected_identity):
            reasons.append('timing pair identity mismatch')
        a, b = pair.get('baseline_seconds'), pair.get('candidate_seconds')
        if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in (a, b)):
            reasons.append('timing interval is missing or invalid')
            continue
        base.append(a); candidate.append(b)
    if seen_base & seen_candidate:
        reasons.append('the same timing sample cannot represent both sides of a pair')
    if not expected_base_identity:
        reasons.append('base timing identity unavailable')
    else:
        for field in ('environment_digest', 'observer_digest', 'cache_state', 'topology', 'stage'):
            if expected_identity[field] != expected_base_identity[field]:
                reasons.append('timing ' + field + ' differs')
    if expected_identity['cache_state'] == 'unknown':
        reasons.append('cache state unavailable')
    noise = max((abs(b-a) for a, b in zip(base, base[1:])), default=0) if not reasons else None
    deltas = [b-a for a, b in zip(base, candidate)]
    growth = bool(not reasons and deltas and all(d > noise for d in deltas))
    concerns = [i for i, value in enumerate(candidate) if value >= concern_seconds]
    duration_concern = (type(candidate_duration_seconds) in (int, float)
                        and candidate_duration_seconds >= concern_seconds)
    if duration_concern:
        reasons.append('observed candidate duration is at least ' + str(concern_seconds) + ' seconds')
    return {'status': 'concerns' if growth or concerns or duration_concern else 'unavailable' if reasons else 'acceptable',
            'reasons': sorted(set(reasons)), 'baseline_seconds': base, 'candidate_seconds': candidate,
            'delta_seconds': deltas, 'noise_envelope_seconds': noise, 'material_growth': growth,
            'sample_digests': sample_digests,
            'concern_samples': concerns, 'interval': 'declared self-test interval; not complete PR elapsed'}


def scaling_assessment(cases, contracts):
    """Bound count growth per input unit; elapsed time cannot establish a complexity bound."""
    import fnmatch
    definitions, samples, findings, unknown = {}, {}, [], []
    for key, contract in contracts.items():
        for family in contract['families']:
            name = family['id']
            if name in definitions and definitions[name] != family:
                findings.append('conflicting scaling family: ' + name)
            definitions[name] = family
    for row in cases:
        matches = [f for f in definitions.values() if any(
            fnmatch.fnmatchcase(row['case']['id'], pattern) for pattern in f['case_patterns'])]
        if len(matches) > 1:
            findings.append('ambiguous scaling family: ' + row['case']['id'])
            continue
        if not matches:
            if row['family'] is not None:
                findings.append('undeclared scaling family: ' + row['case']['id'])
            continue
        family = matches[0]
        if row['family'] != family['id'] or row['input_size'] not in family['input_sizes']:
            findings.append('missing or invalid scaling input: ' + row['case']['id'])
            continue
        counts = samples.setdefault(family['id'], {}).setdefault(row['input_size'], zeros())
        for resource in RESOURCES:
            counts[resource] = max(counts[resource], row['counts'][resource])
    report = []
    for name, family in sorted(definitions.items()):
        measured = samples.get(name, {})
        if set(measured) != set(family['input_sizes']):
            unknown.append('scaling family lacks its declared input sizes: ' + name)
        ordered = sorted(measured)
        for lo, hi in zip(ordered, ordered[1:]):
            for resource in RESOURCES:
                growth = measured[hi][resource] - measured[lo][resource]
                ceiling = family['growth_limits'][resource] * (hi-lo)
                if growth > ceiling:
                    findings.append(f'scaling {name} {resource}: growth {growth} exceeds {ceiling} from {lo} to {hi}')
        report.append({'id': name, 'samples': [{'input_size': size, 'counts': measured[size]} for size in ordered],
                       'growth_limits_per_input_unit': family['growth_limits']})
    return report, findings, unknown


def assess_cost(observation, *, expected_identity, baseline, expected_baseline_digest,
                runtime, census, policy, now, declarations=(), mappings=(), exceptions=(),
                base_observation=None, expected_base_identity=None, expected_cases=None,
                bootstrap_inventory=None, timing_pairs=(), enrollment_issue=None,
                candidate_duration_seconds=None, base_evidence_issue=None):
    """One pure assessment used again at every cache, review and submission boundary.

    Expected identities and enrollment digest come from the controller's retained
    authority, never from the candidate artifact being assessed. The enrollment fixes
    legacy ceilings; a separate actual-base observation owns change attribution.
    """
    from selftest_results import validate_shape
    from build_coordinator_dag import CoordinatorError, validate_test_cost_contracts
    unknown = observation_problems(observation, expected_identity)
    valid_observation = not unknown
    valid_inventory = isinstance(census, dict) and census.get('source_commit') == expected_identity['source_commit']
    if not valid_inventory:
        unknown.append('static census does not match the controller source commit')
        valid_observation = False
        census, runtime, declarations, mappings = {'definitions': [], 'duplicates': []}, [], (), ()
    violations, used, deltas, families = [], [], [], []
    common, added, removed, owner_deltas = [], [], [], []
    aggregate = None
    if digest(policy) != expected_identity['policy_digest']:
        unknown.append('policy differs from the controller identity')
        valid_observation = False
    enrollment = baseline_status(baseline, expected_digest=expected_baseline_digest,
        observer_commit=expected_identity['observer_commit'], observer_digest=expected_identity['observer_digest'],
        environment_digest=expected_identity['environment_digest'])
    valid_baseline = enrollment['mode'] == 'enforced'
    if not valid_baseline:
        unknown.append('needs-baseline: ' + enrollment['reason'])
    else:
        unknown += ['enrollment: ' + reason for reason in baseline['unknown']]
    if enrollment_issue:
        unknown.append('needs-baseline: ' + enrollment_issue)
    if base_evidence_issue:
        unknown.append('base: ' + base_evidence_issue)
    try:
        validate_shape(baseline, 'test-cost-baseline.v1')
        trusted_inventory = digest(baseline) == expected_baseline_digest
    except (ValueError, TypeError):
        trusted_inventory = False
    # Runtime incompatibility invalidates resource comparison, not immutable source
    # identities. Continue enforcing new declarations while measurement is pending.
    legacy = baseline if trusted_inventory else bootstrap_inventory
    if not valid_inventory:
        pass
    elif legacy is not None:
        violations += inventory_findings(runtime, census, legacy, mappings, declarations)
        violations += duplicate_findings(census, legacy)
    else:
        unknown.append('trusted pre-change inventory unavailable; new work cannot be enrolled as legacy')
        # Preserve structural checks even when there is no trusted source/debt reference.
        definitions = {(d['path'], d['qualified_name']) for d in census['definitions']}
        mapped = {(r.get('path'), r.get('qualified_name')) for r in runtime}
        if definitions != mapped:
            violations.append('static/runtime inventory has unexplained definitions or cases')
        violations += duplicate_findings(census, {})
    definitions = {(d['path'], d['qualified_name']): d for d in census['definitions']}
    declared = {case_key(d): d for d in declarations}
    contracts = {}
    for row in runtime:
        try:
            contract = effective_contract(row, definitions.get((row.get('path'), row.get('qualified_name'))),
                                          declared.get(case_key(row)))
            if contract:
                validate_shape(contract, 'test-cost-contract.v1')
                validate_test_cost_contracts({'work_items': [{'id': row['id'], 'test_cost': contract}]})
                contracts[case_key(row)] = contract
        except (ValueError, CoordinatorError) as exc:
            violations.append(str(exc))
    legacy_cases = {case_key(row['case']): row for row in baseline['cases']} if valid_baseline else {}
    actual_cases = observation['cases'] if valid_observation else []
    if valid_observation:
        unknown += observation['unknown']
        for fact in observation.get('ambient_facts', []):
            if fact['kind'] == AMBIENT_GIT_CONFIG:
                row = next((r for r in actual_cases if r['owner'] == fact['owner']), None)
                key = case_key(row['case']) if row else None
                prior = next((r for r in (legacy or {}).get('cases', [])
                              if key and case_key(r['case']) == key), None)
                definition = definitions.get((prior['path'], prior['qualified_name'])) if prior else None
                unchanged = bool(definition and definition['ast_digest'] == prior['source_digest'])
                enrolled = valid_baseline and fact in baseline.get('ambient_facts', [])
                # Existing effects remain explicitly measured debt. Bootstrap has
                # no effect authority; it reports unknown until enrollment. New
                # declared work and newly observed effects cannot borrow that debt.
                if key in contracts or (row and not unchanged) or (valid_baseline and not enrolled):
                    violations.append('ambient Git configuration discovery: ' + fact['owner'])
                else:
                    unknown.append(('enrolled ambient Git debt: ' if enrolled else
                                    'ambient Git baseline unavailable: ') + fact['owner'])
            elif fact['kind'] == 'network-connect':
                row = next((r for r in actual_cases if r['owner'] == fact['owner']), None)
                contract = contracts.get(case_key(row['case'])) if row else None
                if contract and contract['boundary'] == 'pure':
                    violations.append('pure test used a network connection: ' + fact['owner'])
                else:
                    unknown.append('network destination and descendant coverage unavailable: ' + fact['owner'])
        unbudgeted = 0
        if digest([{'id': r['id'], 'occurrence': r['occurrence']} for r in runtime]) != expected_identity['inventory_digest']:
            unknown.append('runtime mapping inventory differs from observation')
        if expected_cases is None and expected_identity['stage'] not in ('full', 'bootstrap'):
            unknown.append('controller-selected case inventory unavailable')
        elif expected_cases is not None and [r['case'] for r in actual_cases] != list(expected_cases):
            unknown.append('observed cases differ from controller-selected cases')
        for row in actual_cases:
            key = case_key(row['case'])
            contract, prior = contracts.get(key), legacy_cases.get(key)
            limits = contract['limits'] if contract else prior['limits'] if prior else None
            if limits is None:
                unbudgeted += 1
                continue
            if prior:
                definition = definitions.get((prior['path'], prior['qualified_name']))
                if definition and definition['ast_digest'] == prior['source_digest']:
                    limits = {r: min(limits[r], prior['limits'][r]) for r in RESOURCES}
            for resource in RESOURCES:
                count = row['counts'][resource]
                if count <= limits[resource]:
                    continue
                allowances = [e for e in exceptions if exception_applies(e, row['case'], resource,
                    expected_identity['source_commit'], now=now,
                    max_seconds=policy['max_exception_seconds']) and count <= e['ceiling']]
                if len(allowances) == 1:
                    if allowances[0] not in used:
                        used.append(allowances[0])
                else:
                    violations.append(f'{key} {resource}: {count} exceeds {limits[resource]} without one live allowance')
        if unbudgeted:
            unknown.append(f'resource ceilings unavailable for {unbudgeted} measured cases; their identities remain in the observation')
        families, growth_findings, missing = scaling_assessment(actual_cases, contracts)
        violations += growth_findings; unknown += missing
        if valid_baseline:
            added_budget = zeros()
            for key, contract in contracts.items():
                prior = legacy_cases.get(key)
                definition = definitions.get((prior['path'], prior['qualified_name'])) if prior else None
                if not prior or not definition or definition['ast_digest'] != prior['source_digest']:
                    for resource in RESOURCES:
                        added_budget[resource] += contract['limits'][resource]
            allowance_budget = zeros()
            for exception in used:
                prior = legacy_cases.get(case_key(exception['case']))
                if prior:
                    resource = exception['resource']
                    allowance_budget[resource] += max(0, exception['ceiling']-prior['limits'][resource])
            full_coverage = digest([r['case'] for r in actual_cases]) == expected_identity['inventory_digest']
            if full_coverage:
                ceilings = {r: baseline['totals'][r]+added_budget[r]+allowance_budget[r] for r in RESOURCES}
                violations += ['aggregate ' + message for message in budget_findings(observation['totals'], ceilings)]
            else:
                unknown.append('whole-suite aggregate coverage unavailable in this focused observation')
            old_owners = {o['owner']: o['counts'] for o in baseline['owners']}
            for owner in observation['owners']:
                name = owner['owner']
                if name.startswith('case:'):
                    continue
                if name in old_owners:
                    ceiling = old_owners[name]
                    if name.startswith('unattributed'):
                        ceiling = {r: ceiling[r]+added_budget[r] for r in RESOURCES}
                else:
                    label = name.split(':', 2)[-1].removeprefix("<class '").removesuffix("'>")
                    owners = {c['fixture_owner']: c['limits'] for c in contracts.values()
                              if label == c['fixture_owner'] or label.startswith(c['fixture_owner'] + '.')}
                    if not owners:
                        unknown.append('resource fixture owner has no declaration: ' + name)
                        continue
                    ceiling = {r: sum(v[r] for v in owners.values()) for r in RESOURCES}
                violations += [name + ' ' + message for message in budget_findings(owner['counts'], ceiling)]
    base_problems = (observation_problems(base_observation, expected_base_identity)
                     if expected_base_identity else ['controller base identity unavailable'])
    if expected_base_identity and expected_base_identity['source_commit'] != expected_identity['base_commit']:
        base_problems.append('base observation is not the actual comparison base')
    if base_problems:
        unknown += ['base: ' + p for p in base_problems]
    elif valid_observation:
        unknown += ['base: ' + p for p in base_observation['unknown']]
        for field in ('observer_commit', 'observer_digest', 'environment_digest', 'cache_state', 'topology', 'stage'):
            if base_observation['identity'][field] != expected_identity[field]:
                unknown.append('comparison ' + field + ' differs')
        if expected_identity['cache_state'] == 'unknown':
            unknown.append('comparison cache state unavailable')
        a = {case_key(r['case']): r for r in base_observation['cases']}
        b = {case_key(r['case']): r for r in actual_cases}
        common = [b[k]['case'] for k in sorted(a.keys() & b.keys())]
        added = [b[k]['case'] for k in sorted(b.keys()-a.keys())]
        removed = [a[k]['case'] for k in sorted(a.keys()-b.keys())]
        deltas = [{'case': b[k]['case'], 'baseline': a[k]['counts'], 'candidate': b[k]['counts'],
                   'delta': {r: b[k]['counts'][r]-a[k]['counts'][r] for r in RESOURCES}}
                  for k in sorted(a.keys() & b.keys())]
        if base_observation['identity']['stage'] == expected_identity['stage']:
            aggregate = {r: observation['totals'][r]-base_observation['totals'][r] for r in RESOURCES}
            left = {o['owner']: o['counts'] for o in base_observation['owners']}
            right = {o['owner']: o['counts'] for o in observation['owners']}
            owner_deltas = [{'owner': key, 'baseline': left.get(key), 'candidate': right.get(key),
                'delta': {r: right[key][r]-left[key][r] for r in RESOURCES} if key in left and key in right else None}
                for key in sorted(left.keys() | right.keys()) if not key.startswith('case:')]
    timing = timing_assessment(timing_pairs, expected_identity=expected_identity,
        expected_base_identity=expected_base_identity, minimum_pairs=policy['timing']['minimum_pairs'],
        concern_seconds=policy['concern_seconds'], candidate_duration_seconds=candidate_duration_seconds)
    timing_findings = timing['reasons'] + (['advisory timing concern requires disposition'] if timing['status']=='concerns' else [])
    status = ('concerns' if violations or timing['status']=='concerns' else
              'unavailable' if unknown or timing['status']=='unavailable' else 'acceptable')
    result = {'schema_version': 'test-cost-assessment.v1', 'identity': expected_identity,
        'baseline_digest': expected_baseline_digest if valid_baseline else None,
        'observation_digest': digest(observation) if valid_observation else None,
        'status': status, 'violations': sorted(set(violations)), 'unknown': sorted(set(unknown)),
        'exceptions': used, 'common': common, 'added': added, 'removed': removed,
        'aggregate_delta': aggregate, 'timing_findings': timing_findings, 'timing': timing,
        'case_deltas': deltas, 'owner_deltas': owner_deltas, 'families': families,
        'cost_clearance': status == 'acceptable', 'scope': expected_identity['stage'],
        'base_identity': expected_base_identity, 'intervals_unavailable': ['acquisition', 'cleanup', 'retries', 'runner-minutes']}
    validate_shape(result, 'test-cost-assessment.v1')
    return result


# One audit callback per interpreter. Python audit hooks cannot be removed, so the
# callback is inert outside a live recorder; wrapped Python attributes ARE restored.
_ACTIVE = None
_AUDIT_INSTALLED = False
AMBIENT_GIT_CONFIG = 'git-config'


def event(resource, amount=1):
    if _ACTIVE is not None:
        _ACTIVE.count(resource, amount)


def _audit(name, args):
    # CPython also audits hot operations such as builtins.id. They carry no
    # resource evidence here; refuse them before touching recorder state.
    if name == 'builtins.id':
        return
    if name not in {'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn',
                    'os.fork', 'shutil.copytree', 'socket.connect'}:
        return
    recorder = _ACTIVE
    if recorder is None or recorder.suspended:
        return
    if name == 'subprocess.Popen':
        import os
        recorder.count('processes')
        executable, argv = args[:2]
        executable_name = Path(os.fsdecode(executable)).name if isinstance(executable, (str, bytes)) else None
        if executable_name is None:
            recorder.unknown.add('process executable kind is unclassified')
        if executable_name in ('git', 'git.exe'):
            recorder.count('git_commands')
            # Git configuration reads happen in the child, beyond this
            # interpreter's file-open audit. Keep only a fixed fact and its
            # owner; command arguments and environment values are never kept.
            if _ambient_git_config_command(argv, args[3] if len(args) > 3 else None):
                recorder.ambient(AMBIENT_GIT_CONFIG)
        # No command strings or environments are retained. Child-internal work is
        # unknown unless a future observer returns qualified descendant evidence.
        recorder.unknown.add('descendant work is not instrumented')
        if isinstance(argv, (list, tuple)) and any(
                os.fsdecode(a) == 'unittest' or Path(os.fsdecode(a)).name.startswith(('demo_', 'selftest.py'))
                for a in argv if isinstance(a, (str, bytes))):
            recorder.count('nested_journeys')
    elif name in ('os.system', 'os.exec', 'os.posix_spawn', 'os.fork') and not recorder.popen_depth:
        recorder.count('processes')
        recorder.unknown.add('alternate process boundary has unknown descendant work')
    elif name == 'shutil.copytree':
        import os
        source = args[0]
        if isinstance(source, (str, bytes)):
            if Path(os.fsdecode(source)).name == '.engine':
                recorder.count('whole_tree_fixtures')
        else:
            recorder.unknown.add('tree-copy source is unclassified')
    elif name == 'socket.connect':
        recorder.ambient('network-connect')


def _ambient_git_config_command(argv, environment):
    """Recognize only unisolated Git config discovery, never its values."""
    import os
    if not isinstance(argv, (list, tuple)):
        return False
    values = [os.fsdecode(value) for value in argv if isinstance(value, (str, bytes))]
    if 'config' not in values:
        return False
    options = values[values.index('config') + 1:]
    # These forms select a repository or caller-named file rather than Git's
    # global/default configuration discovery.
    if any(value in ('--local', '--worktree', '--file', '-f', '--blob') for value in values):
        return False
    # Default writes target the local config. Only a read/discovery consumes
    # ambient config; reading with the short ``git config key`` form counts too.
    reads = {'--get', '--get-all', '--get-regexp', '--get-urlmatch', '--list', '-l', 'get', 'list'}
    positional = [value for value in options if not value.startswith('-')]
    if not reads.intersection(options) and len(positional) != 1:
        return False
    return not _isolated_git_config(environment)


def _isolated_git_config(environment):
    """Whether the child explicitly selected global/system configuration input."""
    import os
    if environment is None:
        environment = os.environ
    if not isinstance(environment, dict):
        return False
    def value(name):
        for key, item in environment.items():
            if isinstance(key, (str, bytes)) and os.fsdecode(key) == name:
                return os.fsdecode(item) if isinstance(item, (str, bytes)) else None
        return None
    global_path = value('GIT_CONFIG_GLOBAL')
    return value('GIT_CONFIG_NOSYSTEM') == '1' and bool(global_path) and os.path.isabs(global_path)


def runtime_limits(root=ROOT):
    """Load bound observation limits before discovery, under fixed implementation ceilings."""
    from selftest_results import read
    policy = read(Path(root) / '.engine/policies/test-cost.json')
    if not isinstance(policy, dict) or policy.get('schema_version') != 'test-cost-policy.v1':
        raise ValueError('invalid test-cost runtime policy')
    ceilings = {'max_observation_bytes': 16 * 1024 * 1024,
                'max_owners': 200000, 'max_counter': 2147483647}
    limits = {name: policy.get(name) for name in ceilings}
    if any(type(value) is not int or not 0 < value <= ceilings[name]
           for name, value in limits.items()):
        raise ValueError('invalid test-cost observation limit')
    return limits


class Recorder:
    """Bounded exclusive counters; no full traces, sleeps, subprocess rewriting or test mocks."""
    def __init__(self, *, max_owners=200000, max_counter=2147483647):
        import threading
        if (type(max_owners) is not int or not 0 < max_owners <= 200000
                or type(max_counter) is not int or not 0 < max_counter <= 2147483647):
            raise ValueError('invalid resource counter limit')
        self.thread_id = threading.get_ident()
        self.max_owners, self.max_counter = max_owners, max_counter
        self.owners = {}
        self.owner = 'unattributed'
        self.unknown = set()
        self.suspended = False
        self.runtime = []
        self.source_binding = None
        self.restores = []
        self.active_cases = []
        self.popen_depth = 0
        self.ambient_facts = set()

    def ambient(self, kind):
        """Record a fixed, bounded known policy fact without source values."""
        import threading
        if self.suspended:
            return
        if kind not in (AMBIENT_GIT_CONFIG, 'network-connect'):
            self.unknown.add('unclassified ambient resource fact')
            return
        owner = self.owner if threading.get_ident() == self.thread_id else 'unattributed:thread'
        if (owner, kind) in self.ambient_facts:
            return
        if len(self.ambient_facts) >= self.max_owners:
            self.unknown.add('ambient fact capacity exceeded')
            return
        self.ambient_facts.add((owner, kind))

    def count(self, resource, amount=1):
        import threading
        if self.suspended:
            return
        if resource not in RESOURCES or type(amount) is not int or amount < 0:
            self.unknown.add('invalid resource counter event')
            return
        owner = self.owner
        if threading.get_ident() != self.thread_id:
            owner = 'unattributed:thread'
            self.unknown.add('background-thread resource ownership is unattributed')
        if owner not in self.owners:
            if len(self.owners) >= self.max_owners:
                self.unknown.add('owner counter capacity exceeded')
                return
            self.owners[owner] = zeros()
        counts = self.owners[owner]
        value = counts[resource] + amount
        if value > self.max_counter:
            self.unknown.add('resource counter capacity exceeded')
            value = self.max_counter
        counts[resource] = value

    def start_case(self, identity):
        self.active_cases.append(self.owner)
        self.owner = 'case:' + json.dumps(identity, sort_keys=True, separators=(',', ':'))
        self.count('processes', 0)

    def stop_case(self):
        self.owner = self.active_cases.pop() if self.active_cases else 'unattributed'

    def _wrap(self, obj, name, resource):
        import functools
        prior = vars(obj)[name]
        original = prior.__func__ if isinstance(prior, classmethod) else getattr(obj, name)
        @functools.wraps(original)
        def observed(*args, **kwargs):
            self.count(resource)
            return original(*args, **kwargs)
        setattr(obj, name, classmethod(observed) if isinstance(prior, classmethod) else observed)
        self.restores.append((obj, name, prior))

    def __enter__(self):
        import sys
        import jsonschema
        import subprocess
        import unittest
        global _ACTIVE, _AUDIT_INSTALLED
        self.previous = _ACTIVE
        if self.previous is not None:
            # Nested observations must not silently remove work from the outer totals.
            self.previous.unknown.add('nested independent recorder; inner work requires explicit join')
        if not _AUDIT_INSTALLED:
            sys.addaudithook(_audit)
            _AUDIT_INSTALLED = True
        _ACTIVE = self
        try:
            prior_init = subprocess.Popen.__init__
            def process_init(*args, **kwargs):
                self.popen_depth += 1
                try:
                    return prior_init(*args, **kwargs)
                finally:
                    self.popen_depth -= 1
            subprocess.Popen.__init__ = process_init
            self.restores.append((subprocess.Popen, '__init__', prior_init))
            prior_run = unittest.TextTestRunner.run
            def nested_run(*args, **kwargs):
                if self.active_cases:
                    self.count('nested_journeys')
                return prior_run(*args, **kwargs)
            unittest.TextTestRunner.run = nested_run
            self.restores.append((unittest.TextTestRunner, 'run', prior_run))
            self._wrap(json.JSONDecoder, 'raw_decode', 'schema_decodes')
            for name in ('Draft3Validator', 'Draft4Validator', 'Draft6Validator', 'Draft7Validator',
                         'Draft201909Validator', 'Draft202012Validator'):
                cls = getattr(jsonschema, name)
                self._wrap(cls, 'check_schema', 'metaschema_validations')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        global _ACTIVE
        for obj, name, prior in reversed(self.restores):
            setattr(obj, name, prior)
        self.restores.clear()
        _ACTIVE = self.previous

    def document(self, *, source, scope, complete, process_exit):
        totals = zeros()
        for counts in self.owners.values():
            for resource in RESOURCES:
                totals[resource] += counts[resource]
        return {'schema_version': 'test-cost-run.v1', 'source': source, 'scope': scope,
                'complete': complete, 'process_exit': process_exit,
                'unknown': sorted(self.unknown), 'totals': totals,
                'owners': [{'owner': owner, 'counts': counts} for owner, counts in sorted(self.owners.items())],
                'ambient_facts': [{'owner': owner, 'kind': kind}
                                  for owner, kind in sorted(self.ambient_facts)],
                'inventory': self.runtime}


def normalize_run(run, identity, *, expected_tree, outcomes=None):
    """Join controller-derived identities to raw observations; a raw file is never a receipt."""
    from selftest_results import validate_shape
    validate_shape(run, 'test-cost-run.v1')
    owners = {o['owner']: o['counts'] for o in run['owners']}
    if len(owners) != len(run['owners']):
        raise ValueError('resource observation contains duplicate owners')
    if any(sum(counts[resource] for counts in owners.values()) != run['totals'][resource]
           for resource in RESOURCES):
        raise ValueError('resource totals disagree with exclusive owner counts')
    if run['source'] != {'tree': expected_tree, 'worktree_dirty': False}:
        raise ValueError('resource observation does not match the immutable measured source')
    inventory = [{'id': c['id'], 'occurrence': c['occurrence']} for c in run['inventory']]
    if digest(inventory) != identity['inventory_digest']:
        raise ValueError('resource inventory does not match the controller identity')
    outcome_cases = {}
    outcomes_passed = True
    if outcomes is not None:
        from selftest_results import validate
        outcomes_passed = all(validate(outcomes))
        if outcomes['inventory'] != inventory or outcomes['source'] != run['source']:
            raise ValueError('cost and outcome observations describe different executions')
        outcome_cases = {case_key(c): c for c in outcomes['cases']}
    cases = []
    for case in run['inventory']:
        selected = {k: case[k] for k in ('id', 'occurrence')}
        owner = 'case:' + json.dumps(selected, sort_keys=True, separators=(',', ':'))
        # Inventory may include unselected cases in a focused run. Absence is unknown,
        # not an invented zero cost: only observed owners become measured case rows.
        outcome = outcome_cases.get(case_key(case))
        known_not_started = outcome and outcome['outcome'] == 'skipped' and not outcome['started']
        if owner in owners or known_not_started:
            cases.append({'case': selected, 'owner': owner, 'counts': owners.get(owner, zeros()),
                          'family': case.get('family'), 'input_size': case.get('input_size')})
    result = {'schema_version': 'test-cost-observation.v1', 'identity': identity,
              'complete': bool(run['complete'] and run['process_exit'] == 0 and outcomes_passed),
              'unknown': run['unknown'], 'totals': run['totals'], 'owners': run['owners'], 'cases': cases}
    if 'ambient_facts' in run:
        result['ambient_facts'] = run['ambient_facts']
    validate_shape(result, 'test-cost-observation.v1')
    return result


def enroll_baseline(observation, census, runtime, *, owner, reason, revisit, declarations=()):
    """Explicit activation only. Routine runs cannot update the pinned budget or add new debt."""
    from selftest_results import validate_shape
    validate_shape(observation, 'test-cost-observation.v1')
    identity = observation['identity']
    if identity['stage'] != 'bootstrap' or not observation['complete']:
        raise ValueError('baseline activation needs complete bootstrap observations')
    if identity['source_commit'] != census['source_commit']:
        raise ValueError('legacy census and measured source differ')
    if not all(isinstance(v, str) and v.strip() for v in (owner, reason, revisit)):
        raise ValueError('baseline enrollment needs owner, reason and revisit condition')
    definitions = {(d['path'], d['qualified_name']): d for d in census['definitions']}
    runtime_map = {case_key(c): c for c in runtime}
    if len(runtime_map) != len(runtime):
        raise ValueError('duplicate runtime identity at baseline activation')
    cases = []
    declared = {case_key(c): c for c in declarations}
    for measured in observation['cases']:
        row = runtime_map.get(case_key(measured['case']))
        definition = definitions.get((row.get('path'), row.get('qualified_name'))) if row else None
        if not definition:
            raise ValueError('legacy activation cannot enroll a new or unmapped case')
        contract = effective_contract(row, definition, declared.get(case_key(row)))
        if contract is not None:
            validate_shape(contract, 'test-cost-contract.v1')
            continue  # A prospective declaration must never become legacy debt on a later bootstrap.
        cases.append({'case': measured['case'], 'source_digest': definition['ast_digest'],
                      'path': definition['path'], 'qualified_name': definition['qualified_name'],
                      'limits': measured['counts']})
    if {case_key(c['case']) for c in observation['cases']} != set(runtime_map):
        raise ValueError('baseline activation needs cost or observed-skip evidence for every runtime case')
    if {(c['path'], c['qualified_name']) for c in runtime} != set(definitions):
        raise ValueError('baseline activation needs complete static/runtime coverage')
    result = {'schema_version': 'test-cost-baseline.v1', 'source_commit': identity['source_commit'],
              'observation_digest': digest(observation), 'identity': identity,
              'owner': owner, 'reason': reason, 'revisit': revisit, 'cases': cases,
              'duplicates': census['duplicates'], 'owners': observation['owners'],
              'totals': observation['totals'], 'unknown': observation['unknown']}
    if 'ambient_facts' in observation:
        result['ambient_facts'] = observation['ambient_facts']
    validate_shape(result, 'test-cost-baseline.v1')
    return result


def baseline_status(baseline, *, expected_digest, observer_commit, observer_digest, environment_digest):
    """Missing/corrupt/incompatible enrollment opens measurement, never cost clearance."""
    from selftest_results import validate_shape
    reason = None
    try:
        validate_shape(baseline, 'test-cost-baseline.v1')
        if digest(baseline) != expected_digest:
            reason = 'enrollment differs from the pinned activation digest'
        elif (baseline['identity']['observer_commit'] != observer_commit
              or baseline['identity']['observer_digest'] != observer_digest):
            reason = 'observer adapter changed; parity and measurement require requalification'
        elif baseline['identity']['environment_digest'] != environment_digest:
            reason = 'execution environment is incompatible'
    except (ValueError, TypeError, KeyError):
        reason = 'baseline is missing or corrupt'
    return {'mode': 'measurement-bootstrap' if reason else 'enforced',
            'cost_clearance': False, 'reason': reason,
            'required': ['existing correctness checks', 'static test inventory checks',
                         'bounded measurement', 'explicit enrollment review'] if reason else []}


def enrolled_observation(baseline):
    """Recover initial all-legacy evidence only when its recorded digest proves equality.

    Later enrollments may exclude prospective cases or carry scaling observations.
    Those need their original observation artifact; absent data is not manufactured.
    """
    from selftest_results import validate_shape
    validate_shape(baseline, 'test-cost-baseline.v1')
    cases = [{'case': row['case'],
              'owner': 'case:' + json.dumps(row['case'], sort_keys=True, separators=(',', ':')),
              'counts': row['limits'], 'family': None, 'input_size': None}
             for row in baseline['cases']]
    observation = {'schema_version': 'test-cost-observation.v1', 'identity': baseline['identity'],
                   'complete': True, 'unknown': baseline['unknown'], 'totals': baseline['totals'],
                   'owners': baseline['owners'], 'cases': cases}
    if 'ambient_facts' in baseline:
        observation['ambient_facts'] = baseline['ambient_facts']
    if digest(observation) != baseline['observation_digest']:
        return None
    return observation


def enrollment_context(*, root=ROOT, environment_digest):
    """Read installed enrollment without ever learning limits from a candidate run.

    Missing or incompatible installations retain their correctness gates and expose
    the same explicit bootstrap state. Activation is a reviewed tracked artifact.
    """
    from selftest_results import read, validate_shape
    baseline = activation = None
    try:
        directory = Path(root) / '.engine/policies'
        activation = read(directory / 'test-cost-activation.json')
        validate_shape(activation, 'test-cost-activation.v1')
        if digest(read(directory / 'test-cost-parity.json')) != activation['parity_rules_digest']:
            raise ValueError('reviewed parity rules differ from activation')
        baseline = unpack_enrollment(read(directory / 'test-cost-legacy-baseline.json'))
        status = baseline_status(baseline, expected_digest=activation['baseline_digest'],
            observer_commit=activation['identity']['observer_commit'],
            observer_digest=observer_fingerprint(root), environment_digest=environment_digest)
        if (baseline['identity'] != activation['identity']
                or baseline['observation_digest'] != activation['observation_digest']
                or len(baseline['cases']) != activation['legacy_case_count']
                or digest(activation['environment']) != environment_digest):
            raise ValueError('activation and enrollment provenance differ')
    except (OSError, ValueError, KeyError, TypeError):
        status = baseline_status(None, expected_digest='', observer_commit='',
                                 observer_digest='', environment_digest=environment_digest)
    return {'baseline': baseline, 'activation': activation, **status}


def require_outcome_parity(native, observed, *, normalizations=(), census=None, runtime=()):
    """An adapter cannot qualify itself by changing the workload or its outcome accounting."""
    from selftest_results import validate, validate_shape
    for result in (native, observed):
        complete, passed = validate(result)
        if not complete or not passed:
            raise ValueError('adapter qualification requires complete passing outcomes on both runs')
    for key in ('source', 'inventory', 'selected', 'executed_count'):
        if native[key] != observed[key]:
            raise ValueError('adapter changed ' + key)
    rules = {}
    definitions = {(d['path'], d['qualified_name']): d for d in (census or {}).get('definitions', [])}
    known_cases = {case_key(c) for c in native['cases']}
    runtime_map = {case_key(c): c for c in runtime}
    for rule in normalizations:
        validate_shape(rule, 'test-cost-parity-rule.v1')
        definition = definitions.get((rule['path'], rule['qualified_name']))
        key = case_key(rule['case'])
        mapped = runtime_map.get(key, {})
        if (key in rules or key not in known_cases or rule['source_tree'] != native['source']['tree']
                or not definition or rule['source_digest'] != definition['ast_digest']
                or any(mapped.get(k) != rule[k] for k in ('path', 'qualified_name'))):
            raise ValueError('parity normalization does not match the reviewed source and case')
        rules[key] = rule
    def outcomes(result):
        import re
        rows = []
        for case in result['cases']:
            row = {k: case[k] for k in ('id', 'occurrence', 'outcome', 'started', 'stopped', 'reason')}
            row['subtests'] = [{**sub, 'id': re.sub(r' at 0x[0-9a-fA-F]+(?=>)', ' at [address]', sub['id'])}
                              for sub in case['subtests']]
            rule = rules.get(case_key(case))
            if rule:
                mode = rule['mode']
                if mode == 'unordered-subtests':
                    row['subtests'].sort(key=lambda sub: json.dumps(sub, sort_keys=True))
                else:
                    pattern = (r'(?<![0-9a-f])[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}(?![0-9a-f])'
                               if mode == 'opaque-uuid4' else r'(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])')
                    tokens = {}
                    def replace(match):
                        value = match.group()
                        if value not in tokens:
                            tokens[value] = '[opaque-' + str(len(tokens)) + ']'
                        return tokens[value]
                    # Preserve distinctness and repeated references across the entire case.
                    # Only the explicitly reviewed volatile token kind changes; outcomes,
                    # input structure, other values and subtest order remain significant.
                    row['subtests'] = [{**sub, 'id': re.sub(pattern, replace, sub['id'])}
                                      for sub in row['subtests']]
            rows.append(row)
        return rows
    if outcomes(native) != outcomes(observed) or native['fixtures'] != observed['fixtures']:
        raise ValueError('adapter changed test outcomes')


def observe_retained_source(source_root, output_directory, *, pattern='test_*.py'):
    """Disposable adapter over the existing child entry, keeping original source bytes intact.

    Call only in a fresh subprocess: loaded baseline test modules keep their original
    names; the observation object is a private alias used only by the retained launcher.
    """
    import importlib.util
    import os
    import sys
    source = Path(source_root).resolve()
    output = Path(output_directory).resolve()
    if output.is_relative_to(source):
        raise ValueError('observation output must be outside the immutable source')
    output.mkdir(parents=True, exist_ok=False)
    observer_root = ROOT
    # Match the retained source's native ``uv --directory .engine`` execution
    # context. Relative project discovery must not resolve the adapter checkout.
    os.chdir(source / '.engine')
    sys.path.insert(0, str(source / '.engine/tools'))
    import selftest
    import engine_fixture
    # Preserve the pinned modules seen by tests. Only the runner's reference changes.
    spec = importlib.util.spec_from_file_location('test_cost_adapter_results',
                                                  observer_root / '.engine/tools/selftest_results.py')
    results = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(results)
    selftest.selftest_results = results
    sys.modules['selftest_cost'] = sys.modules[__name__]
    original_inventory = runtime_inventory
    globals()['runtime_inventory'] = lambda cases, root=source: original_inventory(cases, root=root)
    module = ast.parse((observer_root / '.engine/tools/selftest.py').read_text())
    functions = [node for node in module.body if isinstance(node, ast.FunctionDef)
                 and node.name in ('_run_child', '_run_child_observed')]
    if len(functions) != 2:
        raise ValueError('retained observer adapter does not support this runner version')
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<cost-observer-adapter>', 'exec'), selftest.__dict__)
    clone = engine_fixture.clone_engine
    import functools
    @functools.wraps(clone)
    def observed_clone(*args, **kwargs):
        event('whole_tree_fixtures')
        return clone(*args, **kwargs)
    engine_fixture.clone_engine = observed_clone
    os.environ[selftest._NESTED_ENV] = '1'
    os.environ['ENGINE_AMBIENT_QUALIFICATION_OFF'] = '1'
    from providers import SESSION_ENV_CHAIN
    for name in SESSION_ENV_CHAIN:
        os.environ.pop(name, None)
    args = selftest._build_parser().parse_args([
        '--child', '--start-dir', str(source / '.engine/tools'), '--pattern', pattern,
        '--results-path', str(output / 'outcomes.json'),
        '--performance-path', str(output / 'performance.json')])
    args.cost_path = str(output / 'cost.json')
    results.write(args.cost_path, {'schema_version': 'test-cost-run.v1', 'complete': False,
                                   'unknown': ['child has not finalized']})
    args.progress_fd = os.open(output / 'progress.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return selftest._run_child(args)


def observer_fingerprint(root=ROOT):
    """Content identity of executed observation code, independent of later report consumers."""
    selected = {
        'selftest_cost.py': {'Recorder', 'event', '_audit', 'runtime_inventory', 'declared_contract',
                            'observe_retained_source', 'zeros', 'runtime_limits',
                            '_ambient_git_config_command', '_isolated_git_config'},
        'selftest_results.py': {'Observation', 'write'},
        'selftest.py': {'_run_child', '_run_child_observed'},
    }
    material = {}
    for path, names in selected.items():
        parsed = ast.parse((Path(root) / '.engine/tools' / path).read_text())
        material[path] = [ast.dump(node, include_attributes=False) for node in parsed.body
                          if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
        if len(material[path]) != len(names):
            raise ValueError('observation adapter is missing an expected implementation boundary')
    return digest(material)


def inventory_source(source_root, output_path):
    """Discover a retained source in a fresh process, using its existing serial loader.

    This supplies identity-only bootstrap classification; it creates no measured
    ceilings, cost approval or substitute test execution. The controller pins Git
    independently before using the returned source and census.
    """
    import os
    import sys
    import tempfile
    source, output = Path(source_root).resolve(), Path(output_path).resolve()
    if output.is_relative_to(source) or not (source / '.engine/tools/selftest.py').is_file():
        raise ValueError('inventory needs retained Engine source and an external output path')
    if 'selftest' in sys.modules:
        raise ValueError('source inventory requires a fresh process')
    for key in list(os.environ):
        if key.startswith('GIT_'):
            os.environ.pop(key)
    os.environ.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS='0')
    with tempfile.TemporaryDirectory(prefix='engine-cost-inventory-controls-') as controls:
        os.environ.pop(_COST_EXCEPTION_ENV, None)
        os.environ.update(inventory_environment(controls))
        os.environ.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS='0')
        return _inventory_source(source, output)


def _inventory_source(source, output):
    """Run discovery after ``inventory_source`` has removed ambient authority."""
    import os
    import subprocess
    import sys
    import unittest
    sys.path.insert(0, str(source / '.engine/tools'))
    import selftest
    from providers import SESSION_ENV_CHAIN
    for key in SESSION_ENV_CHAIN:
        os.environ.pop(key, None)
    os.environ[selftest._NESTED_ENV] = '1'
    os.environ['ENGINE_AMBIENT_QUALIFICATION_OFF'] = '1'
    binding = selftest._tree_binding(str(source / '.engine/tools'))
    if not binding['tree'] or binding['worktree_dirty'] is not False:
        raise ValueError('source inventory requires a clean committed checkout')
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    sources = {p.relative_to(source).as_posix(): p.read_text()
               for p in (source / '.engine/tools').rglob('test_*.py')}
    census = static_census(sources, commit)
    loader = unittest.TestLoader()
    cases = list(selftest._flatten(loader.discover(str(source / '.engine/tools'))))
    if loader.errors or len(cases) > 200000:
        raise ValueError('source inventory is incomplete or exceeds its case bound')
    inventory = runtime_inventory(cases, root=source)
    if selftest._tree_binding(str(source / '.engine/tools')) != binding:
        raise ValueError('source changed during inventory discovery')
    if subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() != commit:
        raise ValueError('source commit changed during inventory discovery')
    document = {'schema_version': 'test-cost-inventory.v1', 'source_commit': commit,
                'source': binding, 'runtime': inventory, 'census': census}
    encoded = json.dumps(document, sort_keys=True, allow_nan=False).encode()
    if len(encoded) > 16 * 1024 * 1024:
        raise ValueError('source inventory exceeds its artifact byte bound')
    with output.open('xb') as handle:
        handle.write(encoded)
    return 0


def identity_only_inventory(document, *, expected_commit, expected_tree, duplicate_enrollment=()):
    """Use verified pre-change identities during bootstrap, without inventing resource debt."""
    from selftest_results import validate_shape
    validate_shape(document, 'test-cost-inventory.v1')
    if (document['source_commit'] != expected_commit
            or document['source'] != {'tree': expected_tree, 'worktree_dirty': False}
            or document['census']['source_commit'] != expected_commit):
        raise ValueError('bootstrap source inventory differs from the independently pinned base')
    census = document['census']
    definitions = {(d['path'], d['qualified_name']): d for d in census['definitions']}
    cases = []
    for row in document['runtime']:
        definition = definitions.get((row['path'], row['qualified_name']))
        if not definition:
            raise ValueError('bootstrap cannot infer an unmapped historical case identity')
        cases.append({'case': {k: row[k] for k in ('id', 'occurrence')}, 'path': row['path'],
                      'qualified_name': row['qualified_name'], 'source_digest': definition['ast_digest']})
    legacy = {'cases': cases, 'duplicates': list(duplicate_enrollment)}
    if (len({case_key(c['case']) for c in cases}) != len(cases)
            or {(c['path'], c['qualified_name']) for c in cases} != set(definitions)
            or duplicate_findings(census, legacy)):
        raise ValueError('bootstrap source inventory has unexplained cases or definitions')
    return legacy


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    observe = subs.add_parser('observe-retained', help='observe pinned source through the existing serial child entry')
    observe.add_argument('--source-root', required=True)
    observe.add_argument('--output-directory', required=True)
    observe.add_argument('--pattern', default='test_*.py')
    inspect_parser = subs.add_parser('inspect', help='show the readable identity and largest enrolled resource costs')
    inspect_parser.add_argument('--baseline', required=True)
    inspect_parser.add_argument('--case', help='inspect one exact case id, including all occurrences')
    inventory = subs.add_parser('inventory', help='discover pinned source identities without running cases or learning budgets')
    inventory.add_argument('--source-root', required=True)
    inventory.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    if args.command == 'observe-retained':
        return observe_retained_source(args.source_root, args.output_directory, pattern=args.pattern)
    if args.command == 'inventory':
        return inventory_source(args.source_root, args.output)
    if args.command == 'inspect':
        from selftest_results import read, validate_shape
        baseline = read(args.baseline)
        if baseline.get('schema_version') == 'test-cost-bundle.v1':
            baseline = unpack_enrollment(baseline)
        validate_shape(baseline, 'test-cost-baseline.v1')
        if args.case:
            result = [c for c in baseline['cases'] if c['case']['id'] == args.case]
        else:
            result = {k: baseline[k] for k in ('source_commit', 'identity', 'owner', 'reason', 'revisit', 'totals', 'unknown')}
            result['baseline_digest'] = digest(baseline)
            result['legacy_case_count'] = len(baseline['cases'])
            result['largest_by_resource'] = {resource: [
                {'case': c['case'], 'ceiling': c['limits'][resource]}
                for c in sorted(baseline['cases'], key=lambda c: c['limits'][resource], reverse=True)[:5]
                if c['limits'][resource]] for resource in RESOURCES}
        print(json.dumps(result, indent=2))
        return 0
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
