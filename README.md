# ALERTO

**A Privacy-Preserving ToF-Based Edge Sensing System for Predictive Platform Density Mapping and
Turnstile Ingress Metering for Metro Manila Rail Stations**

> **Status: first turnover (work in progress).** This release covers most of the agreed scope.
> Items marked **⏳ Not yet done** below are still open and will be completed in a later turnover.
> Acceptance (Section 2.4) happens after the demonstration and the full documentation, recorded in the
> commissioning record.

ALERTO measures platform crowding with ceiling-mounted VL53L8CX 8×8 time-of-flight sensors on ESP32-S3
nodes. A Raspberry Pi gateway processes and stores the readings, serves a dashboard, forecasts crowding,
and provides a control/data output for the client's diorama ESP32. It uses no cameras (distance data only)
and runs fully offline.

```
VL53L8CX ─ ESP32-S3 node (Node ID) ──Wi-Fi──▶ Raspberry Pi gateway ──▶ Dashboard (browser)
                                               │  process + store readings
 (architecture supports up to 4 nodes)         │  crowd level, alerts, gating state, forecast
                                               └──▶ control/data output for the client's diorama ESP32
```

---

## Deliverables status

| Item | Deliverable (Section 2.1) | Status |
|---|---|---|
| (a) | ESP32-S3 sensor node with VL53L8CX, Node ID in every message | ✅ Done |
| (b) | Raspberry Pi gateway: receive, process, store, send to dashboard, diorama output | ✅ Done |
| (b) | Communication and message format documented | ⚠️ Partly done (summary below; full protocol document ⏳ not yet done) |
| (c) | Dashboard: live readings, Node ID, crowd level, alerts/gating state | ✅ Done |
| (c) | Dashboard refreshes every 5 minutes | ⏳ Not yet done (currently refreshes every 5–60 s, set in Settings) |
| (d) | Seasonal Naive forecast, 30 min ahead in 5-min steps, model name shown | ⏳ Not yet done (Seasonal Naive model not yet added) |
| (d) | Structure ready for ARIMA, ARIMAX, Prophet | ✅ Done |
| (e) | Demo with ≥1 real ToF node: ESP32-S3 → Pi → Dashboard, gating command, forecast | ⏳ Not yet done (scheduled for the demonstration) |
| (e) | Demo/offline data clearly labelled | ⏳ Not yet done (simulated data is not yet labelled on the dashboard) |
| (f) | Wiring and hardware setup | ✅ Done (this README) |
| (f) | Software installation and setup | ✅ Done (this README) |
| (f) | Configuration | ⚠️ Partly done (this README; full reference ⏳ not yet done) |
| (f) | Known issues | ⚠️ Partly done (list below) |
| (f) | Commissioning record | ⏳ Not yet done |
| (f) | Source code for (a)–(d) | ✅ Done (this repository) |
| 2.2 | Architecture supports 4 simultaneous ToF nodes, modular | ✅ Done |

---

## Repository layout
| Folder | Contents |
|---|---|
| `esp32_tof_reader/` | (a) Sensor node firmware: ESP32-S3 + VL53L8CX |
| `rpi/` | (b)–(d) Raspberry Pi gateway, dashboard and forecasting |
| `esp32_display_gate/` | Example client for the diorama output (see "Outside the agreed scope") |

---

## (a) ESP32-S3 sensor node

**Hardware:** ESP32-S3 DevKit + VL53L8CX breakout.

| VL53L8CX | ESP32-S3 |
|---|---|
| VIN | 3V3 |
| GND | GND |
| SDA | GPIO8 |
| SCL | GPIO9 |
| LPn | 3V3 |

**Firmware:** `esp32_tof_reader/esp32_tof_reader.ino`
- Arduino IDE library: **STM32duino VL53L8CX**.
- Tools: **ESP32S3 Dev Module**, USB CDC On Boot = Enabled, PSRAM = Disabled, CPU = 80 MHz (WiFi),
  Flash Size = 4MB.
- Set `NODE_ID` (unique per node: `platform-A` … `platform-D`) and `NODE_KEY` (= `DEVICE_KEY` on the Pi).
- Power up with the area under the sensor empty; the gateway learns the empty floor from the first ~30 frames.
- Serial Monitor (115200 baud) shows the restart reason, an I2C scan, start-up steps and a status line every
  second. Type `g` for the 8×8 grid, `v` to toggle the status line.

---

## (b) Raspberry Pi gateway

