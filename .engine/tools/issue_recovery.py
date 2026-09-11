"""Durable Engine submission lifecycle; Git state can record, but never replay, a send permit."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import uuid

from issue_recovery_store import GitStore, RecoveryError, REF, _decode_json

CONFIG_NAME = '.engine/operator-issue-recovery.json'
SCHEMAS = Path(__file__).resolve().parents[1] / 'schemas'
NOTICE = ('Engine creation publishes the intended issue content and recovery metadata to '
          + REF + '. It has repository visibility and persists in Git history. '
          'Contents write is repository-wide. Preview stores nothing. Never put secrets in issue content.')


class SetupRequired(RecoveryError):
    """This trusted repository has no recovery activation; existing updates remain available."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def validate(value, name='issue-recovery.v1'):
    from jsonschema import Draft202012Validator
    schema = json.loads((SCHEMAS / (name + '.json')).read_text())
    if list(Draft202012Validator(schema).iter_errors(value)):
        raise RecoveryError('Recovery metadata does not match ' + name + '; restore or inspect it, never reset it.')
    if name == 'issue-recovery.v1':
        if len(json.dumps(value).encode()) > 10 * 1024 * 1024:
            raise RecoveryError('Recovery snapshot exceeds 10 MiB; no unresolved record may be evicted.')
        identities = set()
        submissions = set()
        for key, record in value['records'].items():
            if len(json.dumps(record).encode()) > 1024 * 1024:
                raise RecoveryError('Recovery record exceeds 1 MiB.')
            expected = operation_key(record['producer'], record['source_key']) + ':' + str(record['generation'])
            identity = (record['producer'], record['source_key'], record['generation'])
            if key != expected or identity in identities or record['request_digest'] != digest(record['request']):
                raise RecoveryError('Recovery identity or frozen request digest is inconsistent.')
            identities.add(identity)
            if record['submission_id'] in submissions:
                raise RecoveryError('Recovery journal repeats a submission identity.')
            submissions.add(record['submission_id'])
            previous = value['records'].get(record['previous']) if record['previous'] else None
            if record['generation'] == 1:
                if record['previous'] is not None:
                    raise RecoveryError('First generation cannot have a predecessor.')
            elif (previous is None or previous['producer'] != record['producer']
                  or previous['source_key'] != record['source_key']
                  or previous['generation'] != record['generation'] - 1
                  or previous['state'] not in ('confirmed', 'superseded')):
                raise RecoveryError('Recovery generation has an invalid predecessor.')
            if record['state'] == 'prepared' and record['send_nonce'] is not None:
                raise RecoveryError('A prepared operation cannot already own a send claim.')
            if record['state'] == 'confirmed' and record['issue'] is None:
                raise RecoveryError('Confirmed recovery record is missing its issue identity.')
            if record['state'] in ('send-claimed', 'recovery-needed', 'rejected') and record['send_nonce'] is None:
                raise RecoveryError('Consumed operation is missing its send owner.')
    return value


def load_activation(repository, root=None):
    path = Path(root or Path(__file__).resolve().parents[2]) / CONFIG_NAME
    try:
        config = validate(_decode_json(path.read_text()), 'operator-issue-recovery.v1')
    except FileNotFoundError:
        raise SetupRequired('Recovery setup required: run issue_author.py recovery preview, then explicit recovery init --confirm.') from None
    except (OSError, ValueError) as exc:
        raise RecoveryError('Recovery configuration is unreadable; restore the existing activation.') from exc
    names = list(config['repositories'])
    if len({n.casefold() for n in names}) != len(names):
        raise RecoveryError('Recovery configuration repeats a repository identity.')
    activation = next((v for k, v in config['repositories'].items() if k.casefold() == repository.casefold()), None)
    if activation is None:
        raise SetupRequired('Recovery setup required for the trusted target repository.')
    return activation


def operation_key(producer, source_key):
    return digest([producer, source_key])


class SendPermit:
    """Process-local and consumed before transport. No durable value constructs this object."""
    def __init__(self, client, request):
        self._client, self._request, self._used = client, copy.deepcopy(request), False

    def __reduce__(self):
        raise TypeError('A send permit cannot be serialized.')

    def __call__(self, request):
        if self._used or request != self._request:
            raise RecoveryError('Send permit consumed or request changed.')
        self._used = True
        try:
            return self._client._transport('POST', f'/repos/{self._client.repo}/issues', request)
        except Exception:
            raise RecoveryError('Issue transport response unavailable; reconcile the same submission.') from None


def _load(store):
    tip, snapshot = store.load()
    return tip, validate(snapshot)


