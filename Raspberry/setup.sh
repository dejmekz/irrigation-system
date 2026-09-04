#!/bin/bash
# One-time install of the irrigation controller on a fresh Pi.
#
# Day-to-day updates do NOT use this script — see README.md for the rsync flow.
# This exists for a rebuild, which is exactly when a wrong path or user is
# hardest to spot, so the values below must match irrigation.service.

set -euo pipefail

# Kept in step with irrigation.service; change both together.
SERVICE_USER="openhabian"
INSTALL_DIR="/home/${SERVICE_USER}/irrigation"
SERVICE_FILE="irrigation.service"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "User '$SERVICE_USER' does not exist. Edit SERVICE_USER in this script and"
  echo "in $SERVICE_FILE so the two agree, then re-run." >&2
  exit 1
fi

if ! grep -q "^User=${SERVICE_USER}$" "${SRC_DIR}/${SERVICE_FILE}"; then
  echo "${SERVICE_FILE} does not run as '${SERVICE_USER}'. Refusing to install a" >&2
  echo "unit whose user disagrees with INSTALL_DIR." >&2
  exit 1
fi

echo "=== Installing Mosquitto MQTT broker ==="
sudo apt-get update -q
sudo apt-get install -y mosquitto mosquitto-clients rsync

echo "=== Copying project files to ${INSTALL_DIR} ==="
sudo mkdir -p "$INSTALL_DIR"
# Deliberately not `cp -r .`: that would drag in the venv and clobber an
# existing .env and database on a re-run. No --delete, for the same reason.
sudo rsync -a \
  --exclude='.env' --exclude='*.db' --exclude='*.sqlite' \
  --exclude='venv/' --exclude='.venv/' --exclude='__pycache__/' \
  --exclude='.claude/' --exclude='tests/' \
  "${SRC_DIR}/" "${INSTALL_DIR}/"
sudo chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_DIR"

if [ ! -f "${INSTALL_DIR}/.env" ]; then
  echo "=== Creating .env from the example ==="
  sudo -u "$SERVICE_USER" cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
  sudo chmod 600 "${INSTALL_DIR}/.env"
  NEEDS_ENV=1
fi

echo "=== Creating Python virtual environment ==="
sudo -u "$SERVICE_USER" python3 -m venv "${INSTALL_DIR}/venv"
sudo -u "$SERVICE_USER" "${INSTALL_DIR}/venv/bin/pip" install --upgrade pip -q
sudo -u "$SERVICE_USER" "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q

echo "=== Installing systemd service ==="
sudo cp "${SRC_DIR}/${SERVICE_FILE}" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable irrigation

if [ "${NEEDS_ENV:-0}" = "1" ]; then
  echo ""
  echo "=== Fill in credentials before starting ==="
  echo "  sudo -u ${SERVICE_USER} nano ${INSTALL_DIR}/.env"
  echo "  sudo systemctl start irrigation"
  echo ""
  echo "The unit uses EnvironmentFile=, so it will refuse to start until .env"
  echo "exists — starting with empty MQTT credentials would silently fail to"
  echo "reach the broker, and nothing would water."
  exit 0
fi

sudo systemctl restart irrigation
echo ""
echo "=== Done! ==="
sudo systemctl status irrigation --no-pager
echo ""
echo "Web UI: http://$(hostname -I | awk '{print $1}'):5000"
echo ""
echo "  sudo systemctl status irrigation   # check status"
echo "  sudo journalctl -u irrigation -f   # live logs"
echo "  sudo systemctl restart irrigation  # restart"
