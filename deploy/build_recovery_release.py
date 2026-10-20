"""Build a deterministic, source-only recovery component archive and checksum."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parent.parent
FILES=[
    'deploy/RECOVERY_VERSION','deploy/RECOVERY_RELEASE.md','deploy/CONTRIBUTOR_RECOVERY.md',
    'deploy/contributor_watchdog.py','deploy/mac_runtime_watchdog.py','deploy/mlx_runtime_health.py',
    'deploy/contributor-watchdog.service','deploy/examples/vllm-watchdog.json',
    'deploy/examples/llamacpp-watchdog.json','deploy/examples/ollama-watchdog.json',
    'test_contributor_watchdog.py','test_mac_runtime_watchdog.py','test_mlx_runtime_health.py',
    'deploy/build_recovery_release.py',
]


def build(destination):
    dirty=subprocess.check_output(['git','status','--porcelain','--']+FILES,cwd=ROOT,text=True)
    if dirty.strip():raise SystemExit('Commit recovery release sources before packaging')
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    version=(ROOT/'deploy/RECOVERY_VERSION').read_text(encoding='utf-8-sig').strip()
    name='openhermes-recovery-'+version
    output=Path(destination);output.mkdir(parents=True,exist_ok=True)
    contents={path:(ROOT/path).read_text(encoding='utf-8-sig').replace('\r\n','\n').encode() for path in FILES}
    manifest={'version':version,'source_commit':commit,'files':{p:hashlib.sha256(b).hexdigest() for p,b in sorted(contents.items())}}
    contents['MANIFEST.json']=(json.dumps(manifest,indent=2,sort_keys=True)+'\n').encode()
    archive=output/(name+'.zip')
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as bundle:
        for path,data in sorted(contents.items()):
            info=zipfile.ZipInfo(name+'/'+path,date_time=(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o100644<<16
            bundle.writestr(info,data)
    checksum=hashlib.sha256(archive.read_bytes()).hexdigest()
    (output/'SHA256SUMS').write_text(checksum+'  '+archive.name+'\n',encoding='ascii')
    print(json.dumps({'archive':str(archive),'sha256':checksum,'source_commit':commit}))
    return archive


if __name__=='__main__':build(sys.argv[1] if len(sys.argv)>1 else ROOT/'state/release')
