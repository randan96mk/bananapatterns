"""₿ The API shapes (GET /api/crypto, /api/crypto/candles, /api/crypto/signals, the snapshot PNG, POST quiet / refresh,
the config section with its validation, the stream topic), the page markup markers, the archive move (rows and PNGs,
never deleted), and the rule that nothing in backend/crypto imports an order, broker or paper-book module."""

import os
import re
import subprocess
import sys
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend.config import IST, AppConfig, CryptoConfig, PERSISTED_SECTIONS
from backend.crypto import signals as sg
from backend.database import archive
from tests.crypto_world import T_DAY, cfg, hammer_world, telegram

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = ("backend.tips.kite_orders", "backend.tips.kite_auto", "backend.tips.kite_sim", "backend.tips.book", "backend.engine.paper",
             "backend.engine.real", "backend.options.book", "backend.broker", "backend.kite_api.client", "backend.tips.kite_check")


@pytest.fixture
def api(monkeypatch, fake_kite, make_client):
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    monkeypatch.setenv("BP_NO_CRYPTO", "1")
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("KITE_API_KEY", "key123")
    monkeypatch.setenv("KITE_API_SECRET", "sec")
    from fastapi.testclient import TestClient

    from backend import main
    client = make_client(fake_kite, history_years=1)
    client.config.access_token = ""
    monkeypatch.setattr(main, "KiteClient", lambda kc, ic: client)
    with TestClient(main.app) as tc:
        yield tc


def _seed(monkeypatch, c=None):
    telegram(monkeypatch)
    c = c or cfg(vol_len=20)
    hammer_world()
    sg.process(c, T_DAY, now_ts=T_DAY + 303)
    return c


def test_config_section_and_validation():
    assert "crypto" in PERSISTED_SECTIONS
    c = AppConfig()
    d = CryptoConfig()
    assert (d.enabled, d.symbol, d.tf, d.pivot_type, d.day_boundary, d.use_p) == (False, "BTCUSD", "5m", "traditional", "utc", False)
    alert = {p for p in ("hammer", "shooting_star", "bullish_engulfing", "bearish_engulfing", "morning_star", "evening_star")}
    from backend.crypto import patterns as pt
    assert {p for p in pt.SIZE if getattr(d, p) == "alert"} == alert and all(getattr(d, p) == "mark" for p in pt.SIZE if p not in alert)
    assert (d.ema_on, d.ema_len, d.vol_on, d.vol_len, d.vol_mult, d.trend_on, d.days) == (True, 20, False, 50, 1.0, False, list(pt.DAYS))
    assert (d.alert_from, d.alert_until, d.snapshot, d.forming, d.digest, d.cooldown_candles, d.stop_pad, d.max_stop, d.touch_points, d.eq_tol_pct) == \
        ("08:00", "23:00", True, False, True, 6, 10.0, 50.0, 20.0, 0.1)
    assert all(getattr(d, f"confirm_{p}") for p in ("bullish_harami", "bearish_harami", "inverted_hammer", "hanging_man", "dragonfly_doji", "gravestone_doji"))
    for bad in ({"tf": "2m"}, {"hammer": "maybe"}, {"alert_from": "25:00"}, {"pivot_type": "x"}, {"days": ["funday"]}, {"archive_days": 5}):
        with pytest.raises(ValueError):
            c.update_section("crypto", bad)
    assert c.crypto.tf == "5m" and c.crypto.hammer == "alert"                       # rolled back
    assert c.update_section("crypto", {"pivot_type": "Camarilla", "days": ["Sun", "mon"]})["pivot_type"] == "camarilla"
    assert c.crypto.days == ["mon", "sun"]


