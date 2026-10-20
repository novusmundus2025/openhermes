# SuperCoder project execution

`ehda-supercoder` is a preset backed by the `hermes_coder` Open WebUI Function.
The Function uses Open WebUI's injected identity and conversation metadata, then
calls the private bridge `/coding` start/status API. It does not forward a coding
prompt to the plain model connection or attempt to run model-produced tool text.

Each `(user ID, Open WebUI conversation ID)` maps to one isolated Hermes Project
and one project conversation. New Open WebUI chats get separate workspaces;
follow-up requests reuse the same project files and native Hermes session. Recent
chat context is marked unverified; native tools must inspect current disk state.
Background title/follow-up generation does not create projects or execute code.
Use `/files`, `/project` or `/workspace` to retrieve saved file links without an
inference call. `/result` returns the last coding task and its verified outcome
without starting another task (Open WebUI reserves `/status` for its own UI). `/new-session` starts a fresh native conversation in the same
project after a completed/failed task, preserving files and old logs; it refuses
to reset an active task. This recovers from an interrupted native session without
automatically discarding an uncertain execution record.

Native Hermes runs in the existing constrained project container, with the real
project directory mounted at `/workspace`. Files persist under the account's
managed GX10 profile. Jobs use the existing time/turn budgets, usage ledger,
read-before-write protections, task records and local Git checkpoint mechanism.
No remote Git push or repository deletion is added.

The Function polls the job and publishes its final result once. Partial model
prose is not streamed as a successful answer. Failed native execution produces a
Not verified response with the project/log link and any observed saved files.
A creation/modification request with no observed changed program file is also
Not verified. A completed agent turn is not proof of comprehensive testing;
actual terminal commands, exit codes and output are shown separately.

Text artifacts below 1 MB are detected from current regular files, excluding
private files, dependency/build directories and project bookkeeping. Terminal
results and file hashes are recorded in the completion response. The result is
cached so later tasks cannot change the recorded outcome. Links open the current
file, which may have changed since that task. Larger/binary files remain in the
workspace but are not listed by this text-artifact view.

`/project-file/?project_id=...&path=...` opens the file viewer. It uses the signed-in
Open WebUI session to call the existing owner-checked project read API, renders
file content as text, and allows a local download. URLs contain no account token
or public file ticket. Another account cannot read the project via a shared URL.

Deploy `coding.py` alongside the bridge and install the updated router, file.html
and file.js into Open WebUI. Restart the bridge and Open WebUI while idle, then run
`python3 deploy/install_openwebui_pipe.py --coder`. The installer backs up existing
configuration and preserves the SuperCoder preset's name/metadata/access while
changing its base model to `hermes_coder`. Old chat messages are not rewritten.

Tests: `python -m unittest test_coding test_provider_tools test_agent_contracts`.
Native integration also requires Hermes, Docker and a functioning inference provider.
The 600-second job budget and 16-turn limit remain in place. Individual buffered
provider calls allow 180 seconds so larger code responses do not trigger premature
retries. Missing output budgets default to 4096 tokens; explicit client budgets are
preserved. Project context is capped at 65,536 tokens, which meets Hermes’s 64K
minimum. The cap reduces memory pressure but does not guarantee arbitrary models fit.

A new Open WebUI chat no longer implies a new project. Distinctive project-name
words select a unique owned project; follow-ups retain the mapped project.
Ambiguous requests and requests to build a different application pause before
execution. Reply `/use-project Project name` (or its ID for duplicate names) or
`/new-project New name`; the pending original task then runs. These are conservative
name/context rules, not an inference-based semantic classifier. Existing files
and mappings are preserved.

Automatic project names use one bounded request to the configured model (15 seconds,
80 output tokens). Only validated short names are accepted; failure uses a concise
local fallback. Explicit short names are preserved. Naming usage is recorded through
the existing credit tracker. Existing workspace paths are not moved automatically.

Progress emits a six-second heartbeat: elapsed time, time since activity, tool-result
and failure counts, and the pending tool target. Tool-result events are persisted
immediately rather than waiting for the next model response. Final replies show the
latest actual check rather than a later cleanup command. No percentage is invented.

Completion validation checks requested artifacts, observed code files and actual
successful checks. Test-pass claims need a recorded successful test command.
Explicit JavaScript handler-preservation requests also compare named functions
before and after the task; this is a conservative structural check, not a full
semantic equivalence proof. Dot-relative paths normalize to the same saved file.
Old assistant claims are excluded from historical context. `/new-session` also
starts a history boundary so earlier user tasks are not reintroduced.

