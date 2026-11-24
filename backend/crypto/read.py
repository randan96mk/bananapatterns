"""₿🤖 The market read at a detection (never gates an alert, never acts):

  code   the shape facts, the level, the EMA side, the volume multiple, ATR(14) and its expected 30-minute range
         (backend/market/chart.py), the stop and 1R: all numbers
  model  the candle model worker (backend/market/reads.call_worker; touch levels = the stop and 1R) when
         market.kronos is on: stored in shadow, never in the text model's input
  line   ONE "read:" sentence from the local text model through the existing AI queue (kind "crypto_read", the AI
         guard decides when it runs); a line whose figures are not in the code input is dropped. Shown tagged [AI].
The verdict word (supports the long / short · against · mixed) is set by code, from the EMA side and the volume.
Everything lands in crypto_signals.ai_json."""

import json
import re
import threading
import time as _time
from datetime import datetime
from typing import Callable, Optional

from backend.config import IST
from backend.logging_config.logger import setup_logger
from backend.timefmt import ampm

logger = setup_logger("crypto")

QKIND = "crypto_read"
WORDS = 30
SYSTEM = ("A trading app computed chart facts about a Bitcoin candle pattern at a pivot level. Write ONE line, plain text, starting "
          "with 'read:', at most 30 words: what the pattern and the level say. Use only figures that appear in the input; never "
          "invent a number. Describe; never advise buying or selling.")
GENERATE: Optional[Callable[[str, str], str]] = None       # the tests' fake model: (system, user) -> text
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
BLOCKED = ("buy now", "sell now", "guaranteed", "sure shot")


def verdict(r: dict) -> str:
    """Code's verdict: with the EMA side (and the volume when known) -> supports; against the EMA side -> against."""
    side = "the long" if r["dir"] == "long" else "the short"
    if r.get("ema_ok") is None:
        return "mixed"
    if not r["ema_ok"]:
        return "against"
    if r.get("vol_ok") == 0:
        return "mixed"
    return f"supports {side}"


def facts_text(r: dict, candles: list[dict]) -> str:
    """The code input (every figure the line may use)."""
    from backend.crypto import patterns as pt
    from backend.crypto.signals import TF_LABEL, ist, px
    from backend.market import chart
    f = r.get("facts_json") or {}
    sh = f.get("shape") or {}
    b5 = [{"h": b["h"], "l": b["l"], "c": b["c"]} for b in candles[-60:]]
    a = chart.atr(b5)
    bits = [f"{pt.LABEL[r['pattern']]} on {r['level_name']} {px(r['level_px'])} ({r['dir']} bias), {TF_LABEL.get(r['tf'], r['tf'])} candle at {ampm(ist(r['t']))}"]
    if sh.get("wick_x") is not None:
        bits.append(f"long wick {sh['wick_x']}x the body, {sh.get('wick_pct')}% of the range")
    if f.get("ema") is not None:
        bits.append(f"close {px(f['close'])} {'above' if f['close'] > f['ema'] else 'below'} EMA {f.get('ema_len')} at {px(f['ema'])}")
    if f.get("vol_x") is not None:
        bits.append(f"volume {f['vol_x']}x its average")
    if a is not None:
        bits.append(f"ATR {px(a)} points a candle")
    bits.append(f"suggested stop {px(r['stop'])}, 1R {px(r['r1'])}, entry level {px(r['entry'])}")
    return "; ".join(bits) + "."


def figures_ok(text: str, src: str) -> bool:
    allowed = {x.replace(",", "") for x in NUM_RE.findall(src)}
    allowed |= {x.rstrip("0").rstrip(".") for x in allowed if "." in x}
    return all(x.replace(",", "") in allowed or x.replace(",", "").rstrip("0").rstrip(".") in allowed for x in NUM_RE.findall(text))


def parse_line(raw: str, src: str) -> Optional[str]:
    """The 'read:' line (≤ WORDS words) whose figures are all in `src`, else None."""
    for ln in (raw or "").split("\n"):
        m = re.match(r"^\s*(?:\[?AI\]?\s*:?\s*)?\**\s*read\s*\**\s*[:\-–—]\s*(.+)$", ln.strip(), re.I)
        if not m:
            continue
        text = m.group(1).strip().strip("*").strip()
        w = text.split()
        if len(w) > WORDS:
            text = " ".join(w[:WORDS]).rstrip(",;:") + "…"
        if text and figures_ok(text, src) and not any(b in text.lower() for b in BLOCKED):
            return text
    return None


