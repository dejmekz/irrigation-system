#pragma once
#include "secrets.h"

// ---- MQTT ----
#define MQTT_HOST             "raspi4server.local"
#define MQTT_PORT             1883
#define MQTT_CLIENT_ID        "irrigation_esp32"
#define MQTT_STATUS_TOPIC     "irrigation/status"
#define MQTT_HEARTBEAT_TOPIC  "irrigation/heartbeat"
#define HEARTBEAT_INTERVAL_MS 300000UL  // 5 minutes
#define MQTT_RETRY_INTERVAL_MS 5000     // retry MQTT reconnect every 5 s

// ---- I2C ----
#define I2C_SDA 5
#define I2C_SCL 4
// Lower value reduces glitches on long/noisy cables (try 50000 if problems persist)
#define I2C_CLOCK_HZ 100000UL
#define RTC_I2C_ADDR 0x68   // DS3231 fixed address

// ---- PCF8574A relay expanders ----
#define PCF_ADDR_0  0x38   // Box 1 & 2
#define PCF_ADDR_1  0x3C   // Box 3 & 4

// Relay board polarity: true = active-LOW (relay ON when pin LOW)
#define RELAY_ACTIVE_LOW true

// ---- LCD 20x4 ----
#define LCD_ADDR 0x27
#define LCD_COLS 20
#define LCD_ROWS 4
// Periodic full reinit to recover the HD44780 from a noise-corrupted state
#define LCD_REINIT_INTERVAL_MS 60000UL

// ---- Pump (single, direct GPIO, active-LOW, MQTT-controlled by Pi) ----
#define PUMP_PIN  6

// ---- System ----
#define NUM_BOXES        4
#define VALVES_PER_BOX   3

// ---- OTA ----
// Bump FIRMWARE_VERSION before every release build, then upload the new .bin to the Pi.
// The Pi manifest version must match for the ESP32 to recognise it as an update.
#define FIRMWARE_VERSION  13
#define FIRMWARE_TYPE     "irrigation-esp32c3"
// Served by Apache (port 80), not the Flask dev server on :5000 — Werkzeug
// truncates the 1 MB image to a slow client, which fails the OTA image check.
// The manifest's host/port fields point the binary download at the same place.
#define OTA_MANIFEST_URL  "http://raspi4server.local/firmware/manifest.json"

// ---- WiFi reconnect ----
#define WIFI_RETRY_INTERVAL_MS   10000   // wait 10 s between reconnect attempts

// ---- Safety watchdogs ----
// Stop watering if WiFi is lost for this long while valves are open
#define WIFI_ACTIVE_SAFETY_MS  30000UL
// Hardware task watchdog: reboot if main loop stalls (must exceed WIFI_RETRY_INTERVAL_MS)
#define TASK_WDT_TIMEOUT_S     60
// Stop pump if no valve SET command received for this long while pump is on
#define PUMP_SAFETY_TIMEOUT_MS 1800000UL  // 30 minutes
// Absolute cap on how long a single valve may stay open. This is the last-resort
// dead-man's switch: it fires even when WiFi and MQTT are healthy and the pump is
// off, so a crashed or hung Pi controller cannot leave a valve open indefinitely.
// Timed from the OFF->ON transition only — the Pi's periodic keepalive re-publish
// deliberately does NOT extend it. Must exceed the longest legitimate single step.
#define VALVE_MAX_ON_MS        3600000UL  // 60 minutes
