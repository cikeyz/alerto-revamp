"""
ALERTO forecaster: per-segment 30-minute crowd forecasts with ARIMA, ARIMAX and Prophet.

Runs as a service (density-forecast). Every 5 minutes it:
  1. builds a 5-minute occupancy series for each platform segment (A-D) from the
     sensor rows (each segment = the ESP32 assigned to it in Settings)
  2. updates every model with the newest data and forecasts the next 6 steps (30 min)
  3. writes outputs/forecast.json for the dashboard and the gate/OLED logic

Every 6 hours (and at start) it refits all models and re-scores them with a
rolling-origin backtest on the most recent day: from every half-hour origin, each
model forecasts 30 minutes ahead using only data before that origin. Scores:
  MAE, RMSE (percentage points), MAPE (on steps where the segment was >= 5% occupied)
  agreement (% of forecasts that land in the same Low/Moderate/High level as measured)
Two baselines are scored the same way: Persistence (last value) and Moving average (3 h).

  ARIMA   : recent trend only; (p,d,q) chosen by AIC, re-chosen weekly
  ARIMAX  : ARIMA errors + separate daily shapes for workdays vs weekends/PH holidays
  Prophet : workday/off-day daily seasonality, weekly seasonality, PH holidays

Usage:  python forecaster.py           (service loop)
        python forecaster.py --once    (single refit + forecast, for testing)
"""
import argparse
import itertools
import threading
import json
import logging
import os
import sqlite3
import time
import warnings
from datetime import datetime, timedelta

import holidays
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from statsmodels.tsa.statespace.sarimax import SARIMAX
from statsmodels.tsa.stattools import adfuller

import settings as cfg
import store

warnings.filterwarnings("ignore")
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)
logging.getLogger("prophet").setLevel(logging.WARNING)
log = logging.getLogger("forecaster")

OUT = store.OUT_DIR
STEP = pd.Timedelta(minutes=5)
H = 6                        # 6 x 5 min = 30-minute horizon
MIN_DAYS = 3                 # data needed before forecasting
ARIMA_DAYS = 7               # fit window for ARIMA / ARIMAX
PROPHET_DAYS = 21            # fit window for Prophet
TEST_DAYS = 1                # backtest window
REFIT_HOURS = 6
ORDER_DAYS = 7               # re-search ARIMA orders weekly
MODELS = ["ARIMA", "ARIMAX", "Prophet"]
BASELINES = ["Persistence", "Moving avg (3 h)"]


def write_json(name, obj):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f, indent=1, default=float)
    os.replace(path + ".tmp", path)


