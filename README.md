# OpenHermes

The manual Guided Browser is at `/guided-browser/` (also linked in the Open WebUI
sidebar after refresh). It uses authenticated owner-scoped sessions and native
Hermes browser tools, with screenshots, current element references, click/fill,
back/scroll, and explicit stop. Each session has a separate Hermes runtime and
sandbox; it does not attach to an existing regular-chat browser or its cookies.
There are four guided slots and a 15-minute inactivity expiry. Stop removes that
session's sandbox and private runtime. Backend restarts close child browser workers;
runtime-directory recovery after an abnormal host crash remains operator-managed.
No AI autopilot, reusable workflow recording or noVNC takeover is included yet.
Public HTTP(S) URLs are validated; internal/LAN URLs and arbitrary JavaScript/shell
commands are not accepted. This URL validation is not a network-level egress firewall.
Typed input is not stored in the bridge DB or chat; native runtime cleanup still
matters for website cookies/history. Do not claim secure browser-login handoff.

For the complete multi-user setup sequence and runnable deployment scripts, see
[deploy/SETUP.md](deploy/SETUP.md). Existing installations should use its update
section rather than generating new secrets.

Project memory: AGENTS.md records instructions, .hermes-project/PLAN.md records the agent's plan,
and .hermes-project/CHECKPOINT.md records its verified summary. Existing documents are preserved
when initializing projects. Each agent task independently writes .hermes-project/tasks/TASK-<id>.md with
its exit status and tool-reported edits. Tasks request local Git snapshots after
completion or failure; failed work is labeled partial. No automatic push occurs.
Git snapshots can include manual working-tree edits. They are recovery points,
not proof of correctness or a substitute for backups. Checkpoint failures appear
in the execution log. Secrets must stay out of project files; ignored files and
common environment/private-key files are excluded from automatic staging.

## Projects coding workspace

Open `http://YOUR_SERVER:3000/projects/`, or choose **Projects** in the
Open WebUI sidebar after a page refresh. Sign in using the same Open WebUI account.

- Create, rename, and switch projects; optionally clone a public HTTPS Git repository.
- Browse folders, create files, edit UTF-8 text, and save with Ctrl/Cmd+S.
- Ask the native Hermes harness to edit files and run tools. Tool calls and results
  appear in the chat. Project conversations resume across requests.
- Run commands in the command console, inspect Git changes, and stop running work.
- Preview a static HTML file and its relative assets in a sandboxed iframe.

Each project has a persistent Docker container with its project mounted at
`/workspace`, a separate persistent Hermes state directory at `/state`, resource
limits, dropped capabilities, and no Docker socket or host credential mounts.
The complete Hermes process runs inside that container. Git inspection also runs
inside the container so repository configuration cannot execute commands on the host.
Model access uses a restricted model-proxy key, separate from the user's gateway key.

The native Hermes project registry records these folders under the user's profile.
Project files live at `~/.hermes/profiles/owui-<hash>/workspace/projects/<slug>/`.
Runtime state lives at `/home/a/openhermes/state/project-runtime/<project-id>/`.
Project metadata, jobs, and their output are in the existing SQLite database.

File and job APIs validate the authenticated owner on every request. File access
rejects traversal and symlinks; editor saves detect changes made since opening.
The editor supports regular UTF-8 files up to 1 MB and hides `.git` and `.env` files.
The command console runs finite commands (60 seconds), not an interactive PTY.
Agent tasks have a 600-second budget and 16 tool iterations. Preview currently
serves static files; an authenticated reverse proxy for long-running app servers
is not included. Preview tickets expire after 10 minutes; refresh to renew them.

The project runtime snapshots the installed Hermes code and dependencies. Rebuild
after a Hermes update with `python3 build_project_runtime.py`; existing containers
retain their image until recreated. Project files and `/state` are bind mounts
and survive container recreation.

Install or reapply the Open WebUI extension with `python3 install_projects.py`.
This adds the API routes before the SPA mount and a sidebar navigation script;
original files are backed up under `/app/backend/data/openhermes-projects/`.
Reapply after replacing the Open WebUI container/image. Frontend assets and the
extension router are kept in its persistent data volume. No Open WebUI auth
settings or account roles are changed by the Projects installer.

## Open WebUI chat integration

