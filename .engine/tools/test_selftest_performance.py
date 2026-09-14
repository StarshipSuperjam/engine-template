"""Bounded performance publication cannot control CI verdicts or interpret authored markup."""
import io
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selftest_performance as performance
import selftest_results as records

HEAD='a'*40
TREE='b'*40


def at(second):
    return performance.moment.to_z(1_800_000_000+second)


class ReportAPI:
    """Small platform-shaped fake; every read is explicit and unexpected reads fail."""
    repository='owner/repo'
    def __init__(self,route='full-pr',attempts=1):
        self.runs={};self.jobs={};self.checks=[];self.documents={};self.artifacts=[]
        self.rules=[{'type':'required_status_checks','parameters':{'required_status_checks':[{'context':'engine-ci'},{'context':'engine-guard'}]}}]
        self.classic=None
        for run,context,event in [(10,'engine-ci','pull_request'),(20,'engine-guard','pull_request_target')]:
            for attempt in range(1,(attempts if run==10 else 1)+1):
                start=5+(attempt-1)*100;end=start+(20 if run==10 else 10);jobid=run*100+attempt
                workflow=f'.github/workflows/{context}.yml'
                self.runs[run,attempt]={'id':run,'run_attempt':attempt,'path':workflow,'status':'completed',
                    'event':event,'head_sha':HEAD if run==10 else 'c'*40,'check_suite_id':run*1000,
                    'created_at':at(0),'run_started_at':at(start-2),'updated_at':at(end+1),'conclusion':'success',
                    'pull_requests':[{'head':{'sha':HEAD},'base':{'ref':'main'}}]}
                step={'full-pr':performance.FULL_STEP,'metadata-reuse':performance.REUSE_STEP,
                      'project-only':performance.PROJECT_STEP}[route] if run==10 else 'Guardrail classifier'
                self.jobs[run,attempt]=[{'id':jobid,'run_id':run,'run_attempt':attempt,'name':context,
                    'head_sha':self.runs[run,attempt]['head_sha'],'status':'completed','conclusion':'success',
                    'created_at':at(start-2),'started_at':at(start),'completed_at':at(end),
                    'check_run_url':f'https://api.github.com/repos/owner/repo/check-runs/{jobid}',
                    'labels':['ubuntu-latest'],'steps':[{'name':step,'conclusion':'success',
                        'started_at':at(start+1),'completed_at':at(end-1)}]}]
                self.checks.append({'id':jobid,'url':self.jobs[run,attempt][0]['check_run_url'],'name':context,
                    'head_sha':HEAD,'check_suite':{'id':run*1000},'app':{'slug':'github-actions','id':15368},
                    'status':'completed','conclusion':'success'})
        case=unittest.FunctionTestCase(lambda:None);case.id=lambda:'test_fixture.Case.test_behavior'
        obs=records.Observation([case],[case],source={'tree':TREE,'worktree_dirty':False},scope='full',
            invocation={'start_dir':'tools','pattern':'test_*.py','selection_digest':None},timing=True)
        obs.metadata['ci']={'run_id':'10','run_attempt':str(attempts),'head':HEAD,'route':'pull_request'}
        obs.start(case);obs.outcome(case,'passed');obs.stop(case)
        env={'os':'Linux','release':'fixture','architecture':'x86_64','python':'3.12','cpu_count':2,
             'runner_os':'Linux','runner_arch':'X64','uv':'uv 0.11.8','memory_bytes':1024,'worktree_count':1,'cache':'cold'}
        with mock.patch.object(records,'environment',return_value=env):metrics=obs.performance(0.01,1)
        metrics['parent_seconds']=1
        for i,(prefix,filename,document) in enumerate([(performance.RESULTS_PREFIX,'selftest-results.json',obs.document(True)),
            (performance.PERFORMANCE_PREFIX,'selftest-performance.json',metrics)],1):
            self.artifacts.append({'id':i,'name':f'{prefix}-10-{attempts}','expired':False,'size_in_bytes':1000})
            self.documents[i]=document,filename

    def get(self,path):
        if path=='/branches/main/protection/required_status_checks':
            if self.classic is None:raise performance.APIError(404,'Branch not protected')
            return self.classic
        if path==f'/commits/{HEAD}':return {'sha':HEAD,'commit':{'tree':{'sha':TREE}},'parents':[]}
        match=performance.re.fullmatch(r'/actions/runs/(\d+)/attempts/(\d+)',path)
        if match:return copy.deepcopy(self.runs[int(match[1]),int(match[2])])
        raise AssertionError('unexpected API GET '+path)

    def pages(self,path,key=None):
        if path=='/rules/branches/main':return copy.deepcopy(self.rules)
        if path==f'/commits/{HEAD}/check-runs?filter=all':return copy.deepcopy(self.checks)
        if path=='/actions/runs/10/artifacts':return copy.deepcopy(self.artifacts)
        match=performance.re.fullmatch(r'/actions/runs/(\d+)/attempts/(\d+)/jobs',path)
        if match:return copy.deepcopy(self.jobs[int(match[1]),int(match[2])])
        raise AssertionError('unexpected API page '+path)

    def artifact(self,artifact,filename):
        document,expected=self.documents[artifact['id']]
        if filename!=expected:raise AssertionError(filename)
        return copy.deepcopy(document),'d'*64


