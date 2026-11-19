"""📷 Tips: the channel's screenshots read on this server with RapidOCR (code decides, the model never acts).

  capture   reader.py: a Telethon photo / image document (jpg, png, webp; 2 MB at most) of a post not older than 2 days,
            with tips.vision on, is downloaded to data/tips_media/<channel>/<post_id>.<ext> AFTER ingest stored the post
            (never blocking it); the relative path goes to tips_posts.media_path. Failures are logged, never raised.
  read      read_image(path): RapidOCR (rapidocr_onnxruntime: the ONNX models ship inside the wheel, nothing is
            downloaded at runtime) in a worker thread with a 5 s budget (the first use also loads the engine, once);
            a timeout drops the engine instance (rebuilt next time). Any error -> {error}; the post says
            "screenshot not read: <why>". One read per post; never a post older than 2 days, a file over 2 MB or a
            non-image.
  facts     facts(lines): rules only -> {symbol, expiry, strike, opt_type, price, conf, raw}; no strike pattern -> {}.
  use       apply(): the screenshot is linked to a hint like a follow-up (its own post, the quoted post, the open hint
            of the same minute, else the newest open hint of the day). h["shot"] = {post_id, facts, at}. A strike /
            option type / index that differs from the text -> h["shot"]["mismatch"]: a red line on the card with
            "Use screenshot" / "Keep" (book.set_strike / book.keep_strike), one Telegram line, a Home "Needs a look"
            item; in tips.vision_mode "check" auto does not arm while it is open and an armed entry is cancelled. A
            post that named no index takes the screenshot's (decoded.symbol "shot"). Agreeing: a quiet tag.
  retention sweep(): files under data/tips_media older than 48 h are deleted (daily job, the weekly archive run and
            the reader's start). The OCR text in tips_posts.ocr stays forever. The ONLY deletion of downloaded data."""

import json
import re
import threading
import time as _time
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

from backend.config import DATA_DIR, IST
from backend.database.models import get_db
from backend.logging_config.logger import setup_logger
from backend.tips import book as bk
from backend.tips import decode as dc

logger = setup_logger("tips")

MEDIA_DIR = DATA_DIR / "tips_media"
MAX_BYTES = 2 * 1024 * 1024
MAX_AGE_S = 48 * 3600                       # posts and files: 2 days
BUDGET_S = 5.0                              # one read
LOAD_S = 40.0                               # the engine's first load (once per process) on top of the budget
IMAGE_EXT = {"jpg": "jpg", "jpeg": "jpg", "png": "png", "webp": "webp"}
IMAGE_MIMES = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp"}
LINK_WINDOW_S = 15 * 60                     # a screenshot not quoting the hint sets its shot only this soon after the post
JOB = "tips_media"
OLD = "older than 2 days"


def _now() -> datetime:
    return datetime.now(IST)


def vision_on(config) -> bool:
    return bool(getattr(config.tips, "vision", False))


def vmode(config) -> str:
    m = getattr(config.tips, "vision_mode", "report")
    return m if m in ("report", "check") else "report"


def too_old(posted, now: Optional[datetime] = None) -> bool:
    """A post older than 2 days is never read (catch-up, imports, the export)."""
    now = now or _now()
    p = posted if isinstance(posted, datetime) else datetime.fromisoformat(str(posted))
    if p.tzinfo is None:
        p = p.replace(tzinfo=IST)
    return (now - p).total_seconds() > MAX_AGE_S


# --- the engine (lazy, one instance) --------------------------------------------------------------------------------------

def _make_engine():
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR()


ENGINE_FACTORY: Callable = _make_engine     # tests swap a fake in
_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = ENGINE_FACTORY()
        return _engine


def drop_engine() -> None:
    global _engine
    with _engine_lock:
        _engine = None


