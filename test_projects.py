import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
import subprocess
from unittest.mock import patch
from projects import ProjectService, ProjectError, read_bytes, save_file, tree, private_write


class ProjectBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / 'project'
        self.work.mkdir()
        self.dbfile = self.root / 'test.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE users (user_id TEXT PRIMARY KEY)')
            db.executemany('INSERT INTO users VALUES (?)', [('alice',), ('bob',)])
        self.service = ProjectService(self.connect, lambda user: 'test-profile', {}, self.root)
        with self.connect() as db:
            db.execute('INSERT INTO projects VALUES (?,?,?,?,?,?,?)',
                       ('project-a', 'alice', 'test-profile', 'Test', 'test', str(self.work), 1))

    def connect(self):
        db = sqlite3.connect(self.dbfile, timeout=10)
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def tearDown(self):
        self.temp.cleanup()

    def test_project_delete_owner_and_running_guard(self):
        base = {'action':'delete_project','project_id':'project-a'}
        with self.assertRaises(ProjectError):
            self.service.dispatch({'id':'bob'}, base)
        (self.work / 'keep.txt').write_text('keep')
        with self.connect() as db:
            db.execute("INSERT INTO project_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)", ('busy','project-a','agent','x','running','','[]',None,1,1,'legacy'))
        with self.assertRaises(ProjectError):
            self.service.dispatch({'id':'alice'}, base)
        with self.connect() as db:
            db.execute("UPDATE project_jobs SET state='completed'")
        with patch.object(self.service, 'cleanup_project') as cleanup:
            self.assertTrue(self.service.dispatch({'id':'alice'}, base)['deleted'])
            cleanup.assert_called_once()
        self.assertEqual(self.service.dispatch({'id':'alice'}, {'action':'list'})['projects'], [])
        with self.assertRaises(ProjectError):
            self.service.dispatch({'id':'alice'}, {'action':'tree','project_id':'project-a'})
        self.assertEqual((self.work / 'keep.txt').read_text(), 'keep')

    def test_chat_history_is_separate_and_owner_scoped(self):
        user = {'id': 'alice'}
        base = {'project_id': 'project-a'}
        old = self.service.dispatch(user, dict(base, action='chats'))['chats'][0]
        new = self.service.dispatch(user, dict(base, action='new_chat'))['chat']
        self.assertNotEqual(old['id'], new['id'])
        self.service.dispatch(user, dict(base, action='rename_chat', chat_id=new['id'], title='Named conversation'))
        titles = [c['title'] for c in self.service.dispatch(user, dict(base, action='chats'))['chats']]
        self.assertIn('Named conversation', titles)
        with self.assertRaises(ProjectError):
            self.service.dispatch(user, dict(base, action='rename_chat', chat_id='other-project-chat', title='No'))
        with self.assertRaises(ProjectError):
            self.service.dispatch(user, dict(base, action='rename_chat', chat_id=new['id'], title=''))
        with self.connect() as db:
            db.execute("INSERT INTO project_jobs (id,project_id,kind,prompt,state,output,events,exit_code,created_at,updated_at,chat_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)", ('job-a','project-a','agent','old','completed','','[]',0,1,1,old['id']))
        self.assertEqual(len(self.service.dispatch(user, dict(base, action='jobs', chat_id=old['id']))['jobs']), 1)
        self.assertEqual(self.service.dispatch(user, dict(base, action='jobs', chat_id=new['id']))['jobs'], [])
        for action in ('chats','new_chat','jobs'):
            with self.assertRaises(ProjectError):
                self.service.dispatch({'id':'bob'}, dict(base, action=action, chat_id=new['id']))
        with self.assertRaises(ProjectError):
            self.service.dispatch(user, dict(base, action='jobs', chat_id='other-project-chat'))

    def test_remove_chat_is_recoverable_and_owner_scoped(self):
        user = {'id':'alice'}
        base = {'project_id':'project-a'}
        chat = self.service.dispatch(user,dict(base,action='new_chat'))['chat']['id']
        self.service.dispatch(user,dict(base,action='remove_chat',chat_id=chat))
        self.assertNotIn(chat,[c['id'] for c in self.service.dispatch(user,dict(base,action='chats'))['chats']])
        with self.assertRaises(ProjectError):
            self.service.dispatch(user,dict(base,action='jobs',chat_id=chat))
        for action in ('remove_chat','restore_chat'):
            with self.assertRaises(ProjectError):
                self.service.dispatch({'id':'bob'},dict(base,action=action,chat_id=chat))
        self.service.dispatch(user,dict(base,action='restore_chat',chat_id=chat))
        self.assertIn(chat,[c['id'] for c in self.service.dispatch(user,dict(base,action='chats'))['chats']])
        self.assertTrue(self.work.exists())

    def test_other_user_cannot_read_run_or_preview(self):
        for action in ('read', 'run', 'preview', 'tree', 'jobs', 'changes'):
            with self.subTest(action=action), self.assertRaises(ProjectError) as caught:
                self.service.dispatch({'id': 'bob'}, {'action': action, 'project_id': 'project-a', 'path': 'index.html'})
            self.assertEqual(caught.exception.status, 404)

    def test_symlink_cannot_escape_project(self):
        secret = self.root / 'secret.txt'
        secret.write_text('private')
        (self.work / 'link').symlink_to(secret)
        (self.work / 'directory').symlink_to(self.root, target_is_directory=True)
        for path in ('link', 'directory/secret.txt'):
            with self.assertRaises((OSError, ProjectError)):
                read_bytes(self.work, path)
        self.assertEqual(tree(self.work), [])

    def test_runtime_configuration_cannot_follow_container_symlinks(self):
        secret = self.root / 'host-secret'
        secret.write_text('keep private')
        (self.work / 'config.yaml').symlink_to(secret)
        private_write(self.work, 'config.yaml', 'project settings')
        self.assertEqual(secret.read_text(), 'keep private')
        self.assertFalse((self.work / 'config.yaml').is_symlink())
        self.assertEqual((self.work / 'config.yaml').read_text(), 'project settings')

    def test_repository_credentials_and_non_https_urls_rejected(self):
        for url in ('file:///etc', 'ssh://server/path', 'https://user:secret@example.com/repo'):
            with self.assertRaises(ProjectError):
                self.service.create({'id': 'alice'}, 'test-profile', 'Test', url)

    def test_traversal_and_private_config_rejected(self):
        for path in ('../secret.txt', '/etc/passwd', 'a/../../b', 'a\\b', '.env', '.git/config'):
            with self.subTest(path=path), self.assertRaises(ProjectError):
                read_bytes(self.work, path)

    def test_editor_save_does_not_overwrite_new_agent_change(self):
        target = self.work / 'index.html'
        target.write_text('original')
        previous = hashlib.sha256(b'original').hexdigest()
        target.write_text('agent updated it')
        with self.assertRaises(ProjectError) as caught:
            save_file(self.work, 'index.html', 'stale editor text', previous)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(target.read_text(), 'agent updated it')

    def test_create_and_edit_round_trip(self):
        first = save_file(self.work, 'main.py', 'print(1)\n', None)
        second = save_file(self.work, 'main.py', 'print(2)\n', first)
        self.assertEqual(read_bytes(self.work, 'main.py'), b'print(2)\n')
        self.assertNotEqual(first, second)

    def test_only_one_running_task_per_project(self):
        p = self.service.project('alice', 'project-a')
        with patch.object(self.service, 'worker', return_value=None):
            self.service.job(p, 'command', 'pwd')
            with self.assertRaises(ProjectError) as caught:
                self.service.job(p, 'agent', 'change files')
        self.assertEqual(caught.exception.status, 409)

    def test_existing_container_refreshes_execution_budget(self):
        import json,yaml
        project=self.service.project('alice','project-a')
        runtime=self.root/'project-runtime/project-a';runtime.mkdir(parents=True)
        (runtime/'config.yaml').write_text('agent:\n  run_budget_seconds: 240\nmodel:\n  default: fixture-model\n')
        inspected=[{'Config':{'Labels':{'org.openhermes.project':'project-a'}},'State':{'Running':True}}]
        with patch.object(self.service,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(inspected),'')):
            self.service.container(project)
        config=yaml.safe_load((runtime/'config.yaml').read_text())
        self.assertEqual(config['agent']['run_budget_seconds'],600)
        self.assertEqual(config['model']['default'],'fixture-model')
        self.assertEqual(config['model']['context_length'],65536)

    def test_git_inspection_runs_inside_container(self):
        with patch.object(self.service, 'container', return_value='isolated-project'), patch.object(
            self.service, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            self.service.dispatch({'id': 'alice'}, {'action': 'changes', 'project_id': 'project-a'})
        self.assertEqual(len(run.call_args_list), 2)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:3], ['docker', 'exec', 'isolated-project'])


