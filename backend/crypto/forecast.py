"""₿📈 The candle model on BTC (shadow by default; never gates an alert on its own, never acts):

  signal read   for EVERY pattern signal, on a background thread after the alert went: the model's read with the touch
                levels = the stop and 1R -> fc_json {p_down, p_up, p, dir, exp_px, exp_range, touch_stop, touch_r1,
                horizon_min, paths {median, p25, p75, p10, p90}, last, at}. Scored by code in outcomes.score_due (right).
                When the alert's PNG was made without the cone (the read was not ready), it is re-rendered once with it.
  candidates    on each closed candle, in the service thread AFTER the pattern step and its alerts: one read (touch levels
                = the pivots near the close); a row kind 'forecast' when the odds one way ≥ forecast_p, the expected move
                ≥ forecast_move_atr × ATR, the close within NEAR_ATR × ATR of a pivot, the EMA side agrees, inside the alert
                hours and no forecast on that level within the cooldown. State 'quiet' (shadow) unless forecast_alerts
                is send AND the forecast gate passes (outcomes.forecast_card), then 'sent' through the same alert path.
A worker that is down, slow or wrong gives None: nothing is stored, nothing else changes. Alerts only."""

import threading
from datetime import datetime
from typing import Optional

from backend.config import IST
from backend.crypto import pivots as pv
from backend.logging_config.logger import setup_logger

logger = setup_logger("crypto")

HORIZON_MIN = 30
NEAR_ATR = 0.6
CONTEXT = 120


