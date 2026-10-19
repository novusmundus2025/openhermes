"""One bounded recovery turn around a native Hermes command; protections stay enabled."""
import json
import re
import subprocess
import sys


def payload(event):
    try:
        data = json.loads(event.get('output', '{}'))
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def blocked_paths(events):
    calls = {e.get('tool_call_id'): e for e in events if e.get('type') == 'tool_use'}
    paths = set()
    for event in events:
        if event.get('type') != 'tool_result':
            continue
        data = payload(event)
        call = calls.get(event.get('tool_call_id'), {})
        name = event.get('name')
        error = str(data.get('error', ''))
        if name in ('write_file','patch') and (data.get('stale_write_blocked') or
                any(s in error.lower() for s in ('old_string not found','could not find a match','read the current file','refusing to overwrite'))):
            path = data.get('resolved_path') or data.get('path') or (call.get('input') or {}).get('path')
            if isinstance(path,str) and not path.startswith('/'):
                path = '/workspace/' + path
            if isinstance(path,str) and path.startswith('/workspace/') and '..' not in path.split('/'):
                paths.add(path)
    return sorted(paths)


def verified_recovery(paths, events):
    calls, stages = {}, {p:0 for p in paths}
    for event in events:
        if event.get('type') == 'tool_use':
            calls[event.get('tool_call_id')] = event
        if event.get('type') != 'tool_result' or event.get('is_error'):
            continue
        call = calls.get(event.get('tool_call_id'), {})
        path = (call.get('input') or {}).get('path')
        if isinstance(path,str) and not path.startswith('/'):
            path = '/workspace/' + path
        if path not in stages:
            continue
        data = payload(event)
        if data.get('error'):
            continue
        if event.get('name') in ('read_file','recovery_verify') and isinstance(data.get('content'),str) and not data.get('truncated') and not data.get('not_found') and (call.get('input') or {}).get('offset',1)==1:
            stages[path] = 3 if stages[path] == 2 else max(stages[path],1)
        elif event.get('name') in ('write_file','patch') and stages[path] == 1 and (data.get('verified') is True or data.get('success') is True):
            stages[path] = 2
    return all(stage == 3 for stage in stages.values())


def apply_verified_diff(original, diff):
    """Apply native unified diff only when every old/context line matches the snapshot."""
    source=original.decode().splitlines(keepends=True)
    lines=diff.splitlines(keepends=True);result=[];cursor=0;i=0;hunks=0
    while i<len(lines):
        match=re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@',lines[i])
        if not match:
            if hunks and lines[i].strip():raise ValueError('Unexpected diff trailer')
            i+=1;continue
        start=int(match[1])-1 if int(match[1]) else 0
        if start<cursor or start>len(source):raise ValueError('Overlapping diff')
        result.extend(source[cursor:start]);cursor=start;i+=1;old_count=new_count=0;hunks+=1
        while i<len(lines) and not lines[i].startswith('@@ '):
            line=lines[i]
            if not line or line[0] not in ' +-':raise ValueError('Unsupported diff line')
            mode,content=line[0],line[1:]
            if i+1<len(lines) and lines[i+1].startswith('\\ No newline at end of file'):
                content=content.rstrip('\r\n');i+=1
            if mode in ' -':
                if cursor>=len(source) or source[cursor]!=content:raise ValueError('Diff context mismatch')
                cursor+=1;old_count+=1
            if mode in ' +':result.append(content);new_count+=1
            i+=1
        if old_count!=int(match[2] or 1) or new_count!=int(match[4] or 1):raise ValueError('Incomplete diff')
    if not hunks:raise ValueError('No diff hunks')
    result.extend(source[cursor:]);return ''.join(result).encode()