def report(api,attempt=1):
    return performance.completed_report(api,HEAD,10,attempt,
        [('engine-guard',{'run':20,'attempt':1,'workflow':'.github/workflows/engine-guard.yml'})])


class CompletedReporting(unittest.TestCase):
    def test_both_contexts_and_target_base_head_are_bound_without_guessing(self):
        value=report(ReportAPI())
        self.assertTrue(value['complete'],value['issues'])
        records.validate_shape(value,'ci-test-performance.v1')
        self.assertEqual(value['metrics']['elapsed_seconds'],25)
        self.assertEqual(value['metrics']['active_union_seconds'],20)
        self.assertEqual(value['metrics']['queue_only_seconds'],2)
        self.assertEqual(value['metrics']['queue_excluded_seconds'],23)
        self.assertEqual(value['metrics']['runner_minutes'],0.5)
        self.assertEqual(value['attempts'][1]['workflow_head'],'c'*40)
        self.assertFalse(value['requirements']['historical_policy'])

    def test_retries_retain_failed_attempt_and_cumulative_runner_cost(self):
        api=ReportAPI(attempts=2)
        api.runs[10,1]['conclusion']='failure';api.jobs[10,1][0]['conclusion']='failure'
        api.checks[0]['conclusion']='failure'
        value=report(api,2)
        self.assertTrue(value['complete'],value['issues'])
        self.assertEqual(len(value['attempts']),3)
        self.assertEqual(value['attempts'][0]['conclusion'],'failure')
        self.assertAlmostEqual(value['metrics']['cumulative_runner_minutes'],50/60)

    def test_missing_ambiguous_and_wrong_associations_remain_incomplete(self):
        mutations=[lambda a:a.checks.pop(),lambda a:a.checks.append(copy.deepcopy(a.checks[-1])),
                   lambda a:a.checks[-1].update(head_sha='f'*40),
                   lambda a:a.runs[20,1].update(path='.github/workflows/impostor.yml'),
                   lambda a:a.jobs[20,1][0].update(run_attempt=2),
                   lambda a:a.jobs[20,1][0].update(status='in_progress')]
        for mutate in mutations:
            api=ReportAPI();mutate(api)
            with self.subTest(mutation=mutate):
                value=report(api);self.assertFalse(value['complete']);self.assertTrue(value['issues'])

    def test_missing_expired_and_other_attempt_metrics_never_substitute(self):
        for mutation in [lambda a:a.artifacts.pop(),lambda a:a.artifacts[0].update(expired=True),
                         lambda a:a.documents[1][0]['ci'].update(run_attempt='2')]:
            api=ReportAPI();mutation(api)
            value=report(api)
            self.assertFalse(value['complete'])
            self.assertTrue(value['timing_complete'])
            self.assertIsNotNone(value['metrics'])

    def test_reuse_and_project_only_are_distinct_and_need_no_full_artifacts(self):
        for route in ['metadata-reuse','project-only']:
            api=ReportAPI(route);api.artifacts=[]
            value=report(api)
            self.assertTrue(value['complete'],value['issues']);self.assertEqual(value['route'],route)
            self.assertIsNone(value['cases'])

    def test_main_push_is_a_reference_class_and_never_claims_pr_path_coverage(self):
        api=ReportAPI();api.runs[10,1].update(event='push',head_branch='main',pull_requests=[])
        for document,_ in api.documents.values():document['ci']['route']='push'
        value=performance.completed_report(api,HEAD,10,1)
        self.assertTrue(value['complete'],value['issues'])
        self.assertEqual(value['route'],'main-push')
        self.assertIn('no PR-path credit',value['requirements']['applicability'])

    def test_api_failure_keeps_an_incomplete_structured_report(self):
        api=ReportAPI();api.get=lambda path:(_ for _ in ()).throw(performance.APIError(403,'access unavailable'))
        value=report(api)
        self.assertFalse(value['complete']);self.assertIn('access unavailable',value['issues'])
        records.validate_shape(value,'ci-test-performance.v1')

    def test_classic_required_contexts_and_app_bindings_are_not_ignored(self):
        api=ReportAPI();api.classic={'checks':[{'context':'external-check','app_id':12}],'contexts':[]}
        self.assertFalse(report(api)['complete'])
        api=ReportAPI();api.classic={'checks':[{'context':'engine-ci','app_id':12}],'contexts':[]}
        self.assertFalse(report(api)['complete'])

    def test_comparison_exposes_common_case_cost_and_rejects_environment_drift(self):
        a=report(ReportAPI());b=copy.deepcopy(a)
        b['cases'][0]['seconds']+=3;b['metrics']['elapsed_seconds']+=4
        value=performance.compare_reports(a,b)
        self.assertTrue(value['qualified'],value['reasons'])
        self.assertEqual(value['cases']['common'][0]['delta_seconds'],3)
        b['environment']['worktree_count']=9
        self.assertIn('worktree_count differs',performance.compare_reports(a,b)['reasons'])
        b['environment']['cache']='unknown'
        self.assertIn('cache unavailable',performance.compare_reports(a,b)['reasons'])
        self.assertIsNone(performance.sample_summary([a,b,a])['p90_seconds'])

    def test_comparison_marks_inventory_outcome_and_missing_case_changes_unqualified(self):
        baseline=report(ReportAPI())
        for change, reason in [(lambda d:d['cases'].append({**d['cases'][0],'id':'another'}),'case inventory differs'),
                               (lambda d:d['cases'].clear(),'case inventory differs'),
                               (lambda d:d['cases'][0].update(outcome='failed'),'case outcomes differ'),
                               (lambda d:d.update(cases=None),'case observations unavailable')]:
            candidate=copy.deepcopy(baseline);change(candidate)
            result=performance.compare_reports(baseline,candidate)
            self.assertFalse(result['qualified'])
            self.assertIn(reason,result['reasons'])

    def test_inconsistent_timings_are_rejected_by_reporting_and_publication(self):
        for change in [lambda d:d.update(case_seconds=20),
                       lambda d:d['cases'][0].update(seconds=20),
                       lambda d:d.update(parent_seconds=0),
                       lambda d:d.update(unallocated_seconds=20),
                       lambda d:d['spans'].append({'phase':'_callTestMethod','level':'case',
                                                  'owner':d['cases'][0]['id'],'start':0,'seconds':2})]:
            api=ReportAPI();metrics=api.documents[2][0];change(metrics)
            with self.assertRaises(ValueError):records.validate_performance(metrics)
            self.assertFalse(report(api)['complete'])
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);records.write(root/'outcomes',api.documents[1][0]);records.write(root/'timing',metrics)
                result,timing=performance.stage_observations(root/'outcomes',root/'timing',root/'safe')
                self.assertTrue(result.exists());self.assertFalse(timing.exists())
                self.assertIn('Timing: unknown',performance.observed_summary(result,timing))

    def test_staging_keeps_rejected_bytes_and_preplanted_files_out_of_uploads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for index,content in enumerate(['{','{"secret":"not an observation"}','private'*1000]):
                raw=root/'raw';raw.write_text(content)
                with mock.patch.object(records,'MAX_BYTES',2048):
                    outcomes,timing=performance.stage_observations(raw,raw,root/str(index))
                self.assertEqual(records.validate(records.read(outcomes)),(False,False))
                self.assertNotIn('secret',outcomes.read_text());self.assertNotIn('private',outcomes.read_text())
                self.assertFalse(timing.exists())
            planted=root/'planted';planted.mkdir();sentinel=planted/'selftest-results.json';sentinel.write_text('do not upload')
            with mock.patch('sys.stdout',io.StringIO()):
                code=performance.main(['publish','--results',str(raw),'--performance',str(raw),'--artifact-dir',str(planted)])
            self.assertEqual(code,1)
            self.assertEqual(sentinel.read_text(),'do not upload')

    def test_pagination_visits_every_page_and_refuses_a_truncated_budget(self):
        api=performance.GitHub.__new__(performance.GitHub)
        seen=[]
        def get(path):
            seen.append(path)
            return {'jobs':list(range(100)) if path.endswith('&page=1') else [100]}
        api.get=get
        self.assertEqual(len(api.pages('/jobs','jobs')),101)
        self.assertEqual(len(seen),2)
        with mock.patch.object(performance,'MAX_PAGES',1),self.assertRaises(ValueError):
            api.pages('/jobs','jobs')

    def test_archive_paths_and_cross_host_authorization_are_rejected(self):
        api=performance.GitHub.__new__(performance.GitHub);api.prefix='/repos/owner/repo'
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as archive:archive.writestr('../outside','{}')
        api.raw=lambda path:raw.getvalue()
        with self.assertRaises(ValueError):api.artifact({'id':1,'expired':False,'size_in_bytes':100},'selftest-results.json')
        req=urllib.request.Request('https://api.github.com/repos/owner/repo',headers={'Authorization':'Bearer sentinel'})
        redirected=performance._Redirect().redirect_request(req,None,302,'',{},'https://blob.example/download')
        self.assertFalse(redirected.has_header('Authorization'))


