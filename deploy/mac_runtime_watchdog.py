"""Recover an unhealthy local MLX worker through its existing GUI LaunchAgent."""
import json
import os
from pathlib import Path
import re
import signal
import shlex
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen, Request


class RecoveryGate:
    def __init__(self):
        self.failures = 0
        self.last_recovery = None
        self.started_at = None
        self.seen_healthy = False

    def observe(self, state, now, paused=False):
        if self.started_at is None:
            self.started_at = now
        if state == 'healthy':
            self.seen_healthy = True
        if paused or state == 'healthy':
            self.failures = 0
            return False
        if state == 'unavailable' and not self.seen_healthy and now-self.started_at < 300:
            return False
        self.failures += 1
        threshold = 3 if state in ('dead_worker', 'stalled_worker') else 6
        if self.failures < threshold or (self.last_recovery is not None and now-self.last_recovery < 300):
            return False
        self.failures = 0
        self.last_recovery = now
        return True


def health():
    try:
        with urlopen('http://127.0.0.1:8790/v1/models', timeout=3) as response:
            return 'healthy' if response.status == 200 else 'unavailable'
    except HTTPError as exc:
        body = exc.read(1024)
        if exc.code == 503:
            if b'MLX generation worker unavailable' in body:return 'dead_worker'
            if b'MLX generation worker stalled' in body:return 'stalled_worker'
        return 'unavailable'
    except (URLError, OSError):
        return 'unavailable'


def recover(home, service):
    metadata = home/'persistent-runtime.json'
    if metadata.exists():
        pid = json.loads(metadata.read_text()).get('pid')
        if type(pid) is not int or pid <= 1:
            raise ValueError('Invalid runtime PID; refusing recovery')
        process = subprocess.run(['ps','-p',str(pid),'-o','uid=,command='], capture_output=True, text=True)
        if process.returncode == 0:
            uid, command = process.stdout.strip().split(None, 1)
            expected = str(home/'runtimes/mlx/measured_startup.py')
            if int(uid) != os.getuid() or expected not in command or '--port 8790' not in command:
                raise ValueError('Runtime identity mismatch; refusing recovery')
            os.kill(pid, signal.SIGTERM)
    subprocess.run(['launchctl','kickstart','-k',service], check=True, capture_output=True, timeout=15)


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix+'.watchdog-tmp')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)


def generation_probe():
    with urlopen('http://127.0.0.1:8790/v1/models', timeout=3) as response:
        model = json.load(response)['data'][0]['id']
    request = Request('http://127.0.0.1:8790/v1/chat/completions',
        data=json.dumps({'model':model,'messages':[{'role':'user','content':'Reply OK.'}],
                         'max_tokens':8,'stream':False}).encode(),
        headers={'Content-Type':'application/json'})
    with urlopen(request, timeout=60) as response:
        result=json.load(response)
    return bool(result.get('choices', [{}])[0].get('message', {}).get('content', '').strip())


def recover_paused(home, service):
    config_path = home/'config.json'
    marker = home/'runtime-recovery.json'
    config = json.loads(config_path.read_text())
    if config.get('paused') or not config.get('connected'):
        return False
    metadata = json.loads((home/'persistent-runtime.json').read_text())
    pid = metadata.get('pid')
    if type(pid) is not int or pid <= 1:
        raise ValueError('Invalid runtime PID')
    process = subprocess.run(['ps','-p',str(pid),'-o','uid=,command='], capture_output=True,text=True)
    uid, command = process.stdout.strip().split(None,1)
    argv = shlex.split(command)
    expected = str(home/'runtimes/mlx/measured_startup.py')
    if int(uid)!=os.getuid() or len(argv)<2 or argv[1]!=expected or '--port 8790' not in command:
        raise ValueError('Runtime identity mismatch')
    config['paused']=True
    atomic_json(config_path,config)
    owned_config=config_path.read_bytes()
    atomic_json(marker,{'state':'paused_for_recovery','time':int(time.time())})
    # Give the running agent time to observe the pause before terminating generation.
    time.sleep(10)
    try:
        os.kill(pid,signal.SIGTERM)
    except ProcessLookupError:
        pass  # The paused agent may already have released its runtime.
    for _ in range(30):
        check=subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True,text=True)
        if check.returncode!=0 or check.stdout.strip().startswith('Z'):
            break
        time.sleep(1)
    else:
        raise RuntimeError('Worker did not stop; contributor remains paused')
    time.sleep(5)
    with (home/'runtimes/mlx/server.log').open('ab') as log:
        worker=subprocess.Popen(argv,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    metadata['pid']=worker.pid
    atomic_json(home/'persistent-runtime.json',metadata)
    deadline=time.monotonic()+300
    while time.monotonic()<deadline:
        if config_path.read_bytes()!=owned_config:
            atomic_json(marker,{'state':'manual_change_preserved','time':int(time.time())})
            return False
        if worker.poll() is not None:
            raise RuntimeError('Replacement worker exited; contributor remains paused')
        try:
            if health()=='healthy' and generation_probe():
                # Never undo a manual configuration change made during recovery.
                if config_path.read_bytes()!=owned_config:
                    return False
                config['paused']=False
                atomic_json(config_path,config)
                resumed_config=config_path.read_bytes()
                try:
                    subprocess.run(['launchctl','kickstart','-k',service],check=True,
                                   capture_output=True,timeout=15)
                except Exception:
                    if config_path.read_bytes()==resumed_config:
                        config['paused']=True
                        atomic_json(config_path,config)
                    raise
                atomic_json(marker,{'state':'generation_verified','time':int(time.time())})
                return True
        except (HTTPError,URLError,OSError,ValueError,KeyError,IndexError):
            pass
        time.sleep(10)
    raise RuntimeError('Generation verification timed out; contributor remains paused')


def main():
    home = Path.home()/'.opengpu'
    service = 'gui/'+str(os.getuid())+'/ai.mundusx.node-agent'
    gate = RecoveryGate()
    while True:
        try:
            config = json.loads((home/'config.json').read_text())
            paused = config.get('paused', False) or not config.get('connected', False)
            state = health()
            if gate.observe(state, time.monotonic(), paused):
                recover_paused(home, service)
                print(json.dumps({'event':'runtime_recovery','reason':state,'time':int(time.time())}), flush=True)
            # Keep the existing CLI's process record in sync with launchd respawns.
            info = subprocess.run(['launchctl','print',service],capture_output=True,text=True,timeout=5)
            match = re.search(r'\bpid = (\d+)',info.stdout)
            if match:
                (home/'node-agent.pid').write_text(match[1]+'\n')
        except Exception as exc:
            print(json.dumps({'event':'watchdog_error','type':type(exc).__name__,'time':int(time.time())}),flush=True)
        time.sleep(10)


if __name__ == '__main__':
    main()
