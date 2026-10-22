# Latency log

One entry per measurement. Every change aimed at speed adds a new entry with the same probe.

## What the probe measures

`rpi/tools/latency_probe.py` polls `/api/state` like the dashboard does and reports, using only the gateway
clock:

- `frame_to_screen_ms`: for every frame, gateway receive time to the first dashboard fetch that shows it or a
  newer frame. This is the delay a person watching the dashboard experiences. Receive times of frames
  between two fetches are interpolated from their sequence numbers (assumes a steady send rate). Needs nodes
  that send `seq` incremented by exactly 1 per frame; without it the probe reports only `age_at_fetch_ms`
  and warns. The first fetch is warm-up and not counted; a pause in fetching or a node reboot (seq going
  down) resets the chain instead of inventing frames (`resets`). So does a seq jump larger than the elapsed
  receive time allows at 50 Hz, which catches nodes that send a timestamp instead of a counter. A node
  slower than one frame per `2 x interval + 1 s` would reset on every fetch.
- `age_at_fetch_ms`: age of the newest frame at each fetch. On its own it understates dashboard latency.

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
