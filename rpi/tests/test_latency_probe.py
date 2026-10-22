"""Arithmetic of tools/latency_probe.py: per-frame samples from successive snapshots."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location(
    "latency_probe", Path(__file__).resolve().parents[1] / "tools" / "latency_probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


class TrackerTest(unittest.TestCase):
    def test_ten_second_poll_of_one_hertz_node(self):
        t = probe.Tracker(10)
        t.update(10040, 10000, 10)            # warm-up: frame predates the probe
        t.update(20040, 20000, 20)            # frames 11-19 skipped, 20 shown
        self.assertEqual(t.skipped, 9)
        self.assertEqual(t.lost, 0)
        self.assertEqual(sorted(t.screen), [40] + [1040 + 1000 * k for k in range(9)])
        self.assertEqual(len(t.rows), 2)

    def test_stream_gap_counts_lost_without_interpolating(self):
        t = probe.Tracker(1, interpolate=False)
        for seq in (1, 2, 5):
            t.update(seq * 1000 + 40, seq * 1000, seq)
        self.assertEqual(t.lost, 2)
        self.assertEqual(t.skipped, 0)
        self.assertEqual(t.screen, [40, 40])
        self.assertEqual(len(t.rows), 3)

    def test_same_frame_twice_is_counted_once(self):
        t = probe.Tracker(1)
        t.update(1040, 1000, 1)
        t.update(2040, 2000, 2)
        t.update(2500, 2000, 2)
        self.assertEqual(t.screen, [40])

    def test_no_seq_measures_nothing_per_frame(self):
        t = probe.Tracker(10)
        t.update(10040, 10000, None)
        t.update(20040, 20000, None)
        self.assertTrue(t.no_seq)
        self.assertEqual(t.screen, [])
        self.assertEqual([r[3] for r in t.rows], [40, 40])

    def test_fetch_gap_resets_instead_of_inventing_frames(self):
        t = probe.Tracker(1)
        t.update(3500, 3000, 3)
        t.update(200000, 199900, 200)         # probe paused for minutes
        self.assertEqual(t.resets, 1)
        self.assertEqual(t.screen, [])
        self.assertEqual(t.skipped, 0)

    def test_node_reboot_resets(self):
        t = probe.Tracker(1)
        t.update(50040, 50000, 50)
        t.update(51040, 51000, 2)             # seq went down
        self.assertEqual(t.resets, 1)
        self.assertEqual(t.screen, [])
        t.update(52040, 52000, 3)
        self.assertEqual(t.screen, [40])

    def test_negative_age_is_clamped(self):
        t = probe.Tracker(1)
        t.update(1000, 1000, 1)
        t.update(1995, 2000, 2)
        self.assertEqual(t.screen, [0])

    def test_implausible_seq_jump_resets(self):
        t = probe.Tracker(1)
        t.update(1040, 1000, 300)
        t.update(2040, 2000, 900)             # 600 frames in 1 s: not a 1-per-frame counter
        self.assertEqual(t.resets, 1)
        self.assertEqual(t.screen, [])
        t.update(3040, 3000, 1000000000)      # a huge jump is a reset, not a billion samples
        self.assertEqual(t.resets, 2)
        self.assertEqual(t.screen, [])


def state_doc(seq, refresh_s=1):
    return {"now_ms": 1000 * seq + 40, "settings": {"refresh_s": refresh_s},
            "segments": [{"id": "A", "rx_ms": 1000 * seq, "seq": seq}]}


class ProbeRunTest(unittest.TestCase):
    """Runs main() against stub gateway endpoints."""

    def run_probe(self, bodies, *argv):
        served = iter(bodies)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = next(served, bodies[-1])
                body = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        out, err = io.StringIO(), io.StringIO()
        old = sys.argv
        sys.argv = ["latency_probe.py", "--base", f"http://127.0.0.1:{srv.server_address[1]}", *argv]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = probe.main()
        finally:
            sys.argv = old
            srv.shutdown()
            srv.server_close()
        return code, out.getvalue(), err.getvalue()

    def test_bad_payload_is_counted_not_fatal(self):
        code, out, _ = self.run_probe([b"<html>portal</html>", {"now_ms": 1}, state_doc(1), state_doc(2)],
                                      "--interval", "0.05", "--duration", "0.6")
        self.assertEqual(code, 0)
        self.assertIn("'bad payload': 2", out)

    def test_wrong_typed_fields_are_bad_payload(self):
        bad_rx = state_doc(1)
        bad_rx["segments"][0]["rx_ms"] = "abc"
        bad_now = state_doc(1)
        bad_now["now_ms"] = None
        bad_seq = state_doc(1)
        bad_seq["segments"][0]["seq"] = "12"
        code, out, _ = self.run_probe([bad_rx, bad_now, bad_seq, state_doc(1), state_doc(2)],
                                      "--interval", "0.05", "--duration", "0.6")
        self.assertEqual(code, 0)
        self.assertIn("'bad payload': 3", out)

    def test_missing_segment_and_old_gateway_are_named(self):
        missing = state_doc(1)
        missing["segments"][0]["id"] = "B"
        old_gateway = state_doc(1)
        del old_gateway["segments"][0]["rx_ms"]
        code, _, err = self.run_probe([missing, old_gateway],
                                     "--interval", "0.05", "--duration", "0.4")
        self.assertEqual(code, 1)
        self.assertIn("no segment A", err)
        self.assertIn("no latency fields", err)

    def test_unusable_refresh_interval_stops_instead_of_spinning(self):
        code, _, err = self.run_probe([state_doc(1, refresh_s=-5)], "--duration", "5")
        self.assertEqual(code, 2)
        self.assertIn("Unusable poll interval", err)

    def test_stream_bad_payload_is_counted_not_fatal(self):
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                if self.path != "/api/stream":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for state in (state_doc(1), state_doc(2), {"now_ms": 1}):
                    event = f"event: state\ndata: {json.dumps(state)}\n\n".encode()
                    self.wfile.write(f"{len(event):x}\r\n".encode() + event + b"\r\n")
                self.wfile.flush()
                release.wait(2)              # hold the stream past the probe's duration
                self.close_connection = True

            def log_message(self, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        out, err = io.StringIO(), io.StringIO()
        old = sys.argv
        sys.argv = ["latency_probe.py", "--base", f"http://127.0.0.1:{srv.server_address[1]}",
                    "--stream", "--duration", "0.6"]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = probe.main()
        finally:
            sys.argv = old
            release.set()
            srv.shutdown()
            srv.server_close()
        self.assertEqual(code, 0)
        self.assertIn("stream; responses=2; errors={'bad payload': 1}; frames shown=2;", out.getvalue())
        self.assertIn("stream connections=1", out.getvalue())

    def run_closing_stream(self, states=(), status=200, duration=0.3):
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                if status == 200:
                    for state in states:
                        self.wfile.write(f"event: state\ndata: {json.dumps(state)}\n\n".encode())
                    self.wfile.flush()
                    release.wait(duration + 0.5)
                self.close_connection = True

            def log_message(self, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        out, err = io.StringIO(), io.StringIO()
        argv = ["latency_probe.py", "--base", f"http://127.0.0.1:{srv.server_address[1]}",
                "--stream", "--duration", str(duration)]
        try:
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = probe.main()
        finally:
            release.set()
            srv.shutdown()
            srv.server_close()
        return code, out.getvalue(), err.getvalue()

    def test_stream_http_error_is_named_and_backed_off(self):
        code, out, _ = self.run_closing_stream(status=503, duration=2.2)
        self.assertEqual(code, 0)
        self.assertIn("errors={'HTTP 503': 2}", out)
        self.assertIn("stream connections=2", out)

    def test_closing_stream_read_timeout_is_counted_and_backed_off(self):
        with patch.object(probe, "STREAM_READ_TIMEOUT_S", 0.05, create=True):
            code, out, _ = self.run_closing_stream([state_doc(1)])
        self.assertEqual(code, 0)
        self.assertIn("responses=1; errors={'stream stalled': 1}", out)
        self.assertIn("stream connections=1", out)

    def test_stream_sent_ms_is_the_visibility_time(self):
        states = [state_doc(1), state_doc(2)]
        for state in states:
            state["sent_ms"] = state["segments"][0]["rx_ms"] + 275
        code, out, _ = self.run_closing_stream(states)
        self.assertEqual(code, 0)
        self.assertIn("frame_to_screen_ms min=275 / median=275 / p95=275 / max=275 (n=1)", out)

    def test_stream_summary_reports_lost_frames_without_interpolation(self):
        code, out, _ = self.run_closing_stream([state_doc(seq) for seq in (1, 2, 5)])
        self.assertEqual(code, 0)
        self.assertIn("frames shown=3; frames lost=2; resets=0;", out)
        self.assertIn("frame_to_screen_ms min=40 / median=40 / p95=40 / max=40 (n=2)", out)


if __name__ == "__main__":
    unittest.main()
