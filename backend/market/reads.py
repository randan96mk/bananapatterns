"""📈 The candle model's reads: the worker client, the market_reads store, the index loop and the hint reads, the
6:40 PM scoring, the scorecard and the cards gate.

Speed rules (tests/test_market_read.py proves them): nothing on the arm / order path calls the worker or waits on a
read. `queue_hint` only puts the hint on this module's own queue (a background thread reads it later); reads run on
background threads with the app-side timeout WORKER_TIMEOUT_S; a worker that is down, slow or wrong gives None, no
market_reads row and "no model read" on the card / panel, nothing else changes. The index loop skips a tick while the
previous pass is still running (it never queues up).

Shadow (market.kronos_show "shadow", the default): every read is stored and scored, the cards and the panel show only
the chip "candle model · shadow · hit rate x % over n reads". "cards": the model's tiles show once NIFTY and BANKNIFTY
30-minute direction are each ≥ GATE_PCT over ≥ GATE_READS scored reads (else the reason shows). The code tiles (trend,
levels, pattern, volatility) always show."""

import json
import os
import queue as _q
import subprocess
import sys
import threading
import time as _time
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable, Optional

from backend.config import IST
from backend.database.models import get_db, get_setting, set_setting
from backend.logging_config.logger import setup_logger
from backend.market import candles as cd
from backend.timefmt import ampm

logger = setup_logger("market")

VERSION = "kronos-mini-v1"
WORKER_TIMEOUT_S = 6.0
OPEN, CLOSE = time(9, 15), time(15, 30)
INDEX_HORIZONS, HINT_HORIZONS = (15, 30), (10, 15)
PREMIUM_MIN_BARS = 60
GATE_PCT, GATE_READS = 55.0, 100
SCORE_JOB, SCORE_TIME = "market_score", time(18, 40)
VIX_KEY = "market.vix"                         # app_settings: the last India VIX quote {last, prev, at}
STATUS_KEY = "market.status"                   # app_settings: the loop's last pass {at, fetched, skipped, why}
ROOT = Path(__file__).resolve().parents[2]

# the tests' fake worker: (payload) -> dict | None. None = the real HTTP worker.
WORKER: Optional[Callable[[dict], Optional[dict]]] = None
_call_lock = threading.Lock()                  # one worker call at a time (the timeout covers the call, not the wait)
# in memory (System › AI): the last stored read {at, symbol, ms} and the last failed call {at, text}; lost on a restart
LAST: dict = {"read": None, "error": None}
MODEL, PARAMS, LICENCE = "Kronos-mini", 4_100_000, "MIT"
HEALTH_TTL_S, READ_NOW_GAP_S = 30.0, 60.0


def _now() -> datetime:
    return datetime.now(IST)


def on(config) -> bool:
    return bool(getattr(config.market, "kronos", False))


def show(config) -> str:
    return getattr(config.market, "kronos_show", "shadow")


def market_hours(now: datetime) -> bool:
    from backend.ingest.holidays import is_trading_day
    return is_trading_day(now.date()) and OPEN <= now.time() <= CLOSE


# --- the worker client ----------------------------------------------------------------------------------------------------

def _http(config, payload: dict, timeout: float) -> Optional[dict]:
    url = f"http://127.0.0.1:{int(config.market.port)}/read"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def call_worker(config, payload: dict, timeout: float = WORKER_TIMEOUT_S) -> Optional[dict]:
    """One read from the worker, or None (down, timed out, an error, bad JSON, a reply without the numbers)."""
    try:
        with _call_lock:
            out = WORKER(payload) if WORKER is not None else _http(config, payload, timeout)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.info("Candle model read failed: %s", str(exc)[:160])
        _failed(str(exc))
        return None
    except Exception as exc:
        logger.warning("Candle model read failed: %s", str(exc)[:160])
        _failed(str(exc))
        return None
    if not isinstance(out, dict) or out.get("p_up") is None or not isinstance(out.get("reads", []), list):
        _failed("a reply without the numbers")
        return None
    return out


def _failed(text: str) -> None:
    LAST["error"] = {"at": _now().isoformat(timespec="seconds"), "text": text[:160]}


