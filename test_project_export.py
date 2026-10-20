import base64
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from project_export import export_project, excluded
from projects import ProjectError


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.git('init','--quiet');self.project={'root':str(self.root),'name':'My app'}
    def tearDown(self):self.temp.cleanup()
    def git(self,*args):return subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True)
    def write(self,name,text):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')
    def entries(self):
        data=export_project(self.project)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(data['data']))) as z:return {n:z.read(n) for n in z.namelist()}
    def test_current_worktree_and_ignore_rules_including_tracked_ignored_files(self):
        self.write('app.py','old');self.git('add','app.py');self.git('-c','user.name=Tests','-c','user.email=tests@localhost','commit','--quiet','-m','baseline')
        self.write('app.py','new');self.write('new.txt','untracked');self.write('.gitignore','ignored.txt\n*.tmp\n!keep.tmp\ntracked-ignore.txt\n')
        for name in ['ignored.txt','skip.tmp','keep.tmp','tracked-ignore.txt']:self.write(name,name)
        self.git('add','-f','tracked-ignore.txt');self.write('gone.txt','gone');self.git('add','gone.txt');(self.root/'gone.txt').unlink()
        data=self.entries();self.assertEqual(data['My-app/app.py'],b'new');self.assertIn('My-app/new.txt',data);self.assertIn('My-app/keep.tmp',data)
        for name in ['ignored.txt','skip.tmp','tracked-ignore.txt','gone.txt']:self.assertNotIn('My-app/'+name,data)
    def test_credentials_runtime_and_symlinks_excluded_templates_included(self):
        for name in ['.env','.env.production','key.pem','credentials.json','.hermes-project/PLAN.md','node_modules/pkg/index.js','.ssh/config']:
            self.write(name,'private')
        self.write('.env.example','TOKEN=replace-me');self.write('README.md','docs')
        with tempfile.TemporaryDirectory() as outside:
            target=Path(outside)/'secret';target.write_text('outside')
            (self.root/'linked.txt').symlink_to(target)
            (self.root/'linked-dir').symlink_to(outside,target_is_directory=True)
            data=self.entries()
        self.assertEqual(set(data),{'My-app/.env.example','My-app/README.md'})
    def test_nested_gitignore_and_binary_assets(self):
        self.write('src/.gitignore','*.log\n');self.write('src/debug.log','no');self.write('src/code.js','yes');(self.root/'image.bin').write_bytes(b'\x00\xff')
        data=self.entries();self.assertNotIn('My-app/src/debug.log',data);self.assertEqual(data['My-app/image.bin'],b'\x00\xff')
    def test_size_limit_fails_instead_of_silently_truncating(self):
        self.write('large.txt','abcdef')
        with patch('project_export.MAX_SINGLE',3):
            with self.assertRaises(ProjectError):self.entries()
        with patch('project_export.MAX_TOTAL',3):
            with self.assertRaises(ProjectError):self.entries()
    def test_not_a_repository_fails(self):
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaises(ProjectError):export_project({'root':other,'name':'x'})


if __name__=='__main__':unittest.main()
