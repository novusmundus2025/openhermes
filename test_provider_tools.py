import json
import unittest
from provider_tools import normalize_tools, invalid_tool_protocol, completion_with_retry, coding_tool_policy, scoped_edit_policy, native_tool_profile, finish_scoped_checks, explanation_request, explanation_policy
from unittest.mock import Mock


class ProviderToolTests(unittest.TestCase):
    def request(self):
        return {'tools': [{'type': 'function', 'function': {'name': 'write_file', 'parameters': {
            'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}}, 'required': ['path', 'content']}}}]}

    def response(self, text):
        return {'choices': [{'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}]}

    def test_explanation_intent_and_mixed_edit_requests(self):
        for text in ['possible for u to show me the complete flow of the program?', 'Explain how this API works', 'What does deleteFile do?']:
            self.assertTrue(explanation_request(text),text)
        for text in ['Update server.js and explain the result', 'Explain the flow then fix the bug', 'Can u please restructure this application?']:
            self.assertFalse(explanation_request(text),text)
        self.assertTrue(explanation_request('Write a plan first.\nUser request:\nshow me the complete flow of the program?\n\nCompletion requirements: Save code'))

    def test_explanation_follows_actual_local_imports(self):
        request={'tools':[{'function':{'name':'read_file'}}], 'messages':[{'role':'user','content':'Explain how the program works'}]}
        for i,(path,content) in enumerate([('package.json','{}'),('src/server.js',"const models=require('./models');")]):
            request['messages'] += [{'role':'assistant','tool_calls':[{'id':str(i),'function':{'name':'read_file','arguments':json.dumps({'path':path})}}]}, {'role':'tool','tool_call_id':str(i),'content':json.dumps({'content':content})}]
        result=explanation_policy(request)
        self.assertEqual(result['tool_choice'],'required')
        self.assertIn('src/models/index.js',result['messages'][-1]['content'])
        request['messages'] += [{'role':'assistant','tool_calls':[{'id':'2','function':{'name':'read_file','arguments':'{"path":"/workspace/src/models/index.js"}'}}]}, {'role':'tool','tool_call_id':'2','content':'{"content":"module.exports = {};"}'}]
        self.assertEqual(explanation_policy(request)['tool_choice'],'none')

    def test_explanation_has_no_mutators_or_terminal_and_is_bounded(self):
        request={'tools':[{'function':{'name':name}} for name in ['read_file','write_file','patch','terminal']],
                 'messages':[{'role':'user','content':'Explain the flow of the program'}]}
        result=explanation_policy(request)
        self.assertEqual(result['max_tokens'],512)
        self.assertEqual([t['function']['name'] for t in result['tools']],['read_file'])
        self.assertEqual(len(request['tools']),4)
        for i in range(8):
            request['messages'] += [{'role':'assistant','tool_calls':[{'id':str(i),'function':{'name':'read_file','arguments':json.dumps({'path':'/workspace/src/'+str(i)+'.js'})}}]},
                {'role':'tool','tool_call_id':str(i),'content':'{"content":"code","truncated":false}'}]
        self.assertNotIn('repetition_penalty',explanation_policy(request))
        self.assertEqual(explanation_policy(dict(request,max_tokens=128))['max_tokens'],128)
        request['chat_template_kwargs']={'opengpu_native_tool_guard':True}
        final=explanation_policy(request)
        self.assertEqual(final['tool_choice'],'none')
        self.assertEqual(final['max_tokens'],1536)
        self.assertEqual(final['repetition_penalty'],1.1)

    def test_scoped_completion_needs_readback_and_all_requested_checks(self):
        request={'messages':[{'role':'user','content':'Patch only src/server.js. Run node --check src/server.js and git diff --stat.'}]}
        def result(name,args,value):
            key=str(len(request['messages']))
            request['messages'].extend([{'role':'assistant','tool_calls':[{'id':key,'function':{'name':name,'arguments':json.dumps(args)}}]}, {'role':'tool','tool_call_id':key,'content':json.dumps(value)}])
        result('patch',{'path':'/workspace/src/server.js'},{'success':True})
        result('terminal',{'command':'node --check src/server.js'},{'exit_code':0})
        result('terminal',{'command':'git diff --stat'},{'exit_code':0})
        self.assertIs(finish_scoped_checks(request),request)
        result('read_file',{'path':'/workspace/src/server.js'},{'content':'saved code','truncated':False})
        self.assertEqual(finish_scoped_checks(request)['tool_choice'],'none')
        self.assertNotIn('tool_choice',request)
        result('patch',{'path':'/workspace/src/server.js'},{'success':True})
        self.assertIs(finish_scoped_checks(request),request)

    def test_failed_verification_can_be_reported_instead_of_forcing_more_tools(self):
        request={'tools':[{'function':{'name':'terminal'}},{'function':{'name':'write_file'}}],
                 'messages':[{'role':'user','content':'Fix the application and run tests'},
                   {'role':'assistant','tool_calls':[{'id':'a','function':{'name':'terminal','arguments':'{"command":"npm test"}'}}]},
                   {'role':'tool','tool_call_id':'a','content':'{"exit_code":1,"output":"Database unavailable"}'}]}
        self.assertIs(coding_tool_policy(request),request)

    def test_native_guard_is_opt_in_and_preserves_request(self):
        request={'tools':[{'function':{'name':name}} for name in ['read_file','patch','terminal']],
                 'chat_template_kwargs':{'other':True}}
        self.assertIs(native_tool_profile(request),request)
        result=native_tool_profile(request,'qwen3-coder-mlx')
        self.assertFalse(result['parallel_tool_calls'])
        self.assertTrue(result['chat_template_kwargs']['opengpu_native_tool_guard'])
        self.assertTrue(result['chat_template_kwargs']['other'])
        self.assertNotIn('opengpu_native_tool_guard',request['chat_template_kwargs'])
        self.assertIs(native_tool_profile(dict(request,parallel_tool_calls=True),'qwen3-coder-mlx').get('parallel_tool_calls'),True)
        other={'tools':[{'function':{'name':'browser'}}]}
        self.assertIs(native_tool_profile(other,'qwen3-coder-mlx'),other)

    def test_coding_policy_requires_actions_until_a_real_check(self):
        request={'tools':[{'function':{'name':'terminal'}},{'function':{'name':'write_file'}}],
                 'messages':[{'role':'user','content':'Implement API server.js'}]}
        self.assertEqual(coding_tool_policy(request)['tool_choice'],'required')
        self.assertNotIn('tool_choice',request)
        request['messages'] += [{'role':'assistant','tool_calls':[{'id':'a','function':{'name':'terminal','arguments':'{"command":"node --check server.js"}'}}]},
                                {'role':'tool','tool_call_id':'a','content':'{"exit_code":0}'}]
        self.assertNotIn('tool_choice',coding_tool_policy(request))
        request['messages'][0]['content']='Implement API server.js and tests'
        self.assertEqual(coding_tool_policy(request)['tool_choice'],'required')
        request['messages'][0]['content']='Explain the README'
        self.assertNotIn('tool_choice',coding_tool_policy(request))
        request['tool_choice']='none'
        self.assertEqual(coding_tool_policy(request)['tool_choice'],'none')

    def test_explicit_patch_only_request_cannot_offer_whole_file_write(self):
        request={'messages':[{'role':'user','content':'Fix server.js; patch only the final block'}],
                 'tools':[{'function':{'name':'write_file'}},{'function':{'name':'patch'}},{'function':{'name':'terminal'}}]}
        scoped=scoped_edit_policy(request)
        self.assertNotIn('write_file',[t['function']['name'] for t in scoped['tools']])
        self.assertEqual(len(request['tools']),3)
        self.assertEqual(coding_tool_policy(scoped)['tool_choice'],'required')

    def test_unoffered_native_tool_cannot_escape_scope(self):
        response={'choices':[{'message':{'tool_calls':[{'id':'a','function':{'name':'terminal','arguments':'{}'}}]},'finish_reason':'tool_calls'}]}
        normalized=normalize_tools(response,self.request())
        self.assertEqual(normalized['choices'][0]['message']['tool_calls'],[])
        self.assertIn('unoffered tool',normalized['choices'][0]['message']['content'])

    def test_patch_only_terminal_cannot_write_through_shell(self):
        request={'messages':[{'role':'user','content':'Fix server.js; patch only final block; check import'}],
                 'tools':[{'function':{'name':'terminal','parameters':{'properties':{'command':{'type':'string'}}}}}]}
        scoped=scoped_edit_policy(request)
        command=scoped['tools'][0]['function']['parameters']['properties']['command']
        self.assertIn('node --check server.js',command['enum'])
        response={'choices':[{'message':{'tool_calls':[{'function':{'name':'terminal','arguments':json.dumps({'command':'echo replacement > server.js'})}}]},'finish_reason':'tool_calls'}]}
        self.assertEqual(normalize_tools(response,scoped)['choices'][0]['message']['tool_calls'],[])

    def test_read_only_scope_withholds_both_file_mutators(self):
        request={'messages':[{'role':'user','content':'Read-only. Do not edit, create or delete files. Run npm test.'}],
                 'tools':[{'function':{'name':name}} for name in ['write_file','patch','terminal','read_file']]}
        scoped=scoped_edit_policy(request)
        self.assertEqual([t['function']['name'] for t in scoped['tools']],['terminal','read_file'])

    def test_native_hermes_receives_a_real_structured_tool_call(self):
        result = normalize_tools(self.response('<function=write_file>\n<parameter=path>\n/workspace/index.html\n</parameter>\n<parameter=content>\n<h1>Hello</h1>\n</parameter>'), self.request())
        choice = result['choices'][0]
        self.assertEqual(choice['finish_reason'], 'tool_calls')
        self.assertEqual(json.loads(choice['message']['tool_calls'][0]['function']['arguments']), {'path': '/workspace/index.html', 'content': '<h1>Hello</h1>'})

    def test_unoffered_or_incomplete_tools_remain_text(self):
        for content in ('<function=terminal><parameter=command>id</parameter>',
                        '<function=write_file><parameter=path>x</parameter>',
                        'Here is an example: <function=write_file><parameter=path>x</parameter>'):
            result = normalize_tools(self.response(content), self.request())
            self.assertNotIn('tool_calls', result['choices'][0]['message'])

    def test_reasoning_and_tool_protocol_wrappers(self):
        content = '<think>Plan the file write.</think><tool_call><function=write_file><parameter=path>x</parameter><parameter=content>hi</parameter></function></tool_call>'
        result = normalize_tools(self.response(content), self.request())
        self.assertIn('tool_calls', result['choices'][0]['message'])
        request = self.request()
        request['tool_choice'] = 'none'
        result = normalize_tools(self.response(content), request)
        self.assertNotIn('tool_calls', result['choices'][0]['message'])

    def test_captured_read_file_with_orphan_closing_wrapper(self):
        request = {'tools': [{'type': 'function', 'function': {'name': 'read_file', 'parameters': {
            'properties': {'path': {'type': 'string'}}, 'required': ['path']}}}]}
        text = '<function=read_file>\n<parameter=path>\n/workspace/README.md\n</parameter>\n</function>\n</tool_call>'
        result = normalize_tools(self.response(text), request)
        call = result['choices'][0]['message']['tool_calls'][0]
        self.assertEqual(call['function']['name'], 'read_file')
        self.assertEqual(json.loads(call['function']['arguments']), {'path': '/workspace/README.md'})

    def test_narrative_and_multiple_calls_are_not_executed(self):
        call = '<function=write_file><parameter=path>x</parameter><parameter=content>hi</parameter></function>'
        for text in ('Example: ' + call + '</tool_call>', call + '\nKyle\n' + call, call + '\nextra prose'):
            result = normalize_tools(self.response(text), self.request())
            self.assertNotIn('tool_calls', result['choices'][0]['message'])

    def test_explicit_tool_selection_is_respected(self):
        request = self.request()
        request['tool_choice'] = {'type': 'function', 'function': {'name': 'read_file'}}
        result = normalize_tools(self.response('<function=write_file><parameter=path>x</parameter><parameter=content>hi</parameter>'), request)
        self.assertNotIn('tool_calls', result['choices'][0]['message'])

    def test_mixed_narrative_failure_gets_one_retry(self):
        broken = self.response('I will inspect it.\n[OUT-OF-BAND USER MESSAGE]\n<function=write_file>')
        good = self.response('<function=write_file><parameter=path>x</parameter><parameter=content>hi</parameter>')
        send = Mock(side_effect=[broken, good])
        result = completion_with_retry(dict(self.request(), messages=[{'role':'user','content':'real request'}]), send)
        self.assertEqual(send.call_count, 2)
        self.assertIn('tool_calls', result['choices'][0]['message'])
        self.assertEqual(send.call_args.args[0]['messages'][0]['content'], 'real request')
        self.assertNotIn('OUT-OF-BAND', str(send.call_args.args[0]))

    def test_retry_exhaustion_reports_failure_not_success(self):
        send = Mock(side_effect=lambda _: self.response('First some prose. <function=write_file>broken'))
        result = completion_with_retry(self.request(), send)
        self.assertEqual(send.call_count, 2)
        self.assertTrue(result['choices'][0]['message']['content'].startswith('Tool execution failed:'))
        self.assertNotIn('tool_calls', result['choices'][0]['message'])

    def test_tool_finish_without_call_is_retried_with_required_tool(self):
        broken=self.response('Successfully created it.')
        broken['choices'][0]['finish_reason']='tool_calls'
        good=self.response('<function=write_file><parameter=path>x</parameter><parameter=content>hi</parameter>')
        send=Mock(side_effect=[broken,good])
        result=completion_with_retry(self.request(),send)
        self.assertEqual(send.call_args.args[0]['tool_choice'],'required')
        self.assertIn('tool_calls',result['choices'][0]['message'])

    def test_empty_tool_finish_never_preserves_fabricated_success(self):
        def send(_):
            data=self.response('Successfully created and tested it.')
            data['choices'][0]['finish_reason']='tool_calls'
            return data
        result=completion_with_retry(self.request(),send)['choices'][0]
        self.assertEqual(result['finish_reason'],'stop')
        self.assertTrue(result['message']['content'].startswith('Tool execution failed:'))
        self.assertNotIn('Successfully',result['message']['content'])

    def test_quoted_protocol_example_is_not_an_execution_attempt(self):
        self.assertFalse(invalid_tool_protocol('Example:\n```text\n<function=read_file>\n```'))
        self.assertTrue(invalid_tool_protocol('Some prose\n<function=read_file>'))
        send = Mock(return_value=self.response('Normal explanation'))
        completion_with_retry(self.request(), send)
        self.assertEqual(send.call_count, 1)


if __name__ == '__main__':
    unittest.main()
