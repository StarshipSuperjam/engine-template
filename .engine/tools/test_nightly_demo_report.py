"""Integration coverage for durable automated Issue submissions.

These tests deliberately share ``test_issue_recovery.Remote``: it retains both
the Git-backed journal and server-side issues across fresh clients, while all
producer rendering, triage, recovery, and send-permit code remains real.
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import issue_author  # noqa: E402
import issue_recovery_store as storage  # noqa: E402
import issue_triage  # noqa: E402
import telemetry  # noqa: E402
from test_issue_recovery import ENV, REPO, Remote  # noqa: E402


NOW_1 = '2026-09-10T01:00:00Z'
NOW_2 = '2026-09-10T02:00:00Z'
NOW_3 = '2026-09-10T03:00:00Z'


def telemetry_data(*, now=NOW_1, message='A durable signal needs attention.'):
    return {
        'record': {
            'source_id': 'integration/durable-signal',
            'severity': telemetry.TRUST_CRITICAL,
            'message': message,
            'location': {'file': '.engine/tools/test_nightly_demo_report.py'},
        },
        'first_seen': NOW_1,
        'now': now,
    }


def failed_nightly(*, name='demo_durable.py'):
    return {
        'ok': False,
        'ran': [name],
        'failures': [{'demo': name, 'exit_code': 1, 'output': 'demonstration failed'}],
    }


class ReportRemote(Remote):
    """The recovery fixture plus the reporter's real refresh endpoint."""
    def call(self, method, path, body=None):
        prefix = '/repos/' + REPO
        suffix = path[len(prefix):] if path.startswith(prefix) else path
        if method == 'GET' and suffix.startswith('/labels/'):
            return 200, {'name': 'engine'}
        if method == 'GET' and suffix.startswith('/issues?state=open'):
            import urllib.parse
            query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            page = int(query.get('page', [1])[0])
            rows = [i for i in self.issues if i['state'] == 'open']
            return 200, copy.deepcopy(rows[(page - 1) * 100:page * 100])
        if method == 'PATCH' and suffix.startswith('/issues/'):
            number = int(suffix.rsplit('/', 1)[-1])
            for issue in self.issues:
                if issue['number'] == number:
                    issue.update(copy.deepcopy(body))
                    self.calls.append((method, path, copy.deepcopy(body)))
                    return 200, copy.deepcopy(issue)
            return 404, None
        return super().call(method, path, body)


