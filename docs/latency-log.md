# Latency log

One entry per measurement. Every change aimed at speed adds a new entry with the same probe.

## What the probe measures

`rpi/tools/latency_probe.py` polls `/api/state` like the dashboard does and reports, using only the gateway
clock:

- `frame_to_screen_ms`: for every frame, gateway receive time to the first dashboard fetch that shows it or a
  newer frame. This is the delay a person watching the dashboard experiences. Receive times of frames
  between two fetches are interpolated from their sequence numbers (assumes a steady send rate).
- `age_at_fetch_ms`: age of the newest frame at each fetch.

Not covered: sensor acquisition, node processing, Wi-Fi transit, browser rendering.

## 2026-10-22: baseline, gateway 1.1.1, local laptop

Setup: Windows 11 laptop, fake node at 1 Hz (`tools/fake_node.py`), gateway `1.1.1`, Python `3.11.15`,
Waitress `3.0.2` with 8 threads at `127.0.0.1:8090`. NOT the Raspberry Pi and NOT a real node. Both probes
ran at the same time after the 30-frame calibration.

| Run | Printed output (verbatim) |
| --- | --- |
| `latency_probe.py --interval 10 --duration 120` (current dashboard default) | `interval=10 s; responses=12; frames shown=12; frames skipped=99; frame_to_screen_ms min=36 / median=4055 / p95=9048 / max=9056 (n=111); age_at_fetch_ms min=36 / median=48 / p95=60 / max=60; mean /api/state request time=4.711 ms` |
| `latency_probe.py --interval 1 --duration 60` | `interval=1 s; responses=60; frames shown=60; frames skipped=0; frame_to_screen_ms min=33 / median=48 / p95=54 / max=56 (n=60); age_at_fetch_ms min=33 / median=48 / p95=54 / max=56; mean /api/state request time=3.905 ms` |
| `fake_node.py --hz 1 --duration 200` | `frames sent=200; failures=0; mean POST time=3.907 ms` |

Reading: at the 10 s default, half of all frames take more than 4 s to reach the dashboard and the worst take
about 9 s. Server time is about 4 ms per request, so the polling interval is the cause. The 1 s run is
optimistic: probe and node both run at exactly 1 Hz, so every fetch landed just after a frame. A real
browser polling at 1 s sees 0 to 1 s.