def verify_saved(root, before, events, emit):
    from projects import read_bytes
    expected = dict(before)
    calls = {e.get('tool_call_id'): e for e in events if e.get('type')=='tool_use'}
    for event in events:
        if event.get('type')!='tool_result' or event.get('is_error') or payload(event).get('error'):
            continue
        call=calls.get(event.get('tool_call_id'),{});args=call.get('input') or {};path=args.get('path','')
        if path and not path.startswith('/'):path='/workspace/'+path
        if path not in expected:continue
        if event.get('name')=='write_file' and isinstance(args.get('content'),str):
            expected[path]=args['content'].encode()
        elif event.get('name')=='patch' and payload(event).get('success') and payload(event).get('diff'):
            expected[path]=apply_verified_diff(expected[path],payload(event)['diff'])
        elif event.get('name')=='patch' and payload(event).get('success') and args.get('old_string'):
            old,new=args['old_string'],args.get('new_string','')
            text=expected[path].decode()
            if old in text:expected[path]=text.replace(old,new,-1 if args.get('replace_all') else 1).encode()
    receipts=[]
    for path, content in expected.items():
        actual=read_bytes(root,path[len('/workspace/'):])
        if actual!=content:continue
        key='recovery-verify-'+str(len(receipts))
        pair=[{'type':'tool_use','name':'recovery_verify','tool_call_id':key,'input':{'path':path}},
              {'type':'tool_result','name':'recovery_verify','tool_call_id':key,'output':json.dumps({'content':actual.decode(),'truncated':False,'verified':True,'verification_source':'independent disk read-back'}),'is_error':False}]
        for event in pair:emit(event)
        receipts.extend(pair)
    return receipts


def run(command, prompt, emit, workspace_root=None):
    def attempt(query):
        events = []
        with subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True) as process:
            process.stdin.write(query);process.stdin.close()
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    sys.stderr.write(line)
                    continue
                events.append(event)
                if event.get('type') != 'result':
                    emit(event)
            return process.wait(),events
    code, events = attempt(prompt)
    paths = blocked_paths(events)
    final = next((e for e in reversed(events) if e.get('type')=='result'),None)
    if paths and len(paths)<=5 and code not in (124,130,137):
        if final:
            emit(dict(final,text='A file edit was blocked. Reading current files and attempting one recovery.',exit_code=1))
        recovery = ('Recover the blocked edit from the previous attempt. Original user request:\n'+prompt+
                    '\n\nBlocked files: '+json.dumps(paths)+'. For EACH file: use read_file to read its full CURRENT content, '
                    'merge only changes required by the original request, use patch/write_file, then call read_file AGAIN '
                    'to verify the saved result. Never overwrite from a stale conversation copy. Preserve unrelated content. '
                    'Do not disable guards or use terminal writes to bypass them. Stop and report if recovery cannot be completed.')
        before = {}
        if workspace_root:
            from projects import read_bytes
            try:before={path:read_bytes(workspace_root,path[len('/workspace/'):]) for path in paths}
            except (OSError,ValueError):pass
        code, retried = attempt(recovery)
        if before:
            try:retried.extend(verify_saved(workspace_root,before,retried,emit))
            except (OSError,ValueError,UnicodeError):pass
        final = next((e for e in reversed(retried) if e.get('type')=='result'),None)
        from agent_contracts import execution_issue
        if code or not verified_recovery(paths,retried) or execution_issue(retried):
            final = dict(final or {},type='result',exit_code=1,text='Automatic file-edit recovery failed or could not be verified. '
                         'The overwrite protection remains enabled. Inspect the tool results before retrying.\n\n'+(final or {}).get('text',''))
            code = code or 1
        else:
            final = dict(final,recovery_verified=True)
    if not final:
        final = {'type':'result','exit_code':code or 1,'text':'Agent ended without a final result.'}
        code = code or 1
    emit(final)
    return code


if __name__ == '__main__':
    command=sys.argv[1:]
    root=None
    if command[:1]==['--workspace-root']:
        root=command[1];command=command[2:]
    if command[:1]==['--']:command=command[1:]
    raise SystemExit(run(command,sys.stdin.read(),lambda event:print(json.dumps(event),flush=True),root))
