"""Regression tests for assessment, independent assignment and issue-owned recovery."""
import copy
import json
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
            if state != 'disabled':r['assignment']['milestone']=16
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



class Discovery(unittest.TestCase):
    settings={'activated_at':'2026-09-10T00:00:00Z',
              'milestones':{'none':19,'patch':16,'minor':17,'major':None}}

    def issue(self, body='', created='2026-09-11T00:00:00Z'):
        return {'number':1,'labels':[{'name':'engine'}],'body':body,'created_at':created}

    def test_deleted_section_stays_enrolled_by_creation_or_label_history(self):
        self.assertEqual(triage.enrollment(self.issue(),self.settings),'required')
        old=self.issue(created='2026-09-01T00:00:00Z')
        self.assertEqual(triage.enrollment(old,self.settings,[]),'legacy')
        events=[{'event':'labeled','label':{'name':'engine'},'created_at':'2026-09-11T00:00:00Z'}]
        self.assertEqual(triage.enrollment(old,self.settings,events),'required')
        self.assertEqual(triage.enrollment(old,self.settings),'unknown')
        self.assertEqual(triage.enrollment({**old,'labels':[]},self.settings,events),'out-of-scope')

    def test_discovery_finds_assessed_unassigned_and_excludes_human(self):
        issue=self.issue(triage.render(record()))
        class Client:
            repo='o/r'
            def _transport(self,*args):return 200,[issue,{**issue,'number':2,'labels':[]}]
        config={'schema_version':'operator-issue-triage.v1','repositories':{'o/r':self.settings}}
        result=triage.discover(Client(),config)
        self.assertTrue(result['complete']);self.assertEqual([x['number'] for x in result['items']],[1])
        self.assertEqual(result['items'][0]['record']['assessment']['state'],'assessed')

    def test_unavailable_read_is_not_empty_success(self):
        class Client:
            repo='o/r'
            def _transport(self,*args):return 403,None
        result=triage.discover(Client(),None)
        self.assertFalse(result['complete']);self.assertIn('403',result['error'])

    def test_pre_activation_record_with_any_surviving_marker_remains_visible(self):
        for retained in (triage.START, triage.END, 'engine-issue-triage-data: {}'):
            with self.subTest(retained=retained):
                issue = self.issue('Human text\n' + retained, created='2026-09-01T00:00:00Z')
                self.assertEqual(triage.enrollment(issue, self.settings, []), 'required')
                class Client:
                    repo = 'o/r'
                    def _transport(self, *args):
                        return 200, [issue]
                config = {'schema_version':'operator-issue-triage.v1','repositories':{'o/r':self.settings}}
                result = triage.discover(Client(), config)
                self.assertTrue(result['complete'])
                self.assertEqual(result['items'][0]['number'], 1)
                self.assertIsNotNone(result['items'][0]['error'])

    def test_slow_read_cannot_hold_discovery_past_budget_or_continue_pagination(self):
        import threading
        import time
        release = threading.Event()
        finished = threading.Event()
        calls = []
        class Client:
            repo = 'o/r'
            def _transport(self, *args):
                calls.append(args)
                release.wait(5)
                finished.set()
                return 200, [{}] * 100
        try:
            started = time.monotonic()
            result = triage.discover(Client(), None, max_seconds=0.05)
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(result['complete'])
            self.assertIn('budget', result['error'])
        finally:
            release.set()
            self.assertTrue(finished.wait(2))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['items'], [])

    def test_configuration_distinguishes_disabled_from_missing_and_is_preserved(self):
        import module_coherence
        config={'schema_version':'operator-issue-triage.v1','repositories':{'o/r':self.settings}}
        self.assertIsNone(triage.repo_config(config,'different/repo'))
        self.assertIsNone(triage.repo_config(config,'O/R')['milestones']['major'])
        self.assertIn(triage.CONFIG_NAME,module_coherence.OPERATOR_CONFIG)
        with self.assertRaises(triage.TriageError):
            triage.validate_config({'schema_version':'operator-issue-triage.v1','repositories':{'o/r':{**self.settings,'activated_at':'bad'}}})

    def test_reconciliation_includes_closed_issues_and_refuses_corrupt_records(self):
        paths=[];issue=self.issue(triage.render(record()));issue['state']='closed'
        class Client:
            repo='o/r'
            def _transport(self,method,path,body): paths.append(path);return 200,[issue]
        self.assertEqual(triage.matching_submission(Client(),'test-operation-1'),[issue])
        self.assertIn('state=all',paths[0])
        issue['body']=triage.START+'corrupt'+triage.END
        with self.assertRaises(triage.TriageError):triage.matching_submission(Client(),'test-operation-1')


