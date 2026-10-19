"""Snapshot installed Hermes code/dependencies into a container image, without user data."""
from pathlib import Path
import shutil
import subprocess


def main():
    root = Path(__file__).resolve().parent
    context = root / 'runtime-build'
    context.mkdir(exist_ok=True)
    shutil.copy2(root / 'runtime' / 'Dockerfile', context / 'Dockerfile')
    # Only runtime/code trees are included, never the owner's profile root or credentials.
    paths = ['home/a/.hermes/hermes-agent', 'home/a/.hermes/installs',
             'home/a/.hermes/tools/python-3.14.7+20260901-linux-arm64']
    tools = Path('/home/a/.hermes/tools')
    for prefix in ('node-', 'npm-', 'ffmpeg-', 'ripgrep-'):
        paths.extend(str(p).lstrip('/') for p in tools.glob(prefix + '*') if p.is_dir())
    paths.append('home/a/.hermes/tools/facts.json')
    exclusions = ['.git', 'node_modules', '__pycache__', '*.log', '.env', '.env.*',
                  'auth.json', '*.sqlite*', 'state.db*']
    command = ['tar', *['--exclude=' + item for item in exclusions], '-cf', '-', '-C', '/', *paths]
    archive = subprocess.Popen(command, stdout=subprocess.PIPE)
    try:
        subprocess.run(['tar', '-xf', '-', '-C', str(context)], stdin=archive.stdout, check=True)
    finally:
        archive.stdout.close()
    if archive.wait():
        raise RuntimeError('Could not snapshot Hermes runtime')
    subprocess.run(['docker', 'build', '-t', 'openhermes-project:1', str(context)], check=True)


if __name__ == '__main__':
    main()
