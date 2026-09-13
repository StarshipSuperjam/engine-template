"""Bounded fault controls for declaration and source/runtime identity accounting."""
import copy
import json
from pathlib import Path
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import selftest_cost as cost

CONTRACT = {
    'schema_version': 'test-cost-contract.v1', 'supported_fault': 'Silent test loss or unenrolled cost',
    'boundary': 'pure', 'boundary_rationale': 'Source and inventory decisions have no process boundary',
    'fixture_owner': 'test_selftest_cost', 'dependencies': ['jsonschema', 'selftest_results', 'selftest_support'],
    'data_reads': ['.engine/schemas/test-cost-*.json', '.engine/tools/test_build_coordinator.py',
                   '.engine/policies/test-cost-legacy-static.json'], 'cadence': 'pr',
    'limits': {**cost.zeros(), 'schema_decodes': 10}, 'mutable_state': 'Fresh input per case',
    'cache_lifetime': 'module', 'added_cost_risk': 'Bounded AST and dictionary checks', 'families': [],
}


@cost.declaration(CONTRACT)
class TestInventory(unittest.TestCase):
    def test_unreachable_definitions_and_shadowed_classes_are_not_silently_lost(self):
        source = 'class T:\n def test_a(self): pass\nclass T:\n def test_a(self): return 1\n'
        census = cost.static_census({'test_example.py': source}, 'a'*40)
        self.assertTrue(cost.duplicate_findings(census, {}))
        self.assertTrue(any('orphan test definition' in f for f in cost.inventory_findings([], census, {})))

    def test_budget_growth_and_exception_expiry_cannot_reuse_old_permission(self):
        import moment
        counts = {**cost.zeros(), 'processes': 3}
        self.assertTrue(cost.budget_findings(counts, {**cost.zeros(), 'processes': 2}))
        case = {'id': 'case', 'occurrence': 1}
        exception = {'id': 'debt', 'owner': 'team', 'reason': 'Repair pending',
                     'supported_fault': 'Preserve a process boundary while its fixture is repaired',
                     'fault_preservation_evidence': 'The unchanged boundary regression still passes',
                     'revisit': 'Fixture repair', 'issued_at': '2026-09-13T00:00:00Z',
                     'expires_at': '2026-09-14T00:00:00Z', 'case': case, 'resource': 'processes',
                     'ceiling': 3, 'source_commit': 'a'*40}
        now = '2026-09-13T23:59:59Z'
        self.assertTrue(cost.exception_applies(exception, case, 'processes', 'a'*40, now=now))
        now = moment.to_z(moment.epoch(now) + 1)
        self.assertFalse(cost.exception_applies(exception, case, 'processes', 'a'*40, now=now))
        exception['expires_at'] = '2027-09-14T00:00:00Z'
        self.assertFalse(cost.exception_applies(exception, case, 'processes', 'a'*40, now=now))
        exception['expires_at'] = '2026-09-15T00:00:00Z'
        exception['revisit'] = ''
        self.assertFalse(cost.exception_applies(exception, case, 'processes', 'a'*40, now=now))
        exception['revisit'] = 'Fixture repair'
        exception['fault_preservation_evidence'] = ' '
        self.assertFalse(cost.exception_applies(exception, case, 'processes', 'a'*40, now=now))
        del exception['fault_preservation_evidence']
        self.assertFalse(cost.exception_applies(exception, case, 'processes', 'a'*40, now=now))

    def test_duplicate_allowance_is_exact_and_does_not_follow_changes_or_growth(self):
        source = 'class T:\n def test_a(self): return 1\n def test_a(self): return 2\n'
        census = cost.static_census({'test_example.py': source}, 'a' * 40)
        self.assertEqual(len(cost.duplicate_findings(census, {})), 1)
        entry = {**census['duplicates'][0], 'owner': 'team', 'reason': 'Later relevance audit',
                 'revisit': 'Audit this fault boundary'}
        enrolled = {'duplicates': [entry]}
        self.assertEqual(cost.duplicate_findings(census, enrolled), [])
        for changed in (source.replace('return 1', 'return 3'), source + ' def test_a(self): return 2\n'):
            self.assertTrue(cost.duplicate_findings(cost.static_census({'test_example.py': changed}, 'b'*40), enrolled))
        self.assertTrue(cost.duplicate_findings(cost.static_census({'test_moved.py': source}, 'b'*40), enrolled))
        self.assertEqual(cost.duplicate_findings(cost.static_census({'test_example.py': '\n' + source}, 'b'*40), enrolled), [])

    def test_existing_overwritten_methods_are_enrolled_without_enrolling_new_duplicates(self):
        path = cost.ROOT / '.engine/tools/test_build_coordinator.py'
        census = cost.static_census({'.engine/tools/test_build_coordinator.py': path.read_text()}, 'a'*40)
        known = [x for x in census['duplicates'] if x['qualified_name'].startswith('TestEvidenceDurability.')]
        self.assertEqual(len(known), 2)
        enrollment = json.loads((cost.ROOT / '.engine/policies/test-cost-legacy-static.json').read_text())
        self.assertEqual(cost.duplicate_findings(census, enrollment), [])

    def test_new_case_cannot_borrow_old_identity_and_changed_source_needs_contract(self):
        census = cost.static_census({'test_example.py': 'class T:\n def test_a(self): pass\n'}, 'a'*40)
        record = {'id': 'test_example.T.test_a', 'occurrence': 1, 'path': 'test_example.py',
                  'qualified_name': 'T.test_a', 'contract': None}
        old = {'cases': [{**record, 'source_digest': census['definitions'][0]['ast_digest']}]}
        self.assertEqual(cost.inventory_findings([record], census, old), [])
        for change in ({'id': 'test_example.T.test_b'}, {'occurrence': 2}):
            changed = {**record, **change}
            self.assertTrue(cost.inventory_findings([changed], census, old))
        changed = cost.static_census({'test_example.py': 'class T:\n def test_a(self): return 3\n'}, 'b'*40)
        self.assertTrue(cost.inventory_findings([record], changed, old))
        record['contract'] = CONTRACT
        self.assertEqual(cost.inventory_findings([record], changed, old), [])

    def test_generated_runtime_id_requires_an_explicit_source_mapping(self):
        census = cost.static_census({'test_example.py': 'class T:\n def test_a(self): pass\n'}, 'a'*40)
        record = {'id': 'generated[1]', 'occurrence': 1, 'path': None, 'qualified_name': None,
                  'contract': CONTRACT}
        self.assertTrue(cost.inventory_findings([record], census, {}))
        mapping = {'target': {'id': 'generated[1]', 'occurrence': 1}, 'path': 'test_example.py',
                   'qualified_name': 'T.test_a', 'reason': 'One generated input',
                   'supported_fault': 'Input handling'}
        self.assertEqual(cost.inventory_findings([record], census, {}, [mapping]), [])
        self.assertTrue(cost.inventory_findings([], census, {}, [mapping]))

    def test_inherited_methods_map_to_the_defining_function_and_contracts_are_fresh(self):
        class Base(unittest.TestCase):
            @cost.declaration(CONTRACT)
            def test_value(self): pass
        class Child(Base): pass
        instance = Child('test_value')
        record = cost.runtime_inventory([instance])[0]
        self.assertIn('Base.test_value', record['qualified_name'])
        contract = cost.declared_contract(instance)
        contract['dependencies'].append('mutated')
        self.assertNotIn('mutated', cost.declared_contract(instance)['dependencies'])

    def test_invalid_pure_declaration_is_reported_instead_of_crashing(self):
        census = cost.static_census({'test_example.py': 'class T:\n def test_a(self): pass\n'}, 'a'*40)
        contract = copy.deepcopy(CONTRACT)
        contract['limits']['processes'] = 1
        record = {'id': 'case', 'occurrence': 1, 'path': 'test_example.py',
                  'qualified_name': 'T.test_a', 'contract': contract}
        self.assertTrue(any('invalid declaration' in x for x in cost.inventory_findings([record], census, {})))


