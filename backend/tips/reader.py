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
            "via": st.get("via"), "since": st.get("since"), "channels": channels_of(None)}


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
        if out.get("push"):
            bk.push(config, out["push"][0], out["push"][1])
        if out.get("new") and source in ("live", "preview"):
            kite_after(config, client, out, push)
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
    if kind is None:
        d = dc.decode(text, posted)
        if dc.looks_like_hint(d, text):
            kind = "hint"
        else:
            res = flt.model_classify(config, text, hints) if model and source != "import" else None
            if res:
                kind, hint_id, by, mdl, ms = res["kind"], res["hint_id"], "ai", res["model"], res["ms"]
                if kind == "hint":
                    fill = flt.model_fill(config)
                    d = dc.decode(text, posted, fill)
                    if not ((d.get("strike") is not None and d.get("opt_type")) or d.get("symbol")):
                        kind = "chatter"
            else:
                kind, hint_id = flt.code_classify(text, hints)
    cur = conn.execute("INSERT INTO tips_posts (channel, post_id, posted_at, text, has_media, kind, hint_id, classified_by, model, ms, source, created, reply_to) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (channel, int(post_id), posted.isoformat(timespec="seconds"), text, int(bool(has_media)),
                                                              kind, hint_id, by, mdl, ms, source, now_ist(), reply_to))
    out = {"post_id": post_id, "kind": kind, "hint_id": hint_id, "new": True, "row": cur.lastrowid, "at": posted.isoformat(timespec="seconds")}
    if source == "import" and has_media:                 # 📷 the export never downloads: the reason is stored
        from backend.tips import vision as vs
        vs.import_note(config, conn, channel, int(post_id), posted, has_media)
    if kind == "hint":
        h = new_hint(config, conn, channel, int(post_id), posted, text, d, source, client)
        conn.execute("UPDATE tips_posts SET hint_id = ? WHERE id = ?", (h["id"], cur.lastrowid))
        out["hint_id"] = h["id"]
        fresh = (datetime.now(IST) - posted).total_seconds() <= PUSH_MAX_AGE_S
        if push and source in ("live", "preview") and config.tips.push_hints and fresh:
            from backend.alerts import messages as msg
            out["push"] = (f"tips|hint|{h['id']}", msg.tips_hint(h))
    elif kind == "follow_up" and hint_id:
        h = bk.load(hint_id, conn)
        if h:
            act = pl.followup_action(text)
            now = posted                                     # the post's own time (Telegram's clock, ~1 s behind)
            done = complete_plan(config, h, text, posted, act)   # "SL 410 / TGT 520/550/600+" posted after the call
            if source == "import":
                bk.event(h, posted, f'channel: "{text[:80]}"', "channel")
                line = "follow-up"
            elif done:
                bk.event(h, posted, f'channel: "{text[:80]}" · {done}', "channel")
                line = done
            else:
                line = bk.channel_action(config, h, act, text, now, conn=conn)
            bk.save(h, conn)
            out["line"] = line
            out["act"], out["text"], out["at"] = act, text, posted.isoformat(timespec="seconds")
            if push and source in ("live", "preview") and config.tips.push_followups:
                from backend.alerts import messages as msg
                out["push"] = (f"tips|fu|{channel}|{post_id}", msg.tips_followup(h, text, line, now, act))
    return out


def kite_after(config, client, out: dict, push: bool = True) -> None:
    """⚡ Tips → Kite (PR C), after the post is stored: a follow-up's Kite consequence (any mode: an add-on is never an
    order, avoid / SL hit before the fill cancel an unfilled entry, "active" sends an armed one; auto: the channel's exit
    / trail / SL / part booking on its own position) and, in auto, the hint armed when it is (now) placeable. Never
    raises: a failure is logged, the post is kept."""
    try:
        from backend.tips import kite_auto as ka
        from backend.tips import kite_orders as ko
        if ko.mode(config) == "off" and out.get("kind") != "follow_up":
            return
        now = datetime.fromisoformat(out["at"]) if out.get("at") else datetime.now(IST)   # the post's own time (~1 s behind)
        if (datetime.now(IST) - now).total_seconds() > PUSH_MAX_AGE_S:
            return                                         # a caught-up old post never moves a Kite order
        if out.get("kind") == "follow_up" and out.get("hint_id") and out.get("act"):
            ka.on_followup(config, client, out["hint_id"], out["act"], out.get("text") or "", now, notify=push)
        if out.get("kind") in ("hint", "follow_up") and out.get("hint_id"):
            ka.on_new(config, client, out["hint_id"], now, notify=push)
    except Exception:
        logger.exception("Tips Kite after the post failed")


PLAN_SKIP_ACTIONS = ("targets", "sl_hit", "exit", "partial", "trail")     # "tgt 1 done" states a result, not a plan


def complete_plan(config, h: dict, text: str, posted: datetime, act: Optional[dict] = None) -> Optional[str]:
    """A follow-up that carries the stop and / or targets the call itself lacked (this channel posts "BUY SENSEX 82000
    CE NEAR 460" first and "SL 410 / TGT 520/550/600+" as a reply): fill them in, rebuild the plan and restart the
    record run when nothing has filled yet. Returns the line, or None when the follow-up adds nothing. A post that
    reports a result ("tgt 1 done", "SL hit") never sets a level."""
    if (act or {}).get("action") in PLAN_SKIP_ACTIONS:
        return None
    d = dc.decode(text, posted)
    st, tg = d.get("stop") or {}, [t for t in (d.get("targets") or []) if t is not None]
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
                               "targets", "open_ended", "size_hint", "decoded")}
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
    exp = {"post": "expiry from the post", "listed": "expiry from the nearest listed", "computed": "expiry computed"}.get(h.get("expiry_src"), "")
    bk.event(h, posted, "💡 hint posted · " + how + (f" · {exp}" if exp else "") + ("" if h["plan"]["ok"] else f" · no plan: {h['plan']['why']}")
             + (f" · index {h['symbol']} read from the strike level (the post named none)" if (d.get("decoded") or {}).get("symbol") == "strike" else ""), "code")
    for k in ("taken", "tracked", "open_ended"):
        h[k] = bool(h.get(k))
    bk.save(h, conn)
    return h


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
                          "open_ended", "size_hint", "decoded"):
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
            self._on_message(ch, ev.message)
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

    def _on_message(self, channel: str, m) -> None:
        try:
            when = getattr(m, "date", None) or datetime.now(timezone.utc)
            text = getattr(m, "message", None) or ""
            rt = getattr(getattr(m, "reply_to", None), "reply_to_msg_id", None)      # the quoted post (a reply)
            r = ingest(self.state.config, channel, int(m.id), _iso(when), text, getattr(m, "media", None) is not None,
                       source="live", client=self.state.client, reply_to=int(rt) if rt else None)
            lag = int((datetime.now(timezone.utc) - (when if when.tzinfo else when.replace(tzinfo=timezone.utc))).total_seconds() * 1000)
            if r.get("new"):
                set_status(last_post_at=_iso(when), lag_ms=max(0, lag))
        except Exception:
            logger.exception("Tips post %s/%s failed", channel, getattr(m, "id", "?"))


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


def ensure(state, client_factory=None, fetch=None) -> dict:
    """Start the reader when tips.enabled and a session exist, stop it otherwise. The reader status."""
    global READER
    try:
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
