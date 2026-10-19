"""
title: Hermes Guided Browser — Ehda
author: OpenHermes
description: Native Hermes browsing with bounded Ehda AI control and a human login lane.
version: 2.0.0
"""
import asyncio
import json
from pathlib import Path
import re
import time
from pydantic import BaseModel, Field
import aiohttp


def starting_url(message):
    # Prefer a Markdown link's destination; do not treat its brackets/emphasis as URL text.
    link=re.search(r'\[[^\]]*\]\((https?://[^\s)]+)\)',message)
    if link:return link[1]
    url=re.search(r'https?://[^\s<>\[\]`*]+',message)
    if url:return url[0].rstrip(').,')
    if re.search(r'\b(?:open|visit|use|search|on|using)\s+(?:(?:with|on)\s+)?google\b|\bgoogle\s+(?:search|for|latest)\b',message,re.I):
        return 'https://www.google.com/'
    return None


class Pipe:
    class Valves(BaseModel):
        BROWSER_URL: str = Field(default='http://172.17.0.1:8643', description='OpenHermes private bridge. This replaces the old browser:8077 service.')
        EXEC_MODEL: str = Field(default='mundusx-agnostic', description='Must match the model configured on the OpenHermes server.')
        MAX_STEPS: int = Field(default=10, ge=1, le=25, description='Maximum AI actions per run; server also enforces a ten-minute budget.')
        LEGEND_MAX: int = Field(default=25, ge=1, le=100, description='Maximum elements shown for an explicit manual refresh.')
        WORKSPACE_URL: str = Field(default='/guided-browser/', description='Authenticated human browser workspace; no separate broker or VNC URL required.')

    def __init__(self):
        self.valves=self.Valves()

    def _message(self,data,summary,show_elements=False):
        text=summary+'\n\n[Open live browser / human control]('+self.valves.WORKSPACE_URL+')'
        if show_elements and data.get('control','human')=='human':
            # Do not include login-page snapshots or filled field values in chat.
            snapshot=str(data.get('snapshot',''))
            if not re.search(r'password|passcode|verification code|captcha',snapshot,re.I):
                lines=[]
                for line in snapshot.splitlines():
                    if '[ref=' not in line:continue
                    if re.search(r'textbox|searchbox|combobox|spinbutton',line,re.I):
                        match=re.search(r'\[ref=e\d+\]',line)
                        line=line[:match.end()] if match else '[input omitted]'
                    lines.append(line)
                if lines:text+='\n\n```text\n'+'\n'.join(lines[:self.valves.LEGEND_MAX])[:4000]+'\n```'
        if data.get('session_id'):
            text+='\n\n<!-- openhermes-browser view='+str(data.get('view',0))+' sid='+data['session_id']+' -->'
        return text

    async def pipe(self,body:dict,__user__:dict,__task__=None,__event_emitter__=None):
        if __task__ or (body.get('metadata') or {}).get('task') or body.get('task'):
            yield ''
            return
        if not __user__ or __user__.get('role') not in ('user','admin') or not __user__.get('id'):
            raise ValueError('An approved Open WebUI account is required')
        messages=body.get('messages') or []
        latest=next((m.get('content','') for m in reversed(messages) if m.get('role')=='user'),'')
        if isinstance(latest,list):latest=' '.join(p.get('text','') for p in latest if isinstance(p,dict))
        if not isinstance(latest,str):raise ValueError('Send a text browser task')
        message=latest.strip();command=message.lower()
        config=json.loads(Path('/app/backend/data/openhermes-bridge.json').read_text())
        identity={k:__user__.get(k,'') for k in ('id','name','email')}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=150)) as client:
            async def call(action,**values):
                async with client.post(self.valves.BROWSER_URL.rstrip('/')+'/browser',
                    headers={'Authorization':'Bearer '+config['key']},
                    json={'user':identity,'body':{'action':action,**values}}) as response:
                    data=await response.json()
                    if response.status!=200:raise RuntimeError(data.get('error','Browser request failed'))
                    return data
            if command in ('/help','help','?','/browser',''):
                yield self._message({},'Give a read-only browser task with a public URL, or use `/open URL`. '
                    'Commands: `/auto task`, `/continue`, `/pause`, `/status`, `/refresh`, `/click e1`, `/close`. '
                    'Enter passwords and verification codes only in human control, never in chat.')
                return
            if command in ('/pause','pause','/stop','stop','/login','take control'):
                data=await call('pause')
                yield self._message(data,'AI is paused. Use the browser workspace for login or manual actions. Resume explicitly with `/continue`.')
                return
            data=await call('status')
            sid=data.get('session_id')
            if command=='/close':
                if sid:await call('stop',session_id=sid)
                yield 'Browser session closed and its private runtime cleared.'
                return
            if command=='/status':
                yield self._message(data,(data.get('auto') or {}).get('summary') or ('Browser is open.' if sid else 'No active browser session.'))
                return
            manual_click=re.fullmatch(r'/?click\s+(@?e\d+)',message,re.I)
            if command in ('/refresh','refresh') or manual_click:
                if not sid:raise ValueError('Open a public URL first')
                if manual_click:
                    previous=next((m.get('content','') for m in reversed(messages) if m.get('role')=='assistant'),'')
                    match=re.search(r'<!-- openhermes-browser view=(\d+) sid=([a-f0-9]+) -->',previous if isinstance(previous,str) else '')
                    if not match or match[2]!=sid:raise ValueError('Use /refresh to obtain a current element view before clicking')
                    data=await call('click',session_id=sid,view=int(match[1]),ref=manual_click[1])
                else:data=await call('refresh',session_id=sid)
                yield self._message(data,'Current browser view refreshed.',True)
                return
            if re.match(r'/?(type|password|otp|passcode)\b',command):
                yield self._message(data,'Fill fields in human control in the browser workspace. Do not put credentials in chat.')
                return
            target=starting_url(message)
            if not sid:
                if not target:raise ValueError('Include a public website URL, or ask to open Google, to begin')
                data=await call('start');sid=data['session_id']
            manual_open=command.startswith('/open ') or bool(re.fullmatch(r'https?://\S+',message))
            if target:
                data=await call('navigate',session_id=sid,url=target)
            elif command!='/continue':
                data=await call('refresh',session_id=sid)
            else:
                data=await call('refresh',session_id=sid)
            if manual_open:
                yield self._message(data,'Website opened. Give a browser task to enable AI, or use the workspace manually.',True)
                return
            goal='' if command=='/continue' else re.sub(r'^/auto\s+','',message,flags=re.I)
            run=await call('auto_start',session_id=sid,goal=goal,max_steps=self.valves.MAX_STEPS,model=self.valves.EXEC_MODEL)
            finished=False
            try:
                if __event_emitter__:
                    await __event_emitter__({'type':'status','data':{'description':'AI browsing started. Use the live workspace to pause or take control.','done':False}})
                deadline=time.monotonic()+660
                last_steps=-1
                while time.monotonic()<deadline:
                    run=await call('auto_status')
                    auto=run.get('auto') or {}
                    if __event_emitter__ and auto.get('steps',0)!=last_steps:
                        last_steps=auto.get('steps',0)
                        await __event_emitter__({'type':'status','data':{'description':auto.get('summary','Working…'),'done':False}})
                    if auto.get('state')!='running':
                        finished=True
                        yield self._message(run,auto.get('summary') or 'AI browser run ended.')
                        return
                    await asyncio.sleep(1.5)
                await call('pause')
                finished=True
                yield self._message(run,'AI paused at the response deadline. Review the live workspace.')
            finally:
                if not finished:
                    try:await asyncio.shield(call('pause'))
                    except Exception:pass
                if __event_emitter__:
                    await __event_emitter__({'type':'status','data':{'description':'Browser run finished or handed to human control.','done':True}})