def health(config, timeout: float = 1.0) -> dict:
    """{state ready|loading|error|down, load_s, version} from the worker's /health (never raises)."""
    if WORKER is not None:
        return {"state": "ready", "version": VERSION, "load_s": None}
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(config.market.port)}/health", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return {"state": "down", "version": VERSION, "load_s": None}


_health_cache: dict = {"t": None, "port": None, "v": None}


def cached_health(config) -> dict:
    """health() with a 1 s timeout, kept HEALTH_TTL_S (the System › AI status calls it on every load)."""
    c, port = _health_cache, int(config.market.port)
    if c["v"] is not None and c["port"] == port and _time.monotonic() - c["t"] < HEALTH_TTL_S:
        return c["v"]
    v = health(config, 1.0)
    c.update(t=_time.monotonic(), port=port, v=v)
    return v


# --- the store (never deleted; the archive moves rows older than 180 days) ----------------------------------------------

def _row(r) -> dict:
    d = dict(r)
    for k in ("touch_json", "paths_json", "outcome_json"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else None
        except (TypeError, ValueError):
            d[k] = None
    return d


def store(at: datetime, symbol: str, res: dict, one: dict, extra: dict, hint_id: Optional[int] = None, book: Optional[str] = None,
          interval: Optional[str] = None) -> int:
    """One market_reads row per horizon. `book` "real" marks a read on a Real position (backend/market/real_reads.py);
    `interval` overrides the worker's echo (the Real reads say "1d" / "1m" by the candles they came from)."""
    paths = {**(one.get("paths_summary") or {}), "last": res.get("last"), "samples": one.get("samples"), "context": res.get("context"),
             "touch_any": one.get("touch_any") or {}, **extra}
    with get_db() as conn:
        return conn.execute("INSERT INTO market_reads (at, symbol, interval, horizon, p_up, exp_range, touch_json, paths_json, version, ms, hint_id, book) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (at.isoformat(timespec="seconds"), symbol, interval or res.get("interval") or "1m", int(one["horizon"]), one.get("p_up"),
                             one.get("exp_range"), json.dumps(one.get("touch") or {}), json.dumps(paths), res.get("version") or VERSION,
                             res.get("ms"), hint_id, book)).lastrowid


def rows(where: str = "1", args: tuple = (), order: str = "id") -> list[dict]:
    with get_db() as conn:
        return [_row(r) for r in conn.execute(f"SELECT * FROM market_reads WHERE {where} ORDER BY {order}", args)]


def latest(symbol: str, horizon: int, hint_id: Optional[int] = None, since: Optional[str] = None) -> Optional[dict]:
    q, a = "symbol = ? AND horizon = ? AND book IS NULL", [symbol, horizon]
    q += " AND hint_id = ?" if hint_id is not None else " AND hint_id IS NULL"
    if hint_id is not None:
        a.append(hint_id)
    if since:
        q += " AND at >= ?"
        a.append(since)
    with get_db() as conn:
        r = conn.execute(f"SELECT * FROM market_reads WHERE {q} ORDER BY at DESC, id DESC LIMIT 1", a).fetchone()
    return _row(r) if r else None


def hint_reads(hid: int) -> list[dict]:
    return rows("hint_id = ?", (hid,))


# --- index reads --------------------------------------------------------------------------------------------------------

def read_series(config, symbol: str, bars: list[dict], horizons: tuple, levels: list[float], extra: dict, now: datetime,
                hint_id: Optional[int] = None) -> list[int]:
    """One worker call for the longest horizon (the shorter ones are the same paths cut earlier); one row per horizon."""
    if len(bars) < 30:
        return []
    payload = {"symbol": symbol, "interval": "1m", "candles": cd.to_worker(bars), "horizon": max(horizons), "horizons": list(horizons),
               "samples": int(config.market.samples), "levels": [float(x) for x in levels if x is not None]}
    res = call_worker(config, payload)
    if not res:
        return []
    reads = res.get("reads") or [res]
    ids = [store(now, symbol, res, one, extra, hint_id) for one in reads if int(one.get("horizon") or 0) in horizons]
    if ids:
        LAST["read"] = {"at": now.isoformat(timespec="seconds"), "symbol": symbol, "ms": res.get("ms")}
    return ids