def _save(store, tip, snapshot, key, record):
    updated = copy.deepcopy(snapshot)
    updated['revision'] += 1
    updated['records'][key] = copy.deepcopy(record)
    validate(updated)
    from issue_recovery_store import GitStore as StoreContract
    StoreContract._history_transition(snapshot, updated)
    return store.compare_and_swap(tip, updated), updated


def _held(client, record, reason):
    import issue_triage
    triage = issue_triage.parse(record['request']['body'])
    result = issue_triage.filing_result(client.repo, record['submission_id'], 'creation-uncertain', triage, reason=reason)
    result['recovery'] = {'state': record['state'], 'operation': operation_key(record['producer'], record['source_key']),
                          'generation': record['generation']}
    return result


def _verified_issue(client, record, number):
    import issue_triage
    issue = issue_triage.read_api(client, f'/repos/{client.repo}/issues/{number}')
    triage = issue_triage.observed_record(issue)
    expected_url = f'https://github.com/{client.repo}/issues/{number}'
    if (type(issue.get('number')) is not int or issue['number'] != number
            or type(issue.get('id')) is not int or issue['id'] <= 0
            or not issue_triage.scoped(issue) or triage is None
            or triage['submission_id'] != record['submission_id']
            or str(issue.get('html_url', '')).casefold() != expected_url.casefold()
            or issue.get('state') not in ('open', 'closed')):
        raise RecoveryError('Issue target, scope or submission identity could not be confirmed.')
    known = record.get('issue')
    if known and (known['id'] != issue['id'] or known['number'] != number):
        raise RecoveryError('Confirmed issue identity changed.')
    return issue, triage


def _confirm(client, store, tip, snapshot, key, record, number, observation):
    import issue_triage
    live, triage = _verified_issue(client, record, number)
    updated = copy.deepcopy(record)
    updated.update(state='confirmed', issue={'id': live['id'], 'number': live['number'], 'url': live['html_url']})
    if live['state'] == 'closed' and updated['closed_observation'] is None:
        updated['closed_observation'] = observation
    if updated != record:
        _save(store, tip, snapshot, key, updated)
    result = issue_triage.filing_result(client.repo, record['submission_id'], 'created', triage,
                                     issue=live, reason='Existing submission confirmed; no replay.')
    result['newly_created'] = False
    return result


def reconcile(client, store, tip, snapshot, key, *, observation=None):
    """All-state, fully paginated reconciliation; absence is never permission to send."""
    import issue_triage
    record = snapshot['records'][key]
    try:
        if record['issue']:
            return _confirm(client, store, tip, snapshot, key, record, record['issue']['number'], observation)
        matches = issue_triage.matching_submission(client, record['submission_id'], strict=True)
        if len(matches) != 1:
            return _held(client, record, 'Recovery is held: zero or multiple matching issues; no new POST.')
        return _confirm(client, store, tip, snapshot, key, record, matches[0]['number'], observation)
    except Exception:
        return _held(client, record, 'Recovery is held: complete issue or journal verification was unavailable; no new POST.')


