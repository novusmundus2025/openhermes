"""Persistent project guidance and evidence-based task records."""
import json
import os
import time
import re
import hashlib
from pathlib import Path


PROJECT_STRUCTURE_GUIDANCE = (
    'Project layout policy: For a new project, first inspect the workspace and identify the requested '
    'language, framework, project type and any user-specified folder structure. Follow the user-specified '
    'layout when provided. Otherwise choose the conventional layout recommended by that ecosystem or '
    'its official scaffolder; there is no universal folder tree for every language. Use the selected '
    'framework scaffolder when appropriate and preserve its conventions. Keep entry points thin and '
    'separate application code, tests, configuration and documentation as the project needs. '
    'For example, use Python package/pyproject conventions, Go cmd/internal where warranted, Rust '
    'Cargo src/tests, Java or Kotlin build-tool conventions, and frontend framework conventions. '
    'For Node.js APIs, separate routes, handlers/services and persistence where the scope warrants it. '
    'Do not force these examples onto another ecosystem, create empty speculative layers, or overengineer '
    'a small script. Record the chosen layout and short rationale in .hermes-project/PLAN.md before '
    'implementation and describe how to run and test it in README.md. Keep secrets, dependency folders '
    'and build outputs out of Git. For existing projects, preserve the established layout unless the '
    'user requests restructuring. Verify imports, build configuration and tests against the chosen layout. '
)


TEMPLATES = {
    'AGENTS.md': PROJECT_STRUCTURE_GUIDANCE + '\n\n' + '# Project instructions\n\nRead .hermes-project/PLAN.md, .hermes-project/CHECKPOINT.md and recent .hermes-project/tasks/TASK-*.md before resuming.\nInspect current code before editing. Preserve unrelated changes.\nUpdate .hermes-project/PLAN.md with steps and progress, and .hermes-project/CHECKPOINT.md with verified results,\nchecks, blockers and next steps. Never store passwords or tokens here.\nA saved edit or Git commit does not prove correctness.\n',
    '.hermes-project/PLAN.md': '# Plan\n\nNo plan recorded yet. Hermes should record the objective and actionable steps\nbefore substantial work, and mark progress based on evidence.\n',
    '.hermes-project/CHECKPOINT.md': '# Checkpoint\n\nNo verified project summary recorded yet. See tasks/TASK-*.md for bridge-recorded\ntask outcomes, including failures. Hermes should update this summary after work.\n',
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
    from projects import read_bytes, open_parent, save_file
    # Open each directory without following user-created symlinks.
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in ('.hermes-project', 'tasks'):
            try:
                os.mkdir(name, 0o755, dir_fd=parent)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
    finally:
        os.close(parent)
    old_tasks = [p.name for p in Path(root).iterdir() if re.fullmatch(r'TASK-[a-f0-9]{32}\.md', p.name)]
    for name in ['PLAN.md', 'CHECKPOINT.md'] + old_tasks:
        destination = '.hermes-project/' + ('tasks/' if name.startswith('TASK-') else '') + name
        try:
            original = read_bytes(root, name)
        except (FileNotFoundError, OSError):
            continue
        if create_once(root, destination, original.decode('utf-8')):
            # Do not remove a root document if a different destination already exists.
            if read_bytes(root, name) == original:
                parent, leaf = open_parent(root, name)
                try:
                    os.unlink(leaf, dir_fd=parent)
                finally:
                    os.close(parent)
    for name, text in TEMPLATES.items():
        create_once(root, name, text)
    create_once(root, '.hermes-project/README.md', '# Hermes project records\n\nPLAN.md: objective, steps and progress.\nCHECKPOINT.md: verified summary and next steps.\ntasks/: bridge-recorded outcomes, including failures.\nRoot AGENTS.md: discoverable project instructions.\nThese records do not replace Git history or application tests.\n')
    # Only upgrade the exact generated instruction template; preserve owner instructions.
    old = TEMPLATES['AGENTS.md'].replace('.hermes-project/tasks/TASK-', 'TASK-').replace('.hermes-project/', '')
    try:
        current = read_bytes(root, 'AGENTS.md')
        if current == old.encode():
            save_file(root, 'AGENTS.md', TEMPLATES['AGENTS.md'], hashlib.sha256(current).hexdigest())
    except (FileNotFoundError, OSError):
        pass


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
    text += '\n'.join(checks)+'\nNo independent application tests are certified by this record.\nReview Git diff, run project checks and update .hermes-project/PLAN.md and .hermes-project/CHECKPOINT.md before continuing.\n'
    initialize(root)
    create_once(root, '.hermes-project/tasks/TASK-'+jid+'.md', text)
