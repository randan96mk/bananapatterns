"""🧭 Strategy journey: explains the latest live candle's run (read-only, nothing is traded or saved).

  summary()          each box's in → out with its drop reasons, one sentence on why there was (no) trade, the stocks
                     closest to the trigger, and the strategy's rules in plain words (from the boxes' settings)
  node_stocks(nid)   who came into a box, who passed (with values and where each is now), who dropped and why
  check(symbol)      one stock through every box: pass / drop / waiting with its values, and what a trade would be

A drop's reason is found when asked, by running that box again for that one stock on the same candle's data (the
alert and execute boxes are never re-run: they send messages and open paper trades)."""


from backend.intraday import flow
from backend.intraday import indicators as ind
from backend.intraday.modules import REGISTRY, SKIPPABLE

WAITING = {"no breakout", "range not complete"}
NO_RERUN = {"and", "alert", "execute", "monitor"}
KEEP = ("price", "side", "sides", "setup", "vwap", "rsi", "rvol", "or_high", "or_low", "pdh", "pdl", "entry", "score", "sl", "target", "qty",
        "risk_rs", "stop_pct")


def _trace():
    return flow._state.get("trace")


def _nodes(tr):
    return flow.template(tr.get("key"))["nodes"]


def _title(kind):
    if kind == "and":
        return "All three passed"
    m = REGISTRY.get(kind)
    return m.title if m else "All three passed"


def _vals(it: dict) -> dict:
    return {k: it[k] for k in KEEP if it.get(k) is not None}


def _single(tr, nid, kind, it):
    """(passed item or None, reason) for one stock in one box, on the traced candle."""
    if kind in NO_RERUN or (kind in SKIPPABLE and tr["cfgs"][nid].get("skip")):
        return it, None
    out, drops = REGISTRY[kind]().run(tr["ctx"], [it], tr["cfgs"][nid])
    if out:
        return out[0], None
    return None, next(iter(drops), "dropped")


def _where(tr, sym: str) -> str:
    """Where a stock is now: the first box it came into but did not pass, or how far it went."""
    last = None
    for nid, kind, _, _ in _nodes(tr):
        if kind == "monitor":
            continue
        ins = {x["symbol"] for x in tr["ins"].get(nid, [])}
        outs = {x["symbol"] for x in tr["outs"].get(nid, [])}
        if sym in ins and sym not in outs:
            return f"stopped at {_title(kind)}"
        if sym in outs:
            last = kind
    return "signal sent" if last in ("alert", "execute") else f"passed {_title(last)}" if last else "not in the universe"


