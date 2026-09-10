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
import nightly_demo_report as nightly  # noqa: E402
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

    def test_nightly_report_recovers_after_response_loss_with_the_real_report_entry_point(self):
        self.remote = ReportRemote()
        self.activation = storage.initialize(self.remote.client())
        client = self.remote.client()
        client.recovery_store = self.store()
        self.remote.lose_issue_response = True
        with patch.dict(os.environ, ENV, clear=False):
            held = nightly.report(failed_nightly(), client, REPO)
            self.assertEqual(held['action'], 'held')
            self.assertEqual(self.remote.posts, 1)
            fresh = self.remote.client()
            fresh.recovery_store = self.store()
            recovered = nightly.report(failed_nightly(), fresh, REPO)
        self.assertEqual(recovered['action'], 'updated')
        self.assertEqual(self.remote.posts, 1)

    def test_nightly_failure_with_unavailable_exit_code_is_preserved_and_filed(self):
        self.remote = ReportRemote()
        self.activation = storage.initialize(self.remote.client())
        client = self.remote.client()
        client.recovery_store = self.store()
        unavailable = failed_nightly()
        unavailable['failures'][0]['exit_code'] = None
        with patch.dict(os.environ, ENV, clear=False):
            outcome = nightly.report(unavailable, client, REPO)
        self.assertEqual(outcome['action'], 'filed')
        self.assertEqual(self.remote.posts, 1)
        self.assertIn('`demo_durable.py` — exit None', self.remote.issues[0]['body'])

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


if __name__ == '__main__':
    unittest.main()