def submit(client, intent, prepare, *, producer='manual', source_key=None, observation=None,
           root=None, store=None, retry=False):
    """Resolve identity before rendering or UUID allocation; prepare(sid) returns a frozen Issue request.

    The common issue helper owns validating intent and rendering. Its callback is used only for a
    brand-new operation. A changed observation cannot alter an unresolved frozen request.
    """
    import issue_triage
    if producer not in ('manual', 'telemetry', 'nightly'):
        raise RecoveryError('Unknown Engine producer.')
    source_key = source_key or intent.get('submission_id')
    if not isinstance(source_key, str) or not source_key or len(source_key) > 1024:
        raise RecoveryError('A stable producer source key is required.')
    if producer != 'manual' and (not isinstance(observation, str) or not observation):
        raise RecoveryError('An automatic producer needs an authoritative observation identity.')
    store = store or GitStore(client, load_activation(client.repo, root))
    tip, snapshot = _load(store)
    group = operation_key(producer, source_key)
    previous = [(key, r) for key, r in snapshot['records'].items()
                if r['producer'] == producer and r['source_key'] == source_key]
    key, record = max(previous, key=lambda item: item[1]['generation']) if previous else (None, None)
    generation = 1
    previous_key = None
    if record:
        if record['state'] == 'confirmed':
            # First observation of closure only records closure. A later distinct observation
            # under the producer's existing recurrence policy can begin another generation.
            if (producer != 'manual' and record['closed_observation'] is not None
                    and observation != record['closed_observation'] and not retry):
                try:
                    live, _ = _verified_issue(client, record, record['issue']['number'])
                except Exception:
                    return _held(client, record, 'Prior issue closure could not be verified.')
                if live['state'] != 'closed':
                    return reconcile(client, store, tip, snapshot, key, observation=observation)
            else:
                return reconcile(client, store, tip, snapshot, key, observation=observation)
        elif record['state'] not in ('prepared', 'superseded') or retry:
            return reconcile(client, store, tip, snapshot, key, observation=observation)
        if record['state'] != 'prepared':
            generation, previous_key = record['generation'] + 1, key
            record = None
    if record is None:
        if retry:
            raise RecoveryError('No durable operation exists for this identity; retry cannot authorize creation.')
        sid = intent['submission_id'] if producer == 'manual' and generation == 1 else uuid.uuid4().hex
        request = prepare(sid)
        triage = issue_triage.parse(request['body'])
        if triage is None or triage['submission_id'] != sid or request.get('labels') != ['engine']:
            raise RecoveryError('Prepared Engine request has inconsistent scope or submission identity.')
        key = group + ':' + str(generation)
        record = {'producer': producer, 'source_key': source_key, 'generation': generation,
                  'previous': previous_key, 'submission_id': sid, 'intent': copy.deepcopy(intent),
                  'request': request, 'request_digest': digest(request), 'state': 'prepared',
                  'send_nonce': None, 'issue': None, 'decision': None,
                  'observation': observation, 'closed_observation': None}
        tip, snapshot = _save(store, tip, snapshot, key, record)
    # Recover a pre-existing identity before claiming; a failed read cannot license a send.
    try:
        matches = issue_triage.matching_submission(client, record['submission_id'], strict=True)
    except Exception:
        return _held(client, record, 'Initial complete reconciliation unavailable; no send claim.')
    if matches:
        return reconcile(client, store, tip, snapshot, key, observation=observation)
    claimed = copy.deepcopy(record)
    claimed.update(state='send-claimed', send_nonce=uuid.uuid4().hex)
    try:
        tip, snapshot = _save(store, tip, snapshot, key, claimed)
    except (RecoveryError, OSError):
        # A nonce visible on disk after response loss is not a new permit.
        return _held(client, claimed, 'Send-claim outcome is unknown or conflicted; reload and reconcile only.')
    permit = SendPermit(client, claimed['request'])
    result = issue_triage.file_issue(client, claimed['request']['title'], claimed['request']['body'],
                                    frozen_request=claimed['request'], send=permit)
    if result['filing'] == 'created' and result.get('number'):
        try:
            confirmed = _confirm(client, store, tip, snapshot, key, claimed, result['number'], observation)
            confirmed['newly_created'] = permit._used
            return confirmed
        except Exception:
            return _held(client, claimed, 'Issue or journal confirmation is incomplete; recover the same submission.')
    held = copy.deepcopy(claimed)
    held['state'] = 'rejected' if result['filing'] == 'failed' else 'recovery-needed'
    try:
        _save(store, tip, snapshot, key, held)
    except (RecoveryError, OSError):
        pass  # Durable send-claimed already holds recovery; never replay to repair a failed write.
    result['recovery'] = {'state': held['state'], 'record': key}
    return result


def supersede(store, key, *, expected_revision, reason, quiescent=False):
    """Operator-only new-attempt authorization; does not itself send or create a new identity."""
    if not quiescent or not isinstance(reason, str) or not reason.strip():
        raise RecoveryError('Supersede requires a reason and explicit confirmation that the previous writer is quiescent.')
    tip, snapshot = _load(store)
    if snapshot['revision'] != expected_revision or key not in snapshot['records']:
        raise RecoveryError('Recovery revision changed; inspect it again.')
    record = copy.deepcopy(snapshot['records'][key])
    if record['state'] in ('confirmed', 'superseded'):
        raise RecoveryError('This operation is already terminal; no supersede applied.')
    record.update(state='superseded', decision={'reason': reason, 'quiescent': True,
                  'residual_duplicate_risk': True})
    _save(store, tip, snapshot, key, record)
    return {'state': 'superseded', 'record': key, 'notice': 'A later submission may duplicate an unobserved issue; authorization is retained.'}


