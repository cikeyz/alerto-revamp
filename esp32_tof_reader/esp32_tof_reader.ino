/*
  ESP32-S3 + VL53L8CX  ->  Raspberry Pi 5   (reader only)
  ------------------------------------------------------
  The ESP32 only reads the 8x8 sensor and sends the raw frame to the Pi once
  per second. The Pi does everything else: background calibration, occupancy,
  people counting, per-minute rows, forecasting and the dashboard.

  POST http://10.42.0.1/frame
    {"device_id":"platform-A","d":[64 distances mm],"s":[64 target status]}

  Library: "STM32duino VL53L8CX" (Arduino Library Manager)
  Wiring:  VIN->3V3, GND->GND, SDA->GPIO8, SCL->GPIO9, LPn->3V3
  Board:   ESP32S3 Dev Module, Tools -> USB CDC On Boot: Enabled
*/
#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <vl53l8cx.h>

// ---------- config (must match /etc/commuter-density.env on the Pi) ----------
const char* WIFI_SSID  = "CommuterDensity";
const char* WIFI_PASS  = "";                        // "" = open hotspot
const char* FRAME_URL  = "http://10.42.0.1/frame";
const char* NODE_KEY   = "alerto-key-1231";         // = DEVICE_KEY on the Pi
const char* NODE_ID    = "platform-A";

const int      SDA_PIN     = 8;
const int      SCL_PIN     = 9;
const int      LPN_PIN     = -1;     // LPn tied to 3V3 (use 10 if wired to GPIO10)
const int      I2C_RST_PIN = -1;     // not wired
const uint32_t I2C_HZ      = 400000;
const uint8_t  RANGING_HZ  = 5;      // low rate = lower current; Pi gets 1 frame/s anyway
const uint32_t SEND_EVERY_MS = 1000;
// ------------------------------------------------------------------------------

VL53L8CX sensor(&Wire, LPN_PIN, I2C_RST_PIN);
VL53L8CX_ResultsData results;
int16_t  zoneDist[64];
uint8_t  zoneStatus[64];
bool     haveFrame = false;
uint32_t lastSend = 0, lastFrameMs = 0, lastRetryMs = 0, sent = 0, failed = 0;

bool startSensor() {
  sensor.end();
  sensor.begin();
  uint8_t st = sensor.init();                // loads sensor firmware (~2-3 s)
  if (st) { Serial.printf("Sensor init failed, status %u\n", st); return false; }
  st  = sensor.set_resolution(VL53L8CX_RESOLUTION_8X8);
  st |= sensor.set_ranging_frequency_hz(RANGING_HZ);
  st |= sensor.set_ranging_mode(VL53L8CX_RANGING_MODE_CONTINUOUS);
  st |= sensor.start_ranging();
  if (st) { Serial.printf("Sensor config failed, status %u\n", st); return false; }
  Serial.println("Sensor ranging.");
  lastFrameMs = millis();
  return true;
}

void readSensor() {
  uint8_t ready = 0;
  if (sensor.check_data_ready(&ready) != 0 || !ready) return;
  if (sensor.get_ranging_data(&results) != 0) return;
  for (int i = 0; i < 64; i++) {
    int k = VL53L8CX_NB_TARGET_PER_ZONE * i;
    bool hit = results.nb_target_detected[i] > 0;
    zoneDist[i]   = hit && results.distance_mm[k] > 0 ? results.distance_mm[k] : 0;
    zoneStatus[i] = hit ? results.target_status[k] : 255;
  }
  haveFrame = true;
  lastFrameMs = millis();
}

void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("Joining %s...\n", WIFI_SSID);
  WiFi.disconnect();
  if (strlen(WIFI_PASS)) WiFi.begin(WIFI_SSID, WIFI_PASS); else WiFi.begin(WIFI_SSID);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 8000) delay(200);
  if (WiFi.status() == WL_CONNECTED)
    Serial.printf("Wi-Fi OK, IP %s\n", WiFi.localIP().toString().c_str());
  else
    Serial.println("Wi-Fi not available yet");
}

void sendFrame() {
  static char body[900];
  int n = snprintf(body, sizeof(body), "{\"device_id\":\"%s\",\"d\":[", NODE_ID);
  for (int i = 0; i < 64; i++) n += snprintf(body + n, sizeof(body) - n, i ? ",%d" : "%d", zoneDist[i]);
  n += snprintf(body + n, sizeof(body) - n, "],\"s\":[");
  for (int i = 0; i < 64; i++) n += snprintf(body + n, sizeof(body) - n, i ? ",%u" : "%u", zoneStatus[i]);
  n += snprintf(body + n, sizeof(body) - n, "]}");

  HTTPClient http;
  http.setTimeout(2000);
  http.begin(FRAME_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", NODE_KEY);
  int code = http.POST((uint8_t*)body, n);
  http.end();
  if (code == 200) sent++;
  else { failed++; Serial.printf("POST failed: %d\n", code); }
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println("\nESP32 ToF reader");

  // Wi-Fi first at reduced power, so its start-up spike doesn't coincide with ranging
  WiFi.mode(WIFI_STA);
  WiFi.setTxPower(WIFI_POWER_8_5dBm);
  WiFi.setAutoReconnect(true);
  ensureWiFi();
  delay(500);

  Wire.begin(SDA_PIN, SCL_PIN, I2C_HZ);
  Serial.println("Starting VL53L8CX...");
  while (!startSensor()) { delay(3000); Serial.println("Retrying sensor..."); }
}

void loop() {
  readSensor();

  // sensor watchdog
  if (millis() - lastFrameMs > 3000 && millis() - lastRetryMs > 10000) {
    lastRetryMs = millis();
    Serial.println("No frames - restarting sensor");
    startSensor();
  }

  if (haveFrame && millis() - lastSend >= SEND_EVERY_MS) {
    lastSend = millis();
    ensureWiFi();
    if (WiFi.status() == WL_CONNECTED) sendFrame();
    if ((sent + failed) % 30 == 0)
      Serial.printf("frames sent %lu, failed %lu, centre zone %d mm\n",
                    (unsigned long)sent, (unsigned long)failed, zoneDist[27]);
  }
}
