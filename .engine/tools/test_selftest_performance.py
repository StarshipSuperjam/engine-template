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


if __name__=='__main__':unittest.main()
