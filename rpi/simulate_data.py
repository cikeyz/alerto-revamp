"""
Fills the database with SIMULATED minute rows for segments A-D (sensor ids
platform-A..D) so the dashboard and forecasts can be tried before real data exists.

  python simulate_data.py --days 21     # add simulated history up to now
  python simulate_data.py --clear       # delete ONLY simulated rows (marked frames = -1)
"""
import argparse

import holidays
import numpy as np
import pandas as pd

import store

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=21)
ap.add_argument("--clear", action="store_true")
a = ap.parse_args()
store.init_db()
con = store.connect()

if a.clear:
    n = con.execute("DELETE FROM readings WHERE frames = -1").rowcount
    con.commit()
    print(f"deleted {n} simulated rows")
    raise SystemExit

# relative crowding per segment: D (by the main stairs) fills first, then B
PROFILE = {"A": 0.62, "B": 0.95, "C": 0.75, "D": 1.12}
rng = np.random.default_rng(11)
end = pd.Timestamp.now().floor("min")
idx = pd.date_range(end - pd.Timedelta(days=a.days), end - pd.Timedelta(minutes=1), freq="min")
h = idx.hour + idx.minute / 60
ph = holidays.PH(years=sorted(set(idx.year)))
off = (idx.dayofweek >= 5) | np.array([d in ph for d in idx.date])
open_h = (h >= 5) & (h < 23)
work = 15 + 70 * np.exp(-((h - 7.6) ** 2) / (2 * 0.9 ** 2)) + 75 * np.exp(-((h - 18.1) ** 2) / (2 * 1.1 ** 2))
weekend = 15 + 25 * np.exp(-((h - 14) ** 2) / (2 * 3 ** 2))
base = np.where(off, weekend, work)
days = pd.date_range(idx[0].normalize(), idx[-1].normalize())
day_f = pd.Series(rng.normal(1, 0.08, len(days)), index=days).reindex(idx.normalize()).values
train = 8 * np.maximum(np.sin(2 * np.pi * idx.minute / 4), 0)          # a train every ~4 min

con.execute("DELETE FROM readings WHERE frames = -1")
ts = idx.strftime("%Y-%m-%d %H:%M:%S")
for seg, k in PROFILE.items():
    occ = (base * k * day_f + train + rng.normal(0, 3, len(idx))) * open_h
    occ = np.clip(occ, 0, 100)
    people = occ * 64 / 100 / 3
    rows = zip(ts, [f"platform-{seg}"] * len(idx), occ.round(2), people.round(2),
               np.ceil(people * 1.3).astype(int).tolist(), [-1] * len(idx), ts)
    con.executemany("INSERT INTO readings (ts, device_id, occupancy_pct, people_mean, people_max, frames,"
                    " received_at) VALUES (?,?,?,?,?,?,?)", rows)
con.commit()
print(f"wrote {len(idx) * 4} SIMULATED rows ({a.days} days x 4 segments)")
