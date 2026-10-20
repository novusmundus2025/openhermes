import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from deploy.mac_runtime_watchdog import RecoveryGate, recover, recover_paused, generation_probe


class WatchdogTests(unittest.TestCase):
    def test_cold_start_has_time_to_load_before_network_recovery(self):
        gate=RecoveryGate()
        self.assertFalse(gate.observe('unavailable',0))
        self.assertFalse(gate.observe('unavailable',60))
        self.assertFalse(gate.observe('unavailable',299))
        for now in [300,310,320,330,340]:
            self.assertFalse(gate.observe('unavailable',now))
        self.assertTrue(gate.observe('unavailable',350))

    def test_repeated_failure_recovers_once_then_observes_cooldown(self):
        gate=RecoveryGate()
        self.assertFalse(gate.observe('dead_worker',0))
        self.assertFalse(gate.observe('dead_worker',10))
        self.assertTrue(gate.observe('dead_worker',20))
        self.assertFalse(gate.observe('dead_worker',30))
        self.assertFalse(gate.observe('dead_worker',40))
        self.assertFalse(gate.observe('dead_worker',50))
        self.assertTrue(gate.observe('dead_worker',321))

    def test_healthy_or_paused_resets_failure_streak(self):
        gate=RecoveryGate()
        gate.observe('dead_worker',0);gate.observe('dead_worker',10)
        self.assertFalse(gate.observe('dead_worker',20,paused=True))
        self.assertFalse(gate.observe('dead_worker',30))
        gate.observe('healthy',40)
        self.assertFalse(gate.observe('dead_worker',50))

    def test_manual_pause_is_never_resumed(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp)
            (home/'config.json').write_text('{"paused":true,"connected":true}')
            with patch('deploy.mac_runtime_watchdog.subprocess.run') as run:
                self.assertFalse(recover_paused(home,'test'))
                run.assert_not_called()

    def test_generation_probe_rejects_empty_answer(self):
        from unittest.mock import MagicMock
        first=MagicMock();first.__enter__.return_value.read.return_value=b'{"data":[{"id":"model"}]}'
        second=MagicMock();second.__enter__.return_value.read.return_value=b'{"choices":[{"message":{"content":""}}]}'
        with patch('deploy.mac_runtime_watchdog.urlopen',side_effect=[first,second]):
            self.assertFalse(generation_probe())

    def test_resume_requires_successful_generation(self):
        import json
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp);(home/'runtimes/mlx').mkdir(parents=True)
            (home/'config.json').write_text('{"paused":false,"connected":true}')
            (home/'persistent-runtime.json').write_text('{"pid":123}')
            expected=str(home/'runtimes/mlx/measured_startup.py')
            replies=[subprocess.CompletedProcess([],0,'501 python '+__import__('shlex').quote(expected)+' --port 8790',''),subprocess.CompletedProcess([],1,'',''),subprocess.CompletedProcess([],0,'','')]
            worker=MagicMock();worker.pid=456;worker.poll.return_value=None
            with patch('deploy.mac_runtime_watchdog.subprocess.run',side_effect=replies), patch('deploy.mac_runtime_watchdog.os.getuid',return_value=501,create=True), patch('deploy.mac_runtime_watchdog.os.kill'), patch('deploy.mac_runtime_watchdog.time.sleep'), patch('deploy.mac_runtime_watchdog.subprocess.Popen',return_value=worker), patch('deploy.mac_runtime_watchdog.health',return_value='healthy'), patch('deploy.mac_runtime_watchdog.generation_probe',return_value=True) as probe:
                self.assertTrue(recover_paused(home,'test'))
                probe.assert_called_once()
            self.assertFalse(json.loads((home/'config.json').read_text())['paused'])
            self.assertEqual(json.loads((home/'runtime-recovery.json').read_text())['state'],'generation_verified')

    def test_failed_restart_leaves_pause_set(self):
        import json
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp)
            (home/'config.json').write_text('{"paused":false,"connected":true}')
            (home/'persistent-runtime.json').write_text('{"pid":123}')
            expected=str(home/'runtimes/mlx/measured_startup.py')
            replies=[subprocess.CompletedProcess([],0,'501 python '+__import__('shlex').quote(expected)+' --port 8790',''),subprocess.CompletedProcess([],1,'',''),subprocess.CompletedProcess([],0,'','')]
            with patch('deploy.mac_runtime_watchdog.subprocess.run',side_effect=replies), patch('deploy.mac_runtime_watchdog.os.getuid',return_value=501,create=True), patch('deploy.mac_runtime_watchdog.os.kill'), patch('deploy.mac_runtime_watchdog.time.sleep'):
                with self.assertRaises(OSError):recover_paused(home,'test')
            self.assertTrue(json.loads((home/'config.json').read_text())['paused'])

    def test_recovery_never_signals_an_unrelated_process(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp);(home/'persistent-runtime.json').write_text('{"pid":123}')
            with patch('deploy.mac_runtime_watchdog.subprocess.run',return_value=subprocess.CompletedProcess([],0,'501 unrelated-app','')) as run, patch('deploy.mac_runtime_watchdog.os.kill') as kill, patch('deploy.mac_runtime_watchdog.os.getuid',return_value=501,create=True):
                with self.assertRaises(ValueError):recover(home,'gui/test/service')
                kill.assert_not_called();self.assertEqual(run.call_count,1)


if __name__=='__main__':unittest.main()
