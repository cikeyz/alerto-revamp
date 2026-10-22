"""Latency metadata stays optional on the raw-frame endpoint."""
import os
from pathlib import Path
import tempfile
import unittest


_temp_root = Path(__file__).resolve().parents[1] / "outputs"
_temp_root.mkdir(exist_ok=True)
_temp = tempfile.mkdtemp(prefix="latency-tests-", dir=_temp_root)
os.environ["DENSITY_DB"] = str(Path(_temp) / "density.db")
os.environ["DENSITY_OUT"] = _temp
os.environ["DEVICE_KEY"] = ""
os.environ["ADMIN_PIN"] = ""

import app


class LatencyFieldsTest(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self.headers = {"Host": "127.0.0.1"}
        with app.processor.lock:
            app.processor.sensors.clear()

    def post(self, **extra):
        body = {"device_id": "platform-A", "d": [2500] * 64, "s": [5] * 64}
        body.update(extra)
        return self.client.post("/frame", json=body, headers=self.headers)

    def state(self):
        resp = self.client.get("/api/state", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        state = resp.get_json()
        return state, next(s for s in state["segments"] if s["id"] == "A")

    def test_seq_and_receive_time(self):
        resp = self.post(seq=7)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True})
        state, seg = self.state()
        self.assertEqual(seg["seq"], 7)
        self.assertIsInstance(seg["rx_ms"], int)
        self.assertIsInstance(state["now_ms"], int)
        self.assertGreaterEqual(state["now_ms"], seg["rx_ms"])
        self.assertEqual(seg["source"], "live")
        self.assertTrue(seg["calibrating"])
        for other in state["segments"][1:]:
            self.assertIsNone(other["rx_ms"])
            self.assertIsNone(other["seq"])

    def test_missing_seq(self):
        self.assertEqual(self.post().status_code, 200)
        _, seg = self.state()
        self.assertIsNone(seg["seq"])

    def test_invalid_seq(self):
        self.assertEqual(self.post(seq="abc").status_code, 200)
        _, seg = self.state()
        self.assertIsNone(seg["seq"])

    def test_short_frame(self):
        self.assertEqual(self.post(d=[2500] * 63).status_code, 400)


if __name__ == "__main__":
    unittest.main()
