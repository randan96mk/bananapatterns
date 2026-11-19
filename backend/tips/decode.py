"""💡 Decode a channel post into a hint, by code (regex); the local model may only fill a field code left empty, and only
with a figure / name that is in the text (`decoded[field] = "ai"`).

Shapes seen in the channel (Jul 2021 → Jul 2025):
  Buy bank nifty 50300 PE near 250-270 when comes · Sl 190 · Tgt 320-350-400++
  BANKNIFTY 49600 PE BUY ABOVE 50 · SL 40 · TG 60 80 100 PLUS
  NIFTY 24150 PE · At 190-195 · SL 170 · TG 210/230/250+++++          (no side word: a buy, from the targets)
  Open Trade - · Sell SENSEX 75200 Ce at 40 SL 50 Target 0              (an option sell held to expiry)
  For 8th November, 2023 expiry · Sell Bank nifty 43600 CE at 36 · target 0 · Stop loss - 15 mins candle above 43450 spot
  Buy CRUDE 5300 PE NEAR 267 AND 263 · SL 250 · TGT 275-285-300-325-350-400-450
Aliases: BNF / BN / bank nifty → BANKNIFTY, FN / fin nifty → FINNIFTY, MIDCP → MIDCPNIFTY, crude (oil) → CRUDE (MCX)."""

import re
from datetime import date, datetime
from typing import Callable, Optional

INDEX_NAMES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX")
COMMODITIES = {"CRUDE": "CRUDEOIL", "NATURALGAS": "NATURALGAS", "GOLD": "GOLD", "SILVER": "SILVER"}   # name -> MCX name
CRYPTO = ("BTC", "ETH")
ALIASES = [                                   # order matters: the longer names first
    ("BANKNIFTY", r"bank\s*nifty|banknifty|nifty\s*bank|\bbnf\b|\bbn\b"),
    ("FINNIFTY", r"fin\s*nifty|finnifty|\bfn\b"),
    ("MIDCPNIFTY", r"midcp\s*nifty|midcpnifty|midcap\s*nifty|\bmidcp\b"),
    ("SENSEX", r"\bsensex\b"),
    ("BANKEX", r"\bbankex\b"),
    ("NIFTY", r"\bnifty(?:\s*50)?\b"),
    ("CRUDE", r"\bcrude\s*oil\b|\bcrudeoil\b|\bcrude\b"),
    ("NATURALGAS", r"\bnatural\s*gas\b|\bnaturalgas\b"),
    ("GOLD", r"\bgold\b"),
    ("SILVER", r"\bsilver\b"),
    ("BTC", r"\bbtc\b|\bbitcoin\b"),
    ("ETH", r"\beth\b|\bethereum\b"),
]
MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
NUM = r"(\d+(?:\.\d+)?)"
STOP_WORDS = {"buy", "sell", "short", "long", "at", "near", "above", "below", "around", "cmp", "sl", "tgt", "target", "tg",
              "ce", "pe", "call", "put", "open", "trade", "for", "expiry", "and", "the", "in", "when", "comes", "more", "add",
              "lot", "lots", "qty", "stop", "loss", "fut", "futures", "future", "spot", "index", "option", "options", "rs",
              "only", "now", "then", "today", "intraday", "weekly", "monthly", "new", "fresh", "trade-", "from", "of", "on",
              "avoid", "wait", "active", "done", "hit", "exit", "book", "booked", "level", "levels", "zone", "plus",
              "abv", "strike", "premium", "price", "with", "if", "will", "can", "this", "that", "is", "a", "an", "to",
              "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec", "april", "march"}
