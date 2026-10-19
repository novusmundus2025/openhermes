"""Install the trusted per-user Pipe in an operator-owned Open WebUI 0.11.0 container."""
import argparse
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]

# Executed inside the operator's container. The short-lived admin JWT is never printed.
CONTAINER_SCRIPT = r'''
import json,os,sqlite3,sys,time
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
payload=json.load(sys.stdin)
os.environ['WEBUI_SECRET_KEY']=Path('/app/backend/.webui_secret_key').read_text().strip()
from open_webui.utils.auth import create_token
db=sqlite3.connect('/app/backend/data/webui.db')
admins=db.execute("SELECT id FROM user WHERE role='admin'").fetchall()
uid=payload.get('admin_id')
if not uid:
    if len(admins)!=1: raise SystemExit('Specify --admin-id: exactly one admin could not be selected.')
    uid=admins[0][0]
if (uid,) not in admins: raise SystemExit('Selected user is not an Open WebUI admin.')
token=create_token({'id':uid})
def api(path,data=None):
    req=Request('http://127.0.0.1:8080/api/v1/'+path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urlopen(req,timeout=60) as response:return json.load(response)
try: current=api('functions/id/hermes_profiles')
except HTTPError as error:
    if error.code!=404: raise SystemExit('Cannot inspect Hermes Pipe: HTTP '+str(error.code))
    current=None
if payload['check']:
    print(json.dumps({'installed':bool(current),'active':bool(current and current.get('is_active'))}))
    raise SystemExit(0)
os.umask(0o077)
backup_dir=Path('/app/backend/data/openhermes-backups');backup_dir.mkdir(exist_ok=True)
backup=sqlite3.connect(str(backup_dir/('before-pipe-'+str(time.time_ns())+'.sqlite3')))
db.backup(backup);backup.close()
config_path=Path('/app/backend/data/openhermes-bridge.json')
if config_path.exists():
    (backup_dir/('bridge-'+str(time.time_ns())+'.json')).write_bytes(config_path.read_bytes())
config_path.write_text(json.dumps(payload['bridge']))
config_path.chmod(0o600)
form={'id':'hermes_profiles','name':'Hermes User Profiles','content':payload['source'],'meta':{'description':'Authenticated per-user native Hermes profiles'}}
result=api('functions/id/hermes_profiles/update' if current else 'functions/create',form)
if not result.get('is_active'):api('functions/id/hermes_profiles/toggle',{})
# Preserve existing model grants and add read access for approved authenticated users.
grants=[dict(zip(('principal_type','principal_id','permission'),row)) for row in db.execute(
    "SELECT principal_type,principal_id,permission FROM access_grant WHERE resource_type='model' AND resource_id='hermes_profiles'")]
public={'principal_type':'user','principal_id':'*','permission':'read'}
if public not in grants:grants.append(public)
api('models/model/access/update',{'id':'hermes_profiles','name':'Hermes User Profiles','access_grants':grants})
print('Hermes User Profiles enabled for approved authenticated users. Existing grants preserved.')
print('No account roles, default models or legacy model connections changed.')
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', default='open-webui')
    parser.add_argument('--admin-id', help='Required when more than one administrator exists')
    parser.add_argument('--check', action='store_true', help='Read-only installation/activation check')
    args = parser.parse_args()
    payload = {'admin_id': args.admin_id, 'check': args.check}
    if not args.check:
        settings = json.loads((ROOT / 'settings.json').read_text())
        payload.update(source=(ROOT / 'openwebui_pipe.py').read_text(),
                       bridge={'url': 'http://172.17.0.1:8643', 'key': settings['bridge_key']})
    subprocess.run(['docker', 'exec', '-i', '-e', 'PYTHONPATH=/app/backend',
                    args.container, 'python', '-c', CONTAINER_SCRIPT],
                   input=json.dumps(payload), text=True, check=True)


if __name__ == '__main__':
    main()