def horizon_steps(tf: str) -> int:
    return max(1, HORIZON_MIN * 60 // pv.TF_SECONDS.get(tf, 300))


def atr_of(candles: list[dict]) -> Optional[float]:
    from backend.market import chart
    return chart.atr([{"h": b["h"], "l": b["l"], "c": b["c"]} for b in candles[-60:]])


def call(config, symbol: str, tf: str, candles: list[dict], levels: list[float]) -> Optional[dict]:
    """One worker read on the last CONTEXT candles: {p_up, exp_range, paths_summary, touch, last} or None."""
    from backend.market import reads as rd
    if not rd.on(config) or tf not in ("1m", "5m") or len(candles) < 30:
        return None
    h = horizon_steps(tf)
    payload = {"symbol": symbol, "interval": tf, "horizon": h, "horizons": [h], "samples": int(config.market.samples),
               "candles": [{"at": datetime.fromtimestamp(b["t"], IST).strftime("%Y-%m-%d %H:%M"), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
                            "v": b.get("v") or 0} for b in candles[-CONTEXT:]],
               "levels": [float(x) for x in levels]}
    res = rd.call_worker(config, payload)
    if not res:
        return None
    one = (res.get("reads") or [res])[-1]
    return {**one, "p_up": one.get("p_up", res.get("p_up")), "ms": res.get("ms"), "last": candles[-1]["c"]}


def fc_of(one: dict, tf: str, stop: Optional[float] = None, r1: Optional[float] = None) -> dict:
    """The stored read (fc_json) from a worker read."""
    from backend.market import paths as pth
    p_up = float(one["p_up"])
    ps = one.get("paths_summary") or {}
    paths = {"median": ps.get("q50"), "p25": ps.get("q25"), "p75": ps.get("q75"), "p10": ps.get("q10"), "p90": ps.get("q90")}
    t = one.get("touch") or {}
    med = ps.get("q50") or []
    return {"p_up": round(p_up, 3), "p_down": round(1 - p_up, 3), "p": round(max(p_up, 1 - p_up), 3), "dir": "long" if p_up > 0.5 else "short",
            "exp_px": med[-1] if med else None, "exp_range": one.get("exp_range"),
            "touch_stop": t.get(pth.level_key(stop)) if stop is not None else None, "touch_r1": t.get(pth.level_key(r1)) if r1 is not None else None,
            "horizon_min": horizon_steps(tf) * pv.TF_SECONDS.get(tf, 300) // 60, "paths": paths, "last": one.get("last"), "ms": one.get("ms"),
            "at": datetime.now(IST).isoformat(timespec="seconds"), "show": "shadow"}


# --- the read stored with every pattern signal -------------------------------------------------------------------------------

def signal_read(config, sid: int, candles: list[dict]) -> Optional[dict]:
    """The model's read for one stored signal (touch levels = its stop and 1R): fc_json (+ ai_json.model for the AI
    block), the grade's model step, the cone re-render. None when off / down."""
    from backend.crypto import grade as gd
    from backend.crypto import outcomes
    from backend.crypto import signals as sg
    r = sg.load(sid)
    if not r or r.get("fc_json"):
        return None
    cs = [b for b in candles if b["t"] <= r["t"]]
    one = call(config, r["symbol"], r["tf"], cs, [float(r["stop"]), float(r["r1"])])
    if not one or one.get("p_up") is None:
        return None
    fc = fc_of(one, r["tf"], r["stop"], r["r1"])
    gj = r.get("grade_json")
    cols = {"fc_json": fc}
    if isinstance(gj, dict):
        step = gd.model_step(fc)
        gj = {**gj, "model_step": step}
        if step and not gj.get("few") and gj.get("g"):
            if outcomes.fc_gate_ok(config):
                gj["g_model"] = gd.shift(gj["g"], step)
                cols["grade"] = gj["g_model"]
                gj["model_applied"] = True
            gj["why"] = (gj.get("why") or "") + (f" · model {step:+d}" if gj.get("model_applied") else f" · shadow {step:+d}")
        cols["grade_json"] = gj
    with sg._LOCK:
        cur = sg.load(sid) or {}
        aj = {**(cur.get("ai_json") or {}), "model": {"p_up": fc["p_up"], "stop_first": fc["touch_stop"], "r1_first": fc["touch_r1"],
                                                       "ms": fc["ms"], "at": fc["at"], "show": "shadow"}}
        sg.update(sid, ai_json=aj, **cols)
    r = sg.load(sid)
    if config.crypto.forecast_cone and r.get("png_path") and config.crypto.snapshot:
        try:
            from backend.crypto import snapshot
            snapshot.save(config, r, candles)                # the same file name: the cone lands on the card's PNG
            fc["cone_png"] = True
            sg.update(sid, fc_json=fc)
        except Exception:
            logger.exception("₿ cone re-render failed")
    return fc


_Q_LOCK = threading.Lock()


def read_async(config, sids: list[int], candles: list[dict]) -> Optional[threading.Thread]:
    """The signal reads on one background thread (the alert path never waits on it)."""
    from backend.market import reads as rd
    if not sids or not rd.on(config):
        return None
    cs = list(candles)

    def run():
        with _Q_LOCK:
            for sid in sids:
                try:
                    signal_read(config, sid, cs)
                except Exception:
                    logger.exception("₿ candle model read failed")
    th = threading.Thread(target=run, name="crypto-model", daemon=True)
    th.start()
    THREADS[:] = [x for x in THREADS if x.is_alive()] + [th]
    return th


THREADS: list = []                                     # the running signal-read threads (the tests drain them)


def drain(timeout: float = 15.0) -> None:
    """Wait for the signal reads started so far (tests, shutdown)."""
    for th in list(THREADS):
        th.join(timeout)


# --- the forecast candidates ----------------------------------------------------------------------------------------------

def _pivot_near(levels: dict, close: float, atr: float) -> Optional[tuple]:
    if not levels:
        return None
    name, v = min(levels.items(), key=lambda kv: abs(kv[1] - close))
    return (name, v) if abs(v - close) <= NEAR_ATR * atr else None


def stop_for(direction: str, close: float, pivot: float, atr: float, pad: float) -> float:
    """Beyond the pivot (+ stop_pad) or 1 × ATR from the close, whichever is nearer (a pivot on the wrong side: ATR)."""
    if direction == "short":
        cands = [close + atr] + ([pivot + pad] if pivot + pad > close else [])
        return round(min(cands), 2)
    cands = [close - atr] + ([pivot - pad] if pivot - pad < close else [])
    return round(max(cands), 2)


def evaluate(config, cs: list[dict], lv: dict, read: dict, now: datetime) -> dict:
    """Pure: the candidate's checks {ok, why, dir, pivot, stop, r1, ...} from a worker read on the candle cs[-1]."""
    from backend.crypto import patterns as pt
    from backend.crypto import signals as sg
    c = config.crypto
    close = cs[-1]["c"]
    atr = atr_of(cs)
    if not atr:
        return {"ok": False, "why": "ATR not ready"}
    p_up = float(read["p_up"])
    d = "long" if p_up > 0.5 else "short"
    p = max(p_up, 1 - p_up)
    if abs(p_up - 0.5) < (int(c.forecast_p) - 50) / 100:
        return {"ok": False, "why": f"odds {round(100 * p)} % < {int(c.forecast_p)} %"}
    rng = float(read.get("exp_range") or 0)
    if rng < float(c.forecast_move_atr) * atr:
        return {"ok": False, "why": f"move ±{sg.px(rng)} < {float(c.forecast_move_atr):g} × ATR {sg.px(atr)}"}
    near = _pivot_near(lv, close, atr)
    if not near:
        return {"ok": False, "why": "no pivot within 0.6 × ATR"}
    e = pt.ema([b["c"] for b in cs], int(c.ema_len))[-1]
    if e is None or (close > e) != (d == "long"):
        return {"ok": False, "why": f"EMA {int(c.ema_len)} side disagrees"}
    if not sg.in_hours(config, now):
        return {"ok": False, "why": "outside the alert hours"}
    stop = stop_for(d, close, near[1], atr, float(c.stop_pad))
    r1 = round(close + (close - stop), 2)
    return {"ok": True, "why": None, "dir": d, "p": round(p, 3), "pivot": near, "stop": stop, "r1": r1, "entry": close, "atr": atr, "ema": e}


def _cooldown(config, t: int, level: str, d: str) -> bool:
    from backend.crypto import signals as sg
    c = config.crypto
    since = t - max(1, int(c.cooldown_candles)) * pv.TF_SECONDS[c.tf]
    return bool(sg.rows("symbol = ? AND tf = ? AND kind = 'forecast' AND level_name = ? AND dir = ? AND t > ? AND t <= ?",
                        (c.symbol, c.tf, level, d, since, t), limit=1))


def step(config, t: int, cs: list[dict], now_ts: float) -> Optional[int]:
    """The forecast candidate for the closed candle at `t` (service thread, after the pattern step). The new row id or None."""
    from backend.crypto import grade as gd
    from backend.crypto import outcomes
    from backend.crypto import signals as sg
    from backend.market import reads as rd
    c = config.crypto
    if not rd.on(config) or c.tf not in ("1m", "5m"):
        return None
    cs = [b for b in cs if b["t"] <= t]
    if not cs or cs[-1]["t"] != t:
        return None
    if sg.rows("symbol = ? AND tf = ? AND t = ? AND kind = 'forecast'", (c.symbol, c.tf, t), limit=1):
        return None
    lvs = sg.levels_for(config, t)
    if not lvs:
        return None
    levels = lvs["levels"]
    close = cs[-1]["c"]
    near = sorted(levels.values(), key=lambda v: abs(v - close))[:4]
    one = call(config, c.symbol, c.tf, cs, near)
    if not one or one.get("p_up") is None:
        return None
    now = datetime.fromtimestamp(now_ts, IST)
    ev = evaluate(config, cs, levels, one, now)
    if not ev["ok"]:
        return None
    name, pivot = ev["pivot"]
    if _cooldown(config, t, name, ev["dir"]):
        return None
    two = call(config, c.symbol, c.tf, cs, [ev["stop"], ev["r1"]]) or {}
    fc = fc_of({**one, "touch": two.get("touch") or {}}, c.tf, ev["stop"], ev["r1"])
    word = "up" if ev["dir"] == "long" else "down"
    det = {"pattern": f"forecast {round(100 * ev['p'])} % {word}", "kind": "forecast", "dir": ev["dir"], "t": t, "t0": t, "candles": [cs[-1]],
           "level_name": name, "level_px": pivot, "entry": ev["entry"], "stop": ev["stop"], "r1": ev["r1"], "capped": False,
           "ema": round(ev["ema"], 2), "ema_len": int(c.ema_len), "ema_ok": True, "vol_x": None, "vol_ok": None, "trend_ok": None, "day_ok": True,
           "facts": {"p": ev["p"], "atr": ev["atr"], "exp_range": one.get("exp_range")}}
    gate = outcomes.forecast_card(config)
    send = c.forecast_alerts == "send" and gate["ok"]
    why = None if send else ("shadow · not sent" if c.forecast_alerts == "shadow" else f"shadow · {gate['why']}")
    gj = gd.grade_for(config, det, now_ts, levels=levels, atr=ev["atr"], fc=fc)
    sid = sg.insert(config, det, "sent" if send else "quiet", why, lvs, kind="forecast", fc=fc, grade=gj)
    if send:
        sg.fire(config, sid, now_ts, cs)
    return sid