if __name__ == '__main__':
    unittest.main()


class ProjectCleanupTests(unittest.TestCase):
    def test_local_cleanup_preserves_remote_and_other_profile_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            home=Path(temporary);pid='a'*32;profile='owui-'+('b'*32)
            root=home/'.hermes/profiles'/profile/'workspace/projects/example'
            root.mkdir(parents=True);(root/'.git').mkdir();(root/'.git/config').write_text('remote untouched')
            other=root.parent/'other';other.mkdir();(other/'keep').write_text('keep')
            state=home/'state';runtime=state/'project-runtime'/pid;runtime.mkdir(parents=True)
            shared=home/'shared';shared.mkdir();(shared/'keep').write_text('keep')
            (runtime/'installs').symlink_to(shared,target_is_directory=True)
            service=object.__new__(ProjectService);service.state=state
            import threading
            service.container_lock=threading.Lock()
            service.run=lambda args,**kw: subprocess.CompletedProcess(args,1,'','No such container')
            project={'id':pid,'profile':profile,'slug':'example','root':str(root)}
            with patch('projects.Path.home',return_value=home):
                service.run=lambda args,**kw: subprocess.CompletedProcess(args,0,'[{"Config":{"Labels":{"org.openhermes.project":"someone-else"}}}]','')
                with self.assertRaises(ProjectError):service.cleanup_project(project)
                self.assertTrue(root.exists())
                service.run=lambda args,**kw: subprocess.CompletedProcess(args,1,'','daemon unavailable')
                with self.assertRaises(ProjectError):service.cleanup_project(project)
                self.assertTrue(root.exists())
                service.run=lambda args,**kw: subprocess.CompletedProcess(args,1,'','No such container')
                service.cleanup_project(project)
                self.assertFalse(root.exists());self.assertFalse(runtime.exists())
                self.assertEqual((other/'keep').read_text(),'keep')
                self.assertEqual((shared/'keep').read_text(),'keep')
                self.assertTrue(root.parents[2].exists())
                project['root']=str(other)
                with self.assertRaises(ProjectError):service.cleanup_project(project)