def read_index(config, symbol: str, now: Optional[datetime] = None) -> list[int]:
    now = now or _now()
    bars = cd.index_bars(symbol, 3, now.date().isoformat())
    return read_series(config, symbol, bars, INDEX_HORIZONS, [], {"basis": "index", "index": symbol, "why": "interval"}, now)


# --- hint reads -------------------------------------------------------------------------------------------------------------

def hint_index(h: dict) -> Optional[str]:
    s = (h.get("symbol") or "").upper()
    return s if s in cd.INDICES else None


def hint_levels(h: dict) -> dict:
    """{stop, t1, zone_top}: the premium levels a hint read asks the paths about (None when not a premium level)."""
    from backend.tips import kite_orders as ko
    from backend.tips import plan as pl
    plan = h.get("plan") or {}
    tg = [float(x) for x in (plan.get("targets") or h.get("targets") or []) if x is not None]
    e = plan.get("entry") or h.get("entry") or {}
    top = None
    if e.get("mode") not in (None, "market") and not pl.is_spot(e) and e.get("lo") is not None:
        top = float(e.get("hi") if e.get("hi") is not None else e["lo"])
    return {"stop": ko.premium_stop(h), "t1": tg[0] if tg else None, "zone_top": top}


def read_hint(config, hid: int, why: str, now: Optional[datetime] = None) -> list[int]:
    """The hint's read: its premium's 1-minute candles when ≥ PREMIUM_MIN_BARS, else its index's; horizons 10 and 15; the
    stop and T1 asked as touch levels on a premium read (which is crossed first per path). Never raises."""
    from backend.tips import book as bk
    now = now or _now()
    try:
        h = bk.load(hid)
        if not h:
            return []
        lv = hint_levels(h)
        prem = cd.premium_bars(hid, now.date().isoformat())
        extra = {"why": why, "index": hint_index(h), "levels": {k: v for k, v in lv.items() if v is not None}}
        if len(prem) >= PREMIUM_MIN_BARS:
            levels = [x for x in (lv["stop"], lv["t1"]) if x is not None]
            if lv["zone_top"] is not None and lv["zone_top"] not in levels:
                levels.append(lv["zone_top"])
            sym = h.get("tradingsymbol") or f"#{hid}"
            return read_series(config, sym, prem, HINT_HORIZONS, levels, {**extra, "basis": "premium"}, now, hid)
        idx = hint_index(h)
        if not idx:
            return []
        bars = cd.index_bars(idx, 3, now.date().isoformat())
        return read_series(config, idx, bars, HINT_HORIZONS, [], {**extra, "basis": "index"}, now, hid)
    except Exception:
        logger.exception("Market read of hint #%s failed", hid)
        return []


_hq: "_q.Queue[tuple]" = _q.Queue()
_hthread: Optional[threading.Thread] = None
_hpending: set = set()
_hlock = threading.Lock()
QUEUED: list = []                              # (hid, why, monotonic) of every queue_hint call (the tests read it)
FILL_DELAY_S = 2.0                             # a fill's read waits for the fill's own save first


def queue_hint(config, hid: int, why: str) -> bool:
    """Put the hint on the read queue and return at once (the arm / order path calls this AFTER the order; it never
    waits). Deduped while the same (hint, why) is waiting. Off (market.kronos) → nothing."""
    global _hthread
    if not on(config):
        return False
    QUEUED.append((hid, why, _time.monotonic()))
    del QUEUED[:-200]
    with _hlock:
        if (hid, why) in _hpending:
            return False
        _hpending.add((hid, why))
        _hq.put((config, hid, why, _time.monotonic()))
        if _hthread is None or not _hthread.is_alive():
            _hthread = threading.Thread(target=_hloop, name="market-hint-reads", daemon=True)
            _hthread.start()
    return True


