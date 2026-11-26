"""💡 Catch: the channel's posts reach the app, each stored once in tips_posts with its kind (never deleted).

  reader    a Telegram user client (Telethon) on your own account, in its own thread with its own asyncio loop: new
            posts in about a second, edits and deletions too. It starts only when tips.enabled is on and a session
            exists (TIPS_TG_API_ID / TIPS_TG_API_HASH / TIPS_TG_SESSION in .env; the session string is written once by
            the login flow and never logged). Any failure: state "down" with the reason, never a crash of the app.
  preview   while the reader is down (tips.preview_fallback): the channel's public page t.me/s/<channel> every 30 s,
            parsed as HTML (no login), state "preview".
  import    the Telegram HTML export (messages.html, messages2.html, ...): the history and the record from day one;
            code only (no model), never pushes an alert.
  ingest    one post -> filter (code, then the model) -> decode -> hint + plan, or a follow-up linked to an open hint
            (the paper book follows the channel's exit when auto_follow_channel_exit is on) -> the Telegram line."""

import asyncio
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Callable, Optional

from backend.config import BASE_DIR, DATA_DIR, IST
from backend.database.models import get_db, get_setting, now_ist, set_setting
from backend.logging_config.logger import setup_logger
from backend.tips import book as bk
from backend.tips import decode as dc
from backend.tips import filter as flt
from backend.tips import plan as pl
from backend.tips import track as tr

logger = setup_logger("tips")

ENV_FILE = BASE_DIR / ".env"
SESSION_FILE = DATA_DIR / ".tips_session"
PREVIEW_URL = "https://t.me/s/{channel}"
PREVIEW_EVERY_S = 30
RETRY_READER_S = 60
CATCH_UP = 20                       # posts read back per channel when the reader (re)connects
PUSH_MAX_AGE_S = 15 * 60            # a caught-up post older than this is stored, not pushed
STATES = ("live", "preview", "down", "logged_out")


# --- secrets ------------------------------------------------------------------------------------------------------

def api_creds() -> tuple[Optional[int], Optional[str]]:
    raw, h = (os.getenv("TIPS_TG_API_ID") or "").strip(), (os.getenv("TIPS_TG_API_HASH") or "").strip()
    try:
        return (int(raw) if raw else None), (h or None)
    except ValueError:
        return None, h or None


def session_string() -> Optional[str]:
    s = (os.getenv("TIPS_TG_SESSION") or "").strip()
    if s:
        return s
    try:
        if SESSION_FILE.exists():
            return SESSION_FILE.read_text().strip() or None
    except OSError:
        pass
    return None


def save_session(value: str) -> str:
    """Write TIPS_TG_SESSION into .env (replacing the line), else data/.tips_session (0600). Returns where it went
    (never the value)."""
    os.environ["TIPS_TG_SESSION"] = value
    try:
        lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
        out, done = [], False
        for ln in lines:
            if ln.startswith("TIPS_TG_SESSION="):
                out.append(f"TIPS_TG_SESSION={value}")
                done = True
            else:
                out.append(ln)
        if not done:
            out.append(f"TIPS_TG_SESSION={value}")
        ENV_FILE.write_text("\n".join(out) + "\n")
        return ".env"
    except OSError:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(value)
        try:
            os.chmod(SESSION_FILE, 0o600)
        except OSError:
            pass
        return "data/.tips_session"


def mask(v: Optional[str]) -> Optional[str]:
    """'+919876543210' -> '+91•••••3210'; '@name' -> '@na•••'."""
    if not v:
        return None
    v = str(v)
    if v.lstrip("+").isdigit():
        return v[:3] + "•" * max(0, len(v) - 7) + v[-4:]
    return v[:3] + "•••"


# --- reader state (app_settings "tips.reader", so the page and the seed read the same thing) ------------------------------

def status() -> dict:
    with get_db() as conn:
        raw = get_setting(conn, "tips.reader")
    st = json.loads(raw) if raw else {}
    return {"state": st.get("state") or ("logged_out" if not session_string() else "down"), "reason": st.get("reason"),
            "account_masked": st.get("account_masked"), "last_post_at": st.get("last_post_at"), "lag_ms": st.get("lag_ms"),
            "via": st.get("via"), "since": st.get("since"), "channels": channels_of(None),
            "relayed_today": int(st.get("relayed_today") or 0) if st.get("relay_day") == datetime.now(IST).date().isoformat() else 0}


def set_status(**kw) -> dict:
    with get_db() as conn:
        st = json.loads(get_setting(conn, "tips.reader") or "{}")
        if "state" in kw and kw["state"] != st.get("state"):
            st["since"] = now_ist()
        st.update(kw)
        set_setting(conn, "tips.reader", json.dumps(st))
    return st


_channels_cfg: list = []


def split_channels(raw: str) -> list[str]:
    """tips.channels → entries. New lines and semicolons separate; a comma separates too, but only between plain
    usernames: a private channel's title keeps its commas ("Index Options, Buying with Aakanksha")."""
    out = []
    for part in re.split(r"[;\n]+", str(raw or "")):
        part = part.strip()
        if not part:
            continue
        pieces = part.split(",")                       # usernames only: every piece a plain @name
        if not all(re.fullmatch(r"\s*@?[A-Za-z0-9_]+\s*", p) for p in pieces if p.strip()):
            pieces = [part]                            # a title: its commas stay
        out.extend(p.strip().lstrip("@") for p in pieces if p.strip())
    return out


def channels_of(config) -> list[str]:
    global _channels_cfg
    if config is not None:
        _channels_cfg = [c.lower() for c in split_channels(config.tips.channels)]
    return list(_channels_cfg)


USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{3,31}$")


def is_username(ch: str) -> bool:
    """A public channel's @username (letters, digits, _), as against a private channel's title or numeric id."""
    return bool(USERNAME_RE.match((ch or "").strip().lstrip("@").lower()))


async def find_channel(client, ch: str):
    """The Telethon entity of a channels entry: a username straight away; a title (case-insensitive, whole or the start)
    or a numeric id among the chats the account is in (a private channel has no username)."""
    want = (ch or "").strip().lstrip("@").lower()
    if is_username(want):
        return await client.get_entity(want)
    digits = want.lstrip("-")
    plain = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()      # noqa: E731  punctuation and extra spaces never matter
    want_p = plain(want)
    async for d in client.iter_dialogs():
        ent = getattr(d, "entity", None)
        title = plain(getattr(d, "title", None) or getattr(ent, "title", None) or "")
        if digits.isdigit() and str(abs(int(getattr(ent, "id", 0) or 0))) == digits:
            return ent
        if title and want_p and (title == want_p or title.startswith(want_p) or want_p in title):
            return ent
    raise LookupError(f"no chat titled {ch!r} among the account's chats")


# --- ingest --------------------------------------------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).isoformat(timespec="seconds")


