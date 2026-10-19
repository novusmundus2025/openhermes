"""Import trusted MundusX reward events only for explicitly linked devices."""
import hashlib
import json
import threading
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from credits import schema, award


def import_events(db, events, source):
    schema(db)
    imported = 0
    prefix = 'mundusx:' + hashlib.sha256(source.encode()).hexdigest()[:16] + ':'
    for event in events:
        device = event.get('device_id')
        owner = db.execute('SELECT user_id FROM contributor_devices WHERE device_id=?', (device,)).fetchone()
        if not owner or not event.get('id'):
            continue
        # Control-plane credit amounts are authoritative, including tool reward rules.
        reference = prefix + str(event['id'])
        known = db.execute('SELECT 1 FROM credit_ledger WHERE reference=?', (reference,)).fetchone()
        award(db, owner[0], event['amount'], 'contribution',
              'MundusX ' + str(event.get('entry_type', 'reward')), reference)
        imported += not bool(known)
    return imported


def sync(connect, source):
    if not source:
        return {'status': 'not_configured'}
    url = source['url'].rstrip('/')
    if not url.startswith('https://'):
        raise ValueError('Contribution source must use HTTPS')
    with connect() as db:
        schema(db)
        if not db.execute('SELECT 1 FROM contributor_devices LIMIT 1').fetchone():
            return {'status': 'awaiting_device_owner', 'imported': 0}
    imported = 0
    page = 1
    while page <= 1000:
        headers = {}
        if source.get('token'):
            headers['Authorization'] = 'Bearer ' + source['token']
        req = Request(url + '?' + urlencode({'page': page, 'page_size': 100}), headers=headers)
        with urlopen(req, timeout=20) as response:
            raw = response.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            raise ValueError('Contribution response too large')
        data = json.loads(raw)
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            imported += import_events(db, data['ledger'], url)
        if not data.get('pagination', {}).get('has_next'):
            return {'status': 'ok', 'imported': imported}
        page += 1
    raise RuntimeError('Contribution pagination limit exceeded')


def start_worker(connect, source):
    if not source:
        return
    def run():
        while True:
            try:
                result = sync(connect, source)
                if result.get('imported'):
                    print('Imported contribution rewards:', result['imported'], flush=True)
            except Exception as exc:
                # Avoid logging URLs, tokens, identities, or reward payloads.
                print('Contribution sync failed:', type(exc).__name__, flush=True)
            threading.Event().wait(300)
    threading.Thread(target=run, daemon=True, name='contribution-sync').start()
