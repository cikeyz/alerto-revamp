"""
ALERTO hub on the Raspberry Pi 5: sensor collector, crowd logic, gate/OLED feed,
admin dashboard (captive portal on the hub's Wi-Fi).

Sensor ESP32s (one per platform segment)
  POST /frame            raw 8x8 frame {"device_id","d":[64],"s":[64]}  (X-Device-Key)
  POST /ingest           pre-aggregated minute row (older firmware)      (X-Device-Key)

Output ESP32 (OLED + servo gate)
  GET  /api/display      gate open/closed + commuter message lines for the OLED

Dashboard
  GET  /                 dashboard page
  GET  /api/state        live segments, platform level, alerts, recommendation, gate
  GET  /api/forecast     30-min forecasts per segment for a model, scores, baselines
  GET  /api/history      daily peak per segment for a date range
  GET/POST /api/settings thresholds, preferences, sensor-to-segment mapping (POST needs PIN)
  POST /api/settings/reset
  POST /api/calibrate    relearn the empty floor for a sensor (needs PIN)
  GET  /api/live         latest raw frame per sensor (8x8 view)

Production: waitress-serve --listen=0.0.0.0:80 app:app   Dev: python app.py --port 8080
"""
import hmac
import json
import os
import socket
import textwrap
import threading
from datetime import date, datetime, timedelta

import holidays
from flask import Flask, Response, jsonify, redirect, request, send_from_directory

import settings as cfg
import store
from frames import FrameProcessor

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
PORTAL_IP = os.environ.get("PORTAL_IP", "10.42.0.1")
PORTAL_NAME = os.environ.get("PORTAL_NAME", "alerto.lan").lower()
DEVICE_KEY = os.environ.get("DEVICE_KEY", "")
ADMIN_PIN = os.environ.get("ADMIN_PIN", "")
TS = "%Y-%m-%d %H:%M:%S"
VERSION = "1.1.0"

_hn = socket.gethostname().lower()
ALLOWED_HOSTS = {PORTAL_IP, PORTAL_NAME, "localhost", "127.0.0.1", _hn, f"{_hn}.local"}
ALLOWED_HOSTS |= {h.strip().lower() for h in os.environ.get("EXTRA_HOSTS", "").split(",") if h.strip()}

app = Flask(__name__, static_folder=None)
store.init_db()
processor = FrameProcessor()
_gate = {"closed": False, "since": None}
_gate_lock = threading.Lock()
_cal_pending = set()


