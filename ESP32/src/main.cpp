#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <RTClib.h>
#include <esp32fota.h>
#include "config.h"
#include <esp_task_wdt.h>

// ---- PCF8574A state ----
// Each bit = one relay output. Active-LOW: 0 = relay ON, 1 = relay OFF.
static uint8_t pcfState[2] = {0xFF, 0xFF};
static const uint8_t PCF_ADDR[2] = {PCF_ADDR_0, PCF_ADDR_1};

// ---- Valve / pump state (for LCD) ----
static bool valveOn[NUM_BOXES][VALVES_PER_BOX] = {};
static bool pumpOn = false;

// ---- Hardware health ----
static bool rtcOk = false;
static bool pcfOk[2] = {false, false};
static bool lcdOk = false;
static bool hwStatusDirty = false;

// ---- PCF fault overlay ----
static char pcfErrMsg[LCD_COLS + 1] = "";
static unsigned long pcfErrUntil = 0;

// ---- LCD dirty flag ----
static bool lcdDirty = true;

// ---- Pump safety watchdog ----
static unsigned long lastValveActivityAt = 0;

// ---- Objects ----
WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);
LiquidCrystal_I2C lcd(LCD_ADDR, LCD_COLS, LCD_ROWS);
RTC_DS3231 rtc;
esp32FOTA fota(FIRMWARE_TYPE, FIRMWARE_VERSION);

// =============================================================
// PCF8574A
// =============================================================

bool pcfFlush(int idx)
{
    Wire.beginTransmission(PCF_ADDR[idx]);
    Wire.write(pcfState[idx]);
    bool ok = (Wire.endTransmission() == 0);
    if (ok != pcfOk[idx])
    {
        pcfOk[idx] = ok;
        hwStatusDirty = true;
    }
    if (!ok)
        log_e("PCF[%d] I2C error", idx);
    return ok;
}

// box 1-4, valve 1-3
void setRelay(int box, int valve, bool on)
{
    int idx = (box <= 2) ? 0 : 1;
    int offset = ((box - 1) % 2) * 3; // 0 for box 1/3, 3 for box 2/4
    int bit = offset + (valve - 1);

#if RELAY_ACTIVE_LOW
    if (on)
        pcfState[idx] &= ~(1 << bit);
    else
        pcfState[idx] |= (1 << bit);
#else
    if (on)
        pcfState[idx] |= (1 << bit);
    else
        pcfState[idx] &= ~(1 << bit);
#endif

    if (!pcfFlush(idx))
    {
        snprintf(pcfErrMsg, sizeof(pcfErrMsg), " PCF[%d] relay error! ", idx);
        pcfErrUntil = millis() + 3000;
    }
}

// Turn all outputs off — called before any blocking reconnect
void allOff()
{
#if RELAY_ACTIVE_LOW
    pcfState[0] = pcfState[1] = 0xFF;
    digitalWrite(PUMP_PIN, HIGH);
#else
    pcfState[0] = pcfState[1] = 0x00;
    digitalWrite(PUMP_PIN, LOW);
#endif
    // Fix #3: check I2C result and retry once on failure so we don't
    // report everything off while relays are still physically energised.
    bool ok0 = pcfFlush(0);
    bool ok1 = pcfFlush(1);
    if (!ok0) ok0 = pcfFlush(0);
    if (!ok1) ok1 = pcfFlush(1);
    if (!ok0 || !ok1)
    {
        snprintf(pcfErrMsg, sizeof(pcfErrMsg), " allOff I2C error!  ");
        pcfErrUntil = millis() + 5000;
        log_e("allOff: PCF I2C write failed — relays may still be active!");
    }
    memset(valveOn, 0, sizeof(valveOn));
    pumpOn = false;
    lcdDirty = true;
}

// =============================================================
// LCD helpers
// =============================================================

