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


def parse(body: str) -> dict | None:
    if START not in body and END not in body and 'engine-issue-triage-data:' not in body:
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
