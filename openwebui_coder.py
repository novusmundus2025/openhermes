"""
title: Hermes Project Coder
description: Authenticated persistent coding projects, real Hermes tools and file evidence.
version: 1.1.0
"""
import asyncio
import html
import re
import json
from pathlib import Path
import time
import uuid
from urllib.parse import quote
import aiohttp


def conversation_id(body, metadata):
    return (metadata or {}).get('chat_id') or (body.get('metadata') or {}).get('chat_id') or body.get('chat_id')


def display_text(value):
    return html.unescape(str(value or '')).replace('Â·','·')


def inline(value):
    return '`'+display_text(value).replace('`','\u2032').replace('\n',' ')+'`'


def result_message(data, show_workspace=False):
    pid=quote(data['project_id'],safe='')
    project='/projects/#'+pid
    files=data.get('files') or []
    changed=[f for f in files if f.get('changed')]
    workspace=data.get('state')=='workspace'
    verified=bool(data.get('verified'))
    if workspace:
        text='**Project workspace**\n\n'+display_text(data.get('answer') or 'Your saved files are available below.')
    elif verified:
        text='**✓ Completed**\n\n'+display_text(data.get('answer') or 'The task completed with recorded execution evidence.')
    else:
        text='**⚠ Changes saved · checks incomplete**' if changed else '**⚠ Needs attention**'
        text+='\n\n**Not verified.** '+display_text(data.get('issue') or 'Execution did not finish successfully.')
        if changed:text+=' Your saved changes are available below.'
        issue=str(data.get('issue') or '').lower()
        if 'session' in issue and ('interrupted' in issue or 'unresolved' in issue):
            text+='\n\nNext: inspect the log, then use `/new-session` to continue with the same files.'
        else:
            text+='\n\nNext: open the execution log to inspect the remaining checks.'
    text+='\n\n[Open project & execution log]('+project+')'
    shown=changed or files
    if shown:
        text+='\n\n**'+('Saved changes' if changed else 'Project files')+' · '+str(len(shown))+'**\n\n'
        for file in shown[:12]:
            path=file['path']
            label=path.replace('\\','\\\\').replace('[','\\[').replace(']','\\]')
            text+='- ['+label+'](/project-file/?project_id='+pid+'&path='+quote(path,safe='')+')\n'
        if len(shown)>12:text+='\n'+str(len(shown)-12)+' more files in the project explorer.\n'
    elif workspace:text+='\n\nThis project has no program files yet.'
    commands=data.get('commands') or []
    if commands:
        # Put full request/output history in the project log; keep the latest real result readable.
        latest=commands[-1]
        command=display_text(latest.get('command',''))
        check=bool(re.search(r'(?:^|&&|;)\s*(?:npm\s+(?:test\b|run\s+(?:test|lint|build|check)\b)|node\b|python[3]?\b|pytest\b|cargo\s+(?:test|check)\b|go\s+test\b|curl\b)',command))
        text+='\n\n**'+('Latest check' if check else 'Latest command')+'** · '+inline(command)+' · Exit code: '+str(latest.get('exit_code'))
        output=display_text(latest.get('output')).strip()
        if output and (check or latest.get('exit_code') not in (0,None) or latest.get('is_error')):
            lines=output.splitlines()
            preview='\n'.join(lines[:16])[:1800].replace('```','` ` `')
            text+='\n\n```text\n'+preview+'\n```'
            if len(lines)>16 or len(output)>1800:text+='\nFull output is available in the execution log.'
        elif output:
            text+='\n\nCommand output and diagnostics are available in the execution log.'
    elif not workspace:text+='\n\nNo terminal verification was recorded.'
    if show_workspace:
        text+='\n\n**GX10 workspace folder**\n\n```text\n'+str(data.get('workspace','')).replace('```','` ` `')+'\n```'
    else:text+='\n\nUse `/workspace` for the GX10 folder path.'
    return text


def event_updates(previous, current):
    # Match snapshot overlap, including when the server retains only its last 300 events.
    for count in range(min(len(previous),len(current)),0,-1):
        if previous[-count:]==current[:count]:return current[count:]
    return current


def render_event(event):
    if event.get('type')=='text':return str(event.get('text') or '')
    def safe(value):return str(value).replace('```','` ` `')
    name=safe(event.get('name') or 'tool')
    if event.get('type')=='tool_use':
        args=event.get('input') or {}
        detail=args.get('command') or args.get('path') or args.get('file_path') or ''
        return '\n\n**Request: '+name+'**\n```text\n'+safe(detail)+'\n```\n'
    output=event.get('output') or ''
    try:
        data=json.loads(output)
        failed=event.get('is_error') or data.get('error') or data.get('success') is False or data.get('verified') is False or data.get('exit_code',0) not in (None,0)
        detail=data.get('error') or data.get('output') or data.get('stdout') or data.get('content') or ('Exit code: '+str(data['exit_code']) if 'exit_code' in data else '')
    except (ValueError,TypeError,AttributeError):
        failed=event.get('is_error');detail=output
    label='Failed' if failed else 'Returned'
    return '\n**'+label+': '+name+'**\n'+('```text\n'+safe(detail)[:4000]+'\n```\n' if detail else '')


def tool_failed(event):
    if event.get('is_error'):return True
    try:
        data=json.loads(event.get('output') or '{}')
        return bool(data.get('error') or data.get('success') is False or data.get('verified') is False or data.get('exit_code',0) not in (None,0))
    except (ValueError,TypeError,AttributeError):return False


