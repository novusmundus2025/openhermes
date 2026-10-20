"""
title: Hermes Project Coder
description: Authenticated persistent coding projects, real Hermes tools and file evidence.
version: 1.0.0
"""
import asyncio
import json
from pathlib import Path
import time
import uuid
from urllib.parse import quote
import aiohttp


def conversation_id(body, metadata):
    return (metadata or {}).get('chat_id') or (body.get('metadata') or {}).get('chat_id') or body.get('chat_id')


def result_message(data):
    pid=quote(data['project_id'],safe='')
    project='/projects/#'+pid
    if data.get('state')=='workspace':
        text=data.get('answer') or 'Your saved coding workspace:'
    elif data.get('verified'):
        text=data.get('answer') or 'The project task completed.'
    else:
        text='**Not verified.** '+(data.get('issue') or 'The agent did not complete successfully. Inspect the project execution log before relying on its output.')
    text+='\n\n[Open project and execution log]('+project+')'
    files=data.get('files') or []
    changed=[f for f in files if f.get('changed')]
    shown=changed or files
    if shown:
        text+='\n\n'+('Files created or changed:' if changed else 'Files currently saved in this project:')+'\n\n'
        for file in shown[:30]:
            path=file['path']
            label=path.replace('\\','\\\\').replace('[','\\[').replace(']','\\]')
            text+='- ['+label+'](/project-file/?project_id='+pid+'&path='+quote(path,safe='')+')\n'
    else:
        text+='\n\nNo program files were found in the project.'
    text+='\n\nGX10 folder: `'+data.get('workspace','')+'`'
    commands=data.get('commands') or []
    if commands:
        text+='\n\nObserved terminal results:\n'
        for item in commands[-3:]:
            # Fence cannot be closed by tool output; code stays display-only.
            command=item['command'].replace('```','` ` `')
            output=item.get('output','').replace('```','` ` `')
            text+='\n```text\n$ '+command+'\nExit code: '+str(item.get('exit_code'))+'\n'+output+'\n```\n'
    elif data.get('state')!='workspace':
        text+='\n\nNo terminal verification result was recorded.'
    return text


class Pipe:
    def __init__(self):
        self.name='Hermes Project Coder'

    async def pipe(self,body:dict,__user__:dict,__metadata__:dict=None,__task__=None,__event_emitter__=None):
        if __task__ or (__metadata__ or {}).get('task') or (body.get('metadata') or {}).get('task') or body.get('task'):
            yield ''
            return
        if not __user__ or __user__.get('role') not in ('admin','user') or not __user__.get('id'):
            raise ValueError('An approved Open WebUI account is required')
        cid=conversation_id(body,__metadata__)
        if not cid:
            yield 'Open a saved chat before starting a coding task. No files were created.'
            return
        messages=body.get('messages') or []
        latest=next((m.get('content') for m in reversed(messages) if m.get('role')=='user'),None)
        if isinstance(latest,list):
            latest='\n'.join(p.get('text','') for p in latest if p.get('type')=='text')
        if not isinstance(latest,str) or not latest.strip():
            yield 'Enter a coding request. No files were created.'
            return
        config=json.loads(Path('/app/backend/data/openhermes-bridge.json').read_text())
        user={k:__user__.get(k,'') for k in ('id','name','email')}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
            async def api(payload):
                async with session.post(config['url']+'/coding',headers={'Authorization':'Bearer '+config['key']},json={'user':user,'body':payload}) as response:
                    data=await response.json()
                    if response.status!=200:
                        raise RuntimeError(data.get('error','Coding request failed'))
                    return data
            started=None
            try:
                if latest.strip().lower() in ('/files','/project','/workspace','/new-session','/status','/result'):
                    action='reset_session' if latest.strip().lower()=='/new-session' else 'latest' if latest.strip().lower() in ('/status','/result') else 'workspace'
                    data=await api({'action':action,'conversation_id':cid})
                    if data.get('state') in ('queued','running'):
                        yield 'Hermes is still '+data['state']+'. Completion is not yet verified. [Open project and execution log](/projects/#'+data['project_id']+').'
                    else:
                        yield result_message(data)
                    return
                history=[]
                for message in messages[-7:-1]:
                    if message.get('role') in ('user','assistant') and isinstance(message.get('content'),str):
                        history.append(message['role']+': '+message['content'][:1500])
                started=await api({'action':'start','conversation_id':cid,'request_id':uuid.uuid4().hex,'prompt':latest,'context':'\n'.join(history)[-6000:]})
                deadline=time.monotonic()+660
                while time.monotonic()<deadline:
                    data=await api({'action':'status','job_id':started['job_id']})
                    if data['state'] not in ('queued','running'):
                        yield result_message(data)
                        return
                    if __event_emitter__:
                        await __event_emitter__({'type':'status','data':{'description':'Hermes is working in your isolated project · '+data['state'],'done':False}})
                    await asyncio.sleep(2)
                yield '**Not verified.** The coding task has not returned a final result. [Inspect its project](/projects/#'+started['project_id']+').'
            except (RuntimeError,aiohttp.ClientError,asyncio.TimeoutError) as exc:
                yield '**Not verified.** '+(str(exc) if isinstance(exc,RuntimeError) else 'The coding connection was interrupted.')+('\n\n[Inspect the project](/projects/#'+started['project_id']+')' if started else ' No successful file creation is confirmed.')
            finally:
                if __event_emitter__:
                    await __event_emitter__({'type':'status','data':{'description':'Coding run finished or needs attention.','done':True}})
