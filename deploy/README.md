# Deployment baseline

This repository owns the bridge, Projects frontend/backend, Open WebUI integration,
runtime recipes and tests. Hermes and Open WebUI remain upstream dependencies.

Recorded GX10 baseline: Hermes commit
`9e01b44dd23978f9ff40b7837467f3a9f1582130`, clean upstream working tree;
Open WebUI 0.11.0; Linux ARM64, Docker and Python 3. Install PyYAML from
`requirements.txt`. Frontend builds use `npm ci` and `npm run build`.

Deployment currently assumes Linux user `a`, checkout `/home/a/openhermes`,
Hermes at `/home/a/.hermes/hermes-agent`, its installed ARM64 Python runtime,
and Docker bridge address `172.17.0.1`. Inspect `projects.py`, `provision.py`,
`build_project_runtime.py` and Dockerfiles before deploying elsewhere.
This is a deployment baseline, not a complete clean-server installer.

1. Install the recorded Hermes version and configure its authenticated multiplexed
   gateway on private port 8642 using upstream configuration. Set its model/provider.
2. Copy `settings.example.json` to `settings.json`, fill the secrets and provider
   endpoint, and set permissions to 0600. Use independently generated random bridge
   and gateway secrets; never commit them. `upstream_key` must match the gateway.
3. Build the project image with `python3 build_project_runtime.py`. It snapshots
   trusted installed code/tools, excluding user data. Build `openhermes-browser:1`
   using `runtime/browser.Dockerfile` and a matching trusted ARM64 Playwright
   `chrome-linux/` context. Base sandbox images currently use mutable tags.
4. Install `deploy/openhermes.service` under `~/.config/systemd/user/`, then run
   `systemctl --user daemon-reload` and `systemctl --user enable --now openhermes`.
   Enable lingering through host administration if needed after logout.
5. Fill `deploy/openwebui-bridge.example.json` and copy it into Open WebUI as
   `/app/backend/data/openhermes-bridge.json`, mode 0600. Its key equals `bridge_key`.
   Install `openwebui_pipe.py` as Function `hermes_profiles`, enable it, and grant
   intended accounts access. Other model connections can bypass this integration.
6. Run `python3 install_projects.py` while users are idle: it modifies and restarts
   Open WebUI. Reapply after replacing that container image.
7. Check private bridge `/health`, authenticated chat and Projects using two accounts.
   Keep ports 8642/8643 private; public exposure and company OAuth are not configured.

Back up the SQLite database, user profile/project directories, project runtime state
and Open WebUI data consistently. Protect secret backups separately. Nothing in
this repository migrates live data or changes the current deployment automatically.

Projects soft-deletion hides a project and conversations but retains disk files,
Git, metadata and containers for recovery. It blocks queued/running tasks. There
is no Trash UI or disk reclamation yet.

Run the 48 backend checks on Linux:

```sh
python3 -m unittest test_agent_recovery test_agent_contracts test_projects test_provider_tools test_server
```

Filesystem tests require Linux APIs. Live Swagger regression is separately opt-in
and consumes inference. This prototype has not been load-tested for 200 concurrent
users. Human browser login handoff is not integrated.

No open-source license is granted. Dependency licenses belong to their authors;
the frontend bundle preserves third-party legal comments.
