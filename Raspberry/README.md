# Irrigation Control System — Raspberry Pi

Flask-based service that controls an irrigation system via MQTT and a real-time web UI.

## Hardware

- ESP32-C3 firmware (see `../ESP32/README.md`) running on the local WiFi network
- Two PCF8574A I2C relay expanders on the ESP32 side
  - PCF #1 (0x38): Box 1 (v1–v3) + Box 2 (v1–v3)
  - PCF #2 (0x3C): Box 3 (v1–v3) + Box 4 (v1–v3)
- 4 boxes × 3 valves = 12 relay outputs + 1 pump (direct GPIO)

## Tech Stack

- Flask + Flask-SocketIO — web UI with real-time WebSocket updates
- paho-mqtt 2.x — MQTT client
- APScheduler — cron-based irrigation schedules. While a script runs, the state of
  every open valve and the pump is re-published every `KEEPALIVE_INTERVAL_S` (5 min)
  so a command lost during an ESP32 reboot self-heals and the firmware's pump
  dry-run timer stays refreshed.
- SQLite — scripts, schedules, message log
- python-dotenv — credentials loaded from `.env`
- Bootstrap 5.3 + vanilla JS — frontend

## MQTT Topics

| Topic | Direction | Values |
|---|---|---|
| `irrigation/box/{1-4}/valve/{1-3}/set` | → publish | `ON` / `OFF` (QoS 1) |
| `irrigation/box/{1-4}/valve/{1-3}/state` | ← subscribe | `ON` / `OFF` |
| `irrigation/pump/set` | → publish | `ON` / `OFF` (QoS 1) |
| `irrigation/pump/state` | ← subscribe | `ON` / `OFF` |
| `irrigation/status` | ← subscribe | `online` / `offline` |
| `irrigation/heartbeat` | ← subscribe | JSON |

MQTT broker: Mosquitto on `localhost:1883`.

## Firmware serving

The ESP32's OTA binary is served **statically by the Apache already running on
port 80**, from the directory named by `firmware.dir` in `config.yaml`
(`/var/www/html/firmware` on the Pi — already writable by `openhabian` and
readable by `www-data`, so no sudo is involved). Flask keeps `/firmware/upload`,
which writes the image atomically and stamps `firmware.host`/`firmware.port`
into `manifest.json` so the ESP32 downloads from Apache.

This is not cosmetic: the Werkzeug dev server truncates a 1 MB download to a
client as slow as the ESP32, and the flash then fails its checksum and rolls
back. Overrides: `FIRMWARE_DIR`, `FIRMWARE_HOST`, `FIRMWARE_PORT`.

## Project Structure

```
app/
  __init__.py       App factory — loads config + dotenv, starts MQTT + scheduler
  routes.py         Flask blueprints (dashboard, scripts, schedules, log APIs)
  mqtt_client.py    paho-mqtt wrapper, state tracking
  scheduler.py      APScheduler wrapper, irrigation script execution
  database.py       SQLite helpers (scripts, schedules, message log)
templates/          dashboard, scripts, schedules, log pages
static/css/
config.yaml         Broker host/port, system dimensions, firmware dir (no secrets)
.env                Credentials — gitignored, copy from .env.example
.env.example        Credential template
run.py              Entrypoint
irrigation.service  systemd unit
setup.sh            One-shot install script for Pi (fresh builds only)
requirements.txt
requirements-dev.txt  Test-only dependencies
tests/              pytest suite — stubbed MQTT, temp DB, no hardware
```

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests/ -q
```

Safe to run anywhere: every test uses a stub MQTT client and a throwaway
database, so nothing reaches a broker or a valve. Takes about 30 s — the
scheduler tests step through real one-second waits because that is the tick the
keepalive and the controller sampling run on.

## Setup

### 1. Credentials

Copy the example and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:

```
MQTT_USER=your-mqtt-username
MQTT_PASS=your-mqtt-password
SECRET_KEY=generate-a-random-string-here
```

`.env` is gitignored and will never be committed.

### 2. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Or run `bash setup.sh` on a Raspberry Pi for a full automated install.

### 3. Run

```bash
python run.py
```

Web UI: http://localhost:5000

## Raspberry Pi Deployment

**Host:** `raspi4server.local` · **User:** `openhabian` · **Path:** `/home/openhabian/irrigation/`

### Initial install

> `setup.sh` has the `pi` user hardcoded — run the steps manually instead:

```bash
# On the Pi
cd ~/irrigation
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
sudo cp irrigation.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now irrigation
```

### Deploy code update

```bash
# From the Mac — sync files, update packages, restart
rsync -av --exclude='.claude' /Users/dejmekz/Projects/Irrigation/Raspberry/ openhabian@raspi4server.local:~/irrigation/
ssh openhabian@raspi4server.local "~/irrigation/venv/bin/pip install --upgrade -r ~/irrigation/requirements.txt"
ssh openhabian@raspi4server.local "sudo systemctl restart irrigation"
```

### Update Python packages only

```bash
ssh openhabian@raspi4server.local "~/irrigation/venv/bin/pip install --upgrade -r ~/irrigation/requirements.txt"
ssh openhabian@raspi4server.local "sudo systemctl restart irrigation"
```

### Useful commands on Pi

```bash
sudo systemctl status irrigation
sudo journalctl -u irrigation -f
mosquitto_pub -t irrigation/box/1/valve/1/set -m ON
mosquitto_sub -t 'irrigation/#'
```

## Test Environment (Debian VM)

A Debian 13 (Trixie) ARM64 VM is used for development and testing.

- **SSH:** `ssh dejmekz@debian`
- **App URL:** http://10.77.2.141:5000
- **App path:** `/home/dejmekz/irrigation/`

### Deploy update to VM

```bash
rsync -av --exclude='.claude' /Users/dejmekz/Projects/Irrigation/Raspberry/ dejmekz@debian:~/irrigation/
ssh dejmekz@debian "sudo systemctl restart irrigation"
```

### Useful commands on VM

```bash
# View live logs
sudo journalctl -u irrigation -f

# Service control
sudo systemctl restart irrigation
sudo systemctl status irrigation

# MQTT test
mosquitto_pub -t irrigation/box/1/valve/1/set -m ON
mosquitto_sub -t 'irrigation/#'
```

### Services (both auto-start on boot)

- `mosquitto` — MQTT broker
- `irrigation` — Flask app
