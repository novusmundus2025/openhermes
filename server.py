"""Private bridge between an Open WebUI Pipe and a multiplexed Hermes gateway."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import re
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from usage import Tracker, totals
from credits import account, initial

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("OPENHERMES_STATE", ROOT / "state"))
SCHEMA_LOCK = threading.Lock()


def connect():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(STATE / "users.sqlite3", timeout=210)
    db.execute('PRAGMA foreign_keys=ON')
    with SCHEMA_LOCK:
        if db.execute('PRAGMA journal_mode').fetchone()[0] != 'wal':
            db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL,
            profile TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
            created_at INTEGER NOT NULL, last_seen INTEGER NOT NULL)""")
    return db


def ensure_user(user, creator=None):
    uid = user.get("id")
    if not isinstance(uid, str) or not uid.strip() or len(uid) > 256:
        raise ValueError("Authenticated user ID is required")
    profile = "owui-" + hashlib.sha256(uid.encode()).hexdigest()[:32]
    now = int(time.time())
    db = connect()
    try:
        # Serialize provisioning across concurrent requests and processes.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status FROM users WHERE user_id=?", (uid,)).fetchone()
        db.execute("""INSERT INTO users VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,
            email=excluded.email, last_seen=excluded.last_seen""",
            (uid, str(user.get("name", ""))[:512], str(user.get("email", ""))[:512],
             profile, "pending", now, now))
        if not row or row[0] != "ready":
            if creator is None:
                creator = create_profile
            creator(profile)
            db.execute("UPDATE users SET status='ready' WHERE user_id=?", (uid,))
        db.commit()
        return profile
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def create_profile(profile):
    # Recover if profile creation succeeded before the database transaction committed.
    marker = STATE / (profile + ".ready")
    if not marker.exists():
        subprocess.run([sys.executable, str(ROOT / "provision.py"), profile],
                       check=True, capture_output=True, timeout=200)
        marker.write_text("ready\n")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Do not log user data, tokens, or request bodies.
        print("bridge", self.command, self.path, args[1] if len(args) > 1 else '', flush=True)

    def respond(self, status, data):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            return self.respond(200, {"status": "ok"})
        self.respond(404, {"error": "Not found"})

    def do_POST(self):
        if self.path in ('/browser', '/browser-local'):
            return self.browser()
        if self.path == '/coding':
            return self.coding()
        if self.path == '/projects':
            return self.projects()
        match = re.fullmatch(r'/provider/(owui-[0-9a-f]{32})/v1/chat/completions', self.path)
        if match:
            return self.provider(match.group(1))
        if self.path not in ("/chat", "/account"):
            return self.respond(404, {"error": "Not found"})
        expected = "Bearer " + self.server.settings["bridge_key"]
        if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            return self.respond(401, {"error": "Unauthorized"})
        tracker = None
        profile_lock = None
        completed = False
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8 * 1024 * 1024:
                return self.respond(413, {"error": "Invalid request size"})
            payload = json.loads(self.rfile.read(length))
            user, body = payload["user"], payload.get("body", {})
            if not isinstance(user, dict) or not isinstance(body, dict):
                raise ValueError("Invalid request")
            profile = ensure_user(user)
            policy = self.server.settings.get('credits')
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                initial(db, user['id'], policy)
            if self.path == "/account":
                with connect() as db:
                    stats = totals(db, user['id'])
                    credit_account = account(db, user['id'])
                    estimated = db.execute("SELECT count(*) FROM usage_events WHERE user_id=? AND billing_source='character_estimate'", (user['id'],)).fetchone()[0]
                data = dict(zip(
                    ('requests', 'prompt_tokens', 'completion_tokens', 'total_tokens',
                     'credits_spent_microunits', 'unrated_requests'), stats))
                data.update(credit_account)
                data['pricing_configured'] = bool(policy and 'version' in policy)
                data['estimated_requests'] = estimated
                return self.respond(200, data)
            # Whitelist fields; callers cannot select a different profile or session.
            outgoing = {k: body[k] for k in ("messages", "stream", "temperature",
                        "top_p", "max_tokens", "stop") if k in body}
            outgoing["model"] = "hermes-agent"
            prompt_chars = 0
            for message in outgoing.get('messages', []):
                content = message.get('content', '')
                if isinstance(content, str):
                    prompt_chars += len(content)
                elif isinstance(content, list):
                    prompt_chars += sum(len(part.get('text', '')) for part in content
                                        if isinstance(part, dict) and isinstance(part.get('text'), str))
            profile_lock = self.server.profile_locks[profile]
            profile_lock.acquire()
            tracker = Tracker(connect, user["id"], bool(outgoing.get("stream")), policy, prompt_chars)
            self.server.active_requests[profile] = tracker
            url = self.server.settings["upstream"].rstrip("/")
            req = Request(url + "/p/" + profile + "/v1/chat/completions",
                          data=json.dumps(outgoing).encode(), headers={
                              "Authorization": "Bearer " + hmac.new(
                                  self.server.settings["upstream_key"].encode(),
                                  profile.encode(), hashlib.sha256).hexdigest(),
                              "Content-Type": "application/json"})
            with urlopen(req, timeout=880) as response:
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                while True:
                    chunk = response.read1(8192)
                    if not chunk:
                        break
                    tracker.observe(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
                completed = True
        except (ValueError, KeyError, TypeError):
            self.respond(400, {"error": "Invalid authenticated user or request"})
        except HTTPError as exc:
            self.respond(exc.code, {"error": "Hermes rejected the request", "upstream_status": exc.code})
        except (URLError, TimeoutError, subprocess.SubprocessError, RuntimeError, OSError):
            self.respond(503, {"error": "Profile setup or Hermes unavailable; retry later"})
        finally:
            try:
                if tracker:
                    self.server.active_requests.pop(profile, None)
                    tracker.finish('completed' if completed else 'failed_or_interrupted')
            finally:
                if profile_lock:
                    profile_lock.release()

    def provider(self, profile):
        """Supply a usage-bearing stream for providers which omit stream usage."""
        key = hmac.new(self.server.settings['upstream_key'].encode(), profile.encode(), hashlib.sha256).hexdigest()
        model_key = hmac.new(self.server.settings['upstream_key'].encode(), ('model:' + profile).encode(), hashlib.sha256).hexdigest()
        auth = self.headers.get('Authorization', '')
        if not (hmac.compare_digest(auth, 'Bearer ' + model_key) or hmac.compare_digest(auth, 'Bearer ' + key)):
            return self.respond(401, {'error': 'Unauthorized'})
        with connect() as db:
            if not db.execute('SELECT 1 FROM users WHERE profile=? AND status=?', (profile, 'ready')).fetchone():
                return self.respond(403, {'error': 'Unknown profile'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16 * 1024 * 1024:
                return self.respond(413, {'error': 'Invalid request size'})
            body = json.loads(self.rfile.read(length))
            streaming = bool(body.get('stream'))
            body['stream'] = False
            body.pop('stream_options', None)
            headers = {'Content-Type': 'application/json'}
            if self.server.settings.get('model_upstream_key'):
                headers['Authorization'] = 'Bearer ' + self.server.settings['model_upstream_key']
            from provider_tools import completion_with_retry
            tracker = getattr(self.server, 'active_requests', {}).get(profile)
            def send_model(payload):
                req = Request(self.server.settings['model_upstream'].rstrip('/') + '/chat/completions',
                              data=json.dumps(payload).encode(), headers=headers)
                with urlopen(req, timeout=90) as response:
                    result = json.load(response)
                if tracker:
                    tracker.add_provider_usage(result.get('usage'))
                return result
            data = completion_with_retry(body, send_model)
            if not streaming:
                return self.respond(200, data)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            def send(value):
                self.wfile.write(('data: ' + json.dumps(value) + '\n\n').encode())
                self.wfile.flush()
            base = {k: data.get(k) for k in ('id', 'created', 'model')}
            base['object'] = 'chat.completion.chunk'
            for choice in data.get('choices', []):
                delta = dict(choice.get('message') or {})
                if delta.get('tool_calls'):
                    delta['tool_calls'] = [dict(call, index=i) for i, call in enumerate(delta['tool_calls'])]
                index = choice.get('index', 0)
                send(dict(base, choices=[{'index': index, 'delta': delta, 'finish_reason': None}]))
                send(dict(base, choices=[{'index': index, 'delta': {}, 'finish_reason': choice.get('finish_reason', 'stop')}]))
            send(dict(base, choices=[], usage=data.get('usage')))
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        except HTTPError as exc:
            detail=exc.read(4096).decode(errors='replace')
            if exc.code==502 and 'native OpenAI tool completion failed' in detail and 'status code 404' in detail:
                # A missing dependency is not a transient model overload. Avoid futile 5xx retries.
                self.respond(424, {'error': 'Provider tool-calling backend is unavailable: its internal completion endpoint returned HTTP 404 (upstream HTTP 502). No tool response was received.'})
            else:
                self.respond(exc.code, {'error': 'Model provider rejected the request (HTTP '+str(exc.code)+')'})
        except (URLError, TimeoutError, OSError, ValueError):
            self.respond(503, {'error': 'Model provider unavailable'})

    def coding(self):
        from projects import ProjectError
        if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.settings['bridge_key']):
            return self.respond(401,{'error':'Unauthorized'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=128*1024:
                return self.respond(413,{'error':'Coding request too large'})
            payload=json.loads(self.rfile.read(length))
            user,body=payload['user'],payload['body']
            if not isinstance(user,dict) or not isinstance(body,dict):raise ValueError()
            return self.respond(200,self.server.coding.dispatch(user,body))
        except ProjectError as exc:
            return self.respond(exc.status,{'error':str(exc)})
        except (ValueError,TypeError,KeyError):
            return self.respond(400,{'error':'Invalid coding request'})
        except Exception:
            return self.respond(503,{'error':'Coding service unavailable; no successful completion is confirmed'})

    def browser(self):
        from projects import ProjectError
        if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.settings['bridge_key']):
            return self.respond(401, {'error': 'Unauthorized'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= (10*1024*1024 if self.path == '/browser-local' else 16384):
                return self.respond(413, {'error': 'Browser request too large'})
            request = json.loads(self.rfile.read(length))
            if self.path == '/browser-local':
                if not isinstance(request, dict) or not isinstance(request.get('body'), dict):
                    raise ValueError()
                return self.respond(200, self.server.browser.local.device(request['body']))
            if not isinstance(request, dict) or not isinstance(request.get('user'), dict) or not isinstance(request.get('body'), dict):
                raise ValueError()
            return self.respond(200, self.server.browser.dispatch(request['user'], request['body']))
        except ProjectError as exc:
            return self.respond(exc.status, {'error': str(exc)})
        except (ValueError, KeyError, TypeError):
            return self.respond(400, {'error': 'Invalid browser request'})
        except Exception:
            return self.respond(503, {'error': 'Browser service unavailable'})

    def projects(self):
        from projects import ProjectError
        if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.settings['bridge_key']):
            return self.respond(401, {'error': 'Unauthorized'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2 * 1024 * 1024:
                return self.respond(413, {'error': 'Request too large'})
            request = json.loads(self.rfile.read(length))
            data = self.server.projects.dispatch(request['user'], request['body'])
            self.respond(200, data)
        except ProjectError as exc:
            self.respond(exc.status, {'error': str(exc)})
        except FileNotFoundError:
            self.respond(404, {'error': 'File not found'})
        except FileExistsError:
            self.respond(409, {'error': 'That name already exists'})
        except (PermissionError, OSError):
            self.respond(403, {'error': 'Project path is unavailable or unsafe'})
        except (ValueError, KeyError, TypeError):
            self.respond(400, {'error': 'Invalid project request'})
        except subprocess.TimeoutExpired:
            self.respond(503, {'error': 'Project setup timed out'})


def main():
    os.umask(0o077)
    settings = json.loads((ROOT / "settings.json").read_text())
    if len(settings["bridge_key"]) < 32:
        raise RuntimeError("Bridge key must be at least 32 characters")
    server = ThreadingHTTPServer((settings.get("host", "172.17.0.1"), settings.get("port", 8643)), Handler)
    server.settings = settings
    server.active_requests = {}
    server.profile_locks = defaultdict(threading.Lock)
    from projects import ProjectService
    server.projects = ProjectService(connect, ensure_user, settings, STATE)
    from coding import CodingService
    server.coding = CodingService(server.projects)
    from guided_browser import BrowserService
    server.browser = BrowserService(ensure_user, STATE)
    from browser_automation import BrowserAutomation
    server.browser.automation = BrowserAutomation(server.browser, settings, connect)
    from contributions import start_worker
    start_worker(connect, settings.get('contribution_source'))
    server.serve_forever()


if __name__ == "__main__":
    main()
