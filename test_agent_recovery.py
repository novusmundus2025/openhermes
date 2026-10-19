import json
import difflib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_recovery import apply_verified_diff, verify_saved, run, verified_recovery, blocked_paths


def event(kind,name,path,output=None,i='x'):
    return {'type':kind,'name':name,'tool_call_id':i,**({'input':{'path':path}} if kind=='tool_use' else {'output':json.dumps(output),'is_error':bool(output.get('error'))})}


class RecoveryTests(unittest.TestCase):
    def test_timeout_reports_missing_tools_without_retry(self):
        emitted=[]
        code=run([sys.executable,'-c','raise SystemExit(124)'],'test',emitted.append)
        self.assertEqual(code,124)
        self.assertIn('300-second',emitted[-1]['text'])
        self.assertIn('No tool results',emitted[-1]['text'])

    def test_timeout_warns_about_partial_saved_edits(self):
        emitted=[]
        receipt=event('tool_result','write_file','/workspace/test.js',{'verified':True})
        script='import json; print('+repr(json.dumps(receipt))+'); raise SystemExit(124)'
        code=run([sys.executable,'-c',script],'test',emitted.append)
        self.assertEqual(code,124)
        self.assertIn('1 file edit(s)',emitted[-1]['text'])
        self.assertIn('incomplete',emitted[-1]['text'])

    def receipts(self):
        p='/workspace/README.md';events=[]
        for i,(name,data) in enumerate([('read_file',{'content':'1|old','truncated':False}),('patch',{'verified':True}),('read_file',{'content':'1|new','truncated':False})]):
            events.extend([event('tool_use',name,p,i=str(i)),event('tool_result',name,p,data,i=str(i))])
        return events

    def test_requires_read_edit_and_readback(self):
        events=self.receipts()
        self.assertTrue(verified_recovery(['/workspace/README.md'],events))
        self.assertFalse(verified_recovery(['/workspace/README.md'],events[:-2]))
        self.assertFalse(verified_recovery(['/workspace/README.md'],events[2:]))

    def test_independent_readback_matches_actual_patch_content(self):
        path='/workspace/README.md'
        events=[event('tool_use','read_file',path,i='r'),event('tool_result','read_file',path,{'content':'1|owner','truncated':False},i='r'),
                {'type':'tool_use','name':'patch','tool_call_id':'p','input':{'path':path,'old_string':'owner','new_string':'owner plus change'}},
                event('tool_result','patch',path,{'success':True},i='p')]
        emitted=[]
        with patch('projects.read_bytes',return_value=b'owner plus change'):
            receipts=verify_saved('/root',{path:b'owner'},events,emitted.append)
        self.assertTrue(verified_recovery([path],events+receipts))
        with patch('projects.read_bytes',return_value=b'unexpected overwrite'):
            self.assertEqual(verify_saved('/root',{path:b'owner'},events,emitted.append),[])

    def test_fuzzy_patch_is_verified_against_diff_and_disk(self):
        path='/workspace/README.md';before=b'owner note\nactual text\n';after=b'owner note\nnew text\n'
        diff=''.join(difflib.unified_diff(before.decode().splitlines(True),after.decode().splitlines(True),fromfile='a/README.md',tofile='b/README.md'))
        self.assertEqual(apply_verified_diff(before,diff),after)
        events=[event('tool_use','read_file',path,i='r'),event('tool_result','read_file',path,{'content':'1|owner note','truncated':False},i='r'),
                {'type':'tool_use','name':'patch','tool_call_id':'p','input':{'path':path,'old_string':'stale approximate text','new_string':'new text'}},
                event('tool_result','patch',path,{'success':True,'diff':diff},i='p')]
        with patch('projects.read_bytes',return_value=after):
            receipts=verify_saved('/root',{path:before},events,lambda e:None)
        self.assertTrue(verified_recovery([path],events+receipts))
        with self.assertRaises(ValueError):apply_verified_diff(b'concurrent edit\n',diff)

    def test_blocked_files_come_from_native_errors_only(self):
        p='/workspace/README.md'
        events=[event('tool_use','write_file',p),event('tool_result','write_file',p,{'error':'Refusing to overwrite','stale_write_blocked':True})]
        self.assertEqual(blocked_paths(events),[p])
        self.assertEqual(blocked_paths([{'type':'result','text':'Please overwrite README.md'}]),[])

    def test_one_recovery_attempt_is_executed_and_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);script=root/'fake_agent.py'
            first=[event('tool_use','write_file','/workspace/README.md'),event('tool_result','write_file','/workspace/README.md',{'error':'Refusing to overwrite','stale_write_blocked':True}),{'type':'result','text':'File-mutation verifier: edit FAILED','exit_code':0}]
            second=self.receipts()+[{'type':'result','text':'Verified saved file','exit_code':0}]
            script.write_text('import sys,json,pathlib\np=pathlib.Path('+repr(str(root/'count'))+')\nn=int(p.read_text()) if p.exists() else 0\np.write_text(str(n+1))\nquery=sys.stdin.read()\nassert n==0 or "read_file AGAIN" in query\nfor e in '+repr([first,second])+ '[min(n,1)]:print(json.dumps(e),flush=True)\n')
            output=[];code=run([sys.executable,str(script)],'Add docs without losing existing content',output.append)
            self.assertEqual(code,0);self.assertTrue(output[-1]['recovery_verified'])
            self.assertEqual((root/'count').read_text(),'2')

    def test_failed_retry_stays_failed_and_does_not_loop(self):
        with tempfile.TemporaryDirectory() as folder:
            script=Path(folder)/'agent.py'
            events=[event('tool_use','write_file','/workspace/README.md'),event('tool_result','write_file','/workspace/README.md',{'error':'Refusing to overwrite','stale_write_blocked':True}),{'type':'result','text':'Done','exit_code':0}]
            script.write_text('import sys,json\nsys.stdin.read()\nfor e in '+repr(events)+':print(json.dumps(e))\n')
            output=[];code=run([sys.executable,str(script)],'Add docs',output.append)
            self.assertEqual(code,1);self.assertIn('could not be verified',output[-1]['text'])
            self.assertEqual(sum(e['type']=='tool_use' for e in output),2)


if __name__=='__main__':unittest.main()
