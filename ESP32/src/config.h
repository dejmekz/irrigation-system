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

// ---- PCF8574A relay expanders ----
#define PCF_ADDR_0  0x38   // Box 1 & 2
#define PCF_ADDR_1  0x3C   // Box 3 & 4

// Relay board polarity: true = active-LOW (relay ON when pin LOW)
#define RELAY_ACTIVE_LOW true

// ---- LCD 20x4 ----
#define LCD_ADDR 0x27
#define LCD_COLS 20
#define LCD_ROWS 4

// ---- Pump (single, direct GPIO, active-LOW) ----
#define PUMP_PIN  6
#define PUMP_BOX  1

// ---- System ----
#define NUM_BOXES        4
#define VALVES_PER_BOX   3

// ---- WiFi reconnect ----
#define WIFI_RETRY_INTERVAL_MS   10000   // wait 10 s between reconnect attempts

// ---- Safety watchdogs ----
// Stop watering if WiFi is lost for this long while valves are open
#define WIFI_ACTIVE_SAFETY_MS  30000UL
// Hardware task watchdog: reboot if main loop stalls (must exceed WIFI_RETRY_INTERVAL_MS)
#define TASK_WDT_TIMEOUT_S     60
