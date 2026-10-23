/*
  ESP32-S3 + VL53L8CX sensor node v2.
  Core 1 samples at 4 Hz; core 0 sends every second frame to the Pi.
  Wiring stays SDA GPIO8, SCL GPIO9, 400 kHz, with no LPn or reset GPIO.
*/
#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <StreamString.h>
#include <HWCDC.h>
#include <ArduinoOTA.h>
#include <vl53l8cx.h>
#include <esp_system.h>
#include <esp_ota_ops.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <stdarg.h>
#include <atomic>
#include "secrets.h"

#ifndef OTA_PASSWORD
#error "Define OTA_PASSWORD in secrets.h (at least 12 characters)."
#else
static_assert(sizeof(OTA_PASSWORD) - 1 >= 12, "OTA_PASSWORD must contain at least 12 characters.");
constexpr auto stringsEqual = [](const char* left, const char* right) {
  while (*left && *left == *right) {
    ++left;
    ++right;
  }
  return *left == *right;
};
static_assert(!stringsEqual(OTA_PASSWORD, "replace-ota-password"), "OTA_PASSWORD must not use the example placeholder.");
#endif

#if CONFIG_FREERTOS_UNICORE || ARDUINO_RUNNING_CORE != 1
#error "Build for a dual-core ESP32-S3 with Arduino running on core 1."
#endif

constexpr char FRAME_URL[] = "http://10.42.0.1/frame";
constexpr int SDA_PIN = 8;
constexpr int SCL_PIN = 9;
constexpr int LPN_PIN = -1;
constexpr int I2C_RST_PIN = -1;
constexpr uint32_t I2C_HZ = 400000;
constexpr uint8_t RANGING_HZ = 4;
constexpr uint32_t HTTP_TIMEOUT_MS = 2000;
constexpr uint32_t POST_WATCHDOG_MS = 5 * 60 * 1000;
constexpr uint32_t SENSOR_WATCHDOG_MS = 3000;
constexpr uint32_t SENSOR_RETRY_MS = 10000;
constexpr uint32_t SEQUENCE_WATCHDOG_MS = 10000;

enum class NetworkRestart : uint8_t { None, WiFiConfig, NoPost };

struct Frame {
  uint32_t readyMs;
  int16_t distance[64];
  uint8_t status[64];
};

struct NetworkStatus {
  uint32_t seq = 0;
  uint32_t sent = 0;
  uint32_t failed = 0;
  uint32_t postMs = 0;
  uint32_t lagMs = 0;
  int code = 0;
  int32_t rssi = 0;
  bool connected = false;
  bool started = false;
  int core = -1;
  uint32_t address = 0;
  uint32_t joinAttempts = 0;
  uint32_t joinRetryMs = 0;
  uint32_t wifiConnections = 0;
  uint32_t wifiDisconnects = 0;
  uint32_t httpSetupErrors = 0;
  uint32_t jsonErrors = 0;
  uint32_t httpRestarts = 0;
  uint32_t failureSeq = 0;
  uint32_t failurePostMs = 0;
  uint32_t failureLagMs = 0;
  int failureCode = 0;
  NetworkRestart restart = NetworkRestart::None;
};

VL53L8CX sensor(&Wire, LPN_PIN, I2C_RST_PIN);
VL53L8CX_ResultsData results;
QueueHandle_t frameQueue = nullptr;
portMUX_TYPE statusMux = portMUX_INITIALIZER_UNLOCKED;
NetworkStatus sharedStatus;
Frame latestFrame = {};
bool haveFrame = false;
bool sensorRunning = false;
bool verbose = true;
uint32_t frameCount = 0;
uint32_t lastFrameMs = 0;
uint32_t lastRetryMs = 0;
uint32_t lastStatusMs = 0;
std::atomic<bool> otaInProgress{false};
std::atomic<bool> networkPaused{false};
std::atomic<bool> sensorPaused{false};

#if CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
extern "C" bool verifyRollbackLater() {
  return true;
}
#endif

