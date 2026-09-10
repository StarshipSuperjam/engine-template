"""Offline persistent Git/Issues service: count POSTs across fresh helper processes/clients."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import issue_author
import issue_recovery as recovery
import issue_recovery_store as storage
import issue_triage

REPO = 'acme/project'
ENV = {'GITHUB_REPOSITORY': REPO, 'GITHUB_TOKEN': 'test-only'}
INTENT = {'repository': REPO, 'kind': 'Fix', 'title': 'Recover reports',
          'what_this_is': 'A check failed.', 'whats_next': 'Investigate the evidence.',
          'assessment': {'state': 'pending', 'unknown': 'Impact not investigated.',
                         'next_action': 'Inspect the failure.'}, 'submission_id': 'manual-operation-1'}


class MemoryStore:
    """Test-only durable state seam; deepcopy prevents a process from mutating remote records."""
    def __init__(self):
        self.snapshot = {'schema_version': 'issue-recovery.v1', 'repository_id': 42, 'revision': 0, 'records': {}}
        self.tip = '0' * 40
        self.lose_claim = False
        self.conflict = False

    def load(self):
        return self.tip, copy.deepcopy(self.snapshot)

    def compare_and_swap(self, expected, value):
        if expected != self.tip or self.conflict:
            raise storage.Conflict('contended')
        storage.GitStore._history_transition(self.snapshot, value)
        self.snapshot = copy.deepcopy(value)
        self.tip = hashlib.sha1(json.dumps(value, sort_keys=True).encode()).hexdigest()
        if self.lose_claim and any(r['state'] == 'send-claimed' for r in value['records'].values()):
            self.lose_claim = False
            raise storage.RecoveryError('response lost after claim')
        return self.tip


class Remote:
    """A persistent fake service, including Git objects and non-force reference semantics."""
    def __init__(self):
        self.objects = {}
        self.ref = None
        self.issues = []
        self.posts = 0
        self.calls = []
        self.lose_issue_response = False
        self.issue_rejection = None
        self.lose_ref_response = False
        self.fail_reads = False
        self.malformed_page = False
        self.before_ref = None

    def client(self):
        import telemetry
        return telemetry.GitHubIssues(REPO, 'test-only', transport=self.call)

    @staticmethod
    def object_id(kind, raw):
        return hashlib.sha1(kind.encode() + b' ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()

    def call(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        prefix = '/repos/' + REPO
        if path == prefix:
            return 200, {'id': 42}
        suffix = path[len(prefix):]
        if suffix.startswith('/git/ref/') and method == 'GET':
            return (404, None) if self.ref is None else (200, {'ref': storage.REF, 'object': {'type': 'commit', 'sha': self.ref}})
        if suffix == '/git/refs' and method == 'POST':
            assert body['ref'] == storage.REF
            if self.ref is not None:
                return 422, None
            self.ref = body['sha']
            return 201, {'object': {'sha': self.ref}}
        if suffix.startswith('/git/refs/') and method == 'PATCH':
            assert body['force'] is False
            if self.before_ref:
                callback, self.before_ref = self.before_ref, None
                callback()
            commit = self.objects[body['sha']]
            if commit['parents'] != [{'sha': self.ref}]:
                return 422, None
            self.ref = body['sha']
            if self.lose_ref_response:
                self.lose_ref_response = False
                raise TimeoutError('lost')
            return 200, {'object': {'sha': self.ref}}
        if suffix == '/git/blobs' and method == 'POST':
            raw = base64.b64decode(body['content'])
            sha = self.object_id('blob', raw)
            self.objects[sha] = {'sha': sha, **body}
            return 201, {'sha': sha}
        if suffix == '/git/trees' and method == 'POST':
            entries = body['tree']
            raw = b''.join((e['mode'].lstrip('0') + ' ' + e['path']).encode() + b'\0' + bytes.fromhex(e['sha']) for e in entries)
            sha = self.object_id('tree', raw)
            self.objects[sha] = {'sha': sha, 'tree': copy.deepcopy(entries), 'truncated': False}
            return 201, {'sha': sha}
        if suffix == '/git/commits' and method == 'POST':
            sha = self.object_id('commit', json.dumps(body, sort_keys=True).encode())
            obj = {'sha': sha, 'tree': {'sha': body['tree']}, 'parents': [{'sha': p} for p in body['parents']], 'message': body['message']}
            self.objects[sha] = obj
            return 201, copy.deepcopy(obj)
        if suffix.startswith(('/git/blobs/', '/git/trees/', '/git/commits/')) and method == 'GET':
            obj = self.objects.get(suffix.rsplit('/', 1)[-1])
            return (200, copy.deepcopy(obj)) if obj else (404, None)
        if suffix.startswith('/issues?') and method == 'GET':
            if self.fail_reads:
                return 503, None
            if self.malformed_page:
                return 200, {'not': 'a page'}
            import urllib.parse
            query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            page = int(query.get('page', [1])[0])
            return 200, copy.deepcopy(self.issues[(page - 1) * 100:page * 100])
        if suffix == '/issues' and method == 'POST':
            self.posts += 1
            if self.issue_rejection:
                return self.issue_rejection, None
            number = len(self.issues) + 1
            issue = {'id': number + 1000, 'number': number, 'html_url': f'https://github.com/{REPO}/issues/{number}',
                     'state': 'open', 'milestone': None, **copy.deepcopy(body)}
            self.issues.append(issue)
            if self.lose_issue_response:
                self.lose_issue_response = False
                raise TimeoutError('lost response after server creation')
            return 201, copy.deepcopy(issue)
        if suffix.startswith('/issues/') and method == 'GET':
            number = int(suffix.split('/')[2])
            return next(((200, copy.deepcopy(i)) for i in self.issues if i['number'] == number), (404, None))
        return 404, None


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.remote = Remote()
        self.activation = storage.initialize(self.remote.client())

    def store(self):
        return storage.GitStore(self.remote.client(), self.activation)

    def submit(self, *, observation='run-1', producer='manual', store=None, intent=None):
        client = self.remote.client()
        data = copy.deepcopy(intent or INTENT)
        def prepare(sid):
            value = {**data, 'submission_id': sid}
            return issue_triage.prepare_request(client, issue_author.title_from_input(value), issue_author.body_from_input(value))
        return recovery.submit(client, data, prepare, producer=producer, source_key='stable-signal' if producer != 'manual' else data['submission_id'],
                               observation=observation, store=store or self.store())

    def test_full_helper_requires_setup_and_accepts_activated_store(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(issue_author.IssueInputError):
                issue_author.create_issue_result(INTENT, env=ENV, root=root, issues_factory=lambda *_: self.remote.client())
        result = issue_author.create_issue_result(INTENT, env=ENV, issues_factory=lambda *_: self.remote.client(), recovery_store=self.store())
        self.assertEqual(result['filing'], 'created')
        self.assertEqual(self.remote.posts, 1)

    def test_response_loss_closed_before_fresh_client_recovers_same_issue(self):
        self.remote.lose_issue_response = True
        self.assertEqual(self.submit()['filing'], 'creation-uncertain')
        self.remote.issues[0]['state'] = 'closed'
        self.assertEqual(self.submit()['filing'], 'created')
        self.assertEqual(self.remote.posts, 1)

    def test_claim_response_loss_never_grants_restarted_send(self):
        store = MemoryStore(); store.lose_claim = True
        self.assertEqual(self.submit(store=store)['filing'], 'creation-uncertain')
        self.assertEqual(self.submit(store=store)['filing'], 'creation-uncertain')
        self.assertEqual(self.remote.posts, 0)

    def test_crash_after_claim_before_post_stays_held(self):
        with patch.object(recovery.SendPermit, '__call__', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.submit()
        self.assertEqual(self.submit()['filing'], 'creation-uncertain')
        self.assertEqual(self.remote.posts, 0)

    def test_crash_before_claim_can_resume_prepared(self):
        store = self.store()
        original = store.compare_and_swap
        def save(tip, value):
            if any(r['state'] == 'send-claimed' for r in value['records'].values()):
                raise KeyboardInterrupt
            return original(tip, value)
        with patch.object(store, 'compare_and_swap', side_effect=save):
            with self.assertRaises(KeyboardInterrupt):
                self.submit(store=store)
        self.assertEqual(self.submit()['filing'], 'created')
        self.assertEqual(self.remote.posts, 1)

    def test_changed_evidence_does_not_replace_uncertain_identity(self):
        with patch.object(recovery.SendPermit, '__call__', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.submit(producer='telemetry')
        before = self.store().load()[1]
        changed = {**INTENT, 'what_this_is': 'Changed symptom'}
        self.assertEqual(self.submit(producer='telemetry', intent=changed, observation='run-2')['filing'], 'creation-uncertain')
        self.assertEqual(self.store().load()[1], before)
        self.assertEqual(self.remote.posts, 0)

    def test_closed_recovery_returns_before_later_recurrence(self):
        self.remote.lose_issue_response = True
        self.submit(producer='telemetry')
        self.remote.issues[0]['state'] = 'closed'
        self.assertEqual(self.submit(producer='telemetry', observation='run-2')['filing'], 'created')
        self.assertEqual(self.remote.posts, 1)
        self.assertEqual(self.submit(producer='telemetry', observation='run-2')['filing'], 'created')
        self.assertEqual(self.remote.posts, 1)
        self.assertEqual(self.submit(producer='telemetry', observation='run-3')['filing'], 'created')
        self.assertEqual(self.remote.posts, 2)
        records = self.store().load()[1]['records']
        self.assertEqual(sorted(r['generation'] for r in records.values()), [1, 2])

    def test_rejected_operation_is_not_replayed(self):
        self.remote.issue_rejection = 422
        self.assertEqual(self.submit()['filing'], 'failed')
        self.assertEqual(self.submit()['filing'], 'creation-uncertain')
        self.assertEqual(self.remote.posts, 1)

    def test_multiple_or_incomplete_matches_hold(self):
        self.remote.lose_issue_response = True
        self.submit()
        self.remote.issues.append(copy.deepcopy(self.remote.issues[0]))
        self.assertEqual(self.submit()['filing'], 'creation-uncertain')
        self.remote.malformed_page = True
        self.assertEqual(self.submit()['filing'], 'creation-uncertain')
        self.assertEqual(self.remote.posts, 1)

    def test_recovery_reads_all_pages_and_closed_issues(self):
        self.remote.lose_issue_response = True
        self.submit()
        actual = self.remote.issues[0]
        self.remote.issues = [{'number': n + 2000, 'labels': []} for n in range(100)] + [actual]
        self.assertEqual(self.submit()['filing'], 'created')
        self.assertEqual(self.remote.posts, 1)

    def test_explicit_supersede_retains_duplicate_risk(self):
        with patch.object(recovery.SendPermit, '__call__', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.submit()
        store = self.store(); _, snapshot = store.load(); key = next(iter(snapshot['records']))
        with self.assertRaises(storage.RecoveryError):
            recovery.supersede(store, key, expected_revision=snapshot['revision'], reason='inspected', quiescent=False)
        recovery.supersede(store, key, expected_revision=snapshot['revision'], reason='writer stopped and GitHub inspected', quiescent=True)
        self.assertEqual(self.submit()['filing'], 'created')
        records = self.store().load()[1]['records']
        self.assertTrue(records[key]['decision']['residual_duplicate_risk'])
        self.assertEqual(self.remote.posts, 1)

    def test_ref_loss_conflict_rollback_corruption_and_no_reinit(self):
        store = self.store(); tip, snapshot = store.load()
        updated = {**snapshot, 'revision': 1}
        self.remote.lose_ref_response = True
        with self.assertRaises(storage.RecoveryError):
            store.compare_and_swap(tip, updated)
        fresh_tip, fresh = self.store().load()
        self.assertNotEqual(fresh_tip, tip)
        with self.assertRaises(storage.Conflict):
            self.store().compare_and_swap(tip, updated)
        with self.assertRaises(storage.RecoveryError):
            storage.initialize(self.remote.client())
        observed = self.store(); observed.load(); self.remote.ref = tip
        with self.assertRaises(storage.RecoveryError):
            observed.load()
        self.remote.ref = None
        with self.assertRaises(storage.RecoveryError):
            self.submit()
        self.assertEqual(self.remote.posts, 0)

    def test_blob_corruption_fails_closed(self):
        self.remote.objects[next(k for k, v in self.remote.objects.items() if v.get('encoding') == 'base64')]['content'] = base64.b64encode(b'{}').decode()
        with self.assertRaises(storage.RecoveryError):
            self.submit()
        self.assertEqual(self.remote.posts, 0)

    def test_permit_consumed_before_transport_and_not_serializable(self):
        import pickle
        client = self.remote.client(); request = {'title': 'x', 'body': 'x', 'labels': ['engine']}
        permit = recovery.SendPermit(client, request)
        with self.assertRaises(TypeError):
            pickle.dumps(permit)
        permit(request)
        with self.assertRaises(storage.RecoveryError):
            permit(request)
        self.assertEqual(self.remote.posts, 1)

    def test_overflow_refuses_without_eviction(self):
        store = self.store(); _, before = store.load()
        with self.assertRaises(storage.RecoveryError):
            self.submit(intent={**INTENT, 'what_this_is': 'x' * (1024 * 1024)})
        self.assertEqual(self.store().load()[1], before)
        self.assertEqual(self.remote.posts, 0)


if __name__ == '__main__':
    unittest.main()


class ProcessAndOperatorTests(unittest.TestCase):
    def test_remote_journal_survives_two_real_processes(self):
        import subprocess
        import sys
        remote = Remote()
        activation = storage.initialize(remote.client())
        remote.lose_issue_response = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'remote.json'
            path.write_text(json.dumps({'remote': remote.__dict__, 'activation': activation}))
            script = '''
import json,sys
from test_issue_recovery import Remote,INTENT,ENV
import issue_author,issue_recovery_store
p=sys.argv[1]; state=json.load(open(p)); remote=Remote(); remote.__dict__.update(state['remote'])
result=issue_author.create_issue_result(INTENT,env=ENV,issues_factory=lambda *_:remote.client(),recovery_store=issue_recovery_store.GitStore(remote.client(),state['activation']))
state['remote']=remote.__dict__;open(p,'w').write(json.dumps(state));print(result['filing'])
'''
            first = subprocess.run([sys.executable, '-c', script, str(path)], capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            state = json.loads(path.read_text()); state['remote']['issues'][0]['state'] = 'closed'
            path.write_text(json.dumps(state))
            second = subprocess.run([sys.executable, '-c', script, str(path)], capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second.stdout.strip(), 'created')
            self.assertEqual(json.loads(path.read_text())['remote']['posts'], 1)

    def test_competing_reporters_cannot_both_claim_send(self):
        remote = Remote(); activation = storage.initialize(remote.client())
        client = remote.client(); store = storage.GitStore(client, activation)
        def prepare(sid):
            return issue_triage.prepare_request(client, 'Fix: x', issue_author.body_from_input({**INTENT, 'submission_id': sid}))
        original = store.compare_and_swap
        def interleave(tip, snapshot):
            if any(r['state'] == 'send-claimed' for r in snapshot['records'].values()):
                other = recovery.submit(remote.client(), INTENT, prepare, store=storage.GitStore(remote.client(), activation))
                self.assertEqual(other['filing'], 'created')
            return original(tip, snapshot)
        with patch.object(store, 'compare_and_swap', side_effect=interleave):
            recovery.submit(client, INTENT, prepare, store=store)
        self.assertEqual(remote.posts, 1)

    def test_recovery_cli_explicit_activation_and_inspection(self):
        import contextlib
        import io
        import telemetry
        remote = Remote()
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', ENV), patch.object(telemetry, 'GitHubIssues', return_value=remote.client()), patch.object(recovery, 'CONFIG_NAME', str(Path(directory) / 'config.json')):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(recovery.main(['preview']), 0)
                self.assertEqual(remote.calls, [])
                self.assertEqual(recovery.main(['init']), 2)
                self.assertIsNone(remote.ref)
                self.assertEqual(recovery.main(['init', '--confirm']), 0)
                self.assertEqual(recovery.main(['init', '--confirm']), 2)
                self.assertEqual(recovery.main(['list']), 0)
            self.assertEqual(remote.posts, 0)


class OperatorConfiguration(unittest.TestCase):
    def test_ambiguous_activation_json_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / recovery.CONFIG_NAME
            path.parent.mkdir()
            for text in (
                '{"schema_version":"operator-issue-recovery.v1","repositories":{},"repositories":{}}',
                '{"schema_version":"operator-issue-recovery.v1","repositories":{"acme/project":{"repository_id":NaN,"genesis":"' + 'a' * 40 + '"}}}',
            ):
                path.write_text(text)
                with self.assertRaisesRegex(storage.RecoveryError, 'unreadable'):
                    recovery.load_activation(REPO, root=directory)

    def test_activation_is_preserved_operator_config(self):
        import module_coherence
        import module_manager
        self.assertIn(recovery.CONFIG_NAME, module_coherence.OPERATOR_CONFIG)
        self.assertTrue(module_coherence.is_deployment_private(recovery.CONFIG_NAME))
        self.assertFalse(module_coherence.travels_to_engine_home(recovery.CONFIG_NAME))
        exact, _ = module_manager._reconcile_carveouts()
        self.assertIn(recovery.CONFIG_NAME, exact)
