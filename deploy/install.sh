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

[ -f .env ] || { cp deploy/env.example .env; chmod 600 .env; echo "Created .env, edit it"; }

sudo cp deploy/snore-*.service deploy/snore-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now snore-record.timer snore-analyze.timer
systemctl list-timers 'snore-*'
