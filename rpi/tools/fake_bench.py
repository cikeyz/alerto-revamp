"""Poll /api/display exactly like the turnstile bench firmware and report what it would see."""
import argparse
import json
import math
import socket
import statistics
import time
from urllib.parse import urlsplit

TIMEOUT_S = 0.25      # bench GATEWAY_HTTP_TIMEOUT_MS: connect and read idle timeout
MAX_BYTES = 512       # bench GATEWAY_RESPONSE_MAX_BYTES
MAX_LINES = 4         # bench GUIDANCE_LINES
FAILSAFE_S = 30       # bench FAILSAFE_MS


def poll(host, port, path, host_header):
    """One bench poll: new connection, HTTP/1.0, Content-Length required. Returns (doc, error)."""
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT_S) as s:
            s.settimeout(TIMEOUT_S)
            s.sendall(f"GET {path} HTTP/1.0\r\nHost: {host_header}\r\nConnection: close\r\n\r\n".encode())
            raw = b""
            while chunk := s.recv(4096):
                raw += chunk
    except OSError as e:
        return None, f"socket: {e.__class__.__name__}"
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = lines[0].split(" ")[1] if len(lines[0].split(" ")) > 1 else "?"
    if status != "200":
        return None, f"status {status}"
    headers = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in lines[1:])}
    size = int(headers.get("content-length", "0") or 0)
    if size <= 0 or size > MAX_BYTES:
        return None, f"content-length {size}"
    if len(body) != size:
        return None, "incomplete body"
    try:
        doc = json.loads(body)
    except ValueError:
        return None, "bad json"
    if doc.get("gate") not in ("open", "closed"):
        return None, "bad gate"
    if not isinstance(doc.get("lines"), list) or len(doc["lines"]) > MAX_LINES \
            or not all(isinstance(x, str) for x in doc["lines"]):
        return None, "bad lines"
    return doc, None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:8090/api/display")
    ap.add_argument("--host-header", default="10.42.0.1")
    ap.add_argument("--interval", type=float, default=1.0)   # bench GATEWAY_POLL_MS
    ap.add_argument("--duration", type=float, default=60)
    args = ap.parse_args()
    url = urlsplit(args.url)
    port = url.port or 80
    valid, errors, times, changes = 0, {}, [], []
    last_valid = start = time.monotonic()
    worst_gap, gate = 0.0, None
    while time.monotonic() - start < args.duration:
        t0 = time.monotonic()
        doc, err = poll(url.hostname, port, url.path or "/", args.host_header)
        if err:
            errors[err] = errors.get(err, 0) + 1
        else:
            valid += 1
            times.append((time.monotonic() - t0) * 1000)
            worst_gap = max(worst_gap, t0 - last_valid)
            last_valid = t0
            if doc["gate"] != gate:
                changes.append((round(t0 - start, 1), doc["gate"]))
                gate = doc["gate"]
        time.sleep(max(0, args.interval - (time.monotonic() - t0)))
    worst_gap = max(worst_gap, time.monotonic() - last_valid)
    p = sorted(times)
    lat = (f"median={statistics.median(p):.1f} / p95={p[math.ceil(len(p) * 0.95) - 1]:.1f} / "
           f"max={p[-1]:.1f}") if p else "n/a"
    print(f"polls={valid + sum(errors.values())}; valid={valid}; errors={errors or 'none'}; "
          f"response_ms {lat}; longest_gap_s={worst_gap:.1f} (fail-safe at {FAILSAFE_S}); gate changes={changes}")
    return 0 if valid and worst_gap < FAILSAFE_S else 1


if __name__ == "__main__":
    raise SystemExit(main())
