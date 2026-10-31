"""Flow modules. Each module is registered with its title, icon, settings (with defaults and a simple schema the
screen turns into inputs) and run(ctx, items, cfg) -> (items passed on, {reason: count} dropped). Items are dicts
with at least symbol and token; a module may add fields (price, vwap, signal …) for the next ones. Add a module by
registering a new class; templates (flow.TEMPLATES) wire modules together."""

from collections import Counter

from backend.intraday import indicators as ind

REGISTRY: dict[str, type] = {}


def module(kind: str):
    def wrap(cls):
        cls.kind = kind
        REGISTRY[kind] = cls
        return cls
    return wrap


class Module:
    kind = ""
    title = ""
    icon = ""
    # [(key, label, type 'bool'|'number'|'select', default, extra {min, max, step, options})]
    fields: list = []

    @classmethod
    def defaults(cls) -> dict:
        return {k: d for k, _, _, d, *_ in cls.fields}

    @classmethod
    def schema(cls) -> list:
        return [{"key": k, "label": lbl, "type": t, "default": d, **(x[0] if x else {})} for k, lbl, t, d, *x in cls.fields]

    def summary(self, cfg: dict) -> str:
        return ""

    def run(self, ctx, items: list[dict], cfg: dict):
        return items, {}


@module("trigger")
class Trigger(Module):
    title, icon = "Trigger", "⏰"
    fields = [("interval", "Candle (minutes)", "select", 5, {"options": [1, 3, 5, 15]}),
              ("start", "First run", "select", "09:30", {"options": ["09:20", "09:25", "09:30", "09:45"]}),
              ("end", "Last new signal", "select", "14:30", {"options": ["13:30", "14:00", "14:30", "15:00"]})]

    def summary(self, cfg):
        return f"every {cfg['interval']}m close · {cfg['start']}–{cfg['end']}"


@module("universe")
class Universe(Module):
    title, icon = "Universe", "🧺"
    fields = [("top", "Top N by turnover", "number", 300, {"min": 20, "max": 500, "step": 10}),
              ("min_price", "Min price ₹", "number", 100, {"min": 0, "max": 5000, "step": 10})]

    def summary(self, cfg):
        return f"top {cfg['top']} turnover · price ≥ ₹{cfg['min_price']} · complete data only"

    def run(self, ctx, items, cfg):
        drops = Counter()
        ranked = sorted(items, key=lambda x: ctx.turnover.get(x["symbol"], 0), reverse=True)
        out = []
        for k, it in enumerate(ranked):
            px = ctx.last_price(it)
            if k >= cfg["top"]:
                drops["outside top turnover"] += 1
            elif px is None:
                drops["no live data"] += 1
            elif px < cfg["min_price"]:
                drops["price too low"] += 1
            else:
                out.append({**it, "price": px})
        return out, drops


@module("daily_trend")
class DailyTrend(Module):
    title, icon = "Daily trend", "📈"
    fields = [("above_50", "Close above 50-day avg", "bool", True), ("above_200", "Close above 200-day avg", "bool", True)]

    def summary(self, cfg):
        on = [x for x, k in (("50", "above_50"), ("200", "above_200")) if cfg[k]]
        return ("above " + " & ".join(on) + " DMA" if on else "off") + " · saved daily history"

    def run(self, ctx, items, cfg):
        out, drops = [], Counter()
        for it in items:
            d = ctx.daily.get(it["symbol"]) or {}
            if cfg["above_50"] and not (d.get("sma50") and it["price"] > d["sma50"]):
                drops["under 50-day avg"] += 1
            elif cfg["above_200"] and not (d.get("sma200") and it["price"] > d["sma200"]):
                drops["under 200-day avg"] += 1
            else:
                out.append(it)
        return out, drops


