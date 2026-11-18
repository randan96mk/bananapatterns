"""💡 Sort a post: code first, the model second.

  image      no text, only a photo / video / file (charts and P&L screenshots: ignored, no OCR)
  promo      links only, "Guidelines", a disclaimer line, a quote, an ad for a course / group
  chatter    no number of 3+ digits AND no trade word (never shown to the model); also what nothing else claims
  hint       code decodes a call (decode.looks_like_hint); the model may call a post a hint, then code decodes it
  follow_up  the model (or code, when the model is off) links it to one of the open hints; code checks that the
             symbol / strike in the text match the hint it picked
The local 3B model only classifies {hint | follow_up | chatter} and picks among the open hints' ids."""

import json
import re
import time
from typing import Optional

from backend.logging_config.logger import setup_logger
from backend.tips import decode as dc

logger = setup_logger("tips")

TRADE_WORDS = re.compile(r"\b(buy|sell|ce|pe|sl|target|targets|tgt|tg|exit|book|booked|booking|short|long|cmp|active|activated|triggered|tsl|npnl|hit|done|"
                         r"trail|trailing|avoid|entry|stop\s*loss|stoploss|cost)\b", re.I)
LINK = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+|bit\.ly/\S+", re.I)
PROMO = re.compile(r"guidelines|disclaimer|not a recommendation|sebi|join (my|our|the)|subscribe|mentorship|course|webinar|"
                   r"enrol|enroll|premium (group|members|channel)|whatsapp|instagram|youtube|josh talks|feedbacks?\b|"
                   r"registration|register|limited seats|dm me|contact", re.I)
FOLLOW_CODE = re.compile(r"\b(sl|stop\s*loss)\b.*\b(hit|modif|chang|trail)|\b(target|tgt)s?\s*\d?\s*(done|hit)|\b\d\s*targets?\s*done|"
                         r"\bexit|\bbook|\bcost\b|\bactiv(e|ated)\b|\btriggered\b|\bentry done\b|\btsl\b|\bnpnl\b|\bcmp\b|\btrail|\bavoid\b|"
                         r"\b(sl|stop\s*loss|stoploss)\s*[:\-]?\s*\d|\b(tgt|target|tg)s?\s*[:\-]?\s*\d{2,}|\badd more\b|\bcentury\b|\bdone\b|"
                         r"\b\d+(\.\d+)?\s*(\+|to\b)|^\s*\d+(\.\d+)?\s*\W*$|"
                         r"\bwait\b|\bpart(ly)?\b", re.I | re.S)
SYSTEM = ("You sort posts from a Telegram trading channel. Reply with JSON only: {\"kind\": \"hint\" | \"follow_up\" | "
          "\"chatter\", \"hint_id\": <one of the open hint ids or null>}. hint = a NEW trade call (buy/sell an option, future "
          "or stock with a price). follow_up = an update about one of the open hints (price now, target done, stop hit, "
          "exit, trail, avoid, active). chatter = anything else. Never invent ids.")


def numbers(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)", text or "")]


def code_kind(text: str, has_media: bool, open_levels: Optional[list] = None) -> Optional[str]:
    """image / promo / chatter by code, or None when the post needs a closer look (decode, the model). `open_levels`
    are the open hints' price levels: a 1-2 digit figure near one of them (a premium update like "50 to 90+++") is not
    chatter."""
    t = (text or "").strip()
    if not t:
        return "image" if has_media else "chatter"
    bare = LINK.sub("", t).strip()
    if LINK.search(t) and len(re.findall(r"\w+", bare)) <= 6:
        return "promo"
    first = bare.lstrip("\"'“”«").lower()
    if re.match(r"(option buying |option selling )?guidelines\b|disclaimer\b", first) or t[:1] in ("\"", "“", "«"):
        return "promo"
    if dc.STRIKE_RE.search(bare) and TRADE_WORDS.search(bare):     # a call with a disclaimer link is still a call
        return None
    if PROMO.search(t) and LINK.search(t):
        return "promo"
    if PROMO.search(t) and not TRADE_WORDS.search(t) and not re.search(r"\d{3,}", t):
        return "promo"
    if not re.search(r"\d{3,}", bare) and not TRADE_WORDS.search(bare):
        levels = [x for x in (open_levels or []) if x]
        if levels and any(any(0.5 * lv <= n <= 2.0 * lv for lv in levels) for n in numbers(bare)):
            return None
        return "chatter"
    return None


def _levels(h: dict) -> list:
    e = h.get("entry") or {}
    st = h.get("stop") or {}
    out = [e.get("lo"), e.get("hi")] + list(h.get("targets") or [])
    if st.get("kind") == "premium":
        out.append(st.get("value"))
    return [float(x) for x in out if x]


def open_levels(hints: list[dict]) -> list:
    return [v for h in hints for v in _levels(h)]


