# Open WebUI + Hermes: multi-user setup and scripts

## What we built

1. Open WebUI handles account registration, approval, authentication and roles.
2. The `Hermes User Profiles` Pipe receives the authenticated `__user__` identity.
3. The private bridge validates its trusted connection and records the immutable
   Open WebUI user ID in SQLite. It derives `owui-<sha256(user-id)[:32]>`.
4. On first chat/Projects use, `provision.py` creates a fresh native Hermes profile,
   rather than copying the owner's profile, credentials, memory or conversations.
5. Regular chat uses the shared authenticated Hermes gateway at
   `/p/<profile>/v1/chat/completions`. Each profile has its own derived key.
6. Model calls use the authenticated provider adapter and the configured Ehda
   inference endpoint. Hermes runs on GX10; inference may run on a different node.
7. Projects uses native Hermes CLI in a dedicated container per project. It shares
   the account identity and provider adapter, but does not execute through the shared
   gateway. Files mount at `/workspace`, with separate project runtime at `/state`.
8. The Projects router uses Open WebUI's real authentication; browser input cannot
   choose another user's identity. Ownership checks apply to files, chats and jobs.
9. Credits start at zero, may be negative, and use the SQLite ledger. Current formula:
   `ceil((input_tokens+output_tokens)/100)`, with labeled character-estimate fallback.
   Contribution imports are off until devices are explicitly linked.
10. Plans/checkpoints live in `.hermes-project/`, with root `AGENTS.md` for discovery.
    Agent tasks attempt local Git checkpoint commits. Remote pushes are separate.
11. The Guided Browser at `/guided-browser/` uses native Hermes tools through
    `/api/openhermes/browser/action`. Open WebUI injects the approved user's identity;
    sessions, screenshots and controls are owner-scoped. It needs no separate
    browser:8077, broker:8088 or public VNC service. Autopilot and secure graphical
    login takeover are not implemented by this first version.

## Scripts in this repository

| File | Purpose |
| --- | --- |
| `deploy/configure_new_install.py` | Generate private keys/settings and configure gateway multiplexing on a new installation |
| `deploy/openwebui.compose.yaml` | Start a fresh Open WebUI with a persistent volume and the recorded image digest |
| `deploy/start-services.sh` | Install/start the native gateway and bridge user services |
| `deploy/install_openwebui_pipe.py` | Install/update/enable the Pipe and grant authenticated users model read access |
| `provision.py` | Internal automatic per-user profile provisioning; not a manual account-creation step |
| `build_project_runtime.py` | Build the project image from trusted installed Hermes code/tools |
| `runtime/browser.Dockerfile` | Add matching Chromium to the native desktop sandbox |
| `install_projects.py` | Install Projects routes, UI and sidebar integration; restarts Open WebUI |
| `credits.py` | Operator-only credit ledger/device administration |

## Prerequisites and recorded versions

Current scripts target Linux ARM64 GX10, user `a`, checkout `/home/a/openhermes`,
Docker access, Node/npm, systemd user services, Python 3 and PyYAML.
The native Hermes CLI must already be installed at `/home/a/.local/bin/hermes`,
with its runtime under `/home/a/.hermes`. Recorded upstream commit:
`9e01b44dd23978f9ff40b7837467f3a9f1582130`.

Open WebUI reported 0.11.0 on our server and was installed from `:main`.
The Compose example pins the actual recorded digest rather than following main.
The gateway and runtime integration depend on this Hermes installation layout;
review hardcoded paths in `projects.py`, `provision.py` and the runtime builder
before using a different account or platform. This repo does not install the
upstream Hermes dependency from scratch. Keep upstream licenses with that dependency.

## Fresh installation

Run on GX10 as `a`. Commands below are for a new installation. Do not run the
Compose creation step over an existing server or replace its data volume.

```bash
cd /home/a
git clone https://github.com/novusmundus2025/openhermes.git
cd openhermes
# Use your approved Python environment; install requirements there.
python3 -m pip install -r requirements.txt
npm ci
npm run build

docker compose -f deploy/openwebui.compose.yaml up -d
```

Visit `http://YOUR_GX10:3000/` and create the owner/admin account through Open WebUI.
Keep the first administrator. For subsequent users, configure registration and
account approval in Admin Settings, then approve pending accounts as appropriate.
Do not assign every new user the admin role.

Configure the bridge and native gateway. The script asks for the provider URL,
actual upstream model ID and API key; it never prints the key. Use the real model
ID, not just the display label “Ehda”. It refuses to overwrite existing settings.

```bash
python3 deploy/configure_new_install.py
python3 build_project_runtime.py

# This context must contain the trusted, matching ARM64 chrome-linux directory.
# Supply CHROMIUM_CONTEXT yourself; never use a logged-in browser profile.
export CHROMIUM_CONTEXT=/absolute/path/to/chromium-build-context
docker build -t openhermes-browser:1 -f runtime/browser.Dockerfile "$CHROMIUM_CONTEXT"

bash deploy/start-services.sh
python3 deploy/install_openwebui_pipe.py
python3 install_projects.py
```

If there is more than one administrator, choose explicitly:

