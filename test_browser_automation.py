import asyncio
import tempfile
import threading
import unittest
from unittest.mock import patch
from browser_automation import BrowserAutomation, model_snapshot, login_required, safe_step
from guided_browser import BrowserService
from projects import ProjectError


class Worker:
    def __init__(self,home):
        self.calls=[]
        self.frame={'success':True,'url':'https://example.com/','snapshot':'- link "More" [ref=e1]\n- searchbox "Search" [ref=e2]'}
    def request(self,body):
        self.calls.append(body)
        return dict(self.frame)
    def close(self):pass


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        with patch('guided_browser.threading.Thread'):
            self.browser=BrowserService(lambda user:'profile',self.temp.name,Worker)
        self.public=patch('guided_browser.public_url',side_effect=lambda value:value);self.public.start()
        self.user={'id':'alice'}
        self.start=self.browser.dispatch(self.user,{'action':'start'})
        self.sid=self.start['session_id']
        self.browser.dispatch(self.user,{'action':'navigate','session_id':self.sid,'url':'https://example.com'})
    def tearDown(self):
        self.public.stop();self.temp.cleanup()
    def engine(self,planner):
        self.auto=BrowserAutomation(self.browser,{},planner=planner)
        self.browser.automation=self.auto
        return self.auto
    def launch(self):
        self.auto.start(self.user,{'session_id':self.sid,'goal':'Find information','max_steps':3})
        return self.auto.runs['alice']
    def test_bounded_search_run_finishes_and_returns_human_control(self):
        plans=iter([{'action':'type','ref':'e2','text':'test query'},{'action':'finish','summary':'Visible information found.'}])
        self.engine(lambda *args:next(plans))
        run=self.launch();run['thread'].join(2)
        self.assertEqual(run['state'],'completed');self.assertEqual(run['steps'],1)
        self.assertEqual(self.auto.status(self.user)['control'],'human')
        self.assertNotIn('test query',str(run['history']))
    def test_login_handoff_never_calls_model(self):
        self.browser.sessions['alice']['frame']['snapshot']='- textbox "Password" [ref=e3]'
        self.engine(lambda *args:self.fail('Login page must not be sent to model'))
        run=self.launch();run['thread'].join(2)
        self.assertEqual(run['state'],'human_required')
        self.assertEqual(self.auto.status(self.user)['control'],'human')
    def test_cardmarket_block_stops_without_model_or_inbox_claim(self):
        self.browser.sessions['alice']['frame'].update(title='Attention Required! | Cloudflare',
            snapshot='- heading "Sorry, you have been blocked" [level=1, ref=e1]\n- heading "You are unable to access cardmarket.com" [level=2, ref=e2]')
        self.engine(lambda *args:self.fail('Blocked page must not be sent to model'))
        run=self.launch();run['thread'].join(2)
        self.assertEqual(run['state'],'site_blocked');self.assertEqual(run['steps'],0)
        self.assertIn('could not proceed',run['summary'])
        self.assertEqual(self.auto.status(self.user)['control'],'human')

    def test_iframe_only_page_refreshes_once_without_inference(self):
        self.browser.sessions['alice']['frame']['snapshot']='- Iframe [ref=e1]'
        self.browser.sessions['alice']['worker'].frame['snapshot']='- Iframe [ref=e1]'
        self.engine(lambda *args:self.fail('Unreadable page must not be sent to the model'))
        run=self.launch();run['thread'].join(2)
        self.assertEqual(run['state'],'human_required');self.assertEqual(run['steps'],1)
        self.assertIn('could not be verified',run['summary'])

    def test_pause_discards_pending_model_result_and_allows_human_fill(self):
        entered=threading.Event();release=threading.Event()
        def planner(*args):
            entered.set();release.wait(3);return {'action':'click','ref':'e1'}
        self.engine(planner);run=self.launch();self.assertTrue(entered.wait(1))
        with self.assertRaises(ProjectError):
            self.browser.dispatch(self.user,{'action':'click','session_id':self.sid,'view':1,'ref':'e1'})
        self.auto.pause(self.user)
        self.browser.dispatch(self.user,{'action':'type','session_id':self.sid,'view':1,'ref':'e2','text':'human-only-value'})
        release.set();run['thread'].join(2)
        self.assertEqual([c['action'] for c in self.browser.sessions['alice']['worker'].calls],['navigate','type'])
        self.assertEqual(run['state'],'paused')
    def test_owner_cannot_start_another_users_browser(self):
        self.engine(lambda *args:{'action':'finish'})
        with self.assertRaises(ProjectError):
            self.auto.start({'id':'bob'},{'session_id':self.sid,'goal':'read'})
    def test_step_limit_stops_repeated_actions(self):
        self.engine(lambda *args:{'action':'scroll','direction':'down'})
        run=self.launch();run['thread'].join(2)
        self.assertEqual(run['steps'],3);self.assertEqual(run['state'],'budget_reached')


