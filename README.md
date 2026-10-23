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

## Revamp

- Gateway 1.2.3 and node firmware v2 run on the real Raspberry Pi and node.
- [Latency measurements](docs/latency-log.md)
- [Turnstile bench contract](docs/bench-contract.md)
- [Improvement backlog](docs/improvement-backlog.md)
- [Node v0 backup](backups/node-v0/README.md)

## Deliverables status

| Item | Deliverable (Section 2.1) | Status |
|---|---|---|
| (a) | ESP32-S3 sensor node with VL53L8CX, Node ID in every message | ✅ Done |
| (b) | Raspberry Pi gateway: receive, process, store, send to dashboard, diorama output | ✅ Done |
| (b) | Communication and message format documented | ⚠️ Partly done (summary below; full protocol document ⏳ not yet done) |
| (c) | Dashboard: live readings, Node ID, crowd level, alerts/gating state | ✅ Done |
| (c) | Dashboard refreshes every 5 minutes | ⏳ Not yet done (live updates over `/api/stream` within about a second of each frame; when the stream is unavailable, polls at the Settings interval, 5-60 s) |
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

**Deployment status (2026-10-23):** Node firmware v2 now runs on the node, installed over USB
on 2026-10-23, then updated over Wi-Fi. V1 with non-blocking USB logging passes a 30-minute
closed-port soak on the real Pi, followed by the stream and display probes.
The two earlier candidates were rolled back to v0. See `docs/latency-log.md` for the measured
results, USB logging rule, rollback history and safe serial detach.

**Hardware:** ESP32-S3 N16R8 + VL53L8CX breakout.

| VL53L8CX | ESP32-S3 |
|---|---|
| VIN | 3V3 |
| GND | GND |
| SDA | GPIO8 |
| SCL | GPIO9 |
| LPn / I2C reset | Not wired to ESP32 GPIOs |

**Firmware:** `esp32_tof_reader/esp32_tof_reader.ino`

- Arduino library: **STM32duino VL53L8CX 2.1.0**; ESP32 core **3.3.12**.
- Tools: **ESP32S3 Dev Module**, Flash Size = **16 MB**, Partition Scheme =
  **16M Flash (3MB APP/9.9MB FATFS)** (`app3M_fat9M_16MB`), Flash Mode = **DIO 80 MHz**,
  PSRAM = **Disabled**, CPU = **80 MHz (WiFi)**, USB Mode = **Hardware CDC and JTAG**,
  USB CDC On Boot = **Enabled**, Arduino Runs On = **Core 1**, Events Run On = **Core 1**,
  Upload Mode = **UART0 / Hardware CDC**, Upload Speed = **921600**, Erase All Flash = **Disabled**.
- Copy `esp32_tof_reader/secrets.example.h` to `esp32_tof_reader/secrets.h`, then set
  `WIFI_SSID`, `WIFI_PASS`, `NODE_KEY`, `NODE_ID` and `OTA_PASSWORD` there. The local header is gitignored.
  Use an empty password for an open hotspot. `NODE_ID` is unique per node (`platform-A` to
  `platform-D`); `NODE_KEY` must match the Pi's `DEVICE_KEY`.
- Samples **8x8 at 4 Hz**, autonomous mode, **10 ms integration**, I2C **400 kHz**.
  Sends **every second frame (2 Hz)** to `http://10.42.0.1/frame` with `X-Device-Key` and
  JSON fields `device_id`, `d[64]`, `s[64]`, `seq`. The network task assigns `seq` from
  **1 after boot**, increasing by exactly **1 per POST attempt**, including failed attempts.
- Sensing stays on core 1; the core-0 network task takes the newest frame from a one-slot overwrite queue.
  Wi-Fi sleep is off, TX power stays at **8.5 dBm**, and HTTP/1.1 connections are reused with a
  **2000 ms** timeout. Wi-Fi retries use backoff without waiting in the sensing loop.
- Startup probes **0x29** directly and runs a full diagnostic scan only if that probe fails.
  Sensor initialization precedes Wi-Fi; ranging starts afterward. The sensor restarts after
  **3 seconds without frames**, at most once per **10 seconds**. The node logs a reason and reboots
  after **5 minutes without a successful POST**; boot-time sensor retries share the same
  **5-minute budget** and then reboot, so a newly installed image that cannot start the sensor
  is rolled back to the previous image.
- Power up with the area under the sensor empty; the gateway learns the empty floor from the first ~30 frames.
- Native USB Serial/JTAG logs at **115200 baud** include timestamps and the reset reason, including brownout.
  Type `g` for the **8x8 grid in cm**, `v` to toggle the once-per-second status line.
  Valid zones in the logs have status **5 or 9** and distance **greater than 0**.
  Status includes sent/failed counts, `seq`, last POST duration (`post`, ms), and `lag`
  (ms from frame ready to POST completion).
- USB logging uses a zero TX timeout and drops lines unless HWCDC is connected and a whole line
  fits immediately. Only core 1 prints; network results cross a short numeric snapshot, with no
  logging lock or queue. If `seq` does not advance for 10 seconds while Wi-Fi is up, the network
  task restarts the HTTP client. The 5-minute successful-POST watchdog remains active.

Build FQBN used for the real node:

```text
esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,CPUFreq=80,FlashMode=dio,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB,PSRAM=disabled,LoopCore=1,EventsCore=1,EraseFlash=none
```

### Wireless update

