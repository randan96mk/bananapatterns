"""PNG chart for Telegram alerts: candles, volume, SMA 50/200, base box, pivot, stop, breakout marker.

Rendered headless with matplotlib (Agg). Dark theme to match the dashboard.
"""

import io
from typing import Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from backend.engine.indicators import sma  # noqa: E402

BG, PANEL, GRID, TEXT, MUTED = "#0d1117", "#12161c", "#1e242c", "#c9d1d9", "#7d8590"
UP, DOWN, ACCENT = "#26a69a", "#ef5350", "#f5c518"


def render_chart(candles: list[dict], title: str, subtitle: str = "", base: Optional[dict] = None,
                 stop: Optional[float] = None, breakout_date: Optional[str] = None, bars: int = 130,
                 contractions: Optional[list] = None) -> bytes:
    """base: {start_date, end_date, pivot, low, label}; contractions: [{high_date, high, low_date, low, depth_pct}]."""
    close_all = np.array([c["close"] for c in candles], dtype=float)
    ma50, ma200 = sma(close_all, 50), sma(close_all, 200)
    view = candles[-bars:]
    off = len(candles) - len(view)
    idx = {c["date"]: i for i, c in enumerate(view)}
    o = np.array([c["open"] for c in view]); h = np.array([c["high"] for c in view])
    lo = np.array([c["low"] for c in view]); cl = np.array([c["close"] for c in view])
    v = np.array([c["volume"] for c in view], dtype=float)
    x = np.arange(len(view))
    up = cl >= o

    fig, (ax, axv) = plt.subplots(2, 1, figsize=(9, 5.2), dpi=110, sharex=True,
                                  gridspec_kw={"height_ratios": [4, 1], "hspace": 0.04})
    fig.patch.set_facecolor(BG)
    for a in (ax, axv):
        a.set_facecolor(PANEL)
        a.grid(color=GRID, linewidth=0.6)
        a.tick_params(colors=MUTED, labelsize=8)
        for s in a.spines.values():
            s.set_color(GRID)

    if base:
        x0 = idx.get(base["start_date"], 0 if base["start_date"] < view[0]["date"] else None)
        x1 = idx.get(base["end_date"], len(view) - 1)
        if x0 is not None:
            ax.add_patch(Rectangle((x0 - 0.5, base["low"]), x1 - x0 + 1, base["pivot"] - base["low"],
                                   facecolor=UP, alpha=0.10, edgecolor=UP, linestyle=":", linewidth=1))
            ax.text(x0, base["low"], f" {base.get('label', '')}", color=UP, fontsize=8, va="top")
        ax.axhline(base["pivot"], color=UP, linestyle="--", linewidth=1)
        ax.text(0, base["pivot"], f" pivot ₹{base['pivot']:,.2f}", color=UP, fontsize=8, ha="left", va="bottom")
    if stop:
        ax.axhline(stop, color=DOWN, linestyle="--", linewidth=0.9)
        ax.text(0, stop, f" stop ₹{stop:,.2f}", color=DOWN, fontsize=8, ha="left", va="top")
    for k in contractions or []:
        if k["high_date"] in idx and k["low_date"] in idx:
            a_, b_ = idx[k["high_date"]], idx[k["low_date"]]
            ax.plot([a_, b_], [k["high"], k["low"]], color=ACCENT, linewidth=1)
            ax.text(b_, k["low"], f"-{k['depth_pct']:.0f}%", color=ACCENT, fontsize=7, ha="center", va="top")

    ax.vlines(x, lo, h, color=np.where(up, UP, DOWN), linewidth=0.8)
    ax.bar(x, np.abs(cl - o) + 1e-9, bottom=np.minimum(o, cl), color=np.where(up, UP, DOWN), width=0.7)
    ax.plot(x, ma50[off:], color="#4ea1ff", linewidth=0.9, label="SMA50")
    ax.plot(x, ma200[off:], color="#f5a524", linewidth=0.9, label="SMA200")
    if breakout_date in idx:
        b = idx[breakout_date]
        ax.annotate("▲", (b, h[b]), xytext=(0, 4), textcoords="offset points", color=UP, ha="center", fontsize=10)
    axv.bar(x, v, color=np.where(up, UP + "88", DOWN + "88"), width=0.7)
    axv.set_yticks([])

    ticks = list(range(0, len(view), max(1, len(view) // 6)))
    axv.set_xticks(ticks)
    axv.set_xticklabels([view[i]["date"][2:] for i in ticks])
    ax.legend(loc="upper left", fontsize=7, facecolor=PANEL, edgecolor=GRID, labelcolor=TEXT)
    fig.suptitle(title, color=TEXT, fontsize=11, x=0.01, ha="left", y=0.98)
    if subtitle:
        fig.text(0.01, 0.925, subtitle, color=MUTED, fontsize=8, ha="left")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
