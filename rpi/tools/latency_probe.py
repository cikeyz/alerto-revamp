"""Measure receive-to-screen frame latency using the gateway clock (see docs/latency-log.md).

frame_to_screen_ms needs nodes that send `seq` incremented by exactly 1 per frame; without it only
age_at_fetch_ms is meaningful and the probe says so.
"""
import argparse
import csv
import http.client
import json
import math
import statistics
import sys
import time
from urllib.parse import urlsplit


MIN_FRAME_MS = 20     # no node sends faster than 50 Hz; a bigger seq jump than this allows is not real


class Tracker:
    """Turns successive /api/state snapshots of one segment into per-frame latency samples."""

    def __init__(self, interval_s):
        self.interval_ms = interval_s * 1000
        self.rows, self.screen = [], []
        self.skipped = self.resets = 0
        self.no_seq = False
        self.prev_id = self.prev_seq = self.prev_rx = self.prev_now = None

    def update(self, now_ms, rx_ms, seq):
        if rx_ms is None:
            return
        ident = seq if seq is not None else rx_ms
        if ident == self.prev_id:
            return
        age = max(0, now_ms - rx_ms)
        first = self.prev_now is None
        # a fetch gap (probe paused, gateway restart), a seq going down (node reboot), or a seq jump bigger
        # than the elapsed receive time allows (not a 1-per-frame counter) breaks the chain
        broken = (not first and now_ms - self.prev_now > 2 * self.interval_ms + 1000) or \
                 (seq is not None and self.prev_seq is not None and
                  (seq <= self.prev_seq or (seq - self.prev_seq) * MIN_FRAME_MS > rx_ms - self.prev_rx))
        if broken:
            self.resets += 1
        jump = 0
        if seq is None:
            self.no_seq = True
        elif not first and not broken and self.prev_seq is not None:
            jump = seq - self.prev_seq - 1
            if jump:
                # shortcut: skipped frames' receive times are interpolated by seq, assumes a steady send rate
                step = (rx_ms - self.prev_rx) / (seq - self.prev_seq)
                self.screen.extend(max(0, now_ms - (self.prev_rx + step * k)) for k in range(1, jump + 1))
                self.skipped += jump
            self.screen.append(age)       # the first fetch is warm-up: its frame predates the probe
        self.rows.append((now_ms, rx_ms, seq, age, jump))
        self.prev_id, self.prev_seq, self.prev_rx, self.prev_now = ident, seq, rx_ms, now_ms


def stream_states(con, path, headers, end):
    """Yield each `event: state` payload from /api/stream until `end` (monotonic seconds)."""
    con.request("GET", path, headers=headers)
    resp = con.getresponse()
    if resp.status != 200:
        raise http.client.HTTPException(f"HTTP {resp.status}")
    event, data = None, []
    while time.monotonic() < end:
        if con.sock:
            con.sock.settimeout(min(20, max(0.001, end - time.monotonic())))
        try:
            raw = resp.readline()
        except TimeoutError:
            con.close()                   # never reuse a half-read stream connection
            return
        if not raw:
            raise ConnectionError("stream closed")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if event == "state" and data:
                yield "\n".join(data)
            event, data = None, []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))


