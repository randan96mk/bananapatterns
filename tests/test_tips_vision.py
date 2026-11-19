"""📷 Tips screenshots read with RapidOCR: facts() by rules on hand-written OCR line sets (the four broker screens of
the channel), read_image with a fake engine (timeout, ImportError), the link to a hint, the strike mismatch (card,
Telegram, Home, auto held only in "check"), "Use screenshot" / "Keep", the index from the screenshot, the 2-day rule
(never read an old post; files swept after 48 h, the OCR text stays) and vision off. One real-engine test when
rapidocr_onnxruntime is installed."""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.config import IST
from backend.database.models import get_db
from backend.tips import book as bk
from backend.tips import kite_auto as ka
from backend.tips import kite_orders as ko
from backend.tips import reader as rd
from backend.tips import track as tr
from backend.tips import vision as vs
from tests.test_tips_api import api  # noqa: F401  (the app fixture)
from tests.test_tips_kite_auto import CALL2, CH, S82200, S82300, auto_setup, post, pushed, t19  # noqa: F401
from tests.test_tips_reader import FakeTele, St, msg, session  # noqa: F401


def L(*texts, conf=0.95):
    return [{"text": t, "conf": conf, "box": None} for t in texts]


def B(text, x, y, h=20, w=None, conf=0.95):
    w = w or 12 * len(text)
    return {"text": text, "conf": conf, "box": [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]}


ZERODHA = L("SENSEX 19-Nov-26 82000 CE · BSE FO", "₹535.10", "+75.25 (+16.36%)", "Bid 534.90", "Ask 535.40")
NIFTY = [B("NIFTY 23-Nov-26 25200 CE", 10, 10), B("NSE FO", 10, 34, h=14), B("459.85", 10, 60, h=40),
         B("+12.35 (2.76%)", 10, 104, h=16), B("460.00", 200, 300, h=14)]
SENSEX_TH = L("SENSEX 19th NOV 82300 CE BFO", "337.35", "-4.20 (-1.23%)")
WORDS = [B("82800", 145, 35, h=28), B("SENSEX", 22, 36, h=27), B("19", 90, 36, h=27, w=20), B("NOV", 112, 36, h=27, w=30),
         B("PE", 251, 34, h=29), B("535. 10", 21, 114, h=31)]          # the engine reads words apart, out of order


# --- facts --------------------------------------------------------------------------------------------------------------------

def test_facts_on_the_four_screens():
    f = vs.facts(ZERODHA, t19("12:48"))
    assert (f["symbol"], f["strike"], f["opt_type"], f["expiry"], f["price"]) == ("SENSEX", 82000.0, "CE", "2026-11-19", 535.10)
    f = vs.facts(NIFTY, t19("12:48"))
    assert (f["symbol"], f["strike"], f["opt_type"], f["expiry"], f["price"]) == ("NIFTY", 25200.0, "CE", "2026-11-23", 459.85)
    f = vs.facts(SENSEX_TH, t19("12:48"))
    assert (f["symbol"], f["strike"], f["opt_type"], f["expiry"], f["price"]) == ("SENSEX", 82300.0, "CE", "2026-11-19", 337.35)
    f = vs.facts(WORDS, t19("12:48"))
    assert (f["symbol"], f["strike"], f["opt_type"], f["expiry"], f["price"]) == ("SENSEX", 82800.0, "PE", "2026-11-19", 535.10)
    assert f["conf"] == 0.95 and f["raw"].startswith("SENSEX 19 NOV 82800 PE")


def test_the_percent_line_is_never_the_price_and_no_strike_is_nothing():
    f = vs.facts(L("NIFTY 25200 CE", "+12.35 (2.36%)", "459.85"), t19("12:48"))
    assert f["price"] == 459.85
    f = vs.facts(L("NIFTY 25200 CE", "-3.10 (-0.67%)"), t19("12:48"))
    assert f["price"] is None and f["strike"] == 25200.0
    assert vs.facts(L("P&L", "+₹4,250.00", "Positions (2)"), t19("12:48")) == {}
    assert vs.facts([], t19("12:48")) == {}
    lo = vs.facts([{"text": "SENSEX 82000 CE", "conf": 0.61, "box": None}, {"text": "535.10", "conf": 0.9, "box": None}], t19("12:48"))
    assert lo["conf"] == 0.61                                                    # the lowest of the lines used


# --- read_image ---------------------------------------------------------------------------------------------------------------