```bash
python3 deploy/install_openwebui_pipe.py --admin-id YOUR_OPENWEBUI_ADMIN_USER_ID
```

The Pipe installer runs locally inside the operator-owned container, makes a SQLite
backup, creates a short-lived admin token without printing it, and uses Open WebUI
APIs. It keeps existing access grants, adds authenticated-user read access, and
does not change account roles or unrelated connections. The installer assumes the
recorded SQLite-backed Open WebUI schema, not a PostgreSQL Open WebUI deployment.

Refresh Open WebUI and select **Hermes User Profiles** as the model. Optionally set
that model as the default in Admin Settings. Legacy direct-model connections can
bypass this bridge; they are not automatically removed or altered by these scripts.

For services to persist after logout, a host administrator can enable lingering:

```bash
sudo loginctl enable-linger a
```

## Existing GX10 installation

The integration is already enabled. Do not regenerate keys or rerun new-install
configuration. Inspect first:

```bash
cd /home/a/openhermes
python3 deploy/install_openwebui_pipe.py --check
systemctl --user status openhermes
curl --fail http://172.17.0.1:8643/health
docker ps
```

The existing gateway was started manually. `start-services.sh` deliberately refuses
to start a duplicate when port 8642 is already served outside its service. Switching
to the service requires an idle maintenance window and deliberately stopping that
specific existing gateway first. These scripts have not converted the live gateway.

To update an installed Pipe, run `python3 deploy/install_openwebui_pipe.py` during
an idle window. To reinstall Projects after replacing Open WebUI's image, run
`python3 install_projects.py`; this restarts that container. Rebuild the project
runtime after upstream Hermes updates; existing containers retain their old image.

## Verify two-user isolation

1. Sign in as account A, select Hermes User Profiles, and send a small request.
2. Sign in separately as approved account B and do the same.
3. Confirm two different `owui-*` profiles under `/home/a/.hermes/profiles/`.
4. Create one project under each account. A must not see B's project or files.
5. Within one project, new conversations share files/Git but have distinct sessions.
6. Inspect tool receipts and run application checks; a final answer alone is not proof.

Automated backend checks on Linux:

```bash
python3 -m unittest test_project_memory test_agent_recovery test_agent_contracts test_projects test_provider_tools test_server
```

The Pipe read-only activation check was exercised on our existing server. The new
installation sequence has not been tested end-to-end on a clean GX10; container
image/runtime builds depend on the preinstalled native Hermes toolchain.

## Data, secrets and operational limits

```text
/home/a/openhermes/settings.json                  private server settings
/home/a/openhermes/state/users.sqlite3            identity/credits/project ledger
/home/a/.hermes/profiles/owui-<hash>/              account profile
  workspace/projects/<slug>/                     project files and local .git
/home/a/openhermes/state/project-runtime/<id>/    per-project Hermes runtime/state
Open WebUI volume:/app/backend/data/             Open WebUI accounts and chats
```

Back up those locations consistently; keep encrypted secret backups separately.
Do not commit settings, databases, user projects, browser sessions or logs. Script
backups inside Open WebUI contain private data and must stay private too.

Deleting a project removes its local container, files, Git and runtime after UI
confirmation. Account profiles and remote Git repositories remain. Unpushed work
is lost. No deletion command contacts a remote repository.

The current inference policy is a 10-minute assignment cap with the worker's
existing five-minute no-model-progress watchdog. That policy is configured in
the separate control-plane service; these setup scripts do not configure it.
Projects uses a 600-second attempt and a 630-second overall watchdog. Long chat
history can slow model decisions; a tested fresh repair used much less context.

Model context metering and warnings are not implemented in the UI. Server-side
browser automation exists, but secure human browser-login handoff is not integrated.
No public tunnel or company OAuth is enabled by this guide. Ports 8642 and 8643
must remain private; the bridge trusts Open WebUI's injected identity.

## Guided Browser Function

After updating the bridge and Guided Browser assets, install the complete Function:

```sh
python3 deploy/install_openwebui_pipe.py --guided-browser
python3 deploy/install_openwebui_pipe.py --guided-browser --check
```

Select **Hermes Guided Browser — Ehda** in a new chat. Example:
`Open https://example.com and report the page heading.` Use `/pause` to take
human control, `/continue` to resume the previous task, `/status` to inspect progress,
and `/close` to clear the session. Login is performed in `/guided-browser/`, never
through chat. AI stops on detected login/verification pages and recognized
state-changing controls. The human lane is reference-based fill/click, not a noVNC
desktop; complex CAPTCHA/SSO flows may require additional browser support.

Default valves: `BROWSER_URL=http://172.17.0.1:8643`,
`EXEC_MODEL=mundusx-agnostic`, `MAX_STEPS=10`, `LEGEND_MAX=25`,
`WORKSPACE_URL=/guided-browser/`. The bridge key comes from its private container
configuration. Separate `BROKER_URL` and `NOVNC_URL` services are not needed.
Model usage is recorded against the authenticated user through the existing ledger.
AI requests have a bounded provider timeout; pausing blocks late browser actions
but does not cancel inference already running upstream. Sessions expire after
15 minutes idle, and cookies persist only within that live private session.
Configure HTTPS before public use or login over untrusted networks.