For explicit patch-only instructions, whole-file writes are withheld and terminal
calls are restricted to listed read/check commands. Native structured calls and
text-envelope calls must match offered tools and enum arguments before Hermes can
execute them. This blocks common shell-write fallbacks; it is not a security sandbox
for arbitrary user-authored test programs. Existing read-before-write protections,
container isolation and bounded file-edit recovery remain enabled.

MundusX coding requests keep source-copy repetition penalties neutral. Its MLX
profile originally recognized VS Code’s file tools but missed Hermes’s `patch`
name. Coding requests require actual tool work until an observed check succeeds;
ordinary read-only requests retain normal final-answer behavior. Unexecuted code
prose is never evidence that a program was saved.

## Mac contributor recovery

The investigated HTTP 502/internal HTTP 404 was caused by a Metal out-of-memory
error that killed MLX’s generation thread, not a missing URL. `/v1/models` still
returned 200. The bridge now reports the failed dependency without exposing raw
upstream errors or claiming a missing route.

The Mac node runs in GUI launchd domain `ai.mundusx.node-agent`, with its existing
identity and an absolute `OPENGPU_HOME`. This lets macOS Keychain supply the existing
signing secret; launching the same agent from SSH produced invalid signatures.
No new contributor identity or credit ownership was created.

`deploy/mlx_runtime_health.py <installed mlx_lm/server.py>` applies a backup-first,
idempotent patch for the inspected MLX version: dead generation threads make model
discovery return 503, prompt/decode concurrency default to one, prefill steps default
to 512, and reusable allocator cache is limited to 1 GiB. The node’s `max_jobs` is one.
The generated `measured_startup.py` wrapper is overwritten by the node agent, so the
patch targets installed `mlx_lm/server.py`. Recheck it after an MLX upgrade.

`deploy/mac_runtime_watchdog.py` runs through GUI LaunchAgent
`ai.mundusx.runtime-watchdog`. It checks local runtime health every ten seconds,
recovers after three explicit dead-worker responses or six network failures,
allows cold startup five minutes, and enforces a five-minute recovery cooldown.
It respects paused/disconnected configuration, verifies the worker PID/owner/command
before signaling, and restarts the existing GUI agent without reading credentials.
Use `launchctl bootout gui/$(id -u)/ai.mundusx.runtime-watchdog` and the corresponding
node-agent service when intentionally disabling managed contribution.

Cluster-wide admission based on actual free memory and automatic failover to another
contributor still require control-plane work and validation. Local recovery does not
establish multi-node failover. Do not count failed inference as a successful code edit.

## Integration validation

Browser testing exercised project reuse from a second chat, actual native file tools,
progress, failed checks, result retrieval, secure Git routing and public
clone/fetch/fast-forward pull in a separate Git Regression project. An isolated Git
fixture exercised code updates, two commits, branch/merge, a local bare-remote push
and clone. No company remote was modified. Private push/PR was not exercised because
this HTTP deployment has no saved connection and disables token entry.

The generated enrollment application required manual repair: complete handlers were
recovered from a recorded successful native write, then the collection GET guard,
data-directory creation, startup sequencing and HTTP suite were repaired. The
project contains an explicit manual-repair record and local Git checkpoint. Its
HTTP suite passed eight tests; `deploy/verify_enrollment.js` passed 23 independent
HTTP/persistence checks in a read-only container. This does not establish that the
model can autonomously complete arbitrary applications. Passing a syntax/import
check alone does not prove the requested API is functional.

Runtime recovery now pauses contributor assignments before terminating an unhealthy MLX worker, waits for memory release, and launches the same validated runtime command. It resumes only after a nonempty generation probe. Failed recovery stays paused. Manual pauses or configuration changes during recovery are preserved. This does not reassign or retry interrupted control-plane jobs.

Stalled inference: runtime health returns 503 when a GPU batch is blocked for over 120 seconds or an active completion exceeds 240 seconds. Three failed watchdog observations trigger pause/recovery. Idle workers are excluded; requests are removed from tracking even on errors. These are bounded operational deadlines, not proof of a GPU fault.

For a verified Qwen3-Coder MLX upstream with the installed OpenGPU native guard, set `model_tool_profile` to `qwen3-coder-mlx` in private settings. This opts Hermes read_file/patch/terminal requests into serial native tool termination and registered-name opening; other providers remain unchanged. Do not enable this on an unverified backend. After an observed check fails, the bridge permits a final blocker report rather than forcing repeated tool calls; failed checks still cannot certify completion.

For explicit patch-only JavaScript requests that name `node --check`, the bridge ends tool use after full saved-file readback and successful checks for every named file (plus `git diff --stat` when requested). A later edit attempt invalidates that evidence. The final report remains limited to those checks; this does not certify database behavior or all application requirements.