def _lines_of(res) -> list[dict]:
    """RapidOCR's [[box, text, score], ...] -> [{text, conf, box}]."""
    out = []
    for r in res or []:
        try:
            box, text, score = r[0], r[1], r[2]
        except (TypeError, IndexError):
            continue
        try:
            conf = round(float(score), 3)
        except (TypeError, ValueError):
            conf = None
        out.append({"text": str(text or "").strip(), "conf": conf,
                    "box": [[round(float(x), 1), round(float(y), 1)] for x, y in box] if box is not None else None})
    return [x for x in out if x["text"]]


def read_image(path, budget: float = BUDGET_S) -> dict:
    """{lines [{text, conf, box}], ms, engine} or {error, ms}. Never raises; a timeout drops the engine instance."""
    t0 = _time.monotonic()
    box: dict = {}
    loading = _engine is None

    def work():
        try:
            eng = _get_engine()
            res = eng(str(path))
            res = res[0] if isinstance(res, tuple) else res
            box["lines"] = _lines_of(res)
        except ImportError as exc:
            box["error"] = f"OCR engine not installed ({str(exc)[:80]})"
        except Exception as exc:
            box["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"

    th = threading.Thread(target=work, name="tips-ocr", daemon=True)
    th.start()
    th.join(budget + (LOAD_S if loading else 0))
    ms = int((_time.monotonic() - t0) * 1000)
    if th.is_alive():
        drop_engine()
        return {"error": f"timed out after {budget:g} s", "ms": ms}
    if "error" in box:
        return {"error": box["error"], "ms": ms}
    return {"lines": box.get("lines") or [], "ms": ms, "engine": "rapidocr"}


# --- facts: rules only ----------------------------------------------------------------------------------------------------

STRIKE_SHOT = re.compile(r"(?<![\d.,])(\d{4,6})\s*(CE|PE)\b", re.I)
DATE_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-/]*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:[\s\-/,]+(\d{4}|\d{2})(?!\d))?", re.I)
PRICE_RE = re.compile(r"^(?:₹|rs\.?|inr)?\s*(\d{1,3}(?:,\d{3})+|\d{1,6})\.(\d{1,2})$", re.I)
TOP_ROWS = 6


def _norm(s: str) -> str:
    s = re.sub(r"(\d)\s*\.\s+(\d)", r"\1.\2", s)          # "535. 10" -> "535.10" (OCR splits at the point)
    return re.sub(r"\s+", " ", s).strip()


def _rows(lines: list[dict]) -> list[dict]:
    """Boxes on the same line (the engine reads words apart) joined left to right; rows top to bottom. Lines without
    boxes stay as given. Each row: {text, conf, h (box height), ys}."""
    with_box = [x for x in lines if x.get("box")]
    if len(with_box) != len(lines):
        return [{"text": _norm(x["text"]), "conf": x.get("conf"), "h": 0.0, "parts": [x]} for x in lines]
    items = []
    for x in lines:
        ys = [p[1] for p in x["box"]]
        xs = [p[0] for p in x["box"]]
        items.append((min(ys), max(ys), min(xs), x))
    items.sort(key=lambda t: ((t[0] + t[1]) / 2, t[2]))
    rows: list[list] = []
    for it in items:
        cy, hh = (it[0] + it[1]) / 2, it[1] - it[0]
        if rows:
            r = rows[-1]
            rcy = sum((a[0] + a[1]) / 2 for a in r) / len(r)
            rh = max(a[1] - a[0] for a in r)
            if abs(cy - rcy) <= 0.5 * max(hh, rh):
                r.append(it)
                continue
        rows.append([it])
    out = []
    for r in rows:
        r.sort(key=lambda t: t[2])
        confs = [a[3].get("conf") for a in r if a[3].get("conf") is not None]
        out.append({"text": _norm(" ".join(a[3]["text"] for a in r)), "conf": min(confs) if confs else None,
                    "h": max(a[1] - a[0] for a in r), "parts": [a[3] for a in r]})
    return out