class Engine:
    def __init__(self, lines=(), sleep=0.0):
        self.lines, self.sleep, self.calls = list(lines), sleep, 0

    def __call__(self, path):
        self.calls += 1
        if self.sleep:
            time.sleep(self.sleep)
        return [[x["box"] or [[0, i * 30], [100, i * 30], [100, i * 30 + 20], [0, i * 30 + 20]], x["text"], str(x["conf"])]
                for i, x in enumerate(self.lines)], [0.1]


@pytest.fixture
def engine(monkeypatch):
    e = Engine(SENSEX_TH)
    vs.drop_engine()
    monkeypatch.setattr(vs, "ENGINE_FACTORY", lambda: e)
    yield e
    vs.drop_engine()


def test_read_image_with_a_fake_engine(engine, tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(b"x")
    r = vs.read_image(p)
    assert r["engine"] == "rapidocr" and [x["text"] for x in r["lines"]] == ["SENSEX 19th NOV 82300 CE BFO", "337.35", "-4.20 (-1.23%)"]
    assert r["lines"][0]["conf"] == 0.95 and isinstance(r["ms"], int)
    vs.read_image(p)
    assert engine.calls == 2 and vs._engine is engine                           # one instance, loaded once


def test_read_image_timeout_and_import_error(monkeypatch, tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(b"x")
    slow = Engine(SENSEX_TH, sleep=1.0)
    vs.drop_engine()
    monkeypatch.setattr(vs, "ENGINE_FACTORY", lambda: slow)
    vs._get_engine()
    r = vs.read_image(p, budget=0.1)
    assert r["error"] == "timed out after 0.1 s" and vs._engine is None          # dropped, rebuilt next time

    def missing():
        raise ImportError("No module named 'rapidocr_onnxruntime'")
    monkeypatch.setattr(vs, "ENGINE_FACTORY", missing)
    r = vs.read_image(p)
    assert r["error"].startswith("OCR engine not installed")
    vs.drop_engine()


# --- the world: a hint, a screenshot ------------------------------------------------------------------------------------------

def shot_post(c, s, pid, hhmm, lines, engine, text="", reply_to=None, now=None):
    """An image post through the real ingest, its file in data/tips_media, then read (now = the test clock)."""
    engine.lines = list(lines)
    rd.ingest(c, CH, pid, t19(hhmm), text, True, source="live", push=True, model=False, client=s, reply_to=reply_to)
    folder = vs.MEDIA_DIR / "x"
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / f"{pid}.png"
    f.write_bytes(b"png")
    with get_db() as conn:
        conn.execute("UPDATE tips_posts SET media_path = ? WHERE channel = ? AND post_id = ?", (str(f), CH, pid))
    return vs.process(c, CH, pid, now=now or t19(hhmm))


@pytest.fixture
def appcl(monkeypatch):
    monkeypatch.setattr(ka, "app_client", lambda: ko.sim())


def test_mismatch_in_check_mode_cancels_holds_and_use_screenshot_rearms(pushed, engine, appcl):  # noqa: F811
    c, w, s = auto_setup(vision=True, vision_mode="check")
    w.prices.update({S82300: 309.0, S82200: 309.0})
    hid = post(c, w, s, 1, "13:52", CALL2)["hint_id"]
    assert ko.kstate(bk.load(hid))["state"] == "armed"
    r = shot_post(c, s, 2, "13:52", L("SENSEX 19th NOV 82200 CE BFO", "₹535.10"), engine)
    assert r == {"hint_id": hid, "how": "minute", "did": "mismatch"}
    h = bk.load(hid)
    mm = h["shot"]["mismatch"]
    assert mm["text"]["strike"] == 82300.0 and mm["shot"]["strike"] == 82200.0 and mm["resolved"] is None
    line = "strike mismatch: text 82300 CE, screenshot 82200 CE · the channel says the screenshot wins"
    assert f"📷 Tips #{hid} · {line} · pick the strike on the Tips page" in pushed               # one Telegram line (kind tips)
    assert ko.kstate(h)["state"] == "none" and any("strike mismatch: pick the strike" in x["text"] for x in ko.kstate(h)["log"])
    assert ("warn", f"📷 Tips #{hid} · {line} → pick the strike on the Tips page") in vs.attention_items(t19("13:53"))
    v = tr.hint_view(h, t19("13:53"), c)["shot"]
    assert v["state"] == "mismatch" and v["use_label"] == "Use screenshot 82200 CE" and v["keep_label"] == "Keep 82300 CE"
    assert v["text"] == line and v["price"] == 535.10
    assert ka.auto_arm(c, s, bk.load(hid), t19("13:54")) is None                                  # held while unresolved
    assert "strike mismatch: pick the strike" in (ko.kstate(bk.load(hid)).get("auto") or {}).get("why", "")
    h = bk.set_strike(c, hid, 82200, "CE", None, now=t19("13:55"))
    assert h["strike"] == 82200.0 and h["tradingsymbol"] == "SENSEX26NOV82200CE" and h["decoded"]["strike"] == "owner"
    assert h["shot"]["mismatch"]["resolved"]["choice"] == "shot"
    assert any("you chose the screenshot's strike 82200 CE (text said 82300 CE)" in e["text"] for e in h["events"])
    assert ko.kstate(h)["state"] == "armed"                                                       # auto may arm again
    assert vs.attention_items(t19("13:56")) == []
    with pytest.raises(bk.TipsError):
        bk.keep_strike(c, hid, now=t19("13:57"))                                                  # nothing open any more


def test_report_mode_shows_and_pushes_but_never_holds(pushed, engine, appcl):  # noqa: F811
    c, w, s = auto_setup(vision=True, vision_mode="report")
    w.prices[S82300] = 309.0
    hid = post(c, w, s, 1, "13:52", CALL2)["hint_id"]
    shot_post(c, s, 2, "13:53", L("SENSEX 19 NOV 82200 CE", "535.10"), engine, reply_to=1)
    h = bk.load(hid)
    assert h["shot"]["mismatch"]["mode"] == "report" and h["shot"]["how"] == "reply"
    assert ko.kstate(h)["state"] == "armed" and any("strike mismatch" in p for p in pushed)
    assert vs.blocks_auto(c, h) is None
    h = bk.keep_strike(c, hid, now=t19("13:54"))
    assert h["strike"] == 82300.0 and h["shot"]["mismatch"]["resolved"]["choice"] == "text"
    assert any("you kept the text's strike 82300 CE" in e["text"] for e in h["events"])
    assert tr.hint_view(h, t19("13:54"), c)["shot"]["state"] == "resolved"


def test_agreeing_screenshot_is_quiet_and_feed_shows_the_read(pushed, engine, appcl):  # noqa: F811
    c, w, s = auto_setup(vision=True)
    w.prices[S82300] = 309.0
    hid = post(c, w, s, 1, "13:52", CALL2)["hint_id"]
    n = len(pushed)
    r = shot_post(c, s, 2, "13:52", SENSEX_TH, engine)
    assert r["did"] == "agrees" and len(pushed) == n                                              # nothing pushed
    v = tr.hint_view(bk.load(hid), t19("13:53"), c)["shot"]
    assert v["state"] == "agrees" and v["label"] == "📷 screenshot: SENSEX 82300 CE · 337.35 · ✓ agrees"
    assert v["quiet"] == "✓ screenshot agrees · 337.35 shown" and v["time"] == "1:52 PM"
    # a later P&L screen: read and stored, nothing changes
    r = shot_post(c, s, 3, "14:30", L("P&L", "+₹4,250.00"), engine)
    assert r["did"] == "follow-up screenshot stored" and bk.load(hid)["shot"]["post_id"] == 2
    feed = {p["post_id"]: p for p in tr.feed_rows()}
    assert feed[2]["ocr_line"] == "SENSEX 19th NOV 82300 CE BFO" and feed[2]["label"] == f"screenshot #{hid}"
    assert feed[3]["ocr_line"] == "P&L"


def test_index_from_the_screenshot_when_the_post_named_none(pushed, engine, appcl):  # noqa: F811
    c, w, s = auto_setup(vision=True)
    hid = post(c, w, s, 1, "11:00", "buy 27500 pe near 120\nSL 90\nTGT 160")["hint_id"]
    h = bk.load(hid)
    assert h["symbol"] == "NIFTY" and h["decoded"]["symbol"] == "strike"
    r = shot_post(c, s, 2, "11:00", L("FINNIFTY 25-Nov-26 27500 PE", "118.40"), engine)
    h = bk.load(hid)
    assert r["did"] == "index from the screenshot" and h["symbol"] == "FINNIFTY" and h["decoded"]["symbol"] == "shot"
    assert "mismatch" not in h["shot"] and h["shot"]["agrees"]
    assert any("index from the screenshot: NIFTY → FINNIFTY" in e["text"] for e in h["events"])


def test_a_failed_read_says_why_on_the_card(pushed, monkeypatch, appcl):  # noqa: F811
    c, w, s = auto_setup(vision=True)
    w.prices[S82300] = 309.0
    hid = post(c, w, s, 1, "13:52", CALL2)["hint_id"]

    def missing():
        raise ImportError("No module named 'rapidocr_onnxruntime'")
    vs.drop_engine()
    monkeypatch.setattr(vs, "ENGINE_FACTORY", missing)
    shot_post(c, s, 2, "13:52", [], Engine())
    v = tr.hint_view(bk.load(hid), t19("13:53"), c)["shot"]
    assert v["state"] == "error" and v["label"].startswith("screenshot not read: OCR engine not installed")
    vs.drop_engine()


# --- the 2-day rule, vision off, the reader -------------------------------------------------------------------------------------

def test_an_old_post_is_never_read_and_the_sweep_keeps_the_text(pushed, engine, appcl):  # noqa: F811
    c, w, s = auto_setup(vision=True)
    shot_post(c, s, 5, "10:00", ZERODHA, engine, now=t19("10:00") + timedelta(days=3))
    with get_db() as conn:
        ocr = json.loads(conn.execute("SELECT ocr FROM tips_posts WHERE post_id = 5").fetchone()[0])
    assert ocr == {"error": "older than 2 days", "skipped": True} and engine.calls == 0
    shot_post(c, s, 6, "10:01", ZERODHA, engine)
    old, new = vs.MEDIA_DIR / "x" / "6.png", vs.MEDIA_DIR / "x" / "7.png"
    new.write_bytes(b"png")
    now = datetime.now(IST)
    os.utime(old, ((now - timedelta(days=3)).timestamp(),) * 2)
    os.utime(new, ((now - timedelta(days=1)).timestamp(),) * 2)
    res = vs.sweep(now)
    assert not old.exists() and new.exists() and res["removed"] >= 1
    with get_db() as conn:
        ocr = json.loads(conn.execute("SELECT ocr FROM tips_posts WHERE post_id = 6").fetchone()[0])
    assert ocr["facts"]["strike"] == 82000.0                                                       # the text survives the sweep
    assert vs.due(now, set()) == ["tips_media"] and vs.due(now, {"tips_media"}) == []


def test_vision_off_no_download_no_read(pushed, engine, appcl):  # noqa: F811
    c, w, s = auto_setup()
    assert c.tips.vision is False
    assert shot_post(c, s, 2, "13:52", ZERODHA, engine) is None and engine.calls == 0
    with get_db() as conn:
        assert conn.execute("SELECT ocr FROM tips_posts WHERE post_id = 2").fetchone()[0] is None


class Photo(SimpleNamespace):
    pass


class TeleWithMedia(FakeTele):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.downloads = []

    async def download_media(self, m, file=None):
        self.downloads.append(m.id)
        with open(file, "wb") as fh:
            fh.write(b"png")
        return file


def photo_msg(mid, when, text=""):
    m = msg(mid, text, when, media=object())
    m.photo, m.document, m.file = Photo(), None, SimpleNamespace(size=120_000, mime_type="image/jpeg", ext=".jpg")
    return m


def test_reader_downloads_reads_and_skips_old_posts(session, engine):  # noqa: F811
    st = St()
    st.config.tips.vision = True
    fresh = datetime.now(timezone.utc) - timedelta(minutes=5)
    old = datetime.now(timezone.utc) - timedelta(days=3)
    big = photo_msg(903, fresh)
    big.file = SimpleNamespace(size=3 * 1024 * 1024, mime_type="image/jpeg", ext=".jpg")
    fake = TeleWithMedia(msgs=[photo_msg(901, old), photo_msg(902, fresh), big])
    r = rd.Reader(st, client_factory=lambda: fake)
    fake.reader = r
    r.run_once()
    assert fake.downloads == [902]                                                 # never the old post, never > 2 MB
    with get_db() as conn:
        rows = {x["post_id"]: dict(x) for x in conn.execute("SELECT post_id, ocr, media_path FROM tips_posts")}
    assert json.loads(rows[901]["ocr"]) == {"error": "older than 2 days", "skipped": True}
    assert json.loads(rows[903]["ocr"])["error"] == "larger than 2 MB"
    assert rows[902]["media_path"].endswith("902.jpg") and json.loads(rows[902]["ocr"])["facts"]["strike"] == 82300.0


def test_reader_with_vision_off_never_downloads(session, engine):  # noqa: F811
    st = St()
    fake = TeleWithMedia(msgs=[photo_msg(902, datetime.now(timezone.utc) - timedelta(minutes=5))])
    r = rd.Reader(st, client_factory=lambda: fake)
    fake.reader = r
    r.run_once()
    assert fake.downloads == [] and engine.calls == 0


def test_capture_failure_is_logged_never_raised(session, engine):  # noqa: F811
    st = St()
    st.config.tips.vision = True

    class Broken(TeleWithMedia):
        async def download_media(self, m, file=None):
            raise OSError("disk full")
    m = photo_msg(904, datetime.now(timezone.utc) - timedelta(minutes=1))
    rd.ingest(st.config, "c1", 904, rd._iso(m.date), "", True, source="live", push=False, model=False)
    assert asyncio.run(vs.capture(st.config, Broken(), "c1", m)) is None


def test_strike_route(api):  # noqa: F811
    from backend.api import routes
    c = routes._state.config
    r = rd.ingest(c, "c1", 1, datetime.now(IST) - timedelta(minutes=2), "BUY SENSEX 82300 CE ABOVE 370\nSL 200\nTGT 500", False,
                  source="live", push=False, model=False)
    h = bk.load(r["hint_id"])
    assert api.post(f"/api/tips/hints/{h['id']}/strike", json={"keep": True}).status_code == 409   # no mismatch
    h["shot"] = {"post_id": 2, "facts": {"symbol": "SENSEX", "strike": 82200.0, "opt_type": "CE", "price": 535.1}, "at": h["posted_at"],
                 "mismatch": {"text": {"symbol": "SENSEX", "strike": 82300.0, "opt_type": "CE"},
                              "shot": {"symbol": "SENSEX", "strike": 82200.0, "opt_type": "CE"}, "resolved": None}}
    vs.save_shot(h)
    assert api.post(f"/api/tips/hints/{h['id']}/strike", json={"strike": -1, "opt_type": "CE"}).status_code == 400
    d = api.post(f"/api/tips/hints/{h['id']}/strike", json={"strike": 82200, "opt_type": "CE"}).json()
    assert d["strike"] == 82200.0 and d["shot"]["state"] == "resolved"
    assert api.post(f"/api/tips/hints/{h['id']}/strike", json={"keep": True}).status_code == 409


def test_import_never_downloads_and_says_why():
    from tests.tips_world import cfg
    c = cfg()
    c.tips.vision = True
    with get_db() as conn:
        rd.ingest(c, "c1", 77, t19("10:00") - timedelta(days=10), "", True, source="import", push=False, model=False, conn=conn)
        ocr = json.loads(conn.execute("SELECT ocr FROM tips_posts WHERE post_id = 77").fetchone()[0])
    assert ocr == {"error": "older than 2 days", "skipped": True}


# --- the real engine (only when installed) --------------------------------------------------------------------------------------

def _real_engine() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _real_engine(), reason="rapidocr_onnxruntime / Pillow not installed")
def test_real_engine_reads_a_generated_strike(tmp_path):
    import glob

    from PIL import Image, ImageDraw, ImageFont
    fonts = [f for f in glob.glob("/usr/share/fonts/**/*.ttf", recursive=True) if "Sans" in f] or glob.glob("/usr/share/fonts/**/*.ttf", recursive=True)
    font = ImageFont.truetype(fonts[0], 36) if fonts else ImageFont.load_default()
    img = Image.new("RGB", (520, 200), "white")
    d = ImageDraw.Draw(img)
    d.text((20, 30), "SENSEX 82000 CE", fill="black", font=font)
    d.text((20, 110), "535.10", fill="black", font=font)
    p = tmp_path / "shot.png"
    img.save(p)
    vs.drop_engine()
    r = vs.read_image(p, budget=30)
    vs.drop_engine()
    assert not r.get("error"), r
    f = vs.facts(r["lines"], t19("12:00"))
    assert f["strike"] == 82000.0 and f["opt_type"] == "CE"


def test_price_on_the_same_line_as_the_change():
    rows = [B("SENSEX 19 NOV 82000 CE", 10, 10), B("535.10", 10, 50, h=40), B("+75.25 (+16.36%)", 120, 62, h=16)]
    assert vs.facts(rows, t19("12:48"))["price"] == 535.10