FIELDS = ("symbol", "side", "instrument", "strike", "opt_type", "expiry", "entry", "stop", "targets", "size_hint")
DONE_WORDS = re.compile(r"\b(done|hit|booked|reached|achieved|triggered|gone|went|made|became)\b", re.I)
SL_RE = re.compile(r"\b(stop\s*loss|stoploss|sl|stop)\b\s*[-:=]?\s*", re.I)
TGT_RE = re.compile(r"\b(targets?|tgts?|tg)\b\s*[-:=]?\s*", re.I)
ENTRY_RE = re.compile(r"\b(above|abv|below|near|around|at|cmp|from)\b\s*(?:rs\.?\s*)?" + NUM +
                      r"(?:\s*(?:-|–|to|and|/)\s*" + NUM + r")?|@\s*" + NUM, re.I)
STRIKE_RE = re.compile(r"(?<![\d.])(\d{3,6}(?:\.\d+)?)\s*(?:([a-z]+)\s+)?(ce|pe|call|put)\b|(?<![\d.])(\d{3,6})(c|p)\b", re.I)


STRIKE_BANDS = (("NIFTY", 18000, 32000), ("BANKNIFTY", 44000, 68000), ("SENSEX", 70000, 98000))   # bands, Nov 2026 levels


def index_from_strike(strike: float) -> Optional[str]:
    """The index an option strike belongs to when the post names none (NIFTY ~25k, BANKNIFTY ~55-62k, SENSEX ~80k+).
    FINNIFTY / MIDCPNIFTY / BANKEX overlap those bands and are never inferred."""
    for name, lo, hi in STRIKE_BANDS:
        if lo <= float(strike) <= hi:
            return name
    return None


def symbol_of(text: str) -> Optional[str]:
    """The first alias found (bank nifty before nifty), else None."""
    low = text.lower()
    best = None
    for name, pat in ALIASES:
        m = re.search(pat, low)
        if m and (best is None or m.start() < best[1] or (m.start() == best[1] and len(m.group(0)) > best[2])):
            best = (name, m.start(), len(m.group(0)))
    return best[0] if best else None


def _num(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    v = float(s)
    return int(v) if v == int(v) else v


def _expand(lo: float, hi: float) -> float:
    """'50200-300' -> 50300 (the short form of the upper bound)."""
    if hi < lo and float(hi).is_integer() and float(lo).is_integer():
        a, b = str(int(lo)), str(int(hi))
        if len(b) < len(a):
            v = int(a[:len(a) - len(b)] + b)
            if v >= lo:
                return v
    return hi


def _segment(text: str, rx: re.Pattern) -> Optional[str]:
    """The text after the first SL / target marker up to the end of its line (the next line when that one has no
    number), cut at the next marker of the other kind."""
    m = rx.search(text)
    if not m:
        return None
    rest = text[m.end():]
    line, _, after = rest.partition("\n")
    if not re.search(r"\d", line):
        nxt = next((ln for ln in after.split("\n") if ln.strip()), "")
        line = line + " " + nxt
    other = TGT_RE if rx is SL_RE else SL_RE
    o = other.search(line)
    return line[:o.start()] if o else line


def _side(text: str, targets: list, entry_lo: Optional[float]) -> tuple[Optional[str], str]:
    low = text.lower()
    b = re.search(r"\b(buy|long|bought)\b", low)
    s = re.search(r"\b(sell|short|write)\b", low)
    if b and (not s or b.start() < s.start()):
        return "BUY", "code"
    if s:
        return "SELL", "code"
    if targets and targets[0] == 0:
        return "SELL", "code"
    if targets and entry_lo is not None:
        return ("BUY" if targets[0] > entry_lo else "SELL"), "code"
    return None, "code"


def _expiry(text: str, posted: date) -> Optional[str]:
    """An explicit date in the post ('For 8th November, 2023 expiry', 'Sensex 22nd April'); the year from the post when
    missing (the next year when that date is well before the post)."""
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s*(\d{4})?", text, re.I)
    if not m:
        return None
    day, mon = int(m.group(1)), MONTHS[m.group(2).lower()[:3]]
    year = int(m.group(3)) if m.group(3) else posted.year
    try:
        d = date(year, mon, day)
    except ValueError:
        return None
    if not m.group(3) and (posted - d).days > 30:
        d = date(year + 1, mon, day)
    return d.isoformat()


def _stop(seg: Optional[str], side: Optional[str], opt_type: Optional[str]) -> dict:
    """{kind premium|spot|none, value, dir above|below (spot), tf minutes (spot)}."""
    if seg is None:
        return {"kind": "none", "value": None}
    low = seg.lower()
    tf = re.search(r"(\d+)\s*(?:mins?|minutes?|m)\b", low)
    nums = [float(x) for x in re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)(?!\s*(?:mins?|minutes?)\b)", low)]
    if tf:
        nums = [n for n in nums if n != float(tf.group(1))] or nums
    if not nums:
        return {"kind": "none", "value": None}
    if "spot" in low or "candle" in low or "close" in low:
        val = max(nums)
        d = "above" if "above" in low else "below" if "below" in low else None
        if d is None:                                   # the side that hurts: short CE / long PE -> above
            bad_up = (side == "SELL" and opt_type == "CE") or (side == "BUY" and opt_type == "PE")
            d = "above" if bad_up else "below"
        return {"kind": "spot", "value": _num(str(val)), "dir": d, "tf": int(tf.group(1)) if tf else 15}
    return {"kind": "premium", "value": _num(str(nums[0]))}


def _targets(seg: Optional[str]) -> tuple[list, bool]:
    if seg is None:
        return [], False
    nums = re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)", seg)
    out = [_num(n) for n in nums]
    open_ended = bool(re.search(r"\+|\bplus\b|\bopen\b", seg, re.I))
    return out, open_ended