def _expiry(text: str, posted: date) -> Optional[str]:
    m = DATE_RE.search(text)
    if not m:
        return None
    day, mon = int(m.group(1)), dc.MONTHS[m.group(2).lower()[:3]]
    y = m.group(3)
    year = (2000 + int(y) if len(y) == 2 else int(y)) if y else posted.year
    try:
        d = date(year, mon, day)
    except ValueError:
        return None
    if not y and (posted - d).days > 30:
        d = date(year + 1, mon, day)
    return d.isoformat()


def facts(lines: list[dict], posted=None) -> dict:
    """{symbol, expiry, strike, opt_type, price, conf, raw} from the OCR lines, or {} when no strike pattern is found.
    strike = the 4-6 digit number right before CE / PE; symbol = an index name / alias in the text; expiry = a date
    token (dd-Mon-yy, dd Mon, ddth Mon); price = a stand-alone figure with 1-2 decimals near the top (the biggest one
    on the screen; a % change line is never the price); conf = the lowest confidence of the lines used."""
    posted_d = (posted.date() if isinstance(posted, datetime) else posted if isinstance(posted, date)
                else date.fromisoformat(str(posted)[:10]) if posted else _now().date())
    rows = _rows([x for x in (lines or []) if str(x.get("text") or "").strip()])
    if not rows:
        return {}
    used: list = []
    strike_row = next((r for r in rows if STRIKE_SHOT.search(r["text"])), None)
    if strike_row is None:
        return {}
    m = STRIKE_SHOT.search(strike_row["text"])
    strike, opt = float(m.group(1)), m.group(2).upper()
    used.append(strike_row)
    sym_row = strike_row if dc.symbol_of(strike_row["text"]) else next((r for r in rows if dc.symbol_of(r["text"])), None)
    symbol = dc.symbol_of(sym_row["text"]) if sym_row else None
    if symbol not in dc.INDEX_NAMES:
        symbol = None
    elif sym_row is not strike_row:
        used.append(sym_row)
    exp_row = strike_row if DATE_RE.search(strike_row["text"]) else next((r for r in rows if DATE_RE.search(r["text"])), None)
    expiry = _expiry(exp_row["text"], posted_d) if exp_row else None
    if exp_row is not None and expiry and exp_row not in used:
        used.append(exp_row)
    cands = []
    for i, r in enumerate(rows[:TOP_ROWS + rows.index(strike_row)]):
        if r is strike_row:
            continue
        pieces = [(r["text"], r["h"])]
        if len(r["parts"]) > 1:                          # "535.10  +75.25 (+16.36%)" on one line: each box on its own
            pieces += [(_norm(x["text"]), max(p[1] for p in x["box"]) - min(p[1] for p in x["box"]) if x.get("box") else 0.0)
                       for x in r["parts"]]
        for text, hh in pieces:
            t = text.replace(" ", "") if re.fullmatch(r"[₹\d., ]+", text) else text
            pm = PRICE_RE.match(t)
            if not pm or "%" in text:
                continue
            v = float(pm.group(1).replace(",", "") + "." + pm.group(2))
            if v != strike:
                cands.append((hh, v, -i, r))
                break
    price = None
    if cands:
        sized = any(c[0] for c in cands)              # the biggest figure on the screen; without boxes the top-most
        best = max(cands, key=lambda c: (c[0], c[2]) if sized else (c[2],))
        price = best[1]
        used.append(best[3])
    confs = [r["conf"] for r in used if r.get("conf") is not None]
    return {"symbol": symbol, "expiry": expiry, "strike": strike, "opt_type": opt, "price": price,
            "conf": round(min(confs), 3) if confs else None, "raw": " · ".join(r["text"] for r in rows[:4])[:200]}


def first_line(ocr: Optional[dict]) -> Optional[str]:
    """The feed's "📷 read: <first line>" (the rows rebuilt from the boxes)."""
    lines = (ocr or {}).get("lines") or []
    rows = _rows([x for x in lines if str(x.get("text") or "").strip()])
    return rows[0]["text"][:120] if rows else None


# --- capture (the reader thread) ------------------------------------------------------------------------------------------

