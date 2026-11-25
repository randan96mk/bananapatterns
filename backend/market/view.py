"""📈 What the page shows: the "Market now" panel at the top of Research › Tips (Calls) and the "Market read" block on a
hint card. Every number is from code (chart.py) or from a stored candle-model read with its version; the model's
numbers appear only when reads.gate() says cards (shadow: the chip only). The [AI] lines come from words.py."""

from datetime import datetime
from typing import Optional

from backend.config import IST
from backend.market import candles as cd
from backend.market import chart
from backend.market import reads as rd
from backend.timefmt import ampm

AGREE, AGAINST = 0.55, 0.45                   # the model's vote: p_up ≥ 55 % up, ≤ 45 % down, else none
VERDICTS = ("supports the call", "against the call", "mixed")


def fmt(v, idx: bool = True) -> str:
    """An index level as a whole number (25,812), a premium with up to 2 decimals; '—' for none."""
    if v is None:
        return "—"
    f = float(v)
    if idx and abs(f) >= 1000:
        return f"{round(f):,}"
    return f"{f:,.0f}" if f.is_integer() else f"{f:,.2f}".rstrip("0").rstrip(".")


def hm(t: Optional[str]) -> str:
    """'09:52' -> '9:52 AM' (12-hour IST)."""
    return ampm(t) if t else ""


def pct(p: Optional[float]) -> str:
    return "—" if p is None else f"{round(p * 100):d} %"


def index_facts(symbol: str, now: Optional[datetime] = None) -> Optional[dict]:
    """The index's chart summary (3 sessions of 1-minute bars): computed once per symbol and day every FACTS_S seconds,
    not once per card (25 cards × 3 sessions was most of a 78-s page on 25 Nov)."""
    now = now or datetime.now(IST)
    day = now.date().isoformat()
    return rd.cached(("index_facts", symbol, day), lambda: _index_facts(symbol, day), FACTS_S)


FACTS_S = 20.0


def _index_facts(symbol: str, day: str) -> Optional[dict]:
    bars = cd.index_bars(symbol, 3, day)
    return chart.summary(bars, symbol) if bars else None


def trend_text(tr: dict) -> str:
    """'higher highs since 9:35 · 20 > 50 avg · opening range broken at 9:52'."""
    bits = []
    if tr.get("hh_ll"):
        bits.append(f"{tr['hh_ll']['kind']} since {hm(tr['hh_ll']['since'])}")
    if tr.get("avg_cross"):
        bits.append(f"{tr['avg_cross']} avg")
    o = tr.get("or") or {}
    if o.get("break_at"):
        bits.append(f"opening range broken {o['break_dir']} at {hm(o['break_at'])}")
    elif o.get("complete"):
        bits.append(f"inside the opening range {fmt(o['low'])}–{fmt(o['high'])}")
    return " · ".join(bits) or "no clear swing yet"


def trend_head(tr: dict) -> str:
    vw = "above VWAP" if tr.get("above_vwap") else "below VWAP" if tr.get("above_vwap") is False else "VWAP —"
    return f"{tr['side']} · {vw}"


def call_dir(h: dict) -> Optional[str]:
    """The index direction the call wants: a CE buy / PE sell wants up, a PE buy / CE sell down."""
    side, typ = (h.get("side") or "").upper(), (h.get("opt_type") or "").upper()
    if side not in ("BUY", "SELL"):
        return None
    if typ in ("CE", "PE"):
        return "up" if (side == "BUY") == (typ == "CE") else "down"
    return "up" if side == "BUY" else "down"


def model_vote(p_up: Optional[float]) -> Optional[str]:
    if p_up is None:
        return None
    return "up" if p_up >= AGREE else "down" if p_up <= AGAINST else None


