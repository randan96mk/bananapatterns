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
from datetime import datetime, timedelta
from typing import Optional

from backend.config import IST

from backend.logging_config.logger import setup_logger
from backend.tips import decode as dc
from backend.tips import plan as pl

logger = setup_logger("tips")

TRADE_WORDS = re.compile(r"\b(buy|sell|ce|pe|sl|target|targets|tgt|tg|exit|book|booked|booking|short|long|cmp|active|activated|triggered|tsl|npnl|hit|done|"
                         r"trail|trailing|avoid|entry|stop\s*loss|stoploss|cost)\b", re.I)
SIZE_WORDS = re.compile(r"(?<![\d.])\d{1,2}\s*(?:-\s*\d{1,2}\s*)?lots?\b", re.I)
LINK = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+|bit\.ly/\S+", re.I)
PROMO = re.compile(r"guidelines|disclaimer|not a recommendation|sebi|join (my|our|the)|subscribe|mentorship|course|webinar|"
                   r"enrol|enroll|premium (group|members|channel)|whatsapp|instagram|youtube|josh talks|feedbacks?\b|"
                   r"registration|register|limited seats|dm me|contact", re.I)
FOLLOW_CODE = re.compile(r"\b(sl|stop\s*loss)\b.*\b(hit|modif|chang|trail)|\b(target|tgt)s?\s*\d?\s*(done|hit)|\b\d\s*targets?\s*done|"
                         r"\bexit|\bbook|\bcost\b|\bactiv(e|ated)\b|\btriggered\b|\bentry done\b|\btsl\b|\bnpnl\b|\bcmp\b|\btrail|\bavoid\b|"
                         r"\b(sl|stop\s*loss|stoploss)\s*[:\-]?\s*\d|\b(tgt|target|tg)s?\s*[:\-]?\s*\d{2,}|\badd more\b|\bcentury\b|\bdone\b|"
                         r"\b\d+(\.\d+)?\s*(\+|to\b)|^\s*\d+(\.\d+)?\s*\W*$|"
                         r"\bwait\b|\bpart(ly)?\b|"
                         r"\badd\s+(?:\d+\s*(?:lots?\s*)?)?more\b|\badd\s+\d+\s*lots?\b|\b\d{1,2}\s*(?:-\s*\d{1,2}\s*)?lots?\b|"   # 📏 23 Nov: adds / sizes
                         r"\b(rest|revise|revised)\s*(sl|stop\s*loss|stoploss)\b|\b(t?sl|stop\s*loss|stoploss)\s*(now|gone)\b", re.I | re.S)
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
        if open_levels and SIZE_WORDS.search(bare):              # 📏 "2-2 lots" / "2 lots here": the size of an open call
            return None
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


PLAN_POST = re.compile(r"\b(sl|stop\s*loss|stoploss|tgt|target|targets|tg)s?\b", re.I)
ARMED_STATES = ("armed", "armed_watch", "armed_wait")
STOPPED = ("paused", "closed", "expired", "skipped")


def _kite(h: dict) -> str:
    k = h.get("kite") if isinstance(h.get("kite"), dict) else {}
    return k.get("state") or "none"


def stopped(h: dict) -> bool:
    """Paused / avoided / closed: never the target of a plan post."""
    return h.get("status") in STOPPED or bool((h.get("run") or {}).get("paused"))


def held(h: dict) -> bool:
    """We hold it or have armed it: a paper take (armed / open) or a Kite entry armed / entered."""
    pos = h.get("pos") or {}
    return (bool(h.get("taken")) and pos.get("state") in ("armed", "open")) or _kite(h) in ARMED_STATES + ("entered",)


def engaged(h: dict) -> bool:
    """Filled / armed / level reached (the record run's entry filled on our quotes)."""
    return held(h) or (h.get("run") or {}).get("state") == "active"


def not_taken(h: dict) -> bool:
    pos = h.get("pos") or {}
    return not (h.get("taken") and pos.get("state") in ("open", "closed")) and _kite(h) not in ("entered", "closed") \
        and h.get("status") not in ("closed", "expired", "skipped")


def _lacks_stop(h: dict) -> bool:
    return pl.stop_src(h.get("stop")) in (None,) + pl.REPLACEABLE     # 🛑 a provisional / your stop: the channel's SL replaces it