def media_of(m) -> tuple[Optional[str], Optional[str]]:
    """(the file extension, None) for a Telethon photo / image document, else (None, why not)."""
    photo = getattr(m, "photo", None)
    doc = getattr(m, "document", None)
    f = getattr(m, "file", None)
    size = getattr(f, "size", None) or getattr(doc, "size", None)
    mime = (getattr(f, "mime_type", None) or getattr(doc, "mime_type", None) or "").lower()
    if photo is not None:
        ext = "jpg"
    elif doc is not None and mime in IMAGE_MIMES:
        ext = IMAGE_MIMES[mime]
    elif doc is not None and (getattr(f, "ext", None) or "").lstrip(".").lower() in IMAGE_EXT:
        ext = IMAGE_EXT[(getattr(f, "ext", None) or "").lstrip(".").lower()]
    else:
        return None, "not an image"
    if size and int(size) > MAX_BYTES:
        return None, "larger than 2 MB"
    return ext, None


def _set_ocr(channel: str, post_id: int, ocr: dict, media_path: Optional[str] = None) -> None:
    with get_db() as conn:
        if media_path is not None:
            conn.execute("UPDATE tips_posts SET ocr = ?, media_path = ? WHERE channel = ? AND post_id = ?",
                         (json.dumps(ocr), media_path, channel, int(post_id)))
        else:
            conn.execute("UPDATE tips_posts SET ocr = ? WHERE channel = ? AND post_id = ?", (json.dumps(ocr), channel, int(post_id)))


def _post(channel: str, post_id: int) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM tips_posts WHERE channel = ? AND post_id = ?", (channel, int(post_id))).fetchone()
    return dict(r) if r else None


async def capture(config, client, channel: str, m, loop_run=None) -> Optional[dict]:
    """Download one post's image and read it (the reader's asyncio loop; the read runs in an executor thread so the
    loop keeps serving). Returns what apply() did, or None. Never raises."""
    import asyncio
    try:
        if not vision_on(config) or m is None or getattr(m, "media", None) is None:
            return None
        channel = channel.lstrip("@").lower()
        pid = int(m.id)
        row = _post(channel, pid)
        if not row or row.get("ocr") or row.get("media_path"):
            return None                                       # not stored, or already read once
        when = getattr(m, "date", None)
        if when is not None and too_old(when.astimezone(IST) if when.tzinfo else when.replace(tzinfo=IST)):
            _set_ocr(channel, pid, {"error": OLD, "skipped": True})
            return None
        ext, why = media_of(m)
        if not ext:
            if why != "not an image":
                _set_ocr(channel, pid, {"error": why, "skipped": True})
            return None
        folder = MEDIA_DIR / re.sub(r"[^a-z0-9_.-]+", "_", channel)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{pid}.{ext}"
        got = await client.download_media(m, file=str(target))
        path = Path(got) if got else target
        if not path.exists():
            _set_ocr(channel, pid, {"error": "download failed", "skipped": True})
            return None
        rel = str(path.relative_to(DATA_DIR.parent)) if path.is_relative_to(DATA_DIR.parent) else str(path)
        with get_db() as conn:
            conn.execute("UPDATE tips_posts SET media_path = ? WHERE channel = ? AND post_id = ?", (rel, channel, pid))
        run = loop_run or (lambda fn, *a: asyncio.get_running_loop().run_in_executor(None, fn, *a))
        return await run(process, config, channel, pid)
    except Exception:
        logger.exception("Tips screenshot %s/%s not captured", channel, getattr(m, "id", "?"))
        return None


def media_file(rel: Optional[str]) -> Optional[Path]:
    if not rel:
        return None
    p = Path(rel)
    return p if p.is_absolute() else DATA_DIR.parent / p