class DurableAutomatedSubmissionTests(unittest.TestCase):
    def test_transport_diagnostics_do_not_expose_exception_content(self):
        original = self.remote.call
        def transport(method, path, body=None):
            if method == 'POST' and path.endswith('/issues'):
                raise TimeoutError('private-token-and-body-content')
            return original(method, path, body)
        client = telemetry.GitHubIssues(REPO, 'test-only', transport=transport)
        result = self.submit_telemetry(client=client)
        self.assertEqual(result['filing'], 'creation-uncertain')
        self.assertNotIn('private-token-and-body-content', str(result))

    def test_lifecycle_refuses_nonmonotonic_publication_before_writes(self):
        import issue_recovery
        self.submit_telemetry()
        store = self.store()
        tip, snapshot = store.load()
        key, record = next(iter(snapshot['records'].items()))
        changed = {**record, 'state': 'prepared', 'send_nonce': None, 'issue': None}
        before = len([call for call in self.remote.calls if call[0] != 'GET'])
        with self.assertRaises(storage.RecoveryError):
            issue_recovery._save(store, tip, snapshot, key, changed)
        self.assertEqual(len([call for call in self.remote.calls if call[0] != 'GET']), before)

    def setUp(self):
        self.remote = Remote()
        self.activation = storage.initialize(self.remote.client())

    def store(self):
        return storage.GitStore(self.remote.client(), self.activation)

    def submit_telemetry(self, *, data=None, client=None, store=None, env=ENV):
        return issue_author.create_producer_result(
            'telemetry', data or telemetry_data(), client or self.remote.client(),
            env=env, recovery_store=store or self.store())

    def test_response_loss_then_fresh_client_recovers_closed_telemetry_issue_without_repost(self):
        self.remote.lose_issue_response = True
        held = self.submit_telemetry()
        self.assertEqual(held['filing'], 'creation-uncertain')
        self.assertEqual(self.remote.posts, 1)

        self.remote.issues[0]['state'] = 'closed'
        recovered = self.submit_telemetry(client=self.remote.client())
        self.assertEqual(recovered['filing'], 'created')
        self.assertFalse(recovered['newly_created'])
        self.assertEqual(self.remote.posts, 1)

    def test_changed_telemetry_evidence_cannot_repeat_an_uncertain_post(self):
        self.remote.lose_issue_response = True
        self.assertEqual(self.submit_telemetry()['filing'], 'creation-uncertain')
        changed = telemetry_data(message='The evidence changed after the lost response.')
        recovered = self.submit_telemetry(data=changed, client=self.remote.client())
        self.assertEqual(recovered['filing'], 'created')
        self.assertFalse(recovered['newly_created'])
        self.assertEqual(self.remote.posts, 1)

    def test_first_closed_observation_holds_then_later_observation_creates_next_generation(self):
        self.assertEqual(self.submit_telemetry()['filing'], 'created')
        self.remote.issues[0]['state'] = 'closed'

        first_closed = self.submit_telemetry(data=telemetry_data(now=NOW_2))
        same_observation = self.submit_telemetry(data=telemetry_data(now=NOW_2))
        self.assertEqual((first_closed['filing'], same_observation['filing']), ('created', 'created'))
        self.assertEqual(self.remote.posts, 1)

        later = self.submit_telemetry(data=telemetry_data(now=NOW_3))
        self.assertEqual(later['filing'], 'created')
        self.assertTrue(later['newly_created'])
        self.assertEqual(self.remote.posts, 2)
        generations = [r['generation'] for r in self.store().load()[1]['records'].values()]
        self.assertEqual(sorted(generations), [1, 2])

    def test_missing_activation_refuses_before_an_automatic_post(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(issue_author.IssueInputError):
                issue_author.create_producer_result(
                    'telemetry', telemetry_data(), self.remote.client(), env=ENV, root=root)
        self.assertEqual(self.remote.posts, 0)

    def test_untrusted_target_refuses_before_an_automatic_post(self):
        untrusted = telemetry.GitHubIssues('elsewhere/project', 'test-only', transport=self.remote.call)
        with self.assertRaises(issue_author.IssueInputError):
            issue_author.create_producer_result(
                'telemetry', telemetry_data(), untrusted, env=ENV, recovery_store=self.store())
        self.assertEqual(self.remote.posts, 0)

    def _legacy_issue(self, *, submission_id, number, message='Historical durable signal.'):
        record = telemetry_data(message=message)['record']
        core = telemetry.issue_body(record, NOW_1, NOW_1)
        body = telemetry.producer_body(
            core, telemetry._semantic_finding(record), NOW_1, submission_id=submission_id)
        request = issue_triage.prepare_request(
            self.remote.client(), telemetry.issue_title(record), body)
        return {
            'id': number + 1000,
            'number': number,
            'html_url': f'https://github.com/{REPO}/issues/{number}',
            'state': 'open',
            'milestone': None,
            **copy.deepcopy(request),
        }

    def test_single_matching_historical_report_is_adopted_without_posting(self):
        self.remote.issues.append(self._legacy_issue(submission_id='legacy-1', number=41))
        adopted = self.submit_telemetry()
        self.assertEqual(adopted['filing'], 'created')
        self.assertFalse(adopted['newly_created'])
        self.assertEqual(adopted['number'], 41)
        self.assertEqual(self.remote.posts, 0)

    def test_ambiguous_historical_reports_refuse_without_posting(self):
        self.remote.issues.extend([
            self._legacy_issue(submission_id='legacy-1', number=41),
            self._legacy_issue(submission_id='legacy-2', number=42, message='Another historical signal.'),
        ])
        with self.assertRaisesRegex(issue_author.IssueInputError, 'Multiple historical reports'):
            self.submit_telemetry()
        self.assertEqual(self.remote.posts, 0)


class RecoveryCallerTests(unittest.TestCase):
    """Fresh real clients/stores per observation; only remote/config boundaries are faked."""
    def setUp(self):
        self.remote = ReportRemote()
        self.activation = storage.initialize(self.remote.client())
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = telemetry.Cache(os.path.join(self.directory.name, 'streams.json'))

    def client(self):
        client = self.remote.client()
        client.recovery_store = storage.GitStore(client, self.activation)
        return client

    def snapshot(self):
        return storage.GitStore(self.remote.client(), self.activation).load()[1]

    def observe(self, now, records):
        return telemetry.run(self.client(), records, self.cache,
                             {'persistence': 1, 'auto_resolve': 1, 'triage_pressure': 10}, now,
                             authoritative=telemetry.AUTHORITATIVE_ALL, live=True)

    def uncertain(self, source='integration/durable-signal'):
        record = telemetry_data()['record']
        record['source_id'] = source
        self.remote.lose_issue_response = True
        self.assertTrue(self.observe(NOW_1, [record]).degraded)
        return record

    def test_empty_observation_recovers_and_closes_from_fresh_remote_state(self):
        self.uncertain()
        result = self.observe(NOW_2, [])
        self.assertFalse(result.degraded)
        self.assertEqual(result.recovery['state'], 'recovered')
        self.assertEqual((self.remote.posts, result.opened, result.closed), (1, 0, 1))
        self.assertEqual(self.remote.issues[0]['state'], 'closed')
        self.assertEqual([r['state'] for r in self.snapshot()['records'].values()], ['confirmed'])

    def test_closed_recovery_does_not_refile_until_a_distinct_observation(self):
        record = self.uncertain()
        self.remote.issues[0]['state'] = 'closed'
        result = self.observe(NOW_2, [record])
        self.assertFalse(result.degraded)
        self.assertEqual(self.remote.posts, 1)
        saved = list(self.snapshot()['records'].values())
        self.assertEqual((saved[0]['state'], saved[0]['closed_observation']), ('confirmed', NOW_2))
        self.assertEqual(self.observe(NOW_3, [record]).opened, 1)
        self.assertEqual(self.remote.posts, 2)
        self.assertEqual(sorted(r['generation'] for r in self.snapshot()['records'].values()), [1, 2])

    def test_incomplete_search_keeps_uncertain_record_and_never_posts_on_empty_observation(self):
        for failure in ('zero', 'multiple', 'partial'):
            with self.subTest(failure=failure):
                remote = ReportRemote()
                self.remote = remote
                self.activation = storage.initialize(remote.client())
                self.uncertain()
                before = self.snapshot()
                if failure == 'zero':
                    remote.issues.clear()
                elif failure == 'multiple':
                    other = copy.deepcopy(remote.issues[0])
                    other.update(id=9000, number=99, html_url=f'https://github.com/{REPO}/issues/99')
                    remote.issues.append(other)
                else:
                    remote.malformed_page = True  # all-state query only; open query stays usable
                result = self.observe(NOW_2, [])
                self.assertEqual(result.recovery['state'], 'held')
                self.assertEqual(remote.posts, 1)
                self.assertEqual(self.snapshot(), before)

    def test_multiple_pending_records_use_successive_durable_tips(self):
        for source in ('first/signal', 'second/signal'):
            self.remote.lose_issue_response = True
            data = telemetry_data()
            data['record']['source_id'] = source
            self.assertEqual(issue_author.create_producer_result(
                'telemetry', data, self.client())['filing'], 'creation-uncertain')
        result = self.observe(NOW_2, [])
        self.assertEqual(result.recovery['state'], 'recovered')
        self.assertEqual(len(result.recovery['results']), 2)
        self.assertEqual(self.remote.posts, 2)
        self.assertEqual([r['state'] for r in self.snapshot()['records'].values()], ['confirmed', 'confirmed'])

    def test_existing_report_is_recovered_before_refresh(self):
        record = self.uncertain()
        record['message'] = 'Fresh evidence after response loss.'
        result = self.observe(NOW_2, [record])
        self.assertEqual((result.recovery['state'], result.updated, self.remote.posts), ('recovered', 1, 1))
        self.assertIn(record['message'], self.remote.issues[0]['body'])
        self.assertEqual([r['state'] for r in self.snapshot()['records'].values()], ['confirmed'])

    def test_deleted_activated_ref_stays_held_while_existing_report_closes(self):
        self.uncertain()
        self.remote.ref = None
        result = self.observe(NOW_2, [])
        self.assertEqual((result.recovery['state'], result.closed, self.remote.posts), ('held', 1, 1))
        self.assertIsNone(self.remote.ref)

    def test_single_promotion_reports_a_hold_while_refreshing_the_existing_issue(self):
        import contextlib
        import io
        record = self.uncertain()
        self.remote.ref = None
        diagnostic = io.StringIO()
        with contextlib.redirect_stderr(diagnostic):
            number = telemetry.promote_finding(self.client(), record, NOW_2)
        self.assertEqual(number, 1)
        self.assertIn('recovery is held', diagnostic.getvalue())
        self.assertEqual(self.remote.posts, 1)

    def test_unactivated_existing_report_updates_and_closes(self):
        import issue_recovery
        record = self.uncertain()
        for now, records, count in ((NOW_2, [record], 'updated'), (NOW_3, [], 'closed')):
            client = self.remote.client()  # no injected store
            with patch.object(issue_recovery, 'load_activation', side_effect=issue_recovery.SetupRequired('setup required')):
                result = telemetry.run(client, records, self.cache,
                    {'persistence': 1, 'auto_resolve': 1, 'triage_pressure': 10}, now,
                    authoritative=telemetry.AUTHORITATIVE_ALL, live=True)
            self.assertEqual(result.recovery['state'], 'unactivated')
            self.assertEqual(getattr(result, count), 1)
            self.assertEqual(self.remote.posts, 1)


if __name__ == '__main__':
    unittest.main()