Open WebUI authenticates users. The **Hermes User Profiles** Pipe receives Open
WebUI's injected `__user__`, sends a minimal identity over a private authenticated
connection to this bridge, and routes chat requests to `/p/<profile>/v1/chat/completions`.
The bridge records identities in SQLite and provisions a fresh Hermes profile on
first use. Existing profiles are reused. No separate user registration commands
are needed. Pending Open WebUI accounts are rejected.

Installed server directory: `/home/a/openhermes`.

- Service: `systemctl --user status openhermes`
- Logs: `journalctl --user -u openhermes`
- Users: `/home/a/openhermes/state/users.sqlite3`
- Profiles: `/home/a/.hermes/profiles/owui-*`
- Private settings: `/home/a/openhermes/settings.json` (never commit)
- Pipe secret: `/app/backend/data/openhermes-bridge.json` inside Open WebUI

Refresh Open WebUI and select **Hermes User Profiles** to use automatic profiles.
Other model connections keep their existing behavior. First-use provisioning is
triggered by chatting with this model, rather than merely logging in.

Send `/credits` to this model to view remaining credits, contributor earnings,
grants, spending, and usage. New accounts start at zero; negative balances are
allowed. Spending follows the live MundusX rule:
`ceil((input_tokens + output_tokens) / 100)` credits per completed request.
The `usage_events` table records each request against its user, including actual
reported input/output tokens. When tokens are missing, the existing MundusX
fallback charges `max(1, ceil((prompt_chars + output_chars) / 400))` and labels
the charge as `character_estimate`. The chat command shows the estimate count.
Requests without either kind of measurement remain unrated. Earlier
requests made before accounting was installed are not included.

The provider currently omits usage from streaming responses. Managed Hermes
profiles use the bridge's authenticated provider adapter, which obtains a normal
provider response and reports actual runtime tokens directly to the active
request's meter. Requests for each profile are serialized to keep attribution
correct. Open WebUI can still receive an SSE response, with the answer delivered
when ready. Secrets for model requests stay in private server settings.

Contribution imports are disabled by default. Rewards remain unassigned until an
operator explicitly links a contributor device.
If enabled later through `contribution_source` in private settings, the sync worker
can import trusted reward events every five minutes for explicitly linked devices.
Source event IDs prevent duplicate payouts. Devices are never assigned automatically.

Owner administration, from the installed server directory:

```sh
python3 credits.py show --user-id USER_ID
python3 credits.py link-device --user-id USER_ID --device-id NODE_ID
python3 credits.py sync
python3 credits.py earn --user-id USER_ID --amount 10 --reason 'Verified contribution' --reference UNIQUE_EVENT_ID
```

Awards require a contributor role (linking a device sets it), a unique reference,
and an audit reason. Administration is available through the server CLI only;
users cannot award themselves credits through chat. The balance is always the
sum of ledger entries rather than a separately editable counter.

The bridge uses Python 3 and the standard library; `provision.py` additionally
requires PyYAML and an installed Hermes CLI. Hermes multiplexing must be enabled.
The Pipe requires aiohttp, already installed in Open WebUI. A separate API key is
derived for each profile. Profiles use Docker terminals with no extra host mounts
or forwarded environment variables. Profiles are application separation, not an
operating-system security boundary for mutually untrusted tenants.

Run checks with `python3 -m unittest -v`. The installation backs up the Open WebUI
database to `/app/backend/data/openhermes-before-install.sqlite3` and the original
Hermes configuration to `/home/a/openhermes/config.before-multiplex.yaml`.

To disable this integration, deactivate the `hermes_profiles` Function in Open
WebUI and run `systemctl --user disable --now openhermes`. User records and
profiles remain available. Do not restore the entire database backup over new
Open WebUI activity.

The Projects editor uses bundled CodeMirror 6 with syntax highlighting, folding,
search, bracket matching, and a Problems panel. JavaScript uses browser-side ESLint
with a small built-in rule set; JSON validates with JSON.parse, and other supported
language parsers report syntax errors. These checks do not load a project's ESLint
configuration or replace its tests/type checker. Prettier formats JS/TS, JSON,
HTML/Vue, CSS/SCSS/Less, Markdown, and YAML on demand; formatting requires an
explicit Save. Python has highlighting and parser diagnostics, without formatting.
Build the self-hosted editor bundle with `npm ci` then `npm run build`. Source lives
in `frontend/`; deploy the generated `web/app.js` together with index.html/style.css.
No project source is sent to a formatter service or CDN.

