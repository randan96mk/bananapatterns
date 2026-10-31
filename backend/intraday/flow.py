"""The flow engine: a template (nodes + edges, positions for the canvas) of registered modules, run at each candle
close on the live 1-minute candles (plus saved history for warm-up), every box's state kept for the screen and
every run and signal saved. Node settings are saved in app_settings (flow.nodes) over the module defaults."""

import json
import threading
import time as _time
from collections import Counter
from datetime import datetime, timedelta
from typing import Optional

from backend.config import IST, AppConfig
from backend.database.models import get_candles, get_db, get_setting, now_ist, set_setting
from backend.intraday import store
from backend.intraday.candles import resample
from backend.intraday.modules import REGISTRY
from backend.logging_config.logger import setup_logger

logger = setup_logger("flow")

# node: (id, module kind, inputs, (x, y) on the canvas)
TEMPLATES = {
    "orb_vwap_rvol": {
        "name": "ORB 15m + VWAP + RVOL",
        "nodes": [("trigger", "trigger", [], (20, 100)), ("universe", "universe", ["trigger"], (285, 100)),
                  ("trend", "daily_trend", ["universe"], (550, 40)), ("filters", "intraday_filters", ["universe"], (550, 220)),
                  ("and", "and", ["trend", "filters"], (815, 130)), ("orb", "orb", ["and"], (20, 400)),
                  ("dedup", "dedup", ["orb"], (285, 400)), ("alert", "alert", ["dedup"], (550, 400)),
                  ("execute", "execute", ["alert"], (815, 400))],
    },
}
_state: dict = {"nodes": {}, "run": None, "running": False}
_lock = threading.Lock()
_hist: dict = {}


def template(key: Optional[str] = None) -> dict:
    return TEMPLATES[key or "orb_vwap_rvol"]


def node_configs(key: Optional[str] = None) -> dict:
    with get_db() as conn:
        saved = json.loads(get_setting(conn, "flow.nodes") or "{}")
    return {nid: {**REGISTRY[kind].defaults(), **saved.get(nid, {})} for nid, kind, _, _ in template(key)["nodes"]}


def set_node(nid: str, values: dict) -> dict:
    kinds = {n: k for n, k, _, _ in template()["nodes"]}
    if nid not in kinds:
        raise KeyError(nid)
    allowed = REGISTRY[kinds[nid]].defaults()
    with get_db() as conn:
        saved = json.loads(get_setting(conn, "flow.nodes") or "{}")
        saved[nid] = {**saved.get(nid, {}), **{k: v for k, v in values.items() if k in allowed}}
        set_setting(conn, "flow.nodes", json.dumps(saved))
    return node_configs()[nid]


class Ctx:
    """What modules read: live bars (from the feed), saved history, daily data, today's signals."""

    def __init__(self, day: str, interval: int, feed, history: dict, daily: dict, turnover: dict, notify=None):
        self.day, self.interval, self.feed = day, interval, feed
        self.history, self.daily, self.turnover = history, daily, turnover
        self.notify = notify
        self.emitted: list[dict] = []

    def today_bars(self, it):
        return self.feed.day(it["token"]) if self.feed else []

    def bars(self, it):
        return resample(self.history.get(it["token"], []), self.interval) + resample(self.today_bars(it), self.interval)

    def last_closed(self, it):
        b = resample(self.today_bars(it), self.interval)
        return b[-1] if b else None

    def last_price(self, it):
        b = self.today_bars(it)
        return b[-1]["c"] if b else None

    def signalled_today(self) -> set:
        with get_db() as conn:
            return {r[0] for r in conn.execute("SELECT symbol FROM flow_signals WHERE day = ?", (self.day,))}

    def emit(self, it: dict, telegram: bool = True) -> None:
        row = {k: it.get(k) for k in ("symbol", "signal", "entry", "sl", "target", "rvol", "rsi", "vwap", "or_high")}
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO flow_signals (day, symbol, at, payload) VALUES (?, ?, ?, ?)",
                         (self.day, it["symbol"], now_ist(), json.dumps(row)))
        self.emitted.append(row)
        if telegram and self.notify:
            self.notify(row)


def daily_context(config: AppConfig, stocks: list[dict]) -> tuple[dict, dict]:
    """From the saved daily candles: 50 / 200-day averages and the 20-day average turnover of each stock."""
    daily, turnover = {}, {}
    with get_db() as conn:
        for s in stocks:
            cs = get_candles(conn, s["instrument_token"], limit=200)
            closes = [c["close"] for c in cs]
            daily[s["symbol"]] = {"sma50": sum(closes[-50:]) / 50 if len(closes) >= 50 else None,
                                  "sma200": sum(closes) / 200 if len(closes) >= 200 else None}
            last20 = cs[-20:]
            turnover[s["symbol"]] = sum(c["close"] * c["volume"] for c in last20) / max(len(last20), 1)
    return daily, turnover