def read_json(name):
    try:
        with open(os.path.join(store.OUT_DIR, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def pin_ok(pin):
    return bool(ADMIN_PIN) and hmac.compare_digest(str(pin or ""), ADMIN_PIN)


# ------------------------------------------------------------------ captive portal
@app.before_request
def captive_portal():
    host = (request.host or "").split(":")[0].lower()
    if request.path in ("/ingest", "/frame", "/api/display") or host in ALLOWED_HOSTS:
        return None
    return redirect(f"http://{PORTAL_IP}/", code=302)


@app.after_request
def no_cache(resp):
    if not request.path.startswith("/fonts/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ sensors
def device_ok():
    return not DEVICE_KEY or hmac.compare_digest(request.headers.get("X-Device-Key", ""), DEVICE_KEY)


@app.post("/frame")
def frame():
    if not device_ok():
        return jsonify(error="bad device key"), 403
    j = request.get_json(force=True, silent=True) or {}
    try:
        dev = str(j.get("device_id", "unknown"))[:40]
        d = [int(v) for v in j["d"]]
        s = [int(v) for v in j["s"]]
        if len(d) != 64 or len(s) != 64:
            raise ValueError("need 64 distances and 64 statuses")
    except (KeyError, ValueError, TypeError) as e:
        return jsonify(error=f"bad frame: {e}"), 400
    processor.add(dev, d, s)
    return jsonify(ok=True)


@app.post("/ingest")
def ingest():
    if not device_ok():
        return jsonify(error="bad device key"), 403
    d = request.get_json(force=True, silent=True) or {}
    now = datetime.now()
    try:
        age = min(max(int(d.get("age_s", 0)), 0), 7 * 86400)
        occ = float(d["occupancy_pct"])
        if not 0 <= occ <= 100:
            raise ValueError("occupancy_pct out of range")
        dev = str(d.get("device_id", "unknown"))[:40]
        row = ((now - timedelta(seconds=age)).strftime(TS), dev, occ, float(d.get("people_mean", 0)),
               int(d.get("people_max", 0)), int(d.get("frames", 0)), now.strftime(TS))
    except (KeyError, ValueError, TypeError) as e:
        return jsonify(error=f"bad payload: {e}"), 400
    con = store.connect()
    with con:
        con.execute("INSERT INTO readings (ts, device_id, occupancy_pct, people_mean, people_max, frames,"
                    " received_at) VALUES (?,?,?,?,?,?,?)", row)
    con.close()
    cal = dev in _cal_pending
    _cal_pending.discard(dev)
    return Response(json.dumps({"ok": True, "calibrate": cal}, separators=(",", ":")), mimetype="application/json")


@app.get("/api/live")
def live():
    return jsonify(processor.live())


# ------------------------------------------------------------------ crowd state
def segment_states(s, now):
    live = processor.live()
    con = store.connect()
    recent = (now - timedelta(minutes=3)).strftime(TS)
    out = []
    for seg in cfg.SEGMENT_IDS:
        dev = s["segments"].get(seg, "")
        st = {"id": seg, "device": dev, "occupancy_pct": None, "people": None, "level": "unknown",
              "online": False, "calibrating": False, "last_seen_s": None, "source": None}
        if dev:
            f = live.get(dev)
            age = (now - datetime.strptime(f["t"], TS)).total_seconds() if f else None
            if f and age <= 15:
                st.update(online=True, last_seen_s=int(age), source="live", calibrating=f.get("calibrating", False))
                if not f.get("calibrating"):
                    st.update(occupancy_pct=f["occupancy_pct"], people=f["people"])
            else:
                r = con.execute("SELECT AVG(occupancy_pct), AVG(people_mean), MAX(received_at) FROM readings "
                                "WHERE device_id = ? AND ts >= ?", (dev, recent)).fetchone()
                last = con.execute("SELECT MAX(received_at) FROM readings WHERE device_id = ?",
                                   (dev,)).fetchone()[0]
                if last:
                    st["last_seen_s"] = int((now - datetime.strptime(last, TS)).total_seconds())
                if age is not None:
                    st["last_seen_s"] = int(age) if st["last_seen_s"] is None else min(st["last_seen_s"], int(age))
                if r and r[0] is not None:
                    st.update(occupancy_pct=round(r[0], 1), people=round(r[1] or 0, 1), online=True, source="minute")
        st["level"] = cfg.level(st["occupancy_pct"], s)
        out.append(st)
    con.close()
    return out


def forecast_view(fc, model):
    """{seg: {"next5": v, "points": [(t, v)]}} for one model."""
    out = {}
    for seg, d in (fc or {}).get("segments", {}).items():
        pts = d["models"].get(model)
        if pts:
            out[seg] = {"next5": pts[0]["v"], "points": pts}
    return out


def recommend(segs, fv):
    cand = [g for g in segs if g["occupancy_pct"] is not None]
    if not cand:
        return None
    key = lambda g: fv.get(g["id"], {}).get("next5", g["occupancy_pct"])
    best = min(cand, key=key)
    return {"id": best["id"], "now": best["occupancy_pct"], "next5": round(key(best), 1),
            "basis": "forecast" if best["id"] in fv else "current"}


def update_gate(segs, s):
    with _gate_lock:
        have = [g for g in segs if g["occupancy_pct"] is not None]
        if not s["gate_enabled"] or not have:
            _gate.update(closed=False, since=None)
        elif not _gate["closed"] and all(g["occupancy_pct"] >= s["high_at"] for g in have):
            _gate.update(closed=True, since=datetime.now().strftime(TS))
        elif _gate["closed"] and any(g["occupancy_pct"] < s["high_at"] - 5 for g in have):
            _gate.update(closed=False, since=datetime.now().strftime(TS))   # 5-point hysteresis
        return dict(_gate)


def messages(segs, rec, gate):
    full = [g["id"] for g in segs if g["level"] == "high"]
    if gate["closed"]:
        return {"fil": "Puno ang buong plataporma. Maghintay muna bago pumasok.",
                "en": "The whole platform is full. Please wait before entering.", "kind": "gate"}
    if full and rec and rec["id"] not in full:
        names = [f"Zone {z}" for z in full]
        join = lambda word: names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" {word} " + names[-1]
        zones, zones_en = join("at"), join("and")
        return {"fil": f"Puno na ang {zones}. Pumunta sa Zone {rec['id']}.",
                "en": f"{zones_en} {'is' if len(full) == 1 else 'are'} full. Proceed to Zone {rec['id']}.",
                "kind": "redirect", "to": rec["id"]}
    if rec:
        return {"fil": f"Mas maluwag ang Zone {rec['id']} ngayon.",
                "en": f"Zone {rec['id']} is less crowded now.", "kind": "info", "to": rec["id"]}
    return {"fil": "Walang datos mula sa mga sensor.", "en": "No sensor data yet.", "kind": "none"}


def build_state():
    s = cfg.load()
    now = datetime.now()
    segs = segment_states(s, now)
    fc = read_json("forecast.json")
    model = (fc or {}).get("active_model")
    fv = forecast_view(fc, model) if model else {}
    have = [g["occupancy_pct"] for g in segs if g["occupancy_pct"] is not None]
    platform = round(sum(have) / len(have), 1) if have else None
    rec = recommend(segs, fv)
    gate = update_gate(segs, s)
    alerts = []
    for g in segs:
        if g["level"] == "high":
            alerts.append({"kind": "high", "seg": g["id"],
                           "title": f"Seg {g['id']} is at {round(g['occupancy_pct'])}% (High)",
                           "detail": f"Direct commuters to Zone {rec['id']}." if rec and rec["id"] != g["id"] else "Monitor entry gates closely."})
        elif g["device"] and not g["online"] and g["last_seen_s"] is not None:
            alerts.append({"kind": "offline", "seg": g["id"], "title": f"Seg {g['id']} sensor is not sending data",
                           "detail": f"Last data {g['last_seen_s'] // 60} min ago. Check its power and Wi-Fi."})
    lead = int(s["alert_lead_min"])
    for seg, d in fv.items():
        g = next((x for x in segs if x["id"] == seg), None)
        if not g or g["level"] == "high":
            continue
        for i, p in enumerate(d["points"]):
            mins = (i + 1) * 5
            if mins > lead:
                break
            if p["v"] >= s["high_at"]:
                alerts.append({"kind": "forecast", "seg": seg,
                               "title": f"Seg {seg} forecast to exceed {round(s['high_at'])}% in {mins} minutes",
                               "detail": (f"Prepare to direct commuters to Zone {rec['id']}. " if rec and rec["id"] != seg else "")
                                         + "Monitor entry gates closely."})
                break
    if gate["closed"]:
        alerts.insert(0, {"kind": "gate", "seg": None, "title": "Entry gate closed: every segment is High",
                          "detail": "It reopens when any segment drops 5 points below the High threshold."})
    return {
        "now": now.strftime(TS), "clock_ok": now.year >= 2025, "version": VERSION,
        "station": s["station_name"], "line": s["line_name"],
        "settings": {k: s[k] for k in ("moderate_at", "high_at", "alert_lead_min", "refresh_s", "language")},
        "segments": segs, "platform": {"occupancy_pct": platform, "level": cfg.level(platform, s)},
        "recommendation": rec, "message": messages(segs, rec, gate), "gate": gate, "alerts": alerts,
        "forecast": None if not fc else {
            "model": model, "generated_at": fc.get("generated_at"),
            "segments": {seg: {"history": fc["segments"][seg]["history"][-3:], "points": d["points"]}
                         for seg, d in fv.items()}},
        "forecast_status": read_json("status.json") or {},
        "admin_enabled": bool(ADMIN_PIN),
    }


@app.get("/api/state")
def api_state():
    return jsonify(build_state())


@app.get("/api/display")
def api_display():
    """Compact feed for the output ESP32: 128x64 OLED (21 chars x 6 lines) + servo gate."""
    st = build_state()
    lang = st["settings"]["language"]
    msg = st["message"][lang]
    rec = st["recommendation"]
    title = ("PLATFORM FULL" if st["gate"]["closed"] else
             f"GO TO ZONE {rec['id']}" if rec and st["message"]["kind"] == "redirect" else
             f"ZONE {rec['id']} {'MALUWAG' if lang == 'fil' else 'LESS FULL'}" if rec else "ALERTO")
    levels = "".join(f"{g['id']}:{ {'low': 'L', 'moderate': 'M', 'high': 'H'}.get(g['level'], '-') } "
                     for g in st["segments"]).strip()
    return jsonify({"gate": "closed" if st["gate"]["closed"] else "open",
                    "title": title[:21], "lines": textwrap.wrap(msg, 21)[:4], "levels": levels[:21],
                    "lang": lang, "zone": rec["id"] if rec else None})


# ------------------------------------------------------------------ forecast page
def calendar_context(now):
    ph = holidays.PH(years=[now.year, now.year + 1])
    today = now.date()
    hol = ph.get(today)
    nxt = next(((d, n) for d, n in sorted(ph.items()) if d > today), None)
    off = today.weekday() >= 5 or hol is not None
    return {"day_type": "Holiday" if hol else ("Weekend" if today.weekday() >= 5 else "Workday"),
            "weekday": today.strftime("%A"), "holiday": hol, "off_day": off,
            "next_holiday": None if not nxt else {"date": nxt[0].isoformat(), "name": nxt[1]}}


@app.get("/api/forecast")
def api_forecast():
    fc = read_json("forecast.json")
    now = datetime.now()
    status = read_json("status.json") or {}
    if not fc:
        return jsonify({"ready": False, "status": status, "calendar": calendar_context(now)})
    model = request.args.get("model") or fc.get("active_model")
    segs = {}
    for seg, d in fc["segments"].items():
        segs[seg] = {"history": d["history"], "points": d["models"].get(model, []),
                     "all": {m: v for m, v in d["models"].items()}}
    s = cfg.load()
    nxt = {seg: v["points"][0]["v"] for seg, v in segs.items() if v["points"]}
    least = min(nxt, key=nxt.get) if nxt else None
    return jsonify({
        "ready": True, "status": status, "model": model, "best_model": fc.get("best_model"),
        "active_model": fc.get("active_model"), "auto": s["active_model"] == "auto",
        "generated_at": fc.get("generated_at"), "fitted_at": fc.get("fitted_at"),
        "orders": fc.get("orders"), "metrics": fc.get("metrics", {}), "segments": segs,
        "least_crowded": None if not least else {"id": least, "v": round(nxt[least], 1),
                                                 "level": cfg.level(nxt[least], s)},
        "calendar": calendar_context(now), "thresholds": {"moderate": s["moderate_at"], "high": s["high_at"]},
    })


# ------------------------------------------------------------------ history page
@app.get("/api/history")
def api_history():
    s = cfg.load()
    try:
        end = date.fromisoformat(request.args.get("end") or date.today().isoformat())
        start = date.fromisoformat(request.args.get("start") or (end - timedelta(days=6)).isoformat())
    except ValueError:
        return jsonify(error="dates must be YYYY-MM-DD"), 400
    if start > end:
        start, end = end, start
    start = max(start, end - timedelta(days=92))
    dev2seg = {v: k for k, v in s["segments"].items() if v}
    if not dev2seg:
        return jsonify(days=[], segments=[])
    q = ("SELECT device_id, strftime('%Y-%m-%d %H:', ts) || printf('%02d', (CAST(strftime('%M', ts) AS INTEGER) / 5) * 5) AS b,"
         " AVG(occupancy_pct) FROM readings WHERE ts >= ? AND ts < ? AND device_id IN ("
         + ",".join("?" * len(dev2seg)) + ") GROUP BY device_id, b")
    con = store.connect()
    rows = con.execute(q, [start.isoformat(), (end + timedelta(days=1)).isoformat(), *dev2seg]).fetchall()
    con.close()
    days = {}
    for dev, b, v in rows:
        day, hm = b[:10], b[11:16]
        seg = dev2seg[dev]
        cell = days.setdefault(day, {}).setdefault(seg, {"peak": -1, "t": None, "sum": 0.0, "n": 0})
        if v > cell["peak"]:
            cell["peak"], cell["t"] = v, hm
        cell["sum"] += v
        cell["n"] += 1
    out = []
    for day in sorted(days):
        segs = {seg: {"peak": round(c["peak"], 1), "t": c["t"], "mean": round(c["sum"] / c["n"], 1),
                      "level": cfg.level(c["peak"], s)} for seg, c in days[day].items()}
        top = max(segs, key=lambda k: segs[k]["peak"])
        out.append({"date": day, "segments": segs,
                    "overall": {"seg": top, "peak": segs[top]["peak"], "t": segs[top]["t"], "level": segs[top]["level"]}})
    fc = read_json("forecast.json") or {}
    return jsonify({"start": start.isoformat(), "end": end.isoformat(), "days": out,
                    "segments": [k for k in cfg.SEGMENT_IDS if s["segments"].get(k)],
                    "thresholds": {"moderate": s["moderate_at"], "high": s["high_at"]},
                    "metrics": fc.get("metrics", {}), "best_model": fc.get("best_model"),
                    "fitted_at": fc.get("fitted_at")})


# ------------------------------------------------------------------ settings
@app.get("/api/settings")
def api_settings():
    s = cfg.load()
    con = store.connect()
    known = [r[0] for r in con.execute("SELECT DISTINCT device_id FROM readings WHERE frames >= 0")]
    con.close()
    devices = sorted(set(known) | set(processor.live().keys()) | {v for v in s["segments"].values() if v})
    return jsonify({"settings": s, "devices": devices, "defaults": cfg.DEFAULTS, "admin_enabled": bool(ADMIN_PIN)})


@app.post("/api/settings")
def api_settings_save():
    j = request.get_json(force=True, silent=True) or {}
    if not ADMIN_PIN:
        return jsonify(error="Set ADMIN_PIN in /etc/commuter-density.env to allow changes."), 403
    if not pin_ok(j.get("pin")):
        return jsonify(error="Wrong PIN."), 403
    try:
        saved = cfg.save(j.get("settings", {}))
    except (ValueError, TypeError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, settings=saved)


@app.post("/api/settings/reset")
def api_settings_reset():
    j = request.get_json(force=True, silent=True) or {}
    if not pin_ok(j.get("pin")):
        return jsonify(error="Wrong PIN." if ADMIN_PIN else "Set ADMIN_PIN first."), 403
    return jsonify(ok=True, settings=cfg.reset())


@app.post("/api/calibrate")
def api_calibrate():
    j = request.get_json(force=True, silent=True) or {}
    if not pin_ok(j.get("pin")):
        return jsonify(error="Wrong PIN." if ADMIN_PIN else "Set ADMIN_PIN first."), 403
    s = cfg.load()
    seg = str(j.get("segment", "all"))
    devs = [d for k, d in s["segments"].items() if d and (seg == "all" or k == seg)]
    processor.recalibrate(devs)
    _cal_pending.update(devs)
    return jsonify(ok=True, devices=devs,
                   message="Recalibrating. Keep the area under the sensor clear for about 30 seconds.")


# ------------------------------------------------------------------ pages
@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/fonts/<path:name>")
def fonts(name):
    resp = send_from_directory(os.path.join(STATIC, "fonts"), name)
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


@app.get("/backtest.png")
def backtest_png():
    return send_from_directory(store.OUT_DIR, "backtest.png")


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"):
        return jsonify(error="not found"), 404
    return redirect("/", code=302)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    app.run(host="0.0.0.0", port=ap.parse_args().port, threaded=True)