def _age_ok(h: dict, posted: Optional[datetime], minutes: int) -> bool:
    try:
        return (posted or datetime.now(IST)) - datetime.fromisoformat(h["posted_at"]) <= timedelta(minutes=minutes)
    except (TypeError, ValueError, KeyError):
        return False


BARE_TARGETS = re.compile(r"\s*\d+(?:\.\d+)?(?:\s*[/ ]\s*\d+(?:\.\d+)?)+\s*\+*\s*")


def bare_targets(text: str, h: Optional[dict] = None) -> Optional[list]:
    """🎯 A post that is only 2+ rising numbers separated by / or spaces ("625/660/700/750/800"): the list, else None.
    With `h`: only when every figure sits on the target side of its entry (above for a BUY, below for a SELL)."""
    t = (text or "").strip()
    if not BARE_TARGETS.fullmatch(t):
        return None
    xs = [pl._val(x) for x in re.findall(r"\d+(?:\.\d+)?", t)]
    up = all(b > a for a, b in zip(xs, xs[1:]))
    if len(xs) < 2 or not (up or all(b < a for a, b in zip(xs, xs[1:]))):
        return None
    if h is not None:
        e = h.get("entry") or {}
        ref = pl.entry_level(h) or e.get("hi") or e.get("lo")
        if not ref:
            return None
        sell = h.get("side") == "SELL"
        if up == sell or any((x >= float(ref)) if sell else (x <= float(ref)) for x in xs):
            return None
    elif not up:
        return None
    return xs


def plan_post(text: str, hints: list[dict], posted: Optional[datetime] = None) -> Optional[int]:
    """📌 A bare stop / targets post for an open call that lacks them ("Sl 530", "Tgt 830/880/940", posted as separate
    messages a minute after the call, 26 Nov): code decides, the model is not asked (it had sorted these as chatter).
    The newest open call that LACKS the piece wins (never a paused / avoided / closed one); code_link only when none
    lacks it. A bare list ("625/660/700/750/800") within 15 min of a call with no targets: its targets. Returns the hint
    id, or None when the post is not that."""
    t = text or ""
    if not hints:
        return None
    sz = size_post(t, hints, posted)                      # 📏 "2 lots" on its own line right after the call: its size
    if sz:
        return sz
    live = [h for h in hints if not stopped(h)]
    if BARE_TARGETS.fullmatch(t.strip()):
        pick = [h for h in live if not h.get("targets") and _age_ok(h, posted, 15) and bare_targets(t, h)]
        return pick[-1]["id"] if pick else None
    if not PLAN_POST.search(t) or FOLLOW_CODE.search(t) and re.search(r"\b(done|hit|exit|book)\b", t, re.I):
        return None
    d = dc.decode(t, posted or datetime.now(IST))
    st = d.get("stop") or {}
    tg = [x for x in (d.get("targets") or []) if x is not None]
    has_stop = st.get("kind", "none") != "none" and st.get("value") is not None
    if not has_stop and not tg:
        return None
    if dc.looks_like_hint(d, t):
        return None
    named = bool(dc.symbol_of(t) or dc.STRIKE_RE.search(t))
    cands = [h for h in live if not named or check_link(t, h)]
    lacking = [h for h in cands if (has_stop and _lacks_stop(h)) or (tg and not h.get("targets"))]
    if lacking:
        return lacking[-1]["id"]
    if not cands:
        return None
    hid = code_link(t, cands)
    h = next((x for x in cands if x["id"] == hid), None)
    if h is None:
        return None
    if tg and recent_targets(h, posted):
        return hid
    return None


def size_post(text: str, hints: list[dict], posted: Optional[datetime] = None, minutes: int = 10) -> Optional[int]:
    """A bare size post ("2 lots", "2-2 lots") within `minutes` of the newest open call that names no size (never a
    paused / closed one): that call's (26 Nov 10:53: the call, then "2 lots" as its own message)."""
    t = (text or "").strip()
    if len(re.findall(r"\w+", t)) > 4 or not pl.size_of(t) or pl.size_of(t).get("action") != "size":
        return None
    pick = [h for h in hints if not stopped(h) and not h.get("size_hint") and not h.get("taken") and _age_ok(h, posted, minutes)]
    return pick[-1]["id"] if pick else None


