# Turnstile bench contract

The turnstile bench (ESP32-S3 gate with TFT, servo, RGB light, buzzer and tap sensor) reads the gateway's
`/api/display`. Every gateway change must keep this contract so the bench plugs in with no changes on either
side. `rpi/tests/test_bench_contract.py` checks the response shape and gate behaviour on every test run;
`rpi/tools/fake_bench.py` rehearses the full exchange, including timing, against a running gateway.

## What the bench sends

- `GET http://10.42.0.1/api/display`, port 80, once per second.
- HTTP/1.0, a new connection each time, no key, PIN or other special header.
- Connect timeout 250 ms and read idle timeout 250 ms.
- It joins the hotspot `CommuterDensity` (currently open; the bench keeps the hotspot name and password in
  its local `secrets.h`).

## What the gateway must return

- Status `200` with a `Content-Length` header and a complete body of at most **512 bytes**.
- JSON with:
  - `gate`: exactly `"open"` or `"closed"`.
  - `lines`: an array of at most **4** strings, each at most **21** characters (non-ASCII is folded to `?` on
    the bench).
  - Other keys (`title`, `levels`, `lang`, `zone`) are allowed and ignored by the bench.
- `/api/display` must answer for any `Host` header (the captive-portal redirect must never apply to it).
- Answer quickly under load (every open dashboard stream, every node posting, a forecaster refit running).
  The bench's 250 ms timeouts apply to connecting and to the wait for the response headers; once it is
  reading the body it keeps waiting on an open socket, so a gateway that stalls mid-body can hold the bench
  until its 30 s fail-safe. `fake_bench.py` fails a poll on any 250 ms stall and reports the total response
  time, so stalls show up in a rehearsal.

Anything else (non-200, missing or oversized length, short body, bad JSON, unknown gate value, more than 4
lines) is not a valid heartbeat. After 30 s without a valid response the bench enters its fail-safe (taps
behave as Normal Flow, amber light, screen off).

## Gate behaviour the bench test procedure relies on

From the bench build guide (bench test checklist, steps 2, 3 and 5):

- `gate_enabled` off (Settings, "Close the gate automatically when every segment is High") gives `"open"`.
- `gate_enabled` on closes the gate when every segment with data is at or above `high_at`; `moderate_at=1`,
  `high_at=2` with readings of at least 2 percent is the documented way to force `"closed"`.
- It reopens when the setting is off, when no segment has data, or when any segment falls strictly below
  `high_at - 5`.
- Settings changes need the admin PIN.

## Rules for gateway changes

- Do not change the gate rule, its thresholds or its timing (smoothing, minimum dwell, the Chapter 3
  count-based rule) without updating the bench build guide and re-running its test. Not before 2026-10-27.
- Do not change the hotspot name, password, address or port without updating the bench's `secrets.h` or
  `config.h`. Not before 2026-10-27.
- A deploy restarts `density-web` for a few seconds; the bench rides through anything under 30 s.
- Dashboard streams hold one of Waitress's 8 worker threads each and are capped at 4, leaving 4 for nodes, the
  bench and ordinary requests. Do not raise the stream cap without raising `--threads` in `setup.sh` and
  re-running `fake_bench.py` under load.

## Rehearse

From a repository checkout on a laptop on the same network (`setup.sh` does not install `tools/` on the Pi;
copy `rpi/tools/fake_bench.py` there to run it on the Pi itself):

```
python tools/fake_bench.py --url http://10.42.0.1/api/display --duration 60
```

It prints valid polls, errors by kind, response time and the longest gap between valid responses. Exit code
0 means every gap stayed under the 30 s fail-safe.

## Measurements

| Date | Gateway | Where | Result (verbatim) |
| --- | --- | --- | --- |
| 2026-10-22 | 1.1.0 (running Pi, before the revamp) | laptop over LAN, `--url http://192.168.254.155/api/display --duration 30` | `polls=30; valid=30; errors=none; response_ms median=6.7 / p95=25.5 / max=26.5; longest_gap_s=1.0 (fail-safe at 30); gate changes=[(0.0, 'open'), (7.0, 'closed'), (8.0, 'open')]` |
| 2026-10-22 | 1.2.0 (live push, local laptop) | 4 nodes at 2 Hz, 4 streams open, `--duration 85` | `polls=85; valid=85; errors=none; response_ms median=1.9 / p95=23.7 / max=32.8; longest_gap_s=1.0 (fail-safe at 30); gate changes=[(0.0, 'open')]` |

The 1.1.0 run shows the gate closing and reopening within about a second on live data from one node: the
unsmoothed gate input (improvement backlog C3). The bench would show a one-second Hold.
