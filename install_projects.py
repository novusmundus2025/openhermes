"""Install the same-origin Projects extension into an existing Open WebUI container."""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent
DEST = '/app/backend/data/openhermes-projects'


def main():
    subprocess.run(['docker', 'exec', 'open-webui', 'mkdir', '-p', DEST + '/web'], check=True)
    for name in ('projects_router.py', 'web/index.html', 'web/app.js', 'web/style.css', 'web/nav.js'):
        subprocess.run(['docker', 'cp', str(ROOT / name), 'open-webui:' + DEST + '/' + name], check=True)
    code = '''from pathlib import Path
import shutil
import re
main=Path('/app/backend/open_webui/main.py')
backup=Path('/app/backend/data/openhermes-projects/main.py.before-projects')
if not backup.exists(): shutil.copy2(main,backup)
text=main.read_text()
marker='# OpenHermes Projects extension'
addition="""# OpenHermes Projects extension
import importlib.util as _oh_import
_oh_spec = _oh_import.spec_from_file_location('openhermes_projects', '/app/backend/data/openhermes-projects/projects_router.py')
_oh_module = _oh_import.module_from_spec(_oh_spec)
_oh_spec.loader.exec_module(_oh_module)
app.include_router(_oh_module.router)

"""
if marker not in text:
 anchor="app.mount('/static'"
 assert anchor in text, 'Open WebUI mount point changed; inspect before installing'
 text=text.replace(anchor,addition+anchor,1)
 compile(text,str(main),'exec')
 main.write_text(text)
index=Path('/app/build/index.html')
backup=Path('/app/backend/data/openhermes-projects/index.html.before-projects')
if not backup.exists(): shutil.copy2(index,backup)
text=index.read_text()
script='<script defer src="/projects/nav.js?v=2"></script>'
text=re.sub(r'<script defer src="/projects/nav[.]js[^"]*"></script>',script,text)
if script not in text: text=text.replace('</head>',script+'</head>',1)
index.write_text(text)
print('Projects routes and sidebar link installed')
'''
    subprocess.run(['docker', 'exec', 'open-webui', 'python', '-c', code], check=True)
    subprocess.run(['docker', 'restart', 'open-webui'], check=True)


if __name__ == '__main__':
    main()
