#!/bin/bash
# Run this once on the Raspberry Pi to install and enable the service.

set -e

INSTALL_DIR="/home/pi/irrigation"
SERVICE_FILE="irrigation.service"

echo "=== Installing Mosquitto MQTT broker ==="
sudo apt-get update -q
sudo apt-get install -y mosquitto mosquitto-clients

echo "=== Copying project files ==="
sudo mkdir -p "$INSTALL_DIR"
sudo cp -r . "$INSTALL_DIR"
sudo chown -R pi:pi "$INSTALL_DIR"

echo "=== Creating Python virtual environment ==="
cd "$INSTALL_DIR"
python3 -m venv venv
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q

echo "=== Installing systemd service ==="
sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable irrigation
sudo systemctl start irrigation

echo ""
echo "=== Done! ==="
echo "Service status:"
sudo systemctl status irrigation --no-pager
echo ""
echo "Web UI available at: http://$(hostname -I | awk '{print $1}'):5000"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status irrigation   # check status"
echo "  sudo journalctl -u irrigation -f   # live logs"
echo "  sudo systemctl restart irrigation  # restart"