def rules(cfgs: dict) -> list:
    """The strategy in plain words, from the boxes' settings: [{title, lines}]."""
    g = lambda nid: cfgs.get(nid, {})
    tr, un, td, fi, cx, orb, sc, dd, st, sz = (g(x) for x in ("trigger", "universe", "trend", "filters", "context", "orb",
                                                               "score", "dedup", "stop", "size"))
    entry = [f"On each {tr.get('interval')}-minute candle close from {tr.get('start')} to {tr.get('end')}."]
    entry.append(f"One of the {un.get('top')} most traded stocks, price ≥ ₹{un.get('min_price')}"
                 + (f"; only the {un.get('in_play_top')} with the most unusual first-{un.get('in_play_min')}-minute volume"
                    if un.get("in_play") else "") + ".")
    on = [x for x, k in (("50-day", "above_50"), ("200-day", "above_200")) if td.get(k)]
    entry.append(f"Daily trend: long above its {' and '.join(on)} average" + (", short below" if td.get("mirror") else "")
                 + "." if on else "Daily trend: not used.")
    f = []
    if fi.get("vwap"):
        f.append("long above VWAP, short below")
    if fi.get("rsi_on"):
        f.append(f"RSI {fi.get('rsi_min')}–{fi.get('rsi_max')} (short: {100 - fi.get('rsi_max', 0)}–{100 - fi.get('rsi_min', 0)})")
    if fi.get("rvol_on"):
        f.append(f"volume ≥ {fi.get('rvol_min')}× usual")
    if fi.get("ema_on"):
        f.append("EMA 9 vs 21 with the trade")
    if fi.get("st_on"):
        f.append("Supertrend with the trade")
    entry.append("Today: " + (", ".join(f) if f else "no intraday filter") + ".")
    m = [f"trades allowed: {cx.get('direction')}"]
    if cx.get("follow"):
        m.append("long only while NIFTY is above its VWAP, short only below")
    if cx.get("long_ema"):
        m.append("longs also need NIFTY above its 20-EMA")
    if cx.get("vix_on"):
        m.append(f"India VIX {cx.get('vix_min')}–{cx.get('vix_max')}")
    if cx.get("breadth_on"):
        m.append(f"breadth ≥ {cx.get('breadth_min')}% with the trade")
    entry.append("Market: " + ", ".join(m) + ".")
    from backend.intraday.modules import Setups
    on = Setups.ticked(orb)
    need = min(max(1, int(orb.get("min_count") or 1)), len(on))
    say = {"set_orb": f"ORB: closes beyond the first {orb.get('minutes')} minutes' high (long) / low (short)"
                      + (", only in that range's direction" if orb.get("with_open") else ""),
           "set_pdh": "previous day high / low: closes above yesterday's high (long) / below its low (short)",
           "set_vwap": f"VWAP reclaim: dips to within {orb.get('vwap_dip')}% of VWAP and closes back on its side",
           "set_nbar": f"N-candle breakout: closes beyond the last {orb.get('nbar_n')} candles' high / low",
           "set_ema": "EMA cross: the 9 EMA crosses the 21 EMA on that candle"}
    entry.append(("Trigger: " if len(on) == 1 else f"Trigger: at least {need} of these {len(on)} on the same candle, "
                  "same side (long and short together: skipped): ") + "; ".join(say[k] for k, _ in on)
                 + (f"; no signals from candles starting before {orb['from_candle']}" if orb.get("from_candle", "09:15") > "09:15" else "")
                 + ".")
    entry.append(f"The best {sc.get('top_k')} a candle (relative volume, strength vs NIFTY, RSI) · 1 per stock a day · "
                 f"at most {dd.get('per_day')} signals a day.")
    cap = sz.get("capital", 0)
    size = [f"Risk {sz.get('risk_pct')}% of ₹{cap:,.0f} a trade (₹{cap * sz.get('risk_pct', 0) / 100:,.0f}), at most "
            f"{sz.get('max_pos_pct')}% of the capital a position, {sz.get('max_open')} open at once, {sz.get('max_trades')} "
            f"trades a day; no new trade after losing {sz.get('daily_loss_pct')}% (₹{cap * sz.get('daily_loss_pct', 0) / 100:,.0f}) "
            "in a day."]
    stops = {"orb": "the other side of the opening range", "setup": f"the fired setup's own level ({st.get('setup_stop')})", "atr": f"{st.get('atr_mult')} × ATR from the entry",
             "daily_atr": f"{st.get('daily_atr_frac')} × the daily ATR from the entry", "signal_bar": "the signal candle's low / high",
             "vwap": "VWAP", "percent": f"{st.get('stop_pct')}% from the entry"}
    ex = [f"Stop: {stops.get(st.get('stop'), st.get('stop'))} (skipped if under {st.get('min_stop_pct')}% or over "
          f"{st.get('max_stop_pct')}% away)."]
    ex.append(f"Target: {st.get('target_r')} × the risk." if st.get("target_r") else "Target: none (held until square-off or the stop).")
    if st.get("partial_on"):
        ex.append(f"Book {st.get('partial_pct')}% at {st.get('partial_r')} × the risk.")
    if st.get("trail") and st.get("trail") != "off":
        how = {"breakeven": "stop to the entry", "prev_bar": "under the previous candle", "atr": f"{st.get('trail_atr_mult')} × ATR under the best price",
               "percent": f"{st.get('trail_pct')}% under the best price"}.get(st.get("trail"), st.get("trail"))
        ex.append(f"Trailing: after {st.get('trail_after_r')} × the risk, {how}.")
    if st.get("time_stop_min"):
        ex.append(f"Time stop: out after {st.get('time_stop_min')} minutes unless up {st.get('time_stop_r')} × the risk.")
    ex.append(f"Square-off: everything out at {st.get('squareoff')}.")
    ex.append(f"Entry: a limit at the signal price, valid {st.get('limit_min')} minutes (filled only at that price or better, "
              "no slippage; cancelled if not filled or if it opens beyond the stop)." if st.get("limit_on") else
              "Entry: the next 1-minute open + slippage.")
    ex.append("Paper fills include intraday charges.")
    for nid, prefix, label in (("trend", "Daily trend", "Daily trend"), ("filters", "Today:", "Today's filters"),
                               ("context", "Market:", "Market"), ("score", None, "Score & rank"), ("dedup", None, "Dedup")):
        if g(nid).get("skip"):
            if prefix:
                entry = [f"{label}: ⏭ skipped (every stock passes on)." if x.startswith(prefix) else x for x in entry]
            else:
                entry.append(f"{label}: ⏭ skipped (no ranking / no daily limit).")
    return [{"title": "📥 Entry: all of these", "lines": entry}, {"title": "📏 Size", "lines": size},
            {"title": "📤 Exit: whichever comes first", "lines": ex}]


