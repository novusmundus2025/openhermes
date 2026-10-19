import tempfile
import threading
import time
import unittest
from pathlib import Path
from local_browser import LocalBrowserBroker
from projects import ProjectError


class LocalBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.broker = LocalBrowserBroker(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def pair(self, uid):
        code = self.broker.user_action(uid, {'action':'local_pair'})['pair_code']
        return self.broker.device({'action':'pair','code':code})['token']

    def test_codes_are_single_use_expiring_and_no_plaintext_token_on_disk(self):
        code = self.broker.user_action('alice', {'action':'local_pair'})['pair_code']
        token = self.broker.device({'action':'pair','code':code})['token']
        with self.assertRaises(ProjectError):
            self.broker.device({'action':'pair','code':code})
        self.assertNotIn(token.encode(), self.broker.path.read_bytes())
        code = self.broker.user_action('bob', {'action':'local_pair'})['pair_code']
        self.broker.codes[self.broker.digest(code)] = ('bob',0)
        with self.assertRaises(ProjectError):
            self.broker.device({'action':'pair','code':code})

    def test_cross_user_command_result_and_revocation(self):
        alice, bob = self.pair('alice'), self.pair('bob')
        result = []
        thread = threading.Thread(target=lambda:result.append(self.broker.request('alice',{'action':'refresh'})))
        thread.start()
        deadline = time.monotonic()+2
        task = {'id':None}
        while not task['id'] and time.monotonic()<deadline:
            task = self.broker.device({'action':'poll','token':alice})
        self.assertIsNone(self.broker.device({'action':'poll','token':bob})['id'])
        with self.assertRaises(ProjectError):
            self.broker.device({'action':'result','token':bob,'id':task['id'],'result':{'success':True}})
        self.broker.device({'action':'result','token':alice,'id':task['id'],'result':{'success':True}})
        thread.join(2)
        self.assertEqual(result,[{'success':True}])
        with self.assertRaises(ProjectError):
            self.broker.device({'action':'result','token':alice,'id':task['id'],'result':{'success':True}})
        self.broker.user_action('alice',{'action':'local_revoke'})
        with self.assertRaises(ProjectError):
            self.broker.device({'action':'poll','token':alice})
        self.assertTrue(self.broker.settings('bob')['paired'])

    def test_pairing_cannot_replace_active_browser(self):
        code = self.broker.user_action('alice', {'action':'local_pair'})['pair_code']
        self.broker.active = lambda uid: True
        with self.assertRaises(ProjectError):
            self.broker.device({'action':'pair','code':code})

    def test_selection_persists_and_offline_does_not_fall_back(self):
        self.pair('alice')
        self.assertEqual(LocalBrowserBroker(Path(self.temp.name)).settings('alice')['transport'],'local')
        self.broker.online.clear()
        with self.assertRaises(ProjectError):
            self.broker.request('alice',{'action':'navigate','url':'https://example.com'})
        self.broker.user_action('alice',{'action':'local_select','transport':'server'})
        self.assertEqual(self.broker.settings('alice')['transport'],'server')


if __name__ == '__main__':
    unittest.main()
