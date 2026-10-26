#!/usr/bin/env bash
# Run on the Pi from ~/snore-tracker. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv sync --no-dev

mkdir -p models
[ -f models/yamnet.tflite ] || curl -fsSL -o models/yamnet.tflite \
    "$(uv run python -c 'from snore_tracker.model import MODEL_URL; print(MODEL_URL)')"
echo "4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf  models/yamnet.tflite" \
    | sha256sum -c -

[ -f .env ] || { cp deploy/env.example .env; chmod 600 .env; echo "Created .env, edit it"; }

sudo cp deploy/asound.conf /etc/asound.conf  # dsnoop "snoop": recorder + web UI share the mic
sudo rm -f /etc/polkit-1/rules.d/50-snore-record.rules  # renamed to 50-snore.rules
sudo cp deploy/polkit/50-snore.rules /etc/polkit-1/rules.d/
# Away from home, route posts through the wg-home tunnel (README: Setup)
sudo install -m 755 -o root -g root deploy/nm/50-wg-away /etc/NetworkManager/dispatcher.d/
# The noon git pull is replaced by the n8n webhook (POST /api/deploy)
sudo systemctl disable --now snore-update.timer 2>/dev/null || true
sudo rm -f /etc/systemd/system/snore-update.service /etc/systemd/system/snore-update.timer
sudo cp deploy/snore-*.service deploy/snore-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
# Older installs enabled snore-record.service for boot resume; snore-record-boot does that now
sudo systemctl disable snore-record.service 2>/dev/null || true
sudo systemctl enable snore-record-boot.service  # boot-time resume, only inside the window
sudo systemctl enable --now snore-record.timer snore-analyze.timer snore-post.timer
sudo systemctl enable snore-web.service
sudo systemctl restart snore-web.service  # start, or pick up a changed unit or code
systemctl list-timers 'snore-*'
