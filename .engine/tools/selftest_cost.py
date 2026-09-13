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
            path = Path(inspect.getsourcefile(method)).resolve().relative_to(Path(root).resolve()).as_posix()
            qualified = method.__qualname__
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


# One audit callback per interpreter. Python audit hooks cannot be removed, so the
# callback is inert outside a live recorder; wrapped Python attributes ARE restored.
_ACTIVE = None
_AUDIT_INSTALLED = False


def event(resource, amount=1):
    if _ACTIVE is not None:
        _ACTIVE.count(resource, amount)


def _audit(name, args):
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


class Recorder:
    """Bounded exclusive counters; no full traces, sleeps, subprocess rewriting or test mocks."""
    def __init__(self, *, max_owners=200000, max_counter=2147483647):
        import threading
        self.thread_id = threading.get_ident()
        self.max_owners, self.max_counter = max_owners, max_counter
        self.owners = {}
        self.owner = 'unattributed'
        self.unknown = set()
        self.suspended = False
        self.runtime = []
        self.restores = []
        self.active_cases = []
        self.popen_depth = 0

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
                'inventory': self.runtime}


def normalize_run(run, identity, *, expected_tree):
    """Join controller-derived identities to raw observations; a raw file is never a receipt."""
    from selftest_results import validate_shape
    validate_shape(run, 'test-cost-run.v1')
    if run['source'] != {'tree': expected_tree, 'worktree_dirty': False}:
        raise ValueError('resource observation does not match the immutable measured source')
    inventory = [{'id': c['id'], 'occurrence': c['occurrence']} for c in run['inventory']]
    if digest(inventory) != identity['inventory_digest']:
        raise ValueError('resource inventory does not match the controller identity')
    owners = {o['owner']: o['counts'] for o in run['owners']}
    cases = []
    for case in run['inventory']:
        selected = {k: case[k] for k in ('id', 'occurrence')}
        owner = 'case:' + json.dumps(selected, sort_keys=True, separators=(',', ':'))
        # Inventory may include unselected cases in a focused run. Absence is unknown,
        # not an invented zero cost: only observed owners become measured case rows.
        if owner in owners:
            cases.append({'case': selected, 'owner': owner, 'counts': owners[owner],
                          'family': case.get('family'), 'input_size': case.get('input_size')})
    result = {'schema_version': 'test-cost-observation.v1', 'identity': identity,
              'complete': bool(run['complete'] and run['process_exit'] == 0),
              'unknown': run['unknown'], 'totals': run['totals'], 'owners': run['owners'], 'cases': cases}
    validate_shape(result, 'test-cost-observation.v1')
    return result


def enroll_baseline(observation, census, runtime, *, owner, reason, revisit):
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
    for measured in observation['cases']:
        row = runtime_map.get(case_key(measured['case']))
        definition = definitions.get((row.get('path'), row.get('qualified_name'))) if row else None
        if not definition:
            raise ValueError('legacy activation cannot enroll a new or unmapped case')
        cases.append({'case': measured['case'], 'source_digest': definition['ast_digest'],
                      'path': definition['path'], 'qualified_name': definition['qualified_name'],
                      'limits': measured['counts']})
    result = {'schema_version': 'test-cost-baseline.v1', 'source_commit': identity['source_commit'],
              'observation_digest': digest(observation), 'identity': identity,
              'owner': owner, 'reason': reason, 'revisit': revisit, 'cases': cases,
              'duplicates': census['duplicates'], 'owners': observation['owners'],
              'totals': observation['totals'], 'unknown': observation['unknown']}
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


def require_outcome_parity(native, observed):
    """An adapter cannot qualify itself by changing the workload or its outcome accounting."""
    from selftest_results import validate
    for result in (native, observed):
        complete, passed = validate(result)
        if not complete or not passed:
            raise ValueError('adapter qualification requires complete passing outcomes on both runs')
    for key in ('source', 'inventory', 'selected', 'executed_count'):
        if native[key] != observed[key]:
            raise ValueError('adapter changed ' + key)
    def outcomes(result):
        import re
        rows = []
        for case in result['cases']:
            row = {k: case[k] for k in ('id', 'occurrence', 'outcome', 'started', 'stopped', 'reason')}
            row['subtests'] = [{**sub, 'id': re.sub(r' at 0x[0-9a-fA-F]+(?=>)', ' at [address]', sub['id'])}
                              for sub in case['subtests']]
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
                            'observe_retained_source', 'zeros'},
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


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    observe = subs.add_parser('observe-retained', help='observe pinned source through the existing serial child entry')
    observe.add_argument('--source-root', required=True)
    observe.add_argument('--output-directory', required=True)
    observe.add_argument('--pattern', default='test_*.py')
    args = parser.parse_args(argv)
    if args.command == 'observe-retained':
        return observe_retained_source(args.source_root, args.output_directory, pattern=args.pattern)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