def verdict(trend_side: Optional[str], want: Optional[str], p_up: Optional[float], use_model: bool) -> str:
    """By code: the trend with / against the call, and (cards only) the model's vote ≥ 55 % / ≤ 45 %."""
    t = None if trend_side in (None, "flat") or want is None else trend_side == want
    m = model_vote(p_up) if use_model else None
    mv = None if m is None or want is None else m == want
    if t is None:
        return VERDICTS[0] if mv is True else VERDICTS[1] if mv is False else VERDICTS[2]
    if mv is None or mv == t:
        return VERDICTS[0] if t else VERDICTS[1]
    return VERDICTS[2]


def align(trend_side: Optional[str], want: Optional[str]) -> Optional[str]:
    """with / against / None (a flat trend or no direction)."""
    if trend_side in (None, "flat") or want is None:
        return None
    return "with" if trend_side == want else "against"


def model_tile(config, r: Optional[dict], g: dict, label: str, sc: list) -> dict:
    """The candle model tile: {state off|none|shadow|shown, head, tone, p_up, sub, chip}."""
    if not rd.on(config):
        return {"state": "off", "head": "off", "sub": "Settings › market · kronos", "tone": ""}
    if r is None:
        return {"state": "none", "head": "no model read", "sub": "the worker is down or has not read yet · nothing else changes", "tone": ""}
    if not g["cards"]:
        return {"state": "shadow", "head": "in shadow", "sub": g["why"], "tone": "", "at_label": rd.t12(r["at"])}
    p = r.get("paths_json") or {}
    up = r["p_up"] >= 0.5
    return {"state": "shown", "head": f"{pct(r['p_up'] if up else 1 - r['p_up'])} {'up' if up else 'down'}", "tone": "up" if up else "down",
            "p_up": r["p_up"], "sub": f"from {p.get('context') or '—'} one-minute candles · {p.get('samples') or '—'} sampled paths · {(r.get('ms') or 0) / 1000:.1f} s",
            "version": r["version"], "at_label": rd.t12(r["at"]), "label": label}


def panel(config, symbol: str = "NIFTY", now: Optional[datetime] = None, client=None) -> dict:
    """GET /api/market/now: the four tiles, the backdrop ([AI] lines + the numbers line), the chip, the status."""
    from backend.market import words
    now = now or datetime.now(IST)
    symbol = symbol.upper() if symbol and symbol.upper() in cd.INDICES else "NIFTY"
    h = int(config.market.horizon_min)
    sc = rd.scorecard()
    g = rd.gate(config, sc)
    st = rd.status()
    out = {"symbol": symbol, "indices": list(cd.INDICES), "on": rd.on(config), "mode": g["mode"], "cards": g["cards"], "gate": g["why"],
           "chip": rd.chip(config, symbol, 30, sc), "horizon": h, "interval_s": int(config.market.interval_s),
           "worker": rd.health(config, 0.3) if rd.on(config) else {"state": "off"}, "login": None, "tiles": None, "backdrop": None}
    if client is not None and not cd.logged_in(client):
        out["login"] = cd.LOGIN_TEXT
    elif st.get("why") == cd.LOGIN_TEXT:
        out["login"] = cd.LOGIN_TEXT
    if not rd.on(config):
        out["off"] = "the candle model is off (Settings › market · kronos) · the chart facts need its candles, so nothing is fetched"
        return out
    f = index_facts(symbol, now)
    if not f:
        out["empty"] = out["login"] or "no candles yet · they come every interval from 9:15 AM"
        return out
    tr, lv = f["trend"], f["levels"]
    r = rd.latest(symbol, h)
    vx = rd.vix() or {}
    vchg = round((vx["last"] - vx["prev"]) / vx["prev"] * 100, 1) if vx.get("last") and vx.get("prev") else None
    prev = lv.get("prev") or {}
    out["tiles"] = {
        "trend": {"label": "trend · 5 min", "head": trend_head(tr), "tone": "up" if tr["side"] == "up" else "down" if tr["side"] == "down" else "",
                  "sub": trend_text(tr)},
        "levels": {"label": "levels", "head": f"{fmt(lv.get('support'))} / {fmt(lv.get('resistance'))}",
                   "sub": f"support · resistance from swings · prev close {fmt(prev.get('c'))} · day high {fmt((lv.get('day') or {}).get('h'))} · day low {fmt((lv.get('day') or {}).get('l'))}"},
        "volatility": {"label": "volatility", "head": f"VIX {vx['last']:.1f}" if vx.get("last") else "VIX —",
                       "chg": (f"{'+' if vchg >= 0 else '−'}{abs(vchg):.1f} %" if vchg is not None else None), "tone": "up" if (vchg or 0) > 0 else "down" if (vchg or 0) < 0 else "",
                       "sub": f"ATR 5 min {fmt(f['atr'], False)} pts · expected {h}-min range ±{fmt(f['exp_range'][str(h)], False)} pts"},
        "model": {**model_tile(config, r, g, f"next {h} min", sc), "label": f"candle model · next {h} min"},
    }
    nums = [f"VWAP {fmt(tr.get('vwap'))}", f"20 avg {fmt(tr.get('avg20'))}", f"50 avg {fmt(tr.get('avg50'))}"]
    o = tr.get("or") or {}
    if o.get("high") is not None:
        nums.append(f"OR high {fmt(o['high'])}")
    nums += [f"S {fmt(lv.get('support'))}", f"R {fmt(lv.get('resistance'))}"]
    if vx.get("last"):
        nums.append(f"VIX {vx['last']:.2f}")
    if g["cards"] and r:
        nums.append(f"model {pct(r['p_up'])} up · {r['version']}")
    out["backdrop"] = {**words.panel_view(symbol), "numbers": " · ".join(nums) + " · all from code"}
    out["refreshed"] = rd.t12(r["at"]) if r else ampm(now)
    out["at_label"] = hm(tr.get("at"))
    out["facts"] = {"bars": f["bars"], "day": f["day"]}
    return out


