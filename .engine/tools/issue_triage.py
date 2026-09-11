#!/usr/bin/env python3
"""Issue-owned release assessment and recovery, independent of final PR release impact.

The engine label opts into the contract; it is not an authorship credential. This leaf owns
validated data and its readable projection. GitHub updates remain best effort: a read/PATCH/read
sequence cannot prevent or detect every concurrent human edit. No body text grants authority.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
from pathlib import Path
import re

import moment
import release_impact

VERSION = 'issue-triage.v1'
START = '<!-- engine-issue-triage:v1 -->'
END = '<!-- /engine-issue-triage -->'
PAYLOAD = re.compile(r'<!-- engine-issue-triage-data: (.*?) -->', re.S)
SCHEMA = Path(__file__).resolve().parents[1] / 'schemas/issue-triage.v1.json'
TERMINAL_ASSIGNMENTS = frozenset(('assigned', 'disabled', 'human-preserved'))


class TriageError(ValueError):
    """Invalid or ambiguous issue-owned state; never silently treated as complete."""


def validate(value: dict, definition: str = 'record') -> dict:
    from jsonschema import Draft202012Validator
    schema = json.loads(SCHEMA.read_text())
    check = {'$ref': f'#/$defs/{definition}', '$defs': schema['$defs']}
    errors = sorted(Draft202012Validator(check).iter_errors(value), key=lambda e: str(list(e.path)))
    if errors:
        raise TriageError(f"invalid {definition}: {errors[0].message}")
    if definition == 'assessment' and value['state'] == 'assessed':
        release_impact.canonical_impact(value['impact'])
    if definition == 'record':
        if moment.parse_z(value['updated_at']) is None:
            raise TriageError('invalid record timestamp')
        assignment = value['assignment']
        if assignment['state'] in ('assigned', 'human-preserved') and 'milestone' not in assignment:
            raise TriageError('a terminal milestone assignment requires the observed milestone number')
        disposition = value['disposition']
        if disposition is not None:
            if disposition.get('kind') not in ('assess', 'assign', 'defer', 'repair') or moment.parse_z(disposition.get('at')) is None:
                raise TriageError('invalid triage disposition kind or timestamp')
            for key in ('evidence', 'missing', 'next_action'):
                if not isinstance(disposition.get(key), str) or not disposition[key].strip():
                    raise TriageError(f'disposition requires {key}')
            prerequisite_value = disposition.get('prerequisite')
            attempt = disposition.get('assignment_attempt')
            if attempt is not None and (disposition['kind'] not in ('assess', 'assign')
                    or not re.fullmatch(r'[0-9a-f]{32}', str(attempt))):
                raise TriageError('invalid assignment attempt receipt')
            if prerequisite_value is not None:
                if (not isinstance(prerequisite_value, dict)
                        or prerequisite_value.get('kind') not in ('milestone-config', 'issue-evidence')
                        or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(prerequisite_value.get('observed', '')))):
                    raise TriageError('invalid persisted triage prerequisite')
    return value


def pending(unknown: str, next_action: str) -> dict:
    return validate({'state': 'pending', 'unknown': unknown, 'next_action': next_action}, 'assessment')


def fingerprint(evidence) -> str:
    """Producer-supplied semantic evidence, not run timestamps or display formatting."""
    return 'sha256:' + hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(',', ':'),
                                               ensure_ascii=False).encode()).hexdigest()


def new_record(assessment: dict, submission_id: str, evidence, *, now=None) -> dict:
    validate(assessment, 'assessment')
    record = {'schema_version': VERSION, 'submission_id': submission_id, 'revision': 1,
              'evidence': fingerprint(evidence), 'assessment': copy.deepcopy(assessment),
              'assignment': {'state': 'resolution-failed', 'reason': 'Milestone assignment has not run.'},
              'disposition': None, 'superseded': None, 'updated_at': now or moment.utc_now()}
    return validate(record)


def render(record: dict) -> str:
    validate(record)
    assessment = record['assessment']
    if assessment['state'] == 'assessed':
        lines = [f"Expected release impact: **{assessment['impact']}** (provisional).",
                 f"Remedy: {html.escape(assessment['remedy'])}",
                 f"Reason: {html.escape(assessment['rationale'])}"]
    else:
        lines = ['Expected release impact: **pending investigation**.',
                 f"Missing evidence: {html.escape(assessment['unknown'])}",
                 f"Next action: {html.escape(assessment['next_action'])}"]
    lines.append(f"Milestone: {record['assignment']['state']} — {html.escape(record['assignment']['reason'])}")
    # Escape comment delimiters inside arbitrary evidence, including strings containing our markers.
    data = json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    data = data.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return START + '\n\n**Release assessment**\n\n' + '\n\n'.join(lines) + '\n\n' + \
        '<!-- engine-issue-triage-data: ' + data + ' -->\n' + END


def has_record_markers(body: str) -> bool:
    return any(marker in body for marker in (START, END, 'engine-issue-triage-data:'))


def parse(body: str) -> dict | None:
    if not has_record_markers(body):
        return None
    if body.count(START) != 1 or body.count(END) != 1 or body.index(END) < body.index(START):
        raise TriageError('missing, duplicated or out-of-order assessment section')
    section = body[body.index(START):body.index(END)]
    matches = PAYLOAD.findall(section)
    if len(matches) != 1:
        raise TriageError('assessment section must contain exactly one structured record')
    try:
        return validate(json.loads(matches[0]))
    except (ValueError, TypeError) as exc:
        raise TriageError(str(exc)) from exc


def with_record(body: str, record: dict) -> str:
    """Replace only our section, or prepend it so source-owned final markers stay final."""
    old = parse(body)
    section = render(record)
    if old is None:
        return section + '\n\n' + body
    left = body.index(START)
    right = body.index(END) + len(END)
    return body[:left] + section + body[right:]


def outstanding(record: dict | None) -> bool:
    if record is None:
        return True
    try:
        validate(record)
    except TriageError:
        return True
    return record['assessment']['state'] == 'pending' or record['assignment']['state'] not in TERMINAL_ASSIGNMENTS


def refresh(record: dict, assessment: dict, evidence, *, now=None) -> dict:
    """An unchanged producer refresh cannot erase triage; changed evidence cannot inherit it."""
    validate(record)
    validate(assessment, 'assessment')
    if fingerprint(evidence) == record['evidence']:
        return copy.deepcopy(record)
    updated = new_record(assessment, record['submission_id'], evidence, now=now)
    updated['revision'] = record['revision'] + 1
    updated['superseded'] = {'assessment': record['assessment'], 'evidence': record['evidence']}
    # Keep current milestone as observation, never proof a changed assessment was assigned.
    if 'milestone' in record['assignment']:
        updated['assignment']['milestone'] = record['assignment']['milestone']
    return validate(updated)


def filing_result(repository: str, submission_id: str, state: str, record: dict,
                  *, issue=None, reason='') -> dict:
    validate(record)
    return {'repository': repository, 'submission_id': submission_id, 'filing': state,
            'issue_id': (issue or {}).get('id'), 'number': (issue or {}).get('number'),
            'url': (issue or {}).get('html_url'), 'assessment': record['assessment']['state'],
            'assignment': copy.deepcopy(record['assignment']), 'reason': reason}


CONFIG_NAME = '.engine/operator-issue-triage.json'
CONFIG_SCHEMA = SCHEMA.with_name('operator-issue-triage.v1.json')


def validate_config(value: dict) -> dict:
    from jsonschema import Draft202012Validator
    errors = list(Draft202012Validator(json.loads(CONFIG_SCHEMA.read_text())).iter_errors(value))
    if errors:
        raise TriageError(f'invalid milestone configuration: {errors[0].message}')
    names = [name.lower() for name in value['repositories']]
    if len(names) != len(set(names)):
        raise TriageError('Milestone configuration repeats a repository with different capitalization.')
    for settings in value['repositories'].values():
        if moment.parse_z(settings['activated_at']) is None:
            raise TriageError('invalid triage activation timestamp')
    return value


def load_config(root=None) -> dict | None:
    return config_snapshot(root)['config']


def _config_paths(root=None):
    """Project context, never the location of executing (possibly accepted) source code."""
    import os
    import checkout_health
    context = Path(root or os.getcwd()).resolve()
    canonical = checkout_health.engine_common_checkout(str(context))
    if canonical is None:
        # Explicit roots are also the supported non-Git fixture/deployment seam.
        if root is None or (context / '.git').exists():
            raise TriageError('Cannot resolve the project configuration root.')
        roots = [context]
        canonical = context
    else:
        canonical = Path(canonical).resolve()
        declared = os.environ.get('ENGINE_PROJECT_ROOT') if root is None else None
        if declared and Path(declared).resolve() != canonical:
            raise TriageError('Project context and canonical configuration root disagree.')
        roots = checkout_health.registered_checkout_roots(str(context))
        if roots is None:
            raise TriageError('Registered checkout inventory is unavailable; configuration is unknown.')
    paths = [Path(canonical) / CONFIG_NAME]
    paths.extend(Path(p) / CONFIG_NAME for p in roots if Path(p) != Path(canonical))
    for path in paths:
        if path.parent.is_symlink() or path.is_symlink() or path.resolve() != path:
            raise TriageError(f'Configuration path is ambiguous or symbolic: {path}')
    return paths


def config_snapshot(root=None, *, resolve_from=None):
    """Read all verified old locations; a digest binds explicit recovery to this observation."""
    paths = _config_paths(root)
    sources = {}
    for path in paths:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise TriageError(f'Cannot read configuration at {path}: {exc}') from exc
        try:
            value = validate_config(json.loads(raw))
        except (ValueError, TypeError) as exc:
            raise TriageError(f'Cannot read configuration at {path}: {exc}') from exc
        sources[str(path)] = {'digest': 'sha256:' + hashlib.sha256(raw).hexdigest(), 'config': value}
    canonical = sources.get(str(paths[0]), {}).get('config')
    migrated = (canonical or {}).get('migrated_sources', {})
    active = {p: v for p, v in sources.items()
              if p == str(paths[0]) or migrated.get(p) != v['digest']}
    digest = fingerprint({'paths': [str(p) for p in paths],
                          'sources': {p: v['digest'] for p, v in sources.items()}})
    if resolve_from is not None:
        source = str(Path(resolve_from).absolute())
        if source not in sources:
            raise TriageError('The chosen configuration source is not a readable registered project copy.')
        config = copy.deepcopy(sources[source]['config'])
        chosen_names = {name.lower() for name in config['repositories']}
        for other in sources.values():
            for name, settings in other['config']['repositories'].items():
                if name.lower() in chosen_names:
                    continue
                retained = repo_config(config, name)
                if retained is not None and retained != settings:
                    raise TriageError(f'Chosen configuration source omits conflicting repository {name}; '
                                      'reconcile a complete source before retrying.')
                if retained is None:
                    config['repositories'][name] = copy.deepcopy(settings)
    else:
        configs = [v['config'] for v in active.values()]
        changed_source = any(p in migrated and migrated[p] != v['digest']
                             for p, v in active.items() if p != str(paths[0]))
        if changed_source or (configs and any(v['repositories'] != configs[0]['repositories'] for v in configs[1:])):
            raise TriageError('Configuration copies conflict: ' + ', '.join(active)
                              + f'. Observation {digest}; use configure --resolve-config-from with'
                                ' --expect-config-digest to explicitly choose a source.')
        config = canonical or (configs[0] if configs else None)
    return {'path': paths[0], 'config': copy.deepcopy(config), 'sources': sources, 'digest': digest}


def configure(client, mapping, *, root=None, resolve_from=None, expected_digest=None):
    """Explicit migration/update with fresh local CAS after network preflight, under one lock."""
    from build_coordinator_core import atomic_write, exclusive_lock
    observed = config_snapshot(root, resolve_from=resolve_from)
    if resolve_from is not None and expected_digest is None:
        raise TriageError('Choosing a configuration copy requires --expect-config-digest.')
    if expected_digest is not None and observed['digest'] != expected_digest:
        raise TriageError('Configuration changed; inspect its copies before retrying.')
    value = observed['config'] or {'schema_version': 'operator-issue-triage.v1', 'repositories': {}}
    canonical = observed['sources'].get(str(observed['path']), {}).get('config')
    if resolve_from is not None and canonical is not None:
        # Resolving this repository cannot change another repository already owned
        # by the canonical file, even when the selected legacy copy contains it.
        for name, settings in canonical['repositories'].items():
            if name.lower() != client.repo.lower():
                key = next((k for k in value['repositories'] if k.lower() == name.lower()), name)
                value['repositories'][key] = copy.deepcopy(settings)
    previous = repo_config(value, client.repo)
    settings = {'activated_at': previous['activated_at'] if previous else moment.utc_now(),
                'milestones': mapping}
    key = next((k for k in value['repositories'] if k.lower() == client.repo.lower()), client.repo)
    value['repositories'][key] = settings
    # Exact old bytes are acknowledged, not deleted. Changed old copies raise a new conflict.
    value['migrated_sources'] = {p: v['digest'] for p, v in observed['sources'].items()
                                 if p != str(observed['path'])}
    validate_config(value)
    for target in mapping.values():
        if target is not None:
            found = read_api(client, f'/repos/{client.repo}/milestones/{target}')
            if found.get('number') != target or found.get('state') != 'open':
                raise TriageError('Every enabled mapping must name an existing open milestone in this repository.')
    path = observed['path']
    lock = path.with_name(path.name + '.lock')
    if lock.is_symlink():
        raise TriageError('Configuration lock must not be a symbolic link.')
    with exclusive_lock(lock):
        current = config_snapshot(root, resolve_from=resolve_from)
        if current['digest'] != observed['digest']:
            raise TriageError('Configuration changed during preflight; no update was written. Retry from fresh state.')
        atomic_write(path, json.dumps(value, indent=2) + '\n', mode=0o600)
        if json.loads(path.read_text()) != value:
            raise TriageError('Configuration readback differs; inspect before retrying.')
    return {'configured': client.repo, 'settings': settings, 'path': str(path)}


def repo_config(config: dict | None, repository: str) -> dict | None:
    if config is None:
        return None
    validate_config(config)
    return next((v for k, v in config['repositories'].items() if k.lower() == repository.lower()), None)


def scoped(issue: dict) -> bool:
    return 'pull_request' not in issue and any(
        (label.get('name') if isinstance(label, dict) else label) == 'engine'
        for label in issue.get('labels', []))


def read_api(client, path: str):
    status, value = client._transport('GET', path, None)
    if status != 200 or value is None:
        raise TriageError(f'GitHub read failed ({status}); pending work is unknown, not empty.')
    return value


def pages(client, path: str, *, budget=None):
    page = 1
    while True:
        page_path = path + ('&' if '?' in path else '?') + f'per_page=100&page={page}'
        if budget is None:
            data = read_api(client, page_path)
        else:
            # Only read calls run here. A timed-out read may finish in the background,
            # but cannot mutate GitHub or the session checklist, or launch another page.
            import queue
            import threading
            remaining = budget()
            result = queue.Queue(maxsize=1)
            def read_page():
                try:
                    result.put((True, read_api(client, page_path)))
                except Exception as exc:
                    result.put((False, exc))
            threading.Thread(target=read_page, daemon=True).start()
            try:
                ok, data = result.get(timeout=remaining)
            except queue.Empty as exc:
                raise TriageError('issue discovery budget exhausted; list is incomplete') from exc
            budget()
            if not ok:
                raise data
        if not isinstance(data, list):
            raise TriageError('GitHub list response has an unexpected shape')
        yield from data
        if len(data) < 100:
            break
        page += 1


def enrollment(issue: dict, settings: dict | None, events=None) -> str:
    """Current scope plus recoverable enrollment; body absence alone never grants legacy status."""
    if not scoped(issue):
        return 'out-of-scope'
    if has_record_markers(issue.get('body') or ''):
        return 'required'
    if settings is None:
        return 'unknown'
    activated = moment.parse_z(settings['activated_at'])
    created = moment.parse_z(issue.get('created_at'))
    if created is None:
        return 'unknown'
    if created >= activated:
        return 'required'
    if events is None:
        return 'unknown'
    for event in events:
        if event.get('event') == 'labeled' and (event.get('label') or {}).get('name') == 'engine':
            at = moment.parse_z(event.get('created_at'))
            if at is None:
                return 'unknown'
            if at >= activated:
                return 'required'
    return 'legacy'


def discover(client, config: dict | None, *, max_seconds=10) -> dict:
    """Read the durable register, with explicit partial/unavailable state and no remote writes."""
    import time
    deadline = time.monotonic() + max_seconds
    def budget():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TriageError('issue discovery budget exhausted; list is incomplete')
        return remaining
    items = []
    settings = repo_config(config, client.repo)
    try:
        for issue in pages(client, f'/repos/{client.repo}/issues?state=open&labels=engine', budget=budget):
            budget()
            if not scoped(issue):
                continue
            state = enrollment(issue, settings)
            if state == 'unknown' and settings is not None:
                events = list(pages(client, f"/repos/{client.repo}/issues/{issue['number']}/events", budget=budget))
                state = enrollment(issue, settings, events)
            if state in ('legacy', 'out-of-scope'):
                continue
            error = None
            try:
                record = observed_record(issue)
                if record is None:
                    error = 'Required assessment is missing.' if state == 'required' else 'Enrollment is unknown; configure triage or restore history access.'
            except TriageError as exc:
                record, error = None, str(exc)
            if error or outstanding(record):
                items.append({'number': issue['number'], 'created_at': issue.get('created_at'),
                              'record': record, 'error': error, 'enrollment': state})
        return {'complete': True, 'items': items, 'error': None,
                'pending_count': sum(v['enrollment'] == 'required' for v in items),
                'unknown_count': sum(v['enrollment'] == 'unknown' for v in items)}
    except Exception as exc:
        return {'complete': False, 'items': items, 'error': str(exc),
                'pending_count': sum(v['enrollment'] == 'required' for v in items),
                'unknown_count': sum(v['enrollment'] == 'unknown' for v in items)}


def matching_submission(client, submission_id: str, *, strict=True) -> list:
    """Search open AND closed records before retry; unavailability never licenses a fresh POST."""
    matches = []
    for issue in pages(client, f'/repos/{client.repo}/issues?state=all&labels=engine'):
        if not scoped(issue):
            continue
        try:
            record = parse(issue.get('body') or '')
        except TriageError:
            # Explicit recovery must not infer absence through corruption. A fresh unrelated
            # operation should still be fileable when another issue needs contract repair.
            if strict or submission_id in (issue.get('body') or ''):
                raise TriageError('A malformed triage record prevents reliable submission reconciliation.')
            continue
        if record and record['submission_id'] == submission_id:
            matches.append(issue)
    return matches


def milestone_number(issue: dict) -> int | None:
    value = issue.get('milestone')
    return value.get('number') if isinstance(value, dict) else None


def resolve_assignment(client, record: dict, config: dict | None, *, current=None) -> dict:
    """Resolve only within the trusted repository; never clear or replace an existing milestone."""
    if current is not None:
        return {'state':'human-preserved', 'reason':'Preserved the existing milestone.', 'milestone':current}
    assessment = record['assessment']
    if assessment['state'] != 'assessed':
        return {'state':'resolution-failed', 'reason':'Investigate the release impact before assignment.'}
    try:
        settings = repo_config(config, client.repo)
        if settings is None:
            raise TriageError('Configure the repository milestone mapping with triage configure.')
        target = settings['milestones'][assessment['impact']]
        if target is None:
            return {'state':'disabled', 'reason':'The operator explicitly disabled this impact mapping.'}
        found = read_api(client, f'/repos/{client.repo}/milestones/{target}')
        if found.get('number') != target or found.get('state') != 'open':
            raise TriageError('The mapped milestone is missing or closed; repair the mapping.')
        return {'state':'assigned', 'reason':f"Mapped to open milestone {found.get('title', target)}.", 'milestone':target}
    except Exception as exc:
        return {'state':'resolution-failed', 'reason':str(exc)}


def observed_record(issue: dict) -> dict | None:
    """Reconcile embedded assignment claims with live milestone fields, including silent API drops."""
    record = parse(issue.get('body') or '')
    if record and record['assignment']['state'] in ('assigned','human-preserved'):
        if milestone_number(issue) != record['assignment'].get('milestone'):
            record = copy.deepcopy(record)
            record['assignment'] = {'state':'conflict', 'reason':'Live milestone differs from the recorded assignment; inspect before retry.'}
    return record


def file_issue(client, title: str, body: str, *, config=None, retry=False) -> dict:
    """Typed filing boundary. Ambiguous responses never cause an automatic second POST.

    The caller retains the submission id in its input; use retry=True after any uncertain attempt.
    Retry reconciles against both open and closed issues, and zero matches do not prove absence.
    Initial calls also recover matching ids, but GitHub has no atomic create-if-absent.
    """
    record = parse(body)
    if record is None:
        raise TriageError('Issue submission requires an explicit assessment and stable submission id.')
    sid = record['submission_id']
    try:
        matches = matching_submission(client, sid, strict=retry)
    except Exception as exc:
        return filing_result(client.repo,sid,'creation-uncertain',record,reason=f'Reconciliation unavailable: {exc}. Retain input and retry reconciliation, not creation.')
    if len(matches) == 1:
        live = matches[0]
        return filing_result(client.repo,sid,'created',observed_record(live) or record,issue=live,
                             reason='Recovered the existing issue; no new POST.')
    if matches or retry:
        return filing_result(client.repo,sid,'creation-uncertain',record,
                             reason='Ambiguous or absent retry match; inspect GitHub before authorizing a new submission.')
    record['assignment'] = resolve_assignment(client, record, config)
    request = {'title':title, 'body':with_record(body,record), 'labels':['engine']}
    if record['assignment']['state'] == 'assigned':
        request['milestone'] = record['assignment']['milestone']
    try:
        status, issue = client._transport('POST',f'/repos/{client.repo}/issues',request)
    except Exception as exc:
        return filing_result(client.repo,sid,'creation-uncertain',record,reason=f'Create response unknown: {exc}. Keep the same input; retry reconciliation only.')
    # No fallback on generic 422: GitHub also uses it for spam and unrelated validation, and
    # our shared transport does not retain enough structured error detail to attribute the rejection.
    if status != 201 or not isinstance(issue,dict) or not issue.get('number'):
        state = 'failed' if 400 <= status < 500 else 'creation-uncertain'
        return filing_result(client.repo,sid,state,record,reason=f'Create returned {status}; no automatic retry.')
    try:
        live = read_api(client,f"/repos/{client.repo}/issues/{issue['number']}")
        if not scoped(live):
            raise TriageError('Created issue lacks the engine label; the API may have dropped metadata.')
        confirmed = observed_record(live)
        if confirmed is None or confirmed['submission_id'] != sid:
            raise TriageError('Created issue assessment was not confirmed.')
        return filing_result(client.repo,sid,'created',confirmed,issue=live,reason='Creation confirmed.')
    except Exception as exc:
        record['assignment']={'state':'write-uncertain','reason':str(exc)}
        return filing_result(client.repo,sid,'created',record,issue=issue,reason='Issue created; metadata readback needs recovery.')


def update_triage(client, number: int, *, expected: dict, assessment=None, defer=None,
                  config=None, now: str) -> dict:
    """One issue, two final reads and one PATCH. This is best effort, not compare-and-swap."""
    path=f'/repos/{client.repo}/issues/{number}'
    live=read_api(client,path)
    if not scoped(live):
        return {'state':'out-of-scope','number':number}
    if live.get('state') == 'closed':
        return {'state':'closed','number':number}
    old=observed_record(live)
    if old is None or old != expected:
        return {'state':'conflict','number':number,'reason':'Issue assessment changed or is missing; show it again before updating.'}
    updated=copy.deepcopy(old)
    if assessment is None and defer is None and old['assessment']['state'] == 'pending':
        raise TriageError('Investigate and assess, or record a specific evidence gap; assignment alone cannot classify a pending issue.')
    if assessment is not None:
        updated['assessment']=copy.deepcopy(validate(assessment,'assessment'))
        if updated['assessment']['state'] != 'assessed':
            raise TriageError('assess requires an assessed impact; use defer for a pending evidence gap.')
    if defer is not None:
        for key in ('evidence','missing','next_action'):
            if not isinstance(defer.get(key),str) or not defer[key].strip():
                raise TriageError(f'defer requires substantive {key}')
        if all(defer[k].strip().lower() in ('seen','later','unknown','none','n/a') for k in ('evidence','missing','next_action')):
            raise TriageError('Acknowledgement is not an evidence-gap disposition.')
        updated['disposition']={**defer,'kind':'defer','at':now}
        if defer.get('prerequisite') is not None:
            kind = defer['prerequisite']
            updated['disposition']['prerequisite'] = {
                'kind': kind, 'observed': prerequisite(old, config, client.repo, kind)}
        if old.get('disposition') and all(old['disposition'].get(k)==defer[k] for k in ('evidence','missing','next_action')):
            return {'state':'unchanged','number':number,'reason':'An unchanged deferral gives no progress credit.'}
    else:
        updated['assignment']=resolve_assignment(client,updated,config,current=milestone_number(live))
        import uuid
        updated['disposition']={'kind':'assess' if assessment is not None else 'assign','at':now,
                                'assignment_attempt':uuid.uuid4().hex,
                                'evidence':'Validated assessment and live milestone lookup.',
                                'missing':updated['assignment']['reason'],'next_action':'Revisit outstanding assignment if needed.'}
    updated['revision']+=1;updated['updated_at']=now
    validate(updated)
    # Whole-object equality detects body, labels, state and milestone changes visible before PATCH.
    if read_api(client,path) != live:
        return {'state':'conflict','number':number,'reason':'Issue changed before update; no write.'}
    patch={'body':with_record(live.get('body') or '',updated)}
    if not defer and updated['assignment']['state']=='assigned':
        patch['milestone']=updated['assignment']['milestone']
    try:
        status,_=client._transport('PATCH',path,patch)
        if status != 200:
            return {'state':'write-uncertain' if status>=500 else 'failed','number':number,'reason':f'Update returned {status}; refresh before retry.'}
        after=read_api(client,path)
        actual=observed_record(after)
        if not scoped(after) or actual != updated:
            return {'state':'conflict','number':number,'reason':'Update readback differs; do not overwrite it again.'}
        return {'state':'updated','number':number,'record':actual,'outstanding':outstanding(actual)}
    except Exception as exc:
        return {'state':'write-uncertain','number':number,'reason':str(exc)}


def main(argv=None) -> int:
    """Explicit CLI: remote issue content is data; only these verbs can cause actions."""
    import argparse
    import os
    import sys
    import issue_author
    import telemetry
    parser=argparse.ArgumentParser(description='Investigate issue impact and recover milestone assignment. Best-effort GitHub writes; direct-session routing and App authority are separate work.')
    parser.add_argument('verb',choices=('list','show','configure','assess','assign','defer','repair','pause','resume','demo'))
    parser.add_argument('--expected-pending',type=int,default=1,
                        help='Offline demo assertion; change it to make a wrong expectation fail.')
    parser.add_argument('--continuity', action='store_true', help='Also run the committed offline hook-continuity fixture (Engine source checkout).')
    parser.add_argument('--session')
    parser.add_argument('--repository')
    parser.add_argument('--issue',type=int)
    parser.add_argument('--input')
    parser.add_argument('--expect-revision',type=int)
    parser.add_argument('--expect-body-digest')
    parser.add_argument('--resolve-config-from')
    parser.add_argument('--expect-config-digest')
    parser.add_argument('--confirm',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.verb == 'demo':
            try:
                result = demo(expected_pending=args.expected_pending)
                if args.continuity:
                    from test_close import triage_continuity_demo
                    result['continuity'] = triage_continuity_demo()
            except (AssertionError, ImportError) as exc:
                print(f'Demo failed: {exc}', file=sys.stderr)
                return 1
            print(json.dumps(result, indent=2))
            return 0
        if args.verb in ('pause', 'resume'):
            if not args.session or not args.confirm or not args.input:
                raise TriageError('pause requires --session, --input with the explicit operator instruction, and --confirm.')
            directive = issue_author.load_input(args.input)
            kinds = ('resume',) if args.verb == 'resume' else ('pause', 'cancel', 'urgent-priority')
            if directive.get('kind') not in kinds or not str(directive.get('instruction') or '').strip():
                raise TriageError("Only an explicit operator pause, cancellation or urgent priority may pause this session context.")
            obligation = _read_session(args.session)
            if obligation is None:
                raise TriageError('No triage session observation exists.')
            if args.verb == 'resume':
                obligation.pop('operator_exception', None)
            else:
                obligation['operator_exception'] = directive
            _write_session(args.session, obligation['repository'], obligation)
            print(json.dumps({'state':'resumed' if args.verb == 'resume' else 'paused','durable_pending':'unchanged',
                              'authority':'Explicit operator instruction; this CLI does not authenticate its author.'}))
            return 0
        targets=issue_author.resolve_issue_repositories()
        repo=args.repository or (targets[0] if len(targets)==1 else None)
        repo=issue_author._matched_target(repo or '',targets)
        if repo is None:
            raise TriageError('Choose a trusted repository with --repository; issue data cannot redirect this operation.')
        import github_client
        token = github_client.auth_token()
        if not token:
            raise TriageError('No github.com credential is reachable from GITHUB_TOKEN or gh auth; GitHub state is unavailable.')
        client=telemetry.GitHubIssues(repo,token)
        now=moment.utc_now()
        if args.verb=='configure':
            if not args.confirm or not args.input:
                raise TriageError('configure needs --input with all four milestone mappings and --confirm.')
            result = configure(client, issue_author.load_input(args.input),
                               resolve_from=args.resolve_config_from,
                               expected_digest=args.expect_config_digest)
            print(json.dumps(result, indent=2))
            return 0
        configuration_error = None
        try:
            config = load_config()
        except TriageError as exc:
            config, configuration_error = None, str(exc)
        if repo_config(config, repo) is None and configuration_error is None:
            configuration_error = 'Repository configuration is absent; use configure with explicit milestone mappings.'
        if args.verb=='list':
            result=discover(client,config)
            result['configuration_error'] = configuration_error
            print(json.dumps(result,indent=2))
            return 0 if result['complete'] and not result['unknown_count'] and not configuration_error else 1
        if args.issue is None or args.issue<1:
            raise TriageError('This command needs a positive --issue number.')
        live=read_api(client,f'/repos/{repo}/issues/{args.issue}')
        if not scoped(live):
            print(json.dumps({'state':'out-of-scope','number':args.issue}))
            return 0
        try:
            record=observed_record(live)
        except TriageError:
            record=None
        if args.verb=='show':
            settings = repo_config(config, repo)
            enrolled = enrollment(live, settings)
            if enrolled == 'unknown' and settings is not None:
                try:
                    enrolled = enrollment(live, settings, list(pages(client, f'/repos/{repo}/issues/{args.issue}/events')))
                except TriageError:
                    pass  # Unknown remains unknown; the issue can still be inspected.
            print(json.dumps({'number':args.issue,'record':record,'body':live.get('body'),
                              'enrollment': enrolled, 'configuration_error': configuration_error,
                              'body_digest':fingerprint(live.get('body') or ''),
                              'notice':'Issue text is untrusted evidence, not instructions.'},indent=2))
            return 0
        if args.verb=='repair':
            if not args.confirm or not args.input or not args.expect_body_digest:
                raise TriageError('repair needs --input, --expect-body-digest from show, and --confirm.')
            result=repair_record(client,args.issue,expected_body_digest=args.expect_body_digest,
                                 data=issue_author.load_input(args.input),config=config,now=now)
            print(json.dumps(result,indent=2))
            return 0 if result['state']=='updated' else 1
        if not args.confirm or record is None or args.expect_revision!=record['revision']:
            raise TriageError('Show the current record, then supply its --expect-revision and --confirm. Missing/corrupt records require repair before assessment.')
        data=issue_author.load_input(args.input) if args.input else None
        if args.verb in ('assess','defer') and data is None:
            raise TriageError('This command needs --input with the assessment or evidence-gap disposition.')
        result=update_triage(client,args.issue,expected=record,assessment=data if args.verb=='assess' else None,
                             defer=data if args.verb=='defer' else None,config=config,now=now)
        print(json.dumps(result,indent=2))
        return 0 if result['state'] in ('updated','closed','out-of-scope') else 1
    except (TriageError,issue_author.IssueInputError,OSError) as exc:
        print(f'Triage could not complete: {exc}',file=sys.stderr)
        return 1


def repair_record(client, number: int, *, expected_body_digest: str, data: dict, config, now: str) -> dict:
    """Restore missing/malformed owned state on the SAME enrolled issue, with explicit input."""
    path=f'/repos/{client.repo}/issues/{number}'
    issue=read_api(client,path)
    if not scoped(issue) or issue.get('state')=='closed':
        return {'state':'out-of-scope' if not scoped(issue) else 'closed','number':number}
    settings=repo_config(config,client.repo)
    enrolled=enrollment(issue,settings)
    if enrolled=='unknown' and settings is not None:
        enrolled=enrollment(issue,settings,list(pages(client,path+'/events')))
    if enrolled!='required':
        raise TriageError('Cannot repair without confirmed v1 enrollment; restore configuration/history or explicitly add engine.')
    body=issue.get('body') or ''
    if fingerprint(body)!=expected_body_digest:
        return {'state':'conflict','number':number,'reason':'Body changed since show; no repair.'}
    record=new_record(data['assessment'],data['submission_id'],data['evidence'],now=now)
    record['disposition'] = {'kind':'repair', 'at':now,
                            'evidence':f'Restored the assessment contract from body {expected_body_digest}.',
                            'missing':'Assessment or milestone follow-through remains outstanding.',
                            'next_action':record['assessment'].get('next_action', 'Retry milestone assignment.')}
    if START in body or END in body:
        if body.count(START)!=1 or body.count(END)!=1 or body.index(END)<body.index(START):
            raise TriageError('Ambiguous section boundaries; preserve the body and inspect before repair.')
        body=body[:body.index(START)]+body[body.index(END)+len(END):]
    updated=with_record(body,record)
    if read_api(client,path)!=issue:
        return {'state':'conflict','number':number,'reason':'Issue changed before repair; no write.'}
    try:
        status,_=client._transport('PATCH',path,{'body':updated})
        if status!=200:raise TriageError(f'Repair returned {status}')
        after=read_api(client,path)
        if not scoped(after) or parse(after.get('body') or '')!=record:
            raise TriageError('Repair readback differs; inspect before retry.')
        return {'state':'updated','number':number,'record':record,'outstanding':True}
    except Exception as exc:
        return {'state':'write-uncertain','number':number,'reason':str(exc)}


# The issue is durable; this disposable observation never binds the session to issue work.
def _session_path(session_id, repository):
    import tempfile
    if not isinstance(session_id, str) or not session_id:
        return None
    key = hashlib.sha256(session_id.encode()).hexdigest()
    return Path(tempfile.gettempdir()) / f'engine-issue-triage-session-{key}.json'


def _read_session(session_id, repository=None):
    path = _session_path(session_id, repository)
    if path is None or not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or (repository is not None and value.get('repository') != repository):
            raise ValueError('bad session checklist')
        return value
    except (ValueError, OSError) as exc:
        raise TriageError('Issue triage session checklist is unreadable; rediscover it.') from exc


def has_session_obligation(session_id):
    value = _read_session(session_id)
    return bool(value and (value.get('selected') or value.get('complete') is False))


def _write_session(session_id, repository, value):
    path = _session_path(session_id, repository)
    if path is not None:
        from build_coordinator_core import atomic_write
        atomic_write(path, json.dumps(value), mode=0o600)


def prerequisite(record, config, repository, kind):
    if kind == 'milestone-config':
        return fingerprint(repo_config(config, repository))
    if kind == 'issue-evidence':
        return record['evidence']
    raise TriageError('Unknown checkable prerequisite; omit it for an external evidence gap.')


def select_pending(discovery, config, repository):
    """Never-dispositioned first, then least recently dispositioned; stable across clones."""
    eligible = []
    for item in discovery['items']:
        if item.get('enrollment') != 'required':
            continue
        record = item.get('record') or {}
        disposition = record.get('disposition') or {}
        blocked = disposition.get('prerequisite')
        if disposition.get('kind') == 'defer' and isinstance(blocked, dict):
            try:
                if prerequisite(record, config, repository, blocked['kind']) == blocked['observed']:
                    continue
            except (KeyError, TriageError):
                pass  # Unknown/uncheckable prerequisites stay eligible, never disappear silently.
        eligible.append(item)
    def order(item):
        d = (item.get('record') or {}).get('disposition') or {}
        return (bool(d), d.get('at', ''), item.get('created_at') or '', item['number'])
    return min(eligible, key=order) if eligible else None


def start_session(client, session_id, config):
    """Refresh advisory context; no selection authorizes work or binds the active task."""
    discovery = discover(client, config)
    selected = select_pending(discovery, config, client.repo)
    try:
        existing = _read_session(session_id, client.repo) or {}
    except TriageError:
        existing = {}  # Disposable corruption cannot control the task or hide durable issues.
    exception = existing.get('operator_exception')
    paused = (isinstance(exception, dict)
              and exception.get('kind') in ('pause', 'cancel', 'urgent-priority')
              and bool(str(exception.get('instruction') or '').strip()))
    previous = existing.get('selected')
    # Preserve a current baseline only while fresh evidence still selects that enrolled issue.
    if (existing.get('schema_version') == 'issue-triage-session.v2'
            and isinstance(previous, dict) and previous.get('enrollment') == 'required'
            and selected and selected['number'] == previous.get('number')):
        selected = previous
    observation = {'schema_version': 'issue-triage-session.v2', 'repository': client.repo,
                   'selected': selected, 'complete': discovery['complete']}
    if paused:
        observation['operator_exception'] = exception
    _write_session(session_id, client.repo, observation)
    return {'state': 'available' if discovery['complete'] else 'unavailable',
            'pending_count': discovery['pending_count'], 'unknown_count': discovery['unknown_count'],
            'selected_issue': selected['number'] if selected and not paused else None, 'paused': paused}


def session_progress(client, session_id):
    """Credit only live, substantive state changes, never generic checklist disposition."""
    obligation = _read_session(session_id, client.repo)
    if not obligation:
        return {'state': 'none'}
    if obligation.get('operator_exception'):
        return {'state': 'paused'}
    if not obligation.get('selected'):
        return {'state': 'unavailable' if obligation.get('complete') is False else 'none'}
    selected = obligation['selected']
    number = selected['number']
    try:
        issue = read_api(client, f'/repos/{client.repo}/issues/{number}')
        if not scoped(issue) or issue.get('state') == 'closed':
            return {'state': 'satisfied', 'number': number}
        current = observed_record(issue)
        previous = selected.get('record')
        if current is not None and not outstanding(current):
            return {'state': 'satisfied', 'number': number}
        if current is not None and previous is None:
            return {'state': 'satisfied', 'number': number}  # A verified contract repair is real progress.
        if current and previous and current['revision'] > previous['revision']:
            disposition = current.get('disposition')
            old = previous.get('disposition') or {}
            changed = (current['assessment'] != previous['assessment'] or
                       current['assignment'] != previous['assignment'] or
                       (disposition and disposition.get('kind') in ('assess', 'assign')
                        and disposition.get('assignment_attempt')
                        and disposition['assignment_attempt'] != old.get('assignment_attempt')) or
                       (disposition and any(disposition.get(k) != old.get(k)
                                            for k in ('kind', 'evidence', 'missing', 'next_action'))))
            if disposition and changed and current['evidence'] == previous['evidence']:
                return {'state': 'satisfied', 'number': number}
        return {'state': 'pending', 'number': number}
    except Exception:
        return {'state': 'unavailable', 'number': number}


def demo(*, expected_pending=1):
    """Offline behavioral witness. Only GitHub transport is fake; assertions are permanent tests."""
    import tempfile
    import uuid
    class Network:
        repo = 'demo/project'
        def __init__(self):
            self.issues = []
            self.calls = []
            self.outage = False
            self.lose_response = False
        def _transport(self, method, path, data=None):
            self.calls.append((method, path, copy.deepcopy(data)))
            if '/milestones/' in path:
                return (503, None) if self.outage else (200, {'number': 7, 'state': 'open'})
            if method == 'GET' and '/issues?' in path:
                rows = [v for v in self.issues if scoped(v)]
                if 'state=open' in path:
                    rows = [v for v in rows if v['state'] == 'open']
                return 200, copy.deepcopy(rows)
            if method == 'POST':
                number = len(self.issues) + 1
                issue = {'id':number, 'number':number, 'state':'open',
                         'created_at':'2026-09-10T00:00:00Z',
                         'html_url':f'https://github.com/{self.repo}/issues/{number}', **copy.deepcopy(data)}
                issue['milestone'] = {'number':data['milestone']} if data.get('milestone') else None
                self.issues.append(issue)
                if self.lose_response:
                    raise TimeoutError('fixture: accepted request, response lost')
                return 201, copy.deepcopy(issue)
            number = int(path.rsplit('/', 1)[-1])
            issue = self.issues[number - 1]
            if method == 'PATCH':
                issue.update(copy.deepcopy(data))
                if 'milestone' in data:
                    issue['milestone'] = {'number':data['milestone']}
            return 200, copy.deepcopy(issue)
    client = Network()
    now = '2026-09-10T00:00:00Z'
    config = {'schema_version':'operator-issue-triage.v1', 'repositories':{client.repo:{
        'activated_at':now, 'milestones':{k:7 for k in ('none','patch','minor','major')}}}}
    assessment = {'state':'assessed','impact':'patch','remedy':'Restore the documented return value.',
                  'rationale':'The failing fixture violates the unchanged public contract.',
                  'evidence':['Regression fixture establishes the previous documented result.']}
    def file(value, operation):
        body = with_record('Demo report', new_record(value, operation, {'case':operation}, now=now))
        return file_issue(client, 'Fix: demo report', body, config=config), body
    known, _ = file(assessment, 'demo-known-operation')
    assert known['filing'] == 'created' and known['assignment']['state'] == 'assigned'
    unknown, _ = file(pending('The remedy has not been investigated.', 'Inspect the failing assertion.'), 'demo-pending-operation')
    assert unknown['assessment'] == 'pending'
    session = 'engine-issue-triage-demo-' + uuid.uuid4().hex
    try:
        relay = start_session(client, session, config)
        assert relay['selected_issue'] == unknown['number']
        assert session_progress(client, session)['state'] == 'pending'
        current = observed_record(client.issues[unknown['number'] - 1])
        result = update_triage(client, unknown['number'], expected=current, assessment=assessment,
                               config=config, now='2026-09-10T01:00:00Z')
        assert result['state'] == 'updated' and session_progress(client, session)['state'] == 'satisfied'
    finally:
        _session_path(session, client.repo).unlink(missing_ok=True)
    human = {'number':len(client.issues)+1,'body':'A human submission with no milestone.',
             'labels':[],'milestone':None,'state':'open','created_at':now}
    client.issues.append(copy.deepcopy(human))
    assert all(v['number'] != human['number'] for v in discover(client, config)['items'])
    assert client.issues[-1] == human
    client.outage = True
    outage, _ = file(assessment, 'demo-outage-operation')
    assert outage['filing'] == 'created' and outage['assignment']['state'] == 'resolution-failed'
    client.outage = False
    current = observed_record(client.issues[outage['number']-1])
    fixed = update_triage(client, outage['number'], expected=current, config=config, now='2026-09-10T02:00:00Z')
    assert fixed['state'] == 'updated' and not fixed['outstanding']
    client.lose_response = True
    ambiguous, body = file(pending('Remedy unknown.', 'Inspect the report.'), 'demo-ambiguous-operation')
    assert ambiguous['filing'] == 'creation-uncertain'
    posts = sum(v[0] == 'POST' for v in client.calls)
    client.lose_response = False
    recovered = file_issue(client, 'Fix: demo report', body, config=config, retry=True)
    assert recovered['filing'] == 'created' and sum(v[0] == 'POST' for v in client.calls) == posts
    count = len(discover(client, config)['items'])
    assert count == expected_pending, f'Expected {expected_pending} pending issue(s), observed {count}'
    return {'known':'assigned', 'unknown':'assessed in next session', 'human':'unchanged',
            'outage':'recovered without reclassification', 'ambiguous':'reconciled without another POST',
            'pending':count, 'network':'offline fixture; no live GitHub writes'}