Projects chat now has a per-project history selector and New chat control. Existing
jobs migrate into Original chat and keep their original native Hermes conversation.
Each new chat gets a separate native Hermes session; all chats still share the
project filesystem and one-task-at-a-time execution limit. History stores in SQLite
project_chats/project_jobs; the last selected chat is remembered in the browser.
Tool cards pair calls with results and display explicit Running/Succeeded/Failed
states. Task banners report execution state; they do not certify code correctness.
Arguments/results display separately with decoded strings and scrollable code.

Agent task regression checks:
- `python3 -m unittest test_agent_contracts` tests evidence-based pass/fail rules.
- On the GX10, `python3 regression_swagger.py --live` runs real Hermes/model
  inference against an isolated Node fixture. It preserves API.md, requires an
  interactive /docs page wired to /openapi.json, and checks all five CRUD OpenAPI
  operations. It starts the application and probes HTTP; prose alone cannot pass.
  This opt-in test uses real inference and may install packages in its own container.
  Files, JSONL, responses and report remain in state/regressions/<run-id>/.
  Its dedicated container stops afterward. User projects are not modified.
- Contracts in agent_contracts.py support other required files, preserved-file
  hashes, HTTP status/content and OpenAPI checks. New task types need their own
  contracts; a passing Swagger evaluation does not guarantee arbitrary tasks.
- Project workers now reject a missing final result or an unresolved native
  file-mutation-verifier warning even when the model/process reports success.
  This gate reports failure; it does not bypass file protections or certify intent.

Blocked-edit recovery: project agent jobs run through agent_recovery.py. Native
stale-write or exact-match patch errors can trigger one additional Hermes turn in
that same chat. Recovery must produce a full-file read, a verified file-tool edit,
and a read-back for each blocked path. Otherwise the job stays failed. No guard
is disabled and no terminal write substitutes for a guarded file edit. The overall
watchdog allows up to 630 seconds across both attempts; Stop still stops the
project container. Partial/paginated reads are conservatively unverified by this
recovery checker, so large files may require manual follow-up. Tests run with
`python3 -m unittest test_agent_recovery`.
Recovery read-back is performed independently by the host after the retry: it
compares the saved bytes with the successful file-tool edit applied to the fresh
pre-retry snapshot. A recovery_verify receipt records this check. Native full-read
and successful-edit receipts are still required. The UI labels verified recovery
explicitly while retaining failed attempts in the tool history.

Native browser runtime: regular Hermes user profiles use openhermes-browser:1
(a desktop sandbox with the installed Playwright Chromium copied to
/opt/hermes-chromium and linked as /usr/bin/chromium). Docker recipe is
runtime/browser.Dockerfile; use a build context containing the installed trusted
Playwright chrome-linux folder. No account cookies or user files belong in the
image. Existing profiles have config.before-browser.yaml backups. Native
browser_navigate/browser_snapshot were tested against https://example.com.
This enables server-side automation, not an Open WebUI login takeover panel.
Hermes's native dashboard has a ticketed, lease-controlled desktop viewer; it
still needs an authenticated, per-user integration before secure human login
handoff can be claimed. Do not expose raw CDP/VNC ports or ask for passwords in chat.
Browser configuration uses browser.backend: off in Hermes terminology: this turns
OFF the Browser Use Python replacement and selects native browser_navigate,
browser_snapshot, browser_click and related tools. It does not disable browsing.
A regular per-user chat was verified to execute browser_navigate successfully and
return Example Domain. Test browsers from the earlier backend were closed before
switching to avoid Chromium profile locks. Human login handoff remains unconnected.

Timeout policy: inference assignments are capped at 600 seconds after assignment;
worker no-model-progress watchdog remains 300 seconds. Queued inference requests
are governed separately. Projects runs a 600-second native attempt with a 630-second
overall watchdog including any edit recovery. A provider call may wait 620 seconds
for a terminal upstream response. This bridge receives buffered provider responses
and cannot itself measure token inactivity; the inference worker owns that check.

Project deletion permanently removes only the selected project's server files,
local Git repository, Docker container, runtime state and stored conversations.
The user profile and other projects remain. Remote Git repositories are never
contacted or changed by deletion. Unpushed work is lost; the UI confirms this.
Queued/running project tasks block deletion. An audit tombstone remains.
Previously hidden projects are not automatically purged by this change.
