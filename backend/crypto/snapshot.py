"""₿ The chart PNG sent with an alert (matplotlib, headless; ≤ 900 px wide): the last SNAP_N candles, the pivot lines with
their labels, the EMA, the pattern candles boxed, the marker (green circle under a bullish pattern, red over a bearish
one), the stop and 1R lines, the caption. Saved as data/crypto/snaps/<id>.png (kept; archived with the signal rows,
never deleted). Without matplotlib: None (the alert goes as text)."""

from pathlib import Path
from typing import Optional

from backend.crypto import patterns as pt

SNAP_N = 60
W_IN, H_IN, DPI = 9.0, 4.6, 100            # 900 × 460 px
UP, DOWN, LEVEL, PCOL, EMA_C, TEXT, MUTED = "#1f9d61", "#d64545", "#e08a2e", "#9aa3ad", "#5b6cff", "#1b2430", "#9aa3ad"


def render(r: dict, candles: list[dict], caption: str, png_path: Path) -> Optional[Path]:
    from backend.alerts import charts
    if not charts.CHARTS_AVAILABLE:
        return None
    from backend.crypto.signals import px
    plt, Rect = charts.plt, charts.Rectangle
    f = r.get("facts_json") or {}
    i_last = next((i for i, b in enumerate(candles) if b["t"] == r["t"]), len(candles) - 1)
    cs = candles[max(0, i_last + 4 - SNAP_N): i_last + 5]          # a few candles after the pattern when there are any
    if not cs:
        return None
    closes = [b["c"] for b in candles]
    ema_all = pt.ema(closes, int(f.get("ema_len") or 20))
    off = candles.index(cs[0])
    ema = ema_all[off: off + len(cs)]
    long_ = r["dir"] == "long"
    pats = {b["t"] for b in (f.get("candles") or [])} or {r["t"]}
    lv = f.get("levels") or {}
    lo = min(min(b["l"] for b in cs), r["stop"] if long_ else r["r1"])
    hi = max(max(b["h"] for b in cs), r["r1"] if long_ else r["stop"])
    pad = (hi - lo) * 0.08 or 10
    show = {k: v for k, v in lv.items() if lo - pad * 3 <= v <= hi + pad * 3}
    if r.get("level_name") in lv:
        show[r["level_name"]] = lv[r["level_name"]]
    lo = min([lo] + list(show.values())) - pad
    hi = max([hi] + list(show.values())) + pad
    fig = plt.figure(figsize=(W_IN, H_IN), dpi=DPI)
    try:
        ax = fig.add_axes([0.02, 0.1, 0.84, 0.86])
        right = len(cs) + 6                                       # room right of the last candle for the stop / 1R labels
        ax.set_xlim(-1, right)
        ax.set_ylim(lo, hi)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.set_xticks([])
        ax.set_yticks([])
        for name, v in sorted(show.items(), key=lambda x: -x[1]):
            col = PCOL if name == "P" else LEVEL
            ax.axhline(v, color=col, lw=1, ls=(0, (4, 3)))
            ax.text(right + 0.3, v, f"{name} {px(v)}", color=col, fontsize=9, va="center", clip_on=False)
        xs = [i for i, e in enumerate(ema) if e is not None]
        if xs:
            ax.plot(xs, [ema[i] for i in xs], color=EMA_C, lw=1.2, alpha=0.75)
        bw = 0.6
        for i, b in enumerate(cs):
            col = UP if b["c"] >= b["o"] else DOWN
            ax.plot([i, i], [b["l"], b["h"]], color=col, lw=1)
            ax.add_patch(Rect((i - bw / 2, min(b["o"], b["c"])), bw, max(abs(b["c"] - b["o"]), (hi - lo) * 0.002), color=col))
            if b["t"] in pats:
                box = UP if long_ else DOWN
                ax.add_patch(Rect((i - bw / 2 - 0.25, b["l"] - pad * 0.15), bw + 0.5, b["h"] - b["l"] + pad * 0.3, fill=True,
                                  facecolor=box + "14", edgecolor=box, lw=0.9, ls=(0, (2, 2))))
        k = max(i for i, b in enumerate(cs) if b["t"] in pats or b["t"] == r["t"])
        if long_:
            ax.plot([k], [cs[k]["l"] - pad * 0.5], "o", color=UP, ms=7)
        else:
            ax.plot([k], [cs[k]["h"] + pad * 0.5], "o", color=DOWN, ms=7)
        for v, col, name in ((r["stop"], DOWN, f"stop {px(r['stop'])}"), (r["r1"], UP, f"1R {px(r['r1'])}")):
            ax.plot([max(0, k - 4), right], [v, v], color=col, lw=1.3)
            ax.text(k + 1.2, v + (pad * 0.12 if (v > r["entry"]) else -pad * 0.12), name, color=col, fontsize=9,
                    va="bottom" if v > r["entry"] else "top")
        fig.text(0.02, 0.03, caption, color=MUTED, fontsize=9)
        png_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(png_path, dpi=DPI, facecolor="white")
    finally:
        plt.close(fig)
    return png_path


def save(config, r: dict, candles: list[dict]) -> Optional[Path]:
    from backend.crypto import signals
    return render(r, candles, signals.caption(r, config), signals.snaps_dir() / f"{r['id']}.png")
