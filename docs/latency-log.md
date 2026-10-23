# Latency log

One entry per measurement. Every change aimed at speed adds a new entry with the same probe.

## What the probe measures

`rpi/tools/latency_probe.py` reads `/api/stream` or polls `/api/state` and reports, using only the gateway clock:

- `frame_to_screen_ms`: for received frames, gateway receive time to the first dashboard fetch that shows it or a
  newer frame. This is the delay a person watching the dashboard experiences. Receive times of frames
  between two polling fetches are interpolated from their sequence numbers (assumes a steady send rate).
  Needs nodes that increase `seq` by 1 per POST attempt, so a gap at the gateway means a frame that did not
  arrive. Without `seq` the probe reports only `age_at_fetch_ms` and warns. The first fetch is warm-up and
  not counted; a pause in fetching or a node reboot (seq going down) resets the chain instead of inventing
  frames (`resets`). So does a seq jump larger than the elapsed
  receive time allows at 50 Hz, which catches nodes that send a timestamp instead of a counter. A fetch
  more than `2 x interval + 1 s` after the previous one (probe paused, gateway restart) also resets the chain.
- `age_at_fetch_ms`: age of the newest frame at each fetch. On its own it understates dashboard latency.

Stream mode uses the event's integer `sent_ms` as visibility time, including the stream throttle wait, and
falls back to snapshot `now_ms` for older gateways. It sees every rebuild, so forward sequence gaps count
as `frames lost` and are excluded from `frame_to_screen_ms`. Polling mode keeps interpolating gaps and
counting them as `frames skipped`, because those gaps are mostly frames skipped between fetches.
The CSV gap column is `lost` in stream mode and `skipped` in polling mode.

Not covered: sensor acquisition, node processing, Wi-Fi transit, browser rendering.

## 2026-10-22: baseline, gateway 1.1.1, local laptop

Setup: Windows 11 laptop, fake node at 1 Hz with `seq` (`tools/fake_node.py`), gateway `1.1.1`, Python
`3.13.14`, Waitress `3.0.2` with 8 threads at `127.0.0.1:8090`. NOT the Raspberry Pi and NOT a real node.
Both probes ran at the same time after the 30-frame calibration.

| Run | Printed output (verbatim) |
| --- | --- |
| `latency_probe.py --interval 10 --duration 120` (current dashboard default) | `interval=10 s; responses=12; errors=none; frames shown=12; frames skipped=99; resets=0; frame_to_screen_ms min=74 / median=4575 / p95=9075 / max=9076 (n=110); age_at_fetch_ms min=71 / median=74 / p95=76 / max=76; mean /api/state request time=4.382 ms` |
| `latency_probe.py --interval 1 --duration 60` | `interval=1 s; responses=60; errors=none; frames shown=60; frames skipped=0; resets=0; frame_to_screen_ms min=81 / median=82 / p95=83 / max=84 (n=59); age_at_fetch_ms min=78 / median=82 / p95=83 / max=84; mean /api/state request time=3.668 ms` |
| `fake_node.py --hz 1 --duration 200` | `frames sent=200; failures=0; mean POST time=3.645 ms` |

Reading: at the 10 s default, half of all frames take more than 4.5 s to reach the dashboard and the worst
take about 9 s. Server time is about 4 ms per request, so the polling interval is the cause. The 1 s run is
optimistic: probe and node both run at exactly 1 Hz, so every fetch landed just after a frame. A real
browser polling at 1 s sees 0 to 1 s.

The running node firmware (v0) sends no `seq`, so on the Pi this probe can only report `age_at_fetch_ms`
until the node firmware adds it.

## 2026-10-22: live push, gateway 1.2.0, local laptop under load

Setup: Windows 11 laptop, gateway `1.2.0`, Python `3.13.14`, Waitress `3.0.2` with 8 threads at
`127.0.0.1:8090`. Four fake nodes (`platform-A` to `D`) at 2 Hz with `seq`, all four stream slots held by
probes, plus the bench client (`tools/fake_bench.py`) and a 1 s poller at the same time. NOT the Raspberry Pi
and NOT a real node. Stream figures use the event's `sent_ms`, including the throttle wait; delivery and
browser rendering are not included (localhost delivery is negligible).

