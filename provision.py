"""Create a fresh Hermes profile; never clone the owner's history or credentials."""
import re
import subprocess
import sys
from pathlib import Path
import yaml
import json
import hmac
import hashlib


def provision(profile):
    if not re.fullmatch(r"owui-[0-9a-f]{32}", profile):
        raise ValueError("Invalid managed profile name")
    home = Path.home() / ".hermes" / "profiles" / profile
    if not home.exists():
        subprocess.run(
            ["/home/a/.local/bin/hermes", "profile", "create", profile,
             "--no-alias", "--description", "Automatically provisioned Open WebUI user"],
            check=True, capture_output=True, timeout=180,
        )
    if not (home / "config.yaml").is_file() or not (home / "profile.yaml").is_file():
        raise RuntimeError("Incomplete profile requires inspection")
    settings = json.loads((Path(__file__).parent / "settings.json").read_text())
    key = hmac.new(settings["upstream_key"].encode(), profile.encode(), hashlib.sha256).hexdigest()
    env = home / ".env"
    lines = [line for line in env.read_text().splitlines()
             if not line.startswith(("API_SERVER_KEY=", "OPENHERMES_MODEL_KEY="))]
    env.write_text("\n".join(lines) + "\nAPI_SERVER_KEY=" + key + "\nOPENHERMES_MODEL_KEY=" + key + "\n")
    env.chmod(0o600)
    config = yaml.safe_load((home / "config.yaml").read_text()) or {}
    # MundusX streaming currently omits usage; non-streaming returns runtime tokens.
    config.setdefault('model', {})['streaming'] = False
    if settings.get('model'):
        config['model']['default'] = settings['model']
    config['model']['base_url'] = 'http://172.17.0.1:8643/provider/' + profile + '/v1'
    config['model'].pop('api_key', None)
    config['model']['key_env'] = 'OPENHERMES_MODEL_KEY'
    config["browser"] = {"backend": "off"}  # Use native browser_* tools, not Browser Use Python.
    config["terminal"] = {
        "backend": "docker", "docker_image": "openhermes-browser:1", "cwd": str(home / "workspace"),
        "docker_mount_cwd_to_workspace": True, "docker_volumes": [],
        "docker_forward_env": [], "home_mode": "isolated",
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    (home / "config.yaml").chmod(0o600)
    home.chmod(0o700)


if __name__ == "__main__":
    provision(sys.argv[1])
