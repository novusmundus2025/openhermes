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
        if message.get('tool_calls') or not isinstance(content, str):
            continue
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


def completion_with_retry(request, send):
    """One bounded protocol retry. Never extract actions from mixed narrative."""
    import copy
    def missing_call(choice):
        return choice.get('finish_reason')=='tool_calls' and not (choice.get('message') or {}).get('tool_calls')
    def malformed(data):
        return any(missing_call(c) or (not (c.get('message') or {}).get('tool_calls') and
                   invalid_tool_protocol((c.get('message') or {}).get('content')))
                   for c in data.get('choices', []))
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