| Run | Printed output (verbatim) |
| --- | --- |
| `latency_probe.py --stream --duration 95` (stream 1) | `stream; responses=372; errors=none; frames shown=191; frames skipped=0; resets=0; frame_to_screen_ms min=5 / median=22 / p95=36 / max=67 (n=190); age_at_fetch_ms min=5 / median=22 / p95=36 / max=72; stream connections=1` |
| `latency_probe.py --stream --duration 95` (stream 2) | `stream; responses=371; errors=none; frames shown=191; frames skipped=0; resets=0; frame_to_screen_ms min=5 / median=21 / p95=37 / max=64 (n=190); age_at_fetch_ms min=5 / median=21 / p95=37 / max=85; stream connections=1` |
| `latency_probe.py --stream --duration 95` (stream 3) | `stream; responses=371; errors=none; frames shown=191; frames skipped=0; resets=0; frame_to_screen_ms min=5 / median=21 / p95=36 / max=62 (n=190); age_at_fetch_ms min=5 / median=21 / p95=36 / max=91; stream connections=1` |
| `latency_probe.py --stream --duration 95` (stream 4) | `stream; responses=372; errors=none; frames shown=191; frames skipped=0; resets=0; frame_to_screen_ms min=5 / median=22 / p95=36 / max=66 (n=190); age_at_fetch_ms min=5 / median=22 / p95=36 / max=92; stream connections=1` |
| `latency_probe.py --interval 1 --duration 85` (polling fallback) | `interval=1 s; responses=85; errors=none; frames shown=85; frames skipped=84; resets=0; frame_to_screen_ms min=34 / median=301 / p95=539 / max=567 (n=168); age_at_fetch_ms min=34 / median=36 / p95=40 / max=67; mean /api/state request time=1.313 ms` |
| `fake_bench.py --duration 85` | `polls=85; valid=85; errors=none; response_ms median=3.2 / p95=25.8 / max=26.9; longest_gap_s=1.0 (fail-safe at 30); gate changes=[(0.0, 'open')]` |
| `fake_node.py --device platform-A --hz 2 --duration 130` | `frames sent=260; failures=0; mean POST time=3.480 ms` |
| `fake_node.py --device platform-B --hz 2 --duration 130` | `frames sent=260; failures=0; mean POST time=3.411 ms` |
| `fake_node.py --device platform-C --hz 2 --duration 130` | `frames sent=260; failures=0; mean POST time=3.402 ms` |
| `fake_node.py --device platform-D --hz 2 --duration 130` | `frames sent=260; failures=0; mean POST time=3.626 ms` |

Reading: with the dashboard on the stream, frame-to-screen falls from a median of 4575 ms (10 s polling
baseline) to 21-22 ms, p95 from 9075 ms to 36-37 ms across the four streams, with no skipped frames. Each
stream delivers 371-372 events over 95 s. The bench's `/api/display` stays at 26.9 ms worst case with every
stream slot taken, well inside its 250 ms timeout.

## 2026-10-22: real node v1 on the Pi

Setup: one real ESP32-S3 N16R8 node (`platform-A`, VL53L8CX on GPIO8/9 at 400 kHz), real
Raspberry Pi 5 gateway **1.2.0**, and a Windows 11 laptop over the LAN. The laptop requests
`http://PI_LAN_ADDRESS` with `Host: 10.42.0.1`; the node uses the open `CommuterDensity`
hotspot and posts to `http://10.42.0.1/frame`. Sampling is 4 Hz autonomous with 10 ms integration;
sending is 2 Hz. CPU remains 80 MHz, TX remains 8.5 dBm, and PSRAM is disabled.
Built with Arduino CLI 1.5.1, ESP32 core 3.3.12 and STM32duino VL53L8CX 2.1.0 using the node
settings in the README. Flash use is 1,040,017 / 3,145,728 bytes; static RAM is
52,104 / 327,680 bytes.

Before flashing v1, v0 was restored from all four backup images. Independent flash read-backs of
bootloader, partition table, otadata and app0 matched every SHA-256 in the backup README.
Restored serial showed the v0 banner and `Pi: OK`, with zero failed sends.

The stream probe, 1 s poller, fake bench and 120 s serial capture ran together from about
16:45:25 PHT. One serial connection stayed open after the 30 s boot capture, so starting acceptance
did not reset the board. The boot capture confirms the direct 0x29 probe, reset reason,
sensor-first startup, core-0 networking, core-1 sensing, 4 Hz ranging and the `g` / `v` commands.

The commands below use the scripts in `rpi/tools/`. Stream CSV output was also saved to count sequence gaps.

`PI_LAN_ADDRESS` stands for the Pi's address on the lab LAN; replace it before running a command. On the Pi's own hotspot use 10.42.0.1 directly.

`latency_probe.py --stream --base http://PI_LAN_ADDRESS --host-header 10.42.0.1 --duration 120`:

```text
stream; responses=241; errors=none; frames shown=241; frames skipped=0; resets=0; frame_to_screen_ms min=2 / median=2 / p95=10 / max=174 (n=240); age_at_fetch_ms min=2 / median=2 / p95=10 / max=174; stream connections=1
```

`latency_probe.py --base http://PI_LAN_ADDRESS --host-header 10.42.0.1 --interval 1 --duration 60`:

```text
interval=1 s; responses=60; errors=none; frames shown=60; frames skipped=59; resets=0; frame_to_screen_ms min=1 / median=176 / p95=506 / max=664 (n=118); age_at_fetch_ms min=1 / median=2 / p95=2 / max=3; mean /api/state request time=3.919 ms
```