void logLine(const char* format, ...) {
  if (xPortGetCoreID() != 1 || !HWCDC::isConnected()) return;
  char line[384];
  const int prefix = snprintf(line, sizeof(line), "[%7.2fs] ", millis() / 1000.0);
  if (prefix < 0 || static_cast<size_t>(prefix) >= sizeof(line)) return;
  va_list args;
  va_start(args, format);
  const int added = vsnprintf(line + prefix, sizeof(line) - prefix, format, args);
  va_end(args);
  if (added < 0 || static_cast<size_t>(added) >= sizeof(line) - prefix - 1) return;
  const size_t length = static_cast<size_t>(prefix + added);
  line[length] = '\n';
  // A connected USB bus does not guarantee a reader. Drop a line unless it fits now.
  if (Serial.availableForWrite() < static_cast<int>(length + 1)) return;
  Serial.write(reinterpret_cast<const uint8_t*>(line), length + 1);
}

void publishStatus(const NetworkStatus& status) {
  portENTER_CRITICAL(&statusMux);
  sharedStatus = status;
  portEXIT_CRITICAL(&statusMux);
}

NetworkStatus networkSnapshot() {
  portENTER_CRITICAL(&statusMux);
  NetworkStatus status = sharedStatus;
  portEXIT_CRITICAL(&statusMux);
  return status;
}

void restartFromNetwork(NetworkStatus& status, NetworkRestart reason) {
  status.restart = reason;
  publishStatus(status);
  // Core 1 can report the reason; the reboot never depends on USB making progress.
  vTaskDelay(pdMS_TO_TICKS(250));
  ESP.restart();
}

void restartNode(const char* reason) {
  logLine("Restarting node: %s", reason);
  delay(100);
  ESP.restart();
}

void reportResetReason() {
  const esp_reset_reason_t reason = esp_reset_reason();
  const char* text = "other / unknown";
  switch (reason) {
    case ESP_RST_POWERON:
      text = "power-on (normal, or the USB supply dropped out completely)";
      break;
    case ESP_RST_EXT: text = "external reset"; break;
    case ESP_RST_SW: text = "software restart"; break;
    case ESP_RST_PANIC: text = "panic / exception"; break;
    case ESP_RST_INT_WDT: text = "interrupt watchdog"; break;
    case ESP_RST_TASK_WDT: text = "task watchdog"; break;
    case ESP_RST_WDT: text = "other watchdog"; break;
    case ESP_RST_DEEPSLEEP: text = "deep sleep wake-up"; break;
    case ESP_RST_BROWNOUT:
      text = "brownout (supply voltage dropped; check the power supply)";
      break;
    case ESP_RST_USB: text = "USB Serial/JTAG reset"; break;
    default: break;
  }
  logLine("Last restart: %s (reason %d)", text, static_cast<int>(reason));
}

bool probeSensor() {
  Wire.beginTransmission(0x29);
  if (Wire.endTransmission() == 0) {
    logLine("I2C device at 0x29 <- VL53L8CX (direct probe)");
    return true;
  }
  logLine("0x29 did not answer; running diagnostic I2C scan");
  bool found = false;
  for (uint8_t address = 1; address < 127; ++address) {
    Wire.beginTransmission(address);
    if (Wire.endTransmission() == 0) {
      logLine("I2C device at 0x%02x", address);
      if (address == 0x29) found = true;
    }
  }
  return found;
}

bool initSensor() {
  sensorRunning = false;
  haveFrame = false;
  if (!probeSensor()) return false;
  sensor.end();
  sensor.begin();
  const uint32_t startedMs = millis();
  logLine("Loading VL53L8CX firmware...");
  uint8_t status = sensor.init();
  if (status != 0) {
    logLine("Sensor init failed, status %u", status);
    return false;
  }
  logLine("Sensor firmware loaded in %lu ms", static_cast<unsigned long>(millis() - startedMs));
  status = sensor.set_resolution(VL53L8CX_RESOLUTION_8X8);
  status |= sensor.set_ranging_mode(VL53L8CX_RANGING_MODE_AUTONOMOUS);
  status |= sensor.set_ranging_frequency_hz(RANGING_HZ);
  status |= sensor.set_integration_time_ms(10);
  if (status != 0) {
    logLine("Sensor config failed, status %u", status);
    return false;
  }
  return true;
}