void lcdLine(int row, const char *text)
{
    char buf[LCD_COLS + 1];
    snprintf(buf, sizeof(buf), "%-*s", LCD_COLS, text);
    lcd.setCursor(0, row);
    lcd.print(buf);
}

bool anyActive()
{
    if (pumpOn)
        return true;
    for (int b = 0; b < NUM_BOXES; b++)
        for (int v = 0; v < VALVES_PER_BOX; v++)
            if (valveOn[b][v])
                return true;
    return false;
}

// Active view — one row per box:
// "B1:  V1  V2  V3  Pm"
//  0123456789012345678901234
void lcdShowActive()
{
    for (int b = 0; b < NUM_BOXES; b++)
    {
        char line[LCD_COLS + 1] = "                    ";
        line[0] = 'B';
        line[1] = '1' + b;
        line[2] = ':';
        if (valveOn[b][0])
        {
            line[4] = 'V';
            line[5] = '1';
        }
        if (valveOn[b][1])
        {
            line[8] = 'V';
            line[9] = '2';
        }
        if (valveOn[b][2])
        {
            line[12] = 'V';
            line[13] = '3';
        }
        if (b == 0 && pumpOn)
        {
            line[16] = 'P';
            line[17] = 'm';
        }
        line[LCD_COLS] = '\0';
        lcd.setCursor(0, b);
        lcd.print(line);
    }
}

// Idle view — date & time from DS3231
void lcdShowIdle()
{
    if (lcdDirty)
    {
        lcdLine(0, "  Irrigation Ctrl   ");
        lcdLine(1, "");
    }

    if (!rtcOk)
    {
        lcdLine(2, "  RTC not found!    ");
        lcdLine(3, "");
        return;
    }

    static const char *DAYS[] = {"Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"};
    DateTime now = rtc.now();
    char buf[LCD_COLS + 1];

    // Date only changes once per day — skip the I2C write when unchanged
    static uint8_t lastDay = 255;
    if (lcdDirty || now.day() != lastDay)
    {
        lastDay = now.day();
        snprintf(buf, sizeof(buf), "  %s %02d.%02d.%04d   ",
                 DAYS[now.dayOfTheWeek()],
                 now.day(), now.month(), now.year());
        lcdLine(2, buf);
    }

    snprintf(buf, sizeof(buf), "      %02d:%02d:%02d      ",
             now.hour(), now.minute(), now.second());
    lcdLine(3, buf);
}

void updateLCD()
{
    // Periodic full reinit restores the HD44780 controller after noise-corrupted state
    static unsigned long lastReinit = 0;
    if (millis() - lastReinit >= LCD_REINIT_INTERVAL_MS)
    {
        lastReinit = millis();
        lcd.init();
        lcd.backlight();
        lcdDirty = true;
    }

    if (anyActive())
    {
        static unsigned long lastForce = 0;
        if (lcdDirty || millis() - lastForce >= 30000)
        {
            lcdShowActive();
            lastForce = millis();
        }
    }
    else
    {
        lcdShowIdle();
    }
    lcdDirty = false;

    if (millis() < pcfErrUntil)
        lcdLine(3, pcfErrMsg);
}

// =============================================================
// MQTT
// =============================================================

void publishState(int box, int valve, bool on)
{
    char topic[64];
    if (valve < 0)
        snprintf(topic, sizeof(topic), "irrigation/pump/state");
    else
        snprintf(topic, sizeof(topic), "irrigation/box/%d/valve/%d/state", box, valve);
    if (!mqtt.publish(topic, on ? "ON" : "OFF", true))
    { // retained
        snprintf(pcfErrMsg, sizeof(pcfErrMsg), " MQTT publish failed! ");
        pcfErrUntil = millis() + 5000;
    }
}

void publishAllOff()
{
    for (int b = 1; b <= NUM_BOXES; b++)
        for (int v = 1; v <= VALVES_PER_BOX; v++)
            publishState(b, v, false);
    publishState(0, -1, false);
}