def card_facts(config, h: dict, now: Optional[datetime] = None) -> Optional[dict]:
    """The code facts of one hint's market read (index tiles, levels vs the strike, the pattern, the premium's expected
    range), the latest hint read and the code verdict. None for a hint not on NIFTY / BANKNIFTY / SENSEX."""
    idx = rd.hint_index(h)
    if not idx:
        return None
    now = now or datetime.now(IST)
    f = index_facts(idx, now)
    want = call_dir(h)
    reads = rd.hint_reads(h["id"])
    hz = 15
    r = next((x for x in reversed(reads) if x["horizon"] == hz), None)
    idx_r = rd.latest(idx, 30)
    g = rd.gate(config)
    use = g["cards"]
    p_up_call = None
    if r is not None and (r.get("paths_json") or {}).get("basis") == "premium":
        # a premium read: up = the option's price up = the call's way for a buy, against it for a sell
        p_up_call = r["p_up"] if (h.get("side") or "BUY") == "BUY" else 1 - r["p_up"]
    trs = (f or {}).get("trend") or {}
    model_p = None
    if r is not None and (r.get("paths_json") or {}).get("basis") == "index":
        model_p = r["p_up"]
    elif idx_r is not None:
        model_p = idx_r["p_up"]
    v = verdict(trs.get("side"), want, model_p if use else None, use)
    lv = rd.hint_levels(h)
    return {"index": idx, "facts": f, "want": want, "align": align(trs.get("side"), want), "read": r, "index_read": idx_r, "gate": g,
            "verdict": v, "levels": lv, "model_p": model_p, "p_up_call": p_up_call}


def _touch(r: Optional[dict], level) -> Optional[float]:
    if r is None or level is None:
        return None
    from backend.market.paths import level_key
    return (r.get("touch_json") or {}).get(level_key(level))