def ingest(config, channel: str, post_id: int, posted_at, text: str, has_media: bool = False, source: str = "live",
           push: bool = True, model: bool = True, client=None, conn=None, reply_to: Optional[int] = None) -> dict:
    """Store one post (once per channel + id), sort it, make the hint or link the follow-up. `reply_to` is the post this
    one replies to (Telegram's quote): a reply to a hint's post is that hint's follow-up unless it is a new call itself.
    Returns {post_id, kind, hint_id, new, line}."""
    if conn is None:
        with get_db() as c:
            out = ingest(config, channel, post_id, posted_at, text, has_media, source, push, model, client, c, reply_to)
        if out.get("channel_stop") is not None and out.get("hint_id"):   # 🛑 the DB connection is closed: the Kite lock next
            at = datetime.fromisoformat(out["at"])
            fresh = (datetime.now(IST) - at).total_seconds() <= PUSH_MAX_AGE_S
            try:
                from backend.tips import kite_orders as ko
                h0 = bk.load(out["hint_id"]) or {}
                loud = flt.held(h0) or push_level(config) == "all"     # T6: the SL line pushed only for a hint armed / filled
                ln = bk.channel_stop(config, out["hint_id"], out["channel_stop"], text, at, client=client,
                                     kite=fresh and ko.mode(config) != "off", notify=push and fresh and loud)
                if ln:
                    out["line"] = ln
                if not loud and out.get("push_if_unheld"):
                    out["push"] = out["push_if_unheld"]
            except Exception:
                logger.exception("Tips: the channel's SL not applied")
        live = out.get("new") and source in ("live", "preview")
        held_back = None                                  # T3 / T4: pushed after the Kite step, with its outcome folded in
        if live and out.get("push") and (out.get("kind") == "hint" or (out.get("plan_line") and _auto(config))):
            held_back = out.pop("push")
        for key in ("day_push", "push"):
            if out.get(key):
                bk.push(config, out[key][0], out[key][1])
        if live:
            from backend.tips import ai4                  # 🤖 the call grade (code) before any arming; never acts
            ai4.on_post(config, out)
            armed = kite_after(config, client, out, push, fold=None if held_back is None else "all" if out.get("kind") == "hint" else "armed")
            if held_back is not None:
                bk.push(config, *folded(config, out, held_back, armed))
            if out.get("kind") == "hint" and out.get("hint_id"):
                try:                                      # 📈 the market read: queued AFTER the plan and any arm, never waited on
                    from backend.market import reads as mr
                    mr.queue_hint(config, out["hint_id"], "ingest")
                except Exception:
                    logger.exception("Market read not queued")
        return out
    channel = channel.lstrip("@").lower()
    posted = posted_at if isinstance(posted_at, datetime) else datetime.fromisoformat(str(posted_at))
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=IST)
    posted = posted.astimezone(IST)
    text = (text or "").strip()
    row = conn.execute("SELECT id, kind, hint_id FROM tips_posts WHERE channel = ? AND post_id = ?", (channel, int(post_id))).fetchone()
    if row:
        return {"post_id": post_id, "kind": row["kind"], "hint_id": row["hint_id"], "new": False}
    hints = [h for h in bk.open_hints(channel, conn) if h["posted_at"][:10] == posted.date().isoformat() or h.get("status") in bk.OPEN_STATUSES]
    if source == "import":                              # the export: only the same day's hints within 90 minutes
        hints = [h for h in bk.rows("channel = ? AND posted_at <= ? AND posted_at >= ?", (channel, posted.isoformat(timespec="seconds"),
                 (posted - timedelta(minutes=90)).isoformat(timespec="seconds")), order="id", conn=conn)]
    kind = flt.code_kind(text, has_media, flt.open_levels(hints))
    by, mdl, ms, hint_id = "code", None, None, None
    d = None
    quoted = None                                       # the hint this post replies to, when it replies to a hint's post
    if reply_to:
        q = conn.execute("SELECT hint_id FROM tips_posts WHERE channel = ? AND post_id = ? AND hint_id IS NOT NULL",
                         (channel, int(reply_to))).fetchone()
        quoted = bk.load(q["hint_id"], conn) if q else None
    if quoted is not None and kind in (None, "chatter", "image"):
        d = dc.decode(text, posted)
        same = (d.get("symbol") in (None, quoted.get("symbol"))
                and (d.get("strike") in (None, quoted.get("strike")))
                and (d.get("opt_type") in (None, quoted.get("opt_type"))))
        if dc.looks_like_hint(d, text) and not same:
            kind = "hint"                               # a new call posted as a reply
        else:
            kind, hint_id, by = "follow_up", quoted["id"], "reply"
    repeat = False
    if kind is None:
        d = dc.decode(text, posted)
        rep = flt.repeat_call(d, hints, posted) if dc.looks_like_hint(d, text) else None
        if rep:                                         # 🔁 the same call again within 15 min: that hint's follow-up
            kind, hint_id, repeat = "follow_up", rep, True
        elif dc.looks_like_hint(d, text):
            kind = "hint"
        else:
            sf = flt.strong_followup(text, hints, posted)   # 💪 avoid / active / a result / a booking / wait: code, not the model
            pp = None if sf else flt.plan_post(text, hints, posted)      # 📌 "Sl 530" / "Tgt 830/880/940" for an open call lacking them
            res = None if (pp or sf) else (flt.model_classify(config, text, hints) if model and source != "import" else None)
            if sf:
                kind, hint_id = "follow_up", sf
            elif pp:
                kind, hint_id = "follow_up", pp
            elif res:
                kind, hint_id, by, mdl, ms = res["kind"], res["hint_id"], "ai", res["model"], res["ms"]
                if kind == "hint":
                    fill = flt.model_fill(config)
                    d = dc.decode(text, posted, fill)
                    if not ((d.get("strike") is not None and d.get("opt_type")) or d.get("symbol")):
                        kind = "chatter"
            else:
                kind, hint_id = flt.code_classify(text, hints)
    elif kind == "chatter" and hints:                   # "wait for next level": no trade word, still the channel's word on a call
        sf = flt.strong_followup(text, hints, posted)
        if sf:
            kind, hint_id = "follow_up", sf
    cur = conn.execute("INSERT INTO tips_posts (channel, post_id, posted_at, text, has_media, kind, hint_id, classified_by, model, ms, source, created, reply_to) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (channel, int(post_id), posted.isoformat(timespec="seconds"), text, int(bool(has_media)),
                                                              kind, hint_id, by, mdl, ms, source, now_ist(), reply_to))
    out = {"post_id": post_id, "kind": kind, "hint_id": hint_id, "new": True, "row": cur.lastrowid, "at": posted.isoformat(timespec="seconds")}
    if source == "import" and has_media:                 # 📷 the export never downloads: the reason is stored
        from backend.tips import vision as vs
        vs.import_note(config, conn, channel, int(post_id), posted, has_media)
    live = source in ("live", "preview")
    fresh = (datetime.now(IST) - posted).total_seconds() <= PUSH_MAX_AGE_S
    if kind == "hint":
        h = new_hint(config, conn, channel, int(post_id), posted, text, d, source, client)
        conn.execute("UPDATE tips_posts SET hint_id = ? WHERE id = ?", (h["id"], cur.lastrowid))
        out["hint_id"] = h["id"]
        if live:                                         # a new call the same day: the channel is trading again
            from backend.tips import kite_auto as ka
            ln = ka.reopen_day(conn, h["id"], posted)
            if ln:
                bk.event(h, posted, "day re-opened by a new call", "code")
                bk.save(h, conn)
                if push and fresh:
                    out["day_push"] = (f"tips|day|open|{posted.date().isoformat()}|{h['id']}", ln)
    elif live and pl.day_closed(text):                   # "temporarily close the day": nothing new armed today
        from backend.tips import kite_auto as ka
        ln = ka.close_day(conn, channel, int(post_id), posted, text)
        if ln:
            out["day_closed"] = True
            if push and fresh:
                out["day_push"] = (f"tips|day|close|{posted.date().isoformat()}|{post_id}", ln)
    if kind == "hint":
        if push and source in ("live", "preview") and config.tips.push_hints and fresh:
            from backend.alerts import messages as msg
            out["push"] = (f"tips|hint|{h['id']}", msg.tips_hint(h) + size_line(config, h)
                           + "".join(f"\n📏 {add_line(config, a)}" for a in (h.get("adds") or [])))
    elif kind == "follow_up" and hint_id:
        h = bk.load(hint_id, conn)
        if h:
            act = pl.followup_action(text) if not repeat else {"action": "repeat", "lots": ((d or {}).get("size_hint") or {}).get("lots")}
            now = posted                                     # the post's own time (Telegram's clock, ~1 s behind)
            cst = channel_stop_of(h, text, posted, act)      # 🛑 the channel's SL over a provisional / your stop
            filled = complete_plan(config, h, text, posted, act)   # "SL 410 / TGT 520/550/600+" posted after the call
            done = repeat_line(h, filled) if repeat else filled
            if source == "import":
                bk.event(h, posted, f'channel: "{text[:80]}"', "channel")
                line = "follow-up"
            elif cst is not None:                            # applied after the post is stored (book.channel_stop, the Kite lock)
                if done:
                    bk.event(h, posted, f'channel: "{text[:80]}" · {done}', "channel")
                line = done or f"the channel's SL {pl.fmt(cst)}"
                out["channel_stop"] = cst
            elif done:
                bk.event(h, posted, f'channel: "{text[:80]}" · {done}', "channel")
                line = done
            else:
                line = bk.channel_action(config, h, act, text, now, conn=conn)
            sz = resize_hint(config, h, act, posted)
            if sz:
                line = f"{line} · {sz}"
            bk.save(h, conn)
            out["line"] = line
            out["act"], out["text"], out["at"] = act, text, posted.isoformat(timespec="seconds")
            out["plan_line"] = filled
            out["quiet"] = quiet_followup(config, h, act, text, line, bool(filled or cst is not None or sz))
            if push and source in ("live", "preview") and config.tips.push_followups and not out["quiet"]:
                from backend.alerts import messages as msg
                fu = (f"tips|fu|{channel}|{post_id}", msg.tips_followup(h, text, line, now, act))
                if out.get("channel_stop") is None:
                    out["push"] = fu
                elif done:                                   # the channel's SL pushes its own line only when held (T6):
                    out["push_if_unheld"] = (fu[0], msg.tips_followup(h, text, done, now, act))     # the plan line then
    return out


