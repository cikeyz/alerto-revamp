# Node firmware v0 (as found on platform-A, 2026-10-22)

Read-back of the firmware running on sensor node `platform-A` before any revamp work. This is the baseline to
compare against and the image to restore if a new build regresses.

**Its source is not in this repository.** The node runs a newer revision than
`esp32_tof_reader/esp32_tof_reader.ino` (commit `46cf20f`). Behaviour below was read from the binary, not
from source.

## Files

| File | Flash offset | Size | SHA-256 |
|---|---|---|---|
| `bootloader.bin` | `0x0` | 32 KB | `f417f6cb2d01d80b697bfabbab8ad8a39269541653785e3da323c39de0ecd79a` |
| `partition-table.bin` | `0x8000` | 4 KB | `f3134b747fef242287f33aa0be8a5008958132e0c7611f5bc5bb917c02c9e397` |
| `otadata.bin` | `0xe000` | 8 KB | `f94c5d786a7a8fab06ac5d10e33bf37711a6697636dc037559ea19cc410a17f0` |
| `app0.bin` | `0x10000` | 1280 KB (whole partition) | `f2c56614cae7730dea11e5397c6a6b7c3a8d3902cf8ea9c305346ea13e6b4384` |

Not included: `nvs` (`0x9000`, 20 KB). It holds only the board's own radio calibration and the saved hotspot
name, and the firmware recreates it. `spiffs` and `coredump` were not read.

## Device and build

- ESP32-S3 (QFN56) rev v0.2, 16 MB flash, 8 MB embedded PSRAM. Board USB-serial chip: CH343.
- Built with Arduino-ESP32 core 2.0.14 (ESP-IDF v4.4.6).
- Image header: 4 MB flash, 80 MHz, DIO. The board has 16 MB; the build uses 4 MB. PSRAM is not in use: the boot log
  reports 326,892 bytes of free heap, internal RAM only.
- Partitions: `nvs` 0x9000, `otadata` 0xe000, `app0` 0x10000 (1280 KB, active), `app1` 0x150000 (empty),
  `spiffs` 0x290000, `coredump` 0x3f0000.
- Built with USB CDC On Boot enabled: application logs appear on the ESP32-S3 native USB port, not on the
  CH343 port. The CH343 port shows only the ROM boot banner.

## Behaviour (read from the binary)

Same as the repo sketch:
- Node ID `platform-A`, hotspot `CommuterDensity` (open), `POST http://10.42.0.1/frame` with `X-Device-Key`,
  JSON `{"device_id","d":[64],"s":[64]}`.
- One POST per 1000 ms, 2000 ms HTTP timeout, Wi-Fi TX power 8.5 dBm.
- I2C on SDA GPIO8 / SCL GPIO9 at 400 kHz; LPn and reset not wired.
- Sensor watchdog: restart the sensor after 3 s without frames, at most every 10 s.

Different from the repo sketch:
- Ranging 8x8 in **autonomous** mode at **2 Hz** with **10 ms** integration (sketch: continuous, 5 Hz).
- Startup order: I2C scan and sensor firmware load, then Wi-Fi, then start ranging (sketch: Wi-Fi first).
- Logs the reset reason, including a brownout message, and a timestamp on every line.
- Serial commands: `g` prints the 8x8 grid in cm, `v` toggles a once-per-second status line (valid zones,
  near/far/centre distance, Wi-Fi dBm, last POST result, sent/failed counts).
- Valid zone rule for its own logs: target status 5 or 9 and distance above 0.
- Readable POST errors (`403 wrong NODE_KEY`, `-1 cannot reach the Pi`, `-11 timeout`).

## Restore

Untested. Use the esptool that ships with the Arduino-ESP32 core (v5 syntax shown). Board on its CH343 port:

```
esptool --chip esp32s3 --port COMx write-flash --flash-mode keep --flash-freq keep --flash-size keep 0x0 bootloader.bin 0x8000 partition-table.bin 0xe000 otadata.bin 0x10000 app0.bin
```

Then read each region back and compare with the hashes above (PowerShell):

```
esptool --chip esp32s3 --port COMx read-flash 0x0 0x8000 check-bootloader.bin
esptool --chip esp32s3 --port COMx read-flash 0x8000 0x1000 check-partition-table.bin
esptool --chip esp32s3 --port COMx read-flash 0xe000 0x2000 check-otadata.bin
esptool --chip esp32s3 --port COMx read-flash 0x10000 0x140000 check-app0.bin
Get-FileHash check-*.bin -Algorithm SHA256
```
