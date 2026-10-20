import ast
import io
import types
import unittest
from unittest.mock import Mock
from deploy.mlx_runtime_health import patch_source


def fixture():
    source='class Handler:\n    def do_GET(self):\n        if self.path.startswith("/v1/models"):\n            self.handle_models_request()\ndef main():\n    pass\n'
    for flag,value in [('--decode-concurrency','32'),('--prompt-concurrency','8'),('--prefill-step-size','2048')]:
        source+='parser.add_argument(\n        "'+flag+'",\n        type=int,\n        default='+value+',\n)\n'
    return source


class RuntimeHealthTests(unittest.TestCase):
    def test_dead_worker_models_discovery_fails_in_actual_patched_handler(self):
        tree=ast.parse(patch_source(fixture()))
        handler=next(n for n in tree.body if isinstance(n,ast.ClassDef))
        module=ast.Module(body=[handler],type_ignores=[]);namespace={};exec(compile(module,'fixture','exec'),namespace)
        instance=namespace['Handler']();instance.path='/v1/models';instance.response_generator=types.SimpleNamespace(is_healthy=False)
        instance._set_completion_headers=Mock();instance.end_headers=Mock();instance.wfile=io.BytesIO();instance.handle_models_request=Mock()
        instance.do_GET();instance._set_completion_headers.assert_called_once_with(503);instance.handle_models_request.assert_not_called()
        instance.response_generator.is_healthy=True;instance.do_GET();instance.handle_models_request.assert_called_once()

    def test_stall_health_and_request_cleanup(self):
        from unittest.mock import patch
        import threading
        source=fixture().replace('    def do_GET(self):', '    def do_POST(self):\n        raise RuntimeError("failed request")\n    def do_GET(self):')
        source+='def generation():\n    if True:\n        if True:\n            if True:\n                if True:\n                    prompt_responses, gen_responses = batch_generator.next()\n'
        tree=ast.parse(patch_source(source));handler=next(n for n in tree.body if isinstance(n,ast.ClassDef))
        clock=Mock();clock.monotonic.return_value=500
        namespace={'_oh_time':clock,'_oh_lock':threading.Lock(),'_oh_requests':{123:200}}
        exec(compile(ast.Module(body=[handler],type_ignores=[]),'fixture','exec'),namespace)
        instance=namespace['Handler']();instance.path='/v1/models';instance.response_generator=types.SimpleNamespace(is_healthy=True)
        instance._set_completion_headers=Mock();instance.end_headers=Mock();instance.wfile=io.BytesIO();instance.handle_models_request=Mock()
        instance.do_GET();instance._set_completion_headers.assert_called_once_with(503)
        self.assertIn(b'stalled',instance.wfile.getvalue())
        namespace['_oh_requests'].clear();instance._set_completion_headers.reset_mock()
        instance.do_GET();instance.handle_models_request.assert_called_once()
        with self.assertRaises(RuntimeError):instance.do_POST()
        self.assertEqual(namespace['_oh_requests'],{})
        instance.response_generator._oh_batch_started=300
        instance.do_GET();instance._set_completion_headers.assert_called_once_with(503)

    def test_patch_is_idempotent_and_rejects_unknown_layout(self):
        updated=patch_source(fixture());self.assertEqual(patch_source(updated),updated)
        self.assertIn('mx.set_cache_limit(1024**3)',updated)
        with self.assertRaises(ValueError):patch_source('unsupported')

if __name__=='__main__':unittest.main()
