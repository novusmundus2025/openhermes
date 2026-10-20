"""Normalize Qwen-style tool text into the tools explicitly offered by the client."""
import json
import re
import uuid


def normalize_tools(response, request):
    if request.get('tool_choice') == 'none':
        return response
    offered = {tool.get('function', {}).get('name'): tool.get('function', {})
               for tool in request.get('tools', []) if isinstance(tool, dict)}
    for choice in response.get('choices', []):
        message = choice.get('message') or {}
        content = message.get('content')
        if message.get('tool_calls'):
            selected=request.get('tool_choice')
            calls=message['tool_calls']
            def allowed(call):
                function=call.get('function',{});name=function.get('name')
                if name not in offered or isinstance(selected,dict) and name!=selected.get('function',{}).get('name'):return False
                schema=offered[name].get('parameters') or {}
                try:arguments=json.loads(function.get('arguments','{}'))
                except (ValueError,TypeError):return False
                if not isinstance(arguments,dict) or any(key not in arguments for key in schema.get('required',[])):return False
                return all('enum' not in definition or arguments.get(key) in definition['enum'] for key,definition in schema.get('properties',{}).items() if key in arguments)
            if any(not allowed(call) for call in calls):
                message['tool_calls']=[]
                message['content']='Tool execution failed: the provider returned an unoffered tool or disallowed arguments. No action was executed.'
                choice['finish_reason']='tool_calls'
            continue
        if not isinstance(content, str):continue
        raw_content = content.strip()
        thought = re.match(r'^<think>(.*?)</think>\s*', raw_content, re.S)
        if thought:
            message.setdefault('reasoning_content', thought[1])
            raw_content = raw_content[thought.end():]
        if raw_content.startswith('</think>'):
            raw_content = raw_content[len('</think>'):].strip()
        if raw_content.startswith('<tool_call>'):
            raw_content = raw_content[len('<tool_call>'):].strip()
        # Some providers omit the opening wrapper but still emit its closing tag.
        # Strip only this protocol envelope; never extract calls from narrative text.
        if raw_content.endswith('</tool_call>'):
            raw_content = raw_content[:-len('</tool_call>')].strip()
        match = re.fullmatch(r'<function=([\w.-]+)>(.*)', raw_content, re.S)
        if not match or match[1] not in offered:
            continue
        name, raw = match[1], match[2].strip()
        selected = request.get('tool_choice')
        if isinstance(selected, dict) and selected.get('function', {}).get('name') != name:
            continue
        if raw.endswith('</function>'):
            raw = raw[:-len('</function>')].strip()
        schema = offered[name].get('parameters') or {}
        properties = schema.get('properties') or {}
        arguments, cursor, valid = {}, 0, True
        for parameter in re.finditer(r'<parameter=([\w.-]+)>(.*?)</parameter>', raw, re.S):
            key, value = parameter[1], parameter[2].strip('\r\n')
            if raw[cursor:parameter.start()].strip() or key in arguments or key not in properties:
                valid = False
                break
            cursor = parameter.end()
            kind = properties[key].get('type', 'string')
            try:
                if kind != 'string':
                    value = json.loads(value)
                    expected = {'integer': int, 'number': (int, float), 'boolean': bool, 'array': list, 'object': dict}.get(kind)
                    if expected is None or not isinstance(value, expected) or kind in ('integer', 'number') and isinstance(value, bool):
                        raise ValueError()
            except (ValueError, TypeError):
                valid = False
                break
            if 'enum' in properties[key] and value not in properties[key]['enum']:
                valid = False
                break
            arguments[key] = value
        if not valid or raw[cursor:].strip() or not arguments or any(k not in arguments for k in schema.get('required', [])):
            continue
        message['content'] = None
        message['tool_calls'] = [{'id': 'call_' + uuid.uuid4().hex[:20], 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}]
        choice['finish_reason'] = 'tool_calls'
    return response

def invalid_tool_protocol(text):
    """Detect executable-looking protocol outside quoted Markdown examples."""
    if not isinstance(text, str):
        return False
    unquoted = re.sub(r'(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$', '', text)
    return bool(re.search(r'<function=[\w.-]+>|</?tool_call>|\[OUT-OF-BAND USER MESSAGE\]', unquoted))