def tool_summary(event, call=None):
    name=event.get('name') or (call or {}).get('name') or 'tool'
    args=(call or {}).get('input') or {}
    target=args.get('path') or args.get('file_path') or args.get('command') or ''
    target=display_text(target).removeprefix('/workspace/').replace('\n',' ')
    if len(target)>90:target=target[:87]+'…'
    failed=tool_failed(event)
    actions={'read_file':('Read file','Read failed'),'write_file':('Saved file','Save blocked'),
             'patch':('Applied patch','Patch needs attention'),'terminal':('Ran command','Command failed'),
             'recovery_verify':('Verified edit','Verification needs attention')}
    label=actions.get(name,('Completed '+str(name).replace('_',' '),str(name).replace('_',' ')+' needs attention'))[bool(failed)]
    return ('⚠ ' if failed else '✓ ')+label+(' · '+target if target else '')


def execution_details(events):
    tools=[event for event in events if event.get('type') in ('tool_use','tool_result')]
    return '\n\n**Execution details**\n'+''.join(render_event(event) for event in tools[-12:]) if tools else ''


def git_intent(text):
    if text.strip().lower()=='/git':return {'operation':'clone'}
    request=re.sub(r'^(?:please\s+|can you\s+|could you\s+)', '', text.strip(), flags=re.I)
    match=re.match(r'(?:git\s+)?(clone|fetch|pull|push)\b',request,re.I)
    pr=re.match(r'(?:create|open)\s+(?:a\s+)?(?:pull request|merge request|pr|mr)\b',request,re.I)
    if not match and not pr:return None
    result={'operation':match[1].lower() if match else 'pr'}
    url=re.search(r'https://(?:github|gitlab)\.com/[A-Za-z0-9_./-]+',request)
    if url:result['repository']=url[0]
    ref=re.match(r'git\s+(?:push|pull|fetch)\s+origin\s+([A-Za-z0-9_./-]+)\s*$',request,re.I)
    if ref:result['branch']=ref[1]
    return result


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
            trace=[]
            final_status="Needs attention"
            try:
                if latest.strip().lower() in ('/files','/project','/workspace','/new-session','/status','/result'):
                    action='reset_session' if latest.strip().lower()=='/new-session' else 'latest' if latest.strip().lower() in ('/status','/result') else 'workspace'
                    data=await api({'action':action,'conversation_id':cid})
                    if data.get('state') in ('queued','running'):
                        yield 'Hermes is still '+data['state']+'. Completion is not yet verified. [Open project and execution log](/projects/#'+data['project_id']+').'
                    else:
                        final_status='Project files' if data.get('state')=='workspace' else 'Completed' if data.get('verified') else 'Checks incomplete'
                        yield result_message(data,show_workspace=latest.strip().lower()=='/workspace')
                    return
                # Route explicit remote Git requests to the authenticated operation form.
                # No token is requested in a prompt, sent to the model, or put in tool arguments.
                intent=git_intent(latest)
                if intent:
                    from urllib.parse import urlencode
                    params=dict(intent,conversation_id=cid)
                    final_status='Git connection / operation requested'
                    yield 'Use the [secure Git connection and operation form](/git-connect/?'+urlencode(params)+'). Your saved connection is reused; if needed, connect there and run the operation. Never paste a token into chat. The form shows the repository and branch before execution.'
                    return
                history=[]
                for message in messages[-7:-1]:
                    if message.get('role') in ('user','assistant') and isinstance(message.get('content'),str):
                        history.append(message['role']+': '+message['content'][:1500])
                started=await api({'action':'start','conversation_id':cid,'request_id':uuid.uuid4().hex,'prompt':latest,'context':'\n'.join(history)[-6000:]})
                previous_events=[]
                if __event_emitter__:
                    await __event_emitter__({'type':'status','data':{'description':'Preparing your workspace…','done':False}})
                deadline=time.monotonic()+660
                while time.monotonic()<deadline:
                    data=await api({'action':'status','job_id':started['job_id']})
                    current_events=data.get('events') or []
                    for event in event_updates(previous_events,current_events):
                        trace.append(event)
                        if event.get('type')=='text':
                            # Narrative progress stays in the execution log until its outcome is known.
                            continue
                        elif event.get('type')=='tool_result':
                            call=next((e for e in reversed(trace) if e.get('type')=='tool_use' and e.get('tool_call_id')==event.get('tool_call_id')),None)
                            if __event_emitter__:
                                await __event_emitter__({'type':'status','data':{'description':tool_summary(event,call),'done':False}})
                        elif event.get('type')=='tool_use' and __event_emitter__:
                            await __event_emitter__({'type':'status','data':{'description':{'read_file':'Reading project files…','write_file':'Saving changes…','patch':'Applying changes…','terminal':'Running command…','recovery_verify':'Checking saved changes…'}.get(event.get('name'),'Working…'),'done':False}})
                    previous_events=current_events
                    if data['state'] not in ('queued','running'):
                        final_status='Task verified' if data.get('verified') else 'Needs attention'
                        yield result_message(data)
                        return

                    await asyncio.sleep(2)
                yield '**Not verified.** The coding task has not returned a final result. [Inspect its project](/projects/#'+started['project_id']+').'
            except (RuntimeError,aiohttp.ClientError,asyncio.TimeoutError) as exc:
                yield '**Not verified.** '+(str(exc) if isinstance(exc,RuntimeError) else 'The coding connection was interrupted.')+('\n\n[Inspect the project](/projects/#'+started['project_id']+')' if started else ' No successful file creation is confirmed.')
            finally:
                if __event_emitter__:
                    await __event_emitter__({'type':'status','data':{'description':final_status,'done':True}})