def repeat_call(d: dict, hints: list[dict], posted: Optional[datetime] = None, minutes: int = 15) -> Optional[int]:
    """🔁 A call posted again: the same symbol / strike / option type and the same entry level (± 0.5 %) as an open
    (not paused / closed) hint posted within `minutes` → that hint's id (a follow-up, never a second hint)."""
    lv = pl.entry_level(d)
    if lv is None or d.get("strike") is None:
        return None
    for h in reversed(hints):
        if stopped(h) or not _age_ok(h, posted, minutes) or _kite(h) == "closed" or (h.get("pos") or {}).get("state") == "closed":
            continue
        if (h.get("symbol"), h.get("opt_type")) != (d.get("symbol"), d.get("opt_type")) or h.get("strike") is None:
            continue
        if abs(float(h["strike"]) - float(d["strike"])) > 1e-6:
            continue
        hl = pl.entry_level(h)
        if hl and abs(float(lv) - float(hl)) <= 0.005 * float(hl):
            return h["id"]
    return None


# 💪 strong follow-ups, by code before the model (A1): avoid · active / cmp N · a result · a booking · a bare wait
S_RESULT = re.compile(r"\b(?:tgt|target)s?\s*\d?\s*(?:(?:and|&|,|/)\s*\d\s*)?(?:done|hit|achieved)\b|\b(?:\d|all)\s*(?:tgts?|targets?)\s*(?:done|hit|achieved)\b|"
                      r"\b(?:t?sl|stop\s*loss|stoploss)\s*(?:hit|triggered|gone)\b|\bcentury\b")
S_BOOK = re.compile(r"\b(?:book|booking|exit|exited|trail|trailing|tsl|npnl)\b")
S_ACTIVE = re.compile(r"(?<!not )\b(?:active|activated)\b|\bcmp\s*[:\-]?\s*\d")
S_GENERAL = re.compile(r"\b(?:all|any)\s+(?:the\s+)?(?:trades|calls|positions)\b|\bexit them\b")     # a reminder to everyone, not one call


def strong_kind(text: str) -> Optional[str]:
    """result | book | avoid | active | wait | None."""
    low = (text or "").lower().replace("’", "'")
    if not low.strip() or S_GENERAL.search(low):
        return None
    if S_RESULT.search(low):
        return "result"
    if S_BOOK.search(low):
        return "book"
    if pl.AVOID_RE.search(low):
        return "avoid"
    if S_ACTIVE.search(low) and len(re.findall(r"\w+", low)) <= 6:
        return "active"
    if pl.WAIT_RE.fullmatch(low):
        return "wait"
    return None


def strong_followup(text: str, hints: list[dict], posted: Optional[datetime] = None) -> Optional[int]:
    """💪 A strong follow-up decided by code (the model is not asked): the open hint it belongs to, else None. Results
    and bookings go to the newest hint filled / armed / level-reached, else the newest open one; avoid / active / wait
    to the newest hint not taken. A symbol / strike named in the text must be the hint's."""
    if not hints:
        return None
    k = strong_kind(text)
    if k is None:
        return None
    named = bool(dc.symbol_of(text) or dc.STRIKE_RE.search(text or ""))
    hs = [h for h in hints if not named or check_link(text, h)]
    if not hs:
        return None
    if k in ("result", "book"):
        pick = [h for h in hs if engaged(h)] or [h for h in hs if not stopped(h)] or hs
    else:
        pick = [h for h in hs if not_taken(h)]
    return pick[-1]["id"] if pick else None


def recent_targets(h: dict, posted: Optional[datetime] = None, minutes: int = 15) -> bool:
    """The hint's targets came from a follow-up within `minutes` of `posted` and nothing has filled: a second "Tgt …"
    post is the channel's correction (26 Nov: "Tgt 830/880/940" then "Tgt 730/780/840" a moment later)."""
    ev = next((e for e in reversed(h.get("events") or []) if "plan completed" in str(e.get("text") or "") and "targets" in str(e.get("text") or "")), None)
    if not ev or h.get("taken") or (h.get("run") or {}).get("state") not in (None, "waiting"):
        return False
    try:
        return (posted or datetime.now(IST)) - datetime.fromisoformat(ev["t"]) <= timedelta(minutes=minutes)
    except (TypeError, ValueError):
        return False


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