def scoped_edit_policy(request):
    """Honor an explicit patch-only instruction without exposing whole-file writes."""
    import copy
    user=next((m.get('content') for m in reversed(request.get('messages') or []) if m.get('role')=='user'),'')
    if request.get('tool_choice')=='none' or isinstance(request.get('tool_choice'),dict):return request
    if not isinstance(user,str):return request
    current=user.split('Completion requirements:')[0].split('Historical conversation context')[0]
    read_only=bool(re.search(r'\bread.only\b|\bdo not edit\b',current,re.I))
    if not read_only and not re.search(r'\bpatch only\b',current,re.I):return request
    result=copy.deepcopy(request)
    forbidden={'write_file','patch'} if read_only else {'write_file'}
    result['tools']=[t for t in result.get('tools',[]) if t.get('function',{}).get('name') not in forbidden]
    paths=set(re.findall(r'(?<![\w/])([\w./-]+\.(?:js|py))\b',user.split('Completion requirements:')[0]))
    paths={p.removeprefix('/workspace/').removeprefix('./') for p in paths if '..' not in p.split('/') and p.lower()!='node.js'}
    commands=['pwd','ls -la','ls -la /workspace','git status','git status --short','git diff --stat','git log -2 --oneline','npm test','npm run test','node --test']
    for path in sorted(paths):
        commands.append(('node --check ' if path.endswith('.js') else 'python -m py_compile ')+path)
        if path=='server.js' and re.search(r'\b(?:import|export)\b',user,re.I):
            commands.append('node -e "const s=require(\'./server.js\').server; if(!s || s.listening) process.exit(1); console.log(\'PASS import without listening\')"')
    commands += ['cd /workspace && '+c for c in list(commands)]
    for tool in result['tools']:
        function=tool.get('function',{})
        if function.get('name')=='terminal':
            parameters=function.setdefault('parameters',{}).setdefault('properties',{})
            parameters.setdefault('command',{'type':'string'})['enum']=commands
            function['description']=(function.get('description') or '')+' Scoped task: terminal is limited to the listed read/check commands. Use patch for edits; shell writes are unavailable.'
    return result


def coding_tool_policy(request):
    """Require real tool work until a relevant check returns, including a reported failure."""
    import copy
    names={t.get('function',{}).get('name') for t in request.get('tools',[]) if isinstance(t,dict)}
    if 'terminal' not in names or not names.intersection({'write_file','patch'}) or request.get('tool_choice','auto')!='auto':return request
    messages=request.get('messages') or []
    start=next((i for i in range(len(messages)-1,-1,-1) if messages[i].get('role')=='user'),None)
    if start is None:return request
    text=messages[start].get('content')
    if not isinstance(text,str):return request
    text=text.split('Completion requirements:')[0].split('Historical conversation context')[0]
    if not (re.search(r'\b(create|implement|build|write|save|fix|update|modify|add)\b',text,re.I) and re.search(r'\b(api|app|application|program|script|website|server\.js|test\.js)\b',text,re.I)):return request
    require_test=bool(re.search(r'\btests?\b',text,re.I))
    calls={}
    for message in messages[start+1:]:
        for call in message.get('tool_calls') or []:
            if call.get('function',{}).get('name')=='terminal':
                try:calls[call.get('id')]=json.loads(call['function']['arguments']).get('command','')
                except (ValueError,TypeError,KeyError):pass
        if message.get('role')!='tool':continue
        command=calls.get(message.get('tool_call_id'),'')
        check=re.search(r'(?:^|&&|;)\s*(?:node\b|python[3]?\b|pytest\b|npm\s+(?:test|run\s+(?:test|lint|build|check))\b|cargo\s+(?:test|check)\b|go\s+test\b)',command)
        if require_test and not re.search(r'npm\s+(?:test|run\s+test)|--test|pytest|test[\w.-]*\.(?:js|py)|cargo\s+test|go\s+test',command):continue
        try:result=json.loads(message.get('content',''))
        except (ValueError,TypeError):continue
        if check and isinstance(result,dict) and type(result.get('exit_code')) is int and result['exit_code']>=0 and not result.get('error'):return request
    updated=copy.deepcopy(request);updated['tool_choice']='required';return updated


def current_request_text(text):
    if not isinstance(text,str):return ''
    text=text.rsplit('User request:\n',1)[-1]
    return text.split('Completion requirements:')[0].split('Historical conversation context')[0].strip()


def explanation_request(text):
    text=current_request_text(text)
    mutation=r'(?:fix|update|modify|edit|delete|remove|create|implement|refactor|restructure|build|add)'
    if re.search(r'\b(?:and|then|also)\s+(?:please\s+)?'+mutation+r'\b',text,re.I):return False
    if re.search(r'^(?:please\s+)?'+mutation+r'\b|\b(?:can|could|would)\s+(?:you|u)\s+(?:please\s+)?'+mutation+r'\b',text,re.I):return False
    return bool(re.search(r'\b(?:explain|describe|walk me through)\b|\bshow\b.{0,70}\b(?:flow|architecture|how|program works)\b|\bhow\b.{0,60}\bworks?\b|\bwhat\b.{0,60}\bdoes?\b',text,re.I))


