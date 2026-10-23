# Improvement backlog

Planned changes, recorded before any code changes. Every change lands as a commit or PR in this repository so
improvements and regressions stay traceable. Baseline node firmware: [`backups/node-v0`](../backups/node-v0/).

## Done before the defense

- PR #2: gateway 1.1.1 adds sequence-aware latency measurement with `rpi/tools/latency_probe.py` and `fake_node.py`.
- PR #3: the turnstile bench contract, response and gate tests, and `rpi/tools/fake_bench.py` lock down `/api/display`.
- PR #4: gateway 1.2.0 adds a `StateBuilder` thread, one gate decision per rebuild, `/api/stream` with 4 streams at up to 4 events/s each, a boot id, fast startup 503, a 30 s client watchdog and gated forecast reloads.
- PR #6: node firmware v1 adds 4 Hz sampling, 2 Hz sends, `seq` per POST attempt, core-0 networking, reused HTTP/1.1, Wi-Fi sleep off, non-blocking USB logs, a 10 s seq-stall HTTP restart, a 5-minute no-success reboot and gitignored `secrets.h`.
- Guidance selects the segment with the lowest current occupancy and keeps forecast values for existing consumers.
- Forecasts expire after 15 minutes or when their first point is more than 10 minutes in the past.
- Forecast charts handle empty segment points, and models without points return ready: false.
- The bench receives a fast 503 when the last successful state rebuild is more than 10 seconds old.
- The dashboard hides zone guidance during Hold, clarifies reporting coverage, and uses darker status labels.

## Problem: sensor-to-dashboard lag of several seconds

Baseline as found on 2026-10-22, before the revamp; current measurements are in docs/latency-log.md.

Where the delay comes from, in path order, taken from the code constants:

| Stage | Source | Delay |
|---|---|---|
| Sensor ranging (node v0: 2 Hz autonomous; repo sketch: 5 Hz) | `RANGING_HZ` | 0 to 0.5 s |
| Node posts once per second | `SEND_EVERY_MS = 1000` | 0 to 1 s |
| New HTTP connection per frame, Wi-Fi modem sleep on, TX power 8.5 dBm | `sendFrame()`, `setup()` | usually under 0.5 s; up to the 2 s timeout on a lost packet |
| Dashboard crowd level, gate and alerts poll `/api/state` every `refresh_s` (default 10 s, Settings offers 5/10/30/60 s) | `rpi/settings.py`, `rpi/static/index.html` | **0 to 10 s, the dominant cost** |
| Dashboard 8x8 grid polls `/api/live` | `rpi/static/index.html` | 0 to 1 s |
| Gate display ESP32 polls `/api/display` | `esp32_display_gate.ino` `POLL_MS = 2000` | 0 to 2 s |

Also: a Wi-Fi drop blocks the node loop for up to 8 s in `ensureWiFi()`.

End-to-end latency had not been measured at that point; the figures above are bounds from the constants.

### Measured on the running system (2026-10-22, read-only)

- Gateway `1.1.0`, the version in this repository at the time. Endpoint response time over the LAN, median of 8:
  `/api/state` 12 ms, `/api/live` 6 ms, `/api/display` 7 ms, `/api/forecast` 13 ms. Server work per request
  is not the bottleneck; the polling intervals are.
- Dashboard `refresh_s` is 10 on the running gateway. Node `platform-A` delivers one frame per second.
- Node v0 log: 6.4 s from power-on to ranging (2.0 s I2C scan, 2.9 s sensor firmware load), Wi-Fi RSSI -70 dBm
  at association and -66 to -68 dBm while running, at 8.5 dBm TX power, 14 of 14 POSTs OK, CPU at 80 MHz.

## Software (now)

Ordered by expected impact. Each item gets its own commit or PR with a before/after measurement.