@cost.declaration({**CONTRACT, 'boundary': 'process',
                   'boundary_rationale': 'Exercise actual audited child launches and the serial launcher',
                   'fixture_owner': 'test_selftest_cost.TestResourceObservation',
                   'mutable_state': 'Temporary directories and recorder hooks restored after each case',
                   'limits': {**cost.zeros(), 'processes': 20, 'git_commands': 12,
                              'schema_decodes': 100, 'metaschema_validations': 10,
                              'whole_tree_fixtures': 2, 'nested_journeys': 10}})
class TestResourceObservation(unittest.TestCase):
    def test_fresh_and_corrupt_installations_offer_bootstrap_without_cost_clearance(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            policies = Path(directory) / '.engine/policies'
            policies.mkdir(parents=True)
            for corrupt in (False, True):
                if corrupt:
                    (policies / 'test-cost-activation.json').write_text('{broken')
                    (policies / 'test-cost-legacy-baseline.json').write_text('{}')
                state = cost.enrollment_context(root=directory, environment_digest=cost.digest({}))
                self.assertEqual(state['mode'], 'measurement-bootstrap')
                self.assertFalse(state['cost_clearance'])
                self.assertIn('existing correctness checks', state['required'])
                self.assertIn('explicit enrollment review', state['required'])

    def test_direct_tree_copy_alias_cannot_bypass_the_fixture_counter(self):
        from shutil import copytree
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / '.engine'
            (source / 'nested').mkdir(parents=True)
            (source / 'nested' / 'data').write_text('bounded fixture')
            with cost.Recorder() as observer:
                copytree(str(source), str(Path(directory) / 'copy'))
            self.assertEqual(observer.owners['unattributed']['whole_tree_fixtures'], 1)

    def test_direct_decoder_and_background_work_cannot_disappear_or_borrow_main_owner(self):
        import threading
        decoder = json.JSONDecoder()
        decode = decoder.decode
        with cost.Recorder() as observer:
            observer.start_case({'id': 'main', 'occurrence': 1})
            decode('{"type": "object"}')
            thread = threading.Thread(target=lambda: decoder.raw_decode('{"type": "array"}'))
            thread.start()
            thread.join()
            observer.stop_case()
        record = observer.document(source={}, scope='full', complete=True, process_exit=0)
        self.assertEqual(record['totals']['schema_decodes'], 2)
        self.assertEqual(observer.owners['unattributed:thread']['schema_decodes'], 1)
        self.assertIn('background-thread resource ownership is unattributed', record['unknown'])

    def test_nested_journey_counts_once_and_keeps_work_owned_by_outer_case(self):
        import io
        class Inner(unittest.TestCase):
            def runTest(self):
                json.loads('{"work": 1}')
        with cost.Recorder() as observer:
            observer.start_case({'id': 'outer', 'occurrence': 1})
            result = unittest.TextTestRunner(stream=io.StringIO()).run(Inner())
            observer.stop_case()
        self.assertTrue(result.wasSuccessful())
        record = observer.document(source={}, scope='full', complete=True, process_exit=0)
        self.assertEqual(record['totals']['nested_journeys'], 1)
        self.assertEqual(record['totals']['schema_decodes'], 1)
        self.assertEqual(len(record['owners']), 1)

    def test_imported_aliases_count_real_work_and_restore_after_exception(self):
        from json import loads
        from jsonschema import Draft202012Validator as Validator
        import subprocess
        from subprocess import Popen
        import sys
        original = json.JSONDecoder.raw_decode
        prior_schema = vars(Validator)['check_schema']
        observer = cost.Recorder()
        with self.assertRaisesRegex(RuntimeError, 'stop'):
            with observer:
                observer.start_case({'id': 'case', 'occurrence': 1})
                loads('{"type":"object"}')
                Validator.check_schema({'type': 'object'})
                with Popen([sys.executable, '-c', 'pass'], stdout=subprocess.PIPE) as child:
                    child.communicate()
                with Popen([b'git', b'--version'], stdout=subprocess.PIPE) as child:
                    child.communicate()
                raise RuntimeError('stop')
        self.assertIs(json.JSONDecoder.raw_decode, original)
        self.assertIs(vars(Validator)['check_schema'], prior_schema)
        record = observer.document(source={}, scope='full', complete=True, process_exit=0)
        self.assertEqual(record['totals']['processes'], 2)
        self.assertEqual(record['totals']['git_commands'], 1)
        self.assertGreaterEqual(record['totals']['schema_decodes'], 1)
        self.assertEqual(record['totals']['metaschema_validations'], 1)
        self.assertIn('descendant work is not instrumented', record['unknown'])

    def test_canonical_fixture_seam_counts_one_clone_and_one_git_enumeration(self):
        import subprocess
        import tempfile
        import engine_fixture
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'repo'
            root.mkdir()
            (root / '.engine').mkdir()
            (root / '.engine' / 'fixture.txt').write_text('small tracked fixture')
            subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(root), 'add', '.engine/fixture.txt'], check=True, capture_output=True)
            with cost.Recorder() as observer:
                engine_fixture.clone_engine(str(root), str(Path(directory) / 'copy'))
            totals = observer.document(source={}, scope='full', complete=True, process_exit=0)['totals']
            self.assertEqual(totals['whole_tree_fixtures'], 1)
            self.assertEqual(totals['git_commands'], 1)
            self.assertEqual(totals['processes'], 1)

    def test_counter_bounds_report_unknown_instead_of_silently_wrapping(self):
        observer = cost.Recorder(max_owners=1, max_counter=2)
        observer.count('processes', 3)
        observer.owner = 'another'
        observer.count('processes')
        self.assertEqual(len(observer.unknown), 2)
        self.assertEqual(observer.owners['unattributed']['processes'], 2)
        observer.count('processes', -5)
        self.assertEqual(observer.owners['unattributed']['processes'], 2)
        self.assertIn('invalid resource counter event', observer.unknown)

    def test_real_launcher_keeps_outcomes_with_observation_on_and_off(self):
        import subprocess
        import sys
        import tempfile
        import selftest_results
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'test_small.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertEqual(2+2,4)\n @unittest.skip("known")\n def test_skip(self): pass\n')
            summaries = []
            for enabled in (False, True):
                results, observed = root / 'outcomes.json', root / 'costs.json'
                cmd = [sys.executable, str(cost.ROOT / '.engine/tools/selftest.py'), '--child',
                       '--start-dir', str(root), '--results-path', str(results)]
                if enabled: cmd += ['--cost-path', str(observed)]
                run = subprocess.run(cmd, capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                result = json.loads(results.read_text())
                summaries.append([(c['id'], c['outcome']) for c in result['cases']])
                if enabled:
                    resource = json.loads(observed.read_text())
                    selftest_results.validate_shape(resource, 'test-cost-run.v1')
                    self.assertTrue(resource['complete'])
                    self.assertEqual(resource['process_exit'], 0)
            self.assertEqual(*summaries)


def baseline_example():
    census = cost.static_census({'test_example.py': 'class T:\n def test_a(self): pass\n'}, 'a'*40)
    case = {'id': 'test_example.T.test_a', 'occurrence': 1}
    runtime = [{**case, 'path': 'test_example.py', 'qualified_name': 'T.test_a',
                'contract': None, 'family': None, 'input_size': None}]
    identity = {'source_commit': 'a'*40, 'base_commit': 'a'*40, 'observer_commit': 'b'*40,
                'observer_digest': cost.digest({}), 'plan_digest': cost.digest({}), 'contract_digest': cost.digest({}),
                'policy_digest': cost.digest({}), 'inventory_digest': cost.digest([case]),
                'environment_digest': cost.digest({'test': True}), 'cache_state': 'unknown',
                'topology': 'serial', 'stage': 'bootstrap', 'attempt': 'one', 'node': None,
                'artifact_digest': cost.digest({'tree': 'c'*40})}
    owner = 'case:' + json.dumps(case, sort_keys=True, separators=(',', ':'))
    raw = {'schema_version': 'test-cost-run.v1', 'source': {'tree': 'c'*40, 'worktree_dirty': False},
           'scope': 'full', 'complete': True, 'process_exit': 0, 'unknown': [],
           'totals': cost.zeros(), 'owners': [{'owner': owner, 'counts': cost.zeros()}], 'inventory': runtime}
    observation = cost.normalize_run(raw, identity, expected_tree='c'*40)
    return raw, observation, census, runtime


@cost.declaration(CONTRACT)
class TestBaselineEnrollment(unittest.TestCase):
    def test_volatile_parity_rules_are_source_bound_and_preserve_id_relationships(self):
        import types
        import selftest_results
        raw, _, census, runtime = baseline_example()
        case = types.SimpleNamespace(id=lambda: runtime[0]['id'])
        def report(first, second, repeated):
            observed = selftest_results.Observation([case], [case], source=raw['source'], scope='full',
                         invocation={'start_dir': 'fixture', 'pattern': 'test_*.py', 'selection_digest': None})
            observed.start(case)
            for label in (f'targets=[{first},{second}]', f'target={repeated}; generation=1'):
                observed.subtest(case, types.SimpleNamespace(id=lambda label=label: label), None)
            observed.outcome(case, 'passed')
            observed.stop(case)
            return observed.document(finalized=True)
        a, b = 'a'*12+'4'+'a'*3+'8'+'a'*15, 'b'*12+'4'+'b'*3+'9'+'b'*15
        x, y = 'c'*12+'4'+'c'*3+'a'+'c'*15, 'd'*12+'4'+'d'*3+'b'+'d'*15
        native, observed = report(a, b, a), report(x, y, x)
        rule = {'case': {k: runtime[0][k] for k in ('id', 'occurrence')},
                'path': 'test_example.py', 'qualified_name': 'T.test_a',
                'source_tree': 'c'*40, 'source_digest': census['definitions'][0]['ast_digest'],
                'mode': 'opaque-uuid4', 'owner': 'team', 'reason': 'Explicit random fixture IDs'}
        with self.assertRaises(ValueError):
            cost.require_outcome_parity(native, observed)
        cost.require_outcome_parity(native, observed, normalizations=[rule], census=census, runtime=runtime)
        for changed, rules in ((report(x, y, y), [rule]),
                               (observed, [{**rule, 'source_tree': 'd'*40}])):
            with self.assertRaises(ValueError):
                cost.require_outcome_parity(native, changed, normalizations=rules, census=census, runtime=runtime)
        observed['cases'][0]['subtests'][1]['id'] = observed['cases'][0]['subtests'][1]['id'].replace('generation=1', 'generation=2')
        with self.assertRaises(ValueError):
            cost.require_outcome_parity(native, observed, normalizations=[rule], census=census, runtime=runtime)

    def test_resource_exit_cannot_override_failed_or_incomplete_outcomes(self):
        import types
        import selftest_results
        raw, observation, _, runtime = baseline_example()
        case = types.SimpleNamespace(id=lambda: runtime[0]['id'])
        for outcome in ('passed', 'failed', None):
            report = selftest_results.Observation([case], [case], source=raw['source'], scope='full',
                         invocation={'start_dir': 'fixture', 'pattern': 'test_*.py', 'selection_digest': None})
            if outcome:
                report.start(case)
                report.outcome(case, outcome)
                report.stop(case)
            normalized = cost.normalize_run(raw, observation['identity'], expected_tree='c'*40,
                                            outcomes=report.document(finalized=True))
            self.assertEqual(normalized['complete'], outcome == 'passed')

    def test_prospective_declarations_do_not_expand_legacy_or_follow_changed_source(self):
        _, observation, census, runtime = baseline_example()
        row = {**runtime[0], 'source_digest': census['definitions'][0]['ast_digest'], 'contract': CONTRACT}
        self.assertEqual(cost.inventory_findings(runtime, census, {}, declarations=[row]), [])
        changed = cost.static_census({'test_example.py': 'class T:\n def test_a(self): return 2\n'}, 'd'*40)
        self.assertTrue(any('stale prospective' in f for f in cost.inventory_findings(runtime, changed, {}, declarations=[row])))
        self.assertTrue(cost.inventory_findings(runtime, census, {}))
        enrolled = cost.enroll_baseline(observation, census, runtime, declarations=[row],
                                       owner='team', reason='Explicit activation', revisit='Review')
        self.assertEqual(enrolled['cases'], [])
        self.assertIsNone(cost.enrolled_observation(enrolled))

    def test_compact_enrollment_refuses_changed_digest_expansion_and_trailing_data(self):
        import base64
        raw, _, _, _ = baseline_example()
        packed = cost.pack_enrollment(raw)
        self.assertEqual(cost.unpack_enrollment(packed), raw)
        for bad in ({**packed, 'expanded_bytes': 1}, {**packed, 'document_digest': cost.digest({})},
                    {**packed, 'data': base64.b64encode(base64.b64decode(packed['data']) + b'trailing').decode()}):
            with self.assertRaises(ValueError):
                cost.unpack_enrollment(bad)

    def test_adapter_parity_ignores_addresses_but_retains_subtest_identity_and_outcome(self):
        import types
        import selftest_results
        case = unittest.FunctionTestCase(lambda: None)
        observation = selftest_results.Observation([case], [case], source={'tree': None, 'worktree_dirty': None},
                        scope='full', invocation={'start_dir': 'fixture', 'pattern': 'test_*.py', 'selection_digest': None})
        observation.start(case)
        observation.subtest(case, types.SimpleNamespace(id=lambda: 'input=<function sample at 0x123>'), None)
        observation.outcome(case, 'passed')
        observation.stop(case)
        original = observation.document(finalized=True)
        changed = copy.deepcopy(original)
        changed['cases'][0]['subtests'][0]['id'] = 'input=<function sample at 0x456>'
        cost.require_outcome_parity(original, changed)
        changed['cases'][0]['subtests'][0]['id'] = 'a different input'
        with self.assertRaises(ValueError):
            cost.require_outcome_parity(original, changed)

    def test_bootstrap_activation_preserves_source_and_observer_identities(self):
        raw, observation, census, runtime = baseline_example()
        baseline = cost.enroll_baseline(observation, census, runtime, owner='team', reason='Initial debt', revisit='Audit')
        self.assertEqual(baseline['source_commit'], 'a'*40)
        self.assertEqual(baseline['identity']['observer_commit'], 'b'*40)
        self.assertEqual(cost.enrolled_observation(baseline), observation)
        status = cost.baseline_status(baseline, expected_digest=cost.digest(baseline), observer_commit='b'*40,
                                      observer_digest=observation['identity']['observer_digest'], environment_digest=observation['identity']['environment_digest'])
        self.assertEqual(status['mode'], 'enforced')
        self.assertFalse(status['cost_clearance'])  # Activation alone is never candidate evidence.
        self.assertEqual(cost.inventory_findings(runtime, census, baseline), [])

    def test_missing_corrupt_and_incompatible_baseline_never_grants_clearance(self):
        _, observation, census, runtime = baseline_example()
        baseline = cost.enroll_baseline(observation, census, runtime, owner='team', reason='Initial debt', revisit='Audit')
        for candidate, observer in ((None, 'b'*40), ({}, 'b'*40), (baseline, 'd'*40)):
            status = cost.baseline_status(candidate, expected_digest=cost.digest(baseline), observer_commit=observer,
                                          observer_digest=observation['identity']['observer_digest'], environment_digest=observation['identity']['environment_digest'])
            self.assertEqual(status['mode'], 'measurement-bootstrap')
            self.assertFalse(status['cost_clearance'])
        changed = copy.deepcopy(baseline)
        changed['cases'][0]['limits']['processes'] = 100
        self.assertEqual(cost.baseline_status(changed, expected_digest=cost.digest(baseline), observer_commit='b'*40,
                         observer_digest=observation['identity']['observer_digest'], environment_digest=observation['identity']['environment_digest'])['mode'], 'measurement-bootstrap')

    def test_incomplete_wrong_source_and_new_cases_cannot_be_enrolled(self):
        _, observation, census, runtime = baseline_example()
        for changed in ({**observation, 'complete': False},
                        {**observation, 'identity': {**observation['identity'], 'source_commit': 'd'*40}},
                        {**observation, 'identity': {**observation['identity'], 'stage': 'candidate'}}):
            with self.assertRaises(ValueError):
                cost.enroll_baseline(changed, census, runtime, owner='team', reason='Debt', revisit='Audit')
        runtime[0]['qualified_name'] = 'T.test_new'
        with self.assertRaises(ValueError):
            cost.enroll_baseline(observation, census, runtime, owner='team', reason='Debt', revisit='Audit')

    def test_normalization_refuses_forged_tree_and_inventory(self):
        raw, observation, _, _ = baseline_example()
        with self.assertRaises(ValueError):
            cost.normalize_run(raw, observation['identity'], expected_tree='d'*40)
        raw['inventory'][0]['id'] = 'forged'
        with self.assertRaises(ValueError):
            cost.normalize_run(raw, observation['identity'], expected_tree='c'*40)

    def test_normalization_refuses_double_owned_and_inconsistent_counts(self):
        raw, observation, _, _ = baseline_example()
        raw['owners'].append(copy.deepcopy(raw['owners'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate owners'):
            cost.normalize_run(raw, observation['identity'], expected_tree='c'*40)
        raw['owners'].pop()
        raw['totals']['processes'] = 1
        with self.assertRaisesRegex(ValueError, 'totals disagree'):
            cost.normalize_run(raw, observation['identity'], expected_tree='c'*40)


@cost.declaration({**CONTRACT, 'boundary': 'process',
    'supported_fault': 'Required cost enforcement or new test discovery silently breaks',
    'boundary_rationale': 'Exercise the actual required-check controls and bounded child work',
    'fixture_owner': 'test_selftest_cost.TestRequiredCostCheck',
    'dependencies': ['selftest_cost_check', 'demo_test_cost_contracts', 'yaml', 'engine_fixture'],
    'data_reads': ['.github/workflows/engine-ci.yml', '.engine/policies/test-cost.json',
                   '.engine/templates/test-authoring.py', '.engine/tools/selftest_cost.py'],
    'mutable_state': 'One temporary workflow file; recorder hooks restored by each control',
    'limits': {**cost.zeros(), 'processes': 6, 'git_commands': 5,
               'schema_decodes': 250, 'metaschema_validations': 1,
               'whole_tree_fixtures': 1, 'nested_journeys': 1}})
class TestRequiredCostCheck(unittest.TestCase):
    def test_authoring_template_runs_standalone_with_a_bounded_sibling_closure(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / 'tools'
            tools.mkdir()
            template = (cost.ROOT / '.engine/templates/test-authoring.py').read_text()
            template += '\n    def test_declared_behavior(self):\n        self.assertEqual(2 + 2, 4)\n'
            (tools / 'test_generated.py').write_text(template)
            (tools / 'selftest_cost.py').write_text((cost.ROOT / '.engine/tools/selftest_cost.py').read_text())
            child = subprocess.run([sys.executable, '-m', 'unittest', 'tools.test_generated'],
                                   cwd=root, capture_output=True, text=True)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertIn('Ran 1 test', child.stderr)

    def test_real_negative_controls_reject_growth_and_accept_repaired_helpers(self):
        import demo_test_cost_contracts as demo
        for scenario in demo.SCENARIOS:
            with self.subTest(scenario=scenario):
                report = demo.demonstrate(scenario)
                self.assertTrue(report['violations'])
                self.assertEqual(report['repair_violations'], [])
                self.assertTrue(report['passed'])
                self.assertFalse(report['cost_clearance'])

    def test_workflow_cannot_drop_observation_permission_or_terminal_consumption(self):
        import tempfile
        import yaml
        import selftest_cost_check as check
        workflow = yaml.safe_load((cost.ROOT / '.github/workflows/engine-ci.yml').read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / '.github/workflows/engine-ci.yml'
            path.parent.mkdir(parents=True)
            path.write_text(yaml.safe_dump(workflow))
            self.assertEqual(check.check(root), [])
            mutations = [
                ('selftests', 'run', "python tools/selftest.py --pattern 'test_*.py'"),
                ('cost', 'continue-on-error', True),
                ('cost', 'if', 'false'),
                ('cost', 'env', {}),
                ('cost', 'env', {'PATH': './attacker-bin:$PATH'}),
                ('Write the receipt', 'if', "github.event_name == 'pull_request'"),
                ('Write the receipt', 'run', 'python tools/ci_gatekeeper.py emit-receipt'),
                ('Refuse a run in which no arm did any work', 'if', 'false'),
                ('Refuse a run in which no arm did any work', 'shell', "bash -c 'true # {0}'"),
                ('Refuse a run in which no arm did any work', 'run', 'python tools/ci_gatekeeper.py assert-ran'),
                ('Upload the receipt', 'if', 'false'),
            ]
            for name, field, value in mutations:
                with self.subTest(step=name, field=field):
                    changed = copy.deepcopy(workflow)
                    step = next(s for s in changed['jobs']['engine-ci']['steps'] if s.get('id', s.get('name')) == name)
                    step[field] = value
                    path.write_text(yaml.safe_dump(changed))
                    self.assertTrue(check.check(root))
            changed = copy.deepcopy(workflow)
            for step in changed['jobs']['engine-ci']['steps']:
                if step.get('id') == 'cost' or step.get('name') in ('Write the receipt', 'Refuse a run in which no arm did any work'):
                    step['run'] += '\ntrue'
                    step['shell'] = 'bash {0}'
                if step.get('name') == 'Upload the receipt':
                    step['if'] = 'false'
            path.write_text(yaml.safe_dump(changed))
            self.assertTrue(check.check(root))


if __name__ == '__main__':
    unittest.main()
