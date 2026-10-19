"""Persistent project guidance and evidence-based task records."""
import json
import os
import time


TEMPLATES = {
    'AGENTS.md': '# Project instructions\n\nRead PLAN.md, CHECKPOINT.md and recent TASK-*.md before resuming.\nInspect current code before editing. Preserve unrelated changes.\nUpdate PLAN.md with steps and progress, and CHECKPOINT.md with verified results,\nchecks, blockers and next steps. Never store passwords or tokens here.\nA saved edit or Git commit does not prove correctness.\n',
    'PLAN.md': '# Plan\n\nNo plan recorded yet. Hermes should record the objective and actionable steps\nbefore substantial work, and mark progress based on evidence.\n',
    'CHECKPOINT.md': '# Checkpoint\n\nNo verified project summary recorded yet. See TASK-*.md for bridge-recorded\ntask outcomes, including failures. Hermes should update this summary after work.\n',
}


def create_once(root, name, content):
    from projects import open_parent
    parent, leaf = open_parent(root, name)
    try:
        try:
            fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent)
        except FileExistsError:
            return False
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            file.write(content)
        return True
    finally:
        os.close(parent)


def initialize(root):
    for name, text in TEMPLATES.items():
        create_once(root, name, text)


def record(root, jid, code, events):
    edits = []
    checks = []
    for event in events:
        if event.get('type') != 'tool_result':
            continue
        try:
            data = json.loads(event.get('output', '{}'))
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        if event.get('name') in ('write_file', 'patch') and not event.get('is_error') and (data.get('verified') or data.get('success')):
            edits.extend(p for p in data.get('files_modified', []) if isinstance(p, str))
            if data.get('resolved_path'):
                edits.append(data['resolved_path'])
            if data.get('lint', {}).get('status') == 'error' or data.get('lsp_diagnostics'):
                checks.append('Edit tool reported diagnostics; inspect the execution log.')
    state = 'Completed execution (correctness not certified)' if code == 0 else 'Incomplete / failed execution'
    text = '# Task checkpoint\n\nTask: '+jid+'\n\nRecorded UTC: '+time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime())+'\n\nOutcome: '+state+'\nExit code: '+str(code)+'\n\n'
    text += 'File edits reported successful by tools:\n'+('\n'.join('- '+p for p in sorted(set(edits))) or '- None recorded.')+'\n\n'
    text += '\n'.join(checks)+'\nNo independent application tests are certified by this record.\nReview Git diff, run project checks and update PLAN.md / CHECKPOINT.md before continuing.\n'
    create_once(root, 'TASK-'+jid+'.md', text)
