#!/usr/bin/env python3
"""Demonstrate actual resource growth with an unchanged test body, then its repair.

Run with --scenario shared-helper (or schema, metaschema, fixture, nested, ambient-git,
duplicate, stale). Counters execute real bounded work. Commit/tree identities are explicit
synthetic fixture identities, never evidence about this checkout or a merge qualification.
The command fails if the selected violation is missed or the repaired case is rejected.
"""
from __future__ import annotations
import argparse
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import selftest_cost as cost

SCENARIOS = ('shared-helper', 'schema', 'metaschema', 'fixture', 'nested', 'ambient-git', 'duplicate', 'stale')


def example(scenario):
    """One actual bounded counter journey with explicitly synthetic source identities."""
    if scenario not in SCENARIOS:
        raise ValueError('unknown cost scenario')
    policy = json.loads((cost.ROOT / '.engine/policies/test-cost.json').read_text())
    case = {'id': 'test_example.C.test_behavior', 'occurrence': 1}
    source = 'class C:\n def test_behavior(self): helper()\n'
    census = cost.static_census({'test_example.py': source}, 'a'*40)
    runtime = [{**case, 'path': 'test_example.py', 'qualified_name': 'C.test_behavior',
                'contract': None, 'family': None, 'input_size': None}]
    identity = {'source_commit': 'a'*40, 'base_commit': 'a'*40, 'observer_commit': 'c'*40,
        'observer_digest': cost.digest('synthetic-demo-observer'), 'plan_digest': cost.digest('synthetic-demo-plan'),
        'contract_digest': cost.digest('zero-resource fixture'), 'policy_digest': cost.digest(policy),
        'inventory_digest': cost.digest([case]), 'environment_digest': cost.digest('demo environment'),
        'cache_state': 'cold', 'topology': 'serial', 'stage': 'bootstrap', 'attempt': 'base',
        'node': None, 'artifact_digest': cost.digest('synthetic base tree')}
    owner = 'case:' + json.dumps(case, sort_keys=True, separators=(',', ':'))

    def measure(action):
        with cost.Recorder() as recorder:
            recorder.start_case(case)
            action()
            recorder.stop_case()
        raw = recorder.document(source={}, scope='full', complete=True, process_exit=0)
        counts = next(row['counts'] for row in raw['owners'] if row['owner'] == owner)
        return {'schema_version': 'test-cost-observation.v1', 'identity': copy.deepcopy(identity),
            'complete': True, 'unknown': raw['unknown'], 'totals': raw['totals'], 'owners': raw['owners'],
            'cases': [{'case': case, 'owner': owner, 'counts': counts, 'family': None, 'input_size': None}]}

    base = measure(lambda: None)
    enrollment = cost.enroll_baseline(base, census, runtime, owner='demonstration fixture',
        reason='Actual zero-work fixture measurement; not project debt enrollment', revisit='Each demo run')
    with tempfile.TemporaryDirectory(prefix='test-cost-demo-') as directory:
        folder = Path(directory)
        (folder / '.engine').mkdir()
        (folder / '.engine' / 'one.txt').write_text('one bounded fixture file')
        if scenario == 'fixture':
            subprocess.run(['git', 'init', '-q', str(folder)], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(folder), 'add', '.engine/one.txt'], check=True, capture_output=True)
        def helper():
            if scenario == 'shared-helper':
                subprocess.run([sys.executable, '-c', 'pass'], check=True, capture_output=True)
            elif scenario == 'schema':
                json.loads('{}'); json.loads('{}')
            elif scenario == 'metaschema':
                from jsonschema import Draft202012Validator
                Draft202012Validator.check_schema({'type': 'object'})
            elif scenario == 'fixture':
                from engine_fixture import clone_engine
                clone_engine(str(folder), str(folder / 'copy'))
            elif scenario == 'nested':
                unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite([unittest.FunctionTestCase(lambda: None)]))
            elif scenario == 'ambient-git':
                subprocess.run(['git', 'rev-parse', '--is-inside-work-tree'], cwd=folder, capture_output=True)
        candidate = measure(helper)
    base['identity']['stage'] = 'full'
    identity.update(source_commit='b'*40, stage='full', attempt='candidate', artifact_digest=cost.digest('synthetic candidate tree'))
    candidate['identity'] = copy.deepcopy(identity)
    census['source_commit'] = 'b'*40
    context = {'expected_identity': copy.deepcopy(identity), 'baseline': enrollment,
        'expected_baseline_digest': cost.digest(enrollment), 'runtime': runtime, 'census': census,
        'policy': policy, 'now': '2026-09-13T12:00:00Z', 'base_observation': base,
        'expected_base_identity': copy.deepcopy(base['identity'])}
    if scenario == 'duplicate':
        context['census'] = cost.static_census({'test_example.py': source+' def test_behavior(self): helper()\n'}, 'b'*40)
    elif scenario == 'stale':
        candidate['identity']['attempt'] = 'another attempt'
    return candidate, context


def demonstrate(scenario):
    import build_coordinator_core as core
    import build_coordinator_work as work
    def assess(observation, context):
        context = dict(context)
        now = context.pop('now')
        retained = work.retain_cost(observation, context)
        try:
            return work.assess_retained_cost(retained, expected_identity=context['expected_identity'], now=now)['violations']
        except core.CoordinatorError as exc:
            return ['controller refused unusable evidence: '+str(exc)]
    observation, context = example(scenario)
    broken = assess(observation, context)
    # Repair the helper, not the unchanged test body, and take a new actual observation.
    repaired, repaired_context = example('stale')
    repaired['identity'] = copy.deepcopy(repaired_context['expected_identity'])
    fixed = assess(repaired, repaired_context)
    return {'scenario': scenario, 'evidence_kind': 'real counters with synthetic fixture identities',
        'counts': observation['totals'], 'violations': broken,
        'repair_violations': fixed, 'cost_clearance': False,
        'passed': bool(broken) and not fixed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=SCENARIOS, default='shared-helper')
    args = parser.parse_args(argv)
    result = demonstrate(args.scenario)
    print(json.dumps(result, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