def summary(values):
    values = sorted(values)
    if not values:
        return "min=n/a / median=n/a / p95=n/a / max=n/a"
    return (f"min={values[0]:.0f} / median={statistics.median(values):.0f} / "
            f"p95={values[math.ceil(len(values) * 0.95) - 1]:.0f} / max={values[-1]:.0f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8090")
    ap.add_argument("--host-header")
    ap.add_argument("--segment", default="A")
    ap.add_argument("--interval", type=float)
    ap.add_argument("--stream", action="store_true", help="listen to /api/stream instead of polling")
    ap.add_argument("--duration", type=float, default=120)
    ap.add_argument("--csv")
    args = ap.parse_args()
    if args.interval is not None and (not math.isfinite(args.interval) or args.interval <= 0):
        ap.error("--interval must be positive and finite")
    if not math.isfinite(args.duration) or args.duration <= 0:
        ap.error("--duration must be positive and finite")
    url = urlsplit(args.base)
    if url.scheme not in ("http", "https") or not url.hostname:
        ap.error("--base must be an HTTP or HTTPS URL")
    connection = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    con = connection(url.hostname, url.port, timeout=10)
    path = url.path.rstrip("/") + ("/api/stream" if args.stream else "/api/state")
    headers = {"Host": args.host_header} if args.host_header else {}
    interval, tracker = args.interval, None
    responses, errors, times = 0, {}, []
    next_poll = time.monotonic()
    end = next_poll + args.duration
    while args.stream and time.monotonic() < end:
        # events come at least once per second (the gateway rebuilds every second), so gaps use 1 s
        tracker = tracker or Tracker(1)
        start = time.perf_counter()
        try:
            for body in stream_states(con, path, headers, end):
                try:
                    state = json.loads(body)
                    seg = next(s for s in state["segments"] if s["id"] == args.segment)
                    now_ms, rx_ms, seq = state["now_ms"], seg["rx_ms"], seg.get("seq")
                    if (type(now_ms) is not int or (rx_ms is not None and type(rx_ms) is not int) or
                            (seq is not None and type(seq) is not int)):
                        raise ValueError("wrong-typed latency fields")
                except (ValueError, KeyError, TypeError, StopIteration) as e:
                    # distinguish missing segments and old gateways from malformed responses
                    error = "bad payload"
                    if isinstance(e, StopIteration):
                        error = f"no segment {args.segment}"
                    elif isinstance(e, KeyError) and e.args[0] in ("rx_ms", "now_ms"):
                        error = "no latency fields"
                    errors[error] = errors.get(error, 0) + 1
                    continue
                responses += 1
                tracker.update(now_ms, rx_ms, seq)
        except (OSError, http.client.HTTPException) as e:
            errors[type(e).__name__] = errors.get(type(e).__name__, 0) + 1
            con.close()
            time.sleep(min(1, max(0, end - time.monotonic())))
        times.append((time.perf_counter() - start) * 1000)
    if args.stream:
        interval = 0
    while not args.stream and time.monotonic() < end:
        time.sleep(max(0, min(next_poll, end) - time.monotonic()))
        if time.monotonic() >= end:
            break
        next_poll = max(next_poll + (interval or 1), time.monotonic())
        start = time.perf_counter()
        try:
            con.request("GET", path, headers=headers)
            resp = con.getresponse()
            body = resp.read()
        except (OSError, http.client.HTTPException) as e:
            errors[type(e).__name__] = errors.get(type(e).__name__, 0) + 1
            con.close()                   # reconnect on the next poll instead of losing the run
            continue
        times.append((time.perf_counter() - start) * 1000)
        if resp.status != 200:
            errors[f"HTTP {resp.status}"] = errors.get(f"HTTP {resp.status}", 0) + 1
            continue
        try:
            state = json.loads(body)
            seg = next(s for s in state["segments"] if s["id"] == args.segment)
            now_ms, rx_ms, seq = state["now_ms"], seg["rx_ms"], seg.get("seq")
            if (type(now_ms) is not int or (rx_ms is not None and type(rx_ms) is not int) or
                    (seq is not None and type(seq) is not int)):
                raise ValueError("wrong-typed latency fields")
            if tracker is None and interval is None:
                interval = float(state["settings"]["refresh_s"])
        except (ValueError, KeyError, TypeError, StopIteration) as e:
            # distinguish missing segments and old gateways from malformed responses
            error = "bad payload"
            if isinstance(e, StopIteration):
                error = f"no segment {args.segment}"
            elif isinstance(e, KeyError) and e.args[0] in ("rx_ms", "now_ms"):
                error = "no latency fields"
            errors[error] = errors.get(error, 0) + 1
            continue
        responses += 1
        if tracker is None:
            if not (math.isfinite(interval) and interval > 0):
                print(f"Unusable poll interval {interval!r} from settings; pass --interval.", file=sys.stderr)
                return 2
            next_poll = time.monotonic() + interval
            tracker = Tracker(interval)
        tracker.update(now_ms, rx_ms, seq)
    con.close()
    if tracker is None:
        print(f"No valid /api/state responses; errors={errors}", file=sys.stderr)
        return 1
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(("now_ms", "rx_ms", "seq", "age_at_fetch_ms", "skipped"))
            writer.writerows(tracker.rows)
    mean = statistics.mean(times) if times else 0.0
    mode = "stream" if args.stream else f"interval={interval:g} s"
    print(f"{mode}; responses={responses}; errors={errors or 'none'}; "
          f"frames shown={len(tracker.rows)}; frames skipped={tracker.skipped}; resets={tracker.resets}; "
          f"frame_to_screen_ms {summary(tracker.screen)} (n={len(tracker.screen)}); "
          f"age_at_fetch_ms {summary(r[3] for r in tracker.rows)}; "
          + (f"stream connections={len(times)}" if args.stream else f"mean /api/state request time={mean:.3f} ms"))
    if tracker.no_seq:
        print("WARNING: frames carry no seq, so frame_to_screen_ms is not measured; age_at_fetch_ms "
              "alone understates dashboard latency.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
