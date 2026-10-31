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
        "name": "ORB 15m + VWAP + RVOL · long & short",
        "nodes": [("trigger", "trigger", [], (20, 145)), ("universe", "universe", ["trigger"], (285, 145)),
                  ("trend", "daily_trend", ["universe"], (550, 0)), ("filters", "intraday_filters", ["universe"], (550, 145)),
                  ("context", "context", ["universe"], (550, 290)), ("and", "and", ["trend", "filters", "context"], (815, 145)),
                  ("orb", "orb", ["and"], (20, 450)), ("score", "score", ["orb"], (285, 450)),
                  ("dedup", "dedup", ["score"], (550, 450)), ("stop", "stop", ["dedup"], (815, 450)),
                  ("size", "size", ["stop"], (20, 610)), ("alert", "alert", ["size"], (285, 610)),
                  ("execute", "execute", ["alert"], (550, 610)), ("monitor", "monitor", ["execute"], (815, 610))],
    },
}
NIFTY_TOKEN, VIX_TOKEN = 256265, 264969
_state: dict = {"nodes": {}, "run": None, "running": False}
_lock = threading.Lock()
_hist: dict = {}


def template(key: Optional[str] = None) -> dict:
    return TEMPLATES[key or "orb_vwap_rvol"]


def node_configs(key: Optional[str] = None) -> dict:
    with get_db() as conn:
        saved = json.loads(get_setting(conn, "flow.nodes") or "{}")
    return {nid: {**REGISTRY[kind].defaults(), **saved.get(nid, {})} for nid, kind, _, _ in template(key)["nodes"]}


def layout() -> dict:
    """Box positions moved by hand on the canvas: {node: [x, y]} over the template's."""
    with get_db() as conn:
        return json.loads(get_setting(conn, "flow.layout") or "{}")


def set_layout(pos: dict) -> dict:
    ids = {n for n, _, _, _ in template()["nodes"]}
    cur = layout()
    cur.update({k: [max(0, int(v[0])), max(0, int(v[1]))] for k, v in pos.items() if k in ids})
    with get_db() as conn:
        set_setting(conn, "flow.layout", json.dumps(cur))
    return cur


def stopped() -> bool:
    """⏹ The intraday kill switch: while on, no live data, no runs, no signals, no paper trades (kept across restarts)."""
    with get_db() as conn:
        return get_setting(conn, "flow.stopped") == "1"


def set_stopped(on: bool) -> None:
    with get_db() as conn:
        set_setting(conn, "flow.stopped", "1" if on else "0")


def reset_layout() -> None:
    with get_db() as conn:
        set_setting(conn, "flow.layout", "{}")


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

    def __init__(self, day: str, interval: int, feed, history: dict, daily: dict, turnover: dict, notify=None,
                 mode: str = "signals", t: str = "15:30"):
        self.day, self.interval, self.feed = day, interval, feed
        self.history, self.daily, self.turnover = history, daily, turnover
        self.notify, self.mode, self.t = notify, mode, t
        self.emitted: list[dict] = []
        self._market = None

    def sides(self):
        return ["long", "short"]

    def day_change(self, it):
        b = self.today_bars(it)
        return (b[-1]["c"] / b[0]["o"] - 1) * 100 if b and b[0]["o"] else None

    def market(self) -> dict:
        """NIFTY 50 (price, VWAP, 20-EMA, % from the open), India VIX, breadth (% of stocks above their VWAP)."""
        if self._market is None:
            from backend.intraday import indicators as ind
            m = {}
            nb = self.feed.day(NIFTY_TOKEN) if self.feed else []
            if nb:
                m["nifty"] = nb[-1]["c"]
                m["nifty_chg"] = (nb[-1]["c"] / nb[0]["o"] - 1) * 100 if nb[0]["o"] else 0
                vols = sum(b["v"] for b in nb)
                m["nifty_vwap"] = ind.vwap(nb) if vols else sum(b["c"] for b in nb) / len(nb)   # index: no volume
                m["nifty_ema"] = ind.ema([b["c"] for b in resample(nb, self.interval)], 20)
            vb = self.feed.day(VIX_TOKEN) if self.feed else []
            if vb:
                m["vix"] = vb[-1]["c"]
            if self.feed:
                above = n = 0
                for tok, bars in self.feed.bars.items():
                    if tok in (NIFTY_TOKEN, VIX_TOKEN) or not bars:
                        continue
                    v = ind.vwap(bars)
                    if v:
                        n += 1
                        above += bars[-1]["c"] > v
                m["breadth"] = 100 * above / n if n else None
            self._market = m
        return self._market

    def positions(self) -> dict:
        from backend.intraday import positions as pos
        return pos.day_stats(self.day)

    def open_paper(self, it: dict) -> None:
        from backend.intraday import positions as pos
        if stopped():
            return
        pos.open_new(self.day, it, it["signal_bar"]["t"], it["plan"])

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
        row = {k: it.get(k) for k in ("symbol", "side", "signal", "entry", "sl", "target", "qty", "risk_rs", "rvol", "rsi",
                                      "score", "setup", "stop_pct")}
        row["mode"] = self.mode
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO flow_signals (day, symbol, at, payload) VALUES (?, ?, ?, ?)",
                         (self.day, it["symbol"], now_ist(), json.dumps(row)))
        self.emitted.append(row)
        if telegram and self.notify:
            self.notify("signal", row)


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
                        m = merged.get(x["symbol"])
                        sides = [y for y in (m or x)["sides"] if y in x.get("sides", [])] if m else x.get("sides", [])
                        merged[x["symbol"]] = {**(m or {}), **x, "sides": sides}
            src = [x for x in merged.values() if x["sides"]]
        else:
            src = out[inputs[0]]
        n_in = len(src) if kind != "and" else sum(len(out[i]) for i in inputs)
        passed, drops = (src, {}) if kind == "and" else REGISTRY[kind]().run(ctx, src, cfgs[nid])
        if kind == "monitor":
            st = ctx.positions()
            n_in, passed = st["open"], []
        out[nid] = passed
        res[nid] = {"status": "ok", "in": n_in, "out": len(passed), "ms": round((_time.perf_counter() - t0) * 1000),
                    "drops": dict(Counter(drops).most_common()), "sample": [x["symbol"] for x in passed[:8]]}
        _state["nodes"][nid] = res[nid]
    return res


