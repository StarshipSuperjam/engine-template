"""Regression tests for assessment, independent assignment and issue-owned recovery."""
import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import issue_triage as triage
import release_impact


def assessed(impact='patch'):
    return {'state':'assessed', 'impact':impact, 'remedy':'Restore the existing behavior.',
            'rationale':'The public contract is unchanged.', 'evidence':['test regression and verified remedy']}


def record(assessment=None):
    return triage.new_record(assessment or assessed(), 'test-operation-1', {'failure':'existing behavior'},
                             now='2026-09-10T00:00:00Z')


class Contract(unittest.TestCase):
    def test_explicit_assessment_union_and_canonical_impacts(self):
        for impact in release_impact.RELEASE_IMPACTS:
            triage.validate(assessed(impact), 'assessment')
        for value in ({}, {'state':'pending'}, assessed('Fix'), {**assessed(), 'rationale':' '},
                      {**assessed(), 'evidence':[]}, {**assessed(), 'unknown':'mixed branches'}):
            with self.assertRaises(triage.TriageError): triage.validate(value, 'assessment')
        triage.pending('remedy unknown', 'Inspect the failing assertion.')

    def test_marker_injection_round_trips_without_creating_another_record(self):
        a=assessed();a['rationale']='Evidence includes --> '+triage.START+' <script>x</script>'
        r=record(a);body=triage.with_record('source body\n<!-- final-source-marker -->\n', r)
        self.assertEqual(triage.parse(body),r)
        self.assertEqual(body.count(triage.START),1)
        self.assertTrue(body.endswith('<!-- final-source-marker -->\n'))
        self.assertNotIn('<script>',body)

    def test_missing_duplicate_and_broken_records_do_not_clear_pending(self):
        r=record();section=triage.render(r)
        for body in (section+section,section.replace(triage.END,''),section.replace('"revision":1','"revision":0')):
            with self.assertRaises(triage.TriageError): triage.parse(body)
        self.assertTrue(triage.outstanding(None))
        self.assertTrue(triage.outstanding({}))

    def test_assignment_and_assessment_are_independent(self):
        r=record();self.assertTrue(triage.outstanding(r))
        for state in triage.TERMINAL_ASSIGNMENTS:
            r['assignment']={'state':state,'reason':'Observed disposition.'}
            self.assertFalse(triage.outstanding(r))
            p=copy.deepcopy(r);p['assessment']=triage.pending('Unknown remedy','Inspect the failure.')
            self.assertTrue(triage.outstanding(p))

    def test_refresh_preserves_unchanged_and_invalidates_changed_evidence(self):
        r=record();r['assignment']={'state':'assigned','reason':'Confirmed','milestone':16}
        unchanged=triage.refresh(r,triage.pending('Unknown','Investigate'),{'failure':'existing behavior'})
        self.assertEqual(unchanged,r)
        changed=triage.refresh(r,triage.pending('New failure','Inspect new failure'),{'failure':'new behavior'})
        self.assertEqual(changed['assessment']['state'],'pending')
        self.assertEqual(changed['superseded']['assessment'],r['assessment'])
        self.assertEqual(changed['assignment']['milestone'],16)
        self.assertTrue(triage.outstanding(changed))

    def test_replacing_owned_section_preserves_surrounding_text(self):
        r=record();original='Human prefix\n'+triage.render(r)+'\nHuman suffix\n<!-- source -->\n'
        r['revision']=2
        result=triage.with_record(original,r)
        self.assertTrue(result.startswith('Human prefix\n'))
        self.assertTrue(result.endswith('\nHuman suffix\n<!-- source -->\n'))
        self.assertEqual(triage.parse(result)['revision'],2)

if __name__=='__main__': unittest.main()