def run_once(ctx: Ctx, items: list[dict], key: Optional[str] = None) -> dict:
    """Runs every node in order (inputs first); returns {node: {status, in, out, ms, drops, sample}}."""
    t = template(key)
    cfgs = node_configs(key)
    out: dict[str, list] = {}
    res: dict = {}
    for nid, kind, inputs, _ in t["nodes"]:
        _state["nodes"][nid] = {**_state["nodes"].get(nid, {}), "status": "running"}
        t0 = _time.perf_counter()
        if not inputs:
            src = items
        elif kind == "and":
            sets = [{x["symbol"] for x in out[i]} for i in inputs]
            keep = set.intersection(*sets) if sets else set()
            merged = {}
            for i in inputs:
                for x in out[i]:
                    if x["symbol"] in keep:
                        merged[x["symbol"]] = {**merged.get(x["symbol"], {}), **x}
            src = list(merged.values())
        else:
            src = out[inputs[0]]
        n_in = len(src) if kind != "and" else sum(len(out[i]) for i in inputs)
        passed, drops = (src, {}) if kind == "and" else REGISTRY[kind]().run(ctx, src, cfgs[nid])
        out[nid] = passed
        res[nid] = {"status": "ok", "in": n_in, "out": len(passed), "ms": round((_time.perf_counter() - t0) * 1000),
                    "drops": dict(Counter(drops).most_common()), "sample": [x["symbol"] for x in passed[:8]]}
        _state["nodes"][nid] = res[nid]
    return res


def state(config: AppConfig, day: Optional[str] = None) -> dict:
    """Everything the canvas shows (today's signals, or `day`'s)."""
    t = template()
    cfgs = node_configs()
    nodes = []
    for nid, kind, inputs, pos in t["nodes"]:
        m = REGISTRY[kind]
        nodes.append({"id": nid, "kind": kind, "title": m.title, "icon": m.icon, "inputs": inputs, "x": pos[0], "y": pos[1],
                      "summary": m().summary(cfgs[nid]), "config": cfgs[nid], "schema": m.schema(),
                      **_state["nodes"].get(nid, {"status": "idle"})})
    today = day or datetime.now(IST).date().isoformat()
    with get_db() as conn:
        sigs = [{**json.loads(r["payload"]), "at": r["at"]} for r in conn.execute(
            "SELECT at, payload FROM flow_signals WHERE day = ? ORDER BY at", (today,))]
        last = conn.execute("SELECT at, payload FROM flow_runs ORDER BY id DESC LIMIT 1").fetchone()
    from backend.intraday import feed
    return {"template": t["name"], "mode": config.flow.mode, "interval": cfgs["trigger"]["interval"], "nodes": nodes,
            "signals": sigs, "last_run": json.loads(last["payload"]) if last else None, "feed": feed.status(),
            "running": _state["running"]}


def run_now(config: AppConfig, feed_obj, notify=None, now: Optional[datetime] = None) -> dict:
    """One run of the template at `now` (a candle close): the universe from the Nifty 500 list, live bars from the
    feed, history and daily context from saved data. Saved to flow_runs."""
    from backend.ingest import universe as uni
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        now = now or datetime.now(IST)
        _state["running"] = True
        day = now.date().isoformat()
        stocks = uni.resolve_universe(config.ingest)["stocks"]
        daily, turnover = daily_context(config, stocks)
        cfgs = node_configs()
        if _hist.get("day") != day or _hist.get("n") != config.flow.warmup_days:   # saved history: once a day
            _hist.update(day=day, n=config.flow.warmup_days, data={s["instrument_token"]: store.history(
                s["instrument_token"], day, config.flow.warmup_days) for s in stocks})
        history = _hist["data"]
        if feed_obj is not None:
            feed_obj.builder.close_until(now.strftime("%H:%M"))
        ctx = Ctx(day, cfgs["trigger"]["interval"], feed_obj.builder if feed_obj else None, history, daily, turnover, notify)
        items = [{"symbol": s["symbol"], "token": s["instrument_token"]} for s in stocks]
        t0 = _time.perf_counter()
        res = run_once(ctx, items)
        run = {"at": now.isoformat(timespec="seconds"), "candle": now.strftime("%H:%M"), "nodes": res,
               "signals": len(ctx.emitted), "ms": round((_time.perf_counter() - t0) * 1000)}
        _state["run"] = run
        with get_db() as conn:
            conn.execute("INSERT INTO flow_runs (day, at, payload) VALUES (?, ?, ?)", (day, run["at"], json.dumps(run)))
        return {"status": "ok", **run}
    finally:
        _state["running"] = False
        _lock.release()


def due(now: datetime, config: AppConfig, last: Optional[datetime]) -> bool:
    """A candle has just closed inside the trigger window (checked a few seconds after the close)."""
    tr = node_configs()["trigger"]
    t = now.strftime("%H:%M")
    if config.flow.mode == "off" or not (tr["start"] <= t <= tr["end"]):
        return False
    mins = now.hour * 60 + now.minute - (9 * 60 + 15)
    if mins % tr["interval"] or now.second < 3:
        return False
    return last is None or now - last >= timedelta(seconds=50)
