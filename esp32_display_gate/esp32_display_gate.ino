/*
  ALERTO output node: ESP32 + 0.96" SSD1306 OLED (128x64, I2C) + servo gate
  --------------------------------------------------------------------------
  Polls the Raspberry Pi every 2 s:  GET http://10.42.0.1/api/display
    {"gate":"open|closed","title":"GO TO ZONE A","lines":["Puno na ang Zone D.", ...],
     "levels":"A:L B:M C:M D:H","lang":"fil","zone":"A"}
  - OLED shows the commuter message (Filipino or English, set on the dashboard)
  - Servo turns to GATE_CLOSED_DEG when every segment is High, back to GATE_OPEN_DEG otherwise
  - If the Pi can't be reached for 15 s, the display says so and the gate stays OPEN (fail-safe)

  Libraries (Arduino Library Manager):
    "Adafruit SSD1306" (+ "Adafruit GFX Library"), "ESP32Servo", "ArduinoJson" (v7)
  Wiring (ESP32-S3 DevKit; change the pins below for other boards):
    OLED  VCC->3V3, GND->GND, SDA->GPIO8, SCL->GPIO9
    Servo signal->GPIO4, V+ -> 5V supply (NOT the 3V3 pin), GND -> shared GND
  A servo can draw 500 mA+ when moving: power it from a separate 5 V supply with
  its ground tied to the ESP32 ground, or the ESP32 will brown out.
*/
#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <ESP32Servo.h>

// ---------- config ----------
const char* WIFI_SSID   = "CommuterDensity";
const char* WIFI_PASS   = "";
const char* DISPLAY_URL = "http://10.42.0.1/api/display";

const int OLED_SDA = 8, OLED_SCL = 9;
const uint8_t OLED_ADDR = 0x3C;           // some boards use 0x3D
const int SERVO_PIN = 4;
const int GATE_OPEN_DEG = 0;
const int GATE_CLOSED_DEG = 90;
const uint32_t POLL_MS = 2000;
const uint32_t OFFLINE_MS = 15000;
// ----------------------------

Adafruit_SSD1306 oled(128, 64, &Wire, -1);
Servo gate;
bool gateClosed = false;
uint32_t lastOk = 0, lastPoll = 0;

void moveGate(bool close) {
  if (close == gateClosed) return;
  gateClosed = close;
  gate.write(close ? GATE_CLOSED_DEG : GATE_OPEN_DEG);
  Serial.printf("Gate %s\n", close ? "CLOSED" : "OPEN");
}

void show(const char* title, JsonArray lines, const char* levels) {
  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);
  // title bar (inverted)
  oled.fillRect(0, 0, 128, 12, SSD1306_WHITE);
  oled.setTextColor(SSD1306_BLACK);
  oled.setTextSize(1);
  oled.setCursor(2, 2);
  oled.print(title);
  oled.setTextColor(SSD1306_WHITE);
  int y = 16;
  for (JsonVariant l : lines) {
    if (y > 46) break;
    oled.setCursor(0, y);
    oled.print(l.as<const char*>());
    y += 10;
  }
  oled.drawFastHLine(0, 54, 128, SSD1306_WHITE);
  oled.setCursor(0, 56);
  oled.print(levels);
  oled.display();
}

void showText(const char* title, const char* l1, const char* l2) {
  oled.clearDisplay();
  oled.setTextSize(1);
  oled.setTextColor(SSD1306_WHITE);
  oled.setCursor(0, 0);  oled.print(title);
  oled.setCursor(0, 20); oled.print(l1);
  oled.setCursor(0, 32); oled.print(l2);
  oled.display();
}

void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.disconnect();
  if (strlen(WIFI_PASS)) WiFi.begin(WIFI_SSID, WIFI_PASS); else WiFi.begin(WIFI_SSID);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 8000) delay(200);
}

void poll() {
  ensureWiFi();
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  http.setTimeout(2500);
  http.begin(DISPLAY_URL);
  int code = http.GET();
  if (code == 200) {
    JsonDocument doc;
    if (!deserializeJson(doc, http.getString())) {
      lastOk = millis();
      moveGate(strcmp(doc["gate"] | "open", "closed") == 0);
      show(doc["title"] | "ALERTO", doc["lines"].as<JsonArray>(), doc["levels"] | "");
    }
  } else {
    Serial.printf("GET failed: %d\n", code);
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Wire.begin(OLED_SDA, OLED_SCL);
  if (!oled.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) Serial.println("OLED not found - check wiring/address");
  showText("ALERTO", "Connecting to", WIFI_SSID);

  ESP32PWM::allocateTimer(0);
  gate.setPeriodHertz(50);
  gate.attach(SERVO_PIN, 500, 2400);
  gate.write(GATE_OPEN_DEG);                 // start open (fail-safe)

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  ensureWiFi();
  lastOk = millis();
}

void loop() {
  if (millis() - lastPoll >= POLL_MS) {
    lastPoll = millis();
    poll();
    if (millis() - lastOk > OFFLINE_MS) {       // hub unreachable: open the gate, say so
      moveGate(false);
      showText("ALERTO", "Walang koneksyon", "sa hub. Gate open.");
    }
  }
}