def _entry(text: str, after: int, strike: Optional[float]) -> dict:
    """{mode at|zone|above|below|market, lo, hi, when_comes} from the first price word after the strike (SL / target
    lines excluded)."""
    when = bool(re.search(r"when\s+(it\s+)?comes?", text, re.I))
    body = text[after:]
    for rx in (SL_RE, TGT_RE):                           # never read the stop or a target as the entry
        m = rx.search(body)
        if m:
            body = body[:m.start()] + "\n" + "\n".join(body[m.start():].split("\n")[1:])
    for m in ENTRY_RE.finditer(body):
        word = (m.group(1) or "@").lower()
        lo = _num(m.group(2) or m.group(4))
        hi = _num(m.group(3)) if m.group(3) else None
        if lo is None or (strike is not None and lo == strike):
            continue
        if hi is not None:
            hi = _expand(lo, hi)
            a, b = min(lo, hi), max(lo, hi)
            if b > a * 1.6 or b == a:
                hi, a, b = None, lo, lo
            lo, hi = a, b
        if word in ("above", "abv"):
            return {"mode": "above", "lo": lo, "hi": lo, "when_comes": when}
        if word == "below":
            return {"mode": "below", "lo": lo, "hi": lo, "when_comes": when}
        if hi is not None and hi != lo:
            return {"mode": "zone", "lo": lo, "hi": hi, "when_comes": when}
        return {"mode": "at", "lo": lo, "hi": lo, "when_comes": when}
    return {"mode": "market", "lo": None, "hi": None, "when_comes": False}


def _size(text: str) -> Optional[dict]:
    m = re.search(r"(?<![\d.])(\d{1,3})\s*lots?\b", text, re.I)
    if m and int(m.group(1)) > 0:
        return {"lots": int(m.group(1))}
    m = re.search(r"\b(?:qty|quantity)\s*[:\-]?\s*(\d{1,6})\b", text, re.I)
    if m and int(m.group(1)) > 0:
        return {"qty": int(m.group(1))}
    return None


def _stock(text: str, strike_at: Optional[int]) -> Optional[str]:
    """A stock name: the word right before the strike (or after buy / sell) that is no known word."""
    words = re.findall(r"[A-Za-z][A-Za-z&\-]{1,19}", text[:strike_at] if strike_at is not None else text)
    for w in reversed(words[-3:] if strike_at is not None else words[:4]):
        lw = w.lower()
        if lw in STOP_WORDS or re.fullmatch(r"(ce|pe|sl|tgt|tg)", lw):
            continue
        return w.upper()
    return None