def _range(tr, it, minutes):
    today = tr["ctx"].today_bars(it)
    end = 9 * 60 + 15 + minutes
    rng = [b for b in today if int(b["t"][:2]) * 60 + int(b["t"][3:]) < end]
    if len(rng) < minutes:
        return None, None
    return max(b["h"] for b in rng), min(b["l"] for b in rng)


def summary() -> dict:
    cfgs = flow.node_configs()
    tr = _trace()
    out = {"rules": rules(cfgs), "at": None}
    if not tr:
        out["headline"] = "No live run yet today: the flow runs at each candle close in market hours (or press ▶ Run now)."
        out["funnel"], out["closest"] = [], []
        return out
    funnel = []
    for nid, kind, _, _ in _nodes(tr):
        r = tr["res"].get(nid)
        if r and kind not in ("trigger", "monitor"):
            funnel.append({"id": nid, "kind": kind, "title": _title(kind), "in": r["in"], "out": r["out"], "drops": r["drops"]})
    stop = next((x for x in funnel if x["out"] == 0 and x["in"] > 0), None)
    signals = tr["res"].get("alert", {}).get("out", 0)
    # the stocks that reached the trigger: how far each is from it
    orb_cfg = tr["cfgs"].get("orb", {})
    closest = []
    for it in tr["ins"].get("orb", []):
        hi, lo = _range(tr, it, orb_cfg.get("minutes", 15))
        last = tr["ctx"].last_closed(it)
        if hi is None or not last:
            continue
        c = last["c"]
        for side in it.get("sides", []):
            trig = hi if side == "long" else lo
            away = (trig - c) / c * 100 if side == "long" else (c - trig) / c * 100
            closest.append({"symbol": it["symbol"], "side": side, "last": round(c, 2), "trigger": round(trig, 2),
                            "away_pct": round(away, 2), "rvol": it.get("rvol")})
    closest.sort(key=lambda x: x["away_pct"])
    if signals:
        head = f"{signals} signal(s) on this candle."
    elif stop:
        why = ", ".join(f"{k} {v}" for k, v in list(stop["drops"].items())[:2])
        prev = next((x for x in funnel[::-1] if x["out"] > 0 and funnel.index(x) < funnel.index(stop)), None)
        head = (f"No trade on this candle: {stop['in']} stock(s) reached {stop['title']} and none passed ({why})."
                + (f" {prev['out']} had passed {prev['title']}." if prev else ""))
        if stop["kind"] == "orb" and closest:
            x = closest[0]
            head += f" Closest: {x['symbol']} {abs(x['away_pct']):.2f}% {'under' if x['side'] == 'long' else 'over'} its trigger."
    else:
        head = "No stock reached the end on this candle."
    out.update(at=tr["t"], day=tr["day"], funnel=funnel, headline=head, closest=closest[:25])
    return out


