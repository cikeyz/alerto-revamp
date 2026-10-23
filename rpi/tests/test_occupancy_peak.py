"""Component peaks reject low returns without erasing connected body edges."""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import frames
import store


class OccupancyPeakTest(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / "outputs"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="occupancy-peak-tests-", dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(store, "DB_PATH", str(Path(self.temp.name) / "density.db")))
        self.enterContext(patch.object(frames, "MIN_PEAK_MM", 700))
        self.enterContext(patch.object(frames, "HEIGHT_THRESHOLD_MM", 400))
        self.enterContext(patch.object(frames, "MIN_BLOB_ZONES", 2))
        self.enterContext(patch.object(frames, "ZONES_PER_PERSON", 3.0))
        self.sensor = frames.Sensor("peak-test")
        self.sensor.background = [2500.0] * 64
        self.now = datetime(2026, 10, 23, 12, 0)

    def process(self, heights, statuses=None):
        distances = [2500 - heights.get(i, 0) for i in range(64)]
        self.sensor.process(distances, statuses or [5] * 64, self.now, None, 123, 7)
        return self.sensor.last

    def test_isolated_low_blip_is_ignored(self):
        last = self.process({40: 578})
        self.assertEqual(last["occ"], [False] * 64)
        self.assertEqual(last["occupancy_pct"], 0.0)

    def test_body_keeps_low_edges_and_excludes_separate_low_group(self):
        last = self.process({27: 1300, 28: 450, 36: 500, 40: 550, 48: 500})
        self.assertEqual(last["occ"], [i in (27, 28, 36) for i in range(64)])
        self.assertEqual(last["occupancy_pct"], 4.7)
        self.assertEqual(last["people"], 1)
        self.assertEqual(self.sensor.acc_occ, 3 * 100.0 / 64)
        self.assertEqual(self.sensor.acc_people, 1)

    def test_single_tall_zone_at_each_edge_counts(self):
        for zone in (0, 7, 23, 56, 63):
            with self.subTest(zone=zone):
                last = self.process({zone: 900})
                self.assertEqual(last["occ"], [i == zone for i in range(64)])
                self.assertEqual(last["occupancy_pct"], 1.6)

    def test_peak_is_inclusive_and_per_zone_threshold_is_strict(self):
        last = self.process({27: 700, 28: 400, 29: 450, 63: 699})
        self.assertEqual(last["occ"], [i == 27 for i in range(64)])

    def test_diagonal_and_row_wrap_do_not_connect(self):
        last = self.process({7: 1000, 8: 500, 14: 500})
        self.assertEqual(last["occ"], [i == 7 for i in range(64)])

    def test_invalid_tall_return_cannot_support_low_zones(self):
        for invalid in ("status", "distance", "background"):
            with self.subTest(invalid=invalid):
                self.sensor.background = [2500.0] * 64
                statuses = [5] * 64
                heights = {27: 1000, 28: 500}
                if invalid == "status":
                    statuses[27] = 0
                elif invalid == "distance":
                    heights[27] = 2500
                else:
                    self.sensor.background[27] = 0.0
                self.assertEqual(self.process(heights, statuses)["occ"], [False] * 64)

    def test_zero_peak_restores_unfiltered_outputs(self):
        with patch.object(frames, "MIN_PEAK_MM", 0):
            last = self.process({0: 450, 1: 500, 7: 578, 63: 1000})
        self.assertEqual(last, {
            "d": [2500 - {0: 450, 1: 500, 7: 578, 63: 1000}.get(i, 0) for i in range(64)],
            "s": [5] * 64, "occ": [i in (0, 1, 7, 63) for i in range(64)],
            "people": 1, "occupancy_pct": 6.2, "calibrating": False,
            "t": "2026-10-23 12:00:00", "rx_ms": 123, "seq": 7,
        })
        self.assertEqual(self.sensor.background, [2500.0] * 64)
        self.assertEqual(self.sensor.acc_occ, 6.25)
        self.assertEqual(self.sensor.acc_people, 1)
        self.assertEqual(self.sensor.max_people, 1)
        self.assertEqual(self.sensor.n, 1)

    def test_background_adaptation_uses_raw_occupancy(self):
        self.process({40: 550, 63: 100})
        self.assertEqual(self.sensor.last["occupancy_pct"], 0.0)
        self.assertEqual(self.sensor.background, [2500.0] * 64)
        self.process({63: 100})
        self.assertEqual(self.sensor.background, [2499.0 if i == 63 else 2500.0 for i in range(64)])


if __name__ == "__main__":
    unittest.main()