def process(config, channel: str, post_id: int, now: Optional[datetime] = None, notify: bool = True) -> Optional[dict]:
    """Read one stored post's image once and use the facts. Never raises."""
    now = now or _now()
    try:
        if not vision_on(config):
            return None
        row = _post(channel, post_id)
        if not row or row.get("ocr"):
            return None
        if too_old(row["posted_at"], now):
            _set_ocr(channel, post_id, {"error": OLD, "skipped": True})
            return None
        path = media_file(row.get("media_path"))
        if path is None or not path.exists():
            _set_ocr(channel, post_id, {"error": "no image file", "skipped": True})
            return None
        if path.suffix.lstrip(".").lower() not in IMAGE_EXT:
            _set_ocr(channel, post_id, {"error": "not an image", "skipped": True})
            return None
        if path.stat().st_size > MAX_BYTES:
            _set_ocr(channel, post_id, {"error": "larger than 2 MB", "skipped": True})
            return None
        res = read_image(path)
        ocr = {"lines": res.get("lines") or [], "ms": res.get("ms"), "engine": res.get("engine"), "error": res.get("error"),
               "facts": None, "at": now.isoformat(timespec="seconds")}
        if not res.get("error"):
            ocr["facts"] = facts(ocr["lines"], datetime.fromisoformat(row["posted_at"]))
        _set_ocr(channel, post_id, ocr)
        if res.get("error"):
            logger.info("Tips screenshot %s/%s not read: %s", channel, post_id, res["error"])
        return apply(config, channel, post_id, now, notify)
    except Exception:
        logger.exception("Tips screenshot %s/%s read failed", channel, post_id)
        return None


# --- use: link, compare, flag -----------------------------------------------------------------------------------------------

def link_hint(conn, row: dict) -> tuple[Optional[dict], str]:
    """(the hint this screenshot belongs to, how): its own post's hint, the quoted post's hint, the open hint posted the
    same minute, else the newest open hint of the same day."""
    if row.get("hint_id"):
        h = bk.load(row["hint_id"], conn)
        if h:
            return h, "own" if row.get("kind") == "hint" else "reply" if row.get("reply_to") else "linked"
    if row.get("reply_to"):
        q = conn.execute("SELECT hint_id FROM tips_posts WHERE channel = ? AND post_id = ? AND hint_id IS NOT NULL",
                         (row["channel"], int(row["reply_to"]))).fetchone()
        if q:
            h = bk.load(q["hint_id"], conn)
            if h:
                return h, "reply"
    hs = [h for h in bk.open_hints(row["channel"], conn) if h["posted_at"][:10] == row["posted_at"][:10] and h["posted_at"] <= row["posted_at"]]
    same = [h for h in hs if h["posted_at"][:16] == row["posted_at"][:16]]
    if same:
        return same[-1], "minute"
    if hs:
        return hs[-1], "newest"
    return None, "none"


def save_shot(h: dict, conn=None) -> None:
    if conn is None:
        with get_db() as c:
            return save_shot(h, c)
    conn.execute("UPDATE tips_hints SET shot = ? WHERE id = ?", (json.dumps(h.get("shot")) if h.get("shot") is not None else None, h["id"]))


def _st(v) -> str:
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else str(f)
    except (TypeError, ValueError):
        return "?"


def contract_text(d: dict, with_symbol: bool = False) -> str:
    return " ".join(x for x in ([d.get("symbol")] if with_symbol and d.get("symbol") else []) + [_st(d.get("strike")), d.get("opt_type") or ""] if x)


def compare(h: dict, f: dict) -> Optional[dict]:
    """{"text": {...}, "shot": {...}} when the screenshot's strike / option type / index differs from the hint's."""
    if not f or f.get("strike") is None:
        return None
    t = {"symbol": h.get("symbol"), "strike": h.get("strike"), "opt_type": h.get("opt_type")}
    s = {"symbol": f.get("symbol"), "strike": f.get("strike"), "opt_type": f.get("opt_type")}
    diff = (t["strike"] is not None and float(t["strike"]) != float(s["strike"])) \
        or (t["opt_type"] and s["opt_type"] and t["opt_type"] != s["opt_type"]) \
        or (t["symbol"] and s["symbol"] and t["symbol"] != s["symbol"] and (h.get("decoded") or {}).get("symbol") not in ("strike", "shot"))
    return {"text": t, "shot": s} if diff else None


