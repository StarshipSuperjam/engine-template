"""Bounded fault controls for declaration and source/runtime identity accounting."""
import copy
import json
from pathlib import Path
import unittest

import selftest_cost as cost

CONTRACT = {
    'schema_version': 'test-cost-contract.v1', 'supported_fault': 'Silent test loss or unenrolled cost',
    'boundary': 'pure', 'boundary_rationale': 'Source and inventory decisions have no process boundary',
    'fixture_owner': 'Each case owns fresh dictionaries', 'dependencies': ['jsonschema'],
    'data_reads': ['.engine/schemas/test-cost-contract.v1.json'], 'cadence': 'pr',
    'limits': {**cost.zeros(), 'schema_decodes': 10}, 'mutable_state': 'Fresh input per case',
    'cache_lifetime': 'module', 'added_cost_risk': 'Bounded AST and dictionary checks', 'families': [],
}


@cost.declaration(CONTRACT)
class TestInventory(unittest.TestCase):
    def test_budget_growth_and_exception_expiry_cannot_reuse_old_permission(self):
        from selftest_support import TestClock
        counts = {**cost.zeros(), 'processes': 3}
        self.assertTrue(cost.budget_findings(counts, {**cost.zeros(), 'processes': 2}))
        case = {'id': 'case', 'occurrence': 1}
        exception = {'id': 'debt', 'owner': 'team', 'reason': 'Repair pending',
                     'revisit': 'Fixture repair', 'issued_at': '2026-09-13T00:00:00Z',
                     'expires_at': '2026-09-14T00:00:00Z', 'case': case, 'resource': 'processes',
                     'ceiling': 3, 'source_commit': 'a'*40}
        clock = TestClock('2026-09-13T23:59:59Z')
        self.assertTrue(cost.exception_applies(exception, case, 'processes', 'a'*40, now=clock.now))
        clock.advance(1)
        self.assertFalse(cost.exception_applies(exception, case, 'processes', 'a'*40, now=clock.now))
        exception['expires_at'] = '2027-09-14T00:00:00Z'
        self.assertFalse(cost.exception_applies(exception, case, 'processes', 'a'*40, now=clock.now))
        exception['expires_at'] = '2026-09-15T00:00:00Z'
        exception['revisit'] = ''
        self.assertFalse(cost.exception_applies(exception, case, 'processes', 'a'*40, now=clock.now))

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
                   'limits': {**cost.zeros(), 'processes': 20, 'git_commands': 12,
                              'schema_decodes': 100, 'metaschema_validations': 10,
                              'whole_tree_fixtures': 2, 'nested_journeys': 10}})
class TestResourceObservation(unittest.TestCase):
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
        original = json.JSONDecoder.decode
        prior_schema = vars(Validator)['check_schema']
        observer = cost.Recorder()
        with self.assertRaisesRegex(RuntimeError, 'stop'):
            with observer:
                observer.start_case({'id': 'case', 'occurrence': 1})
                loads('{"type":"object"}')
                Validator.check_schema({'type': 'object'})
                with Popen([sys.executable, '-c', 'pass'], stdout=subprocess.PIPE) as child:
                    child.communicate()
                raise RuntimeError('stop')
        self.assertIs(json.JSONDecoder.decode, original)
        self.assertIs(vars(Validator)['check_schema'], prior_schema)
        record = observer.document(source={}, scope='full', complete=True, process_exit=0)
        self.assertEqual(record['totals']['processes'], 1)
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


if __name__ == '__main__':
    unittest.main()
