"""CardKit delivery: update receipts without replacing unsaved native form fields."""
import copy
import hashlib
import json
from urllib.parse import quote

LIVE_PARTS = ('bench_files', 'bench_sources', 'bench_feedback')


def schema(store):
    with store.connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS card_entities(
          send_key TEXT PRIMARY KEY,card_id TEXT NOT NULL,message_id TEXT UNIQUE,
          sequence INTEGER NOT NULL DEFAULT 0,card_json TEXT NOT NULL,chat_id TEXT NOT NULL DEFAULT '')''')
        if 'chat_id' not in {r['name'] for r in c.execute('PRAGMA table_info(card_entities)')}:
            c.execute("ALTER TABLE card_entities ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''")


def encoded(value): return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def find(value, element_id):
    if isinstance(value, dict):
        if value.get('element_id') == element_id: return value
        for child in value.values():
            match = find(child, element_id)
            if match is not None: return match
    elif isinstance(value, list):
        for child in value:
            match = find(child, element_id)
            if match is not None: return match
    return None


def reference(card_id): return {'type': 'card', 'data': {'card_id': card_id}}


def create(store, client, card, key, budget):
    schema(store)
    with store.connect() as c: row = c.execute('SELECT * FROM card_entities WHERE send_key=?', (key,)).fetchone()
    if row: return dict(row)
    raw = encoded(card)
    reply = client._json('/cardkit/v1/cards', body=encoded({'type': 'card_json', 'data': raw}).encode(), timeout=budget())
    cid = reply['data']['card_id']
    with store.connect() as c:
        c.execute('INSERT OR IGNORE INTO card_entities(send_key,card_id,card_json) VALUES(?,?,?)', (key, cid, raw))
        return dict(c.execute('SELECT * FROM card_entities WHERE send_key=?', (key,)).fetchone())


def send(store, client, chat, card, key, budget):
    row = create(store, client, card, key, budget)
    if row['message_id']: return row['message_id']
    reply = client._json('/im/v1/messages?receive_id_type=chat_id', body=encoded({
        'receive_id': chat, 'msg_type': 'interactive', 'content': encoded(reference(row['card_id'])), 'uuid': key}).encode(), timeout=budget())
    mid = reply['data']['message_id']
    with store.connect() as c: c.execute('UPDATE card_entities SET message_id=?,chat_id=? WHERE send_key=?', (mid, chat, key))
    return mid


def send_owner(store, client, owner, card, key, budget):
    """Create a new menu entity and obtain its native P2P identity once."""
    row = create(store, client, card, key, budget)
    if row['message_id'] and row['chat_id']: return row['message_id'], row['chat_id']
    reply = client._json('/im/v1/messages?receive_id_type=open_id', body=encoded({
        'receive_id': owner, 'msg_type': 'interactive', 'content': encoded(reference(row['card_id'])), 'uuid': key}).encode(), timeout=budget())
    mid, chat = reply['data']['message_id'], reply['data']['chat_id']
    if not mid.startswith('om_') or not chat.startswith('oc_'): raise ValueError('Menu P2P identity missing')
    with store.connect() as c: c.execute('UPDATE card_entities SET message_id=?,chat_id=? WHERE send_key=?', (mid, chat, key))
    return mid, chat


def update(store, client, mid, card, budget):
    schema(store)
    with store.connect() as c: row = c.execute('SELECT * FROM card_entities WHERE message_id=?', (mid,)).fetchone()
    if not row:
        if find(card, 'bench_form') is not None:
            raise ValueError('Legacy message cannot become a native form; reopen the menu')
        client._json('/im/v1/messages/' + quote(mid, safe=''), method='PATCH',
                     body=encoded({'content': encoded(card)}).encode(), timeout=budget())
        return
    row = dict(row); old = json.loads(row['card_json'])
    partial = find(old, 'bench_form') is not None and find(card, 'bench_form') is not None
    merged = copy.deepcopy(old) if partial else card
    actions = []
    if partial:
        for eid in LIVE_PARTS:
            previous, current = find(old, eid), find(card, eid)
            if previous is None or current is None: raise ValueError('Inline card live component missing')
            if previous != current:
                actions.append({'action': 'update_element', 'params': {'element_id': eid, 'element': current}})
                dest = find(merged, eid); dest.clear(); dest.update(current)
        if not actions: return
    elif old == card: return
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('UPDATE card_entities SET sequence=sequence+1 WHERE message_id=?', (mid,))
        sequence = c.execute('SELECT sequence FROM card_entities WHERE message_id=?', (mid,)).fetchone()[0]
    uuid = hashlib.sha256((row['card_id'] + ':' + str(sequence) + ':' + encoded(merged)).encode()).hexdigest()[:40]
    if partial:
        client._json('/cardkit/v1/cards/' + row['card_id'] + '/batch_update', method='POST',
                     body=encoded({'uuid': uuid, 'sequence': sequence, 'actions': encoded(actions)}).encode(), timeout=budget())
    else:
        client._json('/cardkit/v1/cards/' + row['card_id'], method='PUT',
                     body=encoded({'uuid': uuid, 'sequence': sequence, 'card': {'type': 'card_json', 'data': encoded(card)}}).encode(), timeout=budget())
    with store.connect() as c: c.execute('UPDATE card_entities SET card_json=? WHERE message_id=?', (encoded(merged), mid))
