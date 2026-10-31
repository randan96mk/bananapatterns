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
                out.append({**it, "price": px, "sides": ctx.sides()})
        return out, drops


def _keep(it, sides, drops, reason):
    """The item with only the sides that passed, or None (counted under `reason`)."""
    sides = [x for x in it["sides"] if x in sides]
    if not sides:
        drops[reason] += 1
        return None
    return {**it, "sides": sides}


@module("daily_trend")
class DailyTrend(Module):
    title, icon = "Daily trend", "📈"
    fields = [("above_50", "Long: close above 50-day avg", "bool", True), ("above_200", "Long: close above 200-day avg", "bool", True),
              ("mirror", "Short: mirror (below the averages)", "bool", True)]

    def summary(self, cfg):
        on = [x for x, k in (("50", "above_50"), ("200", "above_200")) if cfg[k]]
        return (("> " + " & ".join(on) + " DMA" if on else "off") + (" · shorts below" if cfg["mirror"] else "")
                + " · saved daily history")

    def run(self, ctx, items, cfg):
        out, drops = [], Counter()
        for it in items:
            d, px = ctx.daily.get(it["symbol"]) or {}, it["price"]
            ok = []
            m50, m200 = d.get("sma50"), d.get("sma200")
            if (not cfg["above_50"] or (m50 and px > m50)) and (not cfg["above_200"] or (m200 and px > m200)):
                ok.append("long")
            if not cfg["mirror"] or ((not cfg["above_50"] or (m50 and px < m50)) and (not cfg["above_200"] or (m200 and px < m200))):
                ok.append("short")
            k = _keep(it, ok, drops, "against the daily trend")
            if k:
                out.append(k)
        return out, drops


@module("intraday_filters")
class IntradayFilters(Module):
    title, icon = "Intraday filters", "📊"
    fields = [("vwap", "Long above / short below VWAP", "bool", True),
              ("rsi_on", "RSI(14) in range (short: mirrored)", "bool", True), ("rsi_min", "RSI min", "number", 55, {"min": 0, "max": 100}),
              ("rsi_max", "RSI max", "number", 75, {"min": 0, "max": 100}),
              ("rvol_on", "Relative volume", "bool", True), ("rvol_min", "RVOL ≥", "number", 1.5, {"min": 0, "max": 10, "step": 0.1}),
              ("ema_on", "EMA 9 vs EMA 21", "bool", False), ("st_on", "Supertrend (10,3) with the trade", "bool", False)]

    def summary(self, cfg):
        bits = [x for x, k in (("VWAP", "vwap"), (f"RSI {cfg['rsi_min']}–{cfg['rsi_max']}", "rsi_on"),
                               (f"RVOL ≥ {cfg['rvol_min']}", "rvol_on"), ("EMA 9/21", "ema_on"), ("Supertrend", "st_on")) if cfg[k]]
        return " · ".join(bits) or "off"

    def run(self, ctx, items, cfg):
        out, drops = [], Counter()
        for it in items:
            bars, today = ctx.bars(it), ctx.today_bars(it)
            closes, px = [b["c"] for b in bars], it["price"]
            v = ind.vwap(today) if cfg["vwap"] else None
            r = ind.rsi(closes) if cfg["rsi_on"] else None
            rv = ind.rvol(bars) if cfg["rvol_on"] else None
            if cfg["rvol_on"] and (rv is None or rv < cfg["rvol_min"]):
                drops["low volume"] += 1
                continue
            e9 = ind.ema(closes, 9) if cfg["ema_on"] else None
            e21 = ind.ema(closes, 21) if cfg["ema_on"] else None
            st = ind.supertrend_up(bars) if cfg["st_on"] else None
            ok, why = [], ""
            for side in it["sides"]:
                lg = side == "long"
                if cfg["vwap"] and (v is None or (px <= v if lg else px >= v)):
                    why = why or "wrong side of VWAP"
                elif cfg["rsi_on"] and (r is None or not ((cfg["rsi_min"] <= r <= cfg["rsi_max"]) if lg
                                                          else (100 - cfg["rsi_max"] <= r <= 100 - cfg["rsi_min"]))):
                    why = why or "RSI out of range"
                elif cfg["ema_on"] and not (e9 and e21 and (e9 > e21 if lg else e9 < e21)):
                    why = why or "EMA 9/21 against"
                elif cfg["st_on"] and (st is None or st != lg):
                    why = why or "Supertrend against"
                else:
                    ok.append(side)
            k = _keep(it, ok, drops, why or "filtered")
            if k:
                out.append({**k, "vwap": round(v, 2) if v else None, "rsi": round(r, 1) if r else None,
                            "rvol": round(rv, 2) if rv else None})
        return out, drops