class SafePublication(unittest.TestCase):
    def test_missing_and_invalid_artifacts_are_explicit_unknowns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'bad.json';path.write_text('{')
            summary=performance.observed_summary(path,Path(tmp)/'missing')
        self.assertIn('Outcomes: unknown',summary)
        self.assertIn('Timing: unknown',summary)
        self.assertIn('not the complete required PR CI path',summary)

    def test_publisher_only_appends_summary_and_never_control_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths={name:str(Path(tmp)/name) for name in ['GITHUB_ENV','GITHUB_OUTPUT','GITHUB_PATH','GITHUB_STATE','GITHUB_STEP_SUMMARY']}
            for path in paths.values():Path(path).write_text('sentinel\n')
            with mock.patch.dict(os.environ,paths),mock.patch('sys.stdout',io.StringIO()):
                self.assertEqual(performance.main(['publish','--results',tmp+'/missing','--performance',tmp+'/missing']),0)
            for name,path in paths.items():
                value=Path(path).read_text()
                if name=='GITHUB_STEP_SUMMARY':self.assertIn('Outcomes: unknown',value)
                else:self.assertEqual(value,'sentinel\n')

    def test_authored_names_are_escaped_and_cannot_inject_workflow_lines(self):
        case=unittest.FunctionTestCase(lambda:None)
        case.id=lambda:'<script>bad</script>\n::set-output name=mode::reuse'
        obs=records.Observation([case],[case],source={'tree':None,'worktree_dirty':None},scope='full',
                                invocation={'start_dir':'tools','pattern':'test_*.py','selection_digest':None},timing=True)
        obs.start(case);obs.outcome(case,'passed');obs.stop(case)
        env={'os':'Linux','release':'fixture','architecture':'x86_64','python':'3.12',
             'cpu_count':2,'runner_os':None,'runner_arch':None,'uv':None,'memory_bytes':None,
             'worktree_count':None,'cache':'unknown'}
        with mock.patch.object(records,'environment',return_value=env):metrics=obs.performance(0,1)
        metrics['parent_seconds']=1
        with tempfile.TemporaryDirectory() as tmp:
            result=Path(tmp)/'results';timing=Path(tmp)/'timing'
            records.write(result,obs.document(True));records.write(timing,metrics)
            summary=performance.observed_summary(result,timing)
        self.assertIn('&lt;script&gt;',summary)
        self.assertNotIn('<script>',summary)
        self.assertNotIn('\n::set-output',summary)

    def test_real_launcher_keeps_all_five_runner_controls_on_decoys(self):
        import yaml
        root=Path(__file__).resolve().parents[2]
        workflow=yaml.safe_load((root/'.github/workflows/engine-ci.yml').read_text())
        step=next(s for s in workflow['jobs']['engine-ci']['steps'] if s.get('id')=='selftests')
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp); env=dict(os.environ)
            for name in ['GITHUB_ENV','GITHUB_OUTPUT','GITHUB_PATH','GITHUB_STATE','GITHUB_STEP_SUMMARY']:
                self.assertEqual(step['env'][name],'${{ runner.temp }}/decoy-'+name.lower().replace('_','-'))
                (folder/name).write_text('real sentinel')
                env[name]=str(folder/('decoy-'+name))
            (folder/'test_controls.py').write_text('''import os,unittest
class Controls(unittest.TestCase):
    def test_attempt(self):
        for name in ['GITHUB_ENV','GITHUB_OUTPUT','GITHUB_PATH','GITHUB_STATE','GITHUB_STEP_SUMMARY']:
            with open(os.environ[name],'a') as stream: stream.write('mode=reuse\\n')
''')
            run=subprocess.run([sys.executable,str(root/'.engine/tools/selftest.py'),'--start-dir',tmp,'--cwd',tmp,
                                '--results-path',str(folder/'results.json')],env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            for name in ['GITHUB_ENV','GITHUB_OUTPUT','GITHUB_PATH','GITHUB_STATE','GITHUB_STEP_SUMMARY']:
                self.assertEqual((folder/name).read_text(),'real sentinel')
                self.assertIn('mode=reuse',(folder/('decoy-'+name)).read_text())


import selftest_cost as cost

_COST_CONTRACT = {
    'schema_version': 'test-cost-contract.v1', 'supported_fault': 'Incorrect or stale cost comparison',
    'boundary': 'pure', 'boundary_rationale': 'Pure bounded identity, counter and clock examples',
    'fixture_owner': 'test_selftest_performance.CostAssessment', 'dependencies': ['jsonschema', 'selftest_cost'],
    'data_reads': ['.engine/policies/test-cost.json', '.engine/schemas/test-cost-*.json',
                   '.engine/schemas/ci-test-performance.v1.json'], 'cadence': 'pr',
    'limits': {**cost.zeros(), 'schema_decodes': 50}, 'mutable_state': 'Fresh one-case dictionaries',
    'cache_lifetime': 'case', 'added_cost_risk': 'At most three timing or scaling samples per example', 'families': [],
}


def cost_example():
    policy = json.loads((cost.ROOT / '.engine/policies/test-cost.json').read_text())
    case = {'id': 'test_example.C.test_behavior', 'occurrence': 1}
    source = 'class C:\n def test_behavior(self): pass\n'
    census = cost.static_census({'test_example.py': source}, 'a'*40)
    runtime = [{**case, 'path': 'test_example.py', 'qualified_name': 'C.test_behavior',
                'contract': None, 'family': None, 'input_size': None}]
    identity = {'source_commit': 'a'*40, 'base_commit': 'a'*40, 'observer_commit': 'c'*40,
                'observer_digest': cost.digest('observer'), 'plan_digest': cost.digest('plan'),
                'contract_digest': cost.digest('contract'), 'policy_digest': cost.digest(policy),
                'inventory_digest': cost.digest([case]), 'environment_digest': cost.digest('environment'),
                'cache_state': 'cold', 'topology': 'serial', 'stage': 'bootstrap', 'attempt': 'base',
                'node': None, 'artifact_digest': cost.digest('base-tree')}
    owner = 'case:' + json.dumps(case, sort_keys=True, separators=(',', ':'))
    base = {'schema_version': 'test-cost-observation.v1', 'identity': identity, 'complete': True,
            'unknown': [], 'totals': cost.zeros(), 'owners': [{'owner': owner, 'counts': cost.zeros()}],
            'cases': [{'case': case, 'owner': owner, 'counts': cost.zeros(), 'family': None, 'input_size': None}]}
    enrollment = cost.enroll_baseline(base, census, runtime, owner='team', reason='Explicit fixture debt', revisit='Review')
    census['source_commit'] = 'b'*40
    base = copy.deepcopy(base);base['identity']['stage'] = 'full'
    candidate = copy.deepcopy(base)
    candidate['identity'].update(source_commit='b'*40, attempt='candidate', artifact_digest=cost.digest('candidate-tree'))
    context = {'expected_identity': copy.deepcopy(candidate['identity']), 'baseline': enrollment,
        'expected_baseline_digest': cost.digest(enrollment), 'runtime': runtime, 'census': census,
        'policy': policy, 'now': '2026-09-13T12:00:00Z', 'base_observation': base,
        'expected_base_identity': copy.deepcopy(base['identity'])}
    context['timing_pairs'] = [{'baseline_identity': copy.deepcopy(base['identity']),
       'candidate_identity': copy.deepcopy(candidate['identity']), 'baseline_seconds': 100, 'candidate_seconds': 100}
       for _ in range(3)]
    for index, pair in enumerate(context['timing_pairs']):
        pair['baseline_sample_digest'] = cost.digest(['base sample', index])
        pair['candidate_sample_digest'] = cost.digest(['candidate sample', index])
    return candidate, context


def cost_counts(observation, resource, value):
    result = copy.deepcopy(observation)
    result['cases'][0]['counts'][resource] = value
    result['owners'][0]['counts'][resource] = value
    result['totals'][resource] = value
    return result


@cost.declaration(_COST_CONTRACT)
class CostAssessment(unittest.TestCase):
    def test_existing_ci_report_retains_metrics_and_refuses_unrelated_cost_evidence(self):
        observation, context = cost_example()
        left = report(ReportAPI());right = copy.deepcopy(left)
        left['head'] = 'a'*40;right['head'] = 'b'*40
        for document in (left, right):
            document['cases'][0]['id'] = observation['cases'][0]['case']['id']
        value = performance.compare_reports(left, right, cost_evidence={'observation': observation, **context})
        self.assertEqual(value['cost_assessment']['status'], 'acceptable')
        self.assertEqual(value['raw_samples']['candidate'], right['metrics'])
        right['head'] = 'd'*40
        value = performance.compare_reports(left, right, cost_evidence={'observation': observation, **context})
        self.assertEqual(value['cost_assessment']['status'], 'unavailable')
        self.assertFalse(value['qualified'])

    @cost.declaration({**_COST_CONTRACT, 'boundary': 'filesystem',
        'boundary_rationale': 'Verify written reports, live clock consumption and stale-file refusal',
        'mutable_state': 'Fresh bounded input/output files in a temporary directory'})
    def test_cli_distinguishes_clear_violation_and_review_and_invalidates_old_output(self):
        observation, context = cost_example()
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'input.json', Path(directory) / 'output.json'
            for current, settings, expected in ((observation, context, 0),
                (cost_counts(observation, 'processes', 1), context, 1),
                (observation, {**context, 'base_observation': None}, 2)):
                source.write_text(json.dumps({'observation': current, **settings}))
                with mock.patch.object(performance.moment, 'utc_now', return_value=context['now']):
                    self.assertEqual(performance.main(['assess-cost', '--input', str(source), '--output', str(output)]), expected)
                value = json.loads(output.read_text())
                self.assertEqual(value['cost_clearance'], expected == 0)
            context['exceptions'] = [{'id': 'temporary', 'owner': 'team', 'reason': 'Fixture repair',
                'revisit': 'Repair', 'supported_fault': 'Real boundary', 'fault_preservation_evidence': 'Regression retained',
                'issued_at': '2026-09-13T00:00:00Z', 'expires_at': '2026-09-14T00:00:00Z',
                'source_commit': 'b'*40, 'case': observation['cases'][0]['case'], 'resource': 'processes', 'ceiling': 1}]
            source.write_text(json.dumps({'observation': cost_counts(observation, 'processes', 1), **context}))
            with mock.patch.object(performance.moment, 'utc_now', return_value='2026-09-14T00:00:00Z'):
                self.assertEqual(performance.main(['assess-cost', '--input', str(source), '--output', str(output)]), 1)
            self.assertEqual(json.loads(output.read_text())['exceptions'], [])
            source.write_text('{broken')
            with mock.patch('sys.stdout', new=io.StringIO()):
                self.assertEqual(performance.main(['assess-cost', '--input', str(source), '--output', str(output)]), 1)
            with self.assertRaises(ValueError):
                records.validate_shape(json.loads(output.read_text()), 'test-cost-assessment.v1')

    def test_bootstrap_retains_new_source_checks_without_learning_legacy_limits(self):
        observation, context = cost_example()
        source = {'schema_version': 'test-cost-inventory.v1', 'source_commit': 'a'*40,
            'source': {'tree': 'c'*40, 'worktree_dirty': False}, 'runtime': context['runtime'],
            'census': {**context['census'], 'source_commit': 'a'*40}}
        legacy = cost.identity_only_inventory(source, expected_commit='a'*40, expected_tree='c'*40)
        self.assertNotIn('limits', legacy['cases'][0])
        context.update(baseline=None, expected_baseline_digest=None, bootstrap_inventory=legacy)
        self.assertEqual(performance.compare_cost(observation, **context)['status'], 'unavailable')
        context['census'] = cost.static_census({'test_example.py': 'class C:\n def test_behavior(self): return 2\n'}, 'b'*40)
        result = performance.compare_cost(observation, **context)
        self.assertTrue(any('declaration' in finding for finding in result['violations']))
        with self.assertRaises(ValueError):
            cost.identity_only_inventory(source, expected_commit='d'*40, expected_tree='c'*40)

    def test_shared_cost_growth_fails_with_identical_case_source_and_fast_timing(self):
        observation, context = cost_example()
        self.assertEqual(performance.compare_cost(observation, **context)['status'], 'acceptable')
        changed = cost_counts(observation, 'processes', 1)
        result = performance.compare_cost(changed, **context)
        self.assertEqual(result['status'], 'concerns')
        self.assertFalse(result['cost_clearance'])
        self.assertTrue(any('processes' in finding for finding in result['violations']))
        self.assertEqual(result['case_deltas'][0]['delta']['processes'], 1)
        self.assertEqual(result['common'], [observation['cases'][0]['case']])
        self.assertEqual(result['timing']['status'], 'acceptable')

    def test_renamed_cases_remain_added_and_removed_and_need_fault_preservation(self):
        observation, context = cost_example()
        old = copy.deepcopy(observation['cases'][0]['case'])
        new = {**old, 'id': 'test_example.C.test_renamed'}
        owner = 'case:'+json.dumps(new, sort_keys=True, separators=(',', ':'))
        observation['cases'][0].update(case=new, owner=owner)
        observation['owners'][0]['owner'] = owner
        observation['identity']['inventory_digest'] = cost.digest([new])
        context['expected_identity'] = copy.deepcopy(observation['identity'])
        context['runtime'][0].update(new)
        context['runtime'][0].update(qualified_name='C.test_renamed', contract=_COST_CONTRACT)
        context['census'] = cost.static_census({'test_example.py': 'class C:\n def test_renamed(self): pass\n'}, 'b'*40)
        result = performance.compare_cost(observation, **context)
        self.assertEqual(result['added'], [new]);self.assertEqual(result['removed'], [old])
        self.assertTrue(any('removed case lacks' in finding for finding in result['violations']))
        context['mappings'] = [{'source': old, 'target': new, 'path': 'test_example.py',
            'qualified_name': 'C.test_renamed', 'reason': 'Rename only', 'supported_fault': 'The same boundary remains tested'}]
        self.assertEqual(performance.compare_cost(observation, **context)['violations'], [])

    def test_wrong_source_attempt_policy_and_omitted_base_are_unavailable(self):
        observation, context = cost_example()
        for field, value in [('source_commit', 'd'*40), ('attempt', 'other'),
                             ('artifact_digest', cost.digest('wrong-tree')), ('policy_digest', cost.digest('wrong-policy')),
                             ('contract_digest', cost.digest('wrong-contract')), ('topology', 'parallel')]:
            with self.subTest(field=field):
                changed = copy.deepcopy(observation);changed['identity'][field] = value
                self.assertEqual(performance.compare_cost(changed, **context)['status'], 'unavailable')
        for change in ({'base_observation': None}, {'expected_baseline_digest': cost.digest('changed-budget')},
                       {'census': {**context['census'], 'source_commit': 'd'*40}}):
            result = performance.compare_cost(observation, **{**context, **change})
            self.assertEqual(result['status'], 'unavailable')
            self.assertFalse(result['cost_clearance'])
        observation['complete'] = False
        self.assertEqual(performance.compare_cost(observation, **context)['status'], 'unavailable')

    def test_environment_cache_and_scope_mismatch_cannot_borrow_qualified_comparison(self):
        observation, context = cost_example()
        for field, value in [('environment_digest', cost.digest('other-host')), ('cache_state', 'warm'), ('stage', 'node-focused')]:
            with self.subTest(field=field):
                changed = copy.deepcopy(context)
                changed['base_observation']['identity'][field] = value
                changed['expected_base_identity'][field] = value
                result = performance.compare_cost(observation, **changed)
                self.assertEqual(result['status'], 'unavailable')
                self.assertTrue(any(field in reason for reason in result['unknown']))

    def test_expiry_reassesses_the_same_observation_without_renewing_permission(self):
        observation, context = cost_example();observation = cost_counts(observation, 'processes', 2)
        exception = {'id': 'repair', 'owner': 'team', 'reason': 'Bounded real process fixture',
            'revisit': 'Repair fixture', 'supported_fault': 'Real process boundary',
            'fault_preservation_evidence': 'The unchanged process regression remains',
            'issued_at': '2026-09-13T00:00:00Z', 'expires_at': '2026-09-14T00:00:00Z',
            'source_commit': 'b'*40, 'case': observation['cases'][0]['case'], 'resource': 'processes', 'ceiling': 2}
        context['exceptions'] = [exception];before = cost.digest(observation)
        result = performance.compare_cost(observation, **context)
        self.assertEqual(result['status'], 'acceptable');self.assertEqual(result['exceptions'], [exception])
        context['now'] = '2026-09-14T00:00:00Z'
        self.assertEqual(performance.compare_cost(observation, **context)['status'], 'concerns')
        self.assertEqual(cost.digest(observation), before)
        repaired = cost_counts(observation, 'processes', 0)
        result = performance.compare_cost(repaired, **context)
        self.assertEqual(result['status'], 'acceptable');self.assertEqual(result['exceptions'], [])

    def test_scaling_uses_count_growth_and_requires_every_declared_size(self):
        family = {'id': 'scan', 'case_patterns': ['case-*'], 'input_sizes': [1, 2, 4],
                  'growth_limits': {**cost.zeros(), 'schema_decodes': 2}}
        contract = {**_COST_CONTRACT, 'families': [family]}
        cases = [{'case': {'id': 'case-'+str(n), 'occurrence': 1}, 'family': 'scan', 'input_size': n,
                  'counts': {**cost.zeros(), 'schema_decodes': n}} for n in family['input_sizes']]
        self.assertEqual(cost.scaling_assessment(cases, {('case', 1): contract})[1:], ([], []))
        cases[-1]['counts']['schema_decodes'] = 16
        self.assertTrue(cost.scaling_assessment(cases, {('case', 1): contract})[1])
        self.assertTrue(cost.scaling_assessment(cases[:-1], {('case', 1): contract})[2])

    def test_timing_keeps_measured_noise_and_remains_advisory(self):
        observation, context = cost_example()
        for pair, base, candidate in zip(context['timing_pairs'], [100, 102, 101], [103, 105, 104]):
            pair.update(baseline_seconds=base, candidate_seconds=candidate)
        result = performance.compare_cost(observation, **context)
        self.assertEqual(result['violations'], [])
        self.assertEqual(result['timing']['noise_envelope_seconds'], 2)
        self.assertTrue(result['timing']['material_growth'])
        self.assertEqual(result['status'], 'concerns')
        context['timing_pairs'][1]['candidate_seconds'] = 102
        self.assertFalse(performance.compare_cost(observation, **context)['timing']['material_growth'])
        context['timing_pairs'] = []
        self.assertEqual(performance.compare_cost(observation, **context)['status'], 'unavailable')

    def test_ambient_legacy_debt_cannot_authorize_new_or_declared_effects(self):
        observation, context = cost_example()
        context['runtime'][0].pop('contract', None)
        fact = {'owner': observation['cases'][0]['owner'], 'kind': 'git-config'}
        observation['ambient_facts'] = [fact]
        self.assertTrue(any('ambient Git configuration' in v for v in cost.assess_cost(
            observation, **context)['violations']))
        context['baseline']['ambient_facts'] = [fact]
        context['expected_baseline_digest'] = cost.digest(context['baseline'])
        result = cost.assess_cost(observation, **context)
        self.assertEqual([], result['violations'])
        self.assertTrue(any('enrolled ambient Git debt' in v for v in result['unknown']))
        context['runtime'][0]['contract'] = copy.deepcopy(_COST_CONTRACT)
        self.assertTrue(any('ambient Git configuration' in v for v in cost.assess_cost(
            observation, **context)['violations']))
        context['runtime'][0].pop('contract')
        context['bootstrap_inventory'] = context['baseline']
        context['baseline'] = None
        result = cost.assess_cost(observation, **context)
        self.assertEqual([], result['violations'])
        self.assertFalse(result['cost_clearance'])
        self.assertTrue(any('ambient Git baseline unavailable' in v for v in result['unknown']))
        context['runtime'][0]['contract'] = copy.deepcopy(_COST_CONTRACT)
        self.assertTrue(any('ambient Git configuration' in v for v in cost.assess_cost(
            observation, **context)['violations']))

    def test_observed_long_duration_requires_disclosure_without_a_comparable_pair(self):
        observation, context = cost_example()
        context['timing_pairs'] = []
        context['candidate_duration_seconds'] = 1200
        result = performance.compare_cost(observation, **context)
        self.assertEqual(result['status'], 'concerns')
        self.assertTrue(any('observed candidate duration' in finding for finding in result['timing_findings']))

    def test_repeating_one_timing_sample_does_not_create_qualification(self):
        observation, context = cost_example()
        context['timing_pairs'] = [context['timing_pairs'][0]] * 3
        result = performance.compare_cost(observation, **context)
        self.assertEqual(result['status'], 'unavailable')
        self.assertTrue(any('reused' in reason for reason in result['timing']['reasons']))

    def test_fixture_growth_cannot_hide_in_unchanged_case_totals(self):
        observation, context = cost_example()
        fixture = {'owner': 'fixture:_handleClassSetUp:fixture.C', 'counts': {**cost.zeros(), 'git_commands': 1}}
        for document in (context['baseline'], context['base_observation'], observation):
            document['owners'].append(copy.deepcopy(fixture));document['totals']['git_commands'] = 1
        context['expected_baseline_digest'] = cost.digest(context['baseline'])
        observation['owners'][-1]['counts']['git_commands'] = 2;observation['totals']['git_commands'] = 2
        result = performance.compare_cost(observation, **context)
        self.assertTrue(any('fixture:' in finding for finding in result['violations']))
        self.assertEqual(result['owner_deltas'][0]['delta']['git_commands'], 1)
        self.assertEqual(result['case_deltas'][0]['delta']['git_commands'], 0)

    @cost.declaration({**_COST_CONTRACT, 'boundary': 'process',
        'boundary_rationale': 'Measure a real helper-only process regression through the existing serial launcher',
        'dependencies': ['git', 'subprocess', 'selftest', 'selftest_cost', 'selftest_results'],
        'data_reads': ['.engine/policies/test-cost*.json', '.engine/schemas/test-cost-*.json', '.engine/tools/selftest.py'],
        'limits': {**cost.zeros(), 'processes': 20, 'git_commands': 16, 'schema_decodes': 100, 'nested_journeys': 2},
        'mutable_state': 'Disposable Git repository and two separately retained launcher outputs'})
    def test_real_helper_change_keeps_test_source_and_raises_a_resource_finding(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory);root = folder / 'repo';root.mkdir()
            env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
            env.update(HOME=str(folder), GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
            def git(*args, binary=False):
                return subprocess.check_output(['git', '-C', str(root), *args], env=env,
                                               stderr=subprocess.DEVNULL, text=not binary)
            git('init', '-q');git('config', 'user.email', 'fixture@example.invalid');git('config', 'user.name', 'Fixture')
            test_source = 'import unittest, helper\nclass C(unittest.TestCase):\n def test_behavior(self): helper.run()\n'
            (root / 'test_example.py').write_text(test_source)
            (root / '.gitignore').write_text('__pycache__/\n')
            (root / 'helper.py').write_text('def run(): pass\n')
            script = ('import functools,sys\nsys.path.insert(0,sys.argv[1])\n'
                      'import selftest_cost as cost, selftest\n'
                      'cost.runtime_inventory=functools.partial(cost.runtime_inventory,root=sys.argv[2])\n'
                      'raise SystemExit(selftest.main(["--child","--start-dir",sys.argv[2],'
                      '"--cost-path",sys.argv[3],"--results-path",sys.argv[4],"--performance-path",sys.argv[5]]))')
            reports = []
            for index in range(2):
                if index:
                    (root / 'helper.py').write_text('import subprocess,sys\ndef run(): subprocess.run([sys.executable,"-c","pass"],check=True)\n')
                git('add', '.');git('commit', '-qm', 'Fixture version '+str(index))
                head = git('rev-parse', 'HEAD').strip();tree = git('rev-parse', 'HEAD^{tree}').strip()
                paths = [folder / (str(index)+'-'+name+'.json') for name in ('cost', 'outcomes', 'performance')]
                run = subprocess.run([sys.executable, '-c', script, str(cost.ROOT / '.engine/tools'), str(root),
                                      *map(str, paths)], env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(run.returncode, 0, run.stdout+run.stderr)
                raw, outcomes, timing = [json.loads(path.read_text()) for path in paths]
                artifact = 'sha256:'+hashlib.sha256(git('ls-tree', '-r', '--full-tree', '-z', head, binary=True)).hexdigest()
                reports.append((head, tree, artifact, raw, outcomes, timing))
            self.assertEqual((root / 'test_example.py').read_text(), test_source)
            self.assertEqual(git('diff', reports[0][0], reports[1][0], '--', 'test_example.py'), '')
            observation, context = cost_example()
            base_head, _, _, _, _, _ = reports[0]
            activation = json.loads((cost.ROOT / '.engine/policies/test-cost-activation.json').read_text())
            normalized = []
            for head, tree, artifact, raw, outcomes, timing in reports:
                identity = {**context['expected_identity'], 'source_commit': head, 'base_commit': base_head,
                    'observer_commit': activation['identity']['observer_commit'], 'observer_digest': cost.observer_fingerprint(),
                    'environment_digest': cost.digest(timing['environment']), 'cache_state': timing['environment']['cache'],
                    'artifact_digest': artifact, 'inventory_digest': cost.digest(outcomes['inventory'])}
                normalized.append(cost.normalize_run(raw, identity, expected_tree=tree, outcomes=outcomes))
            base, observation = normalized
            census = cost.static_census({'test_example.py': test_source}, base_head)
            bootstrap = copy.deepcopy(base);bootstrap['identity']['stage'] = 'bootstrap'
            enrollment = cost.enroll_baseline(bootstrap, census, reports[0][3]['inventory'],
                                              owner='fixture', reason='Measured original helper', revisit='This regression')
            census['source_commit'] = reports[1][0]
            context.update(expected_identity=observation['identity'], baseline=enrollment,
                expected_baseline_digest=cost.digest(enrollment), runtime=reports[1][3]['inventory'], census=census,
                base_observation=base, expected_base_identity=base['identity'], timing_pairs=[])
            result = performance.compare_cost(observation, **context)
            self.assertEqual(result['status'], 'concerns')
            self.assertTrue(any('processes' in finding for finding in result['violations']))
            self.assertEqual(result['case_deltas'][0]['delta']['processes'], 1)
            self.assertIn('descendant work is not instrumented', result['unknown'])


if __name__=='__main__':unittest.main()