class FakeGitHub:
    repo='o/r'
    def __init__(self):
        self.issues=[];self.calls=[];self.lookup_status=200;self.post_status=201
        self.drop_milestone=False;self.timeout_after_create=False;self.before_patch=None
        self.read_count=0;self.before_read=None
    def _transport(self,method,path,data):
        self.calls.append((method,path,copy.deepcopy(data)))
        if '/milestones/' in path:
            return self.lookup_status,{'number':16,'state':'open','title':'Patch'}
        if method=='GET' and '?' in path:
            return 200,copy.deepcopy(self.issues)
        if method=='GET':
            self.read_count+=1
            if self.before_read:self.before_read(self)
            return 200,copy.deepcopy(self.issues[0])
        if method=='POST':
            if self.post_status!=201:return self.post_status,None
            issue={'id':100,'number':1,'html_url':'https://github.com/o/r/issues/1','state':'open',
                   'created_at':'2026-09-11T00:00:00Z',**copy.deepcopy(data)}
            issue['milestone']=None if self.drop_milestone or not data.get('milestone') else {'number':data['milestone']}
            self.issues.append(issue)
            if self.timeout_after_create:raise TimeoutError('response lost')
            return 201,copy.deepcopy(issue)
        if method=='PATCH':
            if self.before_patch:self.before_patch(self)
            self.issues[0].update(copy.deepcopy(data))
            if 'milestone' in data:self.issues[0]['milestone']={'number':data['milestone']}
            return 200,copy.deepcopy(self.issues[0])
        raise AssertionError((method,path))


class Filing(unittest.TestCase):
    config={'schema_version':'operator-issue-triage.v1','repositories':{'o/r':Discovery.settings}}
    def file(self,client,assessment=None):
        return triage.file_issue(client,'Fix: report',triage.with_record('Original human text',record(assessment)),config=self.config)
    def test_known_case_assigns_initial_post_and_confirms_readback(self):
        client=FakeGitHub();result=self.file(client)
        self.assertEqual(result['filing'],'created');self.assertEqual(result['assignment']['state'],'assigned')
        post=next(x for x in client.calls if x[0]=='POST');self.assertEqual(post[2]['milestone'],16)
    def test_lookup_outage_files_and_recovers_without_reclassification(self):
        client=FakeGitHub();client.lookup_status=503;result=self.file(client)
        self.assertEqual(result['filing'],'created');self.assertEqual(result['assignment']['state'],'resolution-failed')
        fresh=triage.discover(client,self.config);self.assertEqual(len(fresh['items']),1)
        client.lookup_status=200
        updated=triage.update_triage(client,1,expected=fresh['items'][0]['record'],config=self.config,now='2026-09-12T00:00:00Z')
        self.assertEqual(updated['state'],'updated');self.assertFalse(updated['outstanding'])
        self.assertEqual(updated['record']['assessment'],assessed())
    def test_pending_health_report_never_guesses_impact(self):
        client=FakeGitHub();result=self.file(client,triage.pending('No known remedy','Investigate regression'))
        self.assertEqual(result['assessment'],'pending')
        self.assertFalse(any('/milestones/' in call[1] for call in client.calls))
    def test_timeout_after_create_reconciles_same_id_without_second_post(self):
        client=FakeGitHub();client.timeout_after_create=True
        result=self.file(client);self.assertEqual(result['filing'],'creation-uncertain')
        client.timeout_after_create=False
        again=triage.file_issue(client,'Fix: report',triage.with_record('Original',record()),config=self.config,retry=True)
        self.assertEqual(again['number'],1)
        self.assertEqual(sum(c[0]=='POST' for c in client.calls),1)
    def test_retry_absence_or_multiple_matches_never_posts(self):
        client=FakeGitHub();result=triage.file_issue(client,'Fix: report',triage.render(record()),config=self.config,retry=True)
        self.assertEqual(result['filing'],'creation-uncertain');self.assertFalse(any(c[0]=='POST' for c in client.calls))
        self.file(client);client.issues.append(copy.deepcopy(client.issues[0]));client.calls=[]
        result=self.file(client);self.assertEqual(result['filing'],'creation-uncertain');self.assertFalse(any(c[0]=='POST' for c in client.calls))
    def test_generic_422_auth_and_server_errors_never_trigger_fallback(self):
        for status in (422,401,403,500):
            client=FakeGitHub();client.post_status=status;result=self.file(client)
            self.assertNotEqual(result['filing'],'created');self.assertEqual(sum(c[0]=='POST' for c in client.calls),1)
    def test_silently_dropped_milestone_remains_discoverable(self):
        client=FakeGitHub();client.drop_milestone=True;result=self.file(client)
        self.assertEqual(result['assignment']['state'],'conflict')
        self.assertEqual(len(triage.discover(client,self.config)['items']),1)
    def test_human_assignment_and_unlabelled_issue_are_preserved(self):
        client=FakeGitHub();client.lookup_status=503;self.file(client);client.issues[0]['milestone']={'number':99}
        before=triage.observed_record(client.issues[0]);client.lookup_status=200
        result=triage.update_triage(client,1,expected=before,config=self.config,now='2026-09-12T00:00:00Z')
        self.assertEqual(result['record']['assignment']['milestone'],99)
        self.assertEqual(client.issues[0]['milestone']['number'],99)
        client.issues[0]['labels']=[];client.calls=[]
        result=triage.update_triage(client,1,expected=before,config=self.config,now='2026-09-12T00:00:00Z')
        self.assertEqual(result['state'],'out-of-scope');self.assertFalse(any(c[0]=='PATCH' for c in client.calls))
    def test_final_preflight_detects_observable_human_edit(self):
        client=FakeGitHub();client.lookup_status=503;self.file(client);old=triage.observed_record(client.issues[0]);client.read_count=0
        def race(c):
            if c.read_count==2:c.issues[0]['body']+='\nNew human text'
        client.before_read=race
        result=triage.update_triage(client,1,expected=old,config=self.config,now='2026-09-12T00:00:00Z')
        self.assertEqual(result['state'],'conflict');self.assertIn('New human text',client.issues[0]['body'])
    def test_accepted_limit_readback_cannot_detect_edit_after_final_read(self):
        client=FakeGitHub();client.lookup_status=503;self.file(client);old=triage.observed_record(client.issues[0])
        def race(c):c.issues[0]['body']+='\nConcurrent human text'
        client.before_patch=race
        result=triage.update_triage(client,1,expected=old,config=self.config,now='2026-09-12T00:00:00Z')
        self.assertEqual(result['state'],'updated');self.assertNotIn('Concurrent human text',client.issues[0]['body'])