`fake_bench.py --url http://PI_LAN_ADDRESS/api/display --host-header 10.42.0.1 --duration 60`:

```text
polls=60; valid=60; errors=none; response_ms median=7.3 / p95=30.5 / max=31.9; longest_gap_s=1.0 (fail-safe at 30); gate changes=[(0.0, 'open')]
```

Serial figures from 120 once-per-second status samples:

- `post` ms: min 67, median 77, p95 96, max 291.
- `lag` ms (frame ready to POST complete): min 69, median 79, p95 101, max 293.
- Status sequence 49 to 288 over 119.16 s between the first and last samples, about 2.006 POSTs/s.
  Sensor frame count rises from 99 to 577, about 4.011 frames/s.
- Failed POST counter stays at 0, with no POST error lines, reboot, brownout or sensor watchdog restart.

During measurement, the Pi's segment A is online with `source: live`, integer `seq` 79 then 83 over 2.008 s,
`last_seen_s: 0`, occupancy 32.8%, level moderate and an estimated 6 people. Occupancy matches the
pre-flash v0 snapshot. The stream CSV contains all 241 consecutive sequences from 50 through 290:
0 missing over 240 sequence increments, **0.0% gaps**, with no reset.

The initial 120 s window passes: zero failed POSTs, sequence gaps below 1%, stream p95 **10 ms**
below 500 ms, and fake bench **60/60 valid**, with no reboot or brownout.
The 1 s polling fallback skips intermediate frames as expected at a 2 Hz send rate; its p95 is
506 ms. Sequence-loss acceptance uses the stream rather than that polling skip count.

**Final v1 acceptance fails.** At 16:55:28 PHT, the final read-only `/api/state` check shows
segment A offline, `last_seen_s: 411`, `source: null`, `seq: null`, and unknown occupancy.
The serial session is already closed, so there is no serial evidence of the later failure.
The cause of that observation is not established from v1 serial data. No further v1 flash is attempted.

**Rolled back to v0.** The four backup images are written with the same `keep` settings as the
initial restore test, and all four independent flash read-back SHA-256 values match again.
The final 15 s serial capture shows the v0 banner, 0x29, Wi-Fi joined, 2 Hz ranging and
11 successful `Pi: OK` sends with zero failures. COM7 is closed after the capture.

