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


if __name__ == '__main__':
    unittest.main()