def open_mismatch(h: dict) -> Optional[dict]:
    mm = (h.get("shot") or {}).get("mismatch")
    return mm if mm and not mm.get("resolved") else None


def mismatch_line(h: dict) -> str:
    mm = (h.get("shot") or {}).get("mismatch") or {}
    t, s = mm.get("text") or {}, mm.get("shot") or {}
    sym = bool(t.get("symbol") and s.get("symbol") and t["symbol"] != s["symbol"])
    return (f"strike mismatch: text {contract_text(t, sym)}, screenshot {contract_text(s, sym)} · the channel says the "
            "screenshot wins")


def apply(config, channel: str, post_id: int, now: Optional[datetime] = None, notify: bool = True) -> Optional[dict]:
    """Use one read post: link it, set the hint's shot, take the index the post did not name, flag a mismatch. Returns
    {hint_id, how, did} or None."""
    now = now or _now()
    with get_db() as conn:
        r = conn.execute("SELECT * FROM tips_posts WHERE channel = ? AND post_id = ?", (channel, int(post_id))).fetchone()
        row = dict(r) if r else None
        if not row:
            return None
        try:
            ocr = json.loads(row.get("ocr") or "null") or {}
        except ValueError:
            ocr = {}
        h, how = link_hint(conn, row)
        if h is None:
            return {"hint_id": None, "how": how, "did": "stored"}
        if not row.get("hint_id"):
            conn.execute("UPDATE tips_posts SET hint_id = ? WHERE id = ?", (h["id"], row["id"]))
        f = ocr.get("facts") or {}
        close = how in ("own", "reply") or (
            (datetime.fromisoformat(row["posted_at"]) - datetime.fromisoformat(h["posted_at"])).total_seconds() <= LINK_WINDOW_S)
        if h.get("shot") and (h["shot"].get("facts") or {}).get("strike") is not None:
            return {"hint_id": h["id"], "how": how, "did": "follow-up screenshot stored"}
        if not close or not (f.get("strike") is not None or ocr.get("error")):
            return {"hint_id": h["id"], "how": how, "did": "follow-up screenshot stored"}
        shot = {"post_id": int(post_id), "facts": f or None, "at": row["posted_at"], "how": how}
        if ocr.get("error"):
            shot["error"] = ocr["error"]
            h["shot"] = shot
            save_shot(h, conn)
            return {"hint_id": h["id"], "how": how, "did": "not read"}
        did = "agrees"
        if (h.get("decoded") or {}).get("symbol") == "strike" and f.get("symbol") and f["symbol"] != h.get("symbol") \
                and not h.get("taken") and h.get("status") in bk.OPEN_STATUSES:
            old = h.get("symbol")
            _retarget(config, h, symbol=f["symbol"], src="shot")
            bk.event(h, now, f"📷 index from the screenshot: {old or '?'} → {f['symbol']} (the post named none; the band guess replaced)", "code")
            did = "index from the screenshot"
        elif (h.get("decoded") or {}).get("symbol") == "strike" and f.get("symbol") == h.get("symbol"):
            dec = dict(h.get("decoded") or {})
            dec["symbol"] = "shot"
            h["decoded"] = dec
        mm = compare(h, f)
        h["shot"] = shot
        if mm:
            mm.update(at=now.isoformat(timespec="seconds"), resolved=None, mode=vmode(config))
            shot["mismatch"] = mm
            bk.event(h, now, "📷 " + mismatch_line(h), "code")
            did = "mismatch"
        else:
            shot["agrees"] = True
            if did == "agrees":
                bk.event(h, now, "📷 screenshot agrees" + (f" · {_px(f.get('price'))} shown" if f.get("price") is not None else ""), "code")
        bk.save(h, conn)
        save_shot(h, conn)
    if did == "mismatch":
        if notify:
            bk.push(config, f"tips|shot|{h['id']}", f"📷 Tips #{h['id']} · {mismatch_line(h)} · pick the strike on the Tips page")
        if vmode(config) == "check":
            hold(config, h["id"], now, notify)
    elif did == "index from the screenshot":
        _kite_again(config, h["id"], now)
    return {"hint_id": h["id"], "how": how, "did": did}