1. **Measure first.** Add a frame sequence number and node send time to each frame; log receive, process and
   dashboard-delivery times on the Pi. This becomes the baseline for every later change.
   Partly done (PR #2): sequence-aware gateway receive-to-delivery probes are in place; node send timestamps remain unimplemented.
2. **Push live state to the dashboard.** Server-Sent Events stream from the Pi, sent on every processed frame;
   the dashboard listens instead of polling. The stream is capped at 4 events/s; polling fallback uses the
   Settings interval (5-60 s). Done (PR #4).
3. **Compute state once per frame on the Pi.** Build the state, including the gate decision, when a frame is
   processed, and serve that same copy to `/api/state`, `/api/display` and the stream. The gain is consistency
   (see C2), not server time, which measures about 12 ms. Keep one database connection instead of opening one
   per frame. Partly done (PR #4): shared state and one gate decision per rebuild are in place; database connection reuse remains.
4. **Gate display gets the same push** (or polls at 0.5 to 1 s against the cached state). Partly done (PR #4):
   `/api/display` reads the cached state; the example display still polls every 2 s and has no push client.
5. **Node send path.** Partly done (PR #6): compact binary frames remain deferred.
   - `WiFi.setSleep(false)` to remove modem-sleep wake-up delay. Done (PR #6).
   - Reuse one connection (HTTP keep-alive), or move to MQTT or UDP. The design documents specify Wi-Fi/MQTT. HTTP/1.1 reuse is in place. Done (PR #6).
   - Compact binary frame (192 bytes) instead of up to about 600 bytes of JSON.
6. **Node sampling to the design target:** sample at 4 Hz and send every second frame (2 Hz). Done (PR #6).
7. **Split node work across cores:** sensor reading on one core, networking on the other, so a slow POST or a
   Wi-Fi reconnect never stalls sensing. Done (PR #6).
8. **Rebuild the node firmware from source in this repo** carrying over v0 behaviour (status 5/9 rule,
   reset-reason and brownout log, `g`/`v` commands) so the source matches what is flashed. Done (PR #6).
9. **Use the board as built:** 16 MB flash setting and PSRAM enabled (ESP32-S3 N16R8), and send logs to the
   port the cable actually uses (USB CDC On Boot off when on the CH343 port).
   Partly done (PR #6): 16 MB flash and native USB Serial/JTAG logging are configured; PSRAM remains disabled.
10. **Back up the gateway on the Pi before changing it:** `/opt/commuter-density` code and data, and the
    systemd units. `/etc/commuter-density.env` holds the device key, admin PIN and hotspot password, so it is
    backed up privately and never committed.

11. **Wireless node updates while mounted.** Done (node v2): password-protected ArduinoOTA, a required
    12-character minimum at build time, paused ranging/POSTs/watchdogs during transfers, and rollback
    until the first successful Pi POST. Wrong-password continuity, a real wireless update, automatic
    rollback from a wrong-URL image, slot readbacks and a 30-minute closed-port soak pass.

## Software: consistency and reliability

Found in a second pass. These make the system show or act on the wrong state, which matters as much as raw
speed.

- **C1. A dead node stays "online" with old data for up to 3 minutes.** When a segment's live frame is older
  than 15 s, `segment_states()` (`rpi/app.py`) falls back to the average of the last 3 minutes of stored rows
  and marks the segment online. The level, alerts and gate keep acting on data from a node that stopped.
  Fix: mark the segment offline after a few missed frames (about 3 s at 1 Hz) and keep the minute fallback
  only for firmware that posts `/ingest` rows.
- **C2. The gate only updates when someone asks.** `update_gate()` runs inside `build_state()`, so the gate
  and its hysteresis advance only when a dashboard or the gate display requests `/api/state` or
  `/api/display`. Timing depends on who is polling, and with no client the gate never changes. Fix: decide
  the gate once per processed frame (item 3); endpoints only read it. Done (PR #4).
- **C3. One noisy frame can switch the gate.** Occupancy is computed per frame with no smoothing, and the gate
  has only a 5-point hysteresis and no minimum time in a state. Fix: smooth over a few seconds and require a
  minimum dwell before switching. Also decide which rule governs: the gateway closes when every segment with
  data is at or above High (percent of floor zones occupied) and reopens 5 points below it, while the
  Chapter 3 policy the simulator implements counts people: Hold at the aggregate Hold count or when two
  segments reach the 85% per-segment trigger, leave Hold (to Metered) only when the aggregate is at or below
  the release count, no segment trigger or forecast trigger is active, and staff confirm the release (mock
  platform: 61 of 64, 14 of 16 per segment, 51 of 64).
- **C4. The dashboard mixes two snapshots.** The 8x8 grids come from `/api/live` (1 s) but their colour, the
  tiles and the frame-age check come from `/api/state` (10 s). The grid can show a crowd while the tile still
  says Low, and "No new frames" is computed against a state clock that may be 10 s old, so it can appear
  20 to 30 s late. Fix: one payload for both (item 2). Done for the stream path (PR #4); the polling fallback still reads `/api/state` and `/api/live` separately.
- **C5. Liveness uses the wall clock.** Frame ages are `datetime.now()` differences. A clock step (NTP sync
  now that the Pi has Ethernet, or a missing RTC battery) makes ages negative or huge and flips segments
  online/offline. Fix: monotonic time for ages, wall time only for stored timestamps; fit the RTC battery
  before running offline.
- **C6. Any client on the open hotspot can feed or flood the gateway.** The running node's key is in this
  public repository, and `/frame` creates a new sensor and database rows for any `device_id`. A spoofed or
  misconfigured node can close the gate. Fix: rotate `DEVICE_KEY`, move the node key out of source into a
  gitignored header, and accept only device ids assigned in Settings. The v0 backup image carries the old
  key too, so restoring it after a rotation needs the old key re-enabled or a rebuilt image.
- **R1. Node self-recovery.** Add the ESP32 task watchdog and a reboot after a set time without a successful
  POST. Partly done (PR #6): reboot after 5 minutes without a successful POST is in place; the task watchdog
  remains open and outage recovery is not fault-tested.
- **R2. Gate display fail-safe is late.** Its `ensureWiFi()` blocks for up to 8 s per poll, so the 15 s
  offline fail-safe can take over 20 s. Use non-blocking reconnects. Keep the `/api/display` contract
  unchanged, because the turnstile bench reads it too.

## Deferred node firmware fixes (from the PR #6 review)

- Wi-Fi retries cancel in-flight joins in the `esp32_tof_reader/esp32_tof_reader.ino` reconnect loop.
- Drain the frame queue when the sensor restarts so a pre-stall frame is not sent.
- A sensor failure at boot blocks networking forever.
- The reboot reason can be lost; persist it across reboot.
- Run a multi-hour soak with heap and stack figures and fault tests (hotspot off/on, gateway down).

## Found in the 2026-10-22 read-only scans (after the defense)

- Minute rows can duplicate on backwards clock steps and repeated ingestion (no unique bucket key; averages are unweighted).
- The in-progress minute is lost on restart because there is no shutdown flush.
- Invalid frames read as 0% occupancy because usable-zone coverage is not checked.
- Forecast readiness measures elapsed span rather than observed coverage.
- Backtests fill gaps before the train/test split and compare models at unequal cutoffs.
- Scores pool all six leads under a "30-minute-ahead" label.
- The offline last-seen query scans the whole device history each rebuild.
- Full-range history takes about 1.2 s on 90 days.
- The calibration file is written non-atomically and drift is not persisted.
- The flusher thread dies on a database error.
- Concurrent settings saves lose updates.
- Remapping sensors does not invalidate forecasts.
- Retention only runs after a successful forecast.
- The History chart joins sampled days across missing days.
- The grid height toggle has no keyboard access.
- "Last Updated" reflects the hub rather than sensor freshness.

## Software: speed and snappiness (smaller items)

- **S1. Faster node boot and recovery** (6.4 s today). Probe only address 0x29 instead of a full 2 s I2C scan,
  and load the sensor firmware at a higher I2C clock if the VL53L8CX datasheet allows it (load takes 2.9 s
  at 400 kHz).
- **S2. Node CPU at 80 MHz.** 160 or 240 MHz shortens frame building and network handling; check supply
  current first (see the hardware section).
- **S3. Raise Wi-Fi TX power after ranging starts.** RSSI is -66 to -68 dBm while running (-70 at association) at 8.5 dBm TX, so lost packets
  and retries are likely under load. Raise it in stages and watch the reset reason for brownouts.
- **S4. Hotspot channel.** Fixed at 2.4 GHz channel 6 in `setup.sh`. In a busy room this channel may be
  congested; make it configurable and check the site before a demo.
- **S5. Faster offline alerts.** The live path drops a segment's frames after 15 s, but the segment then
  stays online on the minute fallback for up to 3 minutes (C1). 3 to 5 s is enough at 1 to 2 Hz; this
  lands with C1.
- **S6. Settings tab query.** `SELECT DISTINCT device_id FROM readings WHERE frames >= 0` cannot use the
  existing indexes for the `frames` filter, and at 90-day retention with 4 nodes the table reaches about
  518,000 rows. Unmeasured; track known devices separately or add an index.
- **S7. Forecaster load.** SARIMAX and Prophet refit on the same Pi every 6 h and at start; the
  ARIMA order search re-runs weekly.
  Measure web latency during a refit; cap the forecaster's CPU if it slows the gateway.
- **S8. Dashboard housekeeping.** Pause polling and drawing when the tab is hidden, and skip chart redraws
  when the data has not changed.
- **S9. Waitress threads.** A Server-Sent Events stream holds one of the 8 worker threads per open dashboard.
  Raise the thread count or cap the number of streams when item 2 lands.

## Hardware (deferred, not possible now)

The node uses four wires between the VL53L8CX board and the ESP32-S3: GND, SCL/CLK to GPIO9, SDA/MOSI to
GPIO8, VIN to 3V3. That is enough for I2C mode. Planned changes for a later hardware pass:

- **LPn to GPIO10.** Allows a hardware reset of a hung sensor instead of a software restart. Firmware already
  has the slot (`LPN_PIN`).
- **INT to a spare GPIO** (optional). Read each frame when it is ready instead of polling the sensor over I2C.
- **Check the sensor supply before anything else.** VIN is fed from the ESP32 3V3 pin. Find out whether the
  module has its own regulator on VIN and measure the voltage after it while ranging and transmitting. The v0
  firmware carries a brownout message in its reset-reason report and runs Wi-Fi at reduced power, which
  suggests (unconfirmed) a supply concern; no brownout has been recorded. Low TX power also adds network delay.
- **Measure the mode pins while running:** SPI_I2C_N about 0 V (I2C selected) and LPn about 3.3 V, to
  confirm the module's onboard pull-down and pull-up.
- **Wire colours:** GND is white and SCL is black. Relabel or rewire with black for GND.
- MISO and NCS stay unconnected (SPI only). SPI mainly shortens the sensor firmware load at boot; it does not
  address the dashboard lag.

Unverified: whether this module has onboard pull-ups, pull-downs or a regulator, and the VL53L8CX supply
limits. Check the module and the ST datasheet before rewiring.
