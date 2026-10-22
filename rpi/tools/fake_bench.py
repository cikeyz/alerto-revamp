"""Poll /api/display the way the turnstile bench firmware does and report what it would see.

Mirrors pollGateway(): new connection, HTTP/1.0, 250 ms connect and read idle timeouts, the status and
Content-Length checks before reading the body, then exactly Content-Length bytes. The 30 s fail-safe gap is
measured between completed valid responses, as the bench does.

One deliberate difference: if the body stalls for more than 250 ms the tool fails that poll, while the
bench's HTTPClient keeps waiting on an open socket (and can sit there until its fail-safe). A stall is a
gateway problem either way; the tool reports it instead of hiding it.
"""
import argparse
import json
import math
import re
import socket
import statistics
import time
from urllib.parse import urlsplit

TIMEOUT_S = 0.25      # bench GATEWAY_HTTP_TIMEOUT_MS: connect and read idle timeout
MAX_BYTES = 512       # bench GATEWAY_RESPONSE_MAX_BYTES
MAX_LINES = 4         # bench GUIDANCE_LINES
FAILSAFE_S = 30       # bench FAILSAFE_MS
HEAD_LIMIT = 65536    # the firmware has no cap; this only stops a runaway server from filling memory


def to_int(value):
    """Arduino String::toInt(): leading integer, otherwise 0."""
    m = re.match(r"\s*([+-]?\d+)", value or "")
    return int(m.group(1)) if m else 0


def header_end(raw):
    """(end of headers, start of body) for a CRLF or bare-LF blank line, or None if not seen yet."""
    found = [(i, i + len(sep)) for sep in (b"\r\n\r\n", b"\n\n") if (i := raw.find(sep)) >= 0]
    return min(found) if found else None


def poll(host, port, path, host_header):
    """One bench poll. Returns (doc, error)."""
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT_S) as s:
            s.settimeout(TIMEOUT_S)
            s.sendall(f"GET {path} HTTP/1.0\r\nHost: {host_header}\r\nConnection: close\r\n\r\n".encode())
            raw = b""
            # the firmware reads header lines up to '\n' and trims them, so a bare LF ending counts too
            while (end := header_end(raw)) is None:
                chunk = s.recv(1024)
                if not chunk or len(raw) > HEAD_LIMIT:
                    return None, "no headers"
                raw += chunk
            head, body = raw[:end[0]], raw[end[1]:]
            lines = [ln.strip() for ln in head.decode("latin-1").split("\n")]
            parts = lines[0].split(" ")
            status = parts[1] if len(parts) > 1 else "?"
            headers = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in lines[1:])}
            # HTTPClient reports -1 when the header is absent and toInt() of its value otherwise
            size = to_int(headers["content-length"]) if "content-length" in headers else -1
            if status != "200" or size <= 0 or size > MAX_BYTES:
                return None, f"status {status}" if status != "200" else f"content-length {size}"
            while len(body) < size:              # read exactly Content-Length bytes, as the bench does
                chunk = s.recv(size - len(body))
                if not chunk:
                    return None, "incomplete body"
                body += chunk
            body = body[:size]
    except OSError as e:
        return None, f"socket: {e.__class__.__name__}"
    try:
        doc = json.loads(body)
    except ValueError:
        return None, "bad json"
    if not isinstance(doc, dict) or doc.get("gate") not in ("open", "closed"):
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
    start = last_valid = time.monotonic()
    worst_gap, gate = 0.0, None
    while time.monotonic() - start < args.duration:
        t0 = time.monotonic()
        doc, err = poll(url.hostname, port, url.path or "/", args.host_header)
        done = time.monotonic()
        if err:
            errors[err] = errors.get(err, 0) + 1
        else:
            valid += 1
            times.append((done - t0) * 1000)
            worst_gap = max(worst_gap, done - last_valid)
            last_valid = done                    # the bench's fail-safe clock restarts on completion
            if doc["gate"] != gate:
                changes.append((round(done - start, 1), doc["gate"]))
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