def _hloop() -> None:
    while True:
        config, hid, why, t0 = _hq.get()
        try:
            if why == "fill":
                _time.sleep(max(0.0, FILL_DELAY_S - (_time.monotonic() - t0)))
            ids = read_hint(config, hid, why)
            from backend.market import words
            if why in ("ingest", "fill"):
                words.card(config, hid, why, read_ids=ids)
        except Exception:
            logger.exception("Market hint read #%s failed", hid)
        finally:
            with _hlock:
                _hpending.discard((hid, why))
            _hq.task_done()


def drain(timeout: float = 10.0) -> None:
    """Wait until the hint queue is empty (tests)."""
    end = _time.monotonic() + timeout
    while _time.monotonic() < end and (_hpending or not _hq.empty()):
        _time.sleep(0.02)


def open_kite_hints() -> list[dict]:
    """Hints armed or entered on Kite right now (the interval reads)."""
    from backend.tips import book as bk
    from backend.tips import kite_orders as ko
    return [h for h in bk.rows("kite IS NOT NULL", order="id") if ko.kstate(h)["state"] in ("armed", "armed_wait", "armed_watch", "entered")]


# --- the loop -------------------------------------------------------------------------------------------------------------

def vix() -> Optional[dict]:
    with get_db() as conn:
        raw = get_setting(conn, VIX_KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _save_quotes(client, now: datetime) -> None:
    try:
        q = client.quote([cd.VIX_KEY]) or {}
    except Exception as exc:
        logger.info("VIX quote failed: %s", str(exc)[:120])
        return
    d = q.get(cd.VIX_KEY) or {}
    if d.get("last_price") is None:
        return
    prev = (d.get("ohlc") or {}).get("close")
    with get_db() as conn:
        set_setting(conn, VIX_KEY, json.dumps({"last": float(d["last_price"]), "prev": float(prev) if prev else None,
                                               "at": now.isoformat(timespec="seconds")}))


def status() -> dict:
    with get_db() as conn:
        raw = get_setting(conn, STATUS_KEY)
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _set_status(**kw) -> None:
    with get_db() as conn:
        set_setting(conn, STATUS_KEY, json.dumps(kw))


class MarketService:
    """The index loop (its own thread): every market.interval_s during 9:15 AM–3:30 PM on trading days, the three
    indices' candles fetched (one call each) and read on a background thread, then the open Kite hints and the panel's
    words. Never raises out of its loop; a pass still running makes the next tick skip."""

    def __init__(self, state):
        self.state = state
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._busy = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.skipped = 0
        self.last_manual: Optional[float] = None       # monotonic time of the last "Read now" (System › AI)

    def snapshot(self) -> dict:
        """The in-memory state System › AI shows: {last_read, last_error, worker_pid, skipped}."""
        return {"last_read": LAST["read"], "last_error": LAST["error"], "skipped": self.skipped,
                "worker_pid": self.proc.pid if self.proc and self.proc.poll() is None else None}

    def read_now(self, symbol: str = "NIFTY") -> dict:
        """One index read by hand (System › AI "Read now"): one a minute, refused while the model is off, the worker
        is not ready or a pass runs. Reads the stored candles only; never touches an order. {ok, ids, last_read} or
        {ok: False, code, why}."""
        config = self.state.config
        if symbol not in cd.INDICES:
            return {"ok": False, "code": 400, "why": f"unknown index {symbol}"}
        if not on(config):
            return {"ok": False, "code": 409, "why": "the candle model is off (Settings › market · kronos)"}
        h = cached_health(config)
        if h.get("state") != "ready":
            return {"ok": False, "code": 409, "why": f"the worker is {h.get('state') or 'down'}"}
        if self.last_manual is not None and _time.monotonic() - self.last_manual < READ_NOW_GAP_S:
            wait = int(READ_NOW_GAP_S - (_time.monotonic() - self.last_manual)) + 1
            return {"ok": False, "code": 429, "why": f"one read a minute · again in {wait} s"}
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "code": 409, "why": "a read pass is running"}
        try:
            self.last_manual = _time.monotonic()
            ids = read_index(config, symbol)
        finally:
            self._busy.release()
        if not ids:
            return {"ok": False, "code": 409, "why": (LAST["error"] or {}).get("text") or "no read (fewer than 30 stored candles?)"}
        return {"ok": True, "ids": ids, "last_read": LAST["read"]}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.ensure_worker()
        self._thread = threading.Thread(target=self._loop, name="market-read", daemon=True)
        self._thread.start()
        try:                                              # 📈 the Real positions: a held name whose latest daily candle has no read yet
            from backend.market import real_reads as rr
            rr.start_nightly(self.state.config, wait_ready=True)
        except Exception:
            logger.exception("Real positions start-up read failed")

    def stop(self) -> None:
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def ensure_worker(self) -> Optional[str]:
        """Start the worker process (python -m backend.market.kronos_worker) when market.kronos is on and torch is
        installed; its output goes to logs/kronos_worker.log. Returns why not, or None."""
        import importlib.util
        config = self.state.config
        if not on(config):
            return "off"
        if self.proc and self.proc.poll() is None:
            return None
        if health(config, 0.5).get("state") in ("ready", "loading"):
            return None                                   # already running (a second app copy, or a restart)
        if importlib.util.find_spec("torch") is None:
            logger.info("Candle model: torch is not installed; no worker (the cards say no model read)")
            return "torch missing"
        log = ROOT / "logs" / "kronos_worker.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as fh:
            self.proc = subprocess.Popen([sys.executable, "-m", "backend.market.kronos_worker", "--port", str(int(config.market.port))],
                                         cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT)
        logger.info("Candle model worker started (pid %s, port %s)", self.proc.pid, config.market.port)
        return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Market read tick failed")
            self._stop.wait(max(30, int(self.state.config.market.interval_s)))

    def tick(self, now: Optional[datetime] = None, wait: bool = False) -> dict:
        config, client = self.state.config, self.state.client
        now = now or _now()
        if not on(config):
            _set_status(at=now.isoformat(timespec="seconds"), why="off")
            return {"why": "off"}
        if not market_hours(now):
            return {"why": "closed"}
        if not self._busy.acquire(blocking=False):
            self.skipped += 1
            return {"why": "busy", "skipped": self.skipped}
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None
            self.ensure_worker()
        fetched = {}
        real920 = False
        try:
            from backend.market import real_reads as rr
            real920 = rr.due_920(config, client, now)          # 📈 the Real positions' 9:20 AM read (once a day, logged in)
        except Exception:
            logger.exception("Real 9:20 check failed")
        try:
            if cd.logged_in(client):
                for s in cd.INDICES:
                    try:
                        fetched[s] = cd.fetch(client, s, now)["bars"]
                    except Exception as exc:
                        fetched[s] = f"error: {str(exc)[:80]}"
                _save_quotes(client, now)
            _set_status(at=now.isoformat(timespec="seconds"), why=None if cd.logged_in(client) else cd.LOGIN_TEXT, fetched=fetched)
        except Exception:
            self._busy.release()
            raise
        t = threading.Thread(target=self._pass, args=(config, now, client if real920 else None), name="market-read-pass", daemon=True)
        t.start()
        if wait:
            t.join()
        return {"why": None, "fetched": fetched}

    def _pass(self, config, now: datetime, real920_client=None) -> None:
        try:
            from backend.market import words
            for s in cd.INDICES:
                read_index(config, s, now)
            for h in open_kite_hints():
                read_hint(config, h["id"], "interval", now)
            for s in cd.INDICES:
                words.panel(config, s, now)
        except Exception:
            logger.exception("Market read pass failed")
        try:
            if real920_client is not None:                # 📈 the Real positions' 9:20 AM read (fetch + read, this thread)
                from backend.market import real_reads as rr
                rr.run_920(config, real920_client, now)
        except Exception:
            logger.exception("Real 9:20 read failed")
        finally:
            self._busy.release()