def check_link(text: str, hint: dict) -> bool:
    """Code's check of a link: a symbol or strike named in the text must be the hint's."""
    sym = dc.symbol_of(text)
    if sym and hint.get("symbol") and sym != hint["symbol"]:
        return False
    m = dc.STRIKE_RE.search(text)
    if m and hint.get("strike") is not None:
        s = float(m.group(1) or m.group(4))
        if abs(s - float(hint["strike"])) > 1e-6:
            return False
    return True


def code_link(text: str, hints: list[dict]) -> Optional[int]:
    """The open hint a follow-up most likely refers to, by code: the one whose symbol / strike the text names, else
    the newest one whose levels the figures in the text sit near, else the newest one."""
    if not hints:
        return None
    named = [h for h in hints if check_link(text, h) and (dc.symbol_of(text) or dc.STRIKE_RE.search(text))]
    if named:
        return named[-1]["id"]
    nums = numbers(text)
    for h in reversed(hints):
        lv = _levels(h)
        if nums and lv and any(any(0.5 * x <= n <= 2.0 * x for x in lv) for n in nums):
            return h["id"]
    from backend.tips.plan import followup_action
    act = followup_action(text).get("action")
    live = lambda h: ((h.get("pos") or {}).get("run") or h.get("run") or {}).get("state") == "active"   # noqa: E731
    if act in ("exit", "sl_hit", "stop", "trail", "partial", "targets"):     # about a trade in progress
        pick = [h for h in hints if live(h)]
    elif act in ("avoid", "active"):                                        # about an entry still to come
        pick = [h for h in hints if not live(h)]
    else:
        pick = []
    return (pick or hints)[-1]["id"]


def code_classify(text: str, hints: list[dict]) -> tuple[str, Optional[int]]:
    """The fallback when the model is off or failed: follow-up words with an open hint -> follow_up, else chatter."""
    if hints and FOLLOW_CODE.search(text or ""):
        return "follow_up", code_link(text, hints)
    return "chatter", None


def model_allowed(config) -> bool:
    if not config.tips.model_classify or not config.ai.enabled:
        return False
    try:
        from backend.ai import guard
        ok, _ = guard.allowed("tips", config=config)
        return ok
    except Exception:
        return False


def model_classify(config, text: str, hints: list[dict]) -> Optional[dict]:
    """The 3B model's sort: {kind, hint_id, model, ms} or None (off / failed / an answer outside the rules)."""
    if not model_allowed(config):
        return None
    from backend.ai import client
    model = config.ai.day_model
    open_list = [{"id": h["id"], "call": dc.title(h)} for h in hints]
    prompt = f"Open hints: {json.dumps(open_list)}\nPost:\n{(text or '')[:800]}\nJSON:"
    t0 = time.monotonic()
    try:
        r = client.generate(model, prompt, system=SYSTEM, json_mode=True, timeout=30, options={"temperature": 0, "num_predict": 60})
        d = json.loads(r.get("text") or "{}")
    except Exception as exc:
        logger.info("Tips model sort failed: %s", str(exc)[:120])
        return None
    kind = str(d.get("kind") or "").strip().lower().replace("-", "_")
    if kind not in ("hint", "follow_up", "chatter"):
        return None
    hid = d.get("hint_id")
    ids = {h["id"] for h in hints}
    try:
        hid = int(hid) if hid is not None else None
    except (TypeError, ValueError):
        hid = None
    if kind == "follow_up":
        if hid not in ids:
            hid = code_link(text, hints) if hints else None
        elif not check_link(text, next(h for h in hints if h["id"] == hid)):
            hid = code_link(text, hints)
        if hid is None:
            kind = "chatter"
    else:
        hid = None
    return {"kind": kind, "hint_id": hid, "model": r.get("model") or model, "ms": int((time.monotonic() - t0) * 1000)}


FILL_SYSTEM = ("You read one trading call. Reply with JSON only, with these keys when the text states them: symbol, side "
               "(BUY|SELL), strike, opt_type (CE|PE), entry {mode at|zone|above|below, lo, hi}, stop {kind premium|spot, value}, "
               "targets [numbers]. Copy figures exactly from the text; leave out anything the text does not say.")


def model_fill(config):
    """A `fill(text, missing)` for decode (None when the model may not run): the model's values, which decode keeps
    only when they are in the text."""
    if not model_allowed(config):
        return None

    def fill(text: str, missing: list) -> dict:
        from backend.ai import client
        r = client.generate(config.ai.day_model, f"Missing: {missing}\nCall:\n{(text or '')[:800]}\nJSON:", system=FILL_SYSTEM,
                            json_mode=True, timeout=30, options={"temperature": 0, "num_predict": 120})
        d = json.loads(r.get("text") or "{}")
        return d if isinstance(d, dict) else {}
    return fill
