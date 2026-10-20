import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from deploy.contributor_watchdog import Runtime, recover


def config(backend='vllm'):
    return {'backend':backend,'url':'http://127.0.0.1:8000','model':'test-model',
            'target':{'manager':'systemd-user','name':'inference.service'},
            'agent_target':{'manager':'systemd-user','name':'contributor.service'}}


class ContributorRecoveryTests(unittest.TestCase):
    def test_openai_backends_require_generation_not_just_health(self):
        for backend in ('vllm','llamacpp'):
            runtime=Runtime(config(backend));runtime.request=Mock(side_effect=[{}, {'choices':[{'message':{'content':'OK'}}]}])
            self.assertTrue(runtime.probe());self.assertEqual(runtime.request.call_args_list[1].args[0],'/v1/chat/completions')
            runtime.request=Mock(side_effect=[{}, {'choices':[]}])
            with self.assertRaises(IndexError):runtime.probe()

    def test_ollama_requires_installed_model_and_completed_generation(self):
        runtime=Runtime(config('ollama'));runtime.request=Mock(return_value={'models':[]})
        self.assertFalse(runtime.probe());self.assertEqual(runtime.request.call_count,1)
        runtime.request=Mock(side_effect=[{'models':[{'name':'test-model'}]}, {'done':True,'response':'OK'}])
        self.assertTrue(runtime.probe());self.assertEqual(runtime.request.call_args.args[0],'/api/generate')

    def test_invalid_endpoint_target_or_backend_rejected(self):
        for key,value in [('backend','cuda'),('url','http://remote-host:8000'),('model',''),('target',{'manager':'shell','name':'all'})]:
            candidate=config();candidate[key]=value
            with self.assertRaises(ValueError):Runtime(candidate)

    def test_docker_identity_must_match_before_any_restart(self):
        candidate=config();candidate['target']={'manager':'docker','name':'engine','container_id':'expected'}
        with patch('deploy.contributor_watchdog.subprocess.check_output',return_value='different'),patch('deploy.contributor_watchdog.subprocess.run') as run:
            with self.assertRaises(ValueError):Runtime(candidate).lifecycle('stop')
            run.assert_not_called()

    def fixture(self, root):
        path=Path(root)/'config.json';path.write_text(json.dumps({'paused':False,'connected':True}))
        return path,Path(root)/'recovery.json'

    def test_pause_stop_start_probe_then_resume_agent(self):
        with tempfile.TemporaryDirectory() as root:
            path,state=self.fixture(root);runtime=Runtime(config());actions=[]
            def lifecycle(action,target=None):
                actions.append(action)
                self.assertEqual(json.loads(path.read_text())['paused'], action!='restart')
            runtime.lifecycle=Mock(side_effect=lifecycle);runtime.probe=Mock(return_value=True)
            self.assertEqual(recover(runtime,path,state,sleep=lambda _:None),'resumed')
            self.assertEqual(actions,['stop','start','restart']);runtime.probe.assert_called_once()
            self.assertEqual(json.loads(state.read_text())['state'],'generation_verified')

    def test_restart_failure_stays_paused(self):
        with tempfile.TemporaryDirectory() as root:
            path,state=self.fixture(root);runtime=Runtime(config());runtime.lifecycle=Mock(side_effect=[None,None,RuntimeError()]);runtime.probe=Mock(return_value=True)
            with self.assertRaises(RuntimeError):recover(runtime,path,state,sleep=lambda _:None)
            self.assertTrue(json.loads(path.read_text())['paused'])

    def test_manual_pause_and_manual_changes_are_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            path,state=self.fixture(root);runtime=Runtime(config());runtime.lifecycle=Mock()
            path.write_text('{"paused":true,"connected":true}')
            self.assertEqual(recover(runtime,path,state),'manual_pause');runtime.lifecycle.assert_not_called()
            path,state=self.fixture(root)
            def change(_):path.write_text('{"paused":true,"connected":false}')
            self.assertEqual(recover(runtime,path,state,sleep=change),'manual_change')
            self.assertFalse(json.loads(path.read_text())['connected'])
            self.assertNotIn('start',[c.args[0] for c in runtime.lifecycle.call_args_list])

    def test_failed_verification_never_resumes(self):
        with tempfile.TemporaryDirectory() as root:
            path,state=self.fixture(root);runtime=Runtime(config());runtime.lifecycle=Mock();runtime.probe=Mock(return_value=False)
            with self.assertRaises(RuntimeError):recover(runtime,path,state,sleep=lambda _:None,clock=Mock(side_effect=[0,301]))
            self.assertTrue(json.loads(path.read_text())['paused']);self.assertEqual(runtime.lifecycle.call_count,2)


if __name__=='__main__':unittest.main()