# --- scoring (6:40 PM) ----------------------------------------------------------------------------------------------------

def _actual_bars(r: dict) -> list[dict]:
    p = r.get("paths_json") or {}
    day = r["at"][:10]
    if p.get("basis") == "premium" and r.get("hint_id"):
        return cd.premium_bars(r["hint_id"], day)
    sym = p.get("index") or r["symbol"]
    if sym in cd.TOKENS:
        return [b for b in cd.index_bars(sym, 1, day) if b["d"] == day]
    return []


def outcome(r: dict, bars: list[dict]) -> Optional[dict]:
    """The read against what happened: {up, close, range, first, direction_right, touch_right} or None when the
    candles do not reach the horizon yet."""
    from backend.market import paths as pth
    at = datetime.fromisoformat(r["at"])
    start, end = at.strftime("%H:%M"), (at + timedelta(minutes=int(r["horizon"]))).strftime("%H:%M")
    win = [b for b in bars if start < b["t"] <= end]
    if not win or win[-1]["t"] < end:
        return None
    p = r.get("paths_json") or {}
    last = p.get("last")
    if last is None:
        return None
    close = win[-1]["c"]
    up = close > last
    pred = None if r.get("p_up") is None or r["p_up"] == 0.5 else r["p_up"] > 0.5
    touch = r.get("touch_json") or {}
    first = None
    touch_right = None
    if touch:
        lv = [float(k) for k in touch]
        f = pth.first_touch(last, [b["h"] for b in win], [b["l"] for b in win], lv, len(win))
        first = pth.level_key(f) if f is not None else "none"
        probs = {**touch, "none": round(max(0.0, 1 - sum(touch.values())), 3)}
        best = max(probs, key=lambda k: probs[k])
        touch_right = best == first
    return {"up": up, "close": round(close, 2), "range": round(max(b["h"] for b in win) - min(b["l"] for b in win), 2), "first": first,
            "direction_right": None if pred is None else pred == up, "touch_right": touch_right, "bars": len(win)}


