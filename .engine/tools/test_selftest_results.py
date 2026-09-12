"""Small real unittest journeys proving complete accounting, without repository fixtures."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import selftest
import selftest_results as records


def run_example(root, stop_early):
    """The operator demonstration and permanent regression share one tiny real launcher journey."""
    (root/'test_small.py').write_text('''import unittest
class A(unittest.TestCase):
    def test_a(self): pass
    def run(self, result=None):
        super().run(result)
        if STOP_EARLY: result.stop()
class B(unittest.TestCase):
    def test_b(self): pass
'''.replace('STOP_EARLY',repr(stop_early)))
    output=root/'results.json';legacy=root/'record.json';timing=root/'timing.json'
    command=[sys.executable,str(Path(selftest.__file__).resolve()),'--start-dir',str(root),'--cwd',str(root),
             '--results-path',str(output),'--run-record-path',str(legacy),'--performance-path',str(timing)]
    run=subprocess.run(command,capture_output=True,text=True,timeout=15)
    return run,records.read(output),records.read(legacy),records.read(timing)


def demonstrate(stop_early=False):
    with tempfile.TemporaryDirectory(prefix='engine-outcome-demo-') as tmp:
        run,result,legacy,timing=run_example(Path(tmp),stop_early)
    print('Early stopping: '+('on' if stop_early else 'off'))
    print(f"Selected {len(result['selected'])} cases; started {result['executed_count']}; launcher exit {run.returncode}.")
    print('Outcomes: '+', '.join(row['outcome'] for row in result['cases']))
    print('Complete: '+str(result['complete'])+'; legacy observed count: '+str(legacy['executed']['case_count']))
    expected=(run.returncode==(1 if stop_early else 0) and result['complete']==(not stop_early)
              and result['executed_count']==(1 if stop_early else 2))
    print('Demonstration '+('passed.' if expected else 'FAILED: the launcher did not enforce the expected outcome boundary.'))
    return 0 if expected else 1


class ResultAccounting(unittest.TestCase):
    def observe(self, cases, *, timing=False, inventory=None):
        obs = records.Observation(inventory or cases, cases, source={"tree": None, "worktree_dirty": None},
                                  scope="full", invocation={"start_dir": "tools", "pattern": "test_*.py",
                                                            "selection_digest": None}, timing=timing)
        ref = [None]
        def factory(*args, **kwargs):
            ref[0] = selftest._StructuredResult(*args, observation=obs, **kwargs)
            return ref[0]
        with obs.phases(cases, ref):
            result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=factory).run(unittest.TestSuite(cases))
        return obs, result, obs.document(True)

    def test_mixed_outcomes_match_stock_unittest_and_keep_subtests(self):
        class Cases(unittest.TestCase):
            def test_pass(self): pass
            def test_fail(self): self.fail("fault")
            def test_error(self): raise RuntimeError("fault")
            @unittest.skip("supported platform skip")
            def test_skip(self): pass
            @unittest.expectedFailure
            def test_expected(self): self.fail("expected")
            @unittest.expectedFailure
            def test_unexpected(self): pass
            def test_subtests(self):
                for i in range(3):
                    with self.subTest(i=i):
                        if i == 1: self.skipTest("one subcase")
                        self.assertNotEqual(i, 2)
        cases = list(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
        _, observed, doc = self.observe(cases)
        stock = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
        for name in ("testsRun", "wasSuccessful"):
            a,b=getattr(observed,name),getattr(stock,name)
            self.assertEqual(a() if callable(a) else a,b() if callable(b) else b)
        self.assertEqual([r["outcome"] for r in doc["cases"]],
                         ["error", "expected-failure", "failed", "passed", "skipped", "failed", "unexpected-success"])
        sub = doc["cases"][5]["subtests"]
        self.assertEqual([r["outcome"] for r in sub], ["passed", "skipped", "failed"])
        self.assertEqual(records.validate(doc), (True, False))

    def test_clean_stop_is_incomplete_even_when_unittest_says_success(self):
        class Stop(unittest.TestCase):
            def runTest(self): pass
            def run(self, result=None):
                super().run(result)
                result.stop()
        class Later(unittest.TestCase):
            def runTest(self): pass
        _, result, doc = self.observe([Stop(), Later()])
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(doc["executed_count"], 1)
        self.assertEqual(doc["cases"][1]["outcome"], "unexecuted")
        self.assertEqual(records.validate(doc), (False, False))

    def test_fixture_skip_maps_all_cases_without_claiming_execution(self):
        class Cases(unittest.TestCase):
            @classmethod
            def setUpClass(cls): raise unittest.SkipTest("optional platform")
            def test_a(self): pass
            def test_b(self): pass
        _, result, doc = self.observe(list(unittest.defaultTestLoader.loadTestsFromTestCase(Cases)))
        self.assertEqual(result.testsRun, 0)
        self.assertEqual(doc["fixtures"][0]["affected"], [0,1])
        self.assertEqual([r["outcome"] for r in doc["cases"]], ["skipped", "skipped"])
        self.assertEqual(records.validate(doc), (True, True))

    def test_fixture_error_is_not_success_and_cleanup_still_runs(self):
        cleaned=[]
        class Cases(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                cls.addClassCleanup(lambda: cleaned.append(True))
                raise RuntimeError("fault")
            def runTest(self): pass
        _, result, doc = self.observe([Cases()])
        self.assertEqual(cleaned,[True])
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(doc["cases"][0]["outcome"], "fixture-blocked")
        self.assertEqual(records.validate(doc), (True, False))

    def test_repeated_class_groups_do_not_invent_skips_in_later_group(self):
        class A(unittest.TestCase):
            entries=0
            @classmethod
            def setUpClass(cls):
                cls.entries+=1
                if cls.entries==1: raise unittest.SkipTest("first group only")
            def runTest(self): pass
        class B(unittest.TestCase):
            def runTest(self): pass
        repeated=A()
        _,result,doc=self.observe([repeated,B(),repeated])
        self.assertEqual(result.testsRun,2)
        self.assertEqual([r['outcome'] for r in doc['cases']],['skipped','passed','passed'])
        self.assertEqual(doc['fixtures'][0]['affected'],[0])
        self.assertEqual(records.validate(doc),(True,True))

    def test_failed_setup_and_failed_class_cleanup_are_separate_mapped_errors(self):
        class Cases(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                cls.addClassCleanup(lambda: (_ for _ in ()).throw(ValueError('cleanup')))
                raise RuntimeError('setup')
            def runTest(self): pass
        _,result,doc=self.observe([Cases()])
        self.assertEqual(len(result.errors),2)
        self.assertEqual([f['affected'] for f in doc['fixtures']],[[0],[0]])
        self.assertEqual([f['cleanup'] for f in doc['fixtures']],[False,True])
        self.assertEqual(records.validate(doc),(True,False))

    def test_duplicate_ids_are_distinct_occurrences(self):
        class Cases(unittest.TestCase):
            def runTest(self): pass
        case=Cases()
        _, result, doc=self.observe([case,case,Cases()])
        self.assertEqual(result.testsRun,3)
        self.assertEqual([r["occurrence"] for r in doc["cases"]],[1,2,3])
        self.assertEqual(records.validate(doc),(True,True))

    def test_missing_duplicate_and_forged_green_reports_are_rejected(self):
        class Cases(unittest.TestCase):
            def runTest(self): pass
        _, _, valid = self.observe([Cases(),Cases()])
        for mutate in [lambda d:d["cases"].pop(),
                       lambda d:d["cases"].append(copy.deepcopy(d["cases"][0])),
                       lambda d:d["cases"][0].update(outcome="unexecuted"),
                       lambda d:d["cases"][0].update(started=False,stopped=False)]:
            with self.subTest(mutate=mutate):
                bad=copy.deepcopy(valid);mutate(bad)
                with self.assertRaises(ValueError): records.validate(bad)

    def test_timing_preserves_hooks_and_reports_inclusive_phases(self):
        steps=[]
        class Cases(unittest.TestCase):
            def setUp(self): steps.append("setup"); self.addCleanup(steps.append,"cleanup")
            def runTest(self): steps.append("body")
            def tearDown(self): steps.append("teardown")
        case=Cases(); original=case._callTestMethod
        obs,_,doc=self.observe([case],timing=True)
        self.assertEqual(steps,["setup","body","teardown","cleanup"])
        self.assertEqual(case._callTestMethod,original)
        self.assertGreater(doc["cases"][0]["seconds"],0)
        self.assertTrue({"_callSetUp","_callTestMethod","_callTearDown","_callCleanup"} <= {s["phase"] for s in obs.spans})

    def test_parent_rejects_crashed_and_truncated_documents(self):
        class Cases(unittest.TestCase):
            def runTest(self): pass
        _,_,doc=self.observe([Cases()])
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"result.json"
            records.write(path,doc)
            self.assertFalse(records.finalize(path,-9))
            path.write_text('{"schema_version":')
            self.assertFalse(records.finalize(path,0))

    def test_required_string_bound_and_path_redaction(self):
        with self.assertRaises(ValueError): records.text("x"*(records.MAX_STRING+1))
        self.assertEqual(records.text("skip /Users/private/key.txt"),"skip [path]")

    def test_artifact_byte_limit_and_focused_missing_reports_fail(self):
        class Cases(unittest.TestCase):
            def runTest(self): pass
        _,_,doc=self.observe([Cases(),Cases()])
        doc["scope"]="focused"
        doc["cases"].pop()
        with self.assertRaises(ValueError): records.validate(doc)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(records,"MAX_BYTES",32):
            path=Path(tmp)/"results.json"
            with self.assertRaises(ValueError): records.write(path,{"x":"a"*33})
            path.write_text("x"*33)
            self.assertFalse(records.finalize(path,0))

    def test_module_fixture_error_and_skip_are_mapped_in_actual_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name,exc in [("error","RuntimeError"),("skip","unittest.SkipTest")]:
                (root/f"test_{name}.py").write_text(
                    f"import unittest\ndef setUpModule(): raise {exc}('fixture')\n"
                    "class T(unittest.TestCase):\n    def test_a(self): pass\n    def test_b(self): pass\n")
            output=root/'results.json'
            run=subprocess.run([sys.executable,selftest.__file__,'--start-dir',tmp,'--cwd',tmp,
                                '--results-path',str(output)],capture_output=True,text=True,timeout=15)
            self.assertEqual(run.returncode,1,run.stdout+run.stderr)
            doc=records.read(output)
            self.assertEqual(records.validate(doc),(True,False))
            self.assertEqual(doc['executed_count'],0)
            self.assertEqual([r['outcome'] for r in doc['cases']],['fixture-blocked']*2+['skipped']*2)

    def test_actual_launcher_rejects_clean_early_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            run,result,legacy,timing=run_example(Path(tmp),True)
            self.assertEqual(run.returncode,1,run.stdout+run.stderr)
            self.assertFalse(result['complete'])
            self.assertEqual(result['process_exit'],1)
            self.assertEqual(legacy['executed']['case_count'],1)
            self.assertIsNotNone(timing['parent_seconds'])
            records.validate_shape(timing,'selftest-performance.v1')

    def test_actual_launcher_releases_completed_cases_and_isolates_session_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'test_lifetime.py').write_text('''import gc, os, unittest, weakref
reference = None
class A(unittest.TestCase):
    def test_a(self):
        global reference
        reference = weakref.ref(self)
class B(unittest.TestCase):
    def test_b(self):
        gc.collect()
        self.assertIsNone(reference())
        self.assertNotIn('ENGINE_SESSION_ID', os.environ)
        self.assertNotIn('CLAUDE_CODE_SESSION_ID', os.environ)
''')
            for timing in (False, True):
                command = [sys.executable, str(Path(selftest.__file__).resolve()),
                           '--start-dir', tmp, '--cwd', tmp, '--results-path', str(root/'result.json')]
                if timing:
                    command += ['--performance-path', str(root/'timing.json')]
                run = subprocess.run(command, capture_output=True, text=True, timeout=15,
                                     env={**os.environ, 'ENGINE_SESSION_ID':'ambient-engine',
                                          'CLAUDE_CODE_SESSION_ID':'ambient-claude'})
                self.assertEqual(run.returncode, 0, run.stdout+run.stderr)
                self.assertEqual(records.validate(records.read(root/'result.json')), (True,True))


if __name__ == '__main__':
    if '--demonstrate' in sys.argv:
        import argparse
        parser=argparse.ArgumentParser(description='Watch complete runs and clean early stops through the real launcher.')
        parser.add_argument('--demonstrate',action='store_true')
        parser.add_argument('--stop-early',action='store_true')
        raise SystemExit(demonstrate(parser.parse_args().stop_early))
    unittest.main()
