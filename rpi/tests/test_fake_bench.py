"""tools/fake_bench.py reads responses the way the bench firmware does."""
import importlib.util
from pathlib import Path
import socket
import threading
import time
import unittest

_spec = importlib.util.spec_from_file_location(
    "fake_bench", Path(__file__).resolve().parents[1] / "tools" / "fake_bench.py")
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)

VALID = b'{"gate":"open","lines":["Zone A"]}'


def serve_once(response, keep_open=0.0):
    """Answer one request with raw bytes on an ephemeral port; returns the port."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        with conn:
            conn.recv(1024)
            conn.sendall(response)
            time.sleep(keep_open)
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


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
        doc, err = self.poll(ok(VALID), keep_open=1.0)
        self.assertIsNone(err)

    def test_extra_bytes_after_length_are_ignored(self):
        doc, err = self.poll(ok(VALID, b"trailing"))
        self.assertIsNone(err)

    def test_non_numeric_length_is_rejected_not_crashing(self):
        _, err = self.poll(b"HTTP/1.1 200 OK\r\nContent-Length: abc\r\n\r\n" + VALID)
        self.assertEqual(err, "content-length 0")

    def test_missing_length_is_rejected(self):
        _, err = self.poll(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n")
        self.assertEqual(err, "content-length 0")

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


if __name__ == "__main__":
    unittest.main()
