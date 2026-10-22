"""Arithmetic of tools/latency_probe.py: per-frame samples from successive snapshots."""
import importlib.util
from pathlib import Path
import unittest

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
        self.assertEqual(sorted(t.screen), [40] + [1040 + 1000 * k for k in range(9)])
        self.assertEqual(len(t.rows), 2)

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


if __name__ == "__main__":
    unittest.main()