void setPump(bool on)
{
#if RELAY_ACTIVE_LOW
    digitalWrite(PUMP_PIN, on ? LOW : HIGH);
#else
    digitalWrite(PUMP_PIN, on ? HIGH : LOW);
#endif
    pumpOn = on;
    if (on)
        lastValveActivityAt = millis();
    publishState(0, -1, on);
    lcdDirty = true;
}

void mqttCallback(char *topic, byte *payload, unsigned int len)
{
    bool on = (len == 2 && payload[0] == 'O' && payload[1] == 'N');
    int box, valve;

    if (sscanf(topic, "irrigation/box/%d/valve/%d/set", &box, &valve) == 2 && box >= 1 && box <= NUM_BOXES && valve >= 1 && valve <= VALVES_PER_BOX)
    {
        valveOn[box - 1][valve - 1] = on;
        setRelay(box, valve, on);
        publishState(box, valve, on);
        lastValveActivityAt = millis();
        lcdDirty = true;
    }
    else if (strcmp(topic, "irrigation/pump/set") == 0)
    {
        setPump(on);
    }
    else if (strcmp(topic, "irrigation/cmd/stop_all") == 0)
    {
        allOff();
        publishAllOff();
    }
    else if (strcmp(topic, "irrigation/cmd/ota_update") == 0)
    {
        log_i("OTA check triggered");
        lcdLine(0, "  OTA: checking...  ");
        lcdLine(1, "");
        lcdLine(2, "");
        lcdLine(3, "");
        if (fota.execHTTPcheck())
        {
            log_i("OTA update found, flashing...");
            allOff();
            lcdLine(0, "  OTA: updating...  ");
            lcdLine(1, "  Do not power off  ");
            lcdLine(2, "");
            lcdLine(3, "");
            fota.execOTA(); // reboots on success; falls through only on failure
            log_e("OTA update failed");
            lcdLine(0, "  OTA: failed!      ");
            lcdLine(1, "  Restarting...     ");
            delay(3000);
            ESP.restart();
        }
        else
        {
            log_i("OTA: already up to date");
            lcdLine(1, "  Already latest    ");
            delay(2000);
            lcdDirty = true;
        }
    }
}

void publishHwStatus()
{
    if (!mqtt.connected())
        return;
    char payload[96];
    snprintf(payload, sizeof(payload),
             "{\"pcf0\":%s,\"pcf1\":%s,\"rtc\":%s,\"lcd\":%s}",
             pcfOk[0] ? "true" : "false",
             pcfOk[1] ? "true" : "false",
             rtcOk    ? "true" : "false",
             lcdOk    ? "true" : "false");
    mqtt.publish("irrigation/hw_status", payload, true);
}

void publishHeartbeat()
{
    if (!mqtt.connected())
        return;

    char active[80] = "";
    int pos = 0;

    for (int b = 0; b < NUM_BOXES; b++)
        for (int v = 0; v < VALVES_PER_BOX; v++)
            if (valveOn[b][v])
            {
                int n = snprintf(active + pos, sizeof(active) - pos, "B%d:V%d ", b + 1, v + 1);
                if (n > 0 && pos + n < (int)sizeof(active))
                    pos += n;
            }

    if (pumpOn)
        snprintf(active + pos, (int)sizeof(active) - pos > 0 ? sizeof(active) - pos : 0, "pump");

    bool hwOk = pcfOk[0] && pcfOk[1];
    char payload[160];
    if (active[0] == '\0')
        snprintf(payload, sizeof(payload), "{\"status\":\"idle\",\"fw\":%d,\"hw_ok\":%s}",
                 FIRMWARE_VERSION, hwOk ? "true" : "false");
    else
        snprintf(payload, sizeof(payload), "{\"status\":\"active\",\"active\":\"%s\",\"fw\":%d,\"hw_ok\":%s}",
                 active, FIRMWARE_VERSION, hwOk ? "true" : "false");

    mqtt.publish(MQTT_HEARTBEAT_TOPIC, payload, true);
}

