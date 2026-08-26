#!/usr/bin/env bash
# Run ON the VPS as ubuntu (or adjust USER/HOME below).
# Usage:
#   curl -fsSL ... | bash   OR
#   bash scripts/vps_setup.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/botirirmanovv/aave-liquidation-monitor-v4.git}"
APP_DIR="${APP_DIR:-$HOME/aave-liquidation-monitor-v4}"
BRANCH="${BRANCH:-main}"

echo "==> apt update + python"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 python3-venv python3-pip git curl ca-certificates

if [[ ! -d "$APP_DIR/.git" ]]; then
  echo "==> clone $REPO_URL"
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  echo "==> pull $BRANCH"
  git -C "$APP_DIR" fetch origin
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
fi

cd "$APP_DIR"
echo "==> venv + deps"
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt

if [[ ! -f .env ]]; then
  echo
  echo "WARNING: $APP_DIR/.env missing."
  echo "Copy .env from your PC (scp), then:"
  echo "  sudo cp $APP_DIR/scripts/aave-monitor.service /etc/systemd/system/"
  echo "  sudo systemctl daemon-reload"
  echo "  sudo systemctl enable --now aave-monitor"
  echo
  exit 0
fi

echo "==> systemd unit"
sudo cp "$APP_DIR/scripts/aave-monitor.service" /etc/systemd/system/aave-monitor.service
# Fix paths if not ubuntu home
if [[ "$APP_DIR" != "/home/ubuntu/aave-liquidation-monitor-v4" ]]; then
  sudo sed -i "s|/home/ubuntu/aave-liquidation-monitor-v4|$APP_DIR|g" /etc/systemd/system/aave-monitor.service
  sudo sed -i "s|User=ubuntu|User=$(whoami)|g" /etc/systemd/system/aave-monitor.service
fi
sudo systemctl daemon-reload
sudo systemctl enable --now aave-monitor
sudo systemctl --no-pager status aave-monitor || true
echo
echo "Done. Logs: journalctl -u aave-monitor -f"
echo "Or:        tail -f $APP_DIR/monitor_systemd.log"
