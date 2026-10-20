"""Owner-scoped projects and Docker-contained Hermes jobs."""
import base64
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
import shlex
import sys
import shutil
from urllib.parse import urlparse

HERMES = '/home/a/.local/bin/hermes'
SOURCE = '/home/a/.hermes/hermes-agent'
PYTHON = '/home/a/.hermes/tools/python-3.14.7+20260901-linux-arm64/bin/python3'
MAX_FILE = 1024 * 1024


class ProjectError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def relative(path):
    if not isinstance(path, str) or '\\' in path or '\x00' in path or path.startswith('/'):
        raise ProjectError('Use a relative project path')
    parts = path.split('/') if path else []
    if any(p in ('', '.', '..') for p in parts):
        raise ProjectError('Invalid project path')
    if any(p == '.git' or p.startswith('.env') for p in parts):
        raise ProjectError('Private files cannot be opened here', 403)
    return parts


def open_parent(root, path):
    parts = relative(path)
    if not parts:
        raise ProjectError('Choose a file')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def read_bytes(root, path):
    parent, leaf = open_parent(root, path)
    try:
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
                raise ProjectError('Only regular files smaller than 1 MB can be opened')
            return file.read(MAX_FILE + 1)
    finally:
        os.close(parent)


def private_write(root, name, content):
    """Replace runtime configuration without following a container-created symlink."""
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = '.config-' + uuid.uuid4().hex
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            file.write(content)
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def save_file(root, path, content, expected):
    if not isinstance(content, str) or len(content.encode()) > MAX_FILE:
        raise ProjectError('File must be UTF-8 text smaller than 1 MB')
    parent, leaf = open_parent(root, path)
    try:
        try:
            existing = read_bytes(root, path)
        except FileNotFoundError:
            existing = None
        current = hashlib.sha256(existing).hexdigest() if existing is not None else None
        if expected != current:
            raise ProjectError('File changed since it was opened. Reload it before saving.', 409)
        # Publish atomically within a verified directory; do not follow an agent-created symlink.
        temporary = '.openhermes-save-' + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                file.write(content)
            os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
        return hashlib.sha256(content.encode()).hexdigest()
    finally:
        os.close(parent)


def tree(root, path=''):
    parts = relative(path)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        result = []
        with os.scandir(fd) as entries:
            for entry in entries:
                if entry.name in ('.git', 'node_modules', '.venv', '__pycache__') or entry.name.startswith('.env'):
                    continue
                if entry.is_symlink():
                    continue
                result.append({'name': entry.name, 'path': '/'.join(parts + [entry.name]),
                               'directory': entry.is_dir(follow_symlinks=False)})
                if len(result) >= 500:
                    break
        return sorted(result, key=lambda x: (not x['directory'], x['name'].lower()))
    finally:
        os.close(fd)


