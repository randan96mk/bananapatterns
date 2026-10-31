"""The live feed and the day's cycle for ⚡ Intraday Flow (driven by the scheduler's tick, every ~30 s):
  9:00          warm-up: the last `warmup_days` sessions of 1-minute bars for the Nifty 500 are taken from the saved
                store; only missing stock-days are fetched from Kite (once; then saved)
  9:10 – 15:30  Kite WebSocket ticks (quote mode) build 1-minute candles; the flow runs at each candle close
  15:35         day close: today's bars are saved; stocks with gaps are re-fetched from Kite for today only;
                old days are shrunk to 5-minute bars (retention)
Read-only: Kite data only, never orders."""

import threading
from datetime import datetime
from typing import Optional

from backend.config import IST
from backend.intraday import flow, store
from backend.intraday.candles import CandleBuilder
from backend.logging_config.logger import setup_logger

logger = setup_logger("flow_feed")

_feed: dict = {"state": "off", "ticks": 0, "tokens": 0, "since": None, "error": None, "last_tick": None}
RUNNER = None


def status() -> dict:
    return dict(_feed)


class Feed:
    """KiteTicker in its own thread, feeding a CandleBuilder."""

    def __init__(self, api_key: str, access_token: str, tokens: list[int], ticker_cls=None):
        self.builder = CandleBuilder()
        self.tokens = tokens
        self.ticker = None
        self.api_key, self.access_token = api_key, access_token
        self.ticker_cls = ticker_cls

    def on_ticks(self, ws, ticks):
        now = datetime.now(IST)
        for t in ticks:
            if t.get("last_price") is not None:
                self.builder.add(t["instrument_token"], t["last_price"], int(t.get("volume_traded") or 0),
                                 t.get("exchange_timestamp") or now)
        _feed["ticks"] += len(ticks)
        _feed["last_tick"] = now.isoformat(timespec="seconds")

    def start(self):
        if self.ticker_cls is None:
            from kiteconnect import KiteTicker as ticker_cls
        else:
            ticker_cls = self.ticker_cls
        self.ticker = ticker_cls(self.api_key, self.access_token)

        def on_connect(ws, response):
            ws.subscribe(self.tokens)
            ws.set_mode(ws.MODE_QUOTE, self.tokens)
            _feed.update(state="live", error=None)

        def on_close(ws, code, reason):
            _feed.update(state="reconnecting" if _feed["state"] == "live" else _feed["state"], error=str(reason)[:120])

        def on_error(ws, code, reason):
            _feed["error"] = str(reason)[:120]

        self.ticker.on_ticks, self.ticker.on_connect = self.on_ticks, on_connect
        self.ticker.on_close, self.ticker.on_error = on_close, on_error
        _feed.update(state="connecting", tokens=len(self.tokens), since=datetime.now(IST).isoformat(timespec="seconds"))
        self.ticker.connect(threaded=True)

    def stop(self):
        try:
            if self.ticker:
                self.ticker.close()
        except Exception:
            pass
        _feed["state"] = "off"


class Runner:
    """The day's cycle; tick(now) is cheap and called by the scheduler."""

    def __init__(self, state):
        self.state = state
        self.feed: Optional[Feed] = None
        self.day = None
        self.warmed = self.closed = None
        self.last_run: Optional[datetime] = None

    def _stocks(self):
        from backend.ingest import universe as uni
        return uni.resolve_universe(self.state.config.ingest)["stocks"]

    def warm_up(self, today: str) -> int:
        """Saved sessions before today; fetches only stock-days not saved yet (one request per stock)."""
        from backend.ingest import holidays as hol
        from datetime import date, timedelta
        days, d = [], date.fromisoformat(today)
        while len(days) < self.state.config.flow.warmup_days:
            d -= timedelta(days=1)
            if hol.is_trading_day(d):
                days.append(d.isoformat())
        stocks = {s["instrument_token"]: s["symbol"] for s in self._stocks()}
        todo: dict[int, list[str]] = {}
        for tok, dd in store.missing(list(stocks), days):
            todo.setdefault(tok, []).append(dd)
        n = 0
        for tok, dd in todo.items():
            try:
                n += store.fetch(self.state.client, tok, dd)
            except Exception as exc:
                logger.warning("Warm-up fetch %s: %s", stocks.get(tok), exc)
        logger.info("Flow warm-up: %d stock-days fetched (%d stocks needed some)", n, len(todo))
        return n

    def close_day(self, today: str) -> dict:
        """Saves today's bars; re-fetches stocks with gaps for today only; retention."""
        saved = repaired = 0
        if self.feed:
            for tok in self.feed.tokens:
                bars = self.feed.builder.day(tok)
                if bars:
                    store.save(tok, today, bars, "ticks")
                    saved += 1
            self.feed.stop()
        for tok, _ in store.missing([s["instrument_token"] for s in self._stocks()], [today]):
            try:
                repaired += store.fetch(self.state.client, tok, [today])
            except Exception as exc:
                logger.warning("Day-close repair %s: %s", tok, exc)
        shrunk = store.prune(self.state.config.flow.retention_months * 31, today)
        logger.info("Flow day close %s: %d saved, %d repaired from Kite, %d old days shrunk", today, saved, repaired, shrunk)
        return {"saved": saved, "repaired": repaired, "shrunk": shrunk}

    def tick(self, now: datetime, trading: bool, notify=None) -> Optional[str]:
        cfg, client = self.state.config, self.state.client
        today, t = now.date().isoformat(), now.strftime("%H:%M")
        if self.day != today:
            self.day, self.warmed, self.closed, self.feed = today, None, None, None
        if cfg.flow.mode == "off" or not trading or not getattr(client, "has_token", False):
            return None
        if self.warmed is None and "09:00" <= t < "15:30":
            self.warmed = True
            threading.Thread(target=self.warm_up, args=(today,), name="flow-warmup", daemon=True).start()
            return "warm_up"
        if self.feed is None and "09:10" <= t < "15:30":
            toks = [s["instrument_token"] for s in self._stocks()]
            self.feed = Feed(client.config.api_key, client.config.access_token, toks)
            self.feed.start()
            return "feed"
        if self.feed is not None and flow.due(now, cfg, self.last_run):
            self.last_run = now
            threading.Thread(target=flow.run_now, args=(cfg, self.feed, notify, now), name="flow-run", daemon=True).start()
            return "run"
        if self.closed is None and t >= "15:35" and self.feed is not None:
            self.closed = True
            threading.Thread(target=self.close_day, args=(today,), name="flow-close", daemon=True).start()
            return "close"
        return None
