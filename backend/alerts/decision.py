"""Annotated "decision chart" for alerts: chart + numbered markers + decision panel.

decision_context() computes everything the picture and the message say (verdict,
✓/✗ checklist, levels, what to watch, optional position size) from the candles,
using the same engine as the screens, so the image always matches the alert.
render_decision_chart() draws it:

  ① pivot line (trigger)          shaded buy zone: pivot .. +buy_zone_pct
  ②③④… pullbacks with depths      S stop line (last pullback low − buffer, ≤ max_stop under pivot)
  V volume dry-up (base shaded)   ▲ breakout callout · "now" marker · 50/200-day lines
  E exit day (played out) · LIVE partial candle (intraday, unconfirmed)

The levels and checks are the screen's rules, not recommendations (personal use).
"""

import io
from math import floor
from typing import Optional

import numpy as np

from backend.alerts import charts
from backend.config import AlertsConfig, BaseConfig, VcpConfig
from backend.engine.bases import detect_bases
from backend.engine.indicators import sma
from backend.engine.vcp import evaluate

BG, PANEL, GRID, TEXT, MUTED = "#0d1117", "#12161c", "#222a33", "#e6edf3", "#8b949e"
UP, DOWN, ACC, BLUE, ORANGE = "#26a69a", "#ef5350", "#f5c518", "#4ea1ff", "#f5a524"

BREAKOUT_KINDS = {"breakout", "breakout_low_volume", "below_pivot", "played_out", "climbing"}
STAGE_TEXT = {"breakout": "FRESH BREAKOUT", "breakout_low_volume": "BREAKOUT ON LOW VOLUME",
              "below_pivot": "BACK BELOW THE PIVOT", "played_out": "PLAYED OUT", "climbing": "RECLAIMED THE PIVOT",
              "forming": "FORMING — waiting for a close above the pivot",
              "near_pivot": "NEAR THE PIVOT — a close above it is a breakout",
              "intraday": "INTRADAY — above the pivot, UNCONFIRMED until the close",
              "close_check": "15:15 CHECK — is the breakout holding into the close?"}
LIVE_KINDS = {"intraday", "close_check"}


def _rs(v):
    return f"₹{v:,.2f}"