def explanation_policy(request):
    import copy
    messages=request.get('messages') or []
    start=next((i for i in range(len(messages)-1,-1,-1) if messages[i].get('role')=='user'),None)
    if start is None or not explanation_request(messages[start].get('content')):return request
    result=copy.deepcopy(request)
    result['tools']=[t for t in result.get('tools',[]) if t.get('function',{}).get('name')=='read_file']
    result['tool_choice']='auto'
    calls={};seen=set();imports=set();attempts=0
    for message in messages[start+1:]:
        for call in message.get('tool_calls') or []:
            try:
                if call['function']['name']=='read_file':calls[call['id']]=json.loads(call['function']['arguments']).get('path','')
            except (KeyError,ValueError,TypeError):pass
        if message.get('role')!='tool' or message.get('tool_call_id') not in calls:continue
        attempts+=1
        try:value=json.loads(message.get('content',''))
        except (ValueError,TypeError):continue
        path=calls[message['tool_call_id']]
        if isinstance(value,dict) and 'content' in value and not any(value.get(k) for k in ('error','truncated','not_found')) and '.hermes-project/' not in path and not path.endswith('AGENTS.md'):
            path=path.removeprefix('/workspace/').removeprefix('./')
            seen.add(path)
            if path.endswith(('.js','.cjs','.mjs')):
                import posixpath
                for target in re.findall(r"require\(\s*['\"](\.{1,2}/[^'\"]+)['\"]\s*\)",str(value['content'])):
                    resolved=posixpath.normpath(posixpath.join(posixpath.dirname(path),target))
                    if resolved.startswith('../') or not re.fullmatch(r'[\w./-]+',resolved):continue
                    candidates=(resolved,) if resolved.endswith(('.js','.cjs','.mjs','.json')) else (resolved+'.js',resolved+'/index.js')
                    imports.add(candidates)
    pending=[group for group in sorted(imports) if not any(path in seen for path in group)]
    if len(seen)>=8 or attempts>=12 or not result['tools'] or (len(seen)>=2 and not pending):
        result['tool_choice']='none'
        options=result.get('chat_template_kwargs')
        if isinstance(options,dict):
            if options.get('opengpu_native_tool_guard') is True:result['repetition_penalty']=1.1
            options.pop('opengpu_native_tool_guard',None);options.pop('opengpu_require_tool_call',None)
        cap=1536
        instruction='Inspection is finished. Answer in at most 400 words, with one clear startup-to-request-to-database flow. Do not repeat unknowns or duplicate lists. Inspection budget reached. Answer the explanation question now from inspected code. Explain the entry point, request flow, modules and data flow. State any gaps explicitly. Do not claim tests or edits.'
    else:
        cap=512
        if len(seen)<2 or pending:result['tool_choice']='required'
        instruction='For Node apps, npm start follows package.json scripts.start; main describes library imports and may point to a legacy file. Read the startup script target before explaining execution. Read-only explanation task. Use read_file only as needed, beginning with package.json and its actual entry point, then follow imports. Answer once enough evidence is available. Do not edit files, run tests, or continue an earlier repair. Distinguish verified code flow from runtime assumptions.'
    key='max_completion_tokens' if 'max_completion_tokens' in result else 'max_tokens'
    current=result.get(key)
    result[key]=min(current,cap) if type(current) is int and current>0 else cap
    if pending and result['tool_choice']!='none':instruction+=' Inspect these unresolved local imports before answering (alternative resolution paths in each group): '+json.dumps(pending)
    result.setdefault('messages',[]).append({'role':'system','content':instruction})
    return result


