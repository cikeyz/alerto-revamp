"""SQLite storage shared by app.py, forecast.py and simulate_data.py."""
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DENSITY_DB", os.path.join(BASE, "data", "density.db"))
OUT_DIR = os.environ.get("DENSITY_OUT", os.path.join(BASE, "outputs"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id            INTEGER PRIMARY KEY,
    ts            TEXT    NOT NULL,   -- local time 'YYYY-MM-DD HH:MM:SS' the row describes
    device_id     TEXT    NOT NULL,
    occupancy_pct REAL    NOT NULL,
    people_mean   REAL,
    people_max    INTEGER,
    frames        INTEGER,
    received_at   TEXT    NOT NULL    -- local time the Pi received it
);
CREATE INDEX IF NOT EXISTS idx_readings_ts     ON readings(ts);
CREATE INDEX IF NOT EXISTS idx_readings_dev_ts ON readings(device_id, ts);
CREATE INDEX IF NOT EXISTS idx_readings_recv   ON readings(received_at);
"""


def connect(path=None):
    path = path or DB_PATH
    con = sqlite3.connect(path, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")      # readers (forecast) never block the collector
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_db(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = connect(path)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
