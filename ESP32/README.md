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
| `irrigation/box/{1-4}/valve/{1-3}/set` | ← subscribe | `ON` / `OFF` | Command from Pi, QoS 1 |
| `irrigation/box/{1-4}/valve/{1-3}/state` | → publish | `ON` / `OFF` | Retained state |
| `irrigation/pump/set` | ← subscribe | `ON` / `OFF` | Command from Pi, QoS 1 |
| `irrigation/pump/state` | → publish | `ON` / `OFF` | Retained state |
| `irrigation/cmd/stop_all` | ← subscribe | any | Emergency stop, QoS 1 |
| `irrigation/cmd/ota_update` | ← subscribe | any | Trigger OTA firmware update |
| `irrigation/status` | → publish | `online` / `offline` | LWT |
| `irrigation/heartbeat` | → publish | JSON | Every 5 minutes |

## OTA Firmware Updates

Firmware can be updated over WiFi without a USB connection. The ESP32 checks the Pi's manifest and flashes the new binary when told to.

### How it works

1. Both `manifest.json` and `irrigation.bin` live in a directory served **statically by Apache** on port 80 (`/var/www/html/firmware/` on the Pi), configured under `firmware:` in `config.yaml`. They are deliberately *not* served by Flask: the Werkzeug dev server truncates a 1 MB download to a client as slow as the ESP32, and the image then fails its checksum and rolls back. Flask still owns `/firmware/upload` and `/firmware/trigger`.
2. On `irrigation/cmd/ota_update`, the ESP32 fetches `OTA_MANIFEST_URL` and compares its `FIRMWARE_VERSION` against the manifest. If the manifest version is higher, it downloads the binary from the manifest's `host`/`port` and flashes it, then reboots.

### Deploy workflow

```bash
# 1. Bump FIRMWARE_VERSION in src/config.h (e.g. 1 → 2)
# 2. Build
cd ESP32 && pio run

# 3. Upload binary to Pi (writes to the Apache-served dir, bumps the manifest
#    version and re-stamps its host/port)
curl -F "firmware=@.pio/build/esp32c3/firmware.bin" \
     http://raspi4server.local:5000/firmware/upload

# 4. Trigger the update
curl -X POST http://raspi4server.local:5000/firmware/trigger
# or via MQTT directly:
mosquitto_pub -h raspi4server.local -t irrigation/cmd/ota_update -m trigger
```

Trigger the OTA on a **quiet server** — the manifest is small, but avoid downloading the binary yourself or leaving the dashboard polling while the flash runs.

During the update the LCD shows **"OTA: updating… Do not power off"**. The ESP32 reboots automatically on success. If the update fails it reboots back to the existing firmware.

### Pi endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/firmware/manifest.json` | Manifest (fallback; the ESP32 reads Apache's copy) |
| `GET` | `/firmware/irrigation.bin` | Binary (fallback only — **not** the OTA path) |
| `POST` | `/firmware/upload` | Upload new `.bin` (field: `firmware`); sets the manifest version from the image |
| `POST` | `/firmware/trigger` | Publishes `irrigation/cmd/ota_update` via MQTT |

### Image size ceiling — the image must stay under ~1.00 MB

`Update.begin()` on this device rejects an image of **1,016,768 bytes** and
accepts **1,006,752**, so the usable ceiling sits between the two. That is far
below the 1,310,720 bytes `default.csv` implies, which means the partition table
actually burned on this ESP32-C3 is **not** the one this build assumes. Nothing
in the build warns about it: PlatformIO happily reports "Flash: 74%" against the
partition table it *thinks* is there, and the image links and validates fine.

The failure is silent and easy to misread as a network problem. esp32FOTA opens
the HTTP connection *before* calling `Update.begin()`, so the server logs a
normal 200 and sends whatever fits in the TCP buffers (~89 kB here) before the
device hangs up. On the wire it looks exactly like a truncated download.

**The tell is timing.** A failed flash reboots in about 6 s (`execOTA` returns
immediately, then `delay(3000)` and restart); a real flash takes ~15 s. If a
trigger comes back in single-digit seconds, suspect size, not the network.

`CORE_DEBUG_LEVEL=1` in `platformio.ini` is what currently buys the headroom —
it is worth about 10 kB over level 3, and the build does not fit without it.
Raising it again to debug over serial will silently break OTA. If the image
grows past the ceiling for real, the fix is to reflash a known partition table
over USB rather than to keep shaving bytes.

