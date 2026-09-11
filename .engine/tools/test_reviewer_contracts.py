"""Semantic obligations survive editorial edits; changed mandates cannot borrow that credit."""
import copy
import json
import shutil
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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

    def test_retention_observation_ignores_editorial_path_reordering(self):
        before=contracts.installation_digest(self.root)
        self.path.rename(self.path.with_name('z-architecture.md'))
        self.assertEqual(before,contracts.installation_digest(self.root))

    def test_build_model_renewal_preserves_the_plan_panels_original_policy(self):
        original = self.envelope()
        state = {"review_contract": original, "review_contract_format": 1}
        path = self.root / '.engine/policies/model-bindings.json'
        policy = json.loads(path.read_text())
        policy['providers']['codex']['tiers']['judgment']['model'] = 'new-review-model'
        policy['providers']['codex']['overrides']['engine-qa-review-spec-conformance']['model'] = 'new-review-model'
        path.write_text(json.dumps(policy))
        proposed = contracts.propose_build(state, self.root, ['spec-conformance'])
        old_plan = original['panels']['plan-review'][0]
        retained = proposed['panels']['plan-review'][0]
        self.assertEqual(old_plan['semantic'], retained['semantic'])
        self.assertEqual(original['binding_policy'], retained['source']['binding_policy'])
        self.assertEqual('new-review-model', proposed['panels']['pre-submission-review'][0]['semantic']['bindings']['codex']['model'])
        self.assertEqual(original, state['review_contract'])
        contracts.validate(proposed)
        # A second renewal still retains the first plan review's actual policy.
        state['review_contract'] = proposed
        policy['providers']['codex']['tiers']['judgment']['model'] = 'third-review-model'
        policy['providers']['codex']['overrides']['engine-qa-review-spec-conformance']['model'] = 'third-review-model'
        path.write_text(json.dumps(policy))
        next_contract = contracts.propose_build(state, self.root, ['spec-conformance'])
        self.assertEqual(original['binding_policy'], next_contract['panels']['plan-review'][0]['source']['binding_policy'])
        tampered = copy.deepcopy(next_contract)
        tampered['panels']['plan-review'][0]['source']['binding_policy'] = policy
        tampered['digest'] = contracts.envelope_digest(tampered)
        with self.assertRaisesRegex(contracts.ContractError, 'binding policy'):
            contracts.validate(tampered)

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




