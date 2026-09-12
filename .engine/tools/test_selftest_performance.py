"""Bounded performance publication cannot control CI verdicts or interpret authored markup."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selftest_performance as performance
import selftest_results as records


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
