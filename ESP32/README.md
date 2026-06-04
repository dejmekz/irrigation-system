# Irrigation Controller — ESP32 Firmware

ESP32-C3 firmware for the irrigation system. Controls 4 boxes × 3 valves via two PCF8574A I2C relay expanders, a direct-GPIO pump output, a DS3231 RTC, and a 20×4 I2C LCD. Connects to the Raspberry Pi controller over MQTT via WiFi.

## Hardware

| Component | Detail |
|---|---|
| MCU | ESP32-C3 DevKitM-1 |
| Relay expander #1 | PCF8574A @ 0x38 — Box 1 (bits 0–2) + Box 2 (bits 3–5) |
| Relay expander #2 | PCF8574A @ 0x3C — Box 3 (bits 0–2) + Box 4 (bits 3–5) |
| Pump | Direct GPIO (pin 6), active-LOW |
| RTC | DS3231 @ I2C |
| LCD | 20×4 I2C @ 0x27 |
| I2C bus | SDA = GPIO5, SCL = GPIO4 |

Relay boards are **active-LOW** (`RELAY_ACTIVE_LOW true` in `config.h`).

## MQTT Topics

| Topic | Direction | Values | Notes |
|---|---|---|---|
| `irrigation/box/{1-4}/valve/{1-3}/set` | ← subscribe | `ON` / `OFF` | Command from Pi |
| `irrigation/box/{1-4}/valve/{1-3}/state` | → publish | `ON` / `OFF` | Retained state |
| `irrigation/pump/set` | ← subscribe | `ON` / `OFF` | Command from Pi |
| `irrigation/pump/state` | → publish | `ON` / `OFF` | Retained state |
| `irrigation/cmd/stop_all` | ← subscribe | any | Emergency stop |
| `irrigation/status` | → publish | `online` / `offline` | LWT |
| `irrigation/heartbeat` | → publish | JSON | Every 5 minutes |

## Safety Features

- **WiFi safety shutoff** — if WiFi is lost while any valve or pump is active and stays lost for `WIFI_ACTIVE_SAFETY_MS` (30 s), all outputs are turned off automatically.
- **Hardware task watchdog** — reboots the ESP32 if `loop()` stalls for longer than `TASK_WDT_TIMEOUT_S` (60 s).
- **Retained MQTT state** — all state topics are published as retained messages; after reconnect the Pi immediately receives current state.

## Project Setup

### 1. Credentials

Copy the example secrets file and fill in your values:

```bash
cp src/secrets.h.example src/secrets.h
```

Edit `src/secrets.h`:

```c
#define WIFI_SSID     "your-wifi-ssid"
#define WIFI_PASS     "your-wifi-password"
#define MQTT_USER     "your-mqtt-username"
#define MQTT_PASSWORD "your-mqtt-password"
```

`src/secrets.h` is gitignored and will never be committed.

### 2. Build & Flash (PlatformIO)

```bash
# Build
pio run

# Flash
pio run --target upload

# Serial monitor
pio device monitor
```

Or use the PlatformIO IDE extension in VS Code.

## Project Structure

```
src/
  main.cpp       Main firmware — WiFi, MQTT, PCF relay, LCD, loop logic
  config.h       Hardware constants and timing (no secrets)
  secrets.h      WiFi + MQTT credentials (gitignored)
  secrets.h.example  Template for secrets.h
platformio.ini   Board, framework, library dependencies
```

## Configuration (`config.h`)

Key constants — edit here rather than in code:

| Constant | Default | Description |
|---|---|---|
| `RELAY_ACTIVE_LOW` | `true` | Relay board polarity |
| `WIFI_RETRY_INTERVAL_MS` | 10 000 | Interval between WiFi reconnect attempts |
| `WIFI_ACTIVE_SAFETY_MS` | 30 000 | Max WiFi-loss duration before emergency stop |
| `MQTT_RETRY_INTERVAL_MS` | 5 000 | Interval between MQTT reconnect attempts |
| `HEARTBEAT_INTERVAL_MS` | 300 000 | Heartbeat publish interval (5 min) |
| `TASK_WDT_TIMEOUT_S` | 60 | Hardware watchdog timeout |
