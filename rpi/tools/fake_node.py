"""Send synthetic raw frames at the node's cadence."""
import argparse
import http.client
import json
import math
import random
import statistics
import time
from urllib.parse import urlsplit


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:8090/frame")
    ap.add_argument("--device", default="platform-A")
    ap.add_argument("--key", default="")
    ap.add_argument("--hz", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=60)
    ap.add_argument("--host-header")
    args = ap.parse_args()
    if not math.isfinite(args.hz) or args.hz <= 0:
        ap.error("--hz must be positive and finite")
    if not math.isfinite(args.duration) or args.duration <= 0:
        ap.error("--duration must be positive and finite")
    url = urlsplit(args.url)
    if url.scheme not in ("http", "https") or not url.hostname:
        ap.error("--url must be an HTTP or HTTPS URL")
    connection = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    con = connection(url.hostname, url.port, timeout=10)
    path = url.path or "/"
    if url.query:
        path += "?" + url.query
    headers = {"Content-Type": "application/json"}
    if args.key:
        headers["X-Device-Key"] = args.key
    if args.host_header:
        headers["Host"] = args.host_header
    sent = failures = 0
    times = []
    seq = 1
    next_send = time.monotonic()
    end = next_send + args.duration
    try:
        while time.monotonic() < end:
            time.sleep(max(0, min(next_send, end) - time.monotonic()))
            if time.monotonic() >= end:
                break
            body = json.dumps({"device_id": args.device, "d": [2500 + random.randint(-15, 15) for _ in range(64)],
                               "s": [5] * 64, "seq": seq})
            start = time.perf_counter()
            try:
                con.request("POST", path, body=body, headers=headers)
                resp = con.getresponse()
                resp.read()
                if resp.status == 200:
                    sent += 1
                else:
                    failures += 1
                    con.close()
            except (OSError, http.client.HTTPException):
                failures += 1
                con.close()   # the next request reconnects
            times.append((time.perf_counter() - start) * 1000)
            seq += 1
            next_send = max(next_send + 1 / args.hz, time.monotonic())
    finally:
        con.close()
    mean = statistics.mean(times) if times else 0.0
    print(f"frames sent={sent}; failures={failures}; mean POST time={mean:.3f} ms")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
