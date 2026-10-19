import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('configure_install', Path(__file__).parent / 'deploy/configure_new_install.py')
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


class SetupTests(unittest.TestCase):
    def test_existing_settings_are_not_rotated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'settings.json').write_text('existing private settings')
            with patch.object(configure,'ROOT',root), patch('sys.argv',['configure']):
                with self.assertRaises(SystemExit):configure.main()
            self.assertEqual((root/'settings.json').read_text(),'existing private settings')

    def test_new_configuration_uses_matching_keys_and_preserves_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);home=root/'home';hermes=home/'.hermes';hermes.mkdir(parents=True)
            (root/'settings.example.json').write_text(json.dumps({'credits':{'initial_credits':0}}))
            (hermes/'config.yaml').write_text('gateway: {}\n')
            (hermes/'.env').write_text('UNRELATED=value\nAPI_SERVER_KEY=old-placeholder\n')
            with patch.object(configure,'ROOT',root), patch.object(configure.Path,'home',return_value=home), patch('sys.argv',['configure']), patch('builtins.input',side_effect=['https://example.invalid/v1','test-model']), patch('getpass.getpass',return_value='fixture-only'), patch('builtins.print'):
                configure.main()
            settings=json.loads((root/'settings.json').read_text())
            self.assertEqual(settings['model'],'test-model')
            self.assertNotEqual(settings['bridge_key'],settings['upstream_key'])
            self.assertIn('API_SERVER_KEY='+json.dumps(settings['upstream_key']),(hermes/'.env').read_text())
            self.assertIn('UNRELATED=value',(hermes/'.env').read_text())
            self.assertIn('old-placeholder',(hermes/'.env.before-openhermes').read_text())