bool startRanging() {
  const uint8_t status = sensor.start_ranging();
  if (status != 0) {
    logLine("Sensor start failed, status %u", status);
    return false;
  }
  sensorRunning = true;
  frameCount = 0;
  lastFrameMs = millis();
  logLine("Sensor ranging: 8x8, 4 Hz, autonomous, 10 ms integration, core %d", xPortGetCoreID());
  return true;
}

void setupOta() {
  const String hostname = String("alerto-") + NODE_ID;
  ArduinoOTA.setHostname(hostname.c_str());
  ArduinoOTA.setPassword(OTA_PASSWORD);
  ArduinoOTA.onStart([]() {
    otaInProgress.store(true);
    // Finish any in-flight POST before the transfer starts, then discard its queued frame.
    while (!networkPaused.load() || !sensorPaused.load()) delay(1);
    xQueueReset(frameQueue);
  });
  ArduinoOTA.onError([](ota_error_t) {
    otaInProgress.store(false);
  });
  ArduinoOTA.begin();
}

void otaTask(void*) {
  while (WiFi.status() != WL_CONNECTED) vTaskDelay(pdMS_TO_TICKS(100));
  setupOta();
  for (;;) {
    // Authentication is CPU work; a separate task keeps sampling and POSTs scheduled.
    ArduinoOTA.handle();
    vTaskDelay(pdMS_TO_TICKS(2));
  }
}

void readSensor() {
  if (!sensorRunning) return;
  uint8_t ready = 0;
  if (sensor.check_data_ready(&ready) != 0 || !ready) return;
  if (sensor.get_ranging_data(&results) != 0) return;
  latestFrame.readyMs = millis();
  for (int zone = 0; zone < 64; ++zone) {
    const int target = VL53L8CX_NB_TARGET_PER_ZONE * zone;
    const bool hit = results.nb_target_detected[zone] > 0;
    latestFrame.distance[zone] = hit && results.distance_mm[target] > 0 ? results.distance_mm[target] : 0;
    latestFrame.status[zone] = hit ? results.target_status[target] : 255;
  }
  haveFrame = true;
  lastFrameMs = latestFrame.readyMs;
  if (++frameCount % 2 == 0) {
    // The one-slot queue copies the frame; a slow sender never holds up sensing.
    xQueueOverwrite(frameQueue, &latestFrame);
  }
}

bool appendBody(char* body, size_t capacity, size_t& used, const char* format, ...) {
  if (used >= capacity) return false;
  va_list args;
  va_start(args, format);
  const int added = vsnprintf(body + used, capacity - used, format, args);
  va_end(args);
  if (added < 0 || static_cast<size_t>(added) >= capacity - used) return false;
  used += static_cast<size_t>(added);
  return true;
}

bool buildBody(const Frame& frame, uint32_t seq, char* body, size_t capacity, size_t& used) {
  used = 0;
  if (!appendBody(body, capacity, used, "{\"device_id\":\"%s\",\"seq\":%lu,\"d\":[",
                  NODE_ID, static_cast<unsigned long>(seq))) return false;
  for (int zone = 0; zone < 64; ++zone) {
    if (!appendBody(body, capacity, used, zone ? ",%d" : "%d", frame.distance[zone])) return false;
  }
  if (!appendBody(body, capacity, used, "],\"s\":[")) return false;
  for (int zone = 0; zone < 64; ++zone) {
    if (!appendBody(body, capacity, used, zone ? ",%u" : "%u", frame.status[zone])) return false;
  }
  return appendBody(body, capacity, used, "]}");
}

void reportPostError(int code, uint32_t seq, uint32_t postMs, uint32_t lagMs) {
  const char* text = "HTTP error from the Pi";
  String detail;
  if (code == 403) text = "wrong NODE_KEY";
  else if (code == 400) text = "malformed frame";
  else if (code == -1) text = "cannot reach the Pi";
  else if (code == -11) text = "timeout";
  else if (code < 0) {
    detail = HTTPClient::errorToString(code);
    text = detail.c_str();
  }
  logLine("POST failed: %d %s | seq %lu | post %lu ms | lag %lu ms", code, text,
          static_cast<unsigned long>(seq), static_cast<unsigned long>(postMs),
          static_cast<unsigned long>(lagMs));
}

