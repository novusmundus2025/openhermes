import json
import unittest
from provider_tools import normalize_tools, invalid_tool_protocol, completion_with_retry
from unittest.mock import Mock


class ProviderToolTests(unittest.TestCase):
    def request(self):
        return {'tools': [{'type': 'function', 'function': {'name': 'write_file', 'parameters': {
            'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}}, 'required': ['path', 'content']}}}]}

    def response(self, text):
        return {'choices': [{'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}]}

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
