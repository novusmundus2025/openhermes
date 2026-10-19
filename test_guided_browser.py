import tempfile
import unittest
from unittest.mock import patch
from guided_browser import BrowserService, public_url
from projects import ProjectError


class FakeWorker:
    def __init__(self, home):
        self.closed=False
        self.calls=[]
    def request(self, body):
        self.calls.append(body)
        return {'success':True,'url':'https://example.com','snapshot':'- link "More" [ref=e1]', 'image':None}
    def close(self):self.closed=True


class GuidedBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        with patch('guided_browser.threading.Thread'):
            self.service=BrowserService(lambda user:'profile', self.temp.name, FakeWorker)
        self.public=patch('guided_browser.public_url',side_effect=lambda url:url)
        self.public.start()
    def tearDown(self):
        self.public.stop();self.temp.cleanup()
    def test_owner_scoping_and_stale_views(self):
        a={'id':'alice'};b={'id':'bob'}
        start=self.service.dispatch(a,{'action':'start'})
        base={'session_id':start['session_id']}
        with self.assertRaises(ProjectError):self.service.dispatch(b,dict(base,action='refresh'))
        view=self.service.dispatch(a,dict(base,action='navigate',url='https://example.com'))
        with self.assertRaises(ProjectError):self.service.dispatch(a,dict(base,action='click',view=0,ref='e1'))
        with self.assertRaises(ProjectError):self.service.dispatch(a,dict(base,action='click',view=view['view'],ref='e999'))
        self.service.dispatch(a,dict(base,action='click',view=view['view'],ref='e1'))
        worker=self.service.sessions['alice']['worker']
        self.service.dispatch(a,dict(base,action='stop'))
        self.assertTrue(worker.closed)
        self.assertFalse(self.service.dispatch(a,{'action':'status'})['active'])
    def test_no_navigation_before_start_and_no_arbitrary_commands(self):
        user={'id':'alice'}
        with self.assertRaises(ProjectError):self.service.dispatch(user,{'action':'navigate','url':'https://example.com'})
        with self.assertRaises(ProjectError):self.service.dispatch(user,{'action':'eval','expression':'anything'})
    def test_new_start_is_idempotent_per_user(self):
        first=self.service.dispatch({'id':'alice'},{'action':'start'})
        self.assertEqual(first,self.service.dispatch({'id':'alice'},{'action':'start'}))
    def test_capacity_and_separate_runtime_per_user(self):
        ids=[]
        for i in range(4):
            ids.append(self.service.dispatch({'id':str(i)},{'action':'start'})['session_id'])
        self.assertEqual(len(set(ids)),4)
        with self.assertRaises(ProjectError) as caught:
            self.service.dispatch({'id':'fifth'},{'action':'start'})
        self.assertEqual(caught.exception.status,429)


class URLTests(unittest.TestCase):
    def test_private_and_credential_urls_rejected(self):
        for url in ['file:///etc/passwd','http://user:pass@example.com','http://example.com:8643','http://example.com:invalid']:
            with self.assertRaises(ProjectError):public_url(url)
        with patch('guided_browser.socket.getaddrinfo',return_value=[(None,None,None,None,('127.0.0.1',80))]):
            with self.assertRaises(ProjectError):public_url('http://internal.example')
        with patch('guided_browser.socket.getaddrinfo',return_value=[(None,None,None,None,('93.184.216.34',443))]):
            self.assertEqual(public_url('https://example.com'),'https://example.com')