@module("context")
class MarketContext(Module):
    title, icon = "Market context", "🌍"
    fields = [("direction", "Trades allowed", "select", "both", {"options": ["both", "long", "short"]}),
              ("follow", "Follow NIFTY (long above / short below its VWAP)", "bool", True),
              ("ema_on", "…and its 20-EMA", "bool", False),
              ("vix_on", "India VIX in range", "bool", True), ("vix_min", "VIX min", "number", 10, {"min": 0, "max": 80}),
              ("vix_max", "VIX max", "number", 22, {"min": 0, "max": 80}),
              ("breadth_on", "Breadth (% above VWAP) with the trade", "bool", False),
              ("breadth_min", "Breadth % ≥", "number", 50, {"min": 0, "max": 100})]

    def summary(self, cfg):
        bits = [cfg["direction"]]
        if cfg["follow"]:
            bits.append("follows NIFTY vs VWAP" + (" & 20-EMA" if cfg["ema_on"] else ""))
        if cfg["vix_on"]:
            bits.append(f"VIX {cfg['vix_min']}–{cfg['vix_max']}")
        if cfg["breadth_on"]:
            bits.append(f"breadth ≥ {cfg['breadth_min']}%")
        return " · ".join(bits)

    def run(self, ctx, items, cfg):
        sides = ["long", "short"] if cfg["direction"] == "both" else [cfg["direction"]]
        m = ctx.market()
        why = None
        if cfg["vix_on"] and m.get("vix") is not None and not cfg["vix_min"] <= m["vix"] <= cfg["vix_max"]:
            sides, why = [], "VIX out of range"
        if cfg["follow"] and m.get("nifty") is not None and m.get("nifty_vwap"):
            up = m["nifty"] > m["nifty_vwap"] and (not cfg["ema_on"] or (m.get("nifty_ema") and m["nifty"] > m["nifty_ema"]))
            dn = m["nifty"] < m["nifty_vwap"] and (not cfg["ema_on"] or (m.get("nifty_ema") and m["nifty"] < m["nifty_ema"]))
            sides = [x for x in sides if (x == "long" and up) or (x == "short" and dn)]
            why = why or "against NIFTY"
        if cfg["breadth_on"] and m.get("breadth") is not None:
            b = m["breadth"]
            sides = [x for x in sides if (b >= cfg["breadth_min"] if x == "long" else 100 - b >= cfg["breadth_min"])]
            why = why or "breadth against"
        out, drops = [], Counter()
        for it in items:
            k = _keep(it, sides, drops, why or "market context")
            if k:
                out.append(k)
        return out, drops


@module("and")
class And(Module):
    title, icon = "AND", "⋂"

    def summary(self, cfg):
        return "passes stocks (and sides) that every input passed"


@module("orb")
class OpeningRangeBreakout(Module):
    title, icon = "Setup: ORB", "🎯"
    fields = [("minutes", "Opening range (minutes)", "select", 15, {"options": [5, 15, 30]})]

    def summary(self, cfg):
        return f"close above / below the first {cfg['minutes']} min range"

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
            side = "long" if "long" in it["sides"] and last["c"] > hi else "short" if "short" in it["sides"] and last["c"] < lo else None
            if not side:
                drops["no breakout"] += 1
                continue
            out.append({**it, "side": side, "signal": "BUY" if side == "long" else "SELL", "entry": last["c"],
                        "or_high": round(hi, 2), "or_low": round(lo, 2), "signal_bar": last, "setup": "ORB"})
        return out, drops


