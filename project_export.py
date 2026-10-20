"""Bounded current-worktree ZIP export; no Git history, ignored files or symlinks."""
import base64
import errno
import io
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import zipfile
from projects import ProjectError

MAX_TOTAL=32*1024*1024
MAX_SINGLE=8*1024*1024
MAX_FILES=5000


def excluded(name):
    parts=name.split('/');lower=[p.lower() for p in parts];leaf=lower[-1]
    if any(p in ('.git','.hermes-project','node_modules','.venv','venv','__pycache__','.ssh','.aws','.azure','.gnupg') for p in lower):return True
    if any(p.startswith('.env') for p in lower[:-1]):return True
    if leaf.startswith('.env') and leaf not in ('.env.example','.env.sample','.env.template'):return True
    return (leaf in ('.npmrc','.pypirc','.netrc','credentials.json','auth.json','id_rsa','id_ed25519')
            or leaf.startswith('service-account') or leaf.endswith(('.pem','.key','.p12','.pfx','.sqlite','.sqlite3','.db')))


def git_output(root,args,input_data=None,accepted=(0,)):
    with tempfile.TemporaryFile() as out:
        result=subprocess.run(['git','--no-optional-locks','-c','core.fsmonitor=false','-c','core.hooksPath=/dev/null','-C',str(root)]+args,
                              input=input_data,stdout=out,stderr=subprocess.PIPE,timeout=15)
        if result.returncode not in accepted:raise ProjectError('Git could not prepare the project download',409)
        out.seek(0);data=out.read(2*1024*1024+1)
        if len(data)>2*1024*1024:raise ProjectError('Project has too many files to export',413)
        return data


def file_bytes(root,name):
    parts=name.split('/')
    if not parts or any(p in ('','.','..') for p in parts) or name.startswith('/') or '\\' in name or '\x00' in name:
        raise ProjectError('Unsafe archive path',409)
    parent=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent);os.close(parent);parent=child
        fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        with os.fdopen(fd,'rb') as source:
            info=os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:return None
            if info.st_size>MAX_SINGLE:raise ProjectError('A project file exceeds the 8 MB export limit',413)
            data=source.read(MAX_SINGLE+1);after=os.fstat(source.fileno())
            if len(data)>MAX_SINGLE:raise ProjectError('A project file exceeds the 8 MB export limit',413)
            if (info.st_size,info.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ProjectError('Project changed during export; retry',409)
            return data, bool(info.st_mode & 0o111)
    finally:os.close(parent)


def export_project(project):
    root=Path(project['root'])
    if (root/'.git').is_symlink() or not (root/'.git').is_dir():raise ProjectError('Project needs a local Git repository for ignore-aware export',409)
    candidates=git_output(root,['ls-files','--cached','--others','--exclude-standard','-z']).split(b'\0')
    candidates=sorted(set(p for p in candidates if p))
    if len(candidates)>MAX_FILES:raise ProjectError('Project exceeds the 5000-file export limit',413)
    # --no-index also applies .gitignore to tracked files accidentally committed earlier.
    ignored=set(git_output(root,['check-ignore','--no-index','--stdin','-z'],b'\0'.join(candidates)+b'\0',accepted=(0,1)).split(b'\0')) if candidates else set()
    slug=re.sub(r'[^a-zA-Z0-9_-]+','-',project.get('name','project')).strip('-')[:60] or 'project'
    archive=io.BytesIO();total=0;count=0
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as bundle:
        for raw in candidates:
            if raw in ignored:continue
            try:name=raw.decode('utf-8')
            except UnicodeDecodeError:raise ProjectError('Project contains filenames that cannot be exported as UTF-8',409)
            if excluded(name):continue
            try:item=file_bytes(root,name)
            except OSError as exc:
                if exc.errno in (errno.ENOENT,errno.ELOOP,errno.ENOTDIR):continue
                raise ProjectError('A project file could not be read for export',409) from exc
            if item is None:continue
            data,executable=item;total+=len(data);count+=1
            if total>MAX_TOTAL:raise ProjectError('Project exceeds the 32 MB export limit',413)
            info=zipfile.ZipInfo(slug+'/'+name);info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=(0o100755 if executable else 0o100644)<<16
            bundle.writestr(info,data)
    return {'data':base64.b64encode(archive.getvalue()).decode(),'filename':slug+'.zip','files':count}