void networkTask(void* argument) {
  TaskHandle_t startupTask = static_cast<TaskHandle_t>(argument);
  WiFiClient client;
  HTTPClient http;
  bool httpReady = false;
  bool attemptedJoin = false;
  uint32_t lastJoinMs = 0;
  uint32_t retryMs = 1000;
  uint32_t lastSuccessMs = 0;
  uint32_t lastSeqAdvanceMs = millis();
  uint32_t pauseStartedMs = 0;
#if CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
  bool imageValidated = false;
#endif
  NetworkStatus status;
  status.core = xPortGetCoreID();
  WiFi.persistent(false);
  if (!WiFi.mode(WIFI_STA) || !WiFi.setSleep(false) || !WiFi.setTxPower(WIFI_POWER_8_5dBm)) {
    restartFromNetwork(status, NetworkRestart::WiFiConfig);
  }
  WiFi.setAutoReconnect(false);
  status.started = true;
  publishStatus(status);

  for (;;) {
    const uint32_t now = millis();
    if (otaInProgress.load()) {
      if (!networkPaused.load()) {
        pauseStartedMs = now;
        http.end();
        client.stop();
        httpReady = false;
        networkPaused.store(true);
      }
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }
    if (networkPaused.exchange(false)) {
      // Transfer time does not count against either successful-POST or sequence watchdogs.
      lastSuccessMs += now - pauseStartedMs;
      lastSeqAdvanceMs = now;
    }
    if (now - lastSuccessMs >= POST_WATCHDOG_MS) {
      restartFromNetwork(status, NetworkRestart::NoPost);
    }
    const bool connected = WiFi.status() == WL_CONNECTED;
    if (!connected) {
      if (status.connected) {
        ++status.wifiDisconnects;
        http.end();
        client.stop();
        httpReady = false;
        lastJoinMs = now;
        retryMs = 1000;
      }
      status.connected = false;
      lastSeqAdvanceMs = now;
      if (!attemptedJoin || now - lastJoinMs >= retryMs) {
        WiFi.disconnectAsync();
        WiFi.begin(WIFI_SSID, WIFI_PASS);
        attemptedJoin = true;
        lastJoinMs = now;
        // Let the current association finish while this task yields.
        retryMs = retryMs < 15000 ? retryMs * 2 : 30000;
        ++status.joinAttempts;
        status.joinRetryMs = retryMs;
      }
      publishStatus(status);
      vTaskDelay(pdMS_TO_TICKS(50));
      continue;
    }
    if (!status.connected) {
      ++status.wifiConnections;
      status.address = static_cast<uint32_t>(WiFi.localIP());
      retryMs = 1000;
      lastSeqAdvanceMs = now;
      status.connected = true;
      status.rssi = WiFi.RSSI();
      publishStatus(status);
      if (startupTask != nullptr) {
        xTaskNotifyGive(startupTask);
        startupTask = nullptr;
      }
    }
    status.connected = true;
    status.rssi = WiFi.RSSI();
    publishStatus(status);

    if (now - lastSeqAdvanceMs >= SEQUENCE_WATCHDOG_MS) {
      http.end();
      client.stop();
      httpReady = false;
      ++status.httpRestarts;
      lastSeqAdvanceMs = now;
      publishStatus(status);
    }

    Frame frame;
    if (xQueueReceive(frameQueue, &frame, pdMS_TO_TICKS(100)) != pdTRUE) continue;
    if (!httpReady) {
      httpReady = http.begin(client, FRAME_URL);
      if (!httpReady) {
        ++status.httpSetupErrors;
        publishStatus(status);
        http.end();
        client.stop();
        continue;
      }
      http.useHTTP10(false);
      http.setReuse(true);
      http.setTimeout(HTTP_TIMEOUT_MS);
      http.setConnectTimeout(HTTP_TIMEOUT_MS);
      http.addHeader("Content-Type", "application/json");
      http.addHeader("X-Device-Key", NODE_KEY);
    }

    char body[1024];
    size_t bodySize = 0;
    const uint32_t nextSeq = status.seq + 1;
    if (!buildBody(frame, nextSeq, body, sizeof(body), bodySize)) {
      ++status.jsonErrors;
      publishStatus(status);
      http.end();
      client.stop();
      httpReady = false;
      continue;
    }
    // Assign once, immediately before each POST attempt, even if that POST fails.
    status.seq = nextSeq;
    const uint32_t startedMs = millis();
    lastSeqAdvanceMs = startedMs;
    int code = http.POST(reinterpret_cast<uint8_t*>(body), bodySize);
    if (code > 0) {
      // Consume the response before reusing the socket and detect body read errors.
      StreamString response;
      const int consumed = http.writeToStream(&response);
      if (consumed < 0) code = consumed;
    }
    const uint32_t completedMs = millis();
    status.code = code;
    status.postMs = completedMs - startedMs;
    status.lagMs = completedMs - frame.readyMs;
    if (code == HTTP_CODE_OK) {
#if CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
      if (!imageValidated) {
        imageValidated = esp_ota_mark_app_valid_cancel_rollback() == ESP_OK;
      }
#endif
      ++status.sent;
      lastSuccessMs = completedMs;
    } else {
      ++status.failed;
      status.failureSeq = status.seq;
      status.failureCode = code;
      status.failurePostMs = status.postMs;
      status.failureLagMs = status.lagMs;
      http.end();
      client.stop();
      httpReady = false;
    }
    publishStatus(status);
  }
}

