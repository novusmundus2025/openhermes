"""Reusable artifact/HTTP contract checks for live coding-agent regression runs."""
import hashlib
import json


def digest(data):
    return hashlib.sha256(data).hexdigest()


def execution_issue(events):
    final = next((e for e in reversed(events) if e.get('type') == 'result'), None)
    if final is None:
        return 'No final agent result was received'
    if final.get('exit_code', 0):
        return 'Agent reported an execution failure'
    text = str(final.get('text') or '')
    if any(marker in text.lower() for marker in ('verification is incomplete','request failed;','ended with tool_calls but returned no tool call')):
        return 'Agent reported incomplete or failed tool execution'
    if 'File-mutation verifier:' in text:
        return 'Native verifier reported an unresolved file-edit failure'
    from provider_tools import invalid_tool_protocol
    if invalid_tool_protocol(text) or text.startswith('Tool execution failed:'):
        return 'Model returned unexecuted tool-call text'
    return None


def evaluate_contract(contract, files, responses, events):
    """Evaluate observed evidence, not the assistant's claims. No code execution here."""
    failures = []
    for path, expected in contract.get('preserve', {}).items():
        if path not in files or digest(files[path]) != expected:
            failures.append('Unrelated file changed: ' + path)
    for path in contract.get('required_files', []):
        if not files.get(path):
            failures.append('Missing requested artifact: ' + path)
    for route, check in contract.get('http', {}).items():
        response = responses.get(route, {})
        if response.get('status') != 200:
            failures.append('Endpoint did not return HTTP 200: ' + route)
            continue
        body = response.get('body', '')
        for value in check.get('contains', []):
            if value.lower() not in body.lower():
                failures.append('Endpoint missing expected content: ' + route + ' / ' + value)
        if check.get('openapi'):
            try:
                spec = json.loads(body)
                assert str(spec.get('openapi', '')).startswith('3.')
                for path, methods in check['openapi'].items():
                    for method in methods:
                        operation = spec['paths'][path][method]
                        assert operation.get('responses')
            except (ValueError, KeyError, TypeError, AssertionError):
                failures.append('Missing or invalid OpenAPI operations: ' + route)
    # A prior failed call may be recovered. The final native mutation verifier is
    # the authoritative unresolved-edit signal; never treat model prose as proof.
    final = next((e for e in reversed(events) if e.get('type') == 'result'), {})
    if not final or final.get('exit_code', 0):
        failures.append('Agent did not finish successfully')
    text = final.get('text', '')
    if 'File-mutation verifier:' in text or text.lstrip().startswith(('<function=', '<tool_call>')):
        failures.append('Unresolved tool execution or file mutation failure')
    return {'passed': not failures, 'failures': failures}


def swagger_contract(existing_docs):
    return {'preserve': {'API.md': digest(existing_docs)},
            'http': {'/docs': {'contains': ['swagger-ui', 'SwaggerUIBundle', '/openapi.json']},
                     '/openapi.json': {'openapi': {'/students': ['get', 'post'],
                                                  '/students/{id}': ['get', 'put', 'delete']}}}}