def card(config, h: dict, now: Optional[datetime] = None) -> Optional[dict]:
    """The hint card's "Market read" block, or None (not an index option / the market read is off with no facts)."""
    from backend.market import words
    from backend.tips import kite_orders as ko
    cf = card_facts(config, h, now)
    if not cf:
        return None
    f = cf["facts"]
    g = cf["gate"]
    sc = rd.scorecard()
    out = {"index": cf["index"], "on": rd.on(config), "mode": g["mode"], "cards": g["cards"], "chip": rd.chip(config, cf["index"], 30, sc),
           "verdict": cf["verdict"], "tiles": None, "fan": None, "lines": [], "numbers": None, "entry": None}
    if not f:
        out["empty"] = cd.LOGIN_TEXT if rd.status().get("why") == cd.LOGIN_TEXT else "no index candles yet"
        if not rd.on(config):
            out["empty"] = "the market read is off (Settings › market · kronos)"
        return out
    tr, lv = f["trend"], f["levels"]
    strike = h.get("strike")
    pat = f.get("pattern")
    o = tr.get("or") or {}
    near = []
    last = tr["last"]
    if lv.get("resistance") is not None:
        near.append(("R", lv["resistance"], lv["resistance"] - last))
    if lv.get("support") is not None:
        near.append(("S", lv["support"], lv["support"] - last))
    head_lv = min(near, key=lambda x: abs(x[2])) if near else None
    other = [x for x in near if x is not head_lv]
    otm = None
    if strike is not None and h.get("opt_type") in ("CE", "PE"):
        d = (float(strike) - last) if h["opt_type"] == "CE" else (last - float(strike))
        otm = f"strike {fmt(strike)} is {round(abs(d))} pts {'OTM' if d > 0 else 'ITM'}"
    tone_al = {"with": "up", "against": "down"}.get(cf["align"] or "", "")
    out["tiles"] = {
        "index": {"label": "index", "head": {"with": "with the call", "against": "against the call"}.get(cf["align"] or "", "flat · no side"), "tone": tone_al,
                  "sub": f"{cf['index']} {tr['side']} · {'above' if tr.get('above_vwap') else 'below'} VWAP" + (f" · OR broken {hm(o['break_at'])}" if o.get("break_at") else "")},
        "levels": {"label": "nearest levels",
                   "head": f"{head_lv[0]} {fmt(head_lv[1])} · {'+' if head_lv[2] >= 0 else '−'}{round(abs(head_lv[2]))}" if head_lv else "—",
                   "sub": " · ".join([f"{x[0]} {fmt(x[1])} · {'+' if x[2] >= 0 else '−'}{round(abs(x[2]))}" for x in other] + ([otm] if otm else [])) or "no swing level near"},
        "pattern": {"label": "pattern · 5 min", "head": (f"{pat['name']} · {hm(pat['start'])}–{hm(pat['at'])}" if pat.get("start") else pat["name"]) if pat else "none today",
                    "sub": pat["text"] if pat else "no engulfing, pin bar, inside bar, soldiers / crows, doji or flag yet"},
    }
    r = cf["read"]
    hz = (r or {}).get("horizon") or 15
    mt = model_tile(config, r, g, f"{hz} min", sc)
    if mt["state"] == "shown":
        st, t1 = _touch(r, cf["levels"].get("stop")), _touch(r, cf["levels"].get("t1"))
        basis = (r.get("paths_json") or {}).get("basis")
        if basis == "premium":
            mt["head"] = f"{pct(cf['p_up_call'])} {'up' if (h.get('side') or 'BUY') == 'BUY' else 'down'}"
            mt["tone"] = "up" if (cf["p_up_call"] or 0) >= 0.5 else "down"
        mt["stop_first"], mt["t1_first"] = st, t1
        mt["sub"] = (f"stop level touched first: {pct(st)} · T1 first: {pct(t1)}" if st is not None or t1 is not None else mt["sub"]) + f" · {r['version']}"
        p = r.get("paths_json") or {}
        if basis == "premium" and p.get("q50"):
            out["fan"] = {"q10": p.get("q10"), "q50": p.get("q50"), "q90": p.get("q90"), "last": p.get("last"), "stop": cf["levels"].get("stop"),
                          "t1": cf["levels"].get("t1"), "history": [b["c"] for b in cd.premium_bars(h["id"])][-60:], "at_label": rd.t12(r["at"]), "horizon": hz}
    out["tiles"]["model"] = {**mt, "label": f"candle model · {hz} min"}
    nums = [f"VWAP {fmt(tr.get('vwap'))}"]
    if lv.get("support") is not None:
        nums.append(f"S {fmt(lv['support'])}")
    if lv.get("resistance") is not None:
        nums.append(f"R {fmt(lv['resistance'])}")
    if o.get("high") is not None:
        nums.append(f"OR high {fmt(o['high'])}")
    if pat:
        nums.append(f"{pat['name']} {hm(pat.get('start') or pat['at'])}")
    nums.append(f"ATR 5 min {fmt(f['atr'], False)}")
    if g["cards"] and r:
        nums.append(f"model up {pct(r['p_up'])}")
        if mt.get("stop_first") is not None:
            nums.append(f"stop-first {pct(mt['stop_first'])}")
        if mt.get("t1_first") is not None:
            nums.append(f"T1-first {pct(mt['t1_first'])}")
        nums.append(f"{(r.get('paths_json') or {}).get('samples')} paths · {r['version']}")
    out["numbers"] = " · ".join(nums) + " · all from code"
    wv = words.card_view(h["id"])
    out["lines"], out["state"], out["at_label"], out["words_text"] = wv["lines"], wv["state"], wv.get("at_label"), wv.get("text")
    k = ko.kstate(h)
    out["entry"] = entry_timing(config, h, cf, k)
    return out