QUIET_ACTS = ("price", "note", "wait")
DEFENSIVE_SUMMARY = re.compile(r"^\W*defensive\s+style\b", re.I)
NO_TARGET_YET = "no target reached on our quotes yet"


def push_level(config) -> str:
    """tips.push_level: normal (the quiet follow-ups go to the card log only) | all (every follow-up pushed, as before)."""
    v = str(getattr(config.tips, "push_level", "normal") or "normal").lower()
    return v if v in ("normal", "all") else "normal"


def quiet_followup(config, h: dict, act: Optional[dict], text: str, line: str, changed: bool) -> bool:
    """🔕 A follow-up logged on the card, not pushed (push_level normal): a price / note / wait post or the channel's
    "Defensive Style …" summary that changed nothing (T1); "no target reached on our quotes yet" after its first time
    on a hint, or on a hint we neither hold nor have armed (T2)."""
    if push_level(config) == "all" or changed:
        return False
    a = (act or {}).get("action")
    if a in QUIET_ACTS or a == "repeat" or DEFENSIVE_SUMMARY.match(text or ""):
        return True
    if (line or "").startswith(NO_TARGET_YET):
        if not flt.held(h):
            return True
        return sum(NO_TARGET_YET in str(e.get("text") or "") for e in h.get("events") or []) > 1
    return False


def repeat_line(h: dict, done: Optional[str]) -> str:
    """🔁 "the call again · stop 470 · targets 625/660/700/750/800" (what the repost filled, else the plan as it stands)."""
    if done:
        return "the call again · " + done.replace("plan completed: ", "", 1)
    st = h.get("stop") or {}
    bits = [f"stop {pl.fmt(st['value'])}" if st.get("value") is not None else "no stop"]
    tg = h.get("targets") or []
    bits.append("targets " + "/".join(pl.fmt(t) for t in tg) if tg else "no targets")
    return "the call again · " + " · ".join(bits) + " · nothing new"


def _auto(config) -> bool:
    try:
        from backend.tips import kite_orders as ko
        return ko.mode(config) == "auto"
    except Exception:
        return False


def folded(config, out: dict, held_back: tuple, armed: Optional[dict]) -> tuple:
    """(key, html) of the message held back for the Kite step: the new call with its "auto: …" line last (T3), or a
    completed plan that auto then armed as ONE line "#27 · stop 640 · targets 820/860/900 · armed 2 lots · SL trigger
    750 · resting on Kite" (T4; the plan line alone when nothing was armed)."""
    key, html = held_back
    try:
        from backend.tips import kite_auto as ka
        h = bk.load(out["hint_id"]) if out.get("hint_id") else None
        if not h:
            return key, html
        if out.get("kind") == "hint":
            return key, html + "\n" + ka.state_line(config, h)
        if armed:
            st = h.get("stop") or {}
            tg = h.get("targets") or []
            bits = [f"#{h['id']}"] + ([f"stop {pl.fmt(st['value'])}"] if st.get("value") is not None else []) \
                + (["targets " + "/".join(pl.fmt(t) for t in tg)] if tg else []) + [ka.armed_bits(config, h)]
            return f"tips|kite|auto|{h['id']}|armed", ka.apfx(h) + " · ".join(bits)
    except Exception:
        logger.exception("Tips: the folded message failed")
    return key, html


def kite_after(config, client, out: dict, push: bool = True, fold: Optional[str] = None) -> Optional[dict]:
    """⚡ Tips → Kite (PR C), after the post is stored: a follow-up's Kite consequence (any mode: an add-on is never an
    order, avoid / SL hit before the fill cancel an unfilled entry, "active" sends an armed one; auto: the channel's exit
    / trail / SL / part booking on its own position) and, in auto, the hint armed when it is (now) placeable. Never
    raises: a failure is logged, the post is kept."""
    try:
        from backend.tips import kite_auto as ka
        from backend.tips import kite_orders as ko
        if ko.mode(config) == "off" and out.get("kind") != "follow_up":
            return None
        now = datetime.fromisoformat(out["at"]) if out.get("at") else datetime.now(IST)   # the post's own time (~1 s behind)
        if (datetime.now(IST) - now).total_seconds() > PUSH_MAX_AGE_S:
            return None                                    # a caught-up old post never moves a Kite order
        loud = push and not out.get("quiet")               # 🔕 a quiet follow-up (push_level normal): the card log only
        if out.get("kind") == "follow_up" and out.get("hint_id") and out.get("act") and out.get("channel_stop") is None:
            kline = ka.on_followup(config, client, out["hint_id"], out["act"], out.get("text") or "", now, notify=loud)
            try:                                           # 🤖 the AI second opinion (informational: never acts)
                from backend.tips import kite_check as kc
                kc.enqueue_followup(config, out["hint_id"], out.get("post_id"), out.get("text") or "", out["act"], kline or out.get("line"), notify=loud)
            except Exception:
                logger.exception("Tips follow-up AI read not queued")
        if out.get("kind") in ("hint", "follow_up") and out.get("hint_id"):
            return ka.on_new(config, client, out["hint_id"], now, notify=push, fold=fold)
    except Exception:
        logger.exception("Tips Kite after the post failed")
    return None


