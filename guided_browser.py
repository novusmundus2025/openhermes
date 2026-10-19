"""Authenticated manual browser sessions backed by native Hermes browser tools."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit
import socket

from projects import ProjectError


def public_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ProjectError('Enter a public HTTP or HTTPS URL')
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ProjectError('Enter a valid public website URL')
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
        raise ProjectError('Use a public HTTP or HTTPS URL without embedded credentials')
    if port not in (None, 80, 443):
        raise ProjectError('Only standard public web ports are supported')
    try:
        addresses = socket.getaddrinfo(url.hostname, url.port or (443 if url.scheme == 'https' else 80), type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ProjectError('Private and local network URLs are not supported', 403)
    except socket.gaierror:
        raise ProjectError('Could not resolve this website')
    return value


class NativeWorker:
    def __init__(self, home):
        if not home.is_absolute() or home.resolve() != home or not re.fullmatch(r'[a-f0-9]{32}', home.name):
            raise ProjectError('Invalid private browser runtime path', 503)
        self.home = home
        self.home.mkdir(parents=True, mode=0o700, exist_ok=True)
        (home / 'workspace').mkdir(mode=0o700, exist_ok=True)
        import yaml
        (home / 'config.yaml').write_text(yaml.safe_dump({
            'browser': {'backend': 'off', 'allow_private_urls': False, 'auto_local_for_private_urls': False},
            'terminal': {'backend': 'docker', 'docker_image': 'openhermes-browser:1',
                         'cwd': str(home / 'workspace'), 'docker_mount_cwd_to_workspace': True,
                         'docker_volumes': [], 'docker_forward_env': [], 'home_mode': 'isolated'}}))
        from projects import PYTHON
        env = {k: v for k, v in os.environ.items() if k in ('HOME', 'PATH', 'LANG', 'USER', 'LOGNAME', 'TMPDIR')}
        env.update(HERMES_HOME=str(home), HERMES_SESSION_ID='guided-'+home.name, PYTHONUNBUFFERED='1',
                   TERMINAL_CONTAINER_MEMORY='2048', TERMINAL_CONTAINER_CPU='1')
        self.process = subprocess.Popen([PYTHON, '-I', str(Path(__file__).with_name('guided_browser_worker.py'))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, start_new_session=True)

    def request(self, body):
        raw = json.dumps(body).encode() + b'\n'
        self.process.stdin.write(raw)
        self.process.stdin.flush()
        deadline = time.monotonic() + 100
        chunks = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline-time.monotonic())):
                    break
                part = os.read(self.process.stdout.fileno(), 65536)
                if not part:
                    raise ProjectError('Browser worker ended. Start a new session.', 503)
                chunks.extend(part)
                if len(chunks) > 12 * 1024 * 1024:
                    raise ProjectError('Browser response exceeded the size limit', 503)
                if b'\n' in chunks:
                    return json.loads(chunks.split(b'\n', 1)[0])
        raise ProjectError('Browser operation timed out. Stop and restart the session.', 504)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()
        # Native Bot Desktop keeps its default sandbox alive across CLI exits.
        # Remove only containers with a bind mount inside THIS session's private home.
        ids = subprocess.run(['docker', 'ps', '-aq', '--filter', 'ancestor=openhermes-browser:1'],
                             capture_output=True, text=True, timeout=15, check=True).stdout.split()
        if ids:
            inspected = subprocess.run(['docker', 'inspect', *ids], capture_output=True, text=True, timeout=20, check=True)
            owned = [item['Id'] for item in json.loads(inspected.stdout)
                     if any(Path(mount.get('Source', '/')).is_relative_to(self.home)
                            for mount in item.get('Mounts', []) if mount.get('Type') == 'bind')]
            if owned:
                subprocess.run(['docker', 'rm', '--force', *owned], capture_output=True, timeout=30, check=True)
        if self.home.exists() and shutil.rmtree.avoids_symlink_attacks:
            # Sandbox startup may create root-owned mount directories. A networkless
            # helper can change ownership only beneath the validated private mount;
            # fwalk does not traverse links to shared Hermes tools or other users.
            if any(os.fstat(fd).st_uid != os.getuid() for _, _, _, fd in os.fwalk(self.home, follow_symlinks=False)):
                code = 'import os; [(os.fchown(fd,1000,1000)) for _,_,_,fd in os.fwalk("/cleanup",follow_symlinks=False)]'
                subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--user', '0:0',
                    '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'DAC_OVERRIDE',
                    '--security-opt', 'no-new-privileges', '--pids-limit', '32', '--memory', '128m',
                    '--mount', 'type=bind,src='+str(self.home)+',dst=/cleanup',
                    '--entrypoint', 'python3', 'openhermes-browser:1', '-c', code],
                    capture_output=True, timeout=30, check=True)
            # Hermes may install read-only tool directories in its private home.
            # Change only opened directory descriptors; never follow tool symlinks.
            for _, _, _, fd in os.fwalk(self.home, follow_symlinks=False):
                os.fchmod(fd, 0o700)
            shutil.rmtree(self.home)


class BrowserService:
    def __init__(self, ensure_user, state, factory=NativeWorker):
        self.ensure_user, self.state, self.factory = ensure_user, Path(state), factory
        self.sessions, self.lock = {}, threading.RLock()
        self.automation = None
        threading.Thread(target=self.reap, daemon=True).start()

    def reap(self):
        while True:
            time.sleep(30)
            expired = []
            with self.lock:
                for uid, session in list(self.sessions.items()):
                    if time.monotonic()-session['used'] > 900 and session['lock'].acquire(blocking=False):
                        self.sessions.pop(uid, None)
                        if self.automation and uid in self.automation.runs:
                            self.automation.runs.pop(uid)['cancel'].set()
                        expired.append(session)
            for session in expired:
                try:
                    session['worker'].close()
                except Exception:
                    pass
                finally:
                    session['lock'].release()

    def dispatch(self, user, body, actor=None):
        self.ensure_user(user)
        uid, action = user['id'], body.get('action')
        if action in ('auto_start','auto_status','pause'):
            if not self.automation:
                raise ProjectError('AI browser control is unavailable',503)
            if action=='auto_start':return self.automation.start(user,body)
            if action=='pause':return self.automation.pause(user)
            return self.automation.status(user)
        if action=='stop' and self.automation:
            self.automation.pause(user)
        if action not in ('start', 'status', 'navigate', 'refresh', 'click', 'type', 'back', 'scroll', 'stop'):
            raise ProjectError('Unsupported browser action')
        with self.lock:
            session = self.sessions.get(uid)
            if action == 'status':
                if self.automation:return self.automation.status(user)
                return {'active': bool(session), 'session_id': session['id'] if session else None,
                        'view': session['view'] if session else 0}
            if action == 'start':
                if not session:
                    if len(self.sessions) >= 4:
                        raise ProjectError('All guided browser slots are in use. Try again later.', 429)
                    sid = uuid.uuid4().hex
                    home = self.state / 'guided-browser' / hashlib.sha256(uid.encode()).hexdigest()[:32] / sid
                    session = {'id': sid, 'view': 0, 'used': time.monotonic(), 'lock': threading.Lock(),
                               'worker': self.factory(home), 'ready': False, 'snapshot': '', 'control':'human', 'frame':None}
                    self.sessions[uid] = session
                return {'active': True, 'session_id': session['id'], 'view': session['view']}
            if not session or body.get('session_id') != session['id']:
                raise ProjectError('Browser session not found or expired', 404)
            if actor:
                run=self.automation.runs.get(uid) if self.automation else None
                if not run or run['id']!=actor or run['cancel'].is_set() or session['control']!='auto':
                    raise ProjectError('AI no longer controls this browser',409)
            elif action!='stop' and session.get('control','human')!='human':
                raise ProjectError('Pause AI and take human control before acting',409)
            if not session['lock'].acquire(blocking=False):
                raise ProjectError('A browser operation is already running', 409)
        try:
            session['used'] = time.monotonic()
            if action == 'stop':
                session['worker'].close()
                with self.lock:
                    if self.sessions.get(uid) is session:
                        del self.sessions[uid]
                    if self.automation:self.automation.runs.pop(uid,None)
                return {'active': False}
            if action in ('click', 'type', 'back', 'scroll') and body.get('view') != session['view']:
                raise ProjectError('This view is stale. Refresh before acting.', 409)
            request = {'action': action}
            if action == 'navigate':
                request['url'] = public_url(body.get('url'))
            elif not session['ready']:
                raise ProjectError('Open a website first')
            if action in ('click', 'type'):
                ref = body.get('ref', '')
                if not isinstance(ref, str) or not re.fullmatch(r'@?e[0-9]{1,6}', ref):
                    raise ProjectError('Choose an element from the current snapshot')
                normalized = ref.lstrip('@')
                if not re.search(r'\[ref='+re.escape(normalized)+r'\]', session['snapshot']):
                    raise ProjectError('Element is not in the current snapshot', 409)
                request['ref'] = normalized
            if action == 'type':
                text = body.get('text')
                if not isinstance(text, str) or len(text) > 4096:
                    raise ProjectError('Text must be at most 4096 characters')
                request['text'] = text
            if action == 'scroll':
                if body.get('direction') not in ('up', 'down'):
                    raise ProjectError('Choose up or down')
                request['direction'] = body['direction']
            data = session['worker'].request(request)
            if not data.get('success'):
                raise ProjectError(data.get('error', 'Native browser action failed'), 502)
            public_url(data.get('url'))
            session['view'] += 1
            session['ready'] = True
            session['snapshot'] = str(data.get('snapshot', ''))[:80000]
            frame = {'active': True, 'session_id': session['id'], 'view': session['view'], 'control':session.get('control','human'),
                    'url': data['url'], 'title': str(data.get('title', ''))[:500],
                    'snapshot': session['snapshot'], 'image': data.get('image'),
                    'warning': data.get('warning')}
            session['frame']=frame
            return frame
        except ProjectError as exc:
            if exc.status >= 500:
                session['worker'].close()
                with self.lock:
                    if self.sessions.get(uid) is session:
                        del self.sessions[uid]
            raise
        except (OSError, ValueError):
            session['worker'].close()
            with self.lock:
                if self.sessions.get(uid) is session:
                    del self.sessions[uid]
            raise ProjectError('Browser worker failed. Stop and restart this session.', 503)
        finally:
            session['lock'].release()
            if session.get('control')=='pausing':session['control']='human'
