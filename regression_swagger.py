"""Opt-in live Hermes regression. Run on GX10: python3 regression_swagger.py --live.
Creates an isolated fixture/container, retains evidence, and exits nonzero on failure.
Never edits a user's project. Uses the installed model and private server settings.
"""
import argparse
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid
from agent_contracts import evaluate_contract, swagger_contract
from projects import ProjectService, PYTHON, SOURCE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='Run real inference and container tools')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required; this evaluation uses real model inference')
    root = Path(__file__).resolve().parent
    settings = json.loads((root / 'settings.json').read_text())
    import sqlite3
    with sqlite3.connect(root / 'state/users.sqlite3') as db:
        row = db.execute("SELECT profile FROM users WHERE status='ready' ORDER BY created_at LIMIT 1").fetchone()
    if not row:
        raise RuntimeError('A provisioned Hermes profile is required')
    rid = 'regression-' + uuid.uuid4().hex
    run = root / 'state/regressions' / rid
    work = run / 'project'
    work.mkdir(parents=True)
    docs = b'# Existing API manual\nKeep this manual intact when adding interactive Swagger docs.\n'
    (work / 'API.md').write_bytes(docs)
    (work / 'package.json').write_text(json.dumps({'name':'swagger-regression','version':'1.0.0','scripts':{'start':'node app.js'}}))
    (work / 'app.js').write_text("const http=require('http'); http.createServer((req,res)=>{if(req.url==='/students'){res.setHeader('Content-Type','application/json');res.end('[]');}else{res.statusCode=404;res.end('Not found');}}).listen(process.env.PORT||3000,'0.0.0.0');\n")
    service = ProjectService.__new__(ProjectService)
    service.container_lock = threading.Lock()
    service.state, service.settings = run, settings
    project = {'id':rid,'profile':row[0],'root':str(work)}
    name = service.container(project)
    events, error = [], ''
    try:
        prompt = ('Add Swagger/OpenAPI documentation to this existing Node.js HTTP API. '
                  'Serve interactive Swagger UI at /docs and an OpenAPI 3 JSON spec at /openapi.json. '
                  'Document GET/POST /students and GET/PUT/DELETE /students/{id}. '
                  'Preserve API.md unchanged. Read existing files with read_file before edits. '
                  'Actually save and run the implementation; verify the two HTTP endpoints. '
                  'If a mutation is blocked, reread and recover; report unresolved failures honestly. '
                  'Only work in /workspace. Keep npm start working and honor PORT.')
        command = ['docker','exec','-i','--workdir','/workspace',name,'timeout','--kill-after=5','300',PYTHON,'-I','-c',
                   "import sys,runpy;sys.path.insert(0,'"+SOURCE+"');import hermes_bootstrap;runpy.run_module('hermes_cli.main',run_name='__main__')",
                   'chat','--cli','--query-file','-','--oneshot','--format','stream-json','--in','/workspace','--continue',rid,'--create-if-missing','--max-turns','16','-t','terminal,file']
        try:
            result = subprocess.run(command,input=prompt,text=True,capture_output=True,timeout=320)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or b''
            stderr = exc.stderr or b''
            result = subprocess.CompletedProcess(command,124,stdout.decode() if isinstance(stdout,bytes) else stdout,stderr.decode() if isinstance(stderr,bytes) else stderr)
        (run / 'agent.jsonl').write_text(result.stdout)
        (run / 'stderr.log').write_text(result.stderr)
        for line in result.stdout.splitlines():
            try: events.append(json.loads(line))
            except ValueError: pass
        if result.returncode:
            error = 'Agent process exited ' + str(result.returncode)
        probe = r'''const {spawn}=require('child_process');
const child=spawn('npm',['start'],{cwd:'/workspace',env:{...process.env,PORT:'39129'},stdio:['ignore','pipe','pipe']});
const responses={};setTimeout(async()=>{try{for(const p of ['/docs','/openapi.json']){try{const r=await fetch('http://127.0.0.1:39129'+p);responses[p]={status:r.status,body:await r.text()};}catch(e){responses[p]={status:0,body:e.message};}}console.log(JSON.stringify(responses));}finally{child.kill();setTimeout(()=>process.exit(),100);}},1800);'''
        try:
            checked = service.run(['docker','exec','-i',name,'node','-e',probe],timeout=15)
            responses = json.loads(checked.stdout)
        except (ValueError, subprocess.TimeoutExpired):
            responses = {}
        report = evaluate_contract(swagger_contract(docs),{'API.md':(work/'API.md').read_bytes()} if (work/'API.md').exists() else {},responses,events)
        if error: report['passed']=False;report['failures'].append(error)
        report['run_directory']=str(run)
        (run/'responses.json').write_text(json.dumps(responses,indent=2))
        (run/'report.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report,indent=2),flush=True)
        return 0 if report['passed'] else 1
    finally:
        # Stop only this uniquely named test container. Keep artifacts for diagnosis.
        service.run(['docker','stop','--time','3',name],timeout=15)


if __name__ == '__main__':
    raise SystemExit(main())
