import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch
from coding import CodingService, evidence, clean_project_name, fallback_project_name, suggest_project_name
from openwebui_coder import Pipe, result_message, conversation_id, tool_summary, git_intent, progress_description
from projects import ProjectError


class FakeProjects:
    def __init__(self,root):
        self.root=Path(root);self.records={};self.jobs={};self.calls=[]
    def connect(self):return sqlite3.connect(self.root/'db.sqlite3')
    def ensure_user(self,user):return 'profile'
    def project(self,uid,pid):
        project=self.records.get(pid)
        if not project or project['user_id']!=uid:raise ProjectError('Project not found',404)
        return project
    def dispatch(self,user,body):
        self.calls.append(body)
        if body['action']=='list':return {'projects':[p for p in self.records.values() if p['user_id']==user['id']]}
        if body['action']=='create':
            pid=uuid.uuid4().hex;root=self.root/pid;root.mkdir()
            self.records[pid]={'id':pid,'root':str(root),'user_id':user['id'],'name':body['name']}
            return {'project':self.records[pid]}
        project=self.project(user['id'],body['project_id'])
        if body['action']=='new_chat':return {'chat':{'id':uuid.uuid4().hex}}
        if body['action']=='run':
            jid=uuid.uuid4().hex
            self.jobs[jid]={'id':jid,'state':'running','exit_code':None,'events':[],'prompt':body['prompt']}
            return {'job_id':jid}
        if body['action']=='jobs':return {'jobs':list(self.jobs.values())}


class CodingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.projects=FakeProjects(self.temp.name);self.service=CodingService(self.projects)
        self.read=patch('coding.read_bytes',side_effect=lambda root,path:(Path(root)/path).read_bytes());self.read.start()
    def tearDown(self):self.read.stop();self.temp.cleanup()
    def start(self,uid='alice',cid='chat-a',rid=None,prompt='Create a Node.js program'):
        return self.service.dispatch({'id':uid},{'action':'start','conversation_id':cid,'request_id':rid or uuid.uuid4().hex,'prompt':prompt})
    def test_owner_scoping_and_chat_reuse(self):
        first=self.start();second=self.start();other=self.start('bob')
        self.assertEqual(first['project_id'],second['project_id'])
        self.assertNotEqual(first['project_id'],other['project_id'])
        with self.assertRaises(ProjectError):self.service.dispatch({'id':'bob'},{'action':'status','job_id':first['job_id']})
        self.assertEqual(self.start(cid='other-chat')['state'],'choose_project')
    def test_named_project_reused_in_new_chat_and_choice_preserves_task(self):
        first=self.start(prompt='/new-project enrollment')
        same=self.start(cid='another',prompt='Add swagger to enrollment')
        self.assertEqual(first['project_id'],same['project_id'])
        choice=self.start(cid='unclear',prompt='Add swagger docs')
        self.assertEqual(choice['state'],'choose_project')
        count=len(self.projects.jobs)
        selected=self.start(cid='unclear',prompt='/use-project '+first['project_id'])
        self.assertEqual(selected['project_id'],first['project_id'])
        self.assertTrue(self.projects.jobs[selected['job_id']]['prompt'].startswith('Add swagger docs'))
        self.assertEqual(len(self.projects.jobs),count+1)
        with self.assertRaises(ProjectError):self.start(uid='bob',cid='x',prompt='/use-project '+first['project_id'])

    def test_different_app_does_not_silently_use_current_project(self):
        first=self.start(prompt='/new-project enrollment')
        choice=self.start(prompt='Build a shopping website')
        self.assertEqual(choice['state'],'choose_project')
        new=self.start(prompt='/new-project shop')
        self.assertNotEqual(first['project_id'],new['project_id'])
        self.assertTrue(self.projects.jobs[new['job_id']]['prompt'].startswith('Build a shopping website'))

    def test_project_names_are_short_and_explicit_names_preserved(self):
        result=self.start(prompt='I want to create an application full enrollment api student course subject nodejs')
        name=self.projects.records[result['project_id']]['name']
        self.assertEqual(name,'Enrollment API')
        self.assertLessEqual(len(name),48)
        self.assertEqual(self.projects.records[self.start(cid='explicit',prompt='/new-project School Portal')['project_id']]['name'],'School Portal')
        self.assertIsNone(clean_project_name('../workspace'))
        self.assertIsNone(clean_project_name('I want to create an application'))

    def test_llm_name_validated_and_provider_failure_falls_back(self):
        import io
        self.projects.settings={'model_upstream':'https://provider.invalid/v1','model':'mundusx-agnostic'}
        prompt='I want to create an application full enrollment api student course'
        with patch('usage.Tracker') as tracker, patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps({'choices':[{'message':{'content':'{"name":"Enrollment API"}'}}]}).encode())) as send:
            self.assertEqual(suggest_project_name(prompt,self.projects,{'id':'alice'}),'Enrollment API')
            self.assertEqual(send.call_args.kwargs['timeout'],15)
            tracker.return_value.finish.assert_called_once_with('completed')
        with patch('usage.Tracker'),patch('urllib.request.urlopen',side_effect=TimeoutError):
            self.assertEqual(suggest_project_name(prompt,self.projects,{'id':'alice'}),fallback_project_name(prompt))
        with patch('usage.Tracker'),patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps({'choices':[{'message':{'content':'{"name":"../../bad"}'}}]}).encode())):
            self.assertEqual(suggest_project_name(prompt,self.projects,{'id':'alice'}),fallback_project_name(prompt))

    def test_request_retry_is_idempotent(self):
        rid=uuid.uuid4().hex
        self.assertEqual(self.start(rid=rid),self.start(rid=rid))
        self.assertEqual(len(self.projects.jobs),1)
    def test_latest_status_does_not_start_another_job(self):
        task=self.start()
        result=self.service.dispatch({'id':'alice'},{'action':'latest','conversation_id':'chat-a'})
        self.assertEqual(result['job_id'],task['job_id'])
        self.assertEqual(result['state'],'running');self.assertEqual(len(self.projects.jobs),1)
    def test_workspace_lookup_is_read_only_and_owner_scoped(self):
        task=self.start()
        result=self.service.dispatch({'id':'alice'},{'action':'workspace','conversation_id':'chat-a'})
        self.assertEqual(result['project_id'],task['project_id'])
        self.assertEqual(len(self.projects.jobs),1)
        with self.assertRaises(ProjectError):self.service.dispatch({'id':'bob'},{'action':'workspace','conversation_id':'chat-a'})
    def test_fresh_native_session_preserves_files_and_blocks_active_task(self):
        task=self.start()
        with self.assertRaises(ProjectError):self.service.dispatch({'id':'alice'},{'action':'reset_session','conversation_id':'chat-a'})
        root=Path(self.projects.records[task['project_id']]['root']);(root/'keep.js').write_text('keep')
        self.projects.jobs[task['job_id']]['state']='failed'
        result=self.service.dispatch({'id':'alice'},{'action':'reset_session','conversation_id':'chat-a'})
        self.assertEqual(result['project_id'],task['project_id'])
        self.assertEqual((root/'keep.js').read_text(),'keep')
        with self.service.db() as db:chat_id=db.execute('SELECT chat_id FROM coding_conversations').fetchone()[0]
        self.assertNotEqual(chat_id,task['chat_id'])
    def test_success_prose_without_created_file_is_not_verified(self):
        task=self.start();job=self.projects.jobs[task['job_id']]
        job.update(state='completed',exit_code=0,events=[{'type':'result','text':'Successfully created and ran app.js.'}])
        result=self.service.dispatch({'id':'alice'},{'action':'status','job_id':task['job_id']})
        self.assertFalse(result['verified']);self.assertEqual(result['answer'],'')
    def test_files_and_final_result_are_observed_and_frozen(self):
        task=self.start();root=Path(self.projects.records[task['project_id']]['root'])
        (root/'app.js').write_text('console.log("hello")')
        job=self.projects.jobs[task['job_id']]
        job.update(state='completed',exit_code=0,events=[{'type':'result','text':'Created app.js.'}])
        result=self.service.dispatch({'id':'alice'},{'action':'status','job_id':task['job_id']})
        self.assertTrue(result['verified']);self.assertEqual(result['files'][0]['path'],'app.js')
        (root/'later.js').write_text('another task')
        self.assertEqual(result,self.service.dispatch({'id':'alice'},{'action':'status','job_id':task['job_id']}))
    def test_failed_tool_call_hides_unverified_success_claim(self):
        job={'state':'completed','exit_code':0,'events':[{'type':'result','text':'Created script. Request failed; verification is incomplete.'}]}
        result=evidence(job)
        self.assertFalse(result['verified']);self.assertEqual(result['answer'],'')