def score(now: Optional[datetime] = None) -> dict:
    """Every unscored read whose horizon has passed: its outcome stored (a read whose candles never reached the
    horizon after a day is scored "missing", rights None). {scored, missing, waiting}."""
    now = now or _now()
    out = {"scored": 0, "missing": 0, "waiting": 0}
    cache: dict = {}
    for r in rows("scored_at IS NULL AND book IS NULL"):           # the Real rows: real_reads.score (same job)
        at = datetime.fromisoformat(r["at"])
        if at.tzinfo is None:
            at = at.replace(tzinfo=IST)
        if at + timedelta(minutes=int(r["horizon"])) > now:
            out["waiting"] += 1
            continue
        key = ((r.get("paths_json") or {}).get("basis"), r.get("hint_id"), (r.get("paths_json") or {}).get("index") or r["symbol"], r["at"][:10])
        if key not in cache:
            cache[key] = _actual_bars(r)
        o = outcome(r, cache[key])
        if o is None:
            if (now - at).total_seconds() < 86400:
                out["waiting"] += 1
                continue
            o = {"missing": True, "direction_right": None, "touch_right": None}
            out["missing"] += 1
        else:
            out["scored"] += 1
        with get_db() as conn:
            conn.execute("UPDATE market_reads SET scored_at = ?, outcome_json = ? WHERE id = ?", (now.isoformat(timespec="seconds"), json.dumps(o), r["id"]))
    return out


REAL_DAILY, REAL_920 = "Real · daily", "Real · 9:20 AM"        # the Real book's scorecard groups (real_reads.py)


def group_of(r: dict) -> tuple[str, str, int]:
    p = r.get("paths_json") or {}
    if r.get("book") == "real":
        return (REAL_DAILY if (r.get("interval") or "1d") == "1d" else REAL_920), r.get("interval") or "1d", int(r["horizon"])
    name = "option premium" if p.get("basis") == "premium" else r["symbol"]
    return name, r.get("interval") or "1m", int(r["horizon"])


def group_label(name: str, interval: str, horizon: int) -> str:
    """'NIFTY · 30 min', 'Real · daily · 3 d', 'Real · 9:20 AM'."""
    if name == REAL_DAILY:
        return f"{name} · {horizon} d"
    if name == REAL_920:
        return name
    return f"{name} · {horizon} min"