class PlannerBoundaryTests(unittest.TestCase):
    def test_german_store_search_is_allowed_but_purchase_and_login_are_not(self):
        frame={'snapshot':'- searchbox "Wonach suchst du?" [ref=e78]\n- button "Suche abschicken" [ref=e105]\n- button "In den Warenkorb" [ref=e5]\n- textbox "Passwort" [ref=e6]'}
        self.assertEqual(safe_step({'action':'type','ref':'e78','text':'GX10'},frame)['action'],'type')
        self.assertEqual(safe_step({'action':'click','ref':'e105'},frame)['action'],'click')
        self.assertEqual(safe_step({'action':'click','ref':'e5'},frame)['action'],'human')
        self.assertEqual(safe_step({'action':'type','ref':'e6','text':'secret'},frame)['action'],'human')
        self.assertTrue(login_required(frame))
    def test_provider_batch_uses_first_action_only(self):
        import io,json,time
        engine=BrowserAutomation(None,{'model_upstream':'https://provider.example/v1'})
        calls=[{'function':{'name':'browser_step','arguments':json.dumps(plan)}} for plan in
               ({'action':'type','ref':'e1','text':'AI news'},{'action':'click','ref':'e2'})]
        response={'choices':[{'message':{'tool_calls':calls}}]}
        with patch('browser_automation.urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as send:
            plan=engine.plan({'id':'alice'},{'started':time.monotonic(),'cancel':threading.Event(),'goal':'Find news','history':[]},
                {'url':'https://www.google.com/','snapshot':'- searchbox "Search" [ref=e1]\n- button "Search" [ref=e2]'})
        self.assertEqual(plan,{'action':'type','ref':'e1','text':'AI news'})
        self.assertFalse(json.loads(send.call_args[0][0].data)['parallel_tool_calls'])

    def test_planner_supports_upstream_without_key_and_includes_title(self):
        import io,json,time
        engine=BrowserAutomation(None,{'model_upstream':'https://provider.example/v1'})
        response={'choices':[{'message':{'tool_calls':[{'function':{'name':'browser_step','arguments':json.dumps({'action':'finish','summary':'Example Domain'})}}]}}]}
        with patch('browser_automation.urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as send:
            plan=engine.plan({'id':'alice'},{'started':time.monotonic(),'cancel':threading.Event(),'goal':'Read heading','history':[]},
                {'url':'https://example.com/','title':'Example Domain','snapshot':'- heading "Example Domain" [level=1]'})
        self.assertEqual(plan['action'],'finish')
        request=send.call_args[0][0]
        self.assertNotIn('Authorization',request.headers)
        self.assertIn('Example Domain',request.data.decode())
        from datetime import datetime
        context=json.loads(json.loads(request.data)['messages'][1]['content'])
        self.assertIsNotNone(datetime.fromisoformat(context['current_time']).tzinfo)

    def test_input_values_removed_and_write_actions_handed_off(self):
        text='- textbox "Email" [ref=e1]: private-value\n- textbox "Password" [ref=e2]: secret-value'
        result=model_snapshot(text)
        self.assertNotIn('private-value',result);self.assertNotIn('secret-value',result)
        self.assertNotIn('nested-value',model_snapshot('- textbox "Search" [ref=e1]\n  - text: nested-value\n- link "More" [ref=e2]'))
        self.assertTrue(login_required({'url':'https://example.com/login','snapshot':''}))
        self.assertEqual(safe_step({'action':'click','ref':'e1'},{'snapshot':'- button "Delete message" [ref=e1]'})['action'],'human')
        self.assertEqual(safe_step({'action':'type','ref':'e1','text':'anything'},{'snapshot':'- textbox "Password" [ref=e1]'})['action'],'human')
