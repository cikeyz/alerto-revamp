"""tools/fake_bench.py reads responses the way the bench firmware does."""
import contextlib
import importlib.util
import io
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location(
    "fake_bench", Path(__file__).resolve().parents[1] / "tools" / "fake_bench.py")
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)

VALID = b'{"gate":"open","lines":["Zone A"]}'


def serve(*scripts):
    """Answer one request per script on an ephemeral port. A script is raw bytes, or a function given the
    connection that writes whatever it likes. Returns the port."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(len(scripts))

    def run():
        for script in scripts:
            conn, _ = srv.accept()
            with conn:
                conn.recv(1024)
                try:
                    script(conn) if callable(script) else conn.sendall(script)
                except OSError:
                    pass                          # the client hung up first, as the bench would
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def serve_once(response, keep_open=0.0):
    def script(conn):
        conn.sendall(response)
        time.sleep(keep_open)
    return serve(script)


def ok(body, extra=b""):
    return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body + extra


class FakeBenchTest(unittest.TestCase):
    def poll(self, response, keep_open=0.0):
        return bench.poll("127.0.0.1", serve_once(response, keep_open), "/api/display", "10.42.0.1")

    def test_valid(self):
        doc, err = self.poll(ok(VALID))
        self.assertIsNone(err)
        self.assertEqual(doc["gate"], "open")

    def test_connection_left_open_is_still_valid(self):
        # the bench reads exactly Content-Length bytes and does not wait for the socket to close
        start = time.monotonic()
        doc, err = self.poll(ok(VALID), keep_open=1.0)
        self.assertIsNone(err)
        self.assertLess(time.monotonic() - start, 0.2)

    def test_endless_stream_without_length_fails_fast(self):
        def drip(conn):
            conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            for _ in range(40):                   # 2 s of chunks, each well inside the idle timeout
                conn.sendall(b"5\r\nhello\r\n")
                time.sleep(0.05)
        start = time.monotonic()
        _, err = bench.poll("127.0.0.1", serve(drip), "/api/display", "10.42.0.1")
        self.assertEqual(err, "content-length -1")
        self.assertLess(time.monotonic() - start, 0.5)

    def test_bare_lf_headers_are_accepted(self):
        doc, err = self.poll(b"HTTP/1.1 200 OK\nContent-Length: %d\n\n" % len(VALID) + VALID)
        self.assertIsNone(err)

    def test_extra_bytes_after_length_are_ignored(self):
        doc, err = self.poll(ok(VALID, b"trailing"))
        self.assertIsNone(err)

    def test_non_numeric_length_is_rejected_not_crashing(self):
        _, err = self.poll(b"HTTP/1.1 200 OK\r\nContent-Length: abc\r\n\r\n" + VALID)
        self.assertEqual(err, "content-length 0")

    def test_missing_length_is_rejected(self):
        _, err = self.poll(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n")
        self.assertEqual(err, "content-length -1")

    def test_oversized_is_rejected(self):
        _, err = self.poll(ok(b'{"gate":"open","lines":[],"pad":"' + b"x" * 600 + b'"}'))
        self.assertTrue(err.startswith("content-length"))

    def test_short_body_is_rejected(self):
        _, err = self.poll(b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\n" + VALID)
        self.assertEqual(err, "incomplete body")

    def test_unknown_gate_and_too_many_lines(self):
        _, err = self.poll(ok(b'{"gate":"maybe","lines":[]}'))
        self.assertEqual(err, "bad gate")
        _, err = self.poll(ok(b'{"gate":"closed","lines":["a","b","c","d","e"]}'))
        self.assertEqual(err, "bad lines")

    def test_non_200(self):
        _, err = self.poll(b"HTTP/1.1 302 Found\r\nLocation: /\r\nContent-Length: 0\r\n\r\n")
        self.assertEqual(err, "status 302")

    def test_to_int_matches_arduino(self):
        self.assertEqual(bench.to_int("42"), 42)
        self.assertEqual(bench.to_int(" 42abc"), 42)
        self.assertEqual(bench.to_int("abc"), 0)
        self.assertEqual(bench.to_int(None), 0)


class GapTest(unittest.TestCase):
    def test_fail_safe_gap_counts_from_completed_responses(self):
        # fast answer, then one that finishes 0.2 s after it starts: starts are 0.3 s apart, completions
        # about 0.5 s apart; a 0.45 s fail-safe must trip, as on the bench
        def slow(conn):
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(VALID) + VALID[:5])
            time.sleep(0.2)
            conn.sendall(VALID[5:])
        port = serve(ok(VALID), slow)
        argv = ["fake_bench.py", "--url", f"http://127.0.0.1:{port}/api/display",
                "--interval", "0.3", "--duration", "0.35"]
        out = io.StringIO()
        with patch.object(bench, "FAILSAFE_S", 0.45), patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out):
            code = bench.main()
        self.assertIn("valid=2", out.getvalue())
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