class HistoricalContracts(unittest.TestCase):
    """Synthetic source transitions use real git objects, never dates or fake ancestry answers."""
    @classmethod
    def setUpClass(cls):
        import subprocess
        cls.source_temp = tempfile.TemporaryDirectory()
        cls.source_root = Path(cls.source_temp.name)
        def git(*args):
            return subprocess.run(['git', '-C', str(cls.source_root), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        cls.git = staticmethod(git)
        git('init', '-q', '-b', 'main'); git('config', 'user.email', 'fixture@example.test'); git('config', 'user.name', 'Fixture')
        for rel in ('.claude/agents', '.engine/schemas', '.engine/policies'):
            shutil.copytree(ROOT / rel, cls.source_root / rel)
        shutil.copy(ROOT / '.engine/build-protocol.json', cls.source_root / '.engine/build-protocol.json')
        for path in (cls.source_root / '.claude/agents').glob('*.md'):
            path.write_text('\n'.join(line for line in path.read_text().split('\n')
                                      if not line.startswith(('reviewer-contract:', 'reviewer-contract-version:'))))
        import project_manager
        folder = cls.source_root / '.engine/tools'; folder.mkdir()
        (folder / 'project_manager.py').write_text('PLAN_REVIEW_LENSES = ' + repr(project_manager.PLAN_REVIEW_LENSES))
        git('add', '.');git('commit', '-qm', 'synthetic model-only pre-collector source')
        cls.legacy_commit = git('rev-parse', 'HEAD')
        (folder / 'scoped_agents.py').write_text('# synthetic collector capability marker\n')
        git('add', '.');git('commit', '-qm', 'synthetic native collector transition')
        cls.collector_commit = git('rev-parse', 'HEAD')
        (folder / 'result_contracts.py').write_text((ROOT / '.engine/tools/result_contracts.py').read_text())
        git('add', '.');git('commit', '-qm', 'synthetic result-binding transition')
        cls.observed_commit = git('rev-parse', 'HEAD')
        (folder / 'reviewer_contracts.py').write_text('# synthetic envelope capability marker\n')
        git('add', '.');git('commit', '-qm', 'synthetic envelope transition')
        cls.modern_commit = git('rev-parse', 'HEAD')

    @classmethod
    def tearDownClass(cls):
        cls.source_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(contracts, 'MODEL_ONLY_TRANSITION', self.legacy_commit).start()
        patch.object(contracts, 'HISTORICAL_TRANSITIONS', {'native-collector': self.collector_commit,
                                                       'result-binding': self.observed_commit}).start()

    def plan_fixture(self, *, observed=False, reviewed=True):
        import plan_store, plan_projection, project_manager
        from test_plan_store import _document
        self.commit = self.observed_commit if observed else self.legacy_commit
        doc = _document()
        doc['build_plan']['evidence'].append({'claim':'Synthetic retained original Engine source.',
            'basis':self.commit, 'kind':'observed'})
        self.library = plan_store.PlanLibrary(self.root / 'plans')
        self.slug = self.library.create(doc)
        def approve(record):
            record['approval'] = {'revision':1, 'plan_digest':core.digest(doc), 'depth':'thorough', 'at':'2026-09-01T00:00:00Z'}
        self.library.update_record(self.slug, approve)
        record = self.library.read_record(self.slug)
        body = plan_projection.render_plan(doc, record)
        digest = core.digest(body.encode())
        self.packet_text = (f"Plan review packet — {record['plan_id']} revision 1\n"
            f"Plan digest: {record['approval']['plan_digest']}\nPacket digest: {digest}\n"
            "Required lenses: architecture, feasibility, product-intent, risk-governance\n"
            "Depth: thorough — synthetic historical fixture\n" + '='*78 + '\n\n' + body)
        if observed:
            import result_contracts
            binding=result_contracts.resolve('plan-review-finding.v1',role='plan-review')
            self.packet_text=self.packet_text.replace('='*78+'\n\n','Result contract: '+json.dumps(binding,sort_keys=True)+'\n'+'='*78+'\n\n',1)
        self.packet = self.root / 'retained-plan-packet.md';self.packet.write_text(self.packet_text)
        if reviewed:
            receipt = {'at':'2026-09-01T00:00:00Z','revision':1,'plan_digest':core.digest(doc),
                       'packet_digest':digest,'lenses':['architecture','feasibility','product-intent','risk-governance'],'findings':[]}
            if observed:
                import scoped_agents
                from test_build_coordinator import observe_review_execution
                owner = scoped_agents.plan_owner(record)
                for lens in receipt['lenses']:
                    observe_review_execution(self.library,self.slug,owner,lens,digest,[],packet_content=self.packet_text)
                with plan_store.exclusive_lock_for(self.library, self.slug):
                    scoped_agents.accept_plan(self.library,self.slug,record,receipt,receipt['lenses'],'fixture-root')
            self.library.update_record(self.slug, lambda r:r.update(plan_review=receipt))
        self.record = self.library.read_record(self.slug)
        self.backup = self.root / 'retained-record.json';self.backup.write_text(json.dumps(self.record))
        self.locator = {'source_root':str(self.source_root),'source_commit':self.commit,
                        'backup':str(self.backup),'packets':[str(self.packet)] if reviewed else []}
        return self.record

    def preview(self):
        return contracts.adoption_preview(self.record,self.library,self.slug,self.locator,ROOT)

    def test_source_capabilities_use_real_named_transitions_and_refuse_modern_downgrade(self):
        self.assertEqual({'native-collector':False,'result-binding':False},contracts.source_capabilities(self.source_root,self.legacy_commit))
        self.assertEqual({'native-collector':True,'result-binding':True},contracts.source_capabilities(self.source_root,self.observed_commit))
        for commit in [self.collector_commit,self.modern_commit,'2000-01-01']:
            with self.subTest(commit=commit),self.assertRaises(contracts.ContractError):
                contracts.source_capabilities(self.source_root,commit)

    def test_historical_plan_adoption_is_append_only_and_never_fresh_execution(self):
        import scoped_agents, project_manager, plan_store
        self.plan_fixture();original=copy.deepcopy(self.record);preview=self.preview()
        contracts.apply_adoption(self.record,preview,reason='Accept the named unavailable historical facts.',at='2026-09-10T00:00:00Z',operator_decided=True)
        plan_store.validate_record(self.record)
        self.assertEqual(original,{k:v for k,v in self.record.items() if k!='review_contract_adoptions'})
        owner=scoped_agents.plan_owner(self.record)
        self.assertFalse(scoped_agents.Store(self.library,self.slug).receipt_verified(self.record['plan_review'],owner))
        self.assertTrue(contracts.historical_execution(self.record,self.record['plan_review'],owner))
        self.assertEqual(set(self.record['plan_review']['lenses']),project_manager.review_coverage(self.record))
        self.assertIn('historical-unverified',contracts.historical_disclosure(self.record))
        retry=copy.deepcopy(self.record)
        self.assertFalse(contracts.apply_adoption(self.record,preview,reason='Same explicit decision.',at='later',operator_decided=True))
        self.assertEqual(retry,self.record)
        replacement=copy.deepcopy(self.record['plan_review']);replacement['packet_digest']='sha256:'+'0'*64
        self.assertFalse(contracts.historical_execution(self.record,replacement,owner))

    def test_missing_packet_wrong_source_and_modified_backup_refuse_without_writes(self):
        self.plan_fixture();original=copy.deepcopy(self.record)
        for mutation in ('missing-packet','source-substitution','backup-substitution'):
            with self.subTest(mutation=mutation):
                locator=copy.deepcopy(self.locator)
                if mutation=='missing-packet':locator['packets']=[]
                elif mutation=='source-substitution':locator['source_commit']=self.observed_commit
                else:
                    backup=copy.deepcopy(self.record);backup['plan_review']['lenses']=[]
                    self.backup.write_text(json.dumps(backup))
                with self.assertRaises(contracts.ContractError):
                    contracts.adoption_preview(self.record,self.library,self.slug,locator,ROOT)
                self.assertEqual(original,self.record)

    def test_unapproved_uses_ordinary_approval_and_closed_history_is_unchanged(self):
        self.plan_fixture(reviewed=False)
        preview=self.preview();self.assertEqual([],preview['receipts'])
        self.assertEqual(['approval-envelope'],preview['gaps'])
        for change in ({'approval':None},{'closure':{'state':'complete'}}):
            record={**self.record,**change}
            with self.assertRaises(contracts.ContractError):
                contracts.adoption_preview(record,self.library,self.slug,self.locator,ROOT)

    def test_missing_modern_companion_does_not_enter_historical_cohort(self):
        self.plan_fixture(observed=True,reviewed=False)
        # Positive source provenance alone cannot establish that an observed execution happened.
        self.library.update_record(self.slug,lambda r:r.update(plan_review={
            'at':'2026-09-01T00:00:00Z','revision':1,'plan_digest':r['approval']['plan_digest'],
            'packet_digest':core.digest(self.packet_text.split('='*78+'\n\n',1)[1].encode()),
            'lenses':['architecture'],'findings':[]}))
        self.record=self.library.read_record(self.slug);self.backup.write_text(json.dumps(self.record))
        self.locator['packets']=[str(self.packet)]
        with self.assertRaisesRegex(contracts.ContractError,'modern companion'):
            self.preview()

    def test_observed_cohort_retains_companion_and_fails_after_its_deletion(self):
        import scoped_agents, project_manager, result_contracts
        self.plan_fixture(observed=True)
        original = copy.deepcopy(self.record)
        companion = scoped_agents.Store(self.library,self.slug)
        saved = companion.path.read_bytes()
        preview=self.preview()
        self.assertEqual('observed-pre-envelope',preview['cohort'])
        contracts.apply_adoption(self.record,preview,reason='Adopt recovered sources with intact observed evidence.',at='2026-09-10T00:00:00Z',operator_decided=True)
        self.assertEqual(saved,companion.path.read_bytes())
        self.assertEqual(original,{k:v for k,v in self.record.items() if k!='review_contract_adoptions'})
        self.assertTrue(project_manager._review_execution_satisfied(self.library,self.slug,self.record,self.record['plan_review']))
        # A new installed schema cannot invalidate a previously bound observed result.
        with patch.object(result_contracts,'resolve',side_effect=AssertionError('live schema rediscovery')):
            self.assertTrue(project_manager._review_execution_satisfied(self.library,self.slug,self.record,self.record['plan_review']))
        companion.path.unlink()
        self.assertFalse(project_manager._review_execution_satisfied(self.library,self.slug,self.record,self.record['plan_review']))
        self.assertFalse(contracts.historical_execution(self.record,self.record['plan_review'],scoped_agents.plan_owner(self.record)))

    def test_plan_cli_preview_apply_retry_and_stale_preview(self):
        import project_manager, contextlib, io
        self.plan_fixture();self.root.joinpath('locator.json').write_text(json.dumps(self.locator))
        output=self.root/'preview.json'
        def run(*args):
            with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                return project_manager.main(['--library',str(self.library.root),*args])
        self.assertEqual(0,run('review-contract','historical-preview',self.slug,'--input',str(self.root/'locator.json'),'--output',str(output)))
        original=self.library.read_record(self.slug)
        self.assertEqual(2,run('review-contract','historical-apply',self.slug,'--input',str(output),'--reason','Accept the listed receipt-specific gaps.'))
        self.assertEqual(original,self.library.read_record(self.slug))
        self.assertEqual(0,run('review-contract','historical-apply',self.slug,'--input',str(output),'--reason','Accept the listed receipt-specific gaps.','--operator-decided'))
        adopted=self.library.read_record(self.slug)
        self.assertEqual(0,run('review-contract','historical-apply',self.slug,'--input',str(output),'--reason','Same accepted decision.','--operator-decided'))
        self.assertEqual(adopted,self.library.read_record(self.slug))
        self.assertIn('historical-unverified',(self.library.plan_dir(self.slug)/'PLAN.md').read_text())

    def build_fixture(self, *, observed=False, unreviewed=False):
        """Complete synthetic pre-refresh history; the product commits and later rebase are real."""
        import subprocess, build_coordinator as bc, build_coordinator_review as review
        self.plan_fixture(observed=observed)
        self.product=self.root/'product';self.product.mkdir()
        def git(*args):
            return subprocess.run(['git','-C',str(self.product),*args],check=True,capture_output=True,text=True).stdout.strip()
        self.product_git=git
        git('init','-q','-b','main');git('config','user.email','fixture@example.test');git('config','user.name','Fixture')
        for rel in ('.claude/agents','.engine/schemas','.engine/policies','.engine/check'):
            shutil.copytree(ROOT/rel,self.product/rel)
        shutil.copy(ROOT/'.engine/build-protocol.json',self.product/'.engine/build-protocol.json')
        (self.product/'a.py').write_text('def value():\n    return 1\n')
        git('add','.');git('commit','-qm','synthetic product baseline');self.base=git('rev-parse','HEAD')
        git('update-ref','refs/remotes/origin/main',self.base)
        git('symbolic-ref','refs/remotes/origin/HEAD','refs/remotes/origin/main')
        git('switch','-qc','codex/historical-fixture')
        (self.product/'a.py').write_text('def value():\n    return 2\n')
        git('add','a.py');git('commit','-qm','synthetic reviewed feature');self.reviewed=git('rev-parse','HEAD')
        derived=self.product/'.engine/docs/ci-assurance.md';derived.parent.mkdir(exist_ok=True)
        derived.write_text('Synthetic generated assurance output.\n')
        git('add','.');git('commit','-qm','synthetic reviewed generated repair');self.repaired=git('rev-parse','HEAD')
        self.document=self.library.head(self.slug);self.payload=self.document['build_plan']
        self.plan_path=self.root/'build-plan.json';self.plan_path.write_text(json.dumps(self.payload))
        seal={'revision':1,'reviewed_digest':core.digest(self.document),'sealed_digest':core.digest(self.document),
              'build_plan_digest':core.digest(self.payload),'at':'2026-09-01T00:00:00Z','delta_judgment':'none'}
        self.library.update_record(self.slug,lambda r:r.update(seal=seal))
        patch.object(bc,'ROOT',self.product).start();patch.object(bc,'_library',return_value=self.library).start()
        original_run=bc._run
        patch.object(bc,'_run',side_effect=lambda argv,**kw:original_run(argv,cwd=kw.pop('cwd',self.product),**kw)).start()
        state=bc._initial_state('fixture/repository',7,self.base,self.document['plan_id'],seal['sealed_digest'],self.payload,None)
        state['ownership']={'build_id':'bld_'+'1'*32,'generation':1}
        state['approval']={'plan_digest':state['plan']['digest'],'depth':'thorough'}
        # Original integration names the real authored commit, not a made-up completed node.
        state['work']={item['id']:{'attempt_count':1,'claim':None,'latest_result':None,'latest_failure':None,
            'integration':{'attempt_id':'1'*32,'commit':self.reviewed,'focused_verification':'Synthetic product feature is present in this real commit.'}}
            for item in self.payload['work_items']}
        state['progress']={'current_item':None,'completed':[{'id':item['id'],'commit':self.reviewed} for item in self.payload['work_items']]}
        state['plan']['spec_digest']=bc._canonical_spec(self.payload)['digest']
        state['approval']['spec_digest']=state['plan']['spec_digest']
        ref={'plan_id':self.document['plan_id'],'revision':1,'plan_digest':core.digest(self.document)}
        source=contracts.reconstruct_source(self.source_root,self.commit,ref,'thorough')
        protocol=json.loads((self.product/'.engine/build-protocol.json').read_text())
        def packet(stage,base,tip,lenses):
            referent={'schema_version':'build-review-packet.v1','stage':stage,'raw_intent':self.payload['raw_intent'],
                      'plan':self.payload,'plan_digest':core.digest(self.payload),'intent_digest':state['plan']['intent_digest'],
                      'spec':bc._canonical_spec(self.payload),'commit':tip,'base_commit':base,'impact':{},
                      'protocol_digest':core.digest(protocol),'installed_lenses':lenses,'required_lenses':lenses}
            if stage=='repair':referent['repair_anchor']=base
            digest=core.digest(referent)
            descriptors=[{'lens':p['lens'],'path':p['source']['path'],'digest':p['source']['digest']}
                         for p in source['contract']['panels']['pre-submission-review'] if p['lens'] in lenses]
            if observed:
                bindings={p['lens']:p['semantic']['result_contract'] for p in source['contract']['panels']['pre-submission-review']}
                descriptors=[{**p,'result_contract':bindings[p['lens']]} for p in descriptors]
            descriptors=[{**p,'lens_packet_digest':review.lens_packet_digest(digest,p)} for p in descriptors]
            value={**referent,'referent_digest':digest,'reviewer_contracts':descriptors}
            value['packet_digest']=core.digest(value)
            path=self.root/(stage+'-original-packet.json');path.write_text(json.dumps(value))
            receipts=[{'lens':p['lens'],'packet_digest':value['packet_digest'],'referent_digest':digest,
                       'lens_packet_digest':p['lens_packet_digest'],'commit':tip,'finding_ids':[],
                       'code_execution':'none','reviewed_range':{'base':base,'tip':tip}} for p in descriptors]
            return value,receipts,path
        lenses=[p['lens'] for p in source['contract']['panels']['pre-submission-review']]
        delivery,receipts,delivery_path=packet('deliverable',self.base,self.reviewed,lenses)
        repair,repair_receipts,repair_path=packet('repair',self.reviewed,self.repaired,['technical-integrity'])
        state['reviews']['deliverable']={**bc._empty_review(),**{k:delivery[k] for k in (
            'packet_digest','referent_digest','required_lenses','installed_lenses','reviewer_contracts')},
            'receipts':receipts,'reviewed_commit':self.repaired,'base_commit':self.base}
        state['repair']={'reviewed_commit':self.reviewed,'final_commit':self.repaired,'summary':'Generated output review.',
            'judgment':'scoped','rationale':'Synthetic retained original assessment.','lenses':['technical-integrity'],
            'packet_digest':repair['packet_digest'],'referent_digest':repair['referent_digest'],
            'reviewer_contracts':repair['reviewer_contracts'],'receipts':repair_receipts}
        # A real original finding and its disposition make evidence-loss assertions non-vacuous.
        receipts[0]['finding_ids']=['OLD-1']
        state['findings']=[{'id':'OLD-1','stage':'deliverable','lens':receipts[0]['lens'],
            'packet_digest':receipts[0]['packet_digest'],'lens_packet_digest':receipts[0]['lens_packet_digest'],
            'commit':self.reviewed,'severity':'nit','summary':'Document the synthetic return-value behavior.',
            'disposition':'rejected','rationale':'The fixture demonstrates storage; documentation is outside this synthetic product.',
            'escalation_kind':None,'blocks_this_pr':False,'handoff_summary':'Synthetic historical finding retained.'}]
        if unreviewed:
            state["reviews"]["deliverable"]=bc._empty_review();state["repair"]=None;state["findings"]=[]
        if observed and not unreviewed:
            import scoped_agents
            from test_build_coordinator import observe_review_execution
            for packet_value, records in ((delivery,receipts),(repair,repair_receipts)):
                for receipt in records:
                    report=[{'severity':f['severity'],'message':f['summary'],'location':None} for f in state['findings'] if f['id'] in receipt['finding_ids']]
                    observe_review_execution(self.library,self.slug,scoped_agents.build_owner(state),receipt['lens'],receipt['lens_packet_digest'],report,packet_content=json.dumps(packet_value))
                    scoped_agents.accept_build(self.library,state,receipt,'fixture-root')
        self.store=bc.StateStore(str(self.root/'build-state.json'));self.store.create(state)
        self.record=self.store.read();self.backup=self.root/'retained-build-snapshot.json'
        self.backup.write_text(json.dumps(self.record))
        self.locator={'source_root':str(self.source_root),'source_commit':self.commit,
                      'backup':str(self.backup),'packets':[str(self.packet)] if unreviewed else [str(delivery_path),str(repair_path)]}
        return self.record

    def test_build_historical_preview_preserves_receipts_and_refuses_lost_deliverable(self):
        import build_coordinator as bc, argparse, contextlib, io, scoped_agents
        self.build_fixture();before=copy.deepcopy(self.record)
        locator=self.root/'locator.json';locator.write_text(json.dumps(self.locator));preview=self.root/'preview.json'
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_build_historical_preview(argparse.Namespace(plan=str(self.plan_path),input=str(locator),output=str(preview)),self.store)
            bc.cmd_build_historical_apply(argparse.Namespace(plan=str(self.plan_path),input=str(preview),reason='Accept only the named pre-collector gaps.',operator_decided=True),self.store)
        after=self.store.read()
        self.assertEqual(before['reviews'],after['reviews']);self.assertEqual(before['repair'],after['repair'])
        self.assertEqual(before['findings'],after['findings'])
        self.assertTrue(contracts.effective_build(after))
        self.assertEqual([],scoped_agents.missing_build_evidence(self.library,after,[r for _,r in bc.review.live_receipts(after)]))
        broken=copy.deepcopy(before);broken['reviews']['deliverable']['receipts']=[]
        self.backup.write_text(json.dumps(broken))
        with self.assertRaisesRegex(contracts.ContractError,'missing original deliverable'):
            contracts.adoption_preview(broken,self.library,self.slug,self.locator,self.product)

    def exercise_build_submission(self, *, observed=False):
        import build_coordinator as bc, scoped_agents, argparse, contextlib, io, subprocess, sys
        self.build_fixture(observed=observed)
        companion=scoped_agents.Store(self.library,self.slug)
        original_companion=companion.path.read_bytes() if companion.path.exists() else None
        original={k:copy.deepcopy(self.record[k]) for k in ('reviews','repair','findings')}
        preview=contracts.adoption_preview(self.record,self.library,self.slug,self.locator,self.product)
        decision=self.root/'decision.json';decision.write_text(json.dumps(preview))
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_build_historical_apply(argparse.Namespace(plan=str(self.plan_path),input=str(decision),
                reason='Accept the listed historical gaps for these exact original receipts.',operator_decided=True),self.store)
        # This is an actual patch-equivalent rebase onto an independent upstream commit.
        git=self.product_git
        git('switch','-q','main');(self.product/'upstream.txt').write_text('Independent upstream change.\n')
        # A disposable product declares its own small candidate suite. Run every declared command,
        # rather than attaching a green result under the Engine home's much larger protocol.
        argv=[sys.executable,'-B','-c','import a; assert a.value() == 2']
        protocol_path=self.product/'.engine/build-protocol.json'
        protocol=json.loads(protocol_path.read_text())
        protocol['validation_commands']['candidate']=[{'id':'synthetic-product','label':'Synthetic product assertion','command':argv}]
        protocol_path.write_text(json.dumps(protocol))
        patch.object(bc,'PROTOCOL_PATH',protocol_path).start()
        git('add','upstream.txt','.engine/build-protocol.json');git('commit','-qm','upstream change and product candidate declaration');new_base=git('rev-parse','HEAD')
        git('update-ref','refs/remotes/origin/main',new_base)
        git('switch','-q','codex/historical-fixture');git('rebase','main')
        head=git('rev-parse','HEAD');self.assertNotEqual(self.repaired,head)
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_reconcile(argparse.Namespace(plan=str(self.plan_path)),self.store)
        self.assertTrue(self.store.read()['reconciles'][-1]['contribution_identical'])
        # The synthetic product's candidate evidence is computed from a real run at this head.
        # No mocked validation result, status, range predicate, or submission preview is used.
        self.assertEqual(argv,bc._protocol()['validation_commands']['candidate'][0]['command'])
        checked=subprocess.run(argv,cwd=self.product,capture_output=True,text=True)
        self.assertEqual(0,checked.returncode,checked.stderr)
        candidate={'commit':head,'merge_base':new_base,'protocol_digest':core.digest(bc._protocol()),
                   'argv_digests':{'synthetic-product':core.digest(argv)},'inventory_digest':core.digest(['a.value() == 2']),
                   'run_record':None,'results':[{'id':'synthetic-product','commit':head,'passed':checked.returncode==0,
                                               'summary':'Executed the synthetic product assertion at this exact head.'}]}
        self.store.mutate(lambda s:s.update(validation={'candidate':candidate,'final':None}))
        packet_path=self.root/'upgraded-packet.json'
        packet_args=argparse.Namespace(plan=str(self.plan_path),stage='deliverable',impact=None,standalone=False,
                                       output=str(packet_path),json=False,session=None)
        with patch.object(scoped_agents,'prepare_packets',side_effect=AssertionError('Historical continuation launched a reviewer')),contextlib.redirect_stdout(io.StringIO()):
            bc._packet(packet_args,self.store)
        state=self.store.read()
        self.assertEqual([],bc._missing_receipts(state['reviews']['deliverable'],state=state))
        self.assertEqual(original['reviews']['deliverable']['receipts'],state['reviews']['deliverable']['receipts'])
        self.assertEqual(original['repair'],state['repair']);self.assertEqual(original['findings'],state['findings'])
        # The normal proportional repair owner records the now-empty measured divergence.
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_repair_assess(argparse.Namespace(judgment='none',lens=[],rationale='Measured patch-equivalent rebase; no unread authored delta.',
                                                    guidance=None),self.store)
        state=self.store.read()
        # Only the GitHub transport is synthetic. All status and submission predicates run normally.
        rule=json.loads((ROOT/'.engine/check/pr-body-completeness.json').read_text())['params']
        body='\n'.join(rule['required_phrases'])+'\n\n'+'\n\n'.join('## '+heading+'\n\nImpact: Synthetic historical continuity witness.\n\nThe fixture exercises this section.' for heading in rule['sections'])
        body+='\n\n'+bc._drift_line(state,head)+'\n'+'\n'.join(bc._repair_round_lines(state)+bc._round_guidance_lines(state))
        self.assertIn('observed-pre-envelope' if observed else 'historical-unverified',body)
        pr={'number':7,'state':'OPEN','isDraft':True,'headRefOid':head,'baseRefOid':new_base,'mergeable':'MERGEABLE','body':body,
            'statusCheckRollup':[{'name':'engine-ci','status':'COMPLETED','conclusion':'SUCCESS','completedAt':'2026-09-10T00:00:00Z'}]}
        tree=git('rev-parse','HEAD^{tree}')
        with patch.object(bc.github,'pr_state',side_effect=lambda *a,**kw:copy.deepcopy(pr)),\
                patch('boot.gh_token',return_value='synthetic-fixture-token'),\
                patch.object(bc.ci_gatekeeper,'find_reusable_receipt',return_value=(True,{
                    'run_id':42,'run_attempt':1,'receipt':{'schema':'engine-ci-receipt/v1','mode':'full','tree':tree}})),\
                contextlib.redirect_stdout(io.StringIO()):
            bc._final_import(argparse.Namespace(),self.store)
            bc.cmd_preflight(argparse.Namespace(pr_body=None,json=False),self.store)
            ready=bc._status(self.store.read(),self.payload)
            self.assertEqual([],ready['required_evidence'])
            self.assertEqual([],ready['engineering_judgment'])
            self.assertEqual('ready',ready['phase'])
            self.assertEqual('mark-ready',bc._submit_preview(self.store,str(self.plan_path))['action'])
            valid_receipts=copy.deepcopy(self.store.read()['reviews']['deliverable']['receipts'])
            self.store.mutate(lambda s:s['reviews']['deliverable']['receipts'].pop())
            refused=bc._status(self.store.read(),self.payload)
            self.assertNotEqual('ready',refused['phase'])
            self.assertTrue(any('receipt:' in e for e in refused['required_evidence']))
            with self.assertRaises(bc.CoordinatorError):
                bc._submit_preview(self.store,str(self.plan_path))
            self.store.mutate(lambda s:s['reviews']['deliverable'].update(receipts=valid_receipts))
            if observed:
                saved=companion.path.read_bytes();companion.path.unlink()
                missing=bc._status(self.store.read(),self.payload)
                self.assertNotEqual('ready',missing['phase'])
                self.assertTrue(any('verified fresh review execution' in e for e in missing['required_evidence']))
                with self.assertRaisesRegex(bc.CoordinatorError,'submission evidence is incomplete'):
                    bc._submit_preview(self.store,str(self.plan_path))
                companion.path.write_bytes(saved)
            with patch.object(bc.github,'set_ready',side_effect=lambda *a:pr.update(isDraft=False)) as mark_ready:
                bc.cmd_submit_apply(argparse.Namespace(plan=str(self.plan_path)),self.store)
                mark_ready.assert_called_once()
        self.assertEqual('ready',self.store.read()['submission'])
        self.assertEqual(original['reviews']['deliverable']['receipts'],self.store.read()['reviews']['deliverable']['receipts'])
        self.assertEqual(original['findings'],self.store.read()['findings'])
        self.assertEqual(original_companion,companion.path.read_bytes() if companion.path.exists() else None)
        if not observed:
            # Re-run every unrelated leg for a real new authored commit. Submission must still
            # refuse the unexplained delta; stale candidate/CI/body evidence cannot make this pass.
            (self.product/'unread.txt').write_text('New authored behavior requiring a repair judgment.\n')
            git('add','unread.txt');git('commit','-qm','unexplained authored delta')
            changed_head=git('rev-parse','HEAD')
            checked=subprocess.run(argv,cwd=self.product,capture_output=True,text=True)
            self.assertEqual(0,checked.returncode,checked.stderr)
            changed_candidate={**candidate,'commit':changed_head,'results':[
                {**candidate['results'][0],'commit':changed_head,'passed':checked.returncode==0}]}
            self.store.mutate(lambda s:s.update(validation={'candidate':changed_candidate,'final':None},submission='draft'))
            state=self.store.read()
            changed_body='\n'.join(rule['required_phrases'])+'\n\n'+'\n\n'.join(
                '## '+heading+'\n\nImpact: Synthetic historical continuity witness.\n\nThe fixture exercises this section.' for heading in rule['sections'])
            changed_body+='\n\n'+bc._drift_line(state,changed_head)+'\n'+'\n'.join(bc._repair_round_lines(state)+bc._round_guidance_lines(state))
            pr.update(headRefOid=changed_head,isDraft=True,body=changed_body)
            with patch.object(bc.github,'pr_state',side_effect=lambda *a,**kw:copy.deepcopy(pr)),\
                    patch('boot.gh_token',return_value='synthetic-fixture-token'),\
                    patch.object(bc.ci_gatekeeper,'find_reusable_receipt',return_value=(True,{
                        'run_id':43,'run_attempt':1,'receipt':{'schema':'engine-ci-receipt/v1','mode':'full','tree':git('rev-parse','HEAD^{tree}')}})),\
                    contextlib.redirect_stdout(io.StringIO()):
                bc._final_import(argparse.Namespace(),self.store)
                bc.cmd_preflight(argparse.Namespace(pr_body=None,json=False),self.store)
                blocked=bc._status(self.store.read(),self.payload)
                self.assertEqual([],blocked['required_evidence'])
                self.assertTrue(blocked['engineering_judgment'])
                self.assertEqual('repair-assessment',blocked['phase'])
                with self.assertRaisesRegex(bc.CoordinatorError,'submission evidence is incomplete'):
                    bc._submit_preview(self.store,str(self.plan_path))

    def test_eligible_build_rebases_refreshes_and_submits_without_a_reviewer_launch(self):
        self.exercise_build_submission()

    def test_observed_build_submission_refuses_deleted_companion_at_an_otherwise_ready_gate(self):
        self.exercise_build_submission(observed=True)

    def test_build_observed_adoption_cannot_waive_deleted_companion(self):
        import scoped_agents, build_coordinator as bc
        self.build_fixture(observed=True)
        companion=scoped_agents.Store(self.library,self.slug);original=companion.path.read_bytes()
        preview=contracts.adoption_preview(self.record,self.library,self.slug,self.locator,self.product)
        self.assertEqual('observed-pre-envelope',preview['cohort'])
        contracts.apply_adoption(self.record,preview,reason='Retain intact observed evidence.',at='2026-09-10T00:00:00Z',operator_decided=True)
        receipts=[r for _,r in bc.review.live_receipts(self.record)]
        self.assertEqual([],scoped_agents.missing_build_evidence(self.library,self.record,receipts))
        self.assertEqual(original,companion.path.read_bytes())
        companion.path.unlink()
        self.assertEqual({r['lens'] for r in receipts},set(scoped_agents.missing_build_evidence(self.library,self.record,receipts)))
        self.assertTrue(all(not contracts.historical_execution(self.record,r,scoped_agents.build_owner(self.record)) for r in receipts))

    def test_build_missing_minimum_artifacts_refuse_without_changing_state(self):
        self.build_fixture();original=copy.deepcopy(self.record)
        for fault in ('finding','packet','owner','range','approval','modern-marker'):
            with self.subTest(fault=fault):
                record=copy.deepcopy(original);locator=copy.deepcopy(self.locator)
                if fault=='finding':record['findings']=[]
                elif fault=='packet':locator['packets']=[]
                elif fault=='owner':record['ownership']['generation']+=1
                elif fault=='range':record['reviews']['deliverable']['receipts'][0]['reviewed_range']['base']='0'*40
                elif fault=='approval':record['approval']['depth']='quick'
                else:record['review_contract_format']=1
                # Where an artifact really is absent from the original backup, consent cannot replace it.
                self.backup.write_text(json.dumps(record if fault in ('finding','range') else original))
                with self.assertRaises(contracts.ContractError):
                    contracts.adoption_preview(record,self.library,self.slug,locator,self.product)
                self.assertEqual(original,self.store.read())

    def test_build_apply_checks_owner_consent_staleness_and_atomic_retry(self):
        import build_coordinator as bc,argparse,contextlib,io
        self.build_fixture();preview=contracts.adoption_preview(self.record,self.library,self.slug,self.locator,self.product)
        path=self.root/'decision.json';path.write_text(json.dumps(preview))
        args=argparse.Namespace(plan=str(self.plan_path),input=str(path),reason='Only these named historical gaps.',operator_decided=False)
        original=self.store.read()
        with self.assertRaisesRegex(contracts.ContractError,'operator'):
            bc.cmd_build_historical_apply(args,self.store)
        self.assertEqual(original,self.store.read());args.operator_decided=True
        altered=copy.deepcopy(preview);altered['owner']['generation']+=1;path.write_text(json.dumps(altered))
        with self.assertRaisesRegex(bc.CoordinatorError,'another Build owner'):
            bc.cmd_build_historical_apply(args,self.store)
        self.assertEqual(original,self.store.read());path.write_text(json.dumps(preview))
        with patch.object(self.store,'_write',side_effect=OSError('synthetic interruption before atomic publish')),self.assertRaises(OSError):
            bc.cmd_build_historical_apply(args,self.store)
        self.assertEqual(original,self.store.read())
        with contextlib.redirect_stdout(io.StringIO()):
            bc.cmd_build_historical_apply(args,self.store)
            accepted=self.store.read()
            bc.cmd_build_historical_apply(args,self.store)
        self.assertEqual(accepted,self.store.read())
        self.assertEqual(1,len(accepted['review_contract_adoptions']))

    def test_sealed_unbound_adoption_retains_original_seal_and_empty_receipt_has_no_credit(self):
        import scoped_agents
        self.plan_fixture();document=self.library.head(self.slug)
        seal={'revision':1,'reviewed_digest':core.digest(document),'sealed_digest':core.digest(document),
              'build_plan_digest':core.digest(document['build_plan']),'at':'2026-09-01T00:00:00Z','delta_judgment':'none'}
        self.library.update_record(self.slug,lambda r:r.update(seal=seal))
        self.record=self.library.read_record(self.slug);self.backup.write_text(json.dumps(self.record))
        preview=self.preview()
        contracts.apply_adoption(self.record,preview,reason='Adopt after sealing without changing the old seal.',at='2026-09-10T00:00:00Z',operator_decided=True)
        self.assertEqual(seal,self.record['seal'])
        empty={**self.record['plan_review'],'lenses':[]}
        self.assertFalse(contracts.historical_execution(self.record,empty,scoped_agents.plan_owner(self.record)))

    def test_unexplained_authored_delta_cannot_borrow_reconciled_review_credit(self):
        import build_coordinator as bc,argparse,contextlib,io
        self.build_fixture();preview=contracts.adoption_preview(self.record,self.library,self.slug,self.locator,self.product)
        contracts.apply_adoption(self.record,preview,reason='Accept only listed original gaps.',at='2026-09-10T00:00:00Z',operator_decided=True)
        git=self.product_git
        git('switch','-q','main');(self.product/'upstream.txt').write_text('Upstream.\n')
        git('add','.');git('commit','-qm','upstream');base=git('rev-parse','HEAD')
        git('update-ref','refs/remotes/origin/main',base);git('switch','-q','codex/historical-fixture');git('rebase','main')
        head=git('rev-parse','HEAD')
        # A recorded reconcile is re-measured, not trusted by its boolean flag.
        self.record['reconciles']=[{'base_before':self.base,'from_commit':self.repaired,'base_after':base,
                                   'to_commit':head,'contribution_identical':True}]
        stage={**self.record['reviews']['deliverable'],'base_commit':base,'reviewed_commit':head}
        receipt=self.record['reviews']['deliverable']['receipts'][0]
        self.assertTrue(bc._coverage(stage,'deliverable',self.record)(receipt))
        (self.product/'a.py').write_text('def value():\n    return 99\n')
        git('add','a.py');git('commit','-qm','unread authored delta');stage['reviewed_commit']=git('rev-parse','HEAD')
        self.assertFalse(bc._coverage(stage,'deliverable',self.record)(receipt))
        # Even restating the reconcile's target cannot turn the changed contribution into the old read.
        self.record['reconciles'][0]['to_commit']=stage['reviewed_commit']
        self.assertFalse(bc._coverage(stage,'deliverable',self.record)(receipt))

    def test_unreviewed_build_adopts_contract_without_inventing_execution(self):
        self.build_fixture(observed=True,unreviewed=True)
        preview=contracts.adoption_preview(self.record,self.library,self.slug,self.locator,self.product)
        self.assertEqual([],preview['receipts']);self.assertEqual(['approval-envelope'],preview['gaps'])
        self.assertEqual('observed-pre-envelope',preview['cohort'])
        original=copy.deepcopy(self.record)
        contracts.apply_adoption(self.record,preview,reason='Freeze recovered original sources before any Build review.',at='2026-09-10T00:00:00Z',operator_decided=True)
        self.assertEqual(original['reviews'],self.record['reviews'])
        self.assertEqual([],self.record['review_contract_adoptions'][0]['receipts'])

    def test_deleted_observed_build_panel_cannot_claim_it_was_unreviewed(self):
        import build_coordinator as bc
        self.build_fixture(observed=True)
        self.record['reviews']['deliverable']=bc._empty_review();self.record['repair']=None;self.record['findings']=[]
        self.backup.write_text(json.dumps(self.record));self.locator['packets']=[str(self.packet)]
        with self.assertRaisesRegex(contracts.ContractError,'original accepted Build receipts are missing'):
            contracts.adoption_preview(self.record,self.library,self.slug,self.locator,self.product)

    def test_reconstruction_uses_retained_limits_after_live_limits_expand(self):
        import result_contracts
        original=copy.deepcopy(result_contracts.LIMITS)
        live={**original,'bytes':original['bytes']*2}
        ref={'plan_id':'pln_0123456789ab','revision':1,'plan_digest':'sha256:'+'a'*64}
        with patch.object(result_contracts,'LIMITS',live):
            recovered=contracts.reconstruct_source(self.source_root,self.observed_commit,ref,'thorough')
        for panel in recovered['contract']['panels'].values():
            self.assertTrue(all(p['semantic']['result_contract']['limits']==original for p in panel))


if __name__ == '__main__': unittest.main()