def resize_hint(config, h: dict, act: dict, posted: datetime) -> Optional[str]:
    """📏 "2-2 lots" / "buy 2 lots here" (post mode, nothing filled yet): the hint's size becomes the channel's (the paper
    take and the Kite entry use it; an armed Kite entry is re-planned by kite_auto). After a fill: logged only."""
    a = (act or {}).get("action")
    n = (act or {}).get("size_lots") if a == "add" else (act or {}).get("lots")     # 📏 "2 lots\n\nAdd 2 lots near 700": the size too
    if not n or a not in ("size", "active", "add", "repeat"):
        return None
    if a == "repeat" and (h.get("size_hint") or {}).get("lots"):
        return None                                     # 🔁 the call again: fills a size the hint lacks, never changes one
    k = h.get("kite") if isinstance(h.get("kite"), dict) else {}
    if h.get("taken") or k.get("state") in ("entered", "closed"):
        return f"size {n} lots logged (already filled)"
    if str(getattr(config.tips, "kite_size_mode", "post")) != "post":
        return f"size {n} lots noted (kite_size_mode {config.tips.kite_size_mode})"
    old = (h.get("size_hint") or {}).get("lots")
    h["size_hint"] = {"lots": int(n)}
    bk.event(h, posted, f"size from the channel: {int(n)} lot{'s' if int(n) != 1 else ''}" + (f" (was {old})" if old else ""), "channel")
    return f"size → {int(n)} lots"


def channel_stop_of(h: dict, text: str, posted: datetime, act: Optional[dict]) -> Optional[float]:
    """🛑 The channel's premium SL in this follow-up when the hint's stop is provisional or yours (it replaces it), else
    None: "Sl 530" (the plan post) or "revise sl 530" (a stop action)."""
    if pl.stop_src(h.get("stop")) not in pl.REPLACEABLE:
        return None
    a = (act or {}).get("action")
    if a == "stop" and not act.get("spot") and act.get("value") is not None:
        return float(act["value"])
    if a in PLAN_SKIP_ACTIONS:
        return None
    st = dc.decode(text, posted).get("stop") or {}
    if st.get("kind") == "premium" and st.get("value") is not None:
        return float(st["value"])
    return None


PLAN_SKIP_ACTIONS = ("targets", "sl_hit", "exit", "partial", "trail")     # "tgt 1 done" states a result, not a plan


def complete_plan(config, h: dict, text: str, posted: datetime, act: Optional[dict] = None) -> Optional[str]:
    """A follow-up that carries the stop and / or targets the call itself lacked (this channel posts "BUY SENSEX 82000
    CE NEAR 460" first and "SL 410 / TGT 520/550/600+" as a reply): fill them in, rebuild the plan and restart the
    record run when nothing has filled yet. Returns the line, or None when the follow-up adds nothing. A post that
    reports a result ("tgt 1 done", "SL hit") never sets a level. A provisional stop / yours counts as a stop here: the
    channel's SL replacing it is applied by channel_stop_of → book.channel_stop (under the Kite lock, after the post is
    stored)."""
    if (act or {}).get("action") in PLAN_SKIP_ACTIONS:
        return None
    d = dc.decode(text, posted)
    st, tg = d.get("stop") or {}, [t for t in (d.get("targets") or []) if t is not None]
    if not tg and not h.get("targets"):
        tg = flt.bare_targets(text, h) or []            # 🎯 "625/660/700/750/800" alone: the call's targets
    ref = (h.get("entry") or {}).get("lo") or (h.get("entry") or {}).get("hi")
    if ref and tg and any(t < 0.2 * ref for t in tg):                 # a target far under the entry is a count, not a price
        tg = []
    had_stop = ((h.get("stop") or {}).get("kind") or "none") != "none"
    had_tg = bool(h.get("targets"))
    bits = []
    if st.get("kind", "none") != "none" and st.get("value") is not None and not had_stop:
        h["stop"] = st
        bits.append(f"stop {pl.fmt(st['value'])}")
    if tg and not had_tg:
        h["targets"], h["open_ended"] = tg, bool(d.get("open_ended"))
        bits.append("targets " + "/".join(pl.fmt(t) for t in tg) + ("+" if d.get("open_ended") else ""))
    elif tg and had_tg and tg != list(h.get("targets") or []) and flt.recent_targets(h, posted):
        old_tg = "/".join(pl.fmt(t) for t in h["targets"])      # the channel's correction a moment after its own targets post
        h["targets"], h["open_ended"] = tg, bool(d.get("open_ended"))
        bits.append(f"targets revised {old_tg} → " + "/".join(pl.fmt(t) for t in tg) + ("+" if d.get("open_ended") else ""))
    if not bits:
        return None
    h["plan"] = pl.build(h, config.tips, posted)
    run = h.get("run") or {}
    if not h.get("taken") and run.get("state") in (None, "waiting"):
        h["run"] = pl.new_run(h["plan"])
        if run.get("allowed"):
            h["run"]["allowed"] = True
    return "plan completed: " + " · ".join(bits) + ("" if h["plan"]["ok"] else f" · still no plan: {h['plan']['why']}")


def new_hint(config, conn, channel: str, post_id: int, posted: datetime, text: str, d: Optional[dict], source: str, client=None) -> dict:
    """The hint row of a decoded post: resolved to a contract, planned, tracked or not (and why)."""
    d = d or dc.decode(text, posted)
    h = {k: d.get(k) for k in ("symbol", "side", "instrument", "strike", "opt_type", "expiry", "expiry_src", "entry", "stop",
                               "targets", "open_ended", "size_hint", "decoded", "adds")}
    h["exchange"] = tr.exchange_of(h)
    day, _ = pl.plan_day(posted, h["exchange"])
    tr.resolve(h, None, day)                           # the stored contracts only: the tracker loads a missing dump
    h["plan"] = pl.build(h, config.tips, posted)
    if source == "import" and day < datetime.now(IST).date():
        h.update(status="expired", tracked=False, untracked_why="history · no quotes")
    else:
        h["status"] = "new"
        h["run"] = pl.new_run(h["plan"])
        h["tracked"] = bool(h.get("tradingsymbol")) and client is not None and bool(getattr(client, "has_token", False))
        if h.get("tradingsymbol") and not h["tracked"]:
            h["untracked_why"] = "log in"
    cur = conn.execute("INSERT INTO tips_hints (channel, post_id, created, posted_at, raw, source, status, events) VALUES (?,?,?,?,?,?,?,?)",
                       (channel, post_id, now_ist(), posted.isoformat(timespec="seconds"), text, source, h["status"], "[]"))
    h["id"] = cur.lastrowid
    h.update(events=[], channel=channel, post_id=post_id, posted_at=posted.isoformat(timespec="seconds"), raw=text, source=source)
    how = "decoded by code" if all(v == "code" for v in (d.get("decoded") or {}).values()) else "decoded by code + AI"
    exp = {"post": "expiry from the post", "listed": "expiry from the nearest listed", "computed": "expiry computed",
           "series": "the monthly expiry (series)"}.get(h.get("expiry_src"), "")
    pst = h.get("stop") or {}
    prov = (f" · no SL in the post · provisional stop {pl.fmt(pst['value'])} (−{pst.get('pct')} %) · plan complete"
            if pst.get("src") == "provisional" and h["plan"]["ok"] else "")
    bk.event(h, posted, "💡 hint posted · " + how + (f" · {exp}" if exp else "") + prov + ("" if h["plan"]["ok"] else f" · no plan: {h['plan']['why']}")
             + (f" · index {h['symbol']} read from the strike level (the post named none)" if (d.get("decoded") or {}).get("symbol") == "strike" else ""), "code")
    for a in h.get("adds") or []:                       # 📏 the call's own add: always on the card, never an order by default
        bk.event(h, posted, add_line(config, a), "code")
    for k in ("taken", "tracked", "open_ended"):
        h[k] = bool(h.get(k))
    bk.save(h, conn)
    return h