class CoderFunctionTests(unittest.TestCase):
    def test_progress_reports_waiting_and_actual_tool_activity(self):
        self.assertIn('Waiting for model response',progress_description([],72,72))
        self.assertIn('72s elapsed',progress_description([],72,72))
        events=[{'type':'tool_use','tool_call_id':'a','name':'terminal','input':{'command':'npm test'}}]
        self.assertIn('Running command: npm test',progress_description(events,80,8))
        events.append({'type':'tool_result','tool_call_id':'a','name':'terminal','output':'{"exit_code":1}'})
        value=progress_description(events,82,0)
        self.assertIn('1 tool results',value);self.assertIn('1 failed',value)
        self.assertIn('next step',value)

    def test_missing_provider_dependency_explains_failure(self):
        value=evidence({'state':'failed','exit_code':1,'events':[{'type':'result','exit_code':1,'error':'HTTP 424: Provider tool-calling backend is unavailable internal HTTP 404'}]})
        self.assertFalse(value['verified']);self.assertIn('HTTP 404',value['issue'])
        self.assertNotIn('Agent reported an execution failure',value['issue'])

    def test_partial_outcome_is_readable_without_html_or_server_path_dump(self):
        result=result_message({'project_id':'p','verified':False,'issue':'Execution failed',
             'workspace':'/private/account/workspace','files':[{'path':'server.js','changed':True}],
             'commands':[{'command':'cd /workspace && npm test','exit_code':1,'output':'A check failed'}]})
        self.assertIn('Changes saved · checks incomplete',result)
        self.assertIn('Not verified',result)
        self.assertIn('server.js',result)
        self.assertNotIn('<details',result);self.assertNotIn('<summary',result)
        self.assertNotIn('/private/account/workspace',result)
        self.assertIn('cd /workspace && npm test',result)
        self.assertNotIn('&amp;',result)
    def test_workspace_location_is_explicit_not_lost(self):
        result=result_message({'project_id':'p','state':'workspace','workspace':'/actual/user/project'},show_workspace=True)
        self.assertIn('/actual/user/project',result)
        self.assertNotIn('<details',result)
    def test_install_output_is_not_mislabeled_as_verification_or_dumped_in_chat(self):
        result=result_message({'project_id':'p','verified':False,'commands':[{'command':'cd /workspace && npm install','exit_code':0,'output':'npm warning '+('noisy details '*1000)}]})
        self.assertIn('Latest command',result);self.assertNotIn('Latest check',result)
        self.assertNotIn('noisy details',result);self.assertIn('diagnostics',result)
    def test_compact_tool_status_and_existing_secure_git_routing(self):
        event={'type':'tool_result','name':'terminal','output':'{"exit_code":0}'}
        call={'input':{'command':'cd /workspace && npm install'}}
        text=tool_summary(event,call)
        self.assertIn('&&',text);self.assertNotIn('&amp;',text);self.assertNotIn('<summary',text)
        self.assertEqual(git_intent('git push origin main')['operation'],'push')
        self.assertIsNone(git_intent('Explain git branching'))
    def test_verified_file_links_and_real_command_output_in_chat(self):
        text=result_message({'project_id':'abc','verified':True,'answer':'Done','workspace':'/private/user/project',
             'files':[{'path':'hello world.js','changed':True}],
             'commands':[{'command':'node app.js','exit_code':0,'output':'Alice\n*'}]})
        self.assertIn('/project-file/?project_id=abc&path=hello%20world.js',text)
        self.assertIn('Alice',text);self.assertIn('Exit code: 0',text)
    def test_failure_never_presents_agent_success_prose(self):
        text=result_message({'project_id':'abc','verified':False,'answer':'Successfully created everything!','issue':'Tool failure'})
        self.assertIn('Not verified',text);self.assertNotIn('Successfully',text)
    def test_background_tasks_have_no_filesystem_or_browser_side_effects(self):
        async def run():
            with patch('pathlib.Path.read_text',side_effect=AssertionError('Configuration touched')):
                return [item async for item in Pipe().pipe({}, {}, __task__='title_generation')]
        self.assertEqual(asyncio.run(run()),[''])
    def test_metadata_conversation_id_wins_over_body(self):
        self.assertEqual(conversation_id({'chat_id':'body'},{'chat_id':'injected'}),'injected')