def decode(text: str, posted_at, fill: Optional[Callable[[str, list], dict]] = None) -> dict:
    """The hint fields from one post: {symbol, side, instrument, strike, opt_type, expiry, expiry_src, entry, stop,
    targets, open_ended, size_hint, decoded {field: code|ai}}. `fill(text, missing)` (the model) may return values for
    missing fields; a value is kept only when it can be found in the text."""
    text = text or ""
    posted = posted_at if isinstance(posted_at, date) and not isinstance(posted_at, datetime) else \
        (posted_at.date() if isinstance(posted_at, datetime) else date.fromisoformat(str(posted_at)[:10]))
    out: dict = {k: None for k in FIELDS}
    decoded: dict = {}
    sym = symbol_of(text)
    m = STRIKE_RE.search(text)
    strike = opt = None
    strike_at = None
    if m:
        strike = _num(m.group(1) or m.group(4))
        t = (m.group(3) or m.group(5) or "").lower()
        opt = "CE" if t in ("ce", "call", "c") else "PE"
        strike_at = m.start()
    if sym is None and m is not None and re.search(r"\b(buy|sell)\b", text, re.I):
        sym = _stock(text, strike_at)
    if sym is None and m is None:
        bs = re.search(r"\b(buy|sell)\s+([A-Za-z][A-Za-z&\-]{2,19})\b", text, re.I)
        if bs and bs.group(2).lower() not in STOP_WORDS:
            sym = bs.group(2).upper()
    if sym is None and strike is not None and opt:
        sym = index_from_strike(strike)                   # "buy 82400 pe near 680" names no index: the level says SENSEX
        if sym:
            out["symbol"], decoded["symbol"] = sym, "strike"
    if sym and "symbol" not in decoded:
        out["symbol"], decoded["symbol"] = sym, "code"
    if strike is not None:
        out["strike"], out["opt_type"] = strike, opt
        decoded["strike"] = decoded["opt_type"] = "code"
    if sym in COMMODITIES:
        inst = "commodity"
    elif sym in CRYPTO:
        inst = "crypto"
    elif strike is not None:
        inst = "option"
    elif re.search(r"\bfut(ure)?s?\b", text, re.I):
        inst = "future"
    elif sym:
        inst = "cash"
    else:
        inst = "unknown"
    out["instrument"], decoded["instrument"] = inst, "code"
    tg, open_ended = _targets(_segment(text, TGT_RE))
    if tg:
        out["targets"], decoded["targets"] = tg, "code"
    out["open_ended"] = open_ended
    entry = _entry(text, m.end() if m else 0, strike)
    out["entry"] = entry
    if entry["mode"] != "market":
        decoded["entry"] = "code"
    side, how = _side(text, tg, entry.get("lo"))
    if side:
        out["side"], decoded["side"] = side, how
    st = _stop(_segment(text, SL_RE), side, opt)
    out["stop"] = st
    if st["kind"] != "none":
        decoded["stop"] = "code"
    exp = _expiry(text, posted)
    if exp:
        out["expiry"], decoded["expiry"], out["expiry_src"] = exp, "code", "post"
    sz = _size(text)
    if sz:
        out["size_hint"], decoded["size_hint"] = sz, "code"
    if fill is not None:
        missing = [f for f in ("symbol", "side", "strike", "opt_type", "entry", "stop", "targets") if f not in decoded]
        if missing:
            try:
                got = fill(text, missing) or {}
            except Exception:
                got = {}
            _apply_fill(out, decoded, got, text, missing)
    out["decoded"] = decoded
    out["targets"] = out["targets"] or []
    if out["entry"] is None:
        out["entry"] = {"mode": "market", "lo": None, "hi": None, "when_comes": False}
    return out


def _in_text(v, text: str) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return any(abs(float(n) - f) < 1e-9 for n in re.findall(r"\d+(?:\.\d+)?", text))


