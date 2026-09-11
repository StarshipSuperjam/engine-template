"""Semantic obligations survive editorial edits; changed mandates cannot borrow that credit."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reviewer_contracts as contracts
import build_coordinator_core as core
import agent_coherence_check
import codex_gen

ROOT = Path(__file__).resolve().parents[2]


class ReviewContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for rel in ('.claude/agents', '.engine/schemas', '.engine/policies'):
            shutil.copytree(ROOT / rel, self.root / rel)
        self.ref = {'plan_id': 'pln_0123456789ab', 'revision': 1, 'plan_digest': 'sha256:'+'a'*64}
        self.path = self.root / '.claude/agents/engine-design-review-architecture.md'

    def envelope(self):
        return contracts.capture(self.root, self.ref, 'thorough', ['architecture'],
                                 ['spec-conformance'], instructions='Read all approved obligations.')

    def test_editorial_change_retains_obligation_and_original_source(self):
        old = self.envelope()
        original = copy.deepcopy(old)
        self.path.write_text(self.path.read_text()+'\nEditorial explanatory note.\n')
        delta = contracts.drift(old, self.root)
        self.assertEqual([], delta['changed'])
        self.assertEqual(['architecture'], [x['lens'] for x in delta['editorial']])
        self.assertEqual(original, old)
        new = self.envelope()
        self.assertEqual(old['panels']['plan-review'][0]['semantic_digest'],
                         new['panels']['plan-review'][0]['semantic_digest'])
        self.assertNotEqual(old['digest'], new['digest'])

    def test_mandate_change_is_lens_scoped(self):
        old = self.envelope()
        self.path.write_text(self.path.read_text().replace('reviewer-contract-version: 1','reviewer-contract-version: 2'))
        self.assertEqual(['architecture'], [x['lens'] for x in contracts.drift(old,self.root)['changed']])

    def test_structured_change_does_not_depend_on_version_bump(self):
        old = self.envelope()
        self.path.write_text(self.path.read_text().replace('disallowedTools: [Edit, Write, NotebookEdit, Bash]',
                                                          'disallowedTools: [Edit, Write, NotebookEdit]'))
        self.assertEqual(1,len(contracts.drift(old,self.root)['changed']))

    def test_removed_lens_never_shrinks_stored_roster(self):
        old = self.envelope();self.path.unlink()
        self.assertIsNone(contracts.drift(old,self.root)['changed'][0]['new'])
        self.assertEqual('architecture',contracts.panel(old,'plan-review')[0]['lens'])
        with self.assertRaises(contracts.ContractError): self.envelope()

    def test_header_and_source_tampering_fail(self):
        old=self.envelope()
        for key,value in [('depth','quick'),('instructions','Skip the review')]:
            tampered=copy.deepcopy(old);tampered[key]=value
            with self.assertRaises(contracts.ContractError): contracts.validate(tampered)
        tampered=copy.deepcopy(old)
        tampered['panels']['plan-review'][0]['source']['instructions']+='tamper'
        tampered['digest']=contracts.envelope_digest(tampered)
        with self.assertRaises(contracts.ContractError): contracts.validate(tampered)

    def test_unused_effort_change_preserves_model_only_semantics(self):
        old=self.envelope();p=self.root/'.engine/policies/model-bindings.json'
        b=json.loads(p.read_text());b['tiers']['judgment']['effort']='ultra'
        b['providers']['codex']['tiers']['judgment']['effort']='max';p.write_text(json.dumps(b))
        self.assertEqual([],contracts.drift(old,self.root)['changed'])
        item=self.envelope()['panels']['plan-review'][0]
        self.assertEqual({'mode':'harness-controlled','floor':None},item['semantic']['effort_policy'])
        for model in item['semantic']['bindings'].values(): self.assertNotIn('effort',model)
        rendered=codex_gen.render_agent(str(self.path),str(self.root))
        self.assertNotIn('model_reasoning_effort',rendered)
        self.assertIn('Reviewer mandate:',rendered)

    def test_model_change_is_semantic(self):
        old=self.envelope();p=self.root/'.engine/policies/model-bindings.json'
        b=json.loads(p.read_text());b['providers']['codex']['tiers']['judgment']['model']='different-model'
        p.write_text(json.dumps(b))
        self.assertIn('architecture',[x['lens'] for x in contracts.drift(old,self.root)['changed']])

    def test_duplicate_or_missing_identity_refused(self):
        fields=contracts.frontmatter(self.path.read_text())
        self.assertEqual([],agent_coherence_check.reviewer_identity_findings([fields]))
        self.assertTrue(agent_coherence_check.reviewer_identity_findings([fields,fields]))
        fields.pop('reviewer-contract')
        self.assertTrue(agent_coherence_check.reviewer_identity_findings([fields]))
        self.path.write_text(self.path.read_text().replace('reviewer-contract-version: 1','reviewer-contract-version: true'))
        with self.assertRaises(contracts.ContractError): self.envelope()

    def test_capture_detects_concurrent_installation_change(self):
        actual=contracts.discover
        calls=0
        def changing(root,role=None):
            nonlocal calls
            calls+=1
            if calls==2:self.path.write_text(self.path.read_text()+'\nChanged concurrently.\n')
            return actual(root,role)
        with patch.object(contracts,'discover',side_effect=changing):
            with self.assertRaisesRegex(contracts.ContractError,'changed during approval'): self.envelope()

    def test_canonical_replay_and_bound_schema_integrity(self):
        old=self.envelope();self.assertEqual(old,self.envelope())
        item=old['panels']['plan-review'][0]
        item['semantic']['result_contract']['schema']={'type':'null'}
        item['semantic_digest']=core.digest(item['semantic']);old['digest']=contracts.envelope_digest(old)
        with self.assertRaisesRegex(contracts.ContractError,'result schema'):contracts.validate(old)


if __name__ == '__main__': unittest.main()
