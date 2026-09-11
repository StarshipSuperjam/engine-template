"""Offline persistent Git/Issues service: count POSTs across fresh helper processes/clients."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
        self.malformed_graph = False
        self.before_ref = None

    def client(self):
        import telemetry
        return telemetry.GitHubIssues(REPO, 'test-only', transport=self.call)

    @staticmethod
    def object_id(kind, raw):
        return hashlib.sha1(kind.encode() + b' ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()

    def graph_tree(self, sha):
        tree = self.objects[sha]
        entries = []
        for entry in tree['tree']:
            mode = {'040000': 16384, '100644': 33188}[entry['mode']]
            item = {'name': entry['path'], 'mode': mode, 'type': entry['type'].upper(), 'oid': entry['sha']}
            if entry['type'] == 'tree':
                item['object'] = self.graph_tree(entry['sha'])
            elif entry['type'] == 'blob':
                item['object'] = {'byteSize': len(base64.b64decode(self.objects[entry['sha']]['content']))}
            entries.append(item)
        return {'oid': sha, 'entries': entries}

    def graph_history(self, tip, after):
        commits = []
        sha = tip
        while sha:
            commit = self.objects[sha]
            commits.append({'oid': sha, 'message': commit['message'],
                            'parents': {'nodes': [{'oid': p['sha']} for p in commit['parents']], 'pageInfo': {'hasNextPage': False}},
                            'tree': self.graph_tree(commit['tree']['sha'])})
            sha = commit['parents'][0]['sha'] if commit['parents'] else None
        offset = int(after or 0)
        page = commits[offset:offset + 100]
        more = offset + len(page) < len(commits)
        return {'data': {'repository': {'object': {'oid': tip, 'history': {
            'nodes': page, 'pageInfo': {'hasNextPage': more, 'endCursor': str(offset + len(page)) if more else None}}}}}}

    def call(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == '/graphql' and method == 'POST':
            if self.malformed_graph:
                return 200, {'data': {'repository': None}}
            variables = body.get('variables', {}) if isinstance(body, dict) else {}
            query = body.get('query', '') if isinstance(body, dict) else ''
            if 'RecoveryHistory' in query:
                # A fake must not supply fields the actual query omitted.
                import re
                assert len(re.findall(r"object\s*\{\s*oid\s*\.\.\. on Tree", query)) == 2
                assert '... on Blob { byteSize }' in query
                assert 'nodes { oid } pageInfo { hasNextPage }' in query
                assert 'object(expression: $expression) { oid' in query
                return 200, self.graph_history(variables['expression'], variables.get('after'))
            if 'RecoveryBlobs' in query:
                values = {}
                for key, sha in variables.items():
                    if key.startswith('id'):
                        blob = self.objects.get(sha)
                        if not blob or blob.get('encoding') != 'base64':
                            return 200, {'data': {'repository': {}}}
                        raw = base64.b64decode(blob['content'])
                        values['b' + key[2:]] = {'oid': sha, 'byteSize': len(raw), 'isTruncated': False,
                                                  'text': raw.decode('utf-8')}
                return 200, {'data': {'repository': values}}
            return 200, {'errors': [{'message': 'unknown query'}]}
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

    def test_genesis_response_loss_recovers_only_the_exact_created_root(self):
        for wrong_ref in (False, True):
            with self.subTest(wrong_ref=wrong_ref):
                remote = Remote()
                client = remote.client()
                def lost(method, path, body):
                    answer = remote.call(method, path, body)
                    if method == 'POST' and path.endswith('/git/refs'):
                        if wrong_ref:
                            remote.ref = 'f' * 40
                        raise TimeoutError('reference response lost')
                    return answer
                client._transport = lost
                if wrong_ref:
                    with self.assertRaises(storage.RecoveryError):
                        storage.initialize(client)
                else:
                    activation = storage.initialize(client)
                    self.assertEqual(activation['genesis'], remote.ref)
                    self.assertEqual(storage.GitStore(remote.client(), activation).load()[1]['records'], {})
                self.assertEqual(remote.posts, 0)
                self.assertEqual(sum(method == 'POST' and path.endswith('/git/refs')
                                     for method, path, _body in remote.calls), 1)

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

    def test_fresh_store_cold_read_batches_hundred_commit_history(self):
        store = self.store()
        for number in range(100):
            tip, snapshot = store.load()
            updated = copy.deepcopy(snapshot)
            updated['revision'] += 1
            updated['records'][f'operation-{number}'] = {'state': 'prepared'}
            store.compare_and_swap(tip, updated)
        self.remote.calls.clear()
        fresh = self.store()
        tip, snapshot = fresh.load()
        self.assertEqual(tip, self.remote.ref)
        self.assertEqual(snapshot['revision'], 100)
        self.assertLessEqual(len(self.remote.calls), 12)
        self.assertEqual(sum(path == '/graphql' for _method, path, _body in self.remote.calls), 8)

    def test_graphql_partial_or_malformed_data_refuses_before_send(self):
        self.remote.malformed_graph = True
        with self.assertRaises(storage.RecoveryError):
            self.submit()
        self.assertEqual(self.remote.posts, 0)


class BoundedJournalReads(unittest.TestCase):
    def setUp(self):
        self.remote = Remote()
        self.activation = storage.initialize(self.remote.client())

    def store(self, transport=None):
        client = self.remote.client()
        if transport is not None:
            client._transport = transport
        return storage.GitStore(client, self.activation)

    def seed(self, count):
        writer = self.store()
        for number in range(count):
            tip, snapshot = writer.load()
            snapshot['revision'] += 1
            snapshot['records'][str(number)] = {'state': 'prepared'}
            writer.compare_and_swap(tip, snapshot)
        return writer

    def test_cold_helper_after_a_hundred_operations_stays_below_a_hundred_calls(self):
        costs = []
        for number in range(101):
            self.remote.calls.clear()
            result = issue_author.create_issue_result(
                {**INTENT, 'submission_id': f'manual-{number}'}, env=ENV,
                issues_factory=lambda *_: self.remote.client(), recovery_store=self.store())
            self.assertEqual(result['filing'], 'created', result)
            costs.append(len(self.remote.calls))
        self.assertEqual(self.remote.posts, 101)
        self.assertLess(costs[-1], 100, costs[-1])
        self.assertLess(costs[-1] - costs[0], 30, (costs[0], costs[-1]))

    def test_verified_instance_cache_is_defensive_and_reads_remote_identity_each_time(self):
        store = self.seed(3)
        tip, snapshot = store.load()
        snapshot['records'].clear()
        self.remote.calls.clear()
        self.assertEqual(len(store.load()[1]['records']), 3)
        self.assertEqual([path for _method, path, _body in self.remote.calls],
                         ['/repos/' + REPO, '/repos/' + REPO + '/git/ref/heads/codex/engine-issue-recovery'])
        peer = self.store()
        _, update = peer.load()
        update['revision'] += 1
        peer.compare_and_swap(tip, update)
        self.remote.calls.clear()
        self.assertEqual(store.load()[1]['revision'], 4)
        reads = [body for _method, path, body in self.remote.calls
                 if path == '/graphql' and 'RecoveryBlobs' in body['query']]
        self.assertEqual([len([key for key in body['variables'] if key.startswith('id')]) for body in reads], [1])
        self.remote.ref = tip
        with self.assertRaises(storage.RecoveryError):
            store.load()

    def test_payload_batches_obey_aggregate_byte_budget(self):
        self.seed(100)
        self.remote.calls.clear()
        with patch.object(storage, '_GRAPH_BYTES', 8192):
            self.store().load()
        batches = [body for _method, path, body in self.remote.calls
                   if path == '/graphql' and 'RecoveryBlobs' in body['query']]
        self.assertGreater(len(batches), 6)
        for body in batches:
            sizes = [len(base64.b64decode(self.remote.objects[sha]['content']))
                     for key, sha in body['variables'].items() if key.startswith('id')]
            self.assertLessEqual(sum(sizes), 8192)
            self.assertLessEqual(len(sizes), storage._GRAPH_BATCH)

    def test_history_pages_are_pinned_when_another_writer_advances(self):
        writer = self.seed(100)
        pinned, snapshot = writer.load()
        snapshot['revision'] += 1
        future = writer._post_commit(writer._write_tree(snapshot), [pinned], storage._UPDATE_MESSAGE)
        advanced = False
        def racing(method, path, body):
            nonlocal advanced
            answer = self.remote.call(method, path, body)
            if path == '/graphql' and 'RecoveryHistory' in body['query']:
                self.assertEqual(body['variables']['expression'], pinned if not advanced or body['variables']['after'] else future)
                if not advanced:
                    self.remote.ref = future
                    advanced = True
            return answer
        reader = self.store(racing)
        tip, old = reader.load()
        self.assertEqual((tip, old['revision']), (pinned, 100))
        self.assertEqual(reader.load()[1]['revision'], 101)

    def test_bad_graph_data_and_oversize_metadata_refuse_before_issue_posts(self):
        self.seed(2)
        mutations = [
            lambda d: d.update(errors=[{'message': 'partial result'}]),
            lambda d: d['data']['repository']['object'].update(oid='f' * 40),
            lambda d: d['data']['repository']['object']['history']['nodes'][0]['tree'].update(oid='f' * 40),
            lambda d: d['data']['repository']['object']['history']['nodes'][0]['parents']['pageInfo'].update(hasNextPage=True),
            lambda d: d['data']['repository']['object']['history']['nodes'].pop(0),
            lambda d: d['data']['repository']['object']['history']['nodes'][0]['tree']['entries'][0]['object'].pop('oid'),
            lambda d: d['data']['repository']['object']['history']['nodes'][0]['tree']['entries'][0]['object']['entries'][0]['object']['entries'][0]['object'].update(byteSize=storage._MAX_SNAPSHOT_BYTES + 1),
        ]
        for change in mutations:
            with self.subTest(change=mutations.index(change)):
                def malformed(method, path, body):
                    status, data = self.remote.call(method, path, body)
                    if path == '/graphql' and 'RecoveryHistory' in body['query']:
                        change(data)
                    return status, data
                with self.assertRaises(storage.RecoveryError):
                    self.store(malformed).load()
        self.assertEqual(self.remote.posts, 0)

    def test_truncated_graph_blob_uses_verified_rest_bytes(self):
        self.seed(2)
        def truncated(method, path, body):
            status, data = self.remote.call(method, path, body)
            if path == '/graphql' and 'RecoveryBlobs' in body['query']:
                for blob in data['data']['repository'].values():
                    blob.update(isTruncated=True, text='incomplete')
            return status, data
        self.remote.calls.clear()
        self.assertEqual(self.store(truncated).load()[1]['revision'], 2)
        self.assertTrue(any('/git/blobs/' in path and method == 'GET' for method, path, _body in self.remote.calls))
        def corrupt(method, path, body):
            status, data = truncated(method, path, body)
            if method == 'GET' and '/git/blobs/' in path:
                data['content'] = base64.b64encode(b'{}').decode()
            return status, data
        with self.assertRaises(storage.RecoveryError):
            self.store(corrupt).load()

    def test_cold_history_still_refuses_record_removal(self):
        writer = self.seed(2)
        tip, snapshot = writer.load()
        snapshot['revision'] += 1
        snapshot['records'].clear()
        self.remote.calls.clear()
        with self.assertRaises(storage.RecoveryError):
            writer.compare_and_swap(tip, snapshot)
        self.assertFalse(any(method in ('POST', 'PATCH') for method, _path, _body in self.remote.calls))
        self.remote.ref = writer._post_commit(writer._write_tree(snapshot), [tip], storage._UPDATE_MESSAGE)
        with self.assertRaises(storage.RecoveryError):
            self.store().load()

    def test_repeated_cursor_and_extra_history_after_genesis_refuse(self):
        self.seed(100)
        def extra(method, path, body):
            status, data = self.remote.call(method, path, body)
            if path == '/graphql' and 'RecoveryHistory' in body['query'] and body['variables']['after']:
                data['data']['repository']['object']['history']['pageInfo'].update(hasNextPage=True, endCursor='100')
            return status, data
        with self.assertRaises(storage.RecoveryError):
            self.store(extra).load()

    def test_duplicate_or_nonfinite_json_is_a_typed_refusal(self):
        for raw in (b'{"records":{},"records":{}}', b'{"revision":NaN}'):
            sha = self.remote.object_id('blob', raw)
            self.remote.objects[sha] = {'encoding':'base64', 'content':base64.b64encode(raw).decode(), 'sha':sha}
            with self.assertRaises(storage.RecoveryError):
                list(self.store()._graph_blobs([('0'*40, None, sha, len(raw), True)]))


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
            tools = str(Path(__file__).resolve().parent)
            script = f'''
import json,sys
sys.path.insert(0, {tools!r})
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
    def test_only_absent_activation_is_setup_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / recovery.CONFIG_NAME
            with self.assertRaises(recovery.SetupRequired):
                recovery.load_activation(REPO, root=directory)
            path.parent.mkdir()
            path.write_text(json.dumps({'schema_version': 'operator-issue-recovery.v1', 'repositories': {}}))
            with self.assertRaises(recovery.SetupRequired):
                recovery.load_activation(REPO, root=directory)
            for raw in ('{}', 'not JSON'):
                path.write_text(raw)
                with self.assertRaises(storage.RecoveryError) as caught:
                    recovery.load_activation(REPO, root=directory)
                self.assertNotIsInstance(caught.exception, recovery.SetupRequired)
            with patch.object(Path, 'read_text', side_effect=PermissionError('private path')):
                with self.assertRaises(storage.RecoveryError) as caught:
                    recovery.load_activation(REPO, root=directory)
                self.assertNotIsInstance(caught.exception, recovery.SetupRequired)
                self.assertNotIn('private path', str(caught.exception))

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


if __name__ == '__main__':
    unittest.main()
