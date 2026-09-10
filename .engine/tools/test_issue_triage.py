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


if __name__ == '__main__':
    unittest.main()


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