def scorecard(where: str = "1", args: tuple = ()) -> list[dict]:
    """Per (index | option premium, interval, horizon): {name, horizon, reads (stored), scored, direction_pct, touch_pct,
    touch_n, avg_conf (mean of max(p_up, 1 − p_up), %), since}."""
    g: dict = {}
    for r in rows(where, args):
        k = group_of(r)
        x = g.setdefault(k, {"name": k[0], "interval": k[1], "horizon": k[2], "reads": 0, "scored": 0, "right": 0, "touch_n": 0, "touch_right": 0,
                             "conf": [], "since": r["at"][:10]})
        x["reads"] += 1
        x["since"] = min(x["since"], r["at"][:10])
        o = r.get("outcome_json") or {}
        if o.get("direction_right") is not None:
            x["scored"] += 1
            x["right"] += bool(o["direction_right"])
            x["conf"].append(max(r["p_up"], 1 - r["p_up"]))
        if o.get("touch_right") is not None:
            x["touch_n"] += 1
            x["touch_right"] += bool(o["touch_right"])
    order = {s: i for i, s in enumerate(cd.INDICES)}
    real = {REAL_DAILY: 1, REAL_920: 2}
    out = []
    for k in sorted(g, key=lambda k: (real.get(k[0], 0), k[0] == "option premium", order.get(k[0], 9), k[0], k[2] if k[0] in real else -k[2])):
        x = g[k]
        out.append({"name": x["name"], "interval": x["interval"], "horizon": x["horizon"], "reads": x["reads"], "scored": x["scored"],
                    "direction_pct": round(100.0 * x["right"] / x["scored"], 1) if x["scored"] else None,
                    "touch_n": x["touch_n"], "touch_pct": round(100.0 * x["touch_right"] / x["touch_n"], 1) if x["touch_n"] else None,
                    "avg_conf": round(100.0 * sum(x["conf"]) / len(x["conf"]), 1) if x["conf"] else None, "since": x["since"]})
    return out


def card_row(sc: list[dict], name: str, horizon: int = 30) -> Optional[dict]:
    return next((x for x in sc if x["name"] == name and x["horizon"] == horizon), None)


def gate(config, sc: Optional[list[dict]] = None) -> dict:
    """{cards: the model's tiles may show, mode, why}: kronos_show "cards" AND NIFTY and BANKNIFTY 30-minute direction
    ≥ GATE_PCT over ≥ GATE_READS scored reads each."""
    sc = scorecard() if sc is None else sc
    fails = []
    for s in ("NIFTY", "BANKNIFTY"):
        x = card_row(sc, s, 30) or {"scored": 0, "direction_pct": None}
        if x["scored"] < GATE_READS:
            fails.append(f"{s} 30 min has {x['scored']} scored reads (needs {GATE_READS})")
        elif (x["direction_pct"] or 0) < GATE_PCT:
            fails.append(f"{s} 30 min direction {x['direction_pct']:g} % (needs {GATE_PCT:g} %)")
    earned = not fails
    mode = show(config)
    if not on(config):
        return {"cards": False, "earned": earned, "mode": "off", "why": "the candle model is off (Settings › market · kronos)"}
    if mode != "cards":
        return {"cards": False, "earned": earned, "mode": mode, "why": "shadow: stored and scored, not shown" + ("" if not fails else " · " + "; ".join(fails))}
    return {"cards": earned, "earned": earned, "mode": mode, "why": "the scorecard earns it" if earned else "held in shadow: " + "; ".join(fails)}


def chip(config, name: str = "NIFTY", horizon: int = 30, sc: Optional[list[dict]] = None) -> str:
    """'candle model · shadow · hit rate 58 % over 212 reads' (or · cards / · off)."""
    if not on(config):
        return "candle model · off"
    sc = scorecard() if sc is None else sc
    x = card_row(sc, name, horizon)
    g = gate(config, sc)
    tag = "cards" if g["cards"] else "shadow"
    if not x or not x["scored"]:
        return f"candle model · {tag} · not scored yet"
    return f"candle model · {tag} · hit rate {round(x['direction_pct'])} % over {x['scored']} reads"


_sc_cache: dict = {"key": None, "sc": None}