**Install** (Raspberry Pi 5, Raspberry Pi OS 64-bit, Ethernet connected for the first install):
```
cd rpi
nano config.env        # hotspot name, DEVICE_KEY, ADMIN_PIN
sudo ./setup.sh
```
- Creates the **CommuterDensity** Wi-Fi network (gateway at **10.42.0.1**) that the nodes join.
- Installs two services that start automatically on boot: `density-web` (gateway + dashboard, port 80)
  and `density-forecast` (forecasting).
- Re-running `setup.sh` updates the code and keeps data and settings.
- Logs: `journalctl -u density-web -f`, `journalctl -u density-forecast -f`.
- Fit the Pi 5 RTC battery so the clock is kept without internet.

### Communication and message format (summary)
⏳ A full protocol document is not yet done. Current messages:

**Node → gateway**, once per second: `POST http://10.42.0.1/frame`, header `X-Device-Key: <DEVICE_KEY>`
```json
{"device_id": "platform-A", "d": [64 distances in mm], "s": [64 VL53L8CX target status codes]}
```
`device_id` is the Node ID. Replies: `200` OK, `403` wrong key, `400` malformed frame.

**Gateway → diorama ESP32**, poll every 1–2 s: `GET http://10.42.0.1/api/display`
```json
{"gate": "open", "title": "GO TO ZONE A", "lines": ["Puno na ang Zone D.", "Pumunta sa Zone A."],
 "levels": "A:M B:L C:M D:H", "lang": "fil", "zone": "A"}
```
`gate` is the gating command (`open` / `closed`); `levels` gives each segment's crowd level
(L = Low, M = Moderate, H = High, - = no data).

**Gateway → dashboard:** JSON endpoints `/api/state`, `/api/live`, `/api/forecast`, `/api/history`,
`/api/settings`.

---

## (c) Dashboard
Join the **CommuterDensity** Wi-Fi and open **http://10.42.0.1**. The **Dashboard** tab shows:
- live 8×8 readings per node and the **Node ID** of each sensor
- current crowd level (Low / Moderate / High) per segment and for the platform
- alerts and the **gating state** (gate open / closed)

⏳ **Not yet done:** refresh interval of 5 minutes as specified. It currently refreshes every 5–60 seconds
(set in Settings).

---

## (d) Forecasting
- 30 minutes ahead in 5-minute steps, per segment, recomputed every 5 minutes. The model name is shown on the
  dashboard.
- The forecaster is modular: each model plugs into the same interface. ARIMA, ARIMAX and Prophet are already
  structured in (`rpi/forecaster.py`).

⏳ **Not yet done:** the **Seasonal Naive** model (the contracted model) is not yet added. Until it is,
the dashboard shows the ARIMA / ARIMAX / Prophet forecasts, which are outside the agreed deliverables.

---

## (e) Prototype demonstration
⏳ **Not yet done.** Plan:
1. One real node (ESP32-S3 + VL53L8CX) sending to the gateway.
2. Show the path ESP32-S3 → Raspberry Pi → Dashboard.
3. Show a threshold crossing and the gating command (`gate: closed`).
4. Show the 30-minute forecast.

Simulated data for testing (`rpi/simulate_data.py`) is stored with a marker so it can be removed
(`--clear`). ⏳ **Not yet done:** labelling simulated data as such on the dashboard.

---

## (f) Configuration
`rpi/config.env` (installed as `/etc/commuter-density.env`):

| Key | Meaning |
|---|---|
| `HOTSPOT_SSID` / `HOTSPOT_PASS` | Wi-Fi network the nodes join |
| `PORTAL_IP` | Gateway address (default 10.42.0.1) |
| `DEVICE_KEY` | Shared key; must equal `NODE_KEY` in the node firmware |
| `ADMIN_PIN` | Staff PIN for the dashboard Settings tab |

Dashboard **Settings** tab: crowd-level thresholds, node-to-segment mapping, alert lead time, refresh
interval, data retention, language.
⏳ A full configuration reference is not yet done.

---

## Known issues
- **Clock without internet.** Without the Pi 5 RTC battery, timestamps are wrong after a power-off until the
  Pi is connected to the internet once.
- **Counts are estimates.** People standing very close together can merge into one shape and be undercounted.
- **Forecasts need data.** Forecasting starts after 3 days of readings per segment.
- See the deliverables table above for items not yet done.

---

## Outside the agreed scope
Included for development and testing only; not part of the Deliverables (Section 2.3):
- ARIMA, ARIMAX and Prophet forecasts (the contract requires only that the structure supports them)
- `esp32_display_gate/`: example OLED + servo client for the diorama output
  (the client's diorama ESP32 firmware is excluded)
- History and Forecast dashboard tabs, captive portal