def _set(sid: int, **kw) -> dict:
    from backend.crypto import signals
    with signals._LOCK:
        r = signals.load(sid) or {}
        aj = {**(r.get("ai_json") or {}), **kw}
        signals.update(sid, ai_json=aj)
    return aj


def model_read(config, sid: int, candles: list[dict]) -> Optional[dict]:
    """The candle model on the BTC candles (6 steps ahead, touch levels = the stop and 1R): {p_up, stop_first,
    r1_first, ms} stored in shadow, or None (off / down / slow: nothing else changes)."""
    from backend.crypto import signals
    from backend.market import paths as pth
    from backend.market import reads as rd
    if not rd.on(config):
        return None
    r = signals.load(sid)
    if not r or len(candles) < 30:
        return None
    tf = r["tf"] if r["tf"] in ("1m", "5m") else "5m"
    payload = {"symbol": r["symbol"], "interval": tf, "horizon": 6, "horizons": [6], "samples": int(config.market.samples),
               "candles": [{"at": datetime.fromtimestamp(b["t"], IST).strftime("%Y-%m-%d %H:%M"), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
                            "v": b.get("v") or 0} for b in candles[-120:]],
               "levels": [float(r["stop"]), float(r["r1"])]}
    res = rd.call_worker(config, payload)
    if not res:
        return None
    one = (res.get("reads") or [res])[-1]
    t = one.get("touch") or {}
    out = {"p_up": one.get("p_up"), "stop_first": t.get(pth.level_key(r["stop"])), "r1_first": t.get(pth.level_key(r["r1"])),
           "ms": res.get("ms"), "at": datetime.now(IST).isoformat(timespec="seconds"), "show": "shadow"}
    _set(sid, model=out)
    return out


def _generate(config, src: str) -> tuple[str, str]:
    if GENERATE is not None:
        return GENERATE(SYSTEM, src), "fake"
    from backend.ai import client
    c = config.ai
    r = client.generate(c.day_model, src + "\n\nWrite the one 'read:' line now, plain text:", system=SYSTEM, json_mode=False,
                        timeout=float(c.timeout_s), keep_alive=c.keep_alive, options={"temperature": 0, "num_predict": 90})
    return r.get("text") or "", r.get("model") or c.day_model


def run(config, sid: int) -> dict:
    """The queue's job: the line (figure check), stored; a late line goes out as a second message."""
    from backend.crypto import signals
    r = signals.load(sid)
    if not r:
        return {"skip": "no row"}
    src = (r.get("ai_json") or {}).get("facts") or ""
    t0 = _time.monotonic()
    raw, model = _generate(config, src)
    text = parse_line(raw, src)
    ms = round((_time.monotonic() - t0) * 1000)
    _set(sid, state="done" if text else "empty", line=text, model_name=model, ms=ms, raw=(raw or "")[:1000],
         at=datetime.now(IST).isoformat(timespec="seconds"))
    if text:
        signals.send_ai_late(config, sid, text)
    return {"note": f"crypto read #{sid}", "tokens": None}


def start(config, sid: int, candles: list[dict]) -> Optional[int]:
    """Store the code facts and the verdict, start the model read (its own thread) and queue the line. The AI job id,
    or None (crypto.ai_read off, the local AI off or the queue full): the alert never waits on it."""
    from backend.crypto import signals
    c = config.crypto
    r = signals.load(sid)
    if not r or not c.ai_read:
        return None
    src = facts_text(r, candles)
    _set(sid, facts=src, verdict=verdict(r), state="pending")
    threading.Thread(target=lambda: _safe(model_read, config, sid, list(candles)), name=f"crypto-model-{sid}", daemon=True).start()
    if not getattr(config.ai, "enabled", False):
        _set(sid, state="off", why="the local AI is off")
        return None
    try:
        from backend.ai import queue as q
        job = q.enqueue(config, QKIND, f"crypto:{sid}", lambda: run(config, sid), model=config.ai.day_model, note=f"crypto read #{sid}")
        return job["id"]
    except Exception as exc:
        _set(sid, state="not queued", why=str(exc)[:120])
        return None


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception:
        logger.exception("₿ crypto model read failed")


def wait_line(sid: int, timeout: float) -> Optional[str]:
    """The line when it is written within `timeout` seconds, else None."""
    from backend.crypto import signals
    end = _time.monotonic() + timeout
    while True:
        r = signals.load(sid) or {}
        aj = r.get("ai_json") or {}
        if aj.get("line"):
            return aj["line"]
        if aj.get("state") in ("empty", "off") or _time.monotonic() >= end:
            return None
        _time.sleep(0.05)


def json_of(r: dict) -> str:
    return json.dumps(r.get("ai_json") or {})
