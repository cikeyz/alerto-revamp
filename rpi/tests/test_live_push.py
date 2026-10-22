"""Shared snapshots, gate decisions and bounded live streams."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch


_temp_root = Path(__file__).resolve().parents[1] / "outputs"
_temp_root.mkdir(exist_ok=True)
_temp = tempfile.mkdtemp(prefix="live-push-tests-", dir=_temp_root)
os.environ["DENSITY_DB"] = str(Path(_temp) / "density.db")
os.environ["DENSITY_OUT"] = _temp
os.environ["DEVICE_KEY"] = ""
os.environ["ADMIN_PIN"] = ""

import app


class LivePushTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app._state_builder.close()

    @classmethod
    def tearDownClass(cls):
        app._state_builder = app.StateBuilder()

    def setUp(self):
        self.client = app.app.test_client()
        self.headers = {"Host": "127.0.0.1"}
        self.settings = copy.deepcopy(app.cfg.DEFAULTS)
        self.settings.update(gate_enabled=True, high_at=75, moderate_at=40, language="en")
        self.settings["segments"] = {s: f"platform-{s}" for s in app.cfg.SEGMENT_IDS}
        self.builder = app.StateBuilder(interval=60)
        self.builder_patch = patch.object(app, "_state_builder", self.builder)
        self.builder_patch.start()
        self.addCleanup(self.builder_patch.stop)
        self.settings_patch = patch.object(app.cfg, "load", return_value=self.settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.addCleanup(self.builder.close)
        with app.processor.lock:
            app.processor.sensors.clear()
        with app._gate_lock:
            app._gate.update(closed=False, since=None)

    def state(self):
        response = self.client.get("/api/state", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def post_frame(self, seq=42):
        response = self.client.post("/frame", headers=self.headers, json={
            "device_id": "platform-A", "d": [2500] * 64, "s": [5] * 64, "seq": seq})
        self.assertEqual(response.status_code, 200)

    def live_frame(self, occupancy, seq=7):
        return {"t": app.datetime.now().strftime(app.TS), "rx_ms": int(app.time.time() * 1000),
                "seq": seq, "occupancy_pct": occupancy, "people": 4, "calibrating": False,
                "d": [1500] * 64, "s": [5] * 64, "occ": [True] * 64, "background": [2500] * 64}

    def test_requests_share_revision_and_do_not_decide_gate(self):
        with patch.object(app, "update_gate", wraps=app.update_gate) as update:
            first = self.state()
            gate = dict(app._gate)
            second = self.state()
            self.assertEqual(first["rev"], second["rev"])
            self.assertEqual(first["gate"], second["gate"])
            self.assertEqual(gate, app._gate)
            self.assertEqual(update.call_count, 1)

    def test_frame_rebuild_increases_revision_and_preserves_seq(self):
        first = self.state()
        self.post_frame()
        rebuilt = app.rebuild_now()
        state = self.state()
        self.assertGreater(state["rev"], first["rev"])
        self.assertEqual(rebuilt["rev"], state["rev"])
        self.assertEqual(state["segments"][0]["seq"], 42)
        live = self.client.get("/api/live", headers=self.headers).get_json()
        self.assertEqual(live["platform-A"]["seq"], state["segments"][0]["seq"])

    def test_gate_hysteresis_through_rebuilds(self):
        frames = {f"platform-{s}": self.live_frame(75) for s in app.cfg.SEGMENT_IDS}
        with patch.object(app.processor, "live", side_effect=lambda: copy.deepcopy(frames)):
            self.assertTrue(app.rebuild_now()["gate"]["closed"])
            frames["platform-A"]["occupancy_pct"] = 70
            self.assertTrue(app.rebuild_now()["gate"]["closed"])
            frames["platform-A"]["occupancy_pct"] = 69.9
            self.assertFalse(app.rebuild_now()["gate"]["closed"])
            frames["platform-A"]["occupancy_pct"] = 74.9
            self.assertFalse(app.rebuild_now()["gate"]["closed"])
            frames["platform-A"]["occupancy_pct"] = 75
            self.assertTrue(app.rebuild_now()["gate"]["closed"])
            self.settings["gate_enabled"] = False
            self.assertFalse(app.rebuild_now()["gate"]["closed"])

    def test_display_contract_matches_previous_feed(self):
        frames = {"platform-A": self.live_frame(90), "platform-B": self.live_frame(20)}
        expected = {"gate": "open", "title": "GO TO ZONE B",
                    "lines": ["Zone A is full.", "Proceed to Zone B."],
                    "levels": "A:H B:L C:- D:-", "lang": "en", "zone": "B"}
        with patch.object(app.processor, "live", return_value=frames):
            app.rebuild_now()
            with patch.object(app, "update_gate", side_effect=AssertionError("request decided gate")):
                response = self.client.get("/api/display", headers=self.headers)
            self.assertEqual(response.get_json(), expected)
            self.assertEqual(response.data, app.app.json.response(expected).data)

    def test_stream_first_chunk_contains_state_and_matching_live_frame(self):
        self.post_frame()
        app.rebuild_now()
        response = self.client.get("/api/stream", headers=self.headers, buffered=False)
        try:
            self.assertEqual(response.mimetype, "text/event-stream")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["X-Accel-Buffering"], "no")
            chunk = next(iter(response.response)).decode()
            self.assertTrue(chunk.startswith("event: state\n"))
            state = json.loads(next(line[6:] for line in chunk.splitlines() if line.startswith("data: ")))
            self.assertIsInstance(state["rev"], int)
            self.assertEqual(state["segments"][0]["seq"], state["live"]["platform-A"]["seq"])
        finally:
            response.close()

    def test_fifth_stream_is_rejected_and_closed_streams_release_slots(self):
        streams = []
        try:
            for _ in range(4):
                response = self.client.get("/api/stream", headers=self.headers, buffered=False)
                streams.append(response)
                self.assertEqual(response.status_code, 200)
            fifth = self.client.get("/api/stream", headers=self.headers, buffered=False)
            self.assertEqual(fifth.status_code, 503)
            self.assertIn("error", fifth.get_json())
            fifth.close()
        finally:
            for response in streams:
                response.close()
        replacement = self.client.get("/api/stream", headers=self.headers, buffered=False)
        self.assertEqual(replacement.status_code, 200)
        replacement.close()

    def test_unstarted_stream_releases_slot_on_close(self):
        with app.app.test_request_context("/api/stream", headers=self.headers):
            for _ in range(5):
                response = app.api_stream()
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_stream_waits_for_revision_and_sends_silent_heartbeat(self):
        response = self.client.get("/api/stream", headers=self.headers, buffered=False)
        try:
            chunks = iter(response.response)
            next(chunks)
            with patch.object(self.builder.condition, "wait_for", return_value=False) as wait:
                self.assertEqual(next(chunks), b": ping\n\n")
                self.assertEqual(wait.call_args.kwargs["timeout"], 15)
            self.post_frame(seq=99)
            app.rebuild_now()
            chunk = next(chunks).decode()
            state = json.loads(next(line[6:] for line in chunk.splitlines() if line.startswith("data: ")))
            self.assertEqual(state["segments"][0]["seq"], 99)
        finally:
            response.close()

    def test_builder_refreshes_without_frames(self):
        self.builder.interval = 0.02
        first = self.state()
        with self.builder.condition:
            self.assertTrue(self.builder.condition.wait_for(
                lambda: self.builder.current["state"]["rev"] > first["rev"], timeout=1))

    def test_builder_recovers_after_exception(self):
        self.builder.interval = 0.01
        build = app.build_state
        calls = 0

        def fail_once(live):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("test rebuild failure")
            return build(live)

        with patch.object(app, "build_state", side_effect=fail_once):
            with self.assertLogs(app.app.logger, level="ERROR"):
                state = self.state()
            self.assertGreaterEqual(calls, 2)
            self.assertIsInstance(state["rev"], int)
            self.assertTrue(self.builder.thread.is_alive())

    def test_notifications_during_build_are_coalesced(self):
        entered, finish = threading.Event(), threading.Event()
        build = app.build_state
        calls = []

        def paused(live):
            calls.append(threading.current_thread())
            if len(calls) == 1:
                entered.set()
                finish.wait(2)
            return build(live)

        with patch.object(app, "build_state", side_effect=paused):
            try:
                self.builder.notify()
                self.assertTrue(entered.wait(1))
                for _ in range(100):
                    self.builder.notify()
            finally:
                finish.set()
            self.builder.get()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(t is self.builder.thread for t in calls))

    def test_one_live_read_per_snapshot_and_detached_background(self):
        self.post_frame()
        self.builder.get()
        with patch.object(app.processor, "live", wraps=app.processor.live) as live:
            app.rebuild_now()
            self.assertEqual(live.call_count, 1)
        with app.processor.lock:
            sensor = app.processor.sensors["platform-A"]
            sensor.background = [2500] * 64
        frame = app.processor.live()["platform-A"]
        with app.processor.lock:
            sensor.background[0] = 2490
        self.assertEqual(frame["background"][0], 2500)


    def test_started_stream_releases_its_slot(self):
        response = self.client.get("/api/stream", headers=self.headers, buffered=False)
        chunks = iter(response.response)
        self.assertTrue(next(chunks).startswith(b"event: state"))
        self.assertEqual(app._stream_slots._value, 3)
        response.close()
        self.assertEqual(app._stream_slots._value, 4)

    def test_not_ready_is_a_fast_503(self):
        import time
        with patch.object(app, "build_state", side_effect=RuntimeError("boom")):
            start = time.monotonic()
            display = self.client.get("/api/display", headers={"Host": "10.42.0.1"})
            stream = self.client.get("/api/stream", headers=self.headers, buffered=False)
            elapsed = time.monotonic() - start
        self.assertEqual(display.status_code, 503)
        self.assertEqual(stream.status_code, 503)
        stream.close()
        self.assertLess(elapsed, 0.5)
        self.assertEqual(app._stream_slots._value, 4)

    def test_state_carries_boot_id(self):
        self.post_frame()
        app.rebuild_now()
        self.assertEqual(self.state()["boot"], app.BOOT_ID)

    def test_builder_restarts_after_close(self):
        first = app.rebuild_now()["rev"]
        self.builder.close()
        self.assertGreater(app.rebuild_now()["rev"], first)


if __name__ == "__main__":
    unittest.main()
