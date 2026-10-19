import json
import unittest
from agent_contracts import execution_issue, digest, evaluate_contract, swagger_contract


class AgentContractTests(unittest.TestCase):
    def setUp(self):
        self.docs = b'Existing manual API documentation.\n'
        self.contract = swagger_contract(self.docs)
        self.files = {'API.md': self.docs}
        spec = {'openapi': '3.0.3', 'paths': {path: {method: {'responses': {'200': {'description': 'OK'}}} for method in methods} for path, methods in {'/students': ['get', 'post'], '/students/{id}': ['get', 'put', 'delete']}.items()}}
        self.responses = {'/docs': {'status': 200, 'body': '<html><div id="swagger-ui"></div><script>SwaggerUIBundle({url:"/openapi.json"})</script></html>'}, '/openapi.json': {'status': 200, 'body': json.dumps(spec)}}
        self.events = [{'type': 'result', 'exit_code': 0, 'text': 'Done'}]

    def check(self):
        return evaluate_contract(self.contract, self.files, self.responses, self.events)

    def test_working_swagger_and_preserved_docs_pass(self):
        self.assertTrue(self.check()['passed'])

    def test_markdown_only_despite_success_claim_fails(self):
        self.files['API.md'] = b'Swagger documentation covers every CRUD operation'
        self.responses = {}
        self.assertFalse(self.check()['passed'])

    def test_missing_operation_fails(self):
        self.responses['/openapi.json']['body'] = '{"openapi":"3.0.3","paths":{}}'
        self.assertFalse(self.check()['passed'])

    def test_failed_write_cannot_be_masked_by_completion_claim(self):
        self.events[-1]['text'] = 'Everything works. File-mutation verifier: 1 file edit(s) FAILED'
        self.assertFalse(self.check()['passed'])

    def test_timeout_or_tool_text_is_not_completion(self):
        for events in ([], [{'type':'result','exit_code':124}], [{'type':'result','text':'<function=read_file>'}]):
            self.assertFalse(evaluate_contract(self.contract,self.files,self.responses,events)['passed'])

    def test_execution_gate_does_not_accept_unresolved_mutation(self):
        self.assertIsNotNone(execution_issue([]))
        self.assertIsNotNone(execution_issue([{'type':'result','text':'Done. File-mutation verifier: failed'}]))
        self.assertIsNone(execution_issue([{'type':'result','exit_code':0,'text':'Verified result'}]))

    def test_contract_supports_non_swagger_artifacts(self):
        contract = {'preserve': {'config.json': digest(b'keep')}, 'required_files': ['src/validation.js']}
        self.assertFalse(evaluate_contract(contract, {'config.json':b'keep'}, {}, self.events)['passed'])
        self.assertTrue(evaluate_contract(contract, {'config.json':b'keep','src/validation.js':b'code'}, {}, self.events)['passed'])


if __name__ == '__main__':
    unittest.main()