@module("score")
class ScoreRank(Module):
    title, icon = "Score & rank", "🏅"
    fields = [("top_k", "Keep top K a candle", "number", 3, {"min": 1, "max": 20}),
              ("w_rvol", "Weight: relative volume", "number", 1, {"min": 0, "max": 5, "step": 0.5}),
              ("w_rs", "Weight: strength vs NIFTY", "number", 1, {"min": 0, "max": 5, "step": 0.5}),
              ("w_rsi", "Weight: RSI", "number", 0.5, {"min": 0, "max": 5, "step": 0.5})]

    def summary(self, cfg):
        return f"top {cfg['top_k']} by RVOL ×{cfg['w_rvol']} · strength ×{cfg['w_rs']} · RSI ×{cfg['w_rsi']}"

    def run(self, ctx, items, cfg):
        nifty = ctx.market().get("nifty_chg") or 0
        for it in items:
            sgn = 1 if it["side"] == "long" else -1
            rs = sgn * ((ctx.day_change(it) or 0) - nifty)
            rsi = sgn * ((it.get("rsi") or 50) - 50) / 10
            it["score"] = round(cfg["w_rvol"] * (it.get("rvol") or 1) + cfg["w_rs"] * rs + cfg["w_rsi"] * rsi, 2)
        ranked = sorted(items, key=lambda x: x["score"], reverse=True)
        keep = ranked[:cfg["top_k"]]
        return keep, ({"below the top K": len(ranked) - len(keep)} if len(ranked) > len(keep) else {})


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


@module("stop")
class StopTarget(Module):
    title, icon = "Stop & target", "🛡"
    fields = [("stop", "Stop", "select", "orb", {"options": ["orb", "atr", "signal_bar", "vwap", "percent"]}),
              ("atr_mult", "ATR × (atr stop)", "number", 1.5, {"min": 0.5, "max": 5, "step": 0.1}),
              ("stop_pct", "% (percent stop)", "number", 0.8, {"min": 0.1, "max": 5, "step": 0.1}),
              ("min_stop_pct", "Skip if stop under %", "number", 0.2, {"min": 0, "max": 5, "step": 0.05}),
              ("max_stop_pct", "Skip if stop over %", "number", 2.5, {"min": 0.2, "max": 10, "step": 0.1}),
              ("target_r", "Target × risk (0 = none)", "number", 2, {"min": 0, "max": 10, "step": 0.5}),
              ("partial_on", "Book part early", "bool", False), ("partial_r", "…at × risk", "number", 1, {"min": 0.5, "max": 5, "step": 0.5}),
              ("partial_pct", "…% of the quantity", "number", 50, {"min": 10, "max": 90, "step": 10}),
              ("trail", "Trailing stop", "select", "breakeven", {"options": ["off", "breakeven", "prev_bar", "atr", "percent"]}),
              ("trail_after_r", "Trail after × risk", "number", 1, {"min": 0, "max": 5, "step": 0.5}),
              ("trail_atr_mult", "Trail ATR ×", "number", 2, {"min": 0.5, "max": 5, "step": 0.5}),
              ("trail_pct", "Trail %", "number", 0.5, {"min": 0.1, "max": 5, "step": 0.1}),
              ("time_stop_min", "Time stop (minutes, 0 = off)", "number", 60, {"min": 0, "max": 360, "step": 5}),
              ("time_stop_r", "…unless up × risk", "number", 0.5, {"min": 0, "max": 3, "step": 0.25}),
              ("squareoff", "Square-off", "select", "15:15", {"options": ["14:45", "15:00", "15:10", "15:15", "15:20"]})]

    def summary(self, cfg):
        tgt = f"target {cfg['target_r']}R" if cfg["target_r"] else "no target"
        extra = f" · book {cfg['partial_pct']}% at {cfg['partial_r']}R" if cfg["partial_on"] else ""
        return (f"SL {cfg['stop']} · {tgt}{extra} · trail {cfg['trail']}"
                + (f" · time stop {cfg['time_stop_min']}m" if cfg["time_stop_min"] else "") + f" · out {cfg['squareoff']}")

    def run(self, ctx, items, cfg):
        out, drops = [], Counter()
        for it in items:
            lg, e, bar = it["side"] == "long", it["entry"], it["signal_bar"]
            atr = ind.atr(ctx.bars(it))
            st = cfg["stop"]
            if st == "orb":
                sl = it["or_low"] if lg else it["or_high"]
            elif st == "atr":
                sl = e - cfg["atr_mult"] * atr if atr and lg else e + cfg["atr_mult"] * atr if atr else None
            elif st == "signal_bar":
                sl = bar["l"] if lg else bar["h"]
            elif st == "vwap":
                sl = it.get("vwap")
            else:
                sl = e * (1 - cfg["stop_pct"] / 100) if lg else e * (1 + cfg["stop_pct"] / 100)
            if sl is None or (sl >= e if lg else sl <= e):
                drops["no valid stop"] += 1
                continue
            pct = abs(e - sl) / e * 100
            if pct < cfg["min_stop_pct"]:
                drops["stop too tight"] += 1
                continue
            if pct > cfg["max_stop_pct"]:
                drops["stop too wide"] += 1
                continue
            risk = abs(e - sl)
            tgt = (e + cfg["target_r"] * risk if lg else e - cfg["target_r"] * risk) if cfg["target_r"] else None
            plan = {k: cfg[k] for k in ("partial_on", "partial_r", "partial_pct", "trail", "trail_after_r", "trail_atr_mult",
                                        "trail_pct", "time_stop_min", "time_stop_r", "squareoff")}
            plan["atr"] = atr
            out.append({**it, "sl": round(sl, 2), "target": round(tgt, 2) if tgt else None, "plan": plan,
                        "stop_pct": round(pct, 2)})
        return out, drops