def node_stocks(nid: str) -> dict:
    tr = _trace()
    if not tr:
        return {"error": "no live run yet"}
    kind = next((k for n, k, _, _ in _nodes(tr) if n == nid), None)
    if kind is None:
        return {"error": "no such box"}
    ins = tr["ins"].get(nid, [])
    outs = {x["symbol"]: x for x in tr["outs"].get(nid, [])}
    batch = tr["res"].get(nid, {}).get("drops", {})
    passed = [{"symbol": s, **_vals(x), "now": _where(tr, s)} for s, x in outs.items()]
    dropped = []
    inputs = next(i for n, _, i, _ in _nodes(tr) if n == nid)
    for it in ins:
        if it["symbol"] in outs:
            continue
        if kind == "and":
            miss = [_title(k) for n, k, _, _ in _nodes(tr) if n in inputs
                    and it["symbol"] not in {x["symbol"] for x in tr["outs"].get(n, [])}]
            why = f"dropped at {', '.join(miss)}" if miss else "the sides do not agree (long in one, short in another)"
        elif kind in NO_RERUN:
            why = ", ".join(batch) or "dropped"
        else:
            _, why = _single(tr, nid, kind, it)
            why = why or ("limit / ranking: " + ", ".join(batch) if batch else "dropped")
        dropped.append({"symbol": it["symbol"], "reason": why, "waiting": why in WAITING or str(why).startswith("only "), **_vals(it)})
    return {"id": nid, "title": _title(kind), "at": tr["t"], "in": len(ins), "passed": passed, "dropped": dropped,
            "reasons": batch}


def check(symbol: str) -> dict:
    """One stock through every box on the traced candle, with its values."""
    tr = _trace()
    if not tr:
        return {"error": "no live run yet"}
    symbol = symbol.upper().strip()
    nodes = _nodes(tr)
    first = tr["ins"].get(nodes[0][0], [])
    base = next((x for x in first if x["symbol"] == symbol), None)
    if base is None:
        return {"symbol": symbol, "error": "not in the stock list (Nifty 500)"}
    ctx, cur, steps = tr["ctx"], {}, []
    for nid, kind, inputs, _ in nodes:
        if kind == "monitor":
            continue
        if not inputs:
            src = base
        elif kind == "and":
            got = [cur.get(i) for i in inputs]
            if any(g is None for g in got):
                cur[nid] = None
                steps.append({"id": nid, "title": _title(kind), "state": "skip", "why": "an earlier box dropped it"})
                continue
            sides = [s for s in got[0]["sides"] if all(s in g.get("sides", []) for g in got)]
            merged = {}
            for g in got:
                merged.update(g)
            src = {**merged, "sides": sides} if sides else None
            cur[nid] = src
            steps.append({"id": nid, "title": _title(kind), "state": "pass" if src else "drop",
                          "why": None if src else "the sides do not agree", "values": _vals(src or merged)})
            continue
        else:
            src = cur.get(inputs[0])
        if src is None:
            cur[nid] = None
            steps.append({"id": nid, "title": _title(kind), "state": "skip", "why": "not reached"})
            continue
        if kind in ("alert", "execute"):
            cur[nid] = src
            steps.append({"id": nid, "title": _title(kind), "state": "pass",
                          "why": "would alert" if kind == "alert" else "would open a paper trade (Paper mode)"})
            continue
        res, why = _single(tr, nid, kind, src)
        cur[nid] = res
        steps.append({"id": nid, "title": _title(kind), "state": "pass" if res else "wait" if (why in WAITING or str(why).startswith("only ")) else "drop",
                      "why": why, "values": _vals(res or src)})
    # the facts behind the rules, computed directly (also for the boxes a stock never reached)
    bars, today = ctx.bars(base), ctx.today_bars(base)
    closes = [b["c"] for b in bars]
    d = ctx.daily.get(symbol) or {}
    m = ctx.market()
    hi, lo = _range(tr, base, tr["cfgs"].get("orb", {}).get("minutes", 15))
    last = ctx.last_closed(base)
    facts = {"price": ctx.last_price(base), "last_close": last["c"] if last else None, "vwap": ind.vwap(today) if today else None,
             "rsi": ind.rsi(closes), "rvol": ind.rvol(bars), "sma50": d.get("sma50"), "sma200": d.get("sma200"),
             "atr14_daily": d.get("atr14"), "or_high": hi, "or_low": lo, "nifty": m.get("nifty"),
             "nifty_vwap": m.get("nifty_vwap"), "vix": m.get("vix")}
    facts = {k: round(v, 2) if isinstance(v, float) else v for k, v in facts.items()}
    stop = next((s for s in steps if s["state"] in ("drop", "wait")), None)
    verdict = ("passes every box: a signal" if not stop else
               f"waiting at {stop['title']}: {stop['why']}" if stop["state"] == "wait" else f"stopped at {stop['title']}: {stop['why']}")
    return {"symbol": symbol, "at": tr["t"], "steps": steps, "facts": facts, "verdict": verdict}


