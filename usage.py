"""Per-request usage accounting. Missing provider usage is never treated as zero."""
import json
import time
import uuid
import threading


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS usage_events (
        request_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
        model TEXT NOT NULL, started_at INTEGER NOT NULL, finished_at INTEGER,
        status TEXT NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER,
        total_tokens INTEGER, credits_microunits INTEGER,
        credit_rate_version TEXT)''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(usage_events)')}
    for column in ('prompt_chars', 'output_chars'):
        if column not in columns:
            db.execute('ALTER TABLE usage_events ADD COLUMN ' + column + ' INTEGER')
    if 'billing_source' not in columns:
        db.execute('ALTER TABLE usage_events ADD COLUMN billing_source TEXT')
    db.execute('CREATE INDEX IF NOT EXISTS usage_user_idx ON usage_events(user_id, started_at)')


class Tracker:
    def __init__(self, connect, user_id, streaming, policy=None, prompt_chars=None):
        self.connect = connect
        self.request_id = uuid.uuid4().hex
        self.streaming = streaming
        self.buffer = b''
        self.usage = None
        self.policy = policy
        self.prompt_chars = prompt_chars
        self.output_chars = 0
        self.finalized = False
        self.failed = False
        self.provider_usage = None
        self.usage_lock = threading.Lock()
        with connect() as db:
            schema(db)
            db.execute('INSERT INTO usage_events (request_id,user_id,model,started_at,status,prompt_chars) VALUES (?,?,?,?,?,?)',
                       (self.request_id, user_id, 'hermes-agent', int(time.time()), 'started', prompt_chars))

    def observe(self, data):
        self.buffer += data
        if self.streaming:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                if line.startswith(b'data:'):
                    self.parse(line[5:].strip())
        elif len(self.buffer) > 8 * 1024 * 1024:
            raise ValueError('Model response exceeds accounting buffer limit')

    def parse(self, raw):
        try:
            value = json.loads(raw)
            usage = value.get('usage')
            if isinstance(usage, dict):
                self.usage = usage
            for choice in value.get('choices', []):
                if choice.get('finish_reason') in ('error', 'failed') or value.get('error'):
                    self.failed = True
                message = choice.get('delta' if self.streaming else 'message', {})
                content = message.get('content')
                if isinstance(content, str):
                    self.output_chars += len(content)
        except (ValueError, AttributeError):
            pass

    def add_provider_usage(self, usage):
        if not isinstance(usage, dict):
            return
        values = {k: usage.get(k) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
        if any(type(v) is not int or v < 0 for v in values.values()):
            return
        with self.usage_lock:
            if self.provider_usage is None:
                self.provider_usage = dict.fromkeys(values, 0)
            for key, value in values.items():
                self.provider_usage[key] += value

    def finish(self, status='completed'):
        if self.finalized:
            return
        if not self.streaming:
            self.parse(self.buffer)
        with self.usage_lock:
            if self.provider_usage is not None:
                self.usage = dict(self.provider_usage)
        usage = self.usage or {}
        if self.prompt_chars and usage.get('prompt_tokens') == 0 and usage.get('completion_tokens') == 0:
            self.usage = None
            usage = {}
        if self.failed:
            status = 'failed'
        def count(key):
            value = usage.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        if status == 'completed' and not self.usage:
            status = 'usage_unreported'
        with self.connect() as db:
            db.execute('''UPDATE usage_events SET finished_at=?,status=?,
                prompt_tokens=?,completion_tokens=?,total_tokens=?,output_chars=? WHERE request_id=?''',
                (int(time.time()), status, count('prompt_tokens'), count('completion_tokens'),
                 count('total_tokens'), self.output_chars, self.request_id))
            from credits import settle
            settle(db, self.request_id, self.policy)
        self.finalized = True


def totals(db, user_id):
    schema(db)
    return db.execute('''SELECT count(*),sum(prompt_tokens),sum(completion_tokens),
        sum(total_tokens),sum(credits_microunits),
        sum(CASE WHEN credits_microunits IS NULL THEN 1 ELSE 0 END)
        FROM usage_events WHERE user_id=?''', (user_id,)).fetchone()