def size_line(config, h: dict) -> str:
    """"\n📏 BUY 4 lots (2 × 2) · lots ×2" when tips.kite_lot_multiplier > 1 (the size by code), else ""."""
    from backend.tips import sizing
    if sizing.mult_of(config) <= 1 or not h.get("lot_size"):
        return ""
    try:
        sz = sizing.size(config, h)
    except Exception:
        return ""
    if sz["src"] == "risk":
        return ""
    return f"\n📏 {h.get('side') or 'BUY'} {sizing.mult_text(sz)} · lots ×{sz['mult']}"


def add_line(config, a: dict) -> str:
    """"channel adds 2 lots near 660 (after activation) · the app never adds unless kite_follow_adds is on"."""
    from backend.tips import kite_orders as ko
    txt = ko.add_text(a)
    if getattr(config.tips, "kite_follow_adds", False):
        return f"{txt} · kite_follow_adds on: ONE add after the first fill, inside the lots cap and the risk per trade"
    return f"{txt} · {ko.ADD_OFF}"


def edited(config, channel: str, post_id: int, text: str, at: Optional[datetime] = None, client=None) -> Optional[dict]:
    """An edit: the text is replaced (the old one kept in the hint's events); a hint not yet taken or filled is decoded
    and planned again."""
    at = at or datetime.now(IST)
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tips_posts WHERE channel = ? AND post_id = ?", (channel.lower(), int(post_id))).fetchone()
        if not row:
            return None
        conn.execute("UPDATE tips_posts SET text = ?, edited_at = ? WHERE id = ?", (text, at.isoformat(timespec="seconds"), row["id"]))
        if row["hint_id"] and row["kind"] == "hint":
            h = bk.load(row["hint_id"], conn)
            before = {k: h.get(k) for k in ("symbol", "strike", "opt_type", "entry")}
            bk.event(h, at, f'the channel edited the post (was: "{(row["text"] or "")[:80]}")', "channel")
            if not h.get("taken") and (h.get("run") or {}).get("state", "waiting") == "waiting" and h["status"] in ("new", "waiting", "paused"):
                d = dc.decode(text, datetime.fromisoformat(h["posted_at"]))
                for k in ("symbol", "side", "instrument", "strike", "opt_type", "expiry", "expiry_src", "entry", "stop", "targets",
                          "open_ended", "size_hint", "decoded", "adds"):
                    h[k] = d.get(k)
                h["raw_edited"] = text
                tr.resolve(h, None, datetime.fromisoformat(h["posted_at"]).date())
                h["plan"] = pl.build(h, config.tips, datetime.fromisoformat(h["posted_at"]))
                h["run"] = pl.new_run(h["plan"])
                conn.execute("UPDATE tips_hints SET raw = ? WHERE id = ?", (text, h["id"]))
            bk.save(h, conn)
    if row["hint_id"] and row["kind"] == "hint":                  # ⚡ an armed Kite entry is checked against the new text
        try:
            from backend.tips import kite_auto as ka
            ka.on_edited(config, client if client is not None else ka.app_client(), row["hint_id"], before, at)
        except Exception:
            logger.exception("Tips Kite edit check failed")
    return {"post_id": post_id, "hint_id": row["hint_id"]}


def deleted(channel: Optional[str], post_ids: list, at: Optional[datetime] = None) -> int:
    """A deletion: deleted_at is set (the row and the hint stay)."""
    at = at or datetime.now(IST)
    n = 0
    with get_db() as conn:
        for pid in post_ids:
            q = "UPDATE tips_posts SET deleted_at = ? WHERE post_id = ? AND deleted_at IS NULL" + (" AND channel = ?" if channel else "")
            n += conn.execute(q, (at.isoformat(timespec="seconds"), int(pid), *([channel.lower()] if channel else []))).rowcount
            for r in conn.execute("SELECT hint_id FROM tips_posts WHERE post_id = ? AND hint_id IS NOT NULL AND kind = 'hint'", (int(pid),)):
                h = bk.load(r[0], conn)
                if h:
                    bk.event(h, at, "the channel deleted the post", "channel")
                    bk.save(h, conn)
    return n


# --- the HTML export --------------------------------------------------------------------------------------------------------

class ExportParser(HTMLParser):
    """Telegram Desktop's HTML export: one dict per `div.message.default` {id, date, text, media}."""

    def __init__(self):
        super().__init__()
        self.msgs, self.cur, self.in_text, self.depth, self.title, self._name = [], None, False, 0, None, False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "div" and "name" in cls and self.title is None and self.cur is None:
            self._name = True
        if tag == "div" and "message" in cls and "default" in cls:
            self.cur = {"id": a.get("id"), "date": None, "text": "", "media": []}
            self.msgs.append(self.cur)
            self.in_text, self.depth = False, 0
        if self.cur is None:
            return
        if tag == "div" and "date" in cls and "pull_right" in cls:
            self.cur["date"] = a.get("title")
        if tag == "div" and cls == ["text"]:
            self.in_text, self.depth = True, 1
        elif self.in_text and tag in ("div", "span", "a", "b", "i", "strong", "em", "code", "pre", "u", "s", "blockquote"):
            self.depth += 1
        if tag == "br" and self.in_text:
            self.cur["text"] += "\n"
        if tag == "a" and ("photo_wrap" in cls or "video_file_wrap" in cls or "animated_wrap" in cls):
            self.cur["media"].append("media")
        if tag == "div" and ("media_wrap" in cls or "media_file" in cls):
            self.cur["media"].append("media")

    def handle_endtag(self, tag):
        if self.in_text and tag in ("div", "span", "a", "b", "i", "strong", "em", "code", "pre", "u", "s", "blockquote"):
            self.depth -= 1
            if self.depth <= 0:
                self.in_text = False
        if self._name and tag == "div":
            self._name = False

    def handle_data(self, data):
        if self._name and self.title is None and data.strip():
            self.title = data.strip()
        if self.cur is not None and self.in_text:
            self.cur["text"] += data


def parse_export(html_text: str) -> tuple[Optional[str], list[dict]]:
    """(the channel's title, [{post_id, posted_at, text, has_media}]) from one export page."""
    p = ExportParser()
    p.feed(html_text)
    out = []
    for m in p.msgs:
        mid = re.sub(r"\D", "", m.get("id") or "")
        if not mid or not m.get("date"):
            continue
        try:
            dt = _export_date(m["date"])
        except ValueError:
            continue
        txt = re.sub(r"[ \t]+", " ", m["text"]).strip()
        out.append({"post_id": int(mid), "posted_at": dt, "text": txt, "has_media": bool(m["media"])})
    return p.title, out