def decision_context(candles: list[dict], kind: str, *, symbol: str, company: str = "", industry: str = "",
                     rs: Optional[int] = None, tt_passed: int = 0, tt_of: int = 8,
                     bases_cfg: BaseConfig, vcp_cfg: VcpConfig, alerts_cfg: AlertsConfig,
                     ended: Optional[dict] = None, live: Optional[dict] = None,
                     size_factor: float = 1.0, size_note: str = "") -> Optional[dict]:
    """Everything the decision chart and message need, or None if there is no base.

    live: intraday {ltp, open, high, low, volume} for today's still-forming candle; for close_check also
          {pace, range, verdict} from backend.alerts.intraday.close_verdict.
    ended: played out {date, reason, gain_pct, peak_pct}.
    """
    bases = detect_bases(candles, bases_cfg)
    if not bases:
        return None
    if kind in BREAKOUT_KINDS:
        base = next((b for b in reversed(bases) if b.breakout is not None), None)
        if base is None:
            return None
        end = base.end_index
    else:
        base, end = bases[-1], len(candles) - 1
    v = evaluate(candles, base, rs, tt_passed, vcp_cfg, end=end)
    close = live["ltp"] if live else candles[-1]["close"]
    pivot = base.pivot
    stop = v.stop if v.stop else pivot * (1 - vcp_cfg.max_stop)
    zone_top = pivot * (1 + alerts_cfg.buy_zone_pct)
    closes = np.array([c["close"] for c in candles], dtype=float)
    ma50 = float(sma(closes, 50)[-1]) if len(closes) >= 50 else None
    ext = (close / pivot - 1) * 100
    depths = [k["depth_pct"] for k in v.contractions]

    checks = [(tt_passed >= vcp_cfg.min_trend_checks, f"Trend template {tt_passed}/{tt_of}"),
              (rs is not None and rs >= vcp_cfg.min_rs, f"RS rating {rs if rs is not None else 'n/a'}"),
              (bool(v.shrinking), "Pullbacks shrinking " + (" → ".join(f"{d:.0f}%" for d in depths) or "(none)")),
              ((v.dryup or 9) <= vcp_cfg.max_dryup, f"Volume dried up {v.dryup if v.dryup is not None else 0:.2f}× "
                                                    f"(≤ {vcp_cfg.max_dryup:g})"),
              ((v.tightening or 9) <= vcp_cfg.max_tightening,
               f"Range tightening {v.tightening if v.tightening is not None else 0:.2f}× (≤ {vcp_cfg.max_tightening:g})")]
    bo = base.breakout
    if bo is not None and kind in BREAKOUT_KINDS:
        checks += [(bo.volume_ok, f"Breakout volume {bo.volume_ratio or 0:.1f}× (≥ 1.5)"),
                   (not bo.weak_close, "Closed in upper half" + (f" (range {bo.close_in_range:.2f})"
                                                                if bo.close_in_range is not None else ""))]
    if kind == "close_check" and live:
        checks += [(live.get("pace") is not None and live["pace"] >= alerts_cfg.intraday_volume_mult,
                    f"Volume on pace for {live.get('pace') or 0:.1f}× (≥ {alerts_cfg.intraday_volume_mult:g})"),
                   (live.get("range") is not None and live["range"] >= alerts_cfg.close_min_range,
                    f"Trading in upper part of today's range ({live.get('range') or 0:.2f})")]
    if kind in ("breakout", "breakout_low_volume", "climbing") or kind in LIVE_KINDS:
        checks.append((ext <= alerts_cfg.buy_zone_pct * 100, f"Not extended: {ext:+.1f}% vs pivot "
                                                             f"(≤ {alerts_cfg.buy_zone_pct * 100:g}%)"))

    if kind in ("breakout", "climbing"):
        verdict, vcolor = ("Breakout ✓" + (" · VCP ✓" if v.qualifies else " · not a VCP")), (UP if v.qualifies else ACC)
        watch = [f"Valid while it closes above {_rs(pivot)}.",
                 f"Exit on a close below {_rs(stop)} or the 50-day line.",
                 f"Above {_rs(zone_top)} it's extended: don't chase."]
    elif kind == "breakout_low_volume":
        verdict, vcolor = "Breakout on low volume ⚠", ACC
        watch = [f"Volume {bo.volume_ratio or 0:.1f}× is below the 1.5× rule.",
                 "Wait for volume to confirm, or a fresh base.", f"Fails on a close below {_rs(pivot)}."]
    elif kind == "below_pivot":
        verdict, vcolor = "Failed breakout so far ✗", DOWN
        watch = [f"Reclaim: a close back above {_rs(pivot)}.", f"Exit on a close below {_rs(stop)}.",
                 "Failed breakouts often fall further."]
    elif kind == "played_out":
        verdict, vcolor = "Run ended", MUTED
        e = ended or {}
        watch = [f"Ended {e.get('date', '')}: {e.get('reason', '')}.",
                 f"Result {e.get('gain_pct', 0):+.1f}% vs pivot, peak {e.get('peak_pct', 0):+.1f}%.",
                 "Wait for a new base before acting again."]
    elif kind == "intraday":
        verdict, vcolor = "Above the pivot — UNCONFIRMED", ACC
        watch = [f"Needs a CLOSE above {_rs(pivot)} today on ≥ 1.5× volume.",
                 f"Above {_rs(zone_top)} it's extended.", "The evening scan confirms or rejects it."]
    elif kind == "close_check":
        state = (live or {}).get("verdict")
        if state == "holding":
            verdict, vcolor = "Holding into the close ✓", UP
            watch = [f"Buy zone {_rs(pivot)} – {_rs(zone_top)}, before 15:30.",
                     f"Stop {_rs(stop)}: exit on a close below it.", "The 18:30 scan confirms it on the close."]
        elif state == "extended":
            verdict, vcolor = "Extended — don't chase ✗", DOWN
            watch = [f"{ext:+.1f}% above the pivot, beyond the buy zone ({_rs(zone_top)}).",
                     "Wait for a pullback toward the pivot.", "Buying here widens the stop and the risk."]
        else:
            verdict, vcolor = "Above the pivot, but weak ⚠", ACC
            watch = ["Volume or the close may not confirm the breakout.",
                     "Wait for the 18:30 confirmation or a better day.", f"Counts only on a close above {_rs(pivot)}."]
    else:                                                         # forming / near_pivot
        verdict = "VCP forming ✓" if v.qualifies else "Base forming (not a VCP)"
        vcolor = UP if v.qualifies else ACC
        fail_at = v.contractions[-1]["low"] if v.contractions else stop
        watch = [f"Trigger: a CLOSE above {_rs(pivot)} on ≥ 1.5× volume.",
                 f"Setup fails on a close below {_rs(fail_at)} (last pullback low).",
                 f"Now {abs(ext):.1f}% under the pivot."]

    size = None
    if alerts_cfg.capital > 0 and alerts_cfg.risk_pct > 0:
        entry = max(close, pivot) if kind in BREAKOUT_KINDS | LIVE_KINDS else pivot
        per_share = entry - stop
        if per_share > 0:
            qty = floor(alerts_cfg.capital * alerts_cfg.risk_pct * size_factor / per_share)
            size = {"qty": qty, "entry": entry, "risk_rs": qty * per_share, "value": qty * entry, "note": size_note}

    return {"symbol": symbol, "company": company, "industry": industry, "kind": kind, "base": base, "vcp": v,
            "pivot": pivot, "stop": stop, "zone_top": zone_top, "close": close, "ext_pct": ext,
            "risk_pct": (close - stop) / close * 100, "ma50": ma50, "checks": checks, "verdict": verdict,
            "verdict_color": vcolor, "watch": watch, "size": size, "ended": ended, "live": live,
            "date": candles[-1]["date"]}