def state(config: AppConfig, day: Optional[str] = None) -> dict:
    """Everything the canvas shows (today's signals, or `day`'s)."""
    t = template()
    cfgs = node_configs()
    moved = layout()
    nodes = []
    for nid, kind, inputs, pos in t["nodes"]:
        pos = moved.get(nid, pos)
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
            "running": _state["running"], "stopped": stopped(), "positions": _positions(today), "stats": _stats(today)}


def _positions(day: str) -> list[dict]:
    from backend.intraday import positions as pos
    return pos.load(day)


def _stats(day: str) -> dict:
    from backend.intraday import positions as pos
    st = pos.day_stats(day)
    size = node_configs()["size"]
    c = size["capital"]
    st.update(capital=c, loss_limit=round(c * size["daily_loss_pct"] / 100, 2))
    return st


def journal(days: int = 30) -> dict:
    """Closed paper trades of the last `days` days: per trade, totals, by side / setup / time of day, a daily curve."""
    since = (datetime.now(IST).date() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM flow_positions WHERE status = 'closed' AND day >= ? ORDER BY id DESC", (since,))]

    def agg(ts):
        won = [t for t in ts if t["net"] > 0]
        loss = -sum(t["net"] for t in ts if t["net"] <= 0)
        return {"trades": len(ts), "won": len(won), "win_rate": round(100 * len(won) / len(ts), 1) if ts else None,
                "gross": round(sum(t["gross"] for t in ts), 2), "costs": round(sum(t["costs"] for t in ts), 2),
                "net": round(sum(t["net"] for t in ts), 2),
                "avg_r": round(sum(t.get("r") or 0 for t in ts) / len(ts), 2) if ts else None,
                "profit_factor": round(sum(t["net"] for t in won) / loss, 2) if loss else None}

    def bucket(t):
        e = t.get("entry_t", "")
        return "9:15–10:30" if e < "10:30" else "10:30–12:00" if e < "12:00" else "12:00–close"
    groups = {"side": {}, "setup": {}, "time": {}}
    for t in rows:
        for g, k in (("side", t["side"]), ("setup", t.get("plan", {}).get("setup") or "ORB"), ("time", bucket(t))):
            groups[g].setdefault(k, []).append(t)
    by_day: dict = {}
    for t in rows:
        by_day[t["day"]] = by_day.get(t["day"], 0) + t["net"]
    acc, curve = 0.0, []
    for d in sorted(by_day):
        acc += by_day[d]
        curve.append({"day": d, "net": round(by_day[d], 2), "cum": round(acc, 2)})
    return {"total": agg(rows), "by": {g: {k: agg(v) for k, v in m.items()} for g, m in groups.items()},
            "curve": curve, "trades": rows[:300]}


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
        ctx = Ctx(day, cfgs["trigger"]["interval"], feed_obj.builder if feed_obj else None, history, daily, turnover, notify,
                  config.flow.mode, now.strftime("%H:%M"))
        items = [{"symbol": s["symbol"], "token": s["instrument_token"]} for s in stocks]
        if now.strftime("%H:%M") > cfgs["trigger"]["end"]:                # after the last-signal time: manage only
            items = []
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
