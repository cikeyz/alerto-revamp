"""The turnstile bench's view of /api/display (see docs/bench-contract.md)."""
import json
import os
from pathlib import Path
import tempfile
import unittest


_temp_root = Path(__file__).resolve().parents[1] / "outputs"
_temp_root.mkdir(exist_ok=True)
_temp = tempfile.mkdtemp(prefix="bench-tests-", dir=_temp_root)
os.environ["DENSITY_DB"] = str(Path(_temp) / "density.db")
os.environ["DENSITY_OUT"] = _temp
os.environ["DEVICE_KEY"] = ""
os.environ["ADMIN_PIN"] = ""

import app
import frames
import settings as cfg

FLOOR_MM = 2500
MAX_BYTES = 512      # bench GATEWAY_RESPONSE_MAX_BYTES
MAX_LINES = 4        # bench GUIDANCE_LINES
COLS = 21            # bench TFT columns


class BenchContractTest(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        cfg.reset()
        with app.processor.lock:
            app.processor.sensors.clear()
        for _ in range(30):                       # empty-floor calibration
            self.frame(0)

    def tearDown(self):
        # leave no learned floor or settings behind for other test modules in this process
        with app.processor.lock:
            app.processor.sensors.clear()
        Path(frames._bg_path("platform-A")).unlink(missing_ok=True)
        cfg.reset()

    def frame(self, occupied):
        d = [FLOOR_MM - 1000] * occupied + [FLOOR_MM] * (64 - occupied)
        resp = self.client.post("/frame", json={"device_id": "platform-A", "d": d, "s": [5] * 64},
                                headers={"Host": "10.42.0.1"})
        self.assertEqual(resp.status_code, 200)

    def display(self):
        if hasattr(app, "rebuild_now"):           # gateways that build state off the request path
            app.rebuild_now()
        # the bench sends Host 10.42.0.1; any other host must not be redirected either
        for host in ("10.42.0.1", "example.invalid"):
            resp = self.client.get("/api/display", headers={"Host": host})
            self.assertEqual(resp.status_code, 200, host)
        body = resp.get_data()
        self.assertEqual(int(resp.headers["Content-Length"]), len(body))
        self.assertLessEqual(len(body), MAX_BYTES)
        doc = json.loads(body)
        self.assertIn(doc["gate"], ("open", "closed"))
        self.assertIsInstance(doc["lines"], list)
        self.assertLessEqual(len(doc["lines"]), MAX_LINES)
        for line in doc["lines"]:
            self.assertIsInstance(line, str)
            self.assertLessEqual(len(line), COLS)
        return doc

    def test_no_data_is_open(self):
        with app.processor.lock:
            app.processor.sensors.clear()
        self.assertEqual(self.display()["gate"], "open")

    def test_build_guide_hold_setup(self):
        # BUILD-GUIDE bench test step 3: Moderate 1, High 2, gate enabled, readings >= 2 percent
        cfg.save({"moderate_at": 1, "high_at": 2, "gate_enabled": True})
        self.frame(8)
        self.assertEqual(self.display()["gate"], "closed")
        # step 2: gate setting off reopens
        cfg.save({"gate_enabled": False})
        self.assertEqual(self.display()["gate"], "open")

    def test_hysteresis(self):
        cfg.save({"moderate_at": 10, "high_at": 50, "gate_enabled": True})
        self.frame(64)                            # 100 percent
        self.assertEqual(self.display()["gate"], "closed")
        self.frame(30)                            # 46.9 percent: inside the 5-point band, stays closed
        self.assertEqual(self.display()["gate"], "closed")
        self.frame(28)                            # 43.75 percent: below high_at - 5, reopens
        self.assertEqual(self.display()["gate"], "open")

    def test_both_languages_fit(self):
        for lang in ("fil", "en"):
            cfg.save({"language": lang, "moderate_at": 1, "high_at": 2, "gate_enabled": True})
            self.frame(8)
            self.display()


if __name__ == "__main__":
    unittest.main()