### Version rule

`FIRMWARE_VERSION` in `config.h` **must be bumped** before each build. esp32FOTA flashes only when the manifest version is strictly greater than the version the device is running, so an unbumped build is a no-op.

The manifest takes its version **from the image itself**. The firmware embeds
`IRRIGATION_FW_VERSION=<n>:END` as plain text (`FW_VERSION_TAG` in `main.cpp`)
and `/firmware/upload` reads it back out, so the manifest always states what is
actually in the file.

It did not always work that way, and the old behaviour is worth knowing if you
meet a stale manifest: upload used to increment a counter of its own, floored at
the version the ESP32 last reported. That counter tracked uploads rather than
firmware, so after a successful flash the manifest sat permanently one ahead of
the device and every subsequent trigger re-flashed an image it already had.
Images built before the marker still take that path — the response says
`"version_source": "counter"` rather than `"image"` when it does.

Note the marker needs a real runtime reference to survive `--gc-sections`;
`__attribute__((used))` alone is not enough on this toolchain, which is why
`setup()` prints it. If you ever stop printing it, check with
`strings firmware.bin | grep IRRIGATION_FW_VERSION` before uploading.

The upload response also reports `will_update`, which is false when the manifest
version is not greater than the running one — i.e. triggering would do nothing.

`manifest.json` is **not** in git — it is server-side state that the upload endpoint rewrites, so a tracked copy only ever drifts.

## Safety Features

- **Valve max-open cap** — every valve is closed automatically once it has been open for `VALVE_MAX_ON_MS` (60 min), and the pump is stopped with it if that was the last open valve. This is the last-resort dead-man's switch: it fires even when WiFi, MQTT and the pump are all healthy, so a crashed or hung Pi controller cannot leave a valve open indefinitely. The timer starts on the OFF→ON transition and is *not* extended by the Pi's keepalive re-publishes. A valve closed this way is **latched**: further `ON` commands are refused (and answered with a retained `OFF` state) until an explicit `OFF`, a `stop_all`, or a reboot clears it, so a keepalive cannot silently reopen it.
- **Pump dry-run shutoff** — if no valve `set` command arrives for `PUMP_SAFETY_TIMEOUT_MS` (30 min) while the pump is running, everything is turned off. The Pi re-asserts the state of open valves every 5 minutes while a script runs, so steps longer than this are not cut short.
- **WiFi safety shutoff** — if WiFi is lost while any valve or pump is active and stays lost for `WIFI_ACTIVE_SAFETY_MS` (30 s), all outputs are turned off automatically.
- **Hardware task watchdog** — reboots the ESP32 if `loop()` stalls for longer than `TASK_WDT_TIMEOUT_S` (60 s).
- **Relay write retry** — a relay change that the I2C bus rejects is retried immediately (3 attempts) and then once per second from `loop()` until it lands. The desired state is held in the shadow register, so a failed close is never quietly forgotten.
- **Honest state reporting** — a valve is only reported over MQTT once the write actually reached the expander. A relay whose write failed keeps being reported as open until the retry confirms otherwise, rather than showing closed while still energised.
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
| `I2C_CLOCK_HZ` | 100 000 | I2C bus speed — lower for long/noisy cables (try 50 000) |
| `LCD_REINIT_INTERVAL_MS` | 60 000 | Period between full LCD reinit (noise recovery) |
| `WIFI_RETRY_INTERVAL_MS` | 10 000 | Interval between WiFi reconnect attempts |
| `WIFI_ACTIVE_SAFETY_MS` | 30 000 | Max WiFi-loss duration before emergency stop |
| `PUMP_SAFETY_TIMEOUT_MS` | 1 800 000 | Pump dry-run timeout (30 min) |
| `VALVE_MAX_ON_MS` | 3 600 000 | Absolute cap on how long one valve may stay open (60 min) |
| `MQTT_RETRY_INTERVAL_MS` | 5 000 | Interval between MQTT reconnect attempts |
| `HEARTBEAT_INTERVAL_MS` | 300 000 | Heartbeat publish interval (5 min) |
| `TASK_WDT_TIMEOUT_S` | 60 | Hardware watchdog timeout |
| `FIRMWARE_VERSION` | 13 | Bump before every OTA release build |
| `OTA_MANIFEST_URL` | `http://raspi4server.local/firmware/manifest.json` | Manifest URL (Apache, port 80) |