def _px(v) -> str:
    """A premium as the broker shows it: two decimals (535.10)."""
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def _retarget(config, h: dict, symbol: Optional[str] = None, strike=None, opt_type: Optional[str] = None, src: str = "owner") -> None:
    """The contract of a hint not taken yet: symbol / strike / option type replaced, resolved, the plan rebuilt."""
    from backend.tips import plan as pl
    from backend.tips import track as tr
    dec = dict(h.get("decoded") or {})
    if (symbol and symbol != h.get("symbol")) and h.get("expiry_src") != "post":
        h["expiry"], h["expiry_src"] = None, None              # the old index's listed / computed expiry goes with it
    if symbol:
        h["symbol"], h["instrument"] = symbol, "option"
        dec["symbol"] = src
    if strike is not None:
        h["strike"] = float(strike)
        dec["strike"] = src
    if opt_type:
        h["opt_type"] = opt_type
        dec["opt_type"] = src
    h["decoded"] = dec
    for k in ("tradingsymbol", "lot_size"):
        h[k] = None
    posted = datetime.fromisoformat(h["posted_at"])
    h["exchange"] = tr.exchange_of(h)
    tr.resolve(h, None, posted.date())
    h["plan"] = pl.build(h, config.tips, posted)
    run = h.get("run") or {}
    h["run"] = pl.new_run(h["plan"])
    h["run"]["allowed"], h["run"]["paused"] = bool(run.get("allowed")), bool(run.get("paused"))
    h["tracked"] = bool(h.get("tradingsymbol"))
    if not h["tracked"]:
        h["untracked_why"] = h.get("untracked_why") or "no instrument"
    else:
        h["untracked_why"] = None
    bk.refresh(h)


def hold(config, hid: int, now: datetime, notify: bool = True) -> Optional[str]:
    """vision_mode "check": an armed / resting Kite entry of a hint with an open mismatch is cancelled (auto re-arms after
    the choice). A filled one is only flagged."""
    try:
        from backend.tips import kite_auto as ka
        from backend.tips import kite_orders as ko
        h = bk.load(hid)
        if not h:
            return None
        k = ko.kstate(h)
        if k["state"] in ko.ARMED:
            cl = ko.sim() if k.get("dry") else ka.app_client()
            with ko._LOCK:
                ko.disarm(config, cl, h, now, "strike mismatch: pick the strike", notify)
                h = bk.load(hid)
                k = ko.kstate(h)
                k["auto"] = {**(k.get("auto") or {}), "rearm": True}
                ko.save_kite(h)
            return "cancelled"
        if k["state"] == "entered":
            ko._log(k, now, "Kite: the screenshot shows another strike · the entry has filled · check the position on Kite")
            ko.save_kite(h)
            return "flagged"
    except Exception:
        logger.exception("Tips screenshot hold #%s failed", hid)
    return None


def _kite_again(config, hid: int, now: datetime) -> None:
    try:
        from backend.tips import kite_auto as ka
        ka.on_new(config, ka.app_client(), hid, now)
    except Exception:
        logger.exception("Tips Kite auto after the screenshot failed")


def blocks_auto(config, h: dict) -> Optional[str]:
    """The auto guard: in vision_mode "check" an open mismatch holds the hint."""
    if vmode(config) == "check" and open_mismatch(h):
        return "strike mismatch: pick the strike"
    return None


# --- the view -------------------------------------------------------------------------------------------------------------------

