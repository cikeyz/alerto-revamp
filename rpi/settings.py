"""Hub settings, editable from the dashboard Settings tab. Stored in data/settings.json."""
import json
import os
import threading

import store

PATH = os.path.join(os.path.dirname(store.DB_PATH), "settings.json")
SEGMENT_IDS = ["A", "B", "C", "D"]

DEFAULTS = {
    "station_name": os.environ.get("STATION_NAME", "Central Station"),
    "line_name": os.environ.get("LINE_NAME", "Line 1"),
    # crowd levels, as % of the floor under a sensor that is occupied
    "moderate_at": float(os.environ.get("MODERATE_AT", 40)),
    "high_at": float(os.environ.get("CROWDED_AT", 75)),
    "alert_lead_min": 15,          # warn when a segment is forecast to reach High within this time
    "refresh_s": 10,               # dashboard refresh interval
    "retention_days": 90,          # older readings are deleted
    "language": "fil",             # commuter message language: fil | en
    "active_model": "auto",        # auto | ARIMA | ARIMAX | Prophet
    "gate_enabled": True,          # close the entry gate when every segment is High
    # which ESP32 (NODE_ID) watches which platform segment
    "segments": {s: f"platform-{s}" for s in SEGMENT_IDS},
}

_lock = threading.Lock()


def load():
    with _lock:
        s = json.loads(json.dumps(DEFAULTS))
        try:
            with open(PATH) as f:
                saved = json.load(f)
            for k, v in saved.items():
                if k in s:
                    s[k] = v
        except (OSError, ValueError):
            pass
        return s


def validate(new):
    cur = load()
    out = dict(cur)
    def num(key, lo, hi, cast=float):
        if key in new:
            v = cast(new[key])
            if not lo <= v <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            out[key] = v
    num("moderate_at", 1, 99)
    num("high_at", 2, 100)
    num("alert_lead_min", 5, 30, int)
    num("refresh_s", 2, 120, int)
    num("retention_days", 7, 3650, int)
    if out["moderate_at"] >= out["high_at"]:
        raise ValueError("Moderate must start below High")
    for key in ("station_name", "line_name"):
        if key in new:
            out[key] = str(new[key]).strip()[:60]
    if "language" in new:
        if new["language"] not in ("fil", "en"):
            raise ValueError("language must be fil or en")
        out["language"] = new["language"]
    if "active_model" in new:
        if new["active_model"] not in ("auto", "ARIMA", "ARIMAX", "Prophet"):
            raise ValueError("unknown model")
        out["active_model"] = new["active_model"]
    if "gate_enabled" in new:
        out["gate_enabled"] = bool(new["gate_enabled"])
    if "segments" in new:
        seg = {}
        for s in SEGMENT_IDS:
            v = str(new["segments"].get(s, "")).strip()[:40]
            seg[s] = v
        out["segments"] = seg
    return out


def save(new):
    out = validate(new)
    with _lock:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(out, f, indent=1)
        os.replace(tmp, PATH)
    return out


def reset():
    with _lock:
        try:
            os.remove(PATH)
        except OSError:
            pass
    return load()


def level(v, s=None):
    s = s or load()
    if v is None:
        return "unknown"
    return "high" if v >= s["high_at"] else "moderate" if v >= s["moderate_at"] else "low"