bool mqttReconnect()
{
    if (!mqtt.connect(MQTT_CLIENT_ID, MQTT_USER, MQTT_PASSWORD,
                      MQTT_STATUS_TOPIC, 0, true, "offline"))
    {
        log_e("MQTT connect failed");
        return false;
    }
    log_i("MQTT connected");
    mqtt.publish(MQTT_STATUS_TOPIC, "online", true);
    mqtt.subscribe("irrigation/box/+/valve/+/set");
    mqtt.subscribe("irrigation/pump/set");
    mqtt.subscribe("irrigation/cmd/+");
    for (int b = 1; b <= NUM_BOXES; b++)
        for (int v = 1; v <= VALVES_PER_BOX; v++)
            publishState(b, v, valveOn[b - 1][v - 1]);
    publishState(0, -1, pumpOn);
    publishHeartbeat();
    publishHwStatus();
    return true;
}

// =============================================================
// WiFi
// =============================================================

void wifiConnect()
{
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    lcdLine(0, "  Irrigation Ctrl   ");
    lcdLine(1, "  WiFi connecting...");
    lcdLine(2, "");
    lcdLine(3, "");
}

// =============================================================
// Setup & Loop
// =============================================================

void setup()
{
    Serial.begin(115200);

    Wire.begin(I2C_SDA, I2C_SCL);
    Wire.setClock(I2C_CLOCK_HZ);
    fota.setManifestURL(OTA_MANIFEST_URL);

    // LCD — probe before init; LiquidCrystal_I2C has no return value from init()
    Wire.beginTransmission(LCD_ADDR);
    lcdOk = (Wire.endTransmission() == 0);
    if (!lcdOk)
        log_e("LCD not found at 0x%02X", LCD_ADDR);
    lcd.init();
    lcd.backlight();
    lcdLine(0, "  Irrigation Ctrl   ");
    lcdLine(1, "  Starting...       ");
    lcdLine(2, "");
    lcdLine(3, "");

    // RTC
    rtcOk = rtc.begin();
    if (!rtcOk)
    {
        log_e("RTC not found");
        if (lcdOk) { lcdLine(2, "  RTC not found!    "); delay(2000); }
    }
    else
    {
        log_i("RTC ok");
    }

    // PCF8574A — write initial state (all relays OFF) and verify presence
    for (int i = 0; i < 2; i++)
    {
        Wire.beginTransmission(PCF_ADDR[i]);
        Wire.write(pcfState[i]);
        pcfOk[i] = (Wire.endTransmission() == 0);
        if (!pcfOk[i])
        {
            log_e("PCF[%d] not found", i);
            if (lcdOk)
            {
                char msg[LCD_COLS + 1];
                snprintf(msg, sizeof(msg), "  PCF[%d] not found! ", i);
                lcdLine(2, msg);
                delay(2000);
            }
        }
        else
        {
            log_i("PCF[%d] ok", i);
        }
    }

    // Pump GPIO — start OFF
    pinMode(PUMP_PIN, OUTPUT);
#if RELAY_ACTIVE_LOW
    digitalWrite(PUMP_PIN, HIGH);
#else
    digitalWrite(PUMP_PIN, LOW);
#endif

    // WiFi — auto-reconnect handles transient drops; wifiConnect() for initial join
    WiFi.setAutoReconnect(true);
    wifiConnect();

    // MQTT
    mqtt.setServer(MQTT_HOST, MQTT_PORT);
    mqtt.setCallback(mqttCallback);
    lcdLine(1, "  MQTT connecting...");
    mqttReconnect();
    lcdLine(1, "  Ready             ");
    delay(800);

    // Hardware task watchdog — reboots ESP32 if loop() ever stalls
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
    {
        esp_task_wdt_config_t c = {.timeout_ms = TASK_WDT_TIMEOUT_S * 1000, .idle_core_mask = 0, .trigger_panic = false};
        esp_task_wdt_reconfigure(&c);
    }
#else
    esp_task_wdt_init(TASK_WDT_TIMEOUT_S, false);
#endif
    esp_task_wdt_add(NULL);
}