def _apply_fill(out: dict, decoded: dict, got: dict, text: str, missing: list) -> None:
    """The model's values for the missing fields, each checked against the text (a figure not in the text is dropped)."""
    low = text.lower()
    for f in missing:
        v = got.get(f)
        if v in (None, "", [], {}):
            continue
        if f == "symbol":
            s = str(v).upper().replace(" ", "")
            alias = symbol_of(str(v)) or s
            if symbol_of(text) == alias or s.lower() in low.replace(" ", ""):
                out["symbol"], decoded["symbol"] = alias, "ai"
        elif f == "side":
            s = str(v).upper()
            if s in ("BUY", "SELL"):
                out["side"], decoded["side"] = s, "ai"
        elif f == "opt_type":
            s = str(v).upper()
            if s in ("CE", "PE") and re.search(r"\b" + s.lower() + r"\b|call|put", low):
                out["opt_type"], decoded["opt_type"] = s, "ai"
        elif f == "strike":
            if _in_text(v, text):
                out["strike"], decoded["strike"] = _num(str(v)), "ai"
                if out["instrument"] in ("cash", "unknown"):
                    out["instrument"] = "option"
        elif f == "entry" and isinstance(v, dict):
            lo, hi = v.get("lo"), v.get("hi", v.get("lo"))
            mode = v.get("mode") or ("zone" if hi not in (None, lo) else "at")
            if mode in ("at", "zone", "above", "below") and _in_text(lo, text) and (hi is None or _in_text(hi, text)):
                out["entry"] = {"mode": mode, "lo": _num(str(lo)), "hi": _num(str(hi if hi is not None else lo)),
                                "when_comes": bool(re.search(r"when\s+comes", low))}
                decoded["entry"] = "ai"
        elif f == "stop":
            val = v.get("value") if isinstance(v, dict) else v
            if _in_text(val, text):
                kind = (v.get("kind") if isinstance(v, dict) else None) or "premium"
                out["stop"] = {"kind": "spot" if kind == "spot" else "premium", "value": _num(str(val))}
                if kind == "spot":
                    out["stop"].update(dir=(v.get("dir") if isinstance(v, dict) else None) or "below", tf=15)
                decoded["stop"] = "ai"
        elif f == "targets" and isinstance(v, list):
            keep = [_num(str(x)) for x in v if _in_text(x, text)]
            if keep:
                out["targets"], decoded["targets"] = keep, "ai"


def looks_like_hint(d: dict, text: str) -> bool:
    """A new call, by code: something to trade (a strike + CE/PE, or a commodity / stock name with a side word), a
    price to act on (an entry price with a side word, a stop or targets), and no "done / hit / booked" words (those are
    follow-ups)."""
    if DONE_WORDS.search(text or ""):
        return False
    tradable = (d.get("strike") is not None and d.get("opt_type")) or \
        (d.get("symbol") and d.get("instrument") in ("commodity", "cash", "future", "crypto") and d.get("side"))
    if not tradable:
        return False
    has_side_word = bool(re.search(r"\b(buy|sell)\b", text or "", re.I))
    stop_or_tg = (d.get("stop") or {}).get("kind") not in (None, "none") or bool(d.get("targets"))
    entry_px = (d.get("entry") or {}).get("mode") not in (None, "market")
    return bool(stop_or_tg and (entry_px or has_side_word)) or bool(has_side_word and entry_px)


def title(h: dict) -> str:
    """'BUY BANKNIFTY 50300 PE' / 'SELL CRUDE' / 'BUY RELIANCE'."""
    bits = [h.get("side") or "?", h.get("symbol") or "?"]
    if h.get("strike") is not None:
        s = h["strike"]
        bits.append(f"{s:g}" if isinstance(s, float) else str(s))
    if h.get("opt_type"):
        bits.append(h["opt_type"])
    if h.get("instrument") == "future":
        bits.append("FUT")
    return " ".join(bits)