class ProjectService:
    def __init__(self, connect, ensure_user, settings, state):
        self.connect, self.ensure_user, self.settings, self.state = connect, ensure_user, settings, Path(state)
        self.container_lock = threading.Lock()
        with connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS deleted_projects (project_id TEXT PRIMARY KEY REFERENCES projects(id), deleted_at INTEGER NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
                profile TEXT NOT NULL, name TEXT NOT NULL, slug TEXT NOT NULL,
                root TEXT NOT NULL, created_at INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS project_jobs (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                kind TEXT NOT NULL, prompt TEXT NOT NULL, state TEXT NOT NULL,
                output TEXT NOT NULL, events TEXT NOT NULL, exit_code INTEGER,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)''')
            db.execute('CREATE TABLE IF NOT EXISTS project_chats (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL, created_at INTEGER NOT NULL)')
            if 'deleted_at' not in [r[1] for r in db.execute('PRAGMA table_info(project_chats)')]:
                db.execute('ALTER TABLE project_chats ADD COLUMN deleted_at INTEGER')
            if 'chat_id' not in [r[1] for r in db.execute('PRAGMA table_info(project_jobs)')]:
                db.execute("ALTER TABLE project_jobs ADD COLUMN chat_id TEXT NOT NULL DEFAULT 'legacy'")
            db.execute("INSERT OR IGNORE INTO project_chats (id,project_id,title,created_at) SELECT 'legacy-' || id,id,'Original chat',created_at FROM projects")
            db.execute("UPDATE project_jobs SET chat_id='legacy-' || project_id WHERE chat_id='legacy'")
            db.execute("UPDATE project_jobs SET state='interrupted',exit_code=130 WHERE state IN ('queued','running')")

    def project(self, uid, pid):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM projects WHERE id=? AND user_id=? AND id NOT IN (SELECT project_id FROM deleted_projects)', (pid, uid)).fetchone()
        if row is None:
            raise ProjectError('Project not found', 404)
        return dict(row)

    def run(self, args, **kwargs):
        return subprocess.run(args, capture_output=True, text=True, timeout=kwargs.pop('timeout', 30), **kwargs)

    def cleanup_project(self, project):
        """Remove only this managed project's local resources; never contact Git remotes."""
        pid, profile, slug = project['id'], project['profile'], project['slug']
        if not re.fullmatch(r'[a-f0-9]{32}', pid) or not re.fullmatch(r'owui-[a-f0-9]{32}', profile):
            raise ProjectError('Invalid managed project identity', 409)
        if not re.fullmatch(r'[a-z0-9-]+', slug):
            raise ProjectError('Invalid managed project directory', 409)
        expected = Path.home().resolve() / '.hermes' / 'profiles' / profile / 'workspace' / 'projects' / slug
        root = Path(project['root'])
        runtime = self.state.absolute() / 'project-runtime' / pid
        # Check every computed path before any irreversible operation.
        if root != expected or root.resolve() != expected or runtime.resolve() != runtime:
            raise ProjectError('Project cleanup path is unsafe', 409)
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ProjectError('Safe project cleanup requires Linux', 503)
        for path in (root, runtime):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise ProjectError('Project cleanup path is unsafe', 409)
        container = 'openhermes-project-' + pid
        with self.container_lock:
            inspected = self.run(['docker', 'inspect', container])
            if inspected.returncode == 0:
                info = json.loads(inspected.stdout)[0]
                if info['Config'].get('Labels', {}).get('org.openhermes.project') != pid:
                    raise ProjectError('Project container identity mismatch', 409)
                removed = self.run(['docker', 'rm', '--force', container], timeout=30)
                if removed.returncode:
                    raise ProjectError('Could not remove project container; deletion incomplete', 503)
            elif not any(message in inspected.stderr for message in ('No such object', 'No such container')):
                raise ProjectError('Could not inspect project container; deletion stopped', 503)
            for path in (root, runtime):
                if path.exists():
                    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        shutil.rmtree(path.name, dir_fd=parent)
                    finally:
                        os.close(parent)

    def create(self, user, profile, name, repository=None):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or any(ord(c) < 32 for c in name):
            raise ProjectError('Project name must have 1â€“60 characters')
        name = name.strip()
        if repository:
            parsed = urlparse(repository)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or len(repository) > 2000:
                raise ProjectError('Use an HTTPS Git URL without embedded credentials')
        pid = uuid.uuid4().hex
        slug = (re.sub('[^a-z0-9]+', '-', name.lower()).strip('-')[:40] or 'project') + '-' + pid[:8]
        root = Path.home() / '.hermes' / 'profiles' / profile / 'workspace' / 'projects' / slug
        root.mkdir(parents=True, mode=0o755)
        root.chmod(0o755)
        (root / 'README.md').write_text('# ' + name + '\n\nBuilt with Hermes.\n')
        (root / '.gitignore').write_text('node_modules/\n.venv/\n__pycache__/\n.env\n.env.*\n')
        from project_memory import initialize, PROJECT_STRUCTURE_GUIDANCE
        initialize(root)
        init = self.run(['git', 'init', '--quiet', str(root)])
        if init.returncode:
            raise ProjectError('Could not initialize the project repository', 503)
        self.run(['git', '-C', str(root), 'add', 'README.md', '.gitignore', 'AGENTS.md', '.hermes-project'])
        self.run(['git', '-C', str(root), '-c', 'user.name=OpenHermes', '-c', 'user.email=projects@localhost',
                  'commit', '--quiet', '-m', 'Create project'])
        registered = self.run([HERMES, '-p', profile, 'project', 'create', name, str(root), '--slug', slug], timeout=90)
        if registered.returncode:
            raise ProjectError('Hermes could not register the project', 503)
        with self.connect() as db:
            db.execute('INSERT INTO projects VALUES (?,?,?,?,?,?,?)',
                       (pid, user['id'], profile, name, slug, str(root), int(time.time())))
        project = self.project(user['id'], pid)
        if repository:
            command = ('git -c protocol.file.allow=never -c protocol.ext.allow=never clone --depth 1 --no-recurse-submodules '
                       + shlex.quote(repository) + ' /tmp/openhermes-clone && cp -a /tmp/openhermes-clone/. /workspace/')
            self.job(project, 'command', command)
        return project

    def container(self, project):
        import yaml
        name = 'openhermes-project-' + project['id']
        with self.container_lock:
            inspected = self.run(['docker', 'inspect', name])
            if inspected.returncode == 0:
                info = json.loads(inspected.stdout)[0]
                if info['Config'].get('Labels', {}).get('org.openhermes.project') != project['id']:
                    raise ProjectError('Project container identity mismatch', 503)
                # Existing containers use bind-mounted configuration too. Refresh
                # execution policy rather than retaining budgets from creation day.
                runtime = self.state / 'project-runtime' / project['id']
                config = yaml.safe_load(read_bytes(runtime, 'config.yaml')) or {}
                config.setdefault('agent', {}).update(max_turns=16, run_budget_seconds=600)
                config.setdefault('model', {})['context_length']=65536
                private_write(runtime, 'config.yaml', yaml.safe_dump(config))
                if not info['State']['Running']:
                    result = self.run(['docker', 'start', name])
                    if result.returncode:
                        raise ProjectError('Could not restart the project container', 503)
                return name
            home = self.state / 'project-runtime' / project['id']
            home.mkdir(parents=True, mode=0o700, exist_ok=True)
            (home / 'home').mkdir(exist_ok=True)
            borrowed = home / 'installs'
            if not borrowed.exists():
                borrowed.symlink_to('/home/a/.hermes/installs', target_is_directory=True)
            tools = home / 'tools'
            if not tools.exists():
                tools.symlink_to('/home/a/.hermes/tools', target_is_directory=True)
            source = yaml.safe_load((Path.home() / '.hermes' / 'profiles' / project['profile'] / 'config.yaml').read_text())
            model_key = hmac.new(self.settings['upstream_key'].encode(), ('model:' + project['profile']).encode(), hashlib.sha256).hexdigest()
            model = dict(source.get('model') or {})
            context=65536
            model.update(provider='custom', base_url='http://172.17.0.1:8643/provider/' + project['profile'] + '/v1',
                         key_env='OPENHERMES_MODEL_KEY', streaming=False, context_length=context)
            model.pop('api_key', None)
            config = {'model': model, 'terminal': {'backend': 'local', 'cwd': '/workspace'},
                      'agent': {'max_turns': 16, 'run_budget_seconds': 600}, 'display': {'streaming': True},
                      'platform_toolsets': {'cli': ['terminal', 'file']}}
            private_write(home, 'config.yaml', yaml.safe_dump(config))
            private_write(home, '.env', 'OPENHERMES_MODEL_KEY=' + model_key + '\n')
            result = self.run(['docker', 'run', '-d', '--name', name, '--user', '1000:1000',
                '--label', 'org.openhermes.project=' + project['id'], '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--memory', '2g', '--cpus', '2', '--pids-limit', '256',
                '--mount', 'type=bind,src=' + project['root'] + ',dst=/workspace',
                '--mount', 'type=bind,src=' + str(home) + ',dst=/state',
                '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m', 'openhermes-project:1'], timeout=90)
            if result.returncode:
                raise ProjectError('Could not start the project container', 503)
        return name

    def job(self, project, kind, prompt, chat_id=None):
        if kind not in ('agent', 'command') or not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 32000:
            raise ProjectError('Enter a command or message')
        chat_id = chat_id or 'legacy-' + project['id']
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO project_chats (id,project_id,title,created_at) VALUES (?,?,?,?)", ('legacy-' + project['id'], project['id'], 'Original chat', int(time.time())))
            if not db.execute('SELECT 1 FROM project_chats WHERE id=? AND project_id=? AND deleted_at IS NULL', (chat_id, project['id'])).fetchone():
                raise ProjectError('Chat not found', 404)
        project = dict(project, chat_id=chat_id)
        jid, now = uuid.uuid4().hex, int(time.time())
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM deleted_projects WHERE project_id=?', (project['id'],)).fetchone():
                raise ProjectError('Project not found', 404)
            if db.execute("SELECT 1 FROM project_jobs WHERE project_id=? AND state IN ('queued','running')", (project['id'],)).fetchone():
                raise ProjectError('This project already has a running task', 409)
            db.execute('INSERT INTO project_jobs (id,project_id,kind,prompt,state,output,events,exit_code,created_at,updated_at,chat_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (jid, project['id'], kind, prompt, 'queued', '', '[]', None, now, now, chat_id))
            if kind == 'agent':
                db.execute("UPDATE project_chats SET title=? WHERE id=? AND title IN ('New chat','Original chat')", (prompt.strip()[:65], chat_id))
        threading.Thread(target=self.worker, args=(project, jid, kind, prompt), daemon=True).start()
        return {'job_id': jid}

    def worker(self, project, jid, kind, prompt):
        output, events, exit_code = '', [], 1
        tracker = None
        timer = None
        unexecuted_tools = False
        try:
            container = self.container(project)
            from project_memory import initialize, PROJECT_STRUCTURE_GUIDANCE
            initialize(project['root'])
            with self.connect() as db:
                db.execute("UPDATE project_jobs SET state='running',updated_at=? WHERE id=?", (int(time.time()), jid))
            if kind == 'command':
                command = ['docker', 'exec', '-i', '--workdir', '/workspace', container,
                           'timeout', '--signal=TERM', '--kill-after=5', '60', 'bash', '-lc', prompt]
                input_data = None
            else:
                from usage import Tracker
                tracker = Tracker(self.connect, project['user_id'], False, self.settings.get('credits'), len(prompt))
                command = ['docker', 'exec', '-i', '--workdir', '/workspace', container,
                           'timeout', '--signal=TERM', '--kill-after=5', '600', PYTHON, '-I', '-c',
                           "import sys,runpy;sys.path.insert(0,'" + SOURCE + "');import hermes_bootstrap;import hermes_cli.stream_json as _sj;_sj._TOOL_OUTPUT_CAP=65536;runpy.run_module('hermes_cli.main',run_name='__main__')",
                           'chat', '--cli', '--query-file', '-', '--oneshot', '--format', 'stream-json',
                           '--in', '/workspace', '--continue', ('project-' + project['id'] if project.get('chat_id', '').startswith('legacy-') else 'chat-' + project['chat_id']), '--create-if-missing',
                           '--max-turns', '16', '-t', 'terminal,file']
                input_data = (
                    PROJECT_STRUCTURE_GUIDANCE + '\n\n' +
                    'Read /workspace/AGENTS.md, .hermes-project/PLAN.md, .hermes-project/CHECKPOINT.md and recent .hermes-project/tasks/TASK-*.md records first. '
                    'Write an actionable .hermes-project/PLAN.md before substantial work. Update progress and .hermes-project/CHECKPOINT.md with '
                    'verified results, tests, blockers and next steps before ending. Never store secrets in these files. '
                    'Project execution instructions: Work in /workspace. Before modifying an existing file, '
                    'call read_file to read its FULL CURRENT contents in this task, paging to the end if necessary. '
                    'Terminal cat output and copies from earlier conversation turns do not establish the native '
                    'file-tool read baseline. Files created by npm init or another terminal command already exist: '
                    'read_file them before write_file. Prefer patch for small changes, matching exact current text. '
                    'If patch fails to match or write_file reports stale_write_blocked, read_file again, merge '
                    'the intended change with the current contents, retry, and verify with read_file and git diff. '
                    'Do not repeat a failed write unchanged or claim it succeeded. Terminal cwd may persist; use '
                    'absolute paths for cd. Report unresolved failures honestly.\n\nUser request:\n' + prompt)
            if kind == 'agent':
                from provider_tools import explanation_request
                if explanation_request(prompt):
                    command[command.index('--continue')+1] = 'explain-'+jid
                    input_data = ('Read-only explanation. Inspect package/build configuration and the actual entry point, then follow relevant imports with read_file. For Node apps npm start uses scripts.start, not necessarily the package main library field. Explain the current program flow in chat. Do not edit files, write plans, run tests, or continue earlier repairs. State unknowns.\n\nUser request:\n' + prompt)
                command = [sys.executable, str(Path(__file__).with_name('agent_recovery.py')), '--workspace-root', project['root'], '--'] + command
            with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, bufsize=1) as process:
                expired = threading.Event()
                def expire():
                    expired.set()
                    self.run(['docker', 'stop', '--time', '3', container], timeout=15)
                timer = threading.Timer(630 if kind == 'agent' else 70, expire)
                timer.daemon = True
                timer.start()
                if input_data is not None:
                    process.stdin.write(input_data)
                process.stdin.close()
                last_update = 0
                for line in iter(lambda: process.stdout.readline(MAX_FILE), ''):
                    # Bound output stored in SQLite; rendering is always text, never HTML.
                    if len(output) < MAX_FILE:
                        output += line[:MAX_FILE - len(output)]
                    if kind == 'agent':
                        try:
                            event = json.loads(line)
                            if event.get('type') in ('system', 'text', 'tool_use', 'tool_result', 'result'):
                                events.append(event)
                                events = events[-300:]
                                if event.get('type') == 'result' and tracker:
                                    text = event.get('text') or ''
                                    if text.lstrip().startswith(('<function=', '<tool_call>')):
                                        unexecuted_tools = True
                                        event['text'] = ('The model returned tool-call text instead of a completed tool action. '
                                                         'This response does not confirm any file changes. Check the tool results and retry.\n\n'
                                                         '```text\n' + text + '\n```')
                                    tokens = event.get('tokens') or {}
                                    tracker.observe(json.dumps({'choices': [{'message': {'content': text}}], 'usage': {
                                        'prompt_tokens': tokens.get('input', 0), 'completion_tokens': tokens.get('output', 0),
                                        'total_tokens': tokens.get('total', 0)}}).encode())
                        except (ValueError, AttributeError):
                            pass
                    if time.monotonic() - last_update > 0.3 or (kind == 'agent' and events and events[-1].get('type') in ('tool_result','result')):
                        with self.connect() as db:
                            db.execute('UPDATE project_jobs SET output=?,events=?,updated_at=? WHERE id=?',
                                       (output, json.dumps(events), int(time.time()), jid))
                        last_update = time.monotonic()
                exit_code = process.wait()
                if kind == 'agent' and exit_code == 0:
                    from agent_contracts import execution_issue
                    issue = execution_issue(events)
                    if issue:
                        exit_code = 1
                        output += '\nExecution verification: ' + issue
                if unexecuted_tools:
                    exit_code = 1
                timer.cancel()
                if expired.is_set():
                    exit_code = 124
                    output += '\nTask time limit reached. The project container was stopped.'
        except Exception as exc:
            output += '\n' + (str(exc) if isinstance(exc, ProjectError) else 'Task failed: ' + type(exc).__name__)
        finally:
            if timer:
                timer.cancel()
            if kind == 'agent':
                try:
                    from project_memory import record
                    record(project['root'], jid, exit_code, events)
                    # Git runs inside the sandbox. Disable repository hooks and signing.
                    script = "git -c core.hooksPath=/dev/null add -A -- . ':!.env' ':!.env.*' ':!*.pem' ':!*.key' && if ! git diff --cached --quiet; then git -c core.hooksPath=/dev/null -c commit.gpgsign=false -c user.name=OpenHermes -c user.email=projects@localhost commit -m " + shlex.quote(('Task checkpoint: ' if exit_code == 0 else 'Partial task checkpoint: ') + jid) + '; fi'
                    snapshot = self.run(['docker', 'exec', '--workdir', '/workspace', 'openhermes-project-' + project['id'], 'bash', '-lc', script], timeout=30)
                    if snapshot.returncode:
                        output += '\nLocal Git checkpoint failed; files remain on disk.'
                    else:
                        output += '\nLocal Git checkpoint recorded (or no staged changes).'
                except Exception:
                    output += '\nProject checkpoint could not be fully recorded; inspect files and Git status.'
            if tracker:
                tracker.finish('completed' if exit_code == 0 else 'failed_or_interrupted')
            with self.connect() as db:
                db.execute('UPDATE project_jobs SET state=?,output=?,events=?,exit_code=?,updated_at=? WHERE id=?',
                           ('completed' if exit_code == 0 else 'failed', output, json.dumps(events), exit_code, int(time.time()), jid))

    def dispatch(self, user, payload):
        profile = self.ensure_user(user)
        action = payload.get('action')
        if action == 'list':
            with self.connect() as db:
                db.row_factory = sqlite3.Row
                projects = [dict(r) for r in db.execute('SELECT * FROM projects WHERE user_id=? AND id NOT IN (SELECT project_id FROM deleted_projects) ORDER BY created_at DESC', (user['id'],))]
            return {'user': {'name': user.get('name'), 'id': user['id']}, 'projects': projects}
        if action == 'create':
            return {'project': self.create(user, profile, payload.get('name'), payload.get('repository'))}
        project = self.project(user['id'], payload.get('project_id'))
        root = project['root']
        if action == 'export':
            with self.connect() as db:
                if db.execute("SELECT 1 FROM project_jobs WHERE project_id=? AND state IN ('queued','running')",(project['id'],)).fetchone():
                    raise ProjectError('Wait for the current task to finish before downloading',409)
            from project_export import export_project
            return export_project(project)
        if action == 'delete_project':
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute("SELECT 1 FROM project_jobs WHERE project_id=? AND state IN ('queued','running')", (project['id'],)).fetchone():
                    raise ProjectError('Stop the running task before deleting this project', 409)
                self.cleanup_project(project)
                db.execute('DELETE FROM project_jobs WHERE project_id=?', (project['id'],))
                db.execute('DELETE FROM project_chats WHERE project_id=?', (project['id'],))
                db.execute('INSERT INTO deleted_projects VALUES (?,?)', (project['id'], int(time.time())))
            return {'deleted': True, 'local_resources_removed': True, 'remote_git_untouched': True}
        if action == 'chats':
            with self.connect() as db:
                db.row_factory = sqlite3.Row
                db.execute('INSERT OR IGNORE INTO project_chats (id,project_id,title,created_at) VALUES (?,?,?,?)', ('legacy-' + project['id'], project['id'], 'Original chat', int(time.time())))
                rows = [dict(r) for r in db.execute('SELECT c.*,COALESCE((SELECT MAX(j.created_at) FROM project_jobs j WHERE j.chat_id=c.id),c.created_at) AS updated_at FROM project_chats c WHERE project_id=? AND c.deleted_at IS NULL ORDER BY updated_at DESC,c.rowid DESC', (project['id'],))]
            return {'chats': rows}
        if action in ('remove_chat', 'restore_chat'):
            cid = payload.get('chat_id')
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if not db.execute('SELECT 1 FROM project_chats WHERE id=? AND project_id=?', (cid, project['id'])).fetchone():
                    raise ProjectError('Chat not found', 404)
                if action == 'remove_chat' and db.execute("SELECT 1 FROM project_jobs WHERE chat_id=? AND state IN ('queued','running')", (cid,)).fetchone():
                    raise ProjectError('Stop the running task before removing this conversation', 409)
                db.execute('UPDATE project_chats SET deleted_at=? WHERE id=? AND project_id=?', (int(time.time()) if action == 'remove_chat' else None, cid, project['id']))
            return {'ok': True}
        if action == 'rename_chat':
            title = payload.get('title')
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100 or any(ord(c) < 32 for c in title):
                raise ProjectError('Conversation title must have 1â€“100 characters')
            with self.connect() as db:
                changed = db.execute('UPDATE project_chats SET title=? WHERE id=? AND project_id=? AND deleted_at IS NULL', (title.strip(), payload.get('chat_id'), project['id']))
                if not changed.rowcount:
                    raise ProjectError('Chat not found', 404)
            return {'ok': True}
        if action == 'new_chat':
            cid = uuid.uuid4().hex
            with self.connect() as db:
                db.execute('INSERT INTO project_chats (id,project_id,title,created_at) VALUES (?,?,?,?)', (cid, project['id'], 'New chat', int(time.time())))
            return {'chat': {'id': cid, 'title': 'New chat', 'created_at': int(time.time())}}
        if action == 'rename':
            name = payload.get('name')
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or any(ord(c) < 32 for c in name):
                raise ProjectError('Project name must have 1â€“60 characters')
            result = self.run([HERMES, '-p', profile, 'project', 'rename', project['slug'], name.strip()], timeout=90)
            if result.returncode:
                raise ProjectError('Hermes could not rename the project', 503)
            with self.connect() as db:
                db.execute('UPDATE projects SET name=? WHERE id=?', (name.strip(), project['id']))
            return {'project': self.project(user['id'], project['id'])}
        if action == 'tree':
            return {'entries': tree(root, payload.get('path', ''))}
        if action == 'read':
            data = read_bytes(root, payload.get('path'))
            try:
                content = data.decode('utf-8')
            except UnicodeDecodeError:
                raise ProjectError('This file is binary; use the preview instead')
            return {'content': content, 'sha': hashlib.sha256(data).hexdigest()}
        if action == 'save':
            return {'sha': save_file(root, payload.get('path'), payload.get('content'), payload.get('sha'))}
        if action == 'mkdir':
            fd, leaf = open_parent(root, payload.get('path'))
            try:
                os.mkdir(leaf, 0o755, dir_fd=fd)
            finally:
                os.close(fd)
            return {'ok': True}
        if action == 'run':
            return self.job(project, payload.get('kind'), payload.get('prompt'), payload.get('chat_id'))
        if action == 'jobs':
            with self.connect() as db:
                db.row_factory = sqlite3.Row
                cid = payload.get('chat_id') or 'legacy-' + project['id']
                if not db.execute('SELECT 1 FROM project_chats WHERE id=? AND project_id=? AND deleted_at IS NULL', (cid, project['id'])).fetchone():
                    raise ProjectError('Chat not found', 404)
                rows = [dict(r) for r in db.execute('SELECT * FROM project_jobs WHERE project_id=? AND chat_id=? ORDER BY created_at DESC,rowid DESC LIMIT 200', (project['id'], cid))]
            for row in rows:
                row['events'] = json.loads(row['events'])
            return {'jobs': rows}
        if action == 'stop':
            name = 'openhermes-project-' + project['id']
            inspected = self.run(['docker', 'inspect', name])
            if inspected.returncode == 0:
                info = json.loads(inspected.stdout)[0]
                if info['Config'].get('Labels', {}).get('org.openhermes.project') != project['id']:
                    raise ProjectError('Project container identity mismatch', 503)
                self.run(['docker', 'stop', '--time', '3', name], timeout=15)
            return {'ok': True}
        if action == 'changes':
            container = self.container(project)
            result = self.run(['docker', 'exec', container, 'git', '-C', '/workspace', '--no-pager', 'diff', '--no-ext-diff', '--no-textconv', '--', '.'])
            status = self.run(['docker', 'exec', container, 'git', '-C', '/workspace', 'status', '--short'])
            return {'diff': result.stdout[:MAX_FILE], 'status': status.stdout[:10000]}
        if action == 'preview':
            path = payload.get('path', 'index.html')
            data = read_bytes(root, path)
            return {'data': base64.b64encode(data).decode(), 'mime': mimetypes.guess_type(path)[0] or 'application/octet-stream'}
        raise ProjectError('Unknown project action')