def read_json(name):
    try:
        with open(os.path.join(OUT, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ data
def load_segments(s, now):
    """{segment: 5-min pd.Series} of complete buckets, up to PROPHET_DAYS back."""
    end = now.floor("5min")                                   # last complete bucket ends here
    start = end - pd.Timedelta(days=PROPHET_DAYS)
    con = sqlite3.connect(store.DB_PATH, timeout=10)
    df = pd.read_sql_query(
        "SELECT ts, device_id, occupancy_pct FROM readings WHERE ts >= ? AND ts < ?",
        con, params=(start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
        parse_dates=["ts"])
    con.close()
    out = {}
    for seg, dev in s["segments"].items():
        if not dev:
            continue
        d = df[df.device_id == dev]
        if d.empty:
            continue
        y = d.set_index("ts")["occupancy_pct"].sort_index().resample("5min").mean()
        y = y[y.index < end]
        if y.notna().sum() < 12:
            continue
        y = y.interpolate(limit=3)
        prof = y.groupby([off_day(y.index), y.index.time]).transform("mean")
        y = y.fillna(prof).fillna(y.mean()).clip(0, 100)
        y.index.freq = "5min"
        y.name = seg
        out[seg] = y
    return out


def off_day(index):
    ph = holidays.PH(years=sorted(set(index.year)))
    return (index.dayofweek >= 5) | np.array([d in ph for d in index.date])


def calendar_exog(index, k=6):
    t = (index.hour * 3600 + index.minute * 60) / 86400.0
    off = off_day(index).astype(float)
    work = 1.0 - off
    cols = {"off_day": off}
    for i in range(1, k + 1):
        sn, cs = np.sin(2 * np.pi * i * t), np.cos(2 * np.pi * i * t)
        cols[f"w_s{i}"], cols[f"w_c{i}"], cols[f"o_s{i}"], cols[f"o_c{i}"] = sn * work, cs * work, sn * off, cs * off
    return pd.DataFrame(cols, index=index)


def varying(X):
    return [c for c in X.columns if X[c].std() > 1e-9]


# ------------------------------------------------------------------ models
def sarimax(y, X, order, params=None):
    m = SARIMAX(y, exog=X, order=order, trend="c" if order[1] == 0 else "n",
                enforce_stationarity=False, enforce_invertibility=False)
    return m.filter(params) if params is not None else m.fit(disp=False, maxiter=100)


def choose_d(y):
    try:
        return 0 if adfuller(y.values, autolag="AIC")[1] < 0.05 else 1
    except Exception:
        return 0


def search_order(y, X):
    base = y if X is None else y - y.groupby([off_day(y.index), y.index.time]).transform("mean")
    d = choose_d(base)
    best = None
    for p, q in itertools.product(range(3), range(3)):
        if p == q == 0:
            continue
        try:
            r = sarimax(y, X, (p, d, q))
            if best is None or r.aic < best[1]:
                best = ((p, d, q), r.aic)
        except Exception:
            continue
    return best[0] if best else (1, 0, 1)


def fit_prophet(y):
    from prophet import Prophet
    off = off_day(y.index)
    df = pd.DataFrame({"ds": y.index, "y": y.values, "workday": ~off, "offday": off})
    m = Prophet(daily_seasonality=False, weekly_seasonality=(len(y) * 5 / 1440 >= 14),
                yearly_seasonality=False, changepoint_prior_scale=0.05)
    m.add_seasonality("daily_workday", period=1, fourier_order=10, condition_name="workday")
    m.add_seasonality("daily_offday", period=1, fourier_order=6, condition_name="offday")
    m.add_country_holidays(country_name="PH")
    m.fit(df)
    return m


def prophet_predict(m, idx):
    off = off_day(idx)
    return np.clip(m.predict(pd.DataFrame({"ds": idx, "workday": ~off, "offday": off}))["yhat"].values, 0, 100)


def have_prophet():
    try:
        import prophet  # noqa: F401
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ backtest
def score(actual, pred, s):
    a, p = np.asarray(actual, float), np.asarray(pred, float)
    e = a - p
    mask = a >= 5
    lv = lambda v: np.where(v >= s["high_at"], 2, np.where(v >= s["moderate_at"], 1, 0))
    return {"MAE": float(np.mean(np.abs(e))),
            "RMSE": float(np.sqrt(np.mean(e ** 2))),
            "MAPE": float(np.mean(np.abs(e[mask]) / a[mask]) * 100) if mask.any() else None,
            "agreement": float(np.mean(lv(a) == lv(p)) * 100),
            "n": int(len(a))}


def backtest_segment(y, X, orders, s, use_prophet):
    """Rolling-origin 30-min forecasts over the last TEST_DAYS; params fitted without them."""
    n_test = int(pd.Timedelta(days=TEST_DAYS) / STEP)
    yt, Xt = y.iloc[:-n_test], (None if X is None else X.iloc[:-n_test])
    origins = list(range(len(y) - n_test, len(y) - H + 1, 6))          # every 30 min
    acts, preds = [], {k: [] for k in MODELS + BASELINES}
    fitted = {}
    cols = varying(Xt)
    fitted["ARIMA"] = sarimax(yt, None, orders["ARIMA"]).apply(y)
    fitted["ARIMAX"] = sarimax(yt, Xt[cols], orders["ARIMAX"]).apply(y, exog=X[cols])
    pm = fit_prophet(yt.iloc[-int(pd.Timedelta(days=PROPHET_DAYS) / STEP):]) if use_prophet else None
    p_all = prophet_predict(pm, y.index[-n_test:]) if pm else None
    for o in origins:
        actual = y.values[o:o + H]
        acts.extend(actual)
        for k in ("ARIMA", "ARIMAX"):
            pr = fitted[k].get_prediction(start=o, end=o + H - 1, dynamic=True).predicted_mean
            preds[k].extend(np.clip(np.asarray(pr), 0, 100))
        if pm is not None:
            j = o - (len(y) - n_test)
            preds["Prophet"].extend(p_all[j:j + H])
        preds["Persistence"].extend([y.values[o - 1]] * H)
        preds["Moving avg (3 h)"].extend([y.values[max(0, o - 36):o].mean()] * H)
    out = {k: score(acts, v, s) for k, v in preds.items() if len(v) == len(acts)}
    # example trace for the backtest chart (last test day, 30-min-ahead path at each origin)
    return out


# ------------------------------------------------------------------ main state
class Forecaster:
    def __init__(self):
        self.fitted_at = None
        self.models = {}           # seg -> {"ARIMA": (order, params, cols), "ARIMAX": ..., "Prophet": model}
        self.metrics = {}          # seg -> {model: scores}
        self.orders = read_json("model_orders.json") or {}
        self.thread = None

    def refit(self, segs, s):
        """Fits into new dicts and swaps them in at the end, so forecasts keep using
        the previous models while a (slow) refit runs in the background."""
        use_prophet = have_prophet()
        models, metrics = {}, {}
        for seg, y_full in segs.items():
            t0 = time.time()
            y = y_full.iloc[-int(pd.Timedelta(days=ARIMA_DAYS) / STEP):]
            X = calendar_exog(y.index)
            cols = varying(X)
            o = self.orders.get(seg)
            if not o or time.time() - o.get("saved", 0) > ORDER_DAYS * 86400:
                o = {"ARIMA": list(search_order(y, None)), "ARIMAX": list(search_order(y, X[cols])),
                     "saved": time.time()}
                self.orders[seg] = o
                write_json("model_orders.json", self.orders)
            orders = {"ARIMA": tuple(o["ARIMA"]), "ARIMAX": tuple(o["ARIMAX"])}
            days = (y.index[-1] - y.index[0]) / pd.Timedelta(days=1)
            if days >= TEST_DAYS + 2:
                try:
                    metrics[seg] = backtest_segment(y, X, orders, s, use_prophet)
                except Exception as e:
                    log.warning("backtest %s failed: %s", seg, e)
            entry = {}
            r = sarimax(y, None, orders["ARIMA"])
            entry["ARIMA"] = (orders["ARIMA"], r.params, None)
            r = sarimax(y, X[cols], orders["ARIMAX"])
            entry["ARIMAX"] = (orders["ARIMAX"], r.params, cols)
            if use_prophet:
                try:
                    entry["Prophet"] = fit_prophet(y_full)
                except Exception as e:
                    log.warning("prophet %s failed: %s", seg, e)
            models[seg] = entry
            log.info("refit %s in %.1fs (orders %s)", seg, time.time() - t0, orders)
        self.models, self.metrics, self.fitted_at = models, metrics, datetime.now()

    def forecast(self, segs):
        out = {}
        for seg, y_full in segs.items():
            if seg not in self.models:
                continue
            y = y_full.iloc[-int(pd.Timedelta(days=ARIMA_DAYS) / STEP):]
            fidx = pd.date_range(y.index[-1] + STEP, periods=H, freq="5min")
            res = {}
            for k in ("ARIMA", "ARIMAX"):
                order, params, cols = self.models[seg][k]
                try:
                    if cols is None:
                        r = sarimax(y, None, order, params)
                        f = r.forecast(H)
                    else:
                        X = calendar_exog(y.index)[cols]
                        r = sarimax(y, X, order, params)
                        f = r.forecast(H, exog=calendar_exog(fidx)[cols])
                    res[k] = np.clip(np.asarray(f), 0, 100)
                except Exception as e:
                    log.warning("%s %s forecast failed: %s", seg, k, e)
            if "Prophet" in self.models[seg]:
                res["Prophet"] = prophet_predict(self.models[seg]["Prophet"], fidx)
            out[seg] = {
                "history": [{"t": t.strftime("%Y-%m-%d %H:%M"), "v": round(float(v), 2)}
                            for t, v in y.iloc[-12:].items()],
                "models": {k: [{"t": t.strftime("%Y-%m-%d %H:%M"), "v": round(float(v), 2)}
                               for t, v in zip(fidx, vals)] for k, vals in res.items()},
            }
        return out


def summarise_metrics(metrics):
    """Average each model's scores over segments."""
    keys = {k for m in metrics.values() for k in m}
    out = {}
    for k in MODELS + BASELINES:
        if k not in keys:
            continue
        rows = [m[k] for m in metrics.values() if k in m]
        avg = lambda f: (float(np.mean([r[f] for r in rows if r[f] is not None]))
                         if any(r[f] is not None for r in rows) else None)
        out[k] = {f: (None if avg(f) is None else round(avg(f), 2)) for f in ("MAE", "RMSE", "MAPE", "agreement")}
    return out


def backtest_chart(segs, metrics):
    if not segs:
        return
    fig, ax = plt.subplots(figsize=(10, 3.2))
    for seg, y in segs.items():
        ax.plot(y.index[-288:], y.values[-288:], lw=1, label=f"Seg {seg}")
    ax.set_ylabel("% occupied")
    ax.set_title("Last 24 h by segment (backtest window)")
    ax.legend(ncol=4, fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "backtest.png"), dpi=110)
    plt.close(fig)


def retention(s):
    cutoff = (datetime.now() - timedelta(days=int(s["retention_days"]))).strftime("%Y-%m-%d %H:%M:%S")
    con = store.connect()
    with con:
        n = con.execute("DELETE FROM readings WHERE ts < ?", (cutoff,)).rowcount
    con.close()
    if n:
        log.info("retention: deleted %d old rows", n)


def run_once(fc, force_refit=False):
    s = cfg.load()
    now = pd.Timestamp.now()
    segs = load_segments(s, now)
    days = max(((y.index[-1] - y.index[0]) / pd.Timedelta(days=1) for y in segs.values()), default=0.0)
    status = {"updated": now.strftime("%Y-%m-%d %H:%M"), "data_days": round(days, 2),
              "needed_days": MIN_DAYS, "segments_with_data": sorted(segs)}
    if days < MIN_DAYS:
        write_json("status.json", {**status, "state": "collecting"})
        log.info("collecting: %.2f of %d days", days, MIN_DAYS)
        return
    # a segment joins the models once it has MIN_DAYS of data itself
    ready = {k: v for k, v in segs.items() if (v.index[-1] - v.index[0]) / pd.Timedelta(days=1) >= MIN_DAYS}
    stale = (fc.fitted_at is None or force_refit or
             datetime.now() - fc.fitted_at > timedelta(hours=REFIT_HOURS) or
             set(ready) != set(fc.models))
    if stale and not (fc.thread and fc.thread.is_alive()):
        job = lambda: (fc.refit(ready, s), backtest_chart(ready, fc.metrics))
        if fc.models and not force_refit:
            fc.thread = threading.Thread(target=job, daemon=True)     # keep forecasting meanwhile
            fc.thread.start()
        else:
            job()
    per_seg = fc.forecast(segs)
    summary = summarise_metrics(fc.metrics)
    ranked = [k for k in MODELS if k in summary]
    best = min(ranked, key=lambda k: summary[k]["MAE"]) if ranked else (MODELS[0] if per_seg else None)
    active = s["active_model"] if s["active_model"] != "auto" else best
    write_json("forecast.json", {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "fitted_at": fc.fitted_at.strftime("%Y-%m-%d %H:%M:%S") if fc.fitted_at else None,
        "step_min": 5, "horizon_steps": H,
        "best_model": best, "active_model": active,
        "orders": {seg: {k: fc.orders.get(seg, {}).get(k) for k in ("ARIMA", "ARIMAX")} for seg in per_seg},
        "metrics": summary, "metrics_by_segment": fc.metrics,
        "segments": per_seg,
    })
    write_json("status.json", {**status, "state": "ok"})
    retention(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    store.init_db()
    fc = Forecaster()
    if a.once:
        run_once(fc, force_refit=True)
        return
    while True:
        try:
            run_once(fc)
        except Exception as e:
            log.exception("forecast run failed")
            write_json("status.json", {"state": "error", "message": str(e)[:300],
                                       "updated": datetime.now().strftime("%Y-%m-%d %H:%M")})
        # next 5-minute boundary + 20 s (lets the minute rows land first)
        now = time.time()
        time.sleep(max(5, 300 - now % 300 + 20))


if __name__ == "__main__":
    main()
