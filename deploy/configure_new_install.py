"""Configure a NEW GX10 installation. Refuses to overwrite existing bridge settings."""
import getpass
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import yaml


ROOT = Path(__file__).resolve().parents[1]


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    os.umask(0o077)
    settings_file = ROOT / 'settings.json'
    if settings_file.exists():
        raise SystemExit('settings.json already exists. Existing installations must keep their keys; no changes made.')
    hermes = Path.home() / '.hermes'
    config_file = hermes / 'config.yaml'
    if not config_file.exists():
        raise SystemExit('Install and configure native Hermes first; ~/.hermes/config.yaml is required.')
    endpoint = input('OpenAI-compatible inference base URL, ending /v1: ').strip().rstrip('/')
    if not endpoint.startswith('https://') or not endpoint.endswith('/v1'):
        raise SystemExit('Use the HTTPS inference base URL ending /v1.')
    model = input('Actual upstream model ID: ').strip()
    if not model:
        raise SystemExit('A model ID is required.')
    provider_key = getpass.getpass('Inference API key (not echoed): ').strip()
    if not provider_key:
        raise SystemExit('An inference API key is required.')
    bridge_key, gateway_key = secrets.token_hex(32), secrets.token_hex(32)
    settings = json.loads((ROOT / 'settings.example.json').read_text())
    settings.update(bridge_key=bridge_key, upstream_key=gateway_key,
                    model_upstream=endpoint, model_upstream_key=provider_key, model=model)
    env_file = hermes / '.env'
    for file in (config_file, env_file):
        if file.exists():
            backup = file.with_name(file.name + '.before-openhermes')
            if not backup.exists():
                shutil.copy2(file, backup)
                backup.chmod(0o600)
    config = yaml.safe_load(config_file.read_text()) or {}
    config.setdefault('gateway', {})['multiplex_profiles'] = True
    config.setdefault('model', {}).update(default=model, base_url=endpoint,
                                         provider='custom', key_env='OPENHERMES_UPSTREAM_KEY')
    config['model'].pop('api_key', None)
    config_file.write_text(yaml.safe_dump(config, sort_keys=False))
    config_file.chmod(0o600)
    values = {'API_SERVER_ENABLED': 'true', 'API_SERVER_HOST': '172.17.0.1',
              'API_SERVER_PORT': '8642', 'API_SERVER_KEY': gateway_key,
              'OPENHERMES_UPSTREAM_KEY': provider_key}
    lines = (env_file.read_text().splitlines() if env_file.exists() else [])
    lines = [line for line in lines if line.partition('=')[0].strip() not in values]
    # JSON quoting prevents whitespace and shell-like content becoming .env syntax.
    env_file.write_text('\n'.join(lines + [k + '=' + json.dumps(v) for k, v in values.items()]) + '\n')
    env_file.chmod(0o600)
    with settings_file.open('x') as file:
        json.dump(settings, file, indent=2)
        file.write('\n')
    settings_file.chmod(0o600)
    print('Private settings created and native gateway multiplexing configured. No secrets printed.')
    print('Next: start the gateway/bridge, then run deploy/install_openwebui_pipe.py.')


if __name__ == '__main__':
    main()
