# Contributor runtime recovery

The existing Mac MLX watchdog remains enabled. The backend-neutral watchdog adds
vLLM (including CUDA), llama.cpp and Ollama adapters. CUDA is a compute backend;
select the adapter for the server using it.

Each adapter checks actual generation, not only an HTTP health response. Three
consecutive failed probes trigger admission pause, a runtime stop/start, bounded
startup verification and contributor-agent restart. Probe deadlines and intervals
are configurable. This bounds service latency; under heavy load a deadline does
not establish that hardware is broken. Repeated recovery has a five-minute
cooldown. Failed recovery remains paused. Manual pauses, disconnection and
configuration changes during recovery are preserved. Files and remote Git are
not touched. Interrupted control-plane jobs are not reassigned by this watchdog.

Run only one watchdog per contributor. The generic controller takes a file lock.
Do not run it alongside the Mac-specific MLX watchdog on the same contributor.

## Install

Use one of `deploy/examples/{vllm,llamacpp,ollama}-watchdog.json`. Fill the exact
model, loopback endpoint, contributor config and runtime/agent service names.
Examples are disabled until explicitly configured. Never point runtime stop/start
at a container whose entrypoint only sleeps: that cannot restart an inference
process launched later through docker exec.

Use `systemd-user`, `systemd` or `docker` lifecycle managers. For Docker, provide
an exact inspected `container_id` for each Docker-managed target; identity is
checked before every operation. For system-level units, the watchdog account
must already have permission to manage those specific units. No sudo or privilege
configuration is installed automatically. Use a private `api_key_file` if needed.

From the checkout, verify the configured runtime first:

```sh
python3 -m deploy.contributor_watchdog /path/to/runtime-watchdog.json --check
```

The check performs a small generation using the named installed model. It does
not download a model. Only test when capacity is available. With verified service
names, set enabled=true and install `deploy/contributor-watchdog.service` under
`~/.config/systemd/user/`; adapt its checkout/config paths to the host. Then run
`systemctl --user daemon-reload` and `systemctl --user enable --now contributor-watchdog`.
Use the service journal and adjacent `.state.json` file for recovery outcomes.

## Current inventory

GX10's contributor configuration was manually paused and disconnected when
inspected. Ollama has hermes3:8b and hermes3:70b installed. Its three vLLM image
containers have sleep-only entrypoints and no discovered serving endpoint.
The generic implementation and examples are installed there but are not enabled
against these targets. The existing manual pause was preserved. vLLM/llama.cpp/
Ollama live crash recovery has not been demonstrated on these hosts. Adapter and
recovery-state tests cover these paths with controlled fixtures.

API references: [vLLM](https://docs.vllm.ai/en/latest/serving/online_serving/),
[llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md),
[Ollama](https://docs.ollama.com/api/generate).