void printNetworkEvents(const NetworkStatus& status) {
  static NetworkStatus reported;
  if (status.started && !reported.started) {
    logLine("Network task core %d; Wi-Fi sleep off; TX 8.5 dBm; HTTP/1.1 reuse; timeout 2000 ms",
            status.core);
  }
  if (status.joinAttempts != reported.joinAttempts) {
    logLine("Joining Wi-Fi \"%s\"; non-blocking, next retry in %lu ms", WIFI_SSID,
            static_cast<unsigned long>(status.joinRetryMs));
  }
  if (status.wifiConnections != reported.wifiConnections) {
    logLine("Wi-Fi OK: IP %s, signal %ld dBm", IPAddress(status.address).toString().c_str(),
            static_cast<long>(status.rssi));
  }
  if (status.wifiDisconnects != reported.wifiDisconnects) {
    logLine("Wi-Fi disconnected; retrying from core 0 with backoff");
  }
  if (status.httpSetupErrors != reported.httpSetupErrors) {
    logLine("HTTP setup failed; reconnecting on the next frame");
  }
  if (status.jsonErrors != reported.jsonErrors) {
    logLine("Frame JSON does not fit; no POST attempted");
  }
  if (status.httpRestarts != reported.httpRestarts) {
    logLine("Sequence did not advance for 10 seconds with Wi-Fi up; restarting HTTP client");
  }
  if (status.failed != reported.failed) {
    reportPostError(status.failureCode, status.failureSeq, status.failurePostMs, status.failureLagMs);
  }
  if (status.restart != reported.restart) {
    if (status.restart == NetworkRestart::WiFiConfig) {
      logLine("Restarting node: could not configure Wi-Fi sleep or the 8.5 dBm power limit");
    } else if (status.restart == NetworkRestart::NoPost) {
      logLine("Restarting node: no successful POST for 5 minutes");
    }
  }
  reported = status;
}

bool validZone(int zone) {
  return latestFrame.distance[zone] > 0 &&
         (latestFrame.status[zone] == 5 || latestFrame.status[zone] == 9);
}

void printGrid() {
  if (!haveFrame) {
    logLine("No sensor frame yet");
    return;
  }
  logLine("8x8 grid in cm; -- means status is not 5/9 or distance is not positive");
  for (int row = 0; row < 8; ++row) {
    char line[96] = {};
    size_t used = 0;
    for (int col = 0; col < 8; ++col) {
      const int zone = row * 8 + col;
      if (validZone(zone)) appendBody(line, sizeof(line), used, "%6.1f", latestFrame.distance[zone] / 10.0);
      else appendBody(line, sizeof(line), used, "    --");
    }
    logLine("%s", line);
  }
}

