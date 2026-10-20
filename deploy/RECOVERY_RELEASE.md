# Contributor Recovery 0.1.0-rc.1

This is a recovery-component release candidate, not a stable release of the
complete OpenHermes application. It does not include uncommitted bridge/coder
work or claim parity with every live-server customization.

## Changes

- Mac MLX: bounded concurrency/cache, dead-worker and stalled-request health checks.
- Watchdog: pause assignments, stop/reload inference, verify generation, resume.
- vLLM/CUDA, llama.cpp and Ollama recovery adapters with explicit model/service targets.
- Preserve manual pauses and disconnection; failed recovery stays paused.
- Docker identity validation and one-controller lock in the generic watchdog.
- Config examples disabled by default, deployment instructions and regression tests.

## Validation

- 19 regression tests passed on Windows and GX10 Linux.
- Mac MLX restarted and a real generation probe succeeded after activation.
- Generic adapters have controlled-fixture coverage; live crash/recovery tests for
  vLLM, llama.cpp and Ollama are still outstanding.
- GitHub Actions adds Linux/macOS Python 3.11/3.12 coverage. Its results must be
  reviewed before publishing this candidate.

## Install and activation

See CONTRIBUTOR_RECOVERY.md in this package. Verify checksums first. Configure one
watchdog per contributor. Generic configs are disabled until exact model, endpoint,
runtime and contributor service targets are verified. GX10 was paused/disconnected;
this release preserves that state and does not enable its generic adapters.

The MLX installed-source patch must be reapplied after upgrading mlx-lm. Back up
the installed server source and current watchdog/config before activation. Stop the
watchdog, restore those backups, then restart only the affected contributor to roll
back. Keep a contributor paused if generation remains unhealthy.

## Known limits / stable-release gates

- Reassigning interrupted jobs and cluster-wide available-memory admission are not included.
- Generic live backend recovery and manual-pause behavior must be verified per deployment.
- Recovery deadlines bound latency; a timeout alone does not prove hardware failure.
- The app's stalled coding/folder-restructuring scenario is not certified as resolved.
- Reconcile and test uncommitted bridge/coder and live-only Git/runtime changes before
  releasing the whole OpenHermes stack. Do not use this package to overwrite that stack.