@module("intraday_filters")
class IntradayFilters(Module):
    title, icon = "Intraday filters", "📊"
    fields = [("vwap", "Price above VWAP", "bool", True),
              ("rsi_on", "RSI(14) in range", "bool", True), ("rsi_min", "RSI min", "number", 55, {"min": 0, "max": 100}),
              ("rsi_max", "RSI max", "number", 75, {"min": 0, "max": 100}),
              ("rvol_on", "Relative volume", "bool", True), ("rvol_min", "RVOL ≥", "number", 1.5, {"min": 0, "max": 10, "step": 0.1}),
              ("ema_on", "EMA 9 > EMA 21", "bool", False), ("st_on", "Supertrend (10,3) up", "bool", False)]

    def summary(self, cfg):
        bits = []
        if cfg["vwap"]:
            bits.append("above VWAP")
        if cfg["rsi_on"]:
            bits.append(f"RSI {cfg['rsi_min']}–{cfg['rsi_max']}")
        if cfg["rvol_on"]:
            bits.append(f"RVOL ≥ {cfg['rvol_min']}")
        if cfg["ema_on"]:
            bits.append("EMA 9 > 21")
        if cfg["st_on"]:
            bits.append("Supertrend up")
        return " · ".join(bits) or "off"

    def run(self, ctx, items, cfg):
        out, drops = [], Counter()
        for it in items:
            bars = ctx.bars(it)
            today = ctx.today_bars(it)
            closes = [b["c"] for b in bars]
            px = it["price"]
            v = ind.vwap(today) if cfg["vwap"] else None
            r = ind.rsi(closes) if cfg["rsi_on"] else None
            rv = ind.rvol(bars) if cfg["rvol_on"] else None
            if cfg["vwap"] and (v is None or px <= v):
                drops["below VWAP"] += 1
            elif cfg["rsi_on"] and (r is None or not cfg["rsi_min"] <= r <= cfg["rsi_max"]):
                drops["RSI out of range"] += 1
            elif cfg["rvol_on"] and (rv is None or rv < cfg["rvol_min"]):
                drops["low volume"] += 1
            elif cfg["ema_on"] and not ((e9 := ind.ema(closes, 9)) and (e21 := ind.ema(closes, 21)) and e9 > e21):
                drops["EMA 9 under 21"] += 1
            elif cfg["st_on"] and not ind.supertrend_up(bars):
                drops["Supertrend down"] += 1
            else:
                out.append({**it, "vwap": round(v, 2) if v else None, "rsi": round(r, 1) if r else None,
                            "rvol": round(rv, 2) if rv else None})
        return out, drops


@module("and")
class And(Module):
    title, icon = "AND", "⋂"

    def summary(self, cfg):
        return "passes stocks that every input passed"


@module("orb")
class OpeningRangeBreakout(Module):
    title, icon = "Setup: ORB", "🎯"
    fields = [("minutes", "Opening range (minutes)", "select", 15, {"options": [5, 15, 30]}),
              ("r_mult", "Target (× risk)", "number", 2, {"min": 0.5, "max": 5, "step": 0.5})]

    def summary(self, cfg):
        return f"close above the first {cfg['minutes']} min high · SL = range low · target {cfg['r_mult']}R"

    def run(self, ctx, items, cfg):
        out, drops = [], Counter()
        end = 9 * 60 + 15 + cfg["minutes"]           # minutes after midnight: 9:15 + the range
        for it in items:
            today = ctx.today_bars(it)
            rng = [b for b in today if int(b["t"][:2]) * 60 + int(b["t"][3:]) < end]
            last = ctx.last_closed(it)
            if len(rng) < cfg["minutes"] or not last:
                drops["range not complete"] += 1
                continue
            hi, lo = max(b["h"] for b in rng), min(b["l"] for b in rng)
            if last["c"] <= hi:
                drops["no breakout"] += 1
                continue
            risk = last["c"] - lo
            out.append({**it, "signal": "BUY", "entry": last["c"], "sl": round(lo, 2),
                        "target": round(last["c"] + cfg["r_mult"] * risk, 2), "or_high": round(hi, 2)})
        return out, drops


@module("dedup")
class Dedup(Module):
    title, icon = "Dedup", "🧹"
    fields = [("per_day", "Max signals a day", "number", 10, {"min": 1, "max": 100})]

    def summary(self, cfg):
        return f"1 per stock per day · max {cfg['per_day']} a day"

    def run(self, ctx, items, cfg):
        out, drops = [], Counter()
        seen, n = ctx.signalled_today(), len(ctx.signalled_today())
        for it in items:
            if it["symbol"] in seen:
                drops["already signalled today"] += 1
            elif n >= cfg["per_day"]:
                drops["daily limit"] += 1
            else:
                out.append(it)
                n += 1
        return out, drops


@module("alert")
class Alert(Module):
    title, icon = "Alert", "🔔"
    fields = [("telegram", "Telegram (loud)", "bool", True)]

    def summary(self, cfg):
        return ("Telegram loud" if cfg["telegram"] else "on screen only") + " · saved to today's signals"

    def run(self, ctx, items, cfg):
        for it in items:
            ctx.emit(it, telegram=cfg["telegram"])
        return items, {}


@module("execute")
class Execute(Module):
    title, icon = "Execute", "🚀"

    def summary(self, cfg):
        return "signals only (paper / real come in phase 2)"