void printStatus(const NetworkStatus& status) {
  int valid = 0;
  int nearMm = 0;
  int farMm = 0;
  if (haveFrame) {
    for (int zone = 0; zone < 64; ++zone) {
      if (!validZone(zone)) continue;
      const int distance = latestFrame.distance[zone];
      if (valid == 0 || distance < nearMm) nearMm = distance;
      if (distance > farMm) farMm = distance;
      ++valid;
    }
  }
  const char* post = status.seq == 0 ? "waiting" : (status.code == HTTP_CODE_OK ? "OK" : "FAIL");
  logLine("valid %2d/64 near %d mm far %d mm centre %d mm | wifi %s %ld dBm | Pi: %s | "
          "sent %lu, failed %lu | seq %lu | post %lu ms | lag %lu ms | frames %lu",
          valid, nearMm, farMm, haveFrame && validZone(27) ? latestFrame.distance[27] : 0,
          status.connected ? "up" : "down", static_cast<long>(status.rssi), post,
          static_cast<unsigned long>(status.sent), static_cast<unsigned long>(status.failed),
          static_cast<unsigned long>(status.seq), static_cast<unsigned long>(status.postMs),
          static_cast<unsigned long>(status.lagMs), static_cast<unsigned long>(frameCount));
}

void setup() {
  Serial.setTxTimeoutMs(0);
  Serial.begin(115200);
  delay(500);
  logLine("ALERTO sensor node %s v2", NODE_ID);
  reportResetReason();
  logLine("CPU %u MHz, free heap %u bytes, sensing core %d", getCpuFrequencyMhz(),
          ESP.getFreeHeap(), xPortGetCoreID());
  logLine("Step 1/3: sensor init on SDA=GPIO8 SCL=GPIO9 at 400 kHz");
  Wire.begin(SDA_PIN, SCL_PIN, I2C_HZ);
  const uint32_t sensorInitStartedMs = millis();
  while (!initSensor()) {
    if (millis() - sensorInitStartedMs >= POST_WATCHDOG_MS) {
      restartNode("sensor init failed for 5 minutes");
    }
    logLine("Retrying sensor init in 3 seconds");
    delay(3000);
  }
  frameQueue = xQueueCreate(1, sizeof(Frame));
  if (frameQueue == nullptr) restartNode("could not allocate the one-frame queue");
  logLine("Step 2/3: Wi-Fi from core 0");
  if (xTaskCreatePinnedToCore(networkTask, "node-network", 8192, xTaskGetCurrentTaskHandle(),
                              1, nullptr, 0) != pdPASS) {
    restartNode("could not create the network task");
  }
  if (ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(5000)) == 0) {
    logLine("Wi-Fi not joined yet; sensing continues while core 0 reconnects");
  }
  printNetworkEvents(networkSnapshot());
  delay(500);
  logLine("Step 3/3: start ranging");
  startRanging();
  if (xTaskCreatePinnedToCore(otaTask, "node-ota", 8192, nullptr, 1, nullptr, 0) != pdPASS) {
    restartNode("could not create the wireless update task");
  }
  lastRetryMs = millis() - SENSOR_RETRY_MS;
  logLine("Running. Send every second frame (2 Hz); g = grid in cm, v = toggle 1 s status.");
}

void loop() {
  if (otaInProgress.load()) {
    if (!sensorPaused.load()) {
      if (sensorRunning) sensor.stop_ranging();
      sensorRunning = false;
      haveFrame = false;
      sensorPaused.store(true);
      logLine("Wireless update started; ranging and POSTs paused");
    }
    delay(2);
    return;
  }
  if (sensorPaused.exchange(false)) {
    logLine("Wireless update failed; resuming ranging and POSTs");
    startRanging();
  }
  readSensor();
  const uint32_t now = millis();
  if ((!sensorRunning || now - lastFrameMs > SENSOR_WATCHDOG_MS) &&
      now - lastRetryMs >= SENSOR_RETRY_MS) {
    lastRetryMs = now;
    logLine("No frames for 3 seconds - restarting sensor (at most once per 10 seconds)");
    if (sensorRunning) sensor.stop_ranging();
    if (initSensor()) startRanging();
  }
  while (Serial.available() > 0) {
    const char command = Serial.read();
    if (command == 'g') printGrid();
    else if (command == 'v') {
      verbose = !verbose;
      logLine("Status log %s", verbose ? "on" : "off");
    }
  }
  const NetworkStatus status = networkSnapshot();
  printNetworkEvents(status);
  if (verbose && now - lastStatusMs >= 1000) {
    lastStatusMs = now;
    printStatus(status);
  }
  delay(2);
}