def decision_lines(ctx: dict) -> list[str]:
    """Short 'Why / Watch / Levels' lines for the Telegram caption (plain text; caller escapes)."""
    ok = [t for good, t in ctx["checks"] if good]
    bad = [t for good, t in ctx["checks"] if not good]
    lines = [f"Verdict: {ctx['verdict']}"]
    if ok:
        lines.append("Why: " + "; ".join(ok[:3]))
    if bad:
        lines.append("Caution: " + "; ".join(bad[:2]))
    lines.append(f"Levels: pivot {_rs(ctx['pivot'])} · buy zone to {_rs(ctx['zone_top'])} · stop {_rs(ctx['stop'])} "
                 f"({-ctx['risk_pct']:.1f}% from close)")
    lines.append("Watch: " + " ".join(ctx["watch"][:2]))
    if ctx["size"]:
        s = ctx["size"]
        lines.append(f"Size: ~{s['qty']} shares at {_rs(s['entry'])} risks {_rs(s['risk_rs'])}"
                     + (f" ({s['note']})" if s.get("note") else ""))
    return lines


def _badge(ax, x, y, label, color, dx=0, dy=14):
    return ax.annotate(str(label), (x, y), xytext=(dx, dy), textcoords="offset points", ha="center", va="center",
                       fontsize=10, fontweight="bold", color="#111",
                       bbox=dict(boxstyle="circle,pad=0.25", fc=color, ec="none"))


def _place(ax, taken: list, text: str, xy: tuple, offsets: list, **kw):
    """Annotate at the first offset whose box clears the boxes in `taken` and stays inside the axes."""
    renderer = ax.figure.canvas.get_renderer()
    frame = ax.get_window_extent(renderer)
    ann = None
    for i, off in enumerate(offsets):
        if ann is not None:
            ann.remove()
        ann = ax.annotate(text, xy, xytext=off, textcoords="offset points", **kw)
        box = ann.get_window_extent(renderer).expanded(1.15, 1.35)
        inside = frame.x0 <= box.x0 and box.x1 <= frame.x1 and frame.y0 <= box.y0 and box.y1 <= frame.y1
        if inside and not any(box.overlaps(t) for t in taken):
            break                                             # else the last offset is kept
    taken.append(ann.get_window_extent(renderer).expanded(1.15, 1.35))
    return ann


