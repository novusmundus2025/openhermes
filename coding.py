"""Authenticated chat-to-project execution; never treats assistant prose as file evidence."""
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from contextlib import contextmanager
from projects import ProjectError, read_bytes


def manifest(root):
    files = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules','__pycache__','venv','dist','build') and not (Path(directory)/d).is_symlink()]
        for name in names:
            if name.startswith('.') or name in ('AGENTS.md',) or name.endswith(('.pem','.key','.sqlite3','.db')):
                continue
            path=(Path(directory)/name).relative_to(root).as_posix()
            try:
                data=read_bytes(root,path)
                data.decode('utf-8')
            except (OSError,ProjectError,UnicodeDecodeError):
                continue
            files[path]=hashlib.sha256(data).hexdigest()
            if len(files)>=1000:
                return files
    return files


def evidence(job):
    events=job.get('events') or []
    if isinstance(events,str):events=json.loads(events)
    from agent_contracts import execution_issue
    issue=execution_issue(events)
    good=job['state']=='completed' and job.get('exit_code')==0 and not issue
    final=next((e for e in reversed(events) if e.get('type')=='result'),{})
    if not good and 'Unknown execution blocks this session' in str(final.get('error','')):
        issue='The previous native task was interrupted and its execution state is unresolved. Inspect the project execution log. Use /new-session to continue with a fresh native conversation while preserving project files; no files are rolled back.'
    calls={e.get('tool_call_id'):e for e in events if e.get('type')=='tool_use'}
    commands=[]
    for event in events:
        if event.get('type')!='tool_result' or event.get('name')!='terminal':continue
        call=calls.get(event.get('tool_call_id'),{})
        command=(call.get('input') or {}).get('command')
        if not isinstance(command,str):continue
        try:
            result=event.get('output') or {}
            if isinstance(result,str):result=json.loads(result)
        except (ValueError,TypeError):continue
        if not isinstance(result,dict):continue
        commands.append({'command':command[:1000],'exit_code':result.get('exit_code'),
                         'output':str(result.get('output') or result.get('stdout') or '')[:5000],
                         'is_error':bool(event.get('is_error') or result.get('error'))})
    return {'verified':good,'answer':str(final.get('text') or '')[:16000] if good else '',
            'issue':issue or ('Task execution failed; completion is not verified.' if not good else None),
            'commands':commands[-5:]}


def live_events(job):
    events=job.get('events') or []
    if isinstance(events,str):events=json.loads(events)
    result=[]
    for event in events[-300:]:
        kind=event.get('type')
        if kind not in ('text','tool_use','tool_result'):continue
        item={k:event[k] for k in ('type','name','tool_call_id','timestamp','is_error') if k in event}
        if kind=='text':item['text']=str(event.get('text') or '')[:16000]
        elif kind=='tool_use':
            args=event.get('input') or {}
            item['input']={k:str(args[k])[:4000] for k in ('command','path','file_path') if k in args}
        else:item['output']=str(event.get('output') or '')[:6000]
        result.append(item)
    return result


def project_matches(prompt, projects):
    # Require distinctive project words; generic stack/task words cannot select a workspace.
    ignored=set('supercoder create build application app project api node js react python code program working want this that with from here please add fix update the and for'.split())
    words=set(re.findall(r'[a-z0-9]+',prompt.lower()))
    matches=[]
    for project in projects:
        tokens=set(re.findall(r'[a-z0-9]+',project.get('name','').lower()))-ignored
        if tokens and tokens <= words:matches.append(project)
    return matches