def _export_date(s: str) -> datetime:
    """'7 August 2024, 09:43:01' or '07.08.2024 09:43:01 UTC+05:30' (the export's local time; IST assumed)."""
    s = s.strip()
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2}):(\d{2})(?: UTC([+-]\d{2}):(\d{2}))?", s)
    if m:
        dt = datetime(int(m[3]), int(m[2]), int(m[1]), int(m[4]), int(m[5]), int(m[6]))
        if m[7]:
            off = timezone(timedelta(hours=int(m[7]), minutes=int(m[8]) * (1 if int(m[7]) >= 0 else -1)))
            return dt.replace(tzinfo=off).astimezone(IST)
        return dt.replace(tzinfo=IST)
    return datetime.strptime(s, "%d %B %Y, %H:%M:%S").replace(tzinfo=IST)


def import_export(config, files: list[tuple[str, bytes]], channel: Optional[str] = None) -> dict:
    """Import the HTML export pages (any order): every post stored with its kind, hints decoded and planned (old ones
    expire untracked: no quotes for expired contracts), follow-ups linked within 90 minutes. Never pushes.
    {channel, files, posts, new, kinds, hints}."""
    posts, title = [], None
    for _, data in files:
        t, ps = parse_export(data.decode("utf-8", errors="replace"))
        title = title or t
        posts += ps
    ch = (channel or (channels_of(config) or ["channel"])[0]).lstrip("@").lower()
    posts.sort(key=lambda p: (p["posted_at"], p["post_id"]))
    kinds: dict = {}
    new = hints = 0
    with get_db() as conn:
        for p in posts:
            r = ingest(config, ch, p["post_id"], p["posted_at"], p["text"], p["has_media"], source="import", push=False, model=False, conn=conn)
            if r.get("new"):
                new += 1
                kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
                hints += int(r["kind"] == "hint")
    return {"channel": ch, "title": title, "files": len(files), "posts": len(posts), "new": new, "kinds": kinds, "hints": hints}


# --- the public preview page --------------------------------------------------------------------------------------------------

class PreviewParser(HTMLParser):
    """t.me/s/<channel>: `div.tgme_widget_message[data-post="channel/123"]`, its `div.tgme_widget_message_text`, a photo
    / video wrap, `time[datetime]`."""

    def __init__(self):
        super().__init__()
        self.msgs, self.cur, self.in_text, self.depth = [], None, False, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "div" and "tgme_widget_message" in cls and a.get("data-post"):
            self.cur = {"post": a["data-post"], "text": "", "media": False, "date": None}
            self.msgs.append(self.cur)
            self.in_text = False
            return
        if self.cur is None:
            return
        if tag == "div" and "tgme_widget_message_text" in cls and not self.in_text:
            self.in_text, self.depth = True, 1
            return
        if self.in_text:
            if tag == "br":
                self.cur["text"] += "\n"
            elif tag not in ("img", "br", "i", "wbr"):
                self.depth += 1
        if any(c.startswith(("tgme_widget_message_photo", "tgme_widget_message_video", "tgme_widget_message_document")) for c in cls):
            self.cur["media"] = True
        if tag == "time" and a.get("datetime") and not self.cur["date"]:
            self.cur["date"] = a["datetime"]

    def handle_endtag(self, tag):
        if self.in_text and tag not in ("img", "br", "i", "wbr"):
            self.depth -= 1
            if self.depth <= 0:
                self.in_text = False

    def handle_data(self, data):
        if self.cur is not None and self.in_text:
            self.cur["text"] += data


def parse_preview(html_text: str) -> list[dict]:
    p = PreviewParser()
    p.feed(html_text)
    out = []
    for m in p.msgs:
        ch, _, pid = m["post"].partition("/")
        if not pid.isdigit() or not m["date"]:
            continue
        try:
            dt = datetime.fromisoformat(m["date"].replace("Z", "+00:00")).astimezone(IST)
        except ValueError:
            continue
        out.append({"channel": ch.lower(), "post_id": int(pid), "posted_at": dt, "text": m["text"].strip(), "has_media": m["media"]})
    return out


def preview_once(config, fetch: Optional[Callable[[str], str]] = None, client=None) -> dict:
    """Read each channel's public page once; new posts are ingested (pushed when fresh). {posts, new}."""
    fetch = fetch or _http_get
    seen = new = 0
    last = None
    for ch in channels_of(config):
        if not is_username(ch):                        # a private channel has no public page
            continue
        try:
            html_text = fetch(PREVIEW_URL.format(channel=ch))
        except Exception as exc:
            logger.info("Tips preview %s failed: %s", ch, str(exc)[:120])
            continue
        for p in parse_preview(html_text):
            seen += 1
            r = ingest(config, p["channel"] or ch, p["post_id"], p["posted_at"], p["text"], p["has_media"], source="preview", client=client)
            new += int(bool(r.get("new")))
            last = max(last or p["posted_at"], p["posted_at"])
    if last:
        set_status(last_post_at=last.isoformat(timespec="seconds"))
    return {"posts": seen, "new": new}


def _http_get(url: str) -> str:
    import httpx
    r = httpx.get(url, timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (compatible; reader)"})
    r.raise_for_status()
    return r.text


# --- the Telethon reader ------------------------------------------------------------------------------------------------------

def _telethon_client(session: str, api_id: int, api_hash: str):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(session), api_id, api_hash)


