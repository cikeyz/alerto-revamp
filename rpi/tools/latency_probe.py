"""Measure receive-to-visible frame age using the gateway clock."""
import argparse
import csv
import http.client
import json
import math
import statistics
import sys
import time
from urllib.parse import urlsplit


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8090")
    ap.add_argument("--host-header")
    ap.add_argument("--segment", default="A")
    ap.add_argument("--interval", type=float)
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
    path = url.path.rstrip("/") + "/api/state"
    headers = {"Host": args.host_header} if args.host_header else {}
    interval = args.interval
    responses = skipped = 0
    rows, times = [], []
    previous = previous_seq = previous_rx = None
    screen = []
    next_poll = time.monotonic()
    end = next_poll + args.duration
    try:
        while time.monotonic() < end:
            time.sleep(max(0, min(next_poll, end) - time.monotonic()))
            if time.monotonic() >= end:
                break
            start = time.perf_counter()
            con.request("GET", path, headers=headers)
            resp = con.getresponse()
            body = resp.read()
            times.append((time.perf_counter() - start) * 1000)
            if resp.status != 200:
                print(f"/api/state returned HTTP {resp.status}; check --base and --host-header", file=sys.stderr)
                return 1
            state = json.loads(body)
            responses += 1
            seg = next((s for s in state["segments"] if s["id"] == args.segment), None)
            if seg is None:
                print(f"Segment {args.segment} is not in /api/state", file=sys.stderr)
                return 2
            if "rx_ms" not in seg or "now_ms" not in state:
                print("Gateway has no rx_ms/now_ms latency fields; upgrade the gateway to 1.1.1 or later.",
                      file=sys.stderr)
                return 2
            if interval is None:
                interval = float(state["settings"]["refresh_s"])
                if not math.isfinite(interval) or interval <= 0:
                    raise ValueError("settings.refresh_s must be positive and finite")
            rx_ms, seq = seg["rx_ms"], seg.get("seq")
            if rx_ms is not None:
                identity = ("seq", seq) if seq is not None else ("rx_ms", rx_ms)
                if identity != previous:
                    jump = max(0, seq - previous_seq - 1) if seq is not None and previous_seq is not None else 0
                    skipped += jump
                    rows.append((state["now_ms"], rx_ms, seq, state["now_ms"] - rx_ms, jump))
                    # every frame since the last fetch first reaches the screen at this fetch
                    if jump and previous_rx is not None:
                        # shortcut: skipped frames' receive times are interpolated by seq, assumes a steady send rate
                        step = (rx_ms - previous_rx) / (seq - previous_seq)
                        screen.extend(state["now_ms"] - (previous_rx + step * k) for k in range(1, jump + 1))
                    screen.append(state["now_ms"] - rx_ms)
                    previous, previous_seq, previous_rx = identity, seq, rx_ms
            next_poll = max(next_poll + interval, time.monotonic())
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as e:
        print(f"Could not probe /api/state: {e}", file=sys.stderr)
        return 1
    finally:
        con.close()
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(("now_ms", "rx_ms", "seq", "visible_ms", "skipped"))
            writer.writerows(rows)
    def summary(values):
        values = sorted(values)
        if not values:
            return "min=n/a / median=n/a / p95=n/a / max=n/a (no live frames with rx_ms)"
        return (f"min={values[0]:.0f} / median={statistics.median(values):.0f} / "
                f"p95={values[math.ceil(len(values) * 0.95) - 1]:.0f} / max={values[-1]:.0f}")
    mean = statistics.mean(times) if times else 0.0
    print(f"interval={interval:g} s; responses={responses}; frames shown={len(rows)}; frames skipped={skipped}; "
          f"frame_to_screen_ms {summary(screen)} (n={len(screen)}); "
          f"age_at_fetch_ms {summary(row[3] for row in rows)}; mean /api/state request time={mean:.3f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