def model_status(config, service=None, now: Optional[datetime] = None) -> dict:
    """📈 System › AI · Candle model: {model, params, state off|loading|ready|down, loaded_in_ms, error, last_read {at,
    symbol, ms}, reads_today, scored_today, mode off|shadow|cards, hit {pct, n}, gate {ok, reason}, settings}. Cheap:
    the service's in-memory state, one count query, the worker's /health (1 s timeout, kept 30 s) only when on; the
    scorecard is recomputed only when the number of scored reads changes."""
    now = now or _now()
    snap = service.snapshot() if service is not None else {"last_read": LAST["read"], "last_error": LAST["error"]}
    day = now.date().isoformat()
    with get_db() as conn:
        r = conn.execute("SELECT COUNT(CASE WHEN at >= ? THEN 1 END), COUNT(CASE WHEN at >= ? AND scored_at IS NOT NULL THEN 1 END), "
                         "COUNT(scored_at), (SELECT at || '|' || symbol || '|' || IFNULL(ms, '') FROM market_reads ORDER BY at DESC, id DESC LIMIT 1) "
                         "FROM market_reads", (day, day)).fetchone()
    reads_today, scored_today, scored_all, last_db = r[0] or 0, r[1] or 0, r[2] or 0, r[3]
    key = (os.getenv("BP_DB_PATH"), scored_all)                 # only the scored reads move the gate and the hit rate
    if _sc_cache["key"] != key or _sc_cache["sc"] is None:
        _sc_cache.update(key=key, sc=scorecard())
    sc = _sc_cache["sc"]
    last = snap.get("last_read")
    if not last and last_db:
        at, sym, ms = (last_db.split("|") + ["", ""])[:3]
        last = {"at": at, "symbol": sym, "ms": int(float(ms)) if ms else None}
    err = snap.get("last_error")
    if not on(config):
        state, h = "off", {}
    else:
        h = cached_health(config)
        state = {"ready": "ready", "loading": "loading"}.get(h.get("state"), "down")
    g = gate(config, sc)
    x = card_row(sc, "NIFTY", 30)
    s = config.market
    return {"model": MODEL, "params": PARAMS, "licence": LICENCE, "where": "this server", "version": h.get("version") or VERSION,
            "state": state, "loaded_in_ms": round(float(h["load_s"]) * 1000) if h.get("load_s") is not None else None,
            "error": (h.get("error") or (err or {}).get("text") or ("the worker does not answer on 127.0.0.1:%s" % s.port)) if state == "down" else None,
            "last_error": err, "last_read": last, "reads_today": reads_today, "scored_today": scored_today,
            "mode": g["mode"] if g["mode"] in ("off", "shadow") else ("cards" if g["cards"] else "shadow"), "mode_set": show(config),
            "hit": {"pct": x["direction_pct"], "n": x["scored"]} if x and x["scored"] else None,
            "gate": {"ok": bool(g["cards"]), "reason": g["why"]}, "chip": chip(config, sc=sc),
            "settings": {"kronos": bool(s.kronos), "kronos_show": s.kronos_show, "interval_s": int(s.interval_s), "samples": int(s.samples), "port": int(s.port)},
            "read_now_gap_s": int(READ_NOW_GAP_S)}


def channel_hit_rate(channel: str, since: str, until: str) -> Optional[dict]:
    """The model's direction hit rate on one channel's calls (hint reads of its hints posted in [since, until))."""
    with get_db() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM tips_hints WHERE channel = ? AND posted_at >= ? AND posted_at < ?", (channel, since, until))]
    if not ids:
        return None
    rs = rows(f"hint_id IN ({','.join('?' * len(ids))})", tuple(ids))
    sc = [(r.get("outcome_json") or {}).get("direction_right") for r in rs]
    sc = [x for x in sc if x is not None]
    if not sc:
        return None
    return {"pct": round(100.0 * sum(sc) / len(sc), 1), "n": len(sc)}


def due(now: datetime, config, done: set, trading: bool) -> list[str]:
    """market_score at 6:40 PM on trading days."""
    if trading and on(config) and SCORE_JOB not in done and SCORE_TIME <= now.time() < time(23, 0):
        return [SCORE_JOB]
    return []


def t12(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(str(iso))
    except ValueError:
        return ""
    return ampm(d)