class Qualification(unittest.TestCase):
    def test_conflicting_case_variant_configuration_is_rejected(self):
        config = copy.deepcopy(Filing.config)
        config['repositories']['O/R'] = copy.deepcopy(config['repositories']['o/r'])
        config['repositories']['O/R']['milestones']['patch'] = 99
        with self.assertRaisesRegex(triage.TriageError, 'capitalization'):
            triage.validate_config(config)

    def test_corrupt_persisted_prerequisite_does_not_hide_pending_work(self):
        value = record()
        value['disposition'] = {'kind':'defer','at':value['updated_at'], 'evidence':'Inspected mapping.',
                                'missing':'Target release.', 'next_action':'Read configuration.',
                                'prerequisite':{'kind':'milestone-config','observed':'invented'}}
        with self.assertRaisesRegex(triage.TriageError, 'prerequisite'):
            triage.validate(value)

    def test_demo_asserts_real_path_and_deliberately_wrong_expectation_fails(self):
        self.assertEqual(triage.demo()['pending'], 1)
        with self.assertRaisesRegex(AssertionError, 'Expected 0'):
            triage.demo(expected_pending=0)
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(triage.main(['demo', '--expected-pending', '0']), 1)

    def test_two_creators_can_both_observe_absence_and_create(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier, Lock
        barrier, lock = Barrier(2), Lock()
        class Race(FakeGitHub):
            def _transport(self, method, path, data=None):
                if method == 'GET' and '/issues?' in path:
                    snapshot = copy.deepcopy(self.issues)
                    barrier.wait(timeout=5)
                    return 200, snapshot
                with lock:
                    if method == 'GET' and '/issues/' in path:
                        return 200, copy.deepcopy(self.issues[int(path.rsplit('/',1)[-1])-1])
                    result = super()._transport(method, path, data)
                    if method == 'POST':
                        self.issues[-1]['number'] = len(self.issues)
                        self.issues[-1]['id'] = 100 + len(self.issues)
                        self.issues[-1]['html_url'] = f'https://github.com/o/r/issues/{len(self.issues)}'
                        return 201, copy.deepcopy(self.issues[-1])
                    return result
        client = Race()
        body = triage.render(record())
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(triage.file_issue, client, 'Fix: report', body, config=Filing.config)
                       for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(len(client.issues), 2)  # Accepted limitation, not an assertion of zero duplicates.
        self.assertEqual([v['filing'] for v in results], ['created', 'created'])
        self.assertEqual(len({triage.parse(v['body'])['submission_id'] for v in client.issues}), 1)

    def test_post_preflight_human_milestone_can_be_lost_invisibly(self):
        client = FakeGitHub(); client.lookup_status = 503
        Filing().file(client); client.lookup_status = 200
        old = triage.observed_record(client.issues[0])
        client.before_patch = lambda c: c.issues[0].update(milestone={'number':99})
        result = triage.update_triage(client, 1, expected=old, config=Filing.config,
                                     now='2026-09-12T00:00:00Z')
        self.assertEqual(result['state'], 'updated')
        self.assertEqual(client.issues[0]['milestone']['number'], 16)  # Witness the unsupported remote CAS.

    def test_post_preflight_label_removal_is_observed_but_write_already_happened(self):
        client = FakeGitHub(); client.lookup_status = 503
        Filing().file(client); old = triage.observed_record(client.issues[0])
        client.before_patch = lambda c: c.issues[0].update(labels=[])
        result = triage.update_triage(client, 1, expected=old, config=Filing.config,
                                     now='2026-09-12T00:00:00Z')
        self.assertEqual(result['state'], 'conflict')
        self.assertTrue(any(v[0]=='PATCH' for v in client.calls))
        self.assertEqual(client.issues[0]['labels'], [])

    def test_fresh_unrelated_filing_survives_another_corrupt_issue_but_retry_refuses(self):
        client = FakeGitHub()
        client.issues.append({'number':20,'state':'open','labels':['engine'],'body':triage.START+'broken'})
        self.assertEqual(triage.matching_submission(client, 'fresh-operation-id', strict=False), [])
        with self.assertRaises(triage.TriageError):
            triage.matching_submission(client, 'fresh-operation-id', strict=True)

    def test_missing_section_repair_preserves_human_body_on_same_issue(self):
        client = FakeGitHub(); Filing().file(client)
        client.issues[0]['body'] = 'Human text\n<!-- final-source -->'
        body = client.issues[0]['body']
        result = triage.repair_record(client, 1, expected_body_digest=triage.fingerprint(body),
                                     data={'assessment':assessed(),'submission_id':'repair-operation-id','evidence':['repair']},
                                     config=Filing.config, now='2026-09-12T00:00:00Z')
        self.assertEqual(result['state'], 'updated')
        self.assertTrue(client.issues[0]['body'].endswith(body))
        self.assertEqual(len(client.issues), 1)

    def test_explicit_operator_exception_preserves_remote_pending(self):
        import contextlib, io, tempfile
        from unittest.mock import patch
        client = FakeGitHub()
        Filing().file(client, triage.pending('No remedy yet.', 'Inspect the report.'))
        with tempfile.TemporaryDirectory() as directory:
            session_path = Path(directory) / 'session.json'
            directive = Path(directory) / 'directive.json'
            directive.write_text(json.dumps({'kind':'pause','instruction':'Pause this work now.'}))
            with patch.object(triage, '_session_path', return_value=session_path), contextlib.redirect_stdout(io.StringIO()):
                triage.start_session(client, 'session', Filing.config)
                before = copy.deepcopy(client.issues)
                self.assertEqual(triage.main(['pause','--session','session','--input',str(directive),'--confirm']), 0)
                self.assertEqual(triage.session_progress(client, 'session')['state'], 'paused')
                self.assertEqual(client.issues, before)
                self.assertEqual(len(triage.discover(client, Filing.config)['items']), 1)

    def test_discovery_budget_and_failure_are_not_empty_success(self):
        client = FakeGitHub()
        result = triage.discover(client, Filing.config, max_seconds=0)
        self.assertFalse(result['complete'])
        self.assertIn('budget', result['error'])


class ReviewRegressions(unittest.TestCase):
    def test_resubmitting_pending_input_never_earns_an_assignment_attempt(self):
        import tempfile
        from unittest.mock import patch
        client = FakeGitHub()
        pending = triage.pending('No remedy known.', 'Investigate the failing test.')
        Filing().file(client, pending)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(triage, '_session_path', side_effect=lambda sid, repo: Path(directory) / sid):
                for index in (1, 2):
                    session = f'pending-session-{index}'
                    triage.start_session(client, session, Filing.config)
                    before = copy.deepcopy(client.issues)
                    client.calls.clear()
                    with self.assertRaisesRegex(triage.TriageError, 'use defer'):
                        triage.update_triage(client, 1, expected=triage.observed_record(client.issues[0]),
                                             assessment=pending, config=Filing.config,
                                             now=f'2026-09-12T0{index}:00:00Z')
                    self.assertEqual(client.issues, before)
                    self.assertFalse(any(row[0] == 'PATCH' or '/milestones/' in row[1] for row in client.calls))
                    self.assertEqual(triage.session_progress(client, session)['state'], 'pending')

    def test_repeated_failed_lookup_counts_but_timestamp_edit_does_not(self):
        import tempfile
        from unittest.mock import patch
        client = FakeGitHub(); client.lookup_status = 503
        Filing().file(client)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(triage, '_session_path', side_effect=lambda sid, repo: Path(directory) / sid):
                for index in (1, 2):
                    session = f'session-{index}'
                    triage.start_session(client, session, Filing.config)
                    before = triage.observed_record(client.issues[0])
                    calls = sum('/milestones/' in row[1] for row in client.calls)
                    result = triage.update_triage(client, 1, expected=before, config=Filing.config,
                                                 now=f'2026-09-12T0{index}:00:00Z')
                    self.assertEqual(result['state'], 'updated')
                    self.assertEqual(sum('/milestones/' in row[1] for row in client.calls), calls + 1)
                    self.assertEqual(triage.session_progress(client, session)['state'], 'satisfied')
                    self.assertTrue(result['outstanding'])
                triage.start_session(client, 'timestamps-only', Filing.config)
                edited = triage.observed_record(client.issues[0]); edited['revision'] += 1
                edited['disposition']['at'] = edited['updated_at'] = '2026-09-12T03:00:00Z'
                client.issues[0]['body'] = triage.with_record(client.issues[0]['body'], edited)
                self.assertEqual(triage.session_progress(client, 'timestamps-only')['state'], 'pending')

    def test_repair_advances_fairness_while_pending_survives(self):
        client = FakeGitHub(); Filing().file(client)
        client.issues[0]['body'] = 'Human report without its assessment.'
        second = copy.deepcopy(client.issues[0]); second['number'] = 2
        second['created_at'] = '2026-09-11T01:00:00Z'
        second['body'] = triage.render(record(triage.pending('Remedy unknown.', 'Investigate.')))
        client.issues.append(second)
        self.assertEqual(triage.select_pending(triage.discover(client, Filing.config), Filing.config, client.repo)['number'], 1)
        triage.repair_record(client, 1, expected_body_digest=triage.fingerprint(client.issues[0]['body']),
                             data={'assessment':triage.pending('Remedy unknown.', 'Inspect failure.'),
                                   'submission_id':'repaired-operation', 'evidence':['Restored report.']},
                             config=Filing.config, now='2026-09-12T00:00:00Z')
        discovery = triage.discover(client, Filing.config)
        self.assertEqual(len(discovery['items']), 2)
        self.assertEqual(triage.parse(client.issues[0]['body'])['disposition']['kind'], 'repair')
        self.assertEqual(triage.select_pending(discovery, Filing.config, client.repo)['number'], 2)




class SendBoundary(unittest.TestCase):
    def test_injected_sender_is_called_once_and_never_falls_back(self):
        client = FakeGitHub()
        sent = []
        def send(request):
            sent.append(request)
            raise TimeoutError('The response was lost.')
        result = triage.file_issue(client, 'Fix: report', triage.render(record()), send=send)
        self.assertEqual(result['filing'], 'creation-uncertain')
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['labels'], ['engine'])
        self.assertFalse(any(call[0] == 'POST' for call in client.calls))