@module("size")
class SizeRisk(Module):
    title, icon = "Size & risk", "📏"
    fields = [("capital", "Capital ₹", "number", 100000, {"min": 10000, "max": 100000000, "step": 10000}),
              ("risk_pct", "Risk a trade (% of capital)", "number", 0.5, {"min": 0.05, "max": 5, "step": 0.05}),
              ("max_pos_pct", "Max position (% of capital)", "number", 20, {"min": 1, "max": 500, "step": 1}),
              ("max_open", "Max open positions", "number", 3, {"min": 1, "max": 20}),
              ("max_trades", "Max trades a day", "number", 6, {"min": 1, "max": 50}),
              ("daily_loss_pct", "Daily loss limit (% of capital)", "number", 1.5, {"min": 0.1, "max": 20, "step": 0.1})]

    def summary(self, cfg):
        c = cfg["capital"]
        return (f"₹{c:,.0f} · risk ₹{c * cfg['risk_pct'] / 100:,.0f} a trade · max ₹{c * cfg['max_pos_pct'] / 100:,.0f} a position · "
                f"{cfg['max_open']} open · {cfg['max_trades']} a day · stop for the day at −₹{c * cfg['daily_loss_pct'] / 100:,.0f}")

    def run(self, ctx, items, cfg):
        out, drops = [], Counter()
        c = cfg["capital"]
        st = ctx.positions()
        if st["realised"] <= -c * cfg["daily_loss_pct"] / 100:
            return [], {"daily loss limit hit": len(items)} if items else {}
        n_open, n_day = st["open"], st["trades"]
        for it in items:
            if n_day >= cfg["max_trades"]:
                drops["max trades a day"] += 1
                continue
            if n_open >= cfg["max_open"]:
                drops["max open positions"] += 1
                continue
            per = abs(it["entry"] - it["sl"])
            qty = int(min(c * cfg["risk_pct"] / 100 / per, c * cfg["max_pos_pct"] / 100 / it["entry"]))
            if qty < 1:
                drops["too small for the risk"] += 1
                continue
            out.append({**it, "qty": qty, "risk_rs": round(qty * per, 2), "value": round(qty * it["entry"], 2)})
            n_open += 1
            n_day += 1
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
        return "mode Paper: fills at the next 1-minute open + 0.05% slippage, intraday costs · Real: phase 4"

    def run(self, ctx, items, cfg):
        if ctx.mode != "paper":
            return [], ({"signals only (mode)": len(items)} if items else {})
        for it in items:
            ctx.open_paper(it)
        return items, {}


@module("monitor")
class Monitor(Module):
    title, icon = "Position monitor", "👁"

    def summary(self, cfg):
        return "every 1-minute close: stop · target · partial · trailing · time stop · square-off"