class Reader:
    """The reader thread: Telethon in its own asyncio loop; on any failure "down" (+ the preview fallback) and a retry
    every RETRY_READER_S. `client_factory()` and `fetch(url)` are injectable (tests run with fakes, no network)."""

    def __init__(self, state, client_factory: Optional[Callable] = None, fetch: Optional[Callable[[str], str]] = None):
        self.state = state
        self.client_factory = client_factory
        self.fetch = fetch
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tasks: set = set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        cfg = self.state.config
        channels_of(cfg)
        if not cfg.tips.enabled or not session_string():
            set_status(state="logged_out" if not session_string() else "down",
                       reason=None if not session_string() else "Tips switched off", via=None)
            return False
        if self.running:
            if not self._stop.is_set():
                return True
            self._thread.join(timeout=15)                  # a stop is in progress: let the old thread finish first
            if self._thread.is_alive():
                logger.warning("Tips reader: the old thread has not stopped yet; starting a new one anyway")
        try:                                               # 📷 screenshots older than 2 days go at every start
            from backend.tips import vision as vs
            vs.sweep()
        except Exception:
            logger.exception("Tips media sweep at the reader start failed")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tips-reader", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        c, loop = self._client, self._loop
        if c is not None and loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(c.disconnect(), loop)
            except Exception:
                pass

    def restart(self) -> bool:
        """Stop (the channels changed), wait for the thread, start again. The page says "restarting" meanwhile."""
        set_status(state="down", reason="restarting (channels changed)", via=None)
        self.stop()
        if self._thread is not None:
            self._thread.join(timeout=15)
        return self.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:                          # never out of the thread
                if self._stop.is_set():                       # a stop / restart asked for: the disconnect is expected
                    break
                reason = _reason(exc)
                logger.warning("Tips reader down: %s", reason)
                set_status(state="down", reason=reason, via=None)
            if self._stop.is_set():
                break
            self.fallback(RETRY_READER_S)
        if status()["state"] in ("live", "preview"):
            set_status(state="down", reason="stopped", via=None)

    def fallback(self, seconds: float) -> None:
        """The preview page every 30 s for `seconds` (or a plain wait when preview_fallback is off)."""
        import time as _t
        end = _t.monotonic() + seconds
        while not self._stop.is_set() and _t.monotonic() < end:
            if self.state.config.tips.preview_fallback and self.state.config.tips.enabled:
                try:
                    r = preview_once(self.state.config, self.fetch, self.state.client)
                    st = status()
                    set_status(state="preview", via="preview", reason=st.get("reason") or "reader down")
                    logger.debug("Tips preview: %s", r)
                except Exception as exc:
                    set_status(state="down", reason=_reason(exc), via=None)
            self._stop.wait(PREVIEW_EVERY_S)

    def run_once(self) -> None:
        """Connect, check the session, catch up, then serve updates until disconnected (raises on failure)."""
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._main())
        finally:
            self._client = None
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None

    async def _main(self) -> None:
        cfg = self.state.config
        api_id, api_hash = api_creds()
        sess = session_string()
        if self.client_factory is None and (not api_id or not api_hash):
            raise ReaderError("TIPS_TG_API_ID / TIPS_TG_API_HASH not set in .env")
        if not sess:
            set_status(state="logged_out", reason="no session · log in", via=None)
            self._stop.set()
            return
        client = self.client_factory() if self.client_factory else _telethon_client(sess, api_id, api_hash)
        self._client = client
        await client.connect()
        if not await client.is_user_authorized():
            raise ReaderError("the session is not authorised · log in again")
        me = await client.get_me()
        acct = mask(getattr(me, "phone", None) and ("+" + str(me.phone).lstrip("+")) or getattr(me, "username", None))
        chans = channels_of(cfg)
        entities = []
        for ch in chans:
            try:
                entities.append((ch, await find_channel(client, ch)))
            except Exception as exc:
                logger.warning("Tips channel %s not readable: %s", ch, _reason(exc))
        if not entities:
            raise ReaderError("no readable channel (check tips.channels)")
        names = {getattr(e, "id", None): ch for ch, e in entities}
        for ch, ent in entities:                                       # catch up on what came while away
            msgs = await client.get_messages(ent, limit=CATCH_UP)
            for m in reversed(list(msgs or [])):
                self._on_message(ch, m)
                await self._capture(ch, m)                         # 📷 after the post is stored; old posts are skipped
        from telethon import events
        ents = [e for _, e in entities]

        async def on_new(ev):
            ch = names.get(getattr(ev, "chat_id", None)) or _chat_name(ev, chans)
            r = self._on_message(ch, ev.message)
            self._relay_soon(ch, ev.message, r)                     # ↗ after the post is stored; never blocks it
            if getattr(ev.message, "media", None) is not None and getattr(cfg.tips, "vision", False):
                t = asyncio.get_running_loop().create_task(self._capture(ch, ev.message))   # never blocks the next post
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)

        async def on_edit(ev):
            ch = names.get(getattr(ev, "chat_id", None)) or _chat_name(ev, chans)
            edited(cfg, ch, ev.message.id, getattr(ev.message, "message", "") or "")

        async def on_del(ev):
            deleted(names.get(getattr(ev, "chat_id", None)), list(getattr(ev, "deleted_ids", []) or []))

        client.add_event_handler(on_new, events.NewMessage(chats=ents))
        client.add_event_handler(on_edit, events.MessageEdited(chats=ents))
        client.add_event_handler(on_del, events.MessageDeleted(chats=ents))
        set_status(state="live", reason=None, account_masked=acct, via="telethon", channels=[c for c, _ in entities])
        logger.info("Tips reader live on %d channel(s)", len(entities))
        if self._stop.is_set():
            await client.disconnect()
            return
        await client.run_until_disconnected()
        if not self._stop.is_set():
            raise ReaderError("disconnected")

    async def _capture(self, channel: str, m) -> None:
        """📷 tips.vision: the post's image downloaded and read (backend/tips/vision.py). Never raises."""
        if getattr(m, "media", None) is None or not getattr(self.state.config.tips, "vision", False):
            return
        try:
            from backend.tips import vision as vs
            await vs.capture(self.state.config, self._client, channel, m)
        except Exception:
            logger.exception("Tips screenshot %s/%s failed", channel, getattr(m, "id", "?"))

    def _relay_soon(self, channel: str, m, r: Optional[dict]):
        """↗ tips.relay: a NEW live post (not an edit, not the catch-up, not the rehearsal channel) is forwarded to each
        relay_to entry in a task on this loop, so the next post and the order handling never wait for it."""
        tips = self.state.config.tips
        if not (r and r.get("new")) or not getattr(tips, "relay", False) or channel == REHEARSAL_CHANNEL:
            return None
        try:
            entries = relay_entries(tips)
        except ValueError:
            logger.warning("Tips relay: tips.relay_to has a bad entry; nothing forwarded")
            return None
        if not entries or self._client is None:
            return None
        t = asyncio.get_running_loop().create_task(self._relay(self._client, m, entries))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def _relay(self, client, m, entries: list) -> int:
        """Forward the original message object as-is (text + media, Telegram's own "Forwarded from") to each entry. A
        failure is one log line and, once per entry per day, one Telegram line to the owner; the others still go.
        No retry. Returns the number sent. Never raises."""
        sent = 0
        for entry in entries:
            try:
                peer = await _relay_peer(client, entry)
                await client.forward_messages(peer, m)
                sent += 1
            except Exception as exc:
                why = relay_reason(exc)
                logger.warning("Tips relay to %s failed: %s", entry, why)
                await relay_failed(self.state.config, entry, why)
        if sent:
            try:
                relay_count(sent)
            except Exception:
                logger.exception("Tips relay count failed")
        return sent

    def _on_message(self, channel: str, m) -> Optional[dict]:
        try:
            when = getattr(m, "date", None) or datetime.now(timezone.utc)
            text = getattr(m, "message", None) or ""
            rt = getattr(getattr(m, "reply_to", None), "reply_to_msg_id", None)      # the quoted post (a reply)
            r = ingest(self.state.config, channel, int(m.id), _iso(when), text, getattr(m, "media", None) is not None,
                       source="live", client=self.state.client, reply_to=int(rt) if rt else None)
            lag = int((datetime.now(timezone.utc) - (when if when.tzinfo else when.replace(tzinfo=timezone.utc))).total_seconds() * 1000)
            if r.get("new"):
                set_status(last_post_at=_iso(when), lag_ms=max(0, lag))
            return r
        except Exception:
            logger.exception("Tips post %s/%s failed", channel, getattr(m, "id", "?"))
            return None


# --- ↗ the relay: each new post forwarded as-is from the owner's own account ------------------------------------------------

REHEARSAL_CHANNEL = "rehearsal"         # the dry-run rehearsal hint's channel (kite_orders.REHEARSAL_CHANNEL): never relayed
_relay_peers: dict = {}                 # {entry: the resolved peer}, once per process
_relay_warned: set = set()              # {(entry, day)}: the owner is told once per entry per day


def relay_entries(tips) -> list[str]:
    from backend.config import split_relay
    return split_relay(getattr(tips, "relay_to", "") or "")


async def _relay_peer(client, entry: str):
    """An entry's peer, resolved once per process (client.get_entity; a phone resolves only when it is in the
    account's contacts; a numeric id the session has not seen yet is looked up after loading the dialogs once)."""
    if entry in _relay_peers:
        return _relay_peers[entry]
    if entry.lower().startswith("group:"):
        peer = await find_channel(client, entry.split(":", 1)[1].strip())       # a group / chat by its title among the account's chats
        _relay_peers[entry] = peer
        return peer
    if entry.lstrip("-").isdigit():
        try:
            peer = await client.get_entity(int(entry))
        except ValueError:
            await client.get_dialogs()
            peer = await client.get_entity(int(entry))
    else:
        peer = await client.get_entity(entry)
    _relay_peers[entry] = peer
    return peer


