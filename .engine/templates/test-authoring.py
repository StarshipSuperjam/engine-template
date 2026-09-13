"""Start at the cheapest boundary that can actually expose the supported fault.

Replace the concrete contract fields and bounded resource ceilings before admission.
Use a real disposable process when process/config discovery is the fault under test.
"""
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from selftest_cost import declaration, zeros


@declaration({
    'schema_version': 'test-cost-contract.v1',
    'supported_fault': 'Name the supported behavior that a defect would break',
    'boundary': 'pure', 'boundary_rationale': 'The behavior is a pure decision',
    'fixture_owner': 'This test class', 'dependencies': [], 'data_reads': [],
    'cadence': 'pr', 'limits': zeros(), 'mutable_state': 'Fresh input per case',
    'cache_lifetime': 'none', 'added_cost_risk': 'One bounded in-memory decision', 'families': [],
})
class TestDecision(unittest.TestCase):
    pass  # Add a fault-exposing assertion, not an implementation mirror.