def entry_timing(config, h: dict, cf: dict, k: dict) -> Optional[dict]:
    """Only for a zone / level entry not filled yet: the code line (cards: x % of paths touch the zone top in 15 min;
    shadow: the zone vs now and the expected range) + the [AI] line. Informational: auto behaviour is unchanged."""
    from backend.market import words
    from backend.tips import plan as pl
    e = (h.get("plan") or {}).get("entry") or h.get("entry") or {}
    if k.get("entry_px") is not None or h.get("status") not in ("new", "waiting", "active", "paused") or e.get("mode") in (None, "market") or pl.is_spot(e):
        return None
    top = cf["levels"].get("zone_top")
    if top is None:
        return None
    ltp = (h.get("run") or {}).get("last_ltp")
    zone = (h.get("plan") or {}).get("entry_text") or pl.entry_text(e)
    r = cf["read"]
    line = None
    if cf["gate"]["cards"] and r is not None and (r.get("paths_json") or {}).get("basis") == "premium":
        p = ((r.get("paths_json") or {}).get("touch_any") or {}).get(f"{float(top):g}")
        if p is not None:
            line = f"pullback into the zone {'likely' if p >= 0.5 else 'unlikely'}: {pct(p)} of paths touch the zone top {fmt(top, False)} in {r['horizon']} min"
    if line is None:
        b5 = chart.five(cd.premium_bars(h["id"]))
        a = chart.atr(b5)
        er = chart.expected_range(a, 15)
        gap = None if ltp is None else round(float(ltp) - float(top), 2)
        line = (f"zone top {fmt(top, False)}" + (f" is {fmt(abs(gap), False)} pts {'under' if gap >= 0 else 'over'} now {fmt(ltp, False)}" if gap is not None else "")
                + (f" · the premium's expected 15-min range ±{fmt(er, False)}" if er is not None else " · the premium's range needs more ticks"))
    wv = words.entry_view(h["id"])
    return {"zone": zone, "now": ltp, "code": line, "lines": wv["lines"], "state": wv["state"], "text": wv.get("text"),
            "note": f"{zone} · now {fmt(ltp, False) if ltp is not None else '—'} · informational, auto still waits for the zone"}