def relay_reason(exc: Exception) -> str:
    """A short reason for the log and the owner's line (no secrets are in it)."""
    name = type(exc).__name__
    if name == "ChatForwardsRestrictedError":
        return "the channel restricts forwarding"
    if name == "FloodWaitError":
        return f"flood wait {int(getattr(exc, 'seconds', 0) or 0)} s · skipped"
    if isinstance(exc, LookupError):
        return "no group with that name among your chats (the name as Telegram shows it)"
    if name in ("UsernameNotOccupiedError", "UsernameInvalidError", "PeerIdInvalidError") or isinstance(exc, ValueError):
        return "unknown user (a phone number must be in your contacts)"
    return _reason(exc)


async def relay_failed(config, entry: str, why: str) -> None:
    """One Telegram line to the owner (kind tips) per entry per day; sent off the reader's loop."""
    day = datetime.now(IST).date().isoformat()
    if (entry, day) in _relay_warned:
        return
    _relay_warned.add((entry, day))
    import html
    line = f"↗ relay to {html.escape(entry)} failed: {html.escape(why)}"
    try:
        await asyncio.get_running_loop().run_in_executor(None, bk.push, config, f"tips|relay|fail|{entry}|{day}", line)
    except Exception:
        logger.exception("Tips relay failure line not sent")


def relay_count(n: int) -> int:
    """relayed_today in the reader status (a new day starts at 0). Returns today's count."""
    day = datetime.now(IST).date().isoformat()
    with get_db() as conn:
        st = json.loads(get_setting(conn, "tips.reader") or "{}")
        if st.get("relay_day") != day:
            st["relay_day"], st["relayed_today"] = day, 0
        st["relayed_today"] = int(st.get("relayed_today") or 0) + int(n)
        set_setting(conn, "tips.reader", json.dumps(st))
    return st["relayed_today"]


def _chat_name(ev, chans: list) -> str:
    chat = getattr(ev, "chat", None)
    u = getattr(chat, "username", None)
    return (u or (chans[0] if chans else "channel")).lower()


class ReaderError(Exception):
    pass


def _reason(exc: Exception) -> str:
    """A short reason for the page and the health row (the exception's type and first line; no secrets are in it)."""
    s = str(exc).split("\n")[0][:160]
    return f"{type(exc).__name__}: {s}" if not isinstance(exc, ReaderError) else s


READER: Optional[Reader] = None


OLD_CHANNEL_VALUES = ("aakankshatrading",)      # the old default, moved to the new one once


def migrate_channels(config) -> bool:
    """A saved channels value that is the old default, or the new title broken into words (the dialog once split on
    spaces), becomes the current default. Returns True when it changed."""
    from backend.config import TipsConfig
    default = TipsConfig().channels
    raw = str(config.tips.channels or "")
    pieces = [p.lower() for p in split_channels(raw)]
    words = sorted(default.lower().split())
    if raw == default:
        return False
    if not pieces or pieces == list(OLD_CHANNEL_VALUES) or sorted(w for p in pieces for w in p.replace(",", " ").split()) == words:
        config.tips.channels = default
        return True
    return False


def migrate_time_stop(config) -> bool:
    """A saved intraday_time_stop of 15:20 (the old default) becomes 15:14 once; any other saved value is the owner's."""
    if str(config.tips.intraday_time_stop) == "15:20":
        config.tips.intraday_time_stop = "15:14"
        return True
    return False


def ensure(state, client_factory=None, fetch=None) -> dict:
    """Start the reader when tips.enabled and a session exist, stop it otherwise. The reader status."""
    global READER
    try:
        if migrate_time_stop(state.config):
            from backend.config import save_config
            save_config(state.config)
            logger.info("Tips time stop moved from 15:20 to the default 15:14")
        if migrate_channels(state.config):
            from backend.config import save_config
            save_config(state.config)
            logger.info("Tips channels moved to the default %r", state.config.tips.channels)
    except Exception:
        logger.exception("Tips channels migration failed")
    if READER is None:
        READER = Reader(state, client_factory, fetch)
    if state.config.tips.enabled and session_string():
        READER.start()
    else:
        if READER.running:
            READER.stop()
        set_status(state="logged_out" if not session_string() else "down",
                   reason=None if not session_string() else "Tips switched off", via=None)
    return status()


# --- the login flow (POST /api/tips/login {phone} -> the code -> POST /api/tips/login/code) ------------------------------------

class Login:
    """One pending login: its own asyncio loop thread holds the Telethon client between the code request and the code.
    The session string goes to .env (never to a log or a response)."""

    def __init__(self, client_factory: Optional[Callable] = None):
        self.client_factory = client_factory
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.client = None
        self.phone: Optional[str] = None
        self.hash: Optional[str] = None

    def _ensure_loop(self) -> None:
        if self.loop and self.thread and self.thread.is_alive():
            return
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="tips-login", daemon=True)
        self.thread.start()

    def _run(self, coro, timeout: float = 60):
        self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self, phone: str) -> dict:
        api_id, api_hash = api_creds()
        if self.client_factory is None and (not api_id or not api_hash):
            raise ValueError("TIPS_TG_API_ID / TIPS_TG_API_HASH not set in .env (my.telegram.org → API development tools)")
        phone = re.sub(r"[^\d+]", "", phone or "")
        if len(phone.lstrip("+")) < 8:
            raise ValueError("a phone number with the country code, e.g. +91…")

        async def go():
            if self.client is not None:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
            if self.client_factory:
                c = self.client_factory()
            else:
                from telethon import TelegramClient
                from telethon.sessions import StringSession
                c = TelegramClient(StringSession(), api_id, api_hash)
            await c.connect()
            sent = await c.send_code_request(phone)
            return c, getattr(sent, "phone_code_hash", None)

        self.client, self.hash = self._run(go())
        self.phone = phone
        return {"sent": True, "phone_masked": mask(phone)}

    def code(self, code: str, password: Optional[str] = None) -> dict:
        if self.client is None or not self.phone:
            raise LookupError("no login in progress: send the phone first")
        code = re.sub(r"\D", "", code or "")

        async def go():
            c = self.client
            try:
                await c.sign_in(phone=self.phone, code=code, phone_code_hash=self.hash)
            except Exception as exc:
                if type(exc).__name__ != "SessionPasswordNeededError":
                    raise
                if not password:
                    return {"need_password": True}
                await c.sign_in(password=password)
            me = await c.get_me()
            s = c.session.save()
            await c.disconnect()
            return {"session": s, "account": getattr(me, "phone", None) or getattr(me, "username", None)}

        try:
            r = self._run(go())
        except Exception as exc:
            raise ValueError(_reason(exc))
        if r.get("need_password"):
            return {"ok": False, "need_password": True}
        where = save_session(r["session"])
        acct = mask(("+" + str(r["account"]).lstrip("+")) if r.get("account") and str(r["account"]).lstrip("+").isdigit() else r.get("account"))
        set_status(state="down", reason="logged in · starting", account_masked=acct)
        self.client = None
        return {"ok": True, "saved_to": where, "account_masked": acct}


LOGIN = Login()
