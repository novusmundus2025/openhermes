#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
test -f settings.json || { echo 'Configure settings.json first.' >&2; exit 1; }
# Avoid binding a second gateway over an existing manually launched instance.
if ss -ltn | awk '{print $4}' | grep -Eq ':8642$'; then
  if ! systemctl --user is-active --quiet hermes-gateway; then
    echo 'Port 8642 is already served outside hermes-gateway.service. Keep it or stop it deliberately before using this script.' >&2
    exit 1
  fi
fi
mkdir -p "$HOME/.config/systemd/user"
install -m 644 deploy/hermes-gateway.service "$HOME/.config/systemd/user/"
install -m 644 deploy/openhermes.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now hermes-gateway openhermes
curl --fail --silent http://172.17.0.1:8643/health
echo
