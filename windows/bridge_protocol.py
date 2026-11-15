"""Bounded desktop-to-workstation intake; business state stays on workstation."""
import json
import re

MAX_TEXT = 20000
MAX_PACKET = 128 * 1024
FIELDS = {'version', 'project', 'session', 'text'}


def packet(project, session, text):
    value = {'version': 1, 'project': project, 'session': session, 'text': text}
    validate(value, project)
    return json.dumps(value, ensure_ascii=False)


def validate(value, expected_project):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError('Unexpected bridge packet fields')
    if type(value['version']) is not int or value['version'] != 1:
        raise ValueError('Unsupported bridge protocol')
    if not isinstance(expected_project, str) or not expected_project or value['project'] != expected_project:
        raise ValueError('CC Connect project does not match')
    session = value['session']
    if not isinstance(session, str) or not 1 <= len(session) <= 1024 or any(ord(c) < 32 for c in session):
        raise ValueError('Invalid bridge session')
    if not isinstance(value['text'], str) or not value['text'].strip() or len(value['text']) > MAX_TEXT:
        raise ValueError('Invalid message length')
    return value


def decode(raw, expected_project):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_PACKET:
        raise ValueError('Bridge packet too large')
    return validate(json.loads(raw), expected_project)


def validate_response(raw):
    """Return only the existing exec facade's JSONL, never SSH banners/errors."""
    if not isinstance(raw, str) or not raw or len(raw.encode('utf-8')) > 256 * 1024:
        raise ValueError('Invalid bridge response size')
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not events or events[0].get('type') != 'thread.started' or events[-1].get('type') != 'turn.completed':
        raise ValueError('Incomplete bridge response; do not resend automatically')
    for event in events:
        if not isinstance(event, dict) or event.get('type') not in ('thread.started', 'item.completed', 'turn.completed'):
            raise ValueError('Unexpected bridge event')
        if event['type'] == 'item.completed':
            item = event.get('item')
            if not isinstance(item, dict) or item.get('type') != 'agent_message' or not isinstance(item.get('text'), str):
                raise ValueError('Unexpected business response')
    return raw


def dispatch(value, expected_project, handler):
    """The workstation's original facade still owns validation and all effects."""
    value = validate(value, expected_project)
    return handler(value['session'], value['project'], value['text'])


def blocking_states(snapshot):
    """A zero lock or completed frame count cannot override unresolved jobs."""
    reasons = []
    if snapshot.get('hardware_lock_exists') is not False:
        reasons.append('hardware lock present or not verified')
    if snapshot.get('intake_processes') != []:
        reasons.append('message intake still active or not verified')
    for group in ('jobs', 'preparations', 'arrivals', 'archives', 'ai_requests', 'roadmodel_tasks', 'roadmodel_chains'):
        rows = snapshot.get(group)
        if not isinstance(rows, list):
            reasons.append(group + ' not verified')
            continue
        # Drafts and completed preparation are preserved, not running work.
        allowed = {'SUCCEEDED', 'SUCCESS', 'DONE', 'FAILED', 'CANCELLED', 'CANCELED',
                   'EXPIRED', 'ARCHIVED', 'DELIVERED', 'COMPLETED', 'PARTIAL_SUCCESS',
                   'DRAFT', 'STATIC_PASSED', 'NEEDS_INPUT', 'ACCEPTED'}
        for row in rows:
            if not isinstance(row, dict) or row.get('state') not in allowed:
                reasons.append(group + ': ' + str(row.get('id', row.get('task', '?'))) + ' ' + str(row.get('state')))
    return reasons


def frontend_switch_blockers(snapshot):
    """Connector-only handoff: preserve worker, tasks and hardware ownership."""
    reasons=[]
    for key in ('legacy_bridge_identity_verified','worker_identity_verified','source_hashes_match',
                'receiver_installed_verified','desktop_standby_lease','maintenance_enabled',
                'frozen_inputs_match'):
        if snapshot.get(key) is not True:reasons.append(key)
    if snapshot.get('worker_restart_planned') is not False:reasons.append('worker restart is not allowed')
    if snapshot.get('intake_processes')!=[]:reasons.append('intake still active or not verified')
    if snapshot.get('open_input_batches')!=0:reasons.append('input batches still active or not verified')
    # Other business states are deliberately not a reason to restart/modify them.
    return reasons