void loop()
{
    esp_task_wdt_reset();

    // --- 1. CONTROL OF WI-FI STATE AND SAFETY RESTART ---
    // Fix #12: arm the watchdog timer the moment WiFi drops; only reset it after
    // the connection has been stable for >=5 s to survive brief reconnect flickers.
    static unsigned long wifiLostAt = 0;
    static unsigned long wifiStableAt = 0;
    static bool wifiWasConnected = false;

    if (WiFi.status() == WL_CONNECTED)
    {
        if (!wifiWasConnected)
        {
            // Connection just restored
            lcdLine(1, " WiFi connected ");
            log_i("WiFi connected");
            wifiWasConnected = true;
            wifiStableAt = millis();
        }
        // Only clear the safety timer after the link has been up for 5 s
        if (wifiStableAt != 0 && millis() - wifiStableAt >= 5000)
            wifiLostAt = 0;
    }
    else
    { // WiFi disconnected
        if (wifiWasConnected)
        {
            wifiWasConnected = false;
            wifiStableAt = 0;
        }

        // Arm safety timer immediately on first disconnected loop tick
        if (wifiLostAt == 0)
            wifiLostAt = millis();

        // Security watchdog: stop everything if WiFi has been gone too long while active
        if (anyActive() && millis() - wifiLostAt >= WIFI_ACTIVE_SAFETY_MS)
        {
            allOff(); // Emergency stop
            snprintf(pcfErrMsg, sizeof(pcfErrMsg), " WiFi lost! Stopped ");
            pcfErrUntil = millis() + 5000;
            wifiLostAt = millis(); // restart timer so repeated checks don't re-trigger
            log_e("WiFi lost while watering — all outputs stopped");
        }

        // --- 2. Connection Retry ---
        static unsigned long lastWifiRetry = 0;
        if (millis() - lastWifiRetry > WIFI_RETRY_INTERVAL_MS)
        {
            lastWifiRetry = millis();
            log_w("WiFi lost, reconnecting...");
            WiFi.begin(WIFI_SSID, WIFI_PASS);
        }
    }

    // --- 3. MQTT LOOP + WATCHDOG ---
    if (WiFi.status() == WL_CONNECTED && mqtt.connected())
        mqtt.loop();

    if (WiFi.status() == WL_CONNECTED && !mqtt.connected())
    {
        static unsigned long lastMqttRetry = 0;
        if (millis() - lastMqttRetry > MQTT_RETRY_INTERVAL_MS)
        {
            lastMqttRetry = millis();
            log_w("MQTT lost, reconnecting...");
            mqttReconnect();
        }
    }

    // Pump safety: stop pump AND all valves if no valve SET command for 30 minutes
    if (pumpOn && millis() - lastValveActivityAt >= PUMP_SAFETY_TIMEOUT_MS)
    {
        allOff();
        publishAllOff();
        snprintf(pcfErrMsg, sizeof(pcfErrMsg), " Pump safety stop!  ");
        pcfErrUntil = millis() + 5000;
        log_e("Pump safety stop: no valve activity for 30 min — all outputs off");
    }

    // Publish hw_status immediately when a device goes offline or recovers
    if (hwStatusDirty && mqtt.connected())
    {
        publishHwStatus();
        hwStatusDirty = false;
    }

    // Heartbeat every 5 minutes
    static unsigned long lastHeartbeat = 0;
    if (millis() - lastHeartbeat >= HEARTBEAT_INTERVAL_MS)
    {
        lastHeartbeat = millis();
        publishHeartbeat();
    }

    // Update LCD every second
    static unsigned long lastLCD = 0;
    if (millis() - lastLCD >= 1000)
    {
        lastLCD = millis();
        updateLCD();
    }
}
