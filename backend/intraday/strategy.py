"""📜 The strategy that is running, in plain words: one text written from the boxes on the canvas (their order, the
rules in words, every setting), complete enough to paste anywhere. It changes the moment a box changes, a setup
is loaded or the boxes are reset; those changes are kept in a short log (app_settings flow.changes)."""

import json
from datetime import datetime

from backend.config import IST
from backend.database.models import get_db, get_setting, now_ist, set_setting
from backend.intraday import flow
from backend.intraday.modules import REGISTRY, SKIPPABLE

MODES = {"off": "Off: nothing is traded or tracked",
         "signals": "Signals: each signal is tracked like a paper trade (fill, stop, target, trail, square-off), not traded",
         "paper": "Paper: simulated trades, no orders"}
KEEP = 50


def _fmt(v) -> str:
    return "on" if v is True else "off" if v is False else str(v)


def _labels(kind: str) -> dict:
    return {f["key"]: f["label"] for f in REGISTRY[kind].schema()}


def _order(t: dict) -> list[str]:
    """Each box in run order with what it reads, e.g. 'Intraday filters ← Universe'."""
    title = {nid: f"{REGISTRY[kind].icon} {REGISTRY[kind].title}" for nid, kind, _, _ in t["nodes"]}
    out = []
    for nid, kind, inputs, _ in t["nodes"]:
        src = " + ".join(title[i] for i in inputs)
        out.append(title[nid] + (f" ← {src}" + (" (all must pass)" if kind == "and" else "") if src else ""))
    return out


def text(mode: str, cfgs: dict | None = None) -> str:
    from backend.intraday.journey import rules
    t, cfgs = flow.template(), cfgs or flow.node_configs()
    at = datetime.now(IST).strftime("%-d %b %Y, %-I:%M %p").replace("AM", "am").replace("PM", "pm")
    lines = ["BANANA PATTERNS · INTRADAY FLOW · THE STRATEGY RUNNING NOW",
             f"Written {at} IST from the boxes on the canvas. It changes when any box changes.",
             f"Template: {t['name']}", f"Mode: {MODES.get(mode, mode)}", ""]
    skipped = [REGISTRY[k].title for n, k, _, _ in t["nodes"] if k in SKIPPABLE and cfgs[n].get("skip")]
    lines += ["HOW IT RUNS", "On every candle close the boxes run in this order; a stock must pass each one to reach the next:"]
    lines += [f"  {i}. {x}" for i, x in enumerate(_order(t), 1)]
    lines += [f"Boxes skipped (every stock passes through): {', '.join(skipped) if skipped else 'none'}", ""]
    for k, r in enumerate(rules(cfgs), 1):
        head = r["title"].split(" ", 1)[-1].upper()                 # "📥 Entry: all of these" -> "ENTRY: ALL OF THESE"
        lines.append(f"{k}. {head}")
        lines += [f"  {k}.{j} {x}" for j, x in enumerate(r["lines"], 1)]
        lines.append("")
    lines.append("EVERY SETTING, BOX BY BOX")
    for nid, kind, _, _ in t["nodes"]:
        lab = _labels(kind)
        vals = " · ".join(f"{lab[k]} = {_fmt(v)}" for k, v in cfgs[nid].items() if k in lab)
        lines.append(f"  {REGISTRY[kind].icon} {REGISTRY[kind].title}: {vals or '(no settings)'}")
    lines += ["", "Fills: the next 1-minute open + 0.05% slippage; paper P&L is after intraday charges. Nothing here places orders."]
    return "\n".join(lines)


def changes() -> list:
    with get_db() as conn:
        return json.loads(get_setting(conn, "flow.changes") or "[]")


def record(box: str, what: list[str]) -> None:
    """One entry in the change log: when, which box, what went from what to what."""
    if not what:
        return
    with get_db() as conn:
        log = json.loads(get_setting(conn, "flow.changes") or "[]")
        log.insert(0, {"at": now_ist(), "box": box, "what": what})
        set_setting(conn, "flow.changes", json.dumps(log[:KEEP]))


def diff(kind: str, before: dict, after: dict) -> list[str]:
    lab = _labels(kind)
    return [f"{lab[k]}: {_fmt(before.get(k))} → {_fmt(after.get(k))}" for k in lab if before.get(k) != after.get(k)]
