# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Two-component irrigation system communicating over MQTT:
- **`ESP32/`** — C++/Arduino firmware for ESP32-C3 (PlatformIO). Controls 4 boxes × 3 valves via two PCF8574A I2C relay expanders + one direct-GPIO pump.
- **`Raspberry/`** — Python/Flask web controller. Sends MQTT commands, runs cron-scheduled irrigation scripts, exposes a real-time web UI.

MQTT broker (Mosquitto) runs on the Raspberry Pi at `localhost:1883`. The ESP32 connects to it over WiFi.

## ESP32 Commands (PlatformIO)

```bash
cd ESP32
pio run                       # build
pio run --target upload       # flash
pio device monitor            # serial monitor (115200 baud)
```

Before first build, copy credentials:
```bash
cp src/secrets.h.example src/secrets.h  # fill in WIFI_SSID, WIFI_PASS, MQTT_USER, MQTT_PASSWORD
```

Hardware constants (pin numbers, timing, polarity) live in `src/config.h`. Credentials are in `src/secrets.h` (gitignored).

## Raspberry Pi Commands

```bash
cd Raspberry
cp .env.example .env          # fill in MQTT_USER, MQTT_PASS, SECRET_KEY
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py                  # starts on port 5000 (or config.yaml web.port)
```

No test suite exists.

## Raspberry Pi Architecture

The Flask app factory (`app/__init__.py`) wires together three long-lived singletons stored in `app.extensions`:

- **`mqtt`** (`MQTTClient`) — paho-mqtt wrapper. Tracks live valve/pump state in `self.state` dict. Emits `state_update` / `mqtt_status` SocketIO events on incoming messages. All outbound publishes also call `log_message()`.
- **`scheduler`** (`IrrigationScheduler`) — APScheduler wrapping cron-based schedules. `run_script()` executes a script's step list in a daemon thread, with a `_stop_event` for cancellation. Pump start/stop wraps the entire script if `pump_box` is set.
- **`database`** (`database.py`) — plain sqlite3 helpers for three tables: `scripts` (step lists as JSON), `schedules` (cron strings referencing scripts), `message_log` (capped at 1000 rows).

Flask-SocketIO (`async_mode='threading'`) pushes real-time updates to the browser for state changes, log entries, and script progress.

Script steps are JSON arrays of `{action, box, valve, duration}` objects. Supported actions: `valve_on`, `valve_off`, `pump_on`, `pump_off`, `parallel_group` (with nested `actions` list).

## MQTT Topic Map

| Topic | ESP32 | Pi |
|---|---|---|
| `irrigation/box/{1-4}/valve/{1-3}/set` | subscribes | publishes |
| `irrigation/box/{1-4}/valve/{1-3}/state` | publishes (retained) | subscribes |
| `irrigation/pump/set` | subscribes | publishes |
| `irrigation/pump/state` | publishes (retained) | subscribes |
| `irrigation/cmd/stop_all` | subscribes (emergency stop) | publishes |
| `irrigation/status` | publishes LWT | subscribes |
| `irrigation/heartbeat` | publishes every 5 min | subscribes |

## Development VM

A Debian 13 ARM64 VM (`dejmekz@debian`) mirrors the Pi environment.

```bash
# Deploy and restart
rsync -av --exclude='.claude' Raspberry/ dejmekz@debian:~/irrigation/
ssh dejmekz@debian "sudo systemctl restart irrigation"

# Logs
ssh dejmekz@debian "sudo journalctl -u irrigation -f"

# MQTT testing on VM
mosquitto_pub -t irrigation/box/1/valve/1/set -m ON
mosquitto_sub -t 'irrigation/#'
```

App URL on VM: http://10.77.2.141:5000