def rerun(nid: str) -> dict:
    """↻ Re-run a box and every box after it on the latest candle's data, with the boxes' current settings (a dry run:
    the alert box sends nothing and the execute box opens nothing). The canvas counts and the journey show the result
    until the next candle's live run."""
    import time as _time
    from collections import Counter
    tr = _trace()
    if not tr:
        return {"error": "no live run yet: re-run works on the latest candle's data"}
    nodes = _nodes(tr)
    if nid not in {n for n, *_ in nodes}:
        return {"error": "no such box"}
    cfgs = flow.node_configs(tr.get("key"))
    ins, outs, res = dict(tr["ins"]), dict(tr["outs"]), dict(tr["res"])
    redo = set()
    for n, kind, inputs, _ in nodes:                     # the box and everything downstream of it
        if n == nid or any(i in redo for i in inputs):
            redo.add(n)
    for n, kind, inputs, _ in nodes:
        if n not in redo or kind == "monitor":
            continue
        t0 = _time.perf_counter()
        if kind == "and":
            sets = [{x["symbol"] for x in outs.get(i, [])} for i in inputs]
            keep = set.intersection(*sets) if sets else set()
            merged = {}
            for i in inputs:
                for x in outs.get(i, []):
                    if x["symbol"] in keep:
                        m = merged.get(x["symbol"])
                        sides = [y for y in (m or x)["sides"] if y in x.get("sides", [])] if m else x.get("sides", [])
                        merged[x["symbol"]] = {**(m or {}), **x, "sides": sides}
            src = list({x["symbol"]: x for i in inputs for x in outs.get(i, [])}.values())
            passed, drops = [x for x in merged.values() if x["sides"]], {}
            n_in = sum(len(outs.get(i, [])) for i in inputs)
        else:
            src = ins.get(n, []) if not inputs else outs.get(inputs[0], [])
            n_in = len(src)
            if kind in ("alert", "execute") or (kind in SKIPPABLE and cfgs[n].get("skip")):   # dry run / skipped
                passed, drops = src, {}
            else:
                passed, drops = REGISTRY[kind]().run(tr["ctx"], src, cfgs[n])
        ins[n], outs[n] = src, passed
        res[n] = {"status": "ok", "in": n_in, "out": len(passed), "ms": round((_time.perf_counter() - t0) * 1000),
                  "drops": dict(Counter(drops).most_common()), "sample": [x["symbol"] for x in passed[:8]], "dry": True}
        flow._state["nodes"][n] = res[n]
    tr.update(ins=ins, outs=outs, res=res, cfgs=cfgs, rerun={"from": nid, "at": flow.now_ist()})
    return {"from": nid, "boxes": sorted(redo, key=[n for n, *_ in nodes].index),
            "result": {n: {k: res[n][k] for k in ("in", "out", "drops")} for n in redo if n in res},
            "would_signal": res.get("alert", {}).get("out", 0)}