The post-close problem also reproduces on unchanged v0, including with periodic logging turned off
using `v`. The original capture helper configures DTR and RTS true. The installed runtime is
.NET 10.0.12; the [.NET 10 Windows serial close path](https://github.com/dotnet/runtime/blob/v10.0.0/src/libraries/System.IO.Ports/src/System/IO/Ports/SerialStream.Windows.cs#L637)
clears DTR without an explicit RTS clear. Native USB control/reset cleanup is implicated by these
observations, but the exact cause is not proven.

After releasing both serial control lines and closing COM7, an `esptool --chip esp32s3 --port COM7 run`
application reset recovers v0 without reopening a monitor. Three read-only snapshots at 17:05:49,
17:06:34 and 17:07:19 PHT all show segment A online, `source: live`, `last_seen_s: 0`, advancing
`rx_ms`, and `seq: null` as expected for v0. The latter two show 37.5% occupancy and 7 estimated
people. **V0 remains on the node, verified live across 90 seconds with COM7 closed.**
V1's closed-port liveness needs a new test with corrected monitor cleanup before it can be retained.

The stream metric ends at the gateway's SSE `sent_ms`. Sensor acquisition, Wi-Fi transit and browser
rendering remain outside that metric; the serial figures cover the node's POST path.
Post/lag statistics sample one POST per status line. Wi-Fi outage recovery, the 5-minute reboot,
and a physical brownout were not fault-injected. The power supply remains unverified.

## 2026-10-22: real node v1 on the Pi, closed-port retest (attempt 2)

Result: **FAIL, v0 restored.** The compiled v1 app from `edb0169` is reused without rebuilding
or changing firmware source. App SHA-256:
`b6614280f4bb56b845effcb4b2835bf32002d92c7e4d1ccbb2b61ff0f886130b`.

After upload, DTR and RTS are released, the serial handle is closed, and
`esptool --chip esp32s3 --port COM7 --after hard-reset run` establishes a clean start at
17:15:42 PHT. COM7 is never opened between that clean start and the failed HTTP observation.
The real Pi is read over LAN at `http://PI_LAN_ADDRESS`, with `Host: 10.42.0.1`;
the gateway stays on 1.2.0 and is not changed.

The planned five-minute run has 30 primary samples ten seconds apart, with a one-second
read-only watch of the same endpoint to detect a three-second stall between primary samples.
The watch starts at 17:18:05 PHT and aborts before the first ten-second sample:

```text
Pi snapshot           seq  online  source  last_seen_s  frame_age_ms
2026-10-22 17:18:04     31  true    live             2          2021
2026-10-22 17:18:05     31  true    live             3          3021
```

Both snapshots have `rx_ms: 1792660682388`. The unchanged sequence and frame age of
**3021 ms** exceed the **3000 ms** limit. Primary samples completed: **0/30**.
No five-minute rate is established. The full stall duration is unknown because the test
stops at the violation. The raw responses and a corrected failure analysis preserve the
violating age; the watch summary's running maximum excludes the rejected sample.

The requested 60 s stream probe and 30 s fake bench are **not run** because their preceding
five-minute gate fails, so this attempt has no probe or bench output lines. The optional
serial capture is also skipped. The initial attempt's probe lines above remain historical
results and are not reused as this retest's output.

Rollback writes the same four v0 images with `--flash-mode keep --flash-freq keep --flash-size keep`.
All four independent read-back SHA-256 values match the backup README again. DTR/RTS release,
close and `esptool ... run` are repeated for v0, and COM7 remains closed.
Three Pi checks at 17:21:31, 17:22:16 and 17:23:01 PHT show segment A online, `source: live`,
`last_seen_s: 0`, advancing `rx_ms`, and `seq: null`, across **90 seconds**.
**V0 remains on the node.** The cause of the v1 closed-port stall is unresolved.

COM7 safe detach: native USB Serial/JTAG DTR/RTS affect reset/boot; deassert both before close, then use `esptool --chip esp32s3 --port COM7 --after hard-reset run` and leave COM7 closed.

## 2026-10-22: real node v1 on the Pi, non-blocking USB logging (attempt 3)

Result: **PASS, fixed v1 retained.** One real ESP32-S3 N16R8 and VL53L8CX node sends to the real
Pi 5 gateway over `CommuterDensity`; the Windows 11 laptop reads the Pi over LAN at
`http://PI_LAN_ADDRESS` with `Host: 10.42.0.1`. No Pi configuration is changed.
Board settings, 4 Hz sampling, 2 Hz sends, CPU 80 MHz and TX 8.5 dBm remain as above.
The fixed build uses **1,040,649 / 3,145,728 bytes** of flash and **52,248 / 327,680 bytes** of static RAM.

Diagnosis from the installed ESP32 core 3.3.12 `cores/esp32/HWCDC.cpp`:

- Line 105 sets the default TX timeout to 100 ms; lines 504-505 implement `setTxTimeoutMs`.
- Lines 268-290 check USB connection state, with `connected` latched by host packet pickup
  at lines 144-148. A connected USB bus does not establish that an application is reading.
- Lines 532 and 544 use that timeout to acquire the internal TX mutex. `write()` holds it
  until line 621. Lines 585-603 wait for ring-buffer space; the installed source already
  caps consecutive failed sends at 20 (line 582), about 2 seconds at the default timeout.
  This confirms a blocking logging path under host backpressure, rather than an unbounded
  wait in this installed copy. The old sketch calls `Serial.printf` from sensing and network tasks.

The fix sets `Serial.setTxTimeoutMs(0)` before `Serial.begin`, checks `HWCDC::isConnected()`,
formats a whole line, and drops it unless `availableForWrite()` has enough immediate capacity.
Only core 1 writes logs. The core-0 network task records numeric results; the status snapshot
critical section ends before any logging. Logging shares no application lock or queue.
A 10-second sequence watchdog restarts the HTTP client when Wi-Fi is up and no POST attempt
advances `seq`; the existing 5-minute successful-POST reboot rule remains.

The source confirms that USB logging could delay the old tasks and the fixed build passes this
closed-port test. The exact historical stall has no direct blocked-task trace, so attribution
of the earlier failure solely to USB logging remains an inference.

After verified upload, both DTR and RTS are released and
`esptool --chip esp32s3 --port COM7 --after hard-reset run` starts the candidate cleanly.
COM7 is never opened after that clean start, including after the probes. Startup and status
serial captures are deliberately skipped. Logging without a reader must always drop rather
than delay sensing or networking.

Soak: **1799.983 seconds**, from `2026-10-22T17:44:13.424776+08:00` to `2026-10-22T18:14:13.458454+08:00`.
All **180/180 ten-second samples** and **1801/1801 one-second watch reads** pass.
Sequence rises from **6 to 3613**, averaging
**2.003908 POST attempts/s**. Ten-second sequence deltas are
19 to 21 (median 20).
Segment A stays online with `source: live`, with no sequence reset or gateway boot change.
No stall exceeds 3 seconds. The largest gap between observed advancing Pi receive timestamps
is **1510 ms**, a conservative bound on internal inter-frame gaps;
maximum observed frame age is **35 ms**, maximum `last_seen_s` is
**0**, and the largest watch fetch interval is **1.058 seconds**.

`latency_probe.py --stream --base http://PI_LAN_ADDRESS --host-header 10.42.0.1 --duration 60`:

```text
stream; responses=122; errors=none; frames shown=122; frames skipped=0; resets=0; frame_to_screen_ms min=2 / median=2 / p95=9 / max=207 (n=121); age_at_fetch_ms min=2 / median=2 / p95=9 / max=455; stream connections=1
```

The saved stream CSV contains 122 frames, sequences
3639 through 3760, with
0 missing sequences (0.000% gaps).

`fake_bench.py --url http://PI_LAN_ADDRESS/api/display --host-header 10.42.0.1 --duration 30`:

```text
polls=30; valid=30; errors=none; response_ms median=20.7 / p95=34.1 / max=36.7; longest_gap_s=1.0 (fail-safe at 30); gate changes=[(0.0, 'open')]
```

The final Pi check at 2026-10-22 18:15:57 shows segment A online, `source: live`,
`seq: 3822`, `last_seen_s: 0`,
and occupancy 37.5%.
Evidence, raw one-second responses, all primary samples, build output and probe lines are saved
under `.assets/node-v1-2026-10-22/attempt-3/` in the ALERTO hub, outside the worktree.

COM7 safe detach: native USB Serial/JTAG DTR/RTS affect reset/boot; deassert both before close,
then use `esptool --chip esp32s3 --port COM7 --after hard-reset run` and leave COM7 closed.
Current post/lag figures and internal failed-POST counts are not measured because serial stays closed.
The earlier serial figures belong to the original build. Sequence continuity is the reboot check;
there is no new serial brownout trace. Sensor acquisition, Wi-Fi transit and browser rendering remain
outside the stream metric. Outage recovery, the 10-second HTTP restart, the 5-minute reboot and a
physical brownout are not fault-injected; the power supply remains unverified.

## 2026-10-22: gateway 1.2.1 on the Pi with node v1

Setup: real node v1 at 2 Hz, Raspberry Pi 5 with gateway 1.2.1, and a Windows 11 laptop probing over the LAN.
The laptop uses `http://PI_LAN_ADDRESS` with `Host: 10.42.0.1`. The fake bench baseline precedes the deploy;
the other bench runs span the `density-web` restart and check the gateway after restart.

| Run | Printed output (verbatim) |
| --- | --- |
| Stream probe | `stream; responses=121; errors=none; frames shown=121; frames skipped=0; resets=0; frame_to_screen_ms min=1 / median=2 / p95=9 / max=18 (n=120); age_at_fetch_ms min=1 / median=2 / p95=10 / max=141; stream connections=1` |
| Polling probe, 1 s interval | `interval=1 s; responses=60; errors=none; frames shown=60; frames skipped=59; resets=0; frame_to_screen_ms min=1 / median=227 / p95=510 / max=556 (n=118); age_at_fetch_ms min=1 / median=2 / p95=2 / max=10; mean /api/state request time=3.628 ms` |
| Fake bench, baseline 20 s (gateway 1.2.0) | `polls=20; valid=20; errors=none; response_ms median=17.4 / p95=30.2 / max=31.3; longest_gap_s=1.0 (fail-safe at 30); gate changes=[(0.0, 'open')]` |
| Fake bench, across restart 90 s | `polls=90; valid=90; errors=none; response_ms median=19.1 / p95=29.9 / max=31.0; longest_gap_s=1.0 (fail-safe at 30); gate changes=[(0.0, 'open')]` |
| Fake bench, fresh 60 s after restart | `polls=60; valid=60; errors=none; response_ms median=6.0 / p95=28.1 / max=30.2; longest_gap_s=1.0 (fail-safe at 30); gate changes=[(0.0, 'open')]` |

Reading: the stream reports a median of 2 ms and p95 of 9 ms from gateway receive to event sent; it does not
include the interval from frame ready to POST complete on the node (includes the Pi's response), measured at
a median of 79 ms in the node's own log in the node v1 section, or browser rendering. The 1 s poller reports
a median of 227 ms and p95 of 510 ms, skipping intermediate frames at the
node's 2 Hz send rate. All three bench runs have no errors and a longest gap of 1.0 s between valid responses,
below the 30 s fail-safe. That gap is not an isolated measurement of service downtime.

## 2026-10-23: real node v2, password-protected wireless updates

Result: **PASS, v2 retained on the node.** One real ESP32-S3 N16R8 and VL53L8CX node posts to
the real Pi 5 gateway 1.2.1 over the same hotspot and LAN path as the v1 run. The node stays on USB
as a recovery option, with its serial port closed throughout network acceptance.
Sampling remains 4 Hz, sending remains 2 Hz, CPU stays at 80 MHz and TX power at 8.5 dBm.
Payload shape, sequence assignment, Wi-Fi sleep off and non-blocking USB logging are unchanged.

Arduino CLI 1.5.1 and ESP32 core 3.3.12 compile v2 with `--warnings more` and zero warnings.
Flash use is **1,098,249 / 3,145,728 bytes**; static RAM is **57,096 / 327,680 bytes**.
Builds with a missing password or a password shorter than 12 characters both fail compilation.
The stock ESP32-S3 `esp32s3-libs/3.3.12/sdkconfig:424` contains
`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`. Bootloader and partition table binaries match v1
byte-for-byte; the existing two 3 MB app slots are retained.

The initial v1 baseline rises from sequence 612 to 652 at **2.009394 POST attempts/s**.
After verified USB upload and clean start, v2 rises from sequence 15 to 33 at **2.021294/s**
before the authentication test. Authentication runs in a separate FreeRTOS task so password
checks leave sampling and networking scheduled.

- Wrong password: rejected, exit 1. During authentication, sequence 35 to 49 averages
  **1.998133/s**; afterward, 51 to 95 averages
  **2.000022/s**. The full 39.904 s window averages
  **2.004802/s**, with no reset or stall over 3 s.
- Correct password: upload exits 0. Live POSTs resume **21.986 s**
  after push start, **6.443 s** after upload completion.
  Before the push, sequence 130 to 150 averages **2.012136/s**;
  after reboot, 77 to 117 averages **2.001713/s**.
  Metadata selects app1 at `0x310000`, sequence 2, state VALID. Its full 1,098,400-byte readback
  matches the pushed binary SHA-256, `aa28032b80fe20816b3370d514d5e43513222a7cd003ce07bf0a492506958f4a`.
- Rollback: a throwaway build changes only the Pi URL to a closed TCP port and is never committed.
  Its upload exits 0; the node sends no live POSTs until the five-minute no-success watchdog reboots it.
  Live POSTs return **314.344 s** after upload completion.
  Before the push, sequence 46 to 64 averages **2.017466/s**;
  after recovery, 116 to 156 averages **2.002075/s**.
  Metadata marks app0 sequence 3 ABORTED and retains app1 sequence 2 VALID. The recovered app1
  readback matches the same v2 hash above. No serial connection or manual reset occurs during
  the rollback wait. The gateway boot ID stays unchanged across both wireless-update tests.

Native-USB stub readback fails on the large app with transfer errors. ROM-mode readback using
`esptool --no-stub read-flash --flash-size 16MB` succeeds; each readback is followed by the
DTR/RTS release and clean start used for v1. The port stays closed afterward.

Final soak: **1799.943 seconds**, from `2026-10-23T18:17:19.614035+08:00` to `2026-10-23T18:47:19.652732+08:00`.
All **180/180 ten-second samples** and **1801/1801 one-second watch reads** pass.
Sequence rises from **74 to 3680**, averaging
**2.003397 POST attempts/s**. No sequence reset, gateway boot change or stall over 3 s occurs.
The largest gap between observed advancing Pi receive timestamps is **1502 ms**,
a conservative bound on internal inter-frame gaps. Maximum observed frame age is **28 ms**;
the largest watch fetch interval is **1.077 s**.

`latency_probe.py --stream --duration 60`:

```text
stream; responses=121; errors=none; frames shown=121; frames lost=0; resets=0; frame_to_screen_ms min=2 / median=2 / p95=9 / max=18 (n=120); age_at_fetch_ms min=2 / median=2 / p95=10 / max=110; stream connections=1
```

The stream CSV contains **121 frames**, sequences
**3683 through 3803**, with
**0 missing sequences (0.000% gaps)**.

`fake_bench.py --duration 30`:

```text
polls=30; valid=30; errors=none; response_ms median=18.7 / p95=41.8 / max=57.0; longest_gap_s=1.0 (fail-safe at 30); gate changes=[(0.1, 'closed')]
```

Stream timing ends at the gateway's SSE `sent_ms`; sensor acquisition, Wi-Fi transit and browser
rendering remain outside that metric. Internal POST-failure and brownout logs are not captured
because serial stays closed. A failed transfer after authentication, an OTA transfer longer than five
minutes, power loss during an update and sensor failure are not fault-injected. Mounted operation
on the USB adapter remains unverified. Gateway code, services, calibration,
hotspot and firewall settings are unchanged.

### v2 hardening: placeholder-password guard, bounded boot sensor retries, HTTP 200 validation

The example OTA password is rejected at build time, boot-time sensor retries reboot after
the existing five-minute budget, and image validation now shares the HTTP 200 success condition.
Setup order and Wi-Fi startup are unchanged. The README records password visibility in the
Pi process list. The fixed build uses **1,098,249 / 3,145,728 bytes**, with **zero warnings**;
the example password and an 11-character password both fail their static assertions.

- Fixed OTA: upload exits 0; first POST returns **25.497 s**
  from push start (**6.378 s** after upload). The pre/post
  sequence rates are **2.017979 / 2.011817 Hz**.
  After more than 60 s of normal posting, app0 at `0x10000`, sequence 3, is VALID;
  its full **1,098,400-byte** readback matches the pushed SHA-256 `4cc0aae9d217c2e12fc6cb08ae11324648e874d7286c2fcdbfe2ec848b3a7a8e`.
- Swapped-I2C throwaway image: upload exits 0 and no POST arrives until automatic recovery.
  The first POST returns **329.527 s** from push start,
  **310.244 s** after upload. Pre/post rates are
  **2.008669 / 1.999926 Hz**. Metadata shows app1 sequence 4
  ABORTED, app0 sequence 3 VALID, and the app0 readback retains the same fixed hash.
- Final closed-port watch: **599.956 s**, **60/60 primary samples**,
  **601/601 watch reads**, **2.005147 Hz**, largest observed receive gap
  **1511 ms**, no reset or stall over 3 s. The 60 s stream shows
  **121 frames**, **0 missing sequences**. Exact stream and bench output:

```text
stream; responses=121; errors=none; frames shown=121; frames lost=0; resets=0; frame_to_screen_ms min=1 / median=2 / p95=10 / max=79 (n=120); age_at_fetch_ms min=1 / median=2 / p95=10 / max=341; stream connections=1
polls=30; valid=30; errors=none; response_ms median=14.3 / p95=28.7 / max=30.7; longest_gap_s=1.0 (fail-safe at 30); gate changes=[(0.0, 'closed')]
```

ICMP brackets initial reboot/network loss at **19.128-19.501 s**
from the fixed push start and **19.292-19.768 s** from the fixture push start.
After rollback, Wi-Fi responds **328.839 s** from push start
(**309.557 s** after upload). Exact CPU reset and bootloader-switch instants are
unobserved with serial closed. Each readback is followed by the required clean start.

Known limitation: the OTA onStart pause wait remains unbounded. espota waits 10 s for the node
to connect back; if an in-flight POST delays the start past that window, the push fails and
must be retried. The connection failure reaches onError and the node resumes normal operation.
Both measured correct-password pushes succeed. The timeout path is source-inspected, not
fault-injected. Gateway code, services, calibration,
hotspot, firewall and environment remain unchanged. Mounted operation and power-loss recovery remain untested.

## 2026-10-23: offline component peak filter, gateway 1.2.3

The 400 mm zone rule counts short wall returns as occupancy. Gateway 1.2.3 groups qualifying
zones that share an edge and keeps a group only when its highest return reaches 700 mm.
Lower arm and body-edge zones remain occupied when connected to that taller return. A single
tall zone still counts toward occupancy. `MIN_PEAK_MM` sets the peak requirement; `0` restores
the previous rule. Background adaptation still uses the raw 400 mm mask, and calibration files
and the `/api/display` format are unchanged.

Offline replay uses 299 EMPTY frames and 159 WALK frames with their recorded background.
The fixed WALK labels identify 57 frames with at least one occupied return reaching 900 mm;
the 57/57 retention excludes frames whose highest person return is 400 mm to below 900 mm.
These are a sensing proxy, not an independent position log. Results:

- EMPTY false frames: **69/299 to 0/299**. The 75 false zone hits measure **445-578 mm**.
  There are 64 per-zone runs, 56 lasting one frame. Most hits stand alone, but three components
  contain two adjacent zones each.
- Recorded person frames detected: **57/57 before and after**. Mean occupied zones per labelled
  frame fall from **2.7895 to 2.5263**, with a minimum of **1 before and after**. Mean displayed
  occupancy falls from **4.3544% to 3.9474%**, with a minimum of **1.6% before and after**.
- Relearning from each of **270 sliding 30-frame EMPTY windows**, then testing the other
  269 EMPTY frames, gives **0 false frames in every window**. The previous rule's worst/median
  are **68/25**. EMPTY and WALK replay separately, with normal background drift.

Three window comparisons remove the same WALK frame, sequence **4992**. In the recorded
background, its person return is at zone 23, about 1315 mm above the background, alongside a
541 mm wall return at zone 40. Those three calibration windows have no valid samples for
zone 23, so that zone becomes unusable. The previous rule then reports occupancy only from
the wall return, about 513-521 mm under those backgrounds. Removing it is the intended result;
the filter loses no usable person return in these comparisons and does not repair the missing
calibration samples. A 600 mm peak has the same frame retention, but 700 mm leaves about
122 mm above the highest recorded false return. A 900 mm peak loses 52 paired window detections.

The implemented `Sensor` matches **116,476 occupancy arrays and background arrays** across the
recorded, continuous-stream and window comparisons, with zero mismatches against the offline
replay. With the filter disabled, all **116,476 complete sensor states** match the previous code.
All **544 stream minute writes** match the expected filtered occupancy and people aggregates.
The **77-test suite passes**, including eight new synthetic component tests and the existing
bench contract tests; `git diff --check` passes.

Local gateway 1.2.3 on Windows, Python 3.13.14, synthetic node at 2 Hz, after the real 30-frame
calibration. The following 20 s bench and stream checks run together:

```text
polls=20; valid=20; errors=none; response_ms median=12.0 / p95=21.8 / max=25.9; longest_gap_s=1.0 (fail-safe at 30); gate changes=[(0.0, 'open')]
stream; responses=41; errors=none; frames shown=41; frames lost=0; resets=0; frame_to_screen_ms min=5 / median=6 / p95=17 / max=110 (n=40); age_at_fetch_ms min=5 / median=6 / p95=46 / max=359; stream connections=1
frames sent=80; failures=0; mean POST time=12.709 ms
```

This is offline and local software evidence. No chair stream, seated-person recording, other
venue or prospective physical validation is available. An isolated chair-seat-height return
below 700 mm is intentionally excluded. Gateway 1.2.3 is not deployed in this measurement;
the Raspberry Pi, node, services and stored calibration are untouched.

## 2026-10-24: gateway 1.2.3 deployed with node firmware v2

Gateway **1.2.3** from merged main `c1d71c6` runs on the real Pi 5. The deploy installs
`rpi/app.py`, `rpi/frames.py` and `rpi/static/index.html`; source, staging and installed
SHA-256 hashes agree. `density-web` restarts at **01:16:41 PHT (UTC+08:00)**.
`density-forecast` remains active with the same process and activation time.
The laptop reaches `http://PI_LAN_ADDRESS` with `Host: 10.42.0.1`.

Pre-checks find both services active and installed hashes matching gateway 1.2.1.
The env file has no `MIN_PEAK_MM` key, so the default **700 mm** applies.
The final pre-deploy sequence rate is **2.011337 Hz**;
after restart, platform A is online at **1.976675 Hz**
(seq 5869 to 5879).

Baseline, 20 s; bench across restart, 90 s; stream after restart, 60 s:

```text
polls=20; valid=20; errors=none; response_ms median=20.3 / p95=29.5 / max=31.8; longest_gap_s=1.0 (fail-safe at 30); gate changes=[(0.0, 'closed')]
polls=90; valid=90; errors=none; response_ms median=22.0 / p95=31.9 / max=36.5; longest_gap_s=1.0 (fail-safe at 30); gate changes=[(0.0, 'closed')]
stream; responses=121; errors=none; frames shown=121; frames lost=0; resets=0; frame_to_screen_ms min=2 / median=2 / p95=11 / max=24 (n=120); age_at_fetch_ms min=2 / median=2 / p95=11 / max=131; stream connections=1
```

Every poll in the accepted restart window is valid, with a longest valid-response gap of
**1.0 s**, below the bench's 30 s fail-safe.
The restart follows a completed valid poll; per-poll timestamps preserve the original
one-second cadence and 250 ms timeout. The gap is not a measurement of service downtime.
Stream timing ends at SSE `sent_ms`; sensor acquisition, Wi-Fi transit and browser rendering
remain outside that metric.

The `/api/display` key set and types match the live 1.2.1 baseline: `gate`, `lang`, `levels`,
`title` and `zone` are strings; `lines` is an array of strings. The body is **165 bytes**,
matches `Content-Length` and stays below 512 bytes. The gate rule and hotspot are unchanged.

Peak-filter acceptance uses **30 consecutive `/api/live` frames**, seq
**6001 through 6030**, from `platform-A`. All **60 four-connected occupied components**
have a valid status (5 or 9) and a peak
`background - d` of at least 700 mm; the lowest is **2150 mm**.
There are **zero component violations** and **zero occupancy mismatches**. Each frame has
**59/64 occupied zones**, giving **92.2%** at the API's one-decimal precision
(`round(59 * 100 / 64, 1)`; exact fraction 92.1875%). The node is on a desk against its saved
hallway background, so the high reading is expected. Calibration stays unchanged; this check
does not establish empty-scene, seated-person or defense-venue accuracy.

Live Dashboard captures at **1440 px and 390 px** show no `Line 1 |` header or
`people in view` text. The 390 px capture has no horizontal page overflow and both footers
show **v1.2.3**. The journal since the restart has **zero tracebacks**.

Two earlier attempts are restored to verified 1.2.1 before the accepted attempt. The first
state read uses minute fallback before the first live frame and has no sequence yet;
the live-sequence check now waits for that frame. Both earlier 90 s bench windows record
**89/90 valid**, one socket timeout and a **2.0 s** longest gap; the first window spans both
its deploy and rollback restarts. Immediate rollback checks record 29/30; settled rollback checks pass
30/30, with 121 stream frames and zero lost. A separate steady-state bench passes 60/60.
The accepted restart window does not guarantee zero missed polls at every restart phase.
