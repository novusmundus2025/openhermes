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

Tests: `python -m unittest test_coding test_agent_contracts`. The provider adapter retries a tool-calls finish reason with no actual call once,
then converts it to a verified protocol failure rather than displaying success
prose. Each inference HTTP call is capped at 90 seconds within the existing
600-second agent budget. Runtime integration also requires native Hermes, Docker
and an available inference provider.


Project routing: a new Open WebUI chat no longer implies a new project. Distinctive project-name words select a unique owned project; follow-ups retain the mapped project. Ambiguous requests and requests to build a different application pause before execution. Reply `/use-project Project name` (or its ID for duplicate names) or `/new-project New name`; the pending original task then runs. A new account with no projects gets its first workspace automatically. These are conservative name/context rules, not an inference-based semantic classifier. Existing mappings and project files are preserved.

Automatic new-project names use one bounded request to the configured model (15 seconds, 80 output tokens). Only validated short names are accepted. Failure uses a concise local fallback; explicit short `/new-project` names are preserved. Naming usage is recorded through the existing credit tracker. Existing workspace paths are not moved automatically.


SuperCoder progress now emits a status heartbeat every six seconds with elapsed time, time since the last event, completed tool-result count, failure count, and the currently pending tool target. It does not invent percentages or stream unverified success claims.

A provider-specific failure was reproduced on 2026-10-20: ordinary text completions returned HTTP 200, but native tool completions returned upstream HTTP 502 with an internal completion endpoint HTTP 404. The bridge maps this exact missing-dependency signature to HTTP 424 so Hermes does not spend minutes retrying a missing route. Other provider failures retain their status. Only a fixed, safe diagnostic is returned; raw provider error bodies are not exposed. The final chat explains the dependency failure and keeps the same project available for retry.

Validation on GX10: 15 server tests, 22 coding/function tests, and 51 Git/recovery/project/provider tests passed. Browser tests verified the elapsed-time status history, clear dependency failure, `/result`, secure `/git` routing, and public clone/fetch/fast-forward pull using octocat/Hello-World in the separate Git Regression project. An isolated execution fixture also passed Node code creation, code update, validation checks, two commits, a feature branch, fast-forward merge, push to a temporary local bare repository, and clone/read-back. No company remote was modified. Private push/PR was not exercised: the current HTTP deployment has no saved connection and correctly disables token entry.

This does **not** establish successful end-to-end Hermes code generation: the real provider dependency is still unavailable. A text-only protocol workaround was tested, produced malformed/unreliable output, and was discarded. Repair the native tool endpoint on the inference-provider server before rerunning real agent creation, edit, tests, and checkpoint validation.