def finish_scoped_checks(request):
    """Stop a patch-only verification loop after requested checks and readback succeed."""
    import copy
    if request.get('tool_choice','auto')!='auto':return request
    messages=request.get('messages') or []
    start=next((i for i in range(len(messages)-1,-1,-1) if messages[i].get('role')=='user'),None)
    if start is None:return request
    text=messages[start].get('content','')
    if not isinstance(text,str):return request
    text=text.split('Completion requirements:')[0].split('Historical conversation context')[0]
    if not re.search(r'\bpatch only\b',text,re.I):return request
    paths=set(re.findall(r'(?<![\w/])([\w./-]+\.js)\b',text))
    paths={p.removeprefix('/workspace/').removeprefix('./') for p in paths if '..' not in p.split('/') and p.lower()!='node.js'}
    if not paths or 'node --check' not in text:return request
    calls={};reads=set();checks=set();diff=False
    for message in messages[start+1:]:
        for call in message.get('tool_calls') or []:
            try:calls[call.get('id')]=(call['function']['name'],json.loads(call['function']['arguments']))
            except (KeyError,ValueError,TypeError):pass
        if message.get('role')!='tool':continue
        name,args=calls.get(message.get('tool_call_id'),('',{}))
        try:result=json.loads(message.get('content',''))
        except (TypeError,ValueError):continue
        if not isinstance(result,dict):continue
        path=str(args.get('path','')).removeprefix('/workspace/').removeprefix('./')
        if name in ('patch','write_file'):
            # Any later edit attempt invalidates earlier evidence, even if rejected.
            reads.clear();checks.clear();diff=False
        if name=='read_file' and not result.get('error') and not result.get('truncated') and not result.get('not_found') and 'content' in result:
            reads.add(path)
        if name=='terminal' and result.get('exit_code')==0 and not result.get('error'):
            command=args.get('command','').removeprefix('cd /workspace && ').strip()
            if command.startswith('node --check '):checks.add(command[len('node --check '):].removeprefix('/workspace/').removeprefix('./'))
            if command=='git diff --stat':diff=True
    if not paths<=reads or not paths<=checks or ('git diff --stat' in text and not diff):return request
    result=copy.deepcopy(request);result['tool_choice']='none'
    options=result.get('chat_template_kwargs')
    if isinstance(options,dict):
        options.pop('opengpu_native_tool_guard',None);options.pop('opengpu_require_tool_call',None)
    result.setdefault('messages',[]).append({'role':'system','content':'The scoped file readback and requested checks have completed successfully. Return the final report now. Describe only the checks actually run; do not claim full application verification.'})
    return result


def native_tool_profile(request, profile=None):
    """Enable an installed Qwen/MLX guard only on explicitly configured providers."""
    import copy
    if profile != 'qwen3-coder-mlx' or request.get('parallel_tool_calls') is True:
        return request
    names={t.get('function',{}).get('name') for t in request.get('tools',[]) if isinstance(t,dict)}
    if not {'read_file','patch','terminal'} <= names or request.get('tool_choice') == 'none':
        return request
    updated=copy.deepcopy(request)
    updated['parallel_tool_calls']=False
    updated.setdefault('repetition_penalty',1.0)
    options=updated.get('chat_template_kwargs')
    if options is not None and not isinstance(options,dict):
        raise ValueError('Invalid chat template options')
    updated['chat_template_kwargs']=dict(options or {},opengpu_native_tool_guard=True)
    return updated


def completion_with_retry(request, send):
    """One bounded protocol retry. Never extract actions from mixed narrative."""
    import copy
    def missing_call(choice):
        return choice.get('finish_reason')=='tool_calls' and not (choice.get('message') or {}).get('tool_calls')
    def malformed(data):
        return any(missing_call(c) or (not (c.get('message') or {}).get('tool_calls') and
                   invalid_tool_protocol((c.get('message') or {}).get('content')))
                   for c in data.get('choices', []))
    request = explanation_policy(coding_tool_policy(finish_scoped_checks(scoped_edit_policy(request))))
    data = normalize_tools(send(request), request)
    if not request.get('tools') or request.get('tool_choice') == 'none' or not malformed(data):
        return data
    retry = copy.deepcopy(request)
    if any(missing_call(c) for c in data.get('choices',[])) and retry.get('tool_choice','auto')=='auto':
        retry['tool_choice']='required'
    retry.setdefault('messages', []).append({'role': 'system', 'content':
        'Tool protocol correction: your previous output was malformed and was not executed. '
        'Continue the actual user request using the advertised tools. Return a valid structured '
        'tool_calls response for the next action, or one complete tool protocol envelope. '
        'Do not print multiple unexecuted actions, invent user messages, or claim an action ran. '
        'Read the project files before deciding what changes are needed.'})
    data = normalize_tools(send(retry), retry)
    if malformed(data):
        for choice in data.get('choices', []):
            message = choice.get('message') or {}
            if missing_call(choice) or (not message.get('tool_calls') and invalid_tool_protocol(message.get('content'))):
                message['content'] = ('Tool execution failed: the model returned malformed tool-call text '
                                      'after one retry. No action was executed from that text. Please retry '
                                      'or use a model with reliable tool calling.')
                choice['finish_reason'] = 'stop'
    return data