def shot_view(h: dict) -> Optional[dict]:
    """The card's screenshot line: {state agrees|mismatch|resolved|error, label, facts, price, time, text?, buttons?}."""
    s = h.get("shot")
    if not s:
        return None
    from backend.timefmt import ampm
    f = s.get("facts") or {}
    try:
        t = ampm(datetime.fromisoformat(s["at"])) if s.get("at") else None
    except ValueError:
        t = None
    out = {"post_id": s.get("post_id"), "facts": f, "price": f.get("price"), "time": t, "at": s.get("at")}
    if s.get("error"):
        return {**out, "state": "error", "label": f"screenshot not read: {s['error']}"}
    contract = " ".join(x for x in (f.get("symbol"), _st(f.get("strike")), f.get("opt_type")) if x and x != "?")
    price = f" · {_px(f['price'])}" if f.get("price") is not None else ""
    mm = s.get("mismatch")
    if mm and not mm.get("resolved"):
        t_, s_ = mm.get("text") or {}, mm.get("shot") or {}
        return {**out, "state": "mismatch", "label": f"📷 screenshot: {contract}{price} · ✗ differs", "text": mismatch_line(h),
                "use": {"strike": s_.get("strike"), "opt_type": s_.get("opt_type"), "symbol": s_.get("symbol")},
                "use_label": f"Use screenshot {contract_text(s_)}", "keep_label": f"Keep {contract_text(t_)}"}
    if mm:
        ch = mm["resolved"].get("choice")
        return {**out, "state": "resolved", "label": f"📷 screenshot: {contract}{price} · " + ("screenshot's strike used" if ch == "shot" else "text kept")}
    return {**out, "state": "agrees", "label": f"📷 screenshot: {contract}{price} · ✓ agrees",
            "quiet": "✓ screenshot agrees" + (f" · {_px(f['price'])} shown" if f.get("price") is not None else "")}


def attention_items(now: Optional[datetime] = None) -> list[tuple[str, str]]:
    """Home "Needs a look": every open hint of today with an unresolved screenshot mismatch."""
    now = now or _now()
    out = []
    for h in bk.rows("shot IS NOT NULL AND json_extract(shot, '$.mismatch') IS NOT NULL AND json_extract(shot, '$.mismatch.resolved') IS NULL",
                     order="id"):
        if h["posted_at"][:10] != now.date().isoformat() or h.get("status") not in bk.OPEN_STATUSES:
            continue
        out.append(("warn", f"📷 Tips #{h['id']} · {mismatch_line(h)} → pick the strike on the Tips page"))
    return out[:4]


# --- retention -----------------------------------------------------------------------------------------------------------------

def sweep(now: Optional[datetime] = None, root: Optional[Path] = None) -> dict:
    """Delete the image files under data/tips_media older than 48 h (by file time). The OCR text on the post stays.
    {removed, kept, errors}. Never raises."""
    root = root or MEDIA_DIR
    cut = (now or _now()).timestamp() - MAX_AGE_S
    removed = kept = errors = 0
    if not root.exists():
        return {"removed": 0, "kept": 0, "errors": 0}
    for p in root.rglob("*"):
        try:
            if not p.is_file():
                continue
            if p.stat().st_mtime < cut:
                p.unlink()
                removed += 1
            else:
                kept += 1
        except OSError:
            errors += 1
    if removed:
        logger.info("Tips media sweep: %d image file(s) older than 2 days removed (the OCR text stays)", removed)
    return {"removed": removed, "kept": kept, "errors": errors}


def due(now: datetime, done: set) -> list[str]:
    """["tips_media"] once a day (any time) while the media folder exists."""
    if JOB in done or not MEDIA_DIR.exists():
        return []
    return [JOB]


def import_note(config, conn, channel: str, post_id: int, posted: datetime, has_media: bool) -> None:
    """The export importer never downloads: an image post gets the reason (older than 2 days, or no file)."""
    if not (vision_on(config) and has_media):
        return
    why = OLD if too_old(posted) else "no image file in the export"
    conn.execute("UPDATE tips_posts SET ocr = ? WHERE channel = ? AND post_id = ? AND ocr IS NULL",
                 (json.dumps({"error": why, "skipped": True}), channel, int(post_id)))