def render_decision_chart(candles: list[dict], ctx: dict, bars: int = 110) -> bytes:
    if not charts.CHARTS_AVAILABLE:
        raise RuntimeError("matplotlib is not installed: run .venv/bin/pip install -r requirements.txt")
    plt, Rectangle = charts.plt, charts.Rectangle
    live = ctx.get("live")
    data = list(candles)
    if live:                                                      # today's still-forming candle
        data.append({"date": "LIVE", "open": live["open"], "high": live["high"], "low": live["low"],
                     "close": live["ltp"], "volume": live.get("volume") or 0})
    closes = np.array([c["close"] for c in data], dtype=float)
    ma50, ma200 = sma(closes, 50), sma(closes, 200)
    view = data[-bars:]
    off = len(data) - len(view)
    n = len(view)
    idx = {c["date"]: i for i, c in enumerate(view)}
    o = np.array([c["open"] for c in view]); h = np.array([c["high"] for c in view])
    lo = np.array([c["low"] for c in view]); cl = np.array([c["close"] for c in view])
    vol = np.array([c["volume"] for c in view], dtype=float)
    x = np.arange(n)
    up = cl >= o
    base, v, pivot, stop = ctx["base"], ctx["vcp"], ctx["pivot"], ctx["stop"]

    fig = plt.figure(figsize=(12.5, 7.6), dpi=110)
    fig.patch.set_facecolor(BG)
    gs = fig.add_gridspec(5, 3, width_ratios=[2.1, 0.03, 1], hspace=0.05, wspace=0.02,
                          left=0.05, right=0.985, top=0.87, bottom=0.07)
    ax = fig.add_subplot(gs[0:4, 0])
    axv = fig.add_subplot(gs[4, 0], sharex=ax)
    axp = fig.add_subplot(gs[:, 2])
    for a in (ax, axv):
        a.set_facecolor(PANEL)
        a.grid(color=GRID, linewidth=0.6)
        a.tick_params(colors=MUTED, labelsize=9)
        for s in a.spines.values():
            s.set_color(GRID)
    axp.set_facecolor(BG)
    axp.axis("off")

    x0 = idx.get(base.start_date, 0 if base.start_date < view[0]["date"] else None)
    x1 = idx.get(base.end_date)
    if x0 is not None and x1 is not None:
        ax.add_patch(Rectangle((x0 - .5, base.low), x1 - x0 + 1, pivot - base.low, fc=UP, alpha=.08, ec=UP, ls=":", lw=1))
    ax.axhspan(pivot, ctx["zone_top"], color=UP, alpha=.13, lw=0)
    ax.text(n * 0.62, ctx["zone_top"], "buy zone", color=UP, fontsize=9, ha="center", va="bottom")
    ax.axhline(pivot, color=UP, ls="--", lw=1.3)
    ax.text(1.2, pivot, f" pivot {_rs(pivot)}", color=UP, fontsize=10, va="bottom", fontweight="bold")
    _badge(ax, 0.5, pivot, 1, UP, dx=-2, dy=0)
    ax.axhline(stop, color=DOWN, ls="--", lw=1.1)
    ax.text(1.2, stop, f" stop {_rs(stop)}", color=DOWN, fontsize=10, va="top", fontweight="bold")
    _badge(ax, 0.5, stop, "S", DOWN, dx=-2, dy=0)
    ax.plot(x, ma50[off:], color=BLUE, lw=1.1, label="50-day")
    ax.plot(x, ma200[off:], color=ORANGE, lw=1.1, label="200-day")
    ax.vlines(x, lo, h, color=np.where(up, UP, DOWN), lw=.9)
    ax.bar(x, np.abs(cl - o) + 1e-9, bottom=np.minimum(o, cl), color=np.where(up, UP, DOWN), width=.68)

    ax.legend(loc="upper left", fontsize=9, facecolor=PANEL, edgecolor=GRID, labelcolor=TEXT)
    marks, mark = [], 2
    for k in v.contractions:
        if k["high_date"] in idx and k["low_date"] in idx:
            a_, b_ = idx[k["high_date"]], idx[k["low_date"]]
            ax.plot([a_, b_], [k["high"], k["low"]], color=ACC, lw=1.7)
            marks.append(ax.text(b_, k["low"], f"−{k['depth_pct']:.0f}%", color=ACC, fontsize=10, ha="center",
                                 va="top", fontweight="bold"))
            marks.append(_badge(ax, b_, k["low"], mark, ACC, dy=-26))
            mark += 1
    ax.set_xlim(-1.5, n + 0.5)
    ax.set_ylim(*ax.get_ylim())                                     # freeze limits so boxes can be measured
    renderer = fig.canvas.get_renderer()
    taken = [m.get_window_extent(renderer) for m in marks] + [ax.get_legend().get_window_extent(renderer)]
    from matplotlib.transforms import Bbox
    taken += [Bbox(ax.transData.transform([(i - .45, lo[i]), (i + .45, h[i])])) for i in range(n)]   # candles

    kind = ctx["kind"]
    if base.breakout is not None and kind in BREAKOUT_KINDS and base.breakout.date in idx:
        b = idx[base.breakout.date]
        bo = base.breakout
        _place(ax, taken, f"breakout {bo.date[5:]}\n{bo.through_pct:+.1f}% · {bo.volume_ratio or 0:.1f}× vol",
               (b, h[b]), [(-100, 26), (-140, 10), (-100, 60), (-200, 40), (-150, -40)], color=TEXT, fontsize=9,
               arrowprops=dict(arrowstyle="->", color=UP), bbox=dict(boxstyle="round,pad=.3", fc="#0f2a26", ec=UP),
               annotation_clip=False)
    ended = ctx.get("ended") or {}
    if kind == "played_out" and ended.get("date") in idx:
        e = idx[ended["date"]]
        taken.append(_badge(ax, e, lo[e], "E", DOWN, dy=-16).get_window_extent(renderer))
        _place(ax, taken, f"exit {ended['date'][5:]}\n{ended.get('reason', '')}", (e, lo[e]),
               [(-110, -40), (-110, -80), (-200, -60), (-160, -130), (-220, -110), (-260, -170), (-110, 40)], color=TEXT, fontsize=9,
               arrowprops=dict(arrowstyle="->", color=DOWN), bbox=dict(boxstyle="round,pad=.3", fc="#2a1414", ec=DOWN))
    last_i = n - 1
    now_label = ("LIVE " if live else "now ") + f"{_rs(cl[-1])}\n{ctx['ext_pct']:+.1f}% vs pivot"
    first = (-92, -46) if cl[-1] >= pivot else (-92, 30)
    _place(ax, taken, now_label, (last_i, cl[-1]), [first, (-92, -46), (-92, 30), (-150, -90), (-150, 70), (-200, 0),
                                                    (-120, -140), (-220, -150), (-300, -190)],
           color=TEXT, fontsize=9, arrowprops=dict(arrowstyle="->", color=ACC if live else MUTED),
           bbox=dict(boxstyle="round,pad=.3", fc=PANEL, ec=ACC if live else MUTED))

    axv.bar(x, vol, color=np.where(up, UP + "88", DOWN + "88"), width=.68)
    if x0 is not None and x1 is not None:
        axv.axvspan(x0 - .5, x1 + .5, color=ACC, alpha=.08)
        right_side = x0 > n * 0.6
        label = f"volume dry-up {v.dryup:.2f}× " if v.dryup is not None else "base volume "
        axv.text(x1 if right_side else x0, vol.max() * .82, label if right_side else " " + label, color=ACC,
                 fontsize=9, fontweight="bold", ha="right" if right_side else "left")
    axv.set_yticks([])
    ticks = list(range(0, n, max(1, n // 6)))
    axv.set_xticks(ticks)
    axv.set_xticklabels([view[i]["date"][5:] if view[i]["date"] != "LIVE" else "live" for i in ticks])
    plt.setp(ax.get_xticklabels(), visible=False)

    head_color = UP if kind in ("breakout", "climbing") else DOWN if kind in ("below_pivot", "played_out") else ACC
    fig.text(.05, .94, f"{ctx['symbol']} · {ctx['company']}", color=TEXT, fontsize=16, fontweight="bold")
    fig.text(.05, .9, f"{STAGE_TEXT.get(kind, kind)}   ·   {base.label}   ·   {ctx['industry']}   ·   "
                      f"data to {ctx['date']}", color=head_color, fontsize=10.5)

    y = [0.99]

    def line(txt, color=TEXT, size=10.5, bold=False, gap=.041):
        axp.text(0, y[0], txt, color=color, fontsize=size, fontweight="bold" if bold else "normal",
                 transform=axp.transAxes, va="top")
        y[0] -= gap

    line("VERDICT", MUTED, 9, True, .034)
    line(ctx["verdict"], ctx["verdict_color"], 13.5, True, .05)
    line("CHECKLIST", MUTED, 9, True, .034)
    for good, text in ctx["checks"]:
        line(("✓  " if good else "✗  ") + text, UP if good else DOWN)
    y[0] -= .008
    line("LEVELS", MUTED, 9, True, .034)
    line(f"①  Pivot / trigger   {_rs(pivot)}", UP)
    line(f"     Buy zone          {_rs(pivot)} – {_rs(ctx['zone_top'])}", UP)
    line(f"S   Stop               {_rs(stop)}  ({-ctx['risk_pct']:.1f}% from close)", DOWN)
    if ctx["ma50"]:
        line(f"     50-day line       {_rs(ctx['ma50'])}  (trend-break exit)", BLUE)
    if ctx["size"]:
        s = ctx["size"]
        line(f"     Size ≈ {s['qty']} sh @ {_rs(s['entry'])} · risk {_rs(s['risk_rs'])}", TEXT)
    y[0] -= .008
    line("WHAT TO WATCH", MUTED, 9, True, .034)
    for w in ctx["watch"]:
        line(w, TEXT, 10, gap=.037)
    axp.text(0, 0.0, "Screen rules, not advice · personal use", color=MUTED, fontsize=8, transform=axp.transAxes)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()
