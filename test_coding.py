import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch
from coding import CodingService, evidence
from openwebui_coder import Pipe, result_message, conversation_id
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
        if body['action']=='create':
            pid=uuid.uuid4().hex;root=self.root/pid;root.mkdir()
            self.records[pid]={'id':pid,'root':str(root),'user_id':user['id']}
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
        self.assertNotEqual(first['project_id'],self.start(cid='other-chat')['project_id'])
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
