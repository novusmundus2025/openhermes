"""Owner-scoped outbound local-browser transport. Device secrets never grant chat access."""
import hashlib
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from projects import ProjectError


class LocalBrowserBroker:
    def __init__(self, state):
        self.path = state / 'browser-devices.sqlite3'
        state.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.codes, self.online, self.pending = {}, {}, {}
        self.active = lambda uid: False
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS devices(uid TEXT PRIMARY KEY, token TEXT UNIQUE, selected INTEGER DEFAULT 0)')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def settings(self, uid):
        with self.lock, self.db() as db:
            row = db.execute('SELECT token,selected FROM devices WHERE uid=?', (uid,)).fetchone()
            return {'paired': bool(row and row[0]), 'online': time.monotonic()-self.online.get(uid, 0) < 100,
                    'transport': 'local' if row and row[1] else 'server'}

    def user_action(self, uid, body):
        action = body['action']
        with self.lock, self.db() as db:
            if action == 'local_pair':
                self.codes = {k:v for k,v in self.codes.items() if v[1] > time.monotonic() and v[0] != uid}
                code = secrets.token_urlsafe(24)
                self.codes[self.digest(code)] = (uid, time.monotonic()+300)
                return {**self.settings(uid), 'pair_code': code, 'expires_in': 300}
            if action == 'local_revoke':
                db.execute('DELETE FROM devices WHERE uid=?', (uid,))
                self.online.pop(uid, None)
                self.codes = {k:v for k,v in self.codes.items() if v[0] != uid}
                for item in self.pending.values():
                    if item['uid'] == uid:
                        item['result'] = {'success': False, 'error': 'Local browser disconnected'}
                        item['event'].set()
            elif action == 'local_select':
                if body.get('transport') not in ('server', 'local'):
                    raise ProjectError('Choose server or local browser')
                if body['transport'] == 'local' and not self.settings(uid)['paired']:
                    raise ProjectError('Pair your local browser first', 409)
                db.execute('INSERT INTO devices(uid,token,selected) VALUES(?,NULL,?) ON CONFLICT(uid) DO UPDATE SET selected=excluded.selected',
                           (uid, int(body['transport'] == 'local')))
        return self.settings(uid)

    def device(self, body):
        with self.lock:
            if body.get('action') == 'pair':
                code = self.codes.pop(self.digest(str(body.get('code', ''))), None)
                if not code or code[1] <= time.monotonic():
                    raise ProjectError('Pairing code is invalid or expired', 403)
                uid = code[0]
                if self.active(uid) or any(p['uid'] == uid for p in self.pending.values()):
                    raise ProjectError('Stop the browser session before pairing again', 409)
                token = secrets.token_urlsafe(48)
                with self.db() as db:
                    db.execute('INSERT INTO devices(uid,token,selected) VALUES(?,?,1) ON CONFLICT(uid) DO UPDATE SET token=excluded.token,selected=1', (uid,self.digest(token)))
                self.online[uid] = time.monotonic()
                return {'token': token, 'profile_id': self.digest('browser-profile:'+uid)}
            token = body.get('token')
            if not isinstance(token, str) or len(token) > 200:
                raise ProjectError('Invalid device credential', 403)
            with self.db() as db:
                row = db.execute('SELECT uid FROM devices WHERE token=?', (self.digest(token),)).fetchone()
            if not row:
                raise ProjectError('Local browser pairing revoked or expired', 403)
            uid = row[0]
            self.online[uid] = time.monotonic()
            if body.get('action') == 'poll':
                for cid, item in self.pending.items():
                    if item['uid'] == uid and not item['sent'] and not item['event'].is_set():
                        item['sent'] = True
                        return {'id': cid, 'command': item['command']}
                return {'id': None}
            if body.get('action') == 'result':
                item = self.pending.get(body.get('id'))
                if not item or item['uid'] != uid or not item['sent'] or item['event'].is_set():
                    raise ProjectError('Browser command expired or already completed', 409)
                result = body.get('result')
                if not isinstance(result, dict):
                    raise ProjectError('Invalid browser result')
                item['result'] = result
                item['event'].set()
                return {'ok': True}
            raise ProjectError('Unsupported device action')

    def request(self, uid, command):
        if not self.settings(uid)['online']:
            raise ProjectError('Local browser is offline. Run the companion on your computer.', 503)
        cid = uuid.uuid4().hex
        item = {'uid': uid, 'command': command, 'sent': False, 'event': threading.Event(), 'result': None}
        with self.lock:
            self.pending[cid] = item
        try:
            if not item['event'].wait(80):
                raise ProjectError('Local browser timed out. Check its connection.', 504)
            return item['result']
        finally:
            with self.lock:
                self.pending.pop(cid, None)


class LocalWorker:
    def __init__(self, broker, uid, sid):
        self.broker, self.uid, self.sid = broker, uid, sid

    def request(self, body):
        return self.broker.request(self.uid, {**body, 'session': self.sid})

    def close(self):
        if self.broker.settings(self.uid)['online']:
            try:
                self.request({'action': 'close'})
            except ProjectError:
                pass
