"""Backend-neutral, opt-in contributor watchdog. No shell commands or guessed targets."""
import argparse
import json
from pathlib import Path
import subprocess
import time
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
from deploy.mac_runtime_watchdog import RecoveryGate, atomic_json


class Runtime:
    def __init__(self, config):
        self.config = config
        self.backend = config['backend']
        if self.backend not in ('vllm', 'llamacpp', 'ollama'):
            raise ValueError('Unsupported backend')
        self.url = config['url'].rstrip('/')
        parsed = urlsplit(self.url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.username or parsed.password:
            raise ValueError('Runtime must use a loopback HTTP endpoint')
        self.model = config['model']
        if not isinstance(self.model, str) or not self.model:
            raise ValueError('An explicit model is required')
        self.target = config['target']
        self.agent_target = config['agent_target']
        if self.agent_target.get('manager') not in ('systemd-user', 'systemd', 'docker'):
            raise ValueError('Unsupported contributor lifecycle manager')
        if self.target['manager'] not in ('systemd-user', 'systemd', 'docker'):
            raise ValueError('Unsupported lifecycle manager')
        name = self.target['name']
        if not isinstance(name, str) or not name or name.startswith('-') or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.@-' for c in name):
            raise ValueError('Invalid lifecycle target')
        self.timeout = max(5, min(180, int(config.get('probe_timeout', 45))))

    def request(self, path, payload=None):
        headers = {'Content-Type': 'application/json'}
        # Read a local key file without putting credentials in process arguments or logs.
        if self.config.get('api_key_file'):
            headers['Authorization'] = 'Bearer '+Path(self.config['api_key_file']).read_text().strip()
        request = Request(self.url+path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        with urlopen(request, timeout=self.timeout) as response:
            body = response.read(1024*1024)
        return json.loads(body) if body.strip() else {}

    def probe(self):
        if self.backend == 'ollama':
            models = self.request('/api/tags').get('models', [])
            if not any(m.get('name') == self.model or m.get('model') == self.model for m in models):
                return False
            answer = self.request('/api/generate', {'model': self.model, 'prompt': 'Reply OK.', 'stream': False,
                                                   'options': {'num_predict': 8, 'temperature': 0}})
            return bool(answer.get('done') and answer.get('response', '').strip())
        self.request('/health')
        answer = self.request('/v1/chat/completions', {'model': self.model,
            'messages': [{'role': 'user', 'content': 'Reply OK.'}], 'max_tokens': 8, 'stream': False, 'temperature': 0})
        return bool(answer.get('choices', [{}])[0].get('message', {}).get('content', '').strip())

    def lifecycle(self, action, target=None):
        target = target or self.target
        manager, name = target['manager'], target['name']
        if manager not in ('systemd-user', 'systemd', 'docker') or not name or name.startswith('-') or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.@-' for c in name):
            raise ValueError('Invalid lifecycle target')
        if manager == 'docker':
            # Pin container identity across recovery; names can otherwise be reused.
            identity = subprocess.check_output(['docker', 'inspect', '--format', '{{.Id}}', name], text=True, timeout=10).strip()
            expected = target.get('container_id')
            if not expected or identity != expected:
                raise ValueError('Docker container identity mismatch')
            command = ['docker', action, '--', identity]
        else:
            command = ['systemctl']+(['--user'] if manager == 'systemd-user' else [])+[action, '--', name]
        subprocess.run(command, check=True, capture_output=True, timeout=60)


def recover(runtime, config_path, state_path, sleep=time.sleep, clock=time.monotonic):
    config_path, state_path = Path(config_path), Path(state_path)
    config = json.loads(config_path.read_text())
    if config.get('paused') or not config.get('connected'):
        return 'manual_pause'
    config['paused'] = True
    atomic_json(config_path, config)
    owned = config_path.read_bytes()
    atomic_json(state_path, {'state': 'recovering', 'backend': runtime.backend, 'time': int(time.time())})
    try:
        sleep(10)  # Agent observes admission pause; active requests can finish/fail.
        runtime.lifecycle('stop')
        sleep(5)
        if config_path.read_bytes() != owned:
            return 'manual_change'
        runtime.lifecycle('start')
        deadline = clock()+int(runtime.config.get('startup_timeout', 300))
        while clock() < deadline:
            if config_path.read_bytes() != owned:
                return 'manual_change'
            try:
                good = runtime.probe()
            except Exception:
                good = False
            if good:
                if config_path.read_bytes() != owned:
                    return 'manual_change'
                config['paused'] = False
                atomic_json(config_path, config)
                resumed = config_path.read_bytes()
                try:
                    runtime.lifecycle('restart', runtime.agent_target)
                except Exception:
                    if config_path.read_bytes() == resumed:
                        config['paused'] = True
                        atomic_json(config_path, config)
                    raise
                atomic_json(state_path, {'state': 'generation_verified', 'backend': runtime.backend, 'time': int(time.time())})
                return 'resumed'
            sleep(10)
        raise RuntimeError('Generation verification deadline exceeded')
    except Exception:
        # Recovery failures never undo the admission pause.
        atomic_json(state_path, {'state': 'failed_paused', 'backend': runtime.backend, 'time': int(time.time())})
        raise


def main():
    parser=argparse.ArgumentParser();parser.add_argument('config',type=Path);parser.add_argument('--check',action='store_true');args=parser.parse_args()
    config=json.loads(args.config.read_text());runtime=Runtime(config)
    if args.check:
        print(json.dumps({'backend':runtime.backend,'generation_verified':runtime.probe()}));return
    if not config.get('enabled', False):raise SystemExit('Watchdog disabled in configuration')
    admission=Path(config['contributor_config']);state=args.config.with_suffix('.state.json')
    if not admission.is_file():raise SystemExit('Contributor admission configuration not found')
    # One recovery controller per contributor, even with different runtime configs.
    import fcntl
    lock=admission.with_suffix('.watchdog.lock').open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('Another watchdog owns this contributor')
    gate=RecoveryGate()
    while True:
        try:
            current=json.loads(admission.read_text())
            paused=current.get('paused',False) or not current.get('connected',False)
            if paused:
                gate.observe('healthy',time.monotonic(),paused=True)
            else:
                try:good=runtime.probe()
                except Exception:good=False
                # Probes test actual generation, including engines whose health endpoint stays 200.
                if gate.observe('healthy' if good else 'stalled_worker',time.monotonic()):
                    result=recover(runtime,admission,state)
                    print(json.dumps({'event':'runtime_recovery','backend':runtime.backend,'result':result}),flush=True)
        except Exception as exc:
            print(json.dumps({'event':'watchdog_error','type':type(exc).__name__}),flush=True)
        time.sleep(max(10,int(config.get('interval',60))))


if __name__=='__main__':main()