Install v2 once over USB. Later updates use ArduinoOTA from ESP32 core 3.3.12 and the two existing
3 MB app slots. Set a unique `OTA_PASSWORD` of at least 12 characters in `secrets.h`; missing or
short or example-placeholder passwords fail compilation. Keep the same password for subsequent builds.
The hostname is `alerto-<NODE_ID>.local`, for example `alerto-platform-A.local`.

Push from the Pi because the node must connect back to the sender on its hotspot. Set the local
`PI_HOST` environment variable to the Pi's existing, trusted SSH hostname or LAN address, and set
`ESP32_CORE` and `NODE_BIN` to the installed core directory and compiled sketch `.bin` file.
Copy the core's unmodified `tools/espota.py` and the app binary into the Pi's home directory:

```powershell
ssh -o BatchMode=yes "admin@$env:PI_HOST" 'mkdir -p ~/alerto-ota'
scp "$env:ESP32_CORE\tools\espota.py" "$env:NODE_BIN" "admin@$($env:PI_HOST):alerto-ota/"
ssh -o BatchMode=yes "admin@$env:PI_HOST" 'ip neigh show dev wlan0'
[Environment]::GetEnvironmentVariable('ALERTO_OTA_PASSWORD', 'User') | ssh -o BatchMode=yes "admin@$env:PI_HOST" 'read -r pw; pw=${pw%$(printf "\r")}; python3 ~/alerto-ota/espota.py -i alerto-platform-A.local -I 10.42.0.1 -P 3233 -f ~/alerto-ota/esp32_tof_reader.ino.bin -a "$pw"'
```

Store the matching password in the Windows user environment variable `ALERTO_OTA_PASSWORD` before
running the push command. If mDNS is unavailable, replace the node hostname with its hotspot address
from `ip neigh`. The node listens on UDP 3232 and connects back to Pi TCP 3233; if that connection is
blocked, stop and check the network policy before changing any firewall or hotspot settings.
Do not enable espota debug output, which prints its authentication options.
During a push, espota.py takes the password as `-a`, making it visible in the Pi's process list, so push only from a Pi account nobody else uses.

Authentication runs in a separate task so password checks leave sampling and POSTs scheduled.
Ranging and POSTs pause during the transfer, and both network watchdogs exclude that pause.
A rejected password leaves normal operation running; transfer errors resume ranging and POSTs
without a reboot. A successful transfer reboots into the other app slot. With the core 3.3.12
rollback-enabled bootloader, the new image becomes valid only after its first HTTP 200 POST to
the Pi. If no POST succeeds, the existing five-minute watchdog reboots and restores the previous
image. Keep the previous working USB build available for recovery.

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

The gateway first marks valid zones more than 400 mm above the learned background, then groups
zones that share an edge. A group counts toward occupancy only if its highest return reaches
`MIN_PEAK_MM` (700 mm by default). This removes low wall flicker while keeping lower arm or edge
zones attached to a taller body return. A single tall zone still counts toward occupancy.
Background drift uses the original, unfiltered zone mask. Set `MIN_PEAK_MM=0` to restore the
previous rule; calibration files and the `/api/display` response format stay the same.
Set `MIN_PEAK_MM="700"` in `/etc/commuter-density.env` on the Pi, then restart `density-web`
with `sudo systemctl restart density-web`. Re-running `setup.sh` keeps the existing env file
unless `--reset-config` is supplied.

### Communication and message format (summary)
⏳ A full protocol document is not yet done. Current messages:

**Node → gateway**, twice per second (2 Hz): `POST http://10.42.0.1/frame`, header `X-Device-Key: <DEVICE_KEY>`
```json
{"device_id": "platform-A", "d": [64 distances in mm], "s": [64 VL53L8CX target status codes], "seq": 123}
```
`device_id` is the Node ID. `seq` is optional: a whole number that increases by 1 per POST attempt (restart from
1 after a reboot), so a gap at the gateway means a frame that did not arrive. It lets the gateway's latency
probe measure received frames (`docs/latency-log.md`). A number longer than 4300 digits
fails JSON parsing and the frame is rejected, so keep it a plain counter. Replies: `200` OK, `403` wrong key, `400` malformed frame.

**Gateway → diorama ESP32**, poll every 1–2 s: `GET http://10.42.0.1/api/display`
```json
{"gate": "open", "title": "GO TO ZONE A", "lines": ["Puno na ang Zone D.", "Pumunta sa Zone A."],
 "levels": "A:M B:L C:M D:H", "lang": "fil", "zone": "A"}
```
`gate` is the gating command (`open` / `closed`); `levels` gives each segment's crowd level
(L = Low, M = Moderate, H = High, - = no data).

**Gateway → dashboard:** JSON endpoints `/api/state`, `/api/live`, `/api/forecast`, `/api/history`,
`/api/settings`.
`/api/stream` is a Server-Sent Events stream carrying live state updates to the dashboard.

---

## (c) Dashboard
Join the **CommuterDensity** Wi-Fi and open **http://10.42.0.1**. The **Dashboard** tab shows:
- live 8×8 readings per node and the **Node ID** of each sensor
- current crowd level (Low / Moderate / High) per segment and for the platform
- alerts and the **gating state** (gate open / closed)

⏳ **Not yet done:** refresh interval of 5 minutes as specified. The dashboard receives live updates over
`/api/stream` within about a second of each frame; when the stream is unavailable, it polls at the Settings
interval (5-60 s).

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