class CodingService:
    def __init__(self, projects):
        self.projects=projects
        self.lock=threading.RLock()
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS coding_pending(uid TEXT, conversation TEXT, prompt TEXT, PRIMARY KEY(uid,conversation))')
            db.execute('CREATE TABLE IF NOT EXISTS coding_conversations(uid TEXT, conversation TEXT, project_id TEXT, chat_id TEXT, PRIMARY KEY(uid,conversation))')
            db.execute('CREATE TABLE IF NOT EXISTS coding_requests(uid TEXT, request_id TEXT, project_id TEXT, job_id TEXT, chat_id TEXT, before_files TEXT, PRIMARY KEY(uid,request_id))')
            if 'result_json' not in [r[1] for r in db.execute('PRAGMA table_info(coding_requests)')]:
                db.execute('ALTER TABLE coding_requests ADD COLUMN result_json TEXT')

    @contextmanager
    def db(self):
        db=self.projects.connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def dispatch(self,user,body):
        uid=user['id']
        self.projects.ensure_user(user)
        action=body.get('action')
        if action=='latest':
            with self.db() as db:
                row=db.execute('SELECT r.job_id FROM coding_requests r JOIN coding_conversations c ON c.uid=r.uid AND c.project_id=r.project_id AND c.chat_id=r.chat_id WHERE c.uid=? AND c.conversation=? ORDER BY r.rowid DESC LIMIT 1',(uid,body.get('conversation_id'))).fetchone()
            if not row:raise ProjectError('No coding task was found for this conversation',404)
            return self.dispatch(user,{'action':'status','job_id':row[0]})
        if action in ('workspace','reset_session'):
            with self.db() as db:
                row=db.execute('SELECT project_id,chat_id FROM coding_conversations WHERE uid=? AND conversation=?',(uid,body.get('conversation_id'))).fetchone()
            if not row:raise ProjectError('This chat has no coding project yet. Send a coding request first.',404)
            project=self.projects.project(uid,row[0])
            if action=='reset_session':
                jobs=self.projects.dispatch(user,{'action':'jobs','project_id':row[0],'chat_id':row[1]})['jobs']
                if any(j['state'] in ('queued','running') for j in jobs):
                    raise ProjectError('Stop or wait for the current task before starting a fresh session',409)
                chat=self.projects.dispatch(user,{'action':'new_chat','project_id':row[0]})['chat']
                with self.db() as db:
                    db.execute('UPDATE coding_conversations SET chat_id=? WHERE uid=? AND conversation=?',(chat['id'],uid,body.get('conversation_id')))
            return {'state':'workspace','project_id':row[0],'workspace':project['root'],
                    'answer':'Started a fresh native conversation. Your saved project files and earlier execution logs are preserved.' if action=='reset_session' else 'Your saved coding workspace:',
                    'files':[{'path':p,'sha':sha,'changed':False} for p,sha in manifest(project['root']).items()]}
        if action=='start':
            cid,request_id,prompt=body.get('conversation_id'),body.get('request_id'),body.get('prompt')
            if not isinstance(cid,str) or not 1<=len(cid)<=160 or any(ord(c)<32 for c in cid):
                raise ProjectError('A saved Open WebUI conversation is required')
            if not isinstance(request_id,str) or not re.fullmatch(r'[a-f0-9]{32}',request_id):
                raise ProjectError('Invalid coding request identifier')
            if not isinstance(prompt,str) or not 1<=len(prompt.strip())<=24000:
                raise ProjectError('Coding request must contain 1–24000 characters')
            with self.lock:
                with self.db() as db:
                    existing=db.execute('SELECT project_id,job_id,chat_id FROM coding_requests WHERE uid=? AND request_id=?',(uid,request_id)).fetchone()
                    mapping=db.execute('SELECT project_id,chat_id FROM coding_conversations WHERE uid=? AND conversation=?',(uid,cid)).fetchone()
                if existing:
                    existing_project=self.projects.project(uid,existing[0])
                    return dict(zip(('project_id','job_id','chat_id'),existing),project_name=existing_project.get('name','Project'))
                projects=self.projects.dispatch(user,{'action':'list'})['projects']
                use=re.fullmatch(r'/use-project\s+(.+)',prompt.strip(),re.I)
                new=re.match(r'^(?:/new-project\s+|(?:create|start|build)\s+(?:a\s+)?(?:new|different|separate)\s+project\b[: ]*)(.*)',prompt.strip(),re.I|re.S)
                with self.db() as db:
                    pending=db.execute('SELECT prompt FROM coding_pending WHERE uid=? AND conversation=?',(uid,cid)).fetchone()
                selected=None
                if use:
                    choices=[p for p in projects if p['id']==use[1] or p.get('name','').casefold()==use[1].casefold()]
                    if len(choices)!=1:raise ProjectError('Project name is missing or ambiguous. Use the project identifier shown in /projects.')
                    selected=self.projects.project(uid,choices[0]['id'])
                    if not pending:raise ProjectError('Send the coding task first, then select its project.')
                    prompt=pending[0]
                elif not new:
                    matches=project_matches(prompt,projects)
                    if len(matches)==1:selected=matches[0]
                    elif mapping and not matches and not re.search(r'\b(?:create|build|start)\b.*\b(?:new|different|separate|app|application|system|website)\b',prompt,re.I):
                        selected=self.projects.project(uid,mapping[0])
                if not selected and not new and projects:
                    with self.db() as db:
                        db.execute('INSERT OR REPLACE INTO coding_pending VALUES(?,?,?)',(uid,cid,prompt))
                    return {'state':'choose_project','projects':[{'id':p['id'],'name':p['name']} for p in projects],
                            'answer':'Which project is this task for? No files have been edited.'}
                if new or (not selected and not projects):
                    name=(new[1].strip() if new else prompt.strip().splitlines()[0])
                    if not name:raise ProjectError('Give the new project a name after /new-project.')
                    selected=self.projects.dispatch(user,{'action':'create','name':re.sub(r'[^\w -]','',name)[:60]})['project']
                    if pending and new:prompt=pending[0]
                project=selected
                if mapping and mapping[0]==project['id']:chat_id=mapping[1]
                else:
                    chat_id=self.projects.dispatch(user,{'action':'new_chat','project_id':project['id']})['chat']['id']
                with self.db() as db:
                    db.execute('INSERT OR REPLACE INTO coding_conversations VALUES(?,?,?,?)',(uid,cid,project['id'],chat_id))
                    db.execute('DELETE FROM coding_pending WHERE uid=? AND conversation=?',(uid,cid))
                before=manifest(project['root'])
                context=body.get('context','')
                if not isinstance(context,str):raise ProjectError('Invalid conversation context')
                context=context[:max(0,min(6000,24000-len(prompt)-200))]
                result=self.projects.dispatch(user,{'action':'run','kind':'agent','project_id':project['id'],'chat_id':chat_id,
                    'prompt':prompt+'\n\nCompletion requirements: Save requested code as files under /workspace, run relevant checks using terminal tools, and report exact relative file paths and actual command results. If a tool fails, repair or report the failure; do not claim success from prose alone.'+ ('\n\nHistorical conversation context (unverified; inspect disk rather than trusting earlier success claims):\n'+context if context else '')})
                with self.db() as db:
                    db.execute('INSERT INTO coding_requests(uid,request_id,project_id,job_id,chat_id,before_files) VALUES(?,?,?,?,?,?)',(uid,request_id,project['id'],result['job_id'],chat_id,json.dumps(before)))
                return {'project_id':project['id'],'project_name':project.get('name','Project'),'job_id':result['job_id'],'chat_id':chat_id}
        if action=='status':
            jid=body.get('job_id')
            with self.db() as db:
                row=db.execute('SELECT project_id,chat_id,before_files,result_json FROM coding_requests WHERE uid=? AND job_id=?',(uid,jid)).fetchone()
                if not row:raise ProjectError('Coding task not found',404)
            project=self.projects.project(uid,row[0])
            if row[3]:return json.loads(row[3])
            jobs=self.projects.dispatch(user,{'action':'jobs','project_id':row[0],'chat_id':row[1]})['jobs']
            job=next((j for j in jobs if j['id']==jid),None)
            if not job:raise ProjectError('Coding task not found',404)
            result={'project_id':row[0],'job_id':jid,'state':job['state'],'workspace':project['root'],'events':live_events(job)}
            if job['state'] in ('queued','running'):
                return result
            before=json.loads(row[2]);after=manifest(project['root'])
            result.update(evidence(job))
            result['files']=[{'path':p,'sha':sha,'changed':before.get(p)!=sha} for p,sha in after.items()]
            prompt=job.get('prompt','').split('\n\nCompletion requirements:')[0]
            if result['verified'] and re.search(r'\b(create|write|implement|build|add|modify|fix|change|update|generate)\b',prompt,re.I):
                if not any(f['changed'] for f in result['files']):
                    result.update(verified=False,answer='',issue='No created or modified program files were observed. Completion is not verified.')
            with self.db() as db:
                db.execute('UPDATE coding_requests SET result_json=? WHERE uid=? AND job_id=? AND result_json IS NULL',(json.dumps(result),uid,jid))
            return result
        raise ProjectError('Unsupported coding action')