def test_api_shapes(api, monkeypatch):
    _seed(monkeypatch)
    from backend.api import routes
    routes.S().config.crypto.enabled = True
    j = api.get("/api/crypto").json()
    assert {"enabled", "running", "symbol", "tf", "source", "health", "pivots", "hours", "today", "ai", "signals", "settings", "defaults", "patterns"} <= set(j)
    assert j["source"] == "Delta Exchange" and len(j["patterns"]) == 16 and j["settings"]["tf"] == "5m"
    assert j["hours"]["from12"] == "8:00 AM" and j["hours"]["until12"] == "11:00 PM"
    s = next(x for x in j["signals"] if x["pattern"] == "hammer")
    assert {"id", "when", "label", "dir", "level", "level_px", "entry", "stop", "r1", "state", "state_word", "why", "png", "ai", "text"} <= set(s)
    assert s["when"] == "Tue 24 Nov 10:00 AM" and s["state"] == "sent" and s["png"] is True
    ch = api.get(f"/api/crypto/candles?tf=5m&from={T_DAY - 7200}&to={T_DAY + 300}").json()
    assert ch["offset"] == 19800 and ch["candles"][-1]["time"] == T_DAY + 19800 and ch["ema"] and ch["pivots"][0]["levels"]["S1"] == 59000
    assert any(x["id"] == s["id"] for x in ch["signals"])
    assert api.get("/api/crypto/signals?limit=2").json()["signals"][0]["id"] == max(x["id"] for x in j["signals"])
    png = api.get(f"/api/crypto/snap/{s['id']}.png")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png" and png.content[:4] == b"\x89PNG"
    assert api.get("/api/crypto/snap/99999.png").status_code == 404
    q = api.post("/api/crypto/quiet", json={"hours": 2}).json()
    assert q["quiet_until"] and "quiet until" in q["text"]
    assert api.post("/api/crypto/quiet", json={"hours": 100}).status_code == 400
    assert api.post("/api/crypto/quiet", json={"hours": None}).json()["quiet_until"] is None
    assert api.post("/api/crypto/refresh").json() == {"ok": True}
    r = api.put("/api/config/crypto", json={"tf": "15m", "hammer": "mark"})
    assert r.status_code == 200 and r.json()["tf"] == "15m"
    assert api.put("/api/config/crypto", json={"tf": "3m"}).status_code == 422
    assert api.get("/api/config/crypto").json()["hammer"] == "mark"
    ev = api.get("/api/stream?topics=crypto&once=1").text
    assert "event: crypto" in ev


def test_archive_moves_rows_and_snapshots(monkeypatch):
    c = _seed(monkeypatch)
    rid = sg.rows("pattern = 'hammer'")[0]["id"]
    png = Path(sg.load(rid)["png_path"])
    assert png.is_file()
    later = datetime.fromtimestamp(T_DAY, IST) + timedelta(days=400)
    out = archive.run(c, later)
    assert out["moved_by_table"]["crypto_candles"] == 34 and out["moved_by_table"]["crypto_signals"] >= 1
    assert not png.is_file() and (png.parent / "archive" / png.name).is_file()          # moved, never deleted
    con = sqlite3.connect(str(archive.archive_path()))
    assert con.execute("SELECT COUNT(*) FROM crypto_signals WHERE id = ?", (rid,)).fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM crypto_candles").fetchone()[0] == 34
    near = archive.run(c, datetime.fromtimestamp(T_DAY, IST) + timedelta(days=10))      # within 365 days: nothing moves
    assert near["moved_by_table"]["crypto_candles"] == 0


def test_nothing_imports_an_order_module():
    """Alerts only: backend/crypto never imports or calls an order, broker or paper-book module."""
    for p in (ROOT / "backend" / "crypto").glob("*.py"):
        src = p.read_text()
        for bad in FORBIDDEN + ("place_order", "kite_orders", "paper.", "tradetron", "webhook"):
            assert bad not in src, f"{p.name} mentions {bad}"
    code = ("import sys\n"
            "import backend.crypto.feed, backend.crypto.signals, backend.crypto.patterns, backend.crypto.pivots, backend.crypto.read, "
            "backend.crypto.snapshot, backend.crypto.view\n"
            "from backend.crypto import signals, read, snapshot\n"
            f"bad = [m for m in sys.modules if m.startswith({FORBIDDEN!r})]\n"
            "print('BAD', bad)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=dict(os.environ, BP_DB_PATH="/nonexistent/x.db"), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-1500:]
    assert r.stdout.strip() == "BAD []"


def test_page_markup_markers():
    app = (ROOT / "frontend/src/App.jsx").read_text()
    assert "['crypto', '₿ BTC patterns']" in app and "import('./components/CryptoPage')" in app and "tab === 'crypto' && <CryptoPage />" in app
    page = (ROOT / "frontend/src/components/CryptoPage.jsx").read_text()
    for marker in ("lightweight-charts", "createSeriesMarkers", "api.cryptoCandles", "useLive('crypto')", "Quiet 2 h…", "Resume", "Refresh",
                   "Reset to defaults", "api.putConfig('crypto'", "Detections", "Snapshot", "alerts only", "Inputs", "/api/crypto/snap/"):
        assert marker in page, marker
    for tf in ("'1m'", "'5m'", "'15m'", "'1h'"):
        assert tf in page
    assert re.search(r"\['1d', '1 d'\], \['3d', '3 d'\], \['2w', '2 w'\]", page)
    pkg = (ROOT / "frontend/package.json").read_text()
    assert '"lightweight-charts"' in pkg
    settings = (ROOT / "frontend/src/components/SettingsPanel.jsx").read_text()
    assert "'crypto'" in settings and "HELP_CRYPTO" in settings
    readme = (ROOT / "README.md").read_text()
    assert "₿ BTC pivot patterns" in readme and "never trades" in readme
    gi = (ROOT / ".gitignore").read_text().splitlines()
    assert "data/" in gi                                     # data/crypto (the snapshots) is git-ignored
