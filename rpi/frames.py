"""
Raw-frame processing on the Pi.

The ESP32 sends one raw 8x8 frame per second (64 distances + 64 target statuses).
For each sensor this module:
  - learns an empty-floor background (median of the first CALIB_FRAMES frames, or
    after a recalibration request), saved to data/background_<id>.json
  - marks zones where something is >= HEIGHT_THRESHOLD_MM above the floor
  - groups occupied zones into blobs and estimates people
  - averages each minute into one row in the `readings` table (same table the
    forecast and dashboard already use)
"""
import json
import os
import statistics
import threading
import time
from datetime import datetime, timedelta

import store

HEIGHT_THRESHOLD_MM = float(os.environ.get("HEIGHT_THRESHOLD_MM", 400))
ZONES_PER_PERSON = float(os.environ.get("ZONES_PER_PERSON", 3.0))
MIN_BLOB_ZONES = int(os.environ.get("MIN_BLOB_ZONES", 2))
CALIB_FRAMES = int(os.environ.get("CALIB_FRAMES", 30))
BG_ADAPT_RATE = 0.01          # slow drift correction while the scene is empty
VALID_STATUS = {5, 9}
TS_FMT = "%Y-%m-%d %H:%M:%S"


def _bg_path(dev):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in dev)
    return os.path.join(os.path.dirname(store.DB_PATH), f"background_{safe}.json")


def estimate_people(occ):
    seen = [False] * 64
    people = 0
    for i in range(64):
        if not occ[i] or seen[i]:
            continue
        stack, size = [i], 0
        seen[i] = True
        while stack:
            z = stack.pop()
            size += 1
            r, c = divmod(z, 8)
            for rr, cc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                if 0 <= rr < 8 and 0 <= cc < 8:
                    n = rr * 8 + cc
                    if occ[n] and not seen[n]:
                        seen[n] = True
                        stack.append(n)
        if size >= MIN_BLOB_ZONES:
            people += max(1, round(size / ZONES_PER_PERSON))
    return people


class Sensor:
    def __init__(self, dev):
        self.dev = dev
        self.background = None        # list of 64 floats (mm) or None while learning
        self.calib = []               # frames collected for calibration
        self.bucket = None            # minute string 'YYYY-MM-DD HH:MM'
        self.acc_occ = self.acc_people = 0.0
        self.max_people = self.n = 0
        self.last = None              # latest processed frame for the live view
        self.last_seen = None
        try:
            with open(_bg_path(dev)) as f:
                bg = json.load(f)["background"]
            if len(bg) == 64:
                self.background = bg
        except (OSError, ValueError, KeyError):
            pass

    # ---- calibration
    def recalibrate(self):
        self.background = None
        self.calib = []

    def _learn(self, d, s):
        self.calib.append((d, s))
        if len(self.calib) < CALIB_FRAMES:
            return
        bg = []
        for i in range(64):
            vals = [fd[i] for fd, fs in self.calib if fs[i] in VALID_STATUS and fd[i] > 0]
            bg.append(statistics.median(vals) if vals else 0.0)   # 0 = zone unusable
        self.background = bg
        self.calib = []
        os.makedirs(os.path.dirname(_bg_path(self.dev)), exist_ok=True)
        with open(_bg_path(self.dev), "w") as f:
            json.dump({"background": bg, "saved": datetime.now().strftime(TS_FMT)}, f)

    # ---- per frame
    def process(self, d, s, now, con, rx_ms, seq=None):
        self.last_seen = now
        if self.background is None:
            self._learn(d, s)
            self.last = {"d": d, "s": s, "occ": [False] * 64, "people": 0,
                         "calibrating": True, "calib_progress": len(self.calib), "t": now.strftime(TS_FMT),
                         "rx_ms": rx_ms, "seq": seq}
            return
        bg = self.background
        valid = [s[i] in VALID_STATUS and d[i] > 0 and bg[i] > 0 for i in range(64)]
        occ = [valid[i] and (bg[i] - d[i]) > HEIGHT_THRESHOLD_MM for i in range(64)]
        n_occ = sum(occ)
        if n_occ == 0:
            for i in range(64):
                if valid[i]:
                    bg[i] += BG_ADAPT_RATE * (d[i] - bg[i])
        people = estimate_people(occ)
        occ_pct = n_occ * 100.0 / 64
        self.last = {"d": d, "s": s, "occ": occ, "people": people, "occupancy_pct": round(occ_pct, 1),
                     "calibrating": False, "t": now.strftime(TS_FMT), "rx_ms": rx_ms, "seq": seq}

        minute = now.strftime("%Y-%m-%d %H:%M")
        if self.bucket and minute != self.bucket:
            self.flush(con)
        self.bucket = minute
        self.acc_occ += occ_pct
        self.acc_people += people
        self.max_people = max(self.max_people, people)
        self.n += 1

    def flush(self, con):
        if self.bucket and self.n:
            con.execute(
                "INSERT INTO readings (ts, device_id, occupancy_pct, people_mean, people_max, frames,"
                " received_at) VALUES (?,?,?,?,?,?,?)",
                (self.bucket + ":00", self.dev, round(self.acc_occ / self.n, 2),
                 round(self.acc_people / self.n, 2), self.max_people, self.n,
                 datetime.now().strftime(TS_FMT)))
        self.bucket = None
        self.acc_occ = self.acc_people = 0.0
        self.max_people = self.n = 0


class FrameProcessor:
    def __init__(self):
        self.lock = threading.Lock()
        self.sensors = {}
        t = threading.Thread(target=self._flusher, daemon=True)
        t.start()

    def _get(self, dev):
        if dev not in self.sensors:
            self.sensors[dev] = Sensor(dev)
        return self.sensors[dev]

    def add(self, dev, d, s, seq=None):
        now = datetime.now()
        rx_ms = int(time.time() * 1000)
        with self.lock:
            con = store.connect()
            with con:
                self._get(dev).process(d, s, now, con, rx_ms, seq)
            con.close()

    def recalibrate(self, devs):
        with self.lock:
            for dev in devs:
                self._get(dev).recalibrate()

    def live(self):
        with self.lock:
            return {dev: dict(sen.last, background=sen.background) for dev, sen in self.sensors.items()
                    if sen.last}

    def _flusher(self):
        """Write out minute buckets of sensors that stopped sending."""
        import time
        while True:
            time.sleep(10)
            cutoff = datetime.now() - timedelta(seconds=70)
            with self.lock:
                stale = [s for s in self.sensors.values()
                         if s.bucket and s.last_seen and s.last_seen < cutoff]
                if stale:
                    con = store.connect()
                    with con:
                        for s in stale:
                            s.flush(con)
                    con.close()