class ConfigurationUpgrade(unittest.TestCase):
    def setUp(self):
        import tempfile, subprocess
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'primary'
        self.worktree = self.root.parent / 'linked space'
        self.root.mkdir()
        def git(*args):
            subprocess.run(['git', '-C', str(self.root), *args], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        git('init'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid')
        git('commit', '--allow-empty', '-m', 'fixture')
        git('worktree', 'add', '-b', 'fixture', str(self.worktree))
        self.config = copy.deepcopy(Filing.config)
        self.mapping = {key: None for key in ('none', 'patch', 'minor', 'major')}
        self.canonical = self.root / triage.CONFIG_NAME
        self.legacy = self.worktree / triage.CONFIG_NAME

    def write(self, path, value):
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value))

    def test_worktree_only_migration_preserves_dates_and_other_repositories(self):
        self.config['repositories']['other/project'] = copy.deepcopy(Discovery.settings)
        self.write(self.legacy, self.config)
        before = self.legacy.read_bytes()
        self.assertEqual(triage.load_config(self.root), self.config)
        self.assertEqual(triage.load_config(self.worktree), self.config)
        self.assertFalse(self.canonical.exists())
        triage.configure(FakeGitHub(), self.mapping, root=self.worktree)
        value = triage.load_config(self.root)
        self.assertEqual(value['repositories']['o/r']['activated_at'], Discovery.settings['activated_at'])
        self.assertEqual(value['repositories']['other/project'], Discovery.settings)
        self.assertEqual(self.legacy.read_bytes(), before)
        self.assertEqual(triage.enrollment(Discovery().issue(), value['repositories']['o/r']), 'required')
        triage.configure(FakeGitHub(), self.mapping, root=self.root)
        self.assertEqual(triage.load_config(self.worktree), triage.load_config(self.root))
        self.assertTrue(self.canonical.with_name(self.canonical.name + '.lock').exists())

    def test_conflicting_copies_require_exact_explicit_resolution(self):
        self.write(self.legacy, self.config)
        different = copy.deepcopy(self.config)
        different['repositories']['o/r']['milestones']['patch'] = 99
        self.write(self.canonical, different)
        with self.assertRaisesRegex(triage.TriageError, 'copies conflict'):
            triage.load_config(self.root)
        observed = triage.config_snapshot(self.root, resolve_from=self.legacy)
        with self.assertRaisesRegex(triage.TriageError, 'expect-config-digest'):
            triage.configure(FakeGitHub(), self.mapping, root=self.root, resolve_from=self.legacy)
        triage.configure(FakeGitHub(), self.mapping, root=self.root, resolve_from=self.legacy,
                         expected_digest=observed['digest'])
        self.assertEqual(triage.load_config(self.root)['repositories']['o/r']['milestones'], self.mapping)
        self.legacy.write_text(json.dumps(different))
        with self.assertRaisesRegex(triage.TriageError, 'copies conflict'):
            triage.load_config(self.root)

    def test_source_change_during_network_preflight_refuses_without_write(self):
        self.write(self.legacy, self.config)
        class Client(FakeGitHub):
            def _transport(inner, *args):
                value = copy.deepcopy(self.config)
                value['repositories']['o/r']['activated_at'] = '2026-09-01T00:00:00Z'
                self.write(self.legacy, value)
                return super()._transport(*args)
        with self.assertRaisesRegex(triage.TriageError, 'changed during preflight'):
            triage.configure(Client(), {key: 16 for key in self.mapping}, root=self.root)
        self.assertFalse(self.canonical.exists())

    def test_changed_acknowledged_bytes_conflict_even_when_mappings_match(self):
        self.config['repositories']['o/r']['milestones'] = self.mapping
        self.write(self.legacy, self.config)
        triage.configure(FakeGitHub(), self.mapping, root=self.root)
        before = self.canonical.read_bytes()
        self.legacy.write_text(json.dumps(self.config, indent=2))
        with self.assertRaisesRegex(triage.TriageError, 'copies conflict'):
            triage.load_config(self.root)
        with self.assertRaisesRegex(triage.TriageError, 'copies conflict'):
            triage.configure(FakeGitHub(), self.mapping, root=self.root)
        self.assertEqual(self.canonical.read_bytes(), before)
        observed = triage.config_snapshot(self.root, resolve_from=self.canonical)
        triage.configure(FakeGitHub(), self.mapping, root=self.root, resolve_from=self.canonical,
                         expected_digest=observed['digest'])
        self.assertEqual(triage.load_config(self.root)['repositories'], self.config['repositories'])

    def test_selected_legacy_copy_cannot_drop_other_repository(self):
        self.write(self.legacy, self.config)
        canonical = copy.deepcopy(self.config)
        canonical['repositories']['other/project'] = copy.deepcopy(Discovery.settings)
        self.write(self.canonical, canonical)
        observed = triage.config_snapshot(self.root, resolve_from=self.legacy)
        triage.configure(FakeGitHub(), self.mapping, root=self.root, resolve_from=self.legacy,
                         expected_digest=observed['digest'])
        actual = triage.load_config(self.root)
        self.assertEqual(actual['repositories']['other/project'], Discovery.settings)
        self.assertEqual(actual['repositories']['o/r']['activated_at'], Discovery.settings['activated_at'])
        self.assertEqual(json.loads(self.legacy.read_text()), self.config)

    def test_omitted_conflicting_repository_refuses_without_guessing(self):
        import subprocess
        third = self.root.parent / 'third'
        subprocess.run(['git', '-C', str(self.root), 'worktree', 'add', '-b', 'third', str(third)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.write(self.legacy, self.config)
        canonical = copy.deepcopy(self.config)
        canonical['repositories']['other/project'] = copy.deepcopy(Discovery.settings)
        self.write(self.canonical, canonical)
        different = copy.deepcopy(canonical)
        different['repositories']['other/project']['activated_at'] = '2026-09-01T00:00:00Z'
        self.write(third / triage.CONFIG_NAME, different)
        before = self.canonical.read_bytes()
        with self.assertRaisesRegex(triage.TriageError, 'omits conflicting repository'):
            triage.config_snapshot(self.root, resolve_from=self.legacy)
        self.assertEqual(self.canonical.read_bytes(), before)

    def test_selected_copy_cannot_replace_an_unaffected_canonical_repository(self):
        canonical = copy.deepcopy(self.config)
        canonical['repositories']['other/project'] = copy.deepcopy(Discovery.settings)
        self.write(self.canonical, canonical)
        legacy = copy.deepcopy(canonical)
        legacy['repositories']['o/r']['activated_at'] = '2026-08-01T00:00:00Z'
        legacy['repositories']['other/project']['activated_at'] = '2026-09-01T00:00:00Z'
        legacy['repositories']['other/project']['milestones'] = self.mapping
        self.write(self.legacy, legacy)
        observed = triage.config_snapshot(self.root, resolve_from=self.legacy)
        triage.configure(FakeGitHub(), self.mapping, root=self.root, resolve_from=self.legacy,
                         expected_digest=observed['digest'])
        actual = triage.load_config(self.root)
        self.assertEqual(actual['repositories']['other/project'], canonical['repositories']['other/project'])
        self.assertEqual(actual['repositories']['o/r']['activated_at'], '2026-08-01T00:00:00Z')

    def test_unreadable_and_symbolic_copies_do_not_create_new_activation(self):
        self.legacy.parent.mkdir()
        self.legacy.write_text('broken json')
        with self.assertRaisesRegex(triage.TriageError, 'Cannot read configuration'):
            triage.configure(FakeGitHub(), self.mapping, root=self.root)
        self.legacy.unlink()
        self.legacy.symlink_to(self.root.parent / 'elsewhere')
        with self.assertRaisesRegex(triage.TriageError, 'symbolic'):
            triage.configure(FakeGitHub(), self.mapping, root=self.root)
        self.assertFalse(self.canonical.exists())

    def test_accepted_source_location_is_not_configuration_context(self):
        from unittest.mock import patch
        import os
        self.write(self.canonical, self.config)
        with patch.object(triage, '__file__', str(self.root.parent / 'accepted/.engine/tools/issue_triage.py')), \
             patch('os.getcwd', return_value=str(self.worktree)), \
             patch.dict(os.environ, {'ENGINE_PROJECT_ROOT': str(self.root)}):
            self.assertEqual(triage.load_config(), self.config)
        with patch('os.getcwd', return_value=str(self.worktree)), \
             patch.dict(os.environ, {'ENGINE_PROJECT_ROOT': str(self.root.parent / 'unrelated')}):
            with self.assertRaisesRegex(triage.TriageError, 'disagree'):
                triage.load_config()

    def test_same_repository_concurrent_update_refuses_stale_writer(self):
        import multiprocessing
        context=multiprocessing.get_context('spawn')
        barrier=context.Barrier(2); outcomes=context.Queue()
        self.write(self.canonical,self.config)
        processes=[context.Process(target=_configure_race,args=(str(self.root),'o/r',barrier,outcomes)) for _ in range(2)]
        for process in processes: process.start()
        try:
            results=[outcomes.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(10); self.assertEqual(process.exitcode,0)
        finally:
            for process in processes:
                if process.is_alive(): process.terminate(); process.join()
        self.assertEqual(sorted(state for _,state in results),['changed','configured'])
        self.assertEqual(triage.load_config(self.root)['repositories']['o/r']['activated_at'],Discovery.settings['activated_at'])

    def test_two_processes_refuse_stale_preflight_and_retry_preserves_both_mappings(self):
        import multiprocessing
        context = multiprocessing.get_context('spawn')
        barrier = context.Barrier(2)
        outcomes = context.Queue()
        self.write(self.canonical, self.config)
        processes = [context.Process(target=_configure_race, args=(str(self.root), repo, barrier, outcomes))
                     for repo in ('first/project', 'second/project')]
        for process in processes: process.start()
        try:
            results = [outcomes.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive(): process.terminate(); process.join()
        self.assertEqual(sorted(state for _, state in results), ['changed', 'configured'])
        winner = next(repo for repo, state in results if state == 'configured')
        loser = next(repo for repo, state in results if state == 'changed')
        value = triage.load_config(self.root)
        self.assertIn(winner, value['repositories']); self.assertNotIn(loser, value['repositories'])
        self.assertEqual(value['repositories']['o/r'], Discovery.settings)
        client = FakeGitHub(); client.repo = loser
        triage.configure(client, self.mapping, root=self.root)
        value = triage.load_config(self.root)
        self.assertIn(winner, value['repositories']); self.assertIn(loser, value['repositories'])


def _configure_race(root, repo, barrier, outcomes):
    class Client(FakeGitHub):
        def _transport(self, *args):
            barrier.wait(timeout=10)
            return super()._transport(*args)
    client = Client(); client.repo = repo
    try:
        triage.configure(client, {'none': None, 'patch': 16, 'minor': None, 'major': None}, root=root)
        outcomes.put((repo, 'configured'))
    except triage.TriageError as exc:
        outcomes.put((repo, 'changed' if 'changed during preflight' in str(exc) else str(exc)))


class EligibilityRegression(unittest.TestCase):
    def test_unknown_legacy_issue_is_observed_but_never_selected(self):
        client = FakeGitHub()
        client.issues = [{'number':221, 'labels':['engine'], 'body':'Old report',
                          'created_at':'2026-06-23T00:00:00Z', 'milestone':{'number':19}}]
        result = triage.discover(client, None)
        self.assertTrue(result['complete']); self.assertEqual(result['unknown_count'], 1)
        self.assertEqual(result['pending_count'], 0)
        self.assertIsNone(triage.select_pending(result, None, client.repo))
        self.assertEqual(result['items'][0]['enrollment'], 'unknown')
        self.assertTrue(all(row[0] == 'GET' for row in client.calls))
        client.issues.append({**client.issues[0], 'number':222, 'body':triage.render(record())})
        result = triage.discover(client, None)
        self.assertEqual(result['pending_count'], 1)
        self.assertEqual(triage.select_pending(result, None, client.repo)['number'], 222)



class AuthenticationRecovery(unittest.TestCase):
    def test_gh_only_cli_assessment_and_assignment_use_github_com(self):
        import contextlib, io, os
        from unittest.mock import patch
        import github_client, issue_author, telemetry
        client=FakeGitHub()
        Filing().file(client, triage.pending('Unknown remedy', 'Investigate'))
        command=[]
        def run(args, **kwargs):
            import subprocess
            command.append(args)
            self.assertEqual(args, ['gh','auth','token','--hostname','github.com'])
            return subprocess.CompletedProcess(args,0,'fixture-secret\n','')
        with patch.dict(os.environ, {'GH_HOST':'enterprise.invalid'}, clear=True), \
             patch('subprocess.run', side_effect=run), \
             patch.object(issue_author,'resolve_issue_repositories',return_value=['o/r']), \
             patch.object(issue_author,'load_input',return_value=assessed()), \
             patch.object(triage,'load_config',return_value=Filing.config), \
             patch.object(telemetry,'GitHubIssues',return_value=client) as factory, \
             contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(triage.main(['assess','--issue','1','--input','fixture',
                                         '--expect-revision','1','--confirm']),0)
            self.assertEqual(triage.observed_record(client.issues[0])['assignment']['state'],'assigned')
            self.assertTrue(command)
            factory.assert_called_with('o/r','fixture-secret')
        self.assertNotIn('fixture-secret',out.getvalue()+err.getvalue())

    def test_other_host_only_auth_cannot_reach_transport(self):
        import contextlib, io, os, subprocess
        from unittest.mock import patch
        import issue_author, telemetry
        def run(args, **kwargs):
            self.assertIn('github.com',args)
            return subprocess.CompletedProcess(args,1,'','fixture-other-host-secret')
        with patch.dict(os.environ, {'GH_HOST':'enterprise.invalid'}, clear=True), \
             patch('subprocess.run',side_effect=run), \
             patch.object(issue_author,'resolve_issue_repositories',return_value=['o/r']), \
             patch.object(telemetry,'GitHubIssues') as factory, \
             contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(triage.main(['list']),1)
            factory.assert_not_called()
        self.assertNotIn('fixture-other-host-secret',err.getvalue())
        self.assertIn('No github.com credential',err.getvalue())

    def test_missing_config_list_is_unknown_but_marker_pending_survives(self):
        import contextlib, io
        from unittest.mock import patch
        import github_client, issue_author, telemetry
        client=FakeGitHub();Filing().file(client)
        client.issues.append({'number':221,'labels':['engine'],'body':'Legacy','created_at':'2026-06-23T00:00:00Z'})
        with patch.object(github_client,'auth_token',return_value='fixture'), \
             patch.object(issue_author,'resolve_issue_repositories',return_value=['o/r']), \
             patch.object(triage,'load_config',return_value=None), \
             patch.object(telemetry,'GitHubIssues',return_value=client), contextlib.redirect_stdout(io.StringIO()) as out:
            # Turn the first known issue into genuine pending assignment without deleting its marker.
            client.issues[0]['milestone']=None
            self.assertEqual(triage.main(['list']),1)
        value=json.loads(out.getvalue())
        self.assertEqual(value['unknown_count'],1);self.assertEqual(value['pending_count'],1)
        self.assertIn('configure',value['configuration_error'])
        self.assertEqual(triage.select_pending(value,None,client.repo)['number'],1)

    def test_wrong_target_refuses_before_credentials_or_network(self):
        import contextlib,io
        from unittest.mock import patch
        import github_client,issue_author
        with patch.object(issue_author,'resolve_issue_repositories',return_value=['o/r']), \
             patch.object(github_client,'auth_token') as token,contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(triage.main(['list','--repository','other/repo']),1)
            token.assert_not_called()


if __name__ == '__main__':
    unittest.main()