def main(argv=None):
    import argparse
    import issue_author
    import telemetry
    parser = argparse.ArgumentParser(description=NOTICE)
    parser.add_argument('verb', choices=('preview', 'init', 'list', 'show', 'reconcile', 'adopt', 'supersede'))
    parser.add_argument('--repository')
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--record')
    parser.add_argument('--issue', type=int)
    parser.add_argument('--producer', choices=('telemetry', 'nightly'))
    parser.add_argument('--source-key')
    parser.add_argument('--expect-revision', type=int)
    parser.add_argument('--reason')
    parser.add_argument('--quiescent', action='store_true')
    parser.add_argument('--genesis')
    parser.add_argument('--repository-id', type=int)
    args = parser.parse_args(argv)
    try:
        targets = issue_author.resolve_issue_repositories()
        repo = issue_author._matched_target(args.repository or (targets[0] if len(targets) == 1 else ''), targets)
        if repo is None:
            raise RecoveryError('Choose a trusted repository with --repository.')
        if args.verb == 'preview':
            print(json.dumps({'repository': repo, 'ref': REF, 'notice': NOTICE, 'writes': False}, indent=2))
            return 0
        import os
        token = os.environ.get('GITHUB_TOKEN')
        if not token:
            raise RecoveryError('GITHUB_TOKEN is required; no credential discovery is performed.')
        client = telemetry.GitHubIssues(repo, token)
        if args.verb == 'init':
            if not args.confirm:
                raise RecoveryError('Review recovery preview, then initialize with --confirm.')
            path = Path(__file__).resolve().parents[2] / CONFIG_NAME
            existing = (validate(_decode_json(path.read_text()), 'operator-issue-recovery.v1') if path.exists()
                        else {'schema_version': 'operator-issue-recovery.v1', 'repositories': {}})
            if any(k.casefold() == repo.casefold() for k in existing['repositories']):
                raise RecoveryError('Activation already exists; inspect or restore it instead of reinitializing.')
            if args.genesis or args.repository_id:
                if not args.genesis or not args.repository_id:
                    raise RecoveryError('Explicit restoration requires both --genesis and --repository-id.')
                activation = {'repository_id': args.repository_id, 'genesis': args.genesis}
                _load(GitStore(client, activation))
            else:
                from issue_recovery_store import initialize
                activation = initialize(client)
            existing['repositories'][repo] = activation
            validate(existing, 'operator-issue-recovery.v1')
            import build_coordinator_core
            build_coordinator_core.atomic_write(path, json.dumps(existing, indent=2) + '\n')
            print(json.dumps({'state': 'initialized', 'activation': activation, 'notice': NOTICE}))
            return 0
        store = GitStore(client, load_activation(repo))
        tip, snapshot = _load(store)
        if args.verb == 'list':
            print(json.dumps({'revision': snapshot['revision'], 'records': [
                {'record': key, 'state': r['state'], 'producer': r['producer'], 'generation': r['generation'], 'issue': r['issue']}
                for key, r in snapshot['records'].items()]}, indent=2))
            return 0
        if args.verb == 'adopt' and args.producer:
            if args.record or not args.confirm:
                raise RecoveryError('Initial legacy adoption needs --confirm and no --record.')
            result = issue_author.adopt_legacy_producer_issue(args.producer, client, number=args.issue,
                source_key=args.source_key, expected_revision=args.expect_revision, reason=args.reason,
                recovery_store=store)
            print(json.dumps(result, indent=2))
            return 0
        if args.producer or args.source_key:
            raise RecoveryError('--producer and --source-key are only for initial legacy adoption.')
        if args.record not in snapshot['records']:
            raise RecoveryError('Choose an existing --record from recovery list.')
        record = snapshot['records'][args.record]
        if args.verb == 'show':
            print(json.dumps({'revision': snapshot['revision'], 'record': record, 'notice': NOTICE}, indent=2))
            return 0
        if not args.confirm or args.expect_revision != snapshot['revision']:
            raise RecoveryError('Inspect the record, then provide --expect-revision and --confirm.')
        if args.verb == 'supersede':
            result = supersede(store, args.record, expected_revision=args.expect_revision,
                               reason=args.reason, quiescent=args.quiescent)
        elif args.verb == 'adopt':
            if not args.issue or args.issue < 1:
                raise RecoveryError('Adopt requires a positive --issue with the same submission marker.')
            result = _confirm(client, store, tip, snapshot, args.record, record, args.issue, None)
        else:
            result = reconcile(client, store, tip, snapshot, args.record)
        print(json.dumps(result, indent=2))
        return 0 if result.get('filing') == 'created' or result.get('state') == 'superseded' else 1
    except (RecoveryError, issue_author.IssueInputError) as exc:
        print('Recovery refused: ' + str(exc), file=__import__('sys').stderr)
        return 2
    except (ValueError, OSError):
        print('Recovery refused: configuration or journal data is unreadable; restore the existing activation.', file=__import__('sys').stderr)
        return 2
