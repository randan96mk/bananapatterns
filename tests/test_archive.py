"""🧹 Archive and vacuum: old log rows MOVE to data/archive.db (never deleted), the rest stays."""

import sqlite3
import threading
from datetime import datetime, timedelta

import pytest

from backend.config import IST, AppConfig
from backend.database import archive
from backend.database.models import get_db
from tests.test_api import api  # noqa: F401  (the app fixture)

NOW = datetime(2026, 11, 15, 3, 5, tzinfo=IST)          # a Sunday, 3:05 AM
OLD = (NOW - timedelta(days=200)).date().isoformat()     # past the 120-day cutoff
NEW = (NOW - timedelta(days=10)).date().isoformat()


def _seed(now=NOW):
    OLD = (now - timedelta(days=200)).date().isoformat()
    NEW = (now - timedelta(days=10)).date().isoformat()
    with get_db() as conn:
        for day in (OLD, NEW):
            for i in range(3):
                conn.execute("INSERT INTO options_chain_snap (ts, day, kind, expiry, strike, type, ltp) VALUES (?, ?, 'live', ?, ?, 'CE', 1)",
                             (f"{day}T10:0{i}:00+05:30", day, day, 24000 + i))
            conn.execute("INSERT INTO tips_quotes (hint_id, t, ltp) VALUES (1, ?, 5)", (f"{day}T10:00:00+05:30",))
            conn.execute("INSERT INTO ai_jobs (created, kind, key, status) VALUES (?, 'night', ?, 'done')", (f"{day}T20:00:00+05:30", day))
            conn.execute("INSERT INTO sent_alerts (key, sent_at, kind, text, ok) VALUES (?, ?, 'breakout', 'x', 1)",
                         (f"{day}|bases|ABC|breakout", f"{day}T18:30:00+05:30"))
        conn.execute("INSERT INTO ai_jobs (created, kind, key, status) VALUES (?, 'night', 'stuck', 'queued')", (f"{OLD}T20:00:00+05:30",))
        conn.execute("INSERT INTO sent_alerts (key, sent_at, kind, text, ok) VALUES (?, ?, 'breakout', 'x', 0)",
                     (f"{OLD}|bases|FAIL|breakout", f"{OLD}T18:30:00+05:30"))      # never sent: stays
        conn.execute("INSERT INTO sent_alerts (key, sent_at, kind, text, ok) VALUES ('near|bases|XYZ|100.0', ?, 'near', 'x', 1)",
                     (f"{OLD}T18:30:00+05:30",))                                       # once-per-base dedup: stays
        for job, day in (("eod", OLD), ("eod", NEW), ("weekly", OLD)):                # weekly: only an old row, the latest
            conn.execute("INSERT INTO job_runs (job, run_date, started_at, status) VALUES (?, ?, ?, 'ok')", (job, day, f"{day}T18:00:00"))


def _count(table, db=None):
    if db:
        c = sqlite3.connect(str(db))
        try:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            c.close()
    with get_db() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_old_rows_move_young_stay_and_the_archive_gets_them():
    _seed()
    r = archive.run(AppConfig(), NOW)
    assert r["moved_by_table"] == {"options_chain_snap": 3, "tips_quotes": 1, "tips_ticks": 0, "ai_jobs": 1, "job_runs": 1, "sent_alerts": 1}
    assert r["moved"] == 7 and r["error"] is None and r["vacuumed"] is True
    with get_db() as conn:
        assert {x[0] for x in conn.execute("SELECT DISTINCT day FROM options_chain_snap")} == {NEW}
        assert [x[0] for x in conn.execute("SELECT key FROM ai_jobs ORDER BY id")] == [NEW, "stuck"]   # queued stays
        keys = {x[0] for x in conn.execute("SELECT key FROM sent_alerts")}
        assert keys == {f"{NEW}|bases|ABC|breakout", f"{OLD}|bases|FAIL|breakout", "near|bases|XYZ|100.0"}
        runs = {(x[0], x[1]) for x in conn.execute("SELECT job, run_date FROM job_runs WHERE job != 'archive'")}
        assert runs == {("eod", NEW), ("weekly", OLD)}                                 # the latest row per job stays
    db = archive.archive_path()
    assert db.exists() and db.parent == archive.db_path().parent
    assert _count("options_chain_snap", db) == 3 and _count("tips_quotes", db) == 1 and _count("sent_alerts", db) == 1
    assert _count("job_runs", db) == 1 and _count("ai_jobs", db) == 1
    c = sqlite3.connect(str(db))
    try:
        assert c.execute("SELECT strike FROM options_chain_snap ORDER BY strike").fetchall() == [(24000,), (24001,), (24002,)]
    finally:
        c.close()


def test_second_run_is_idempotent_and_nothing_is_lost():
    _seed()
    total = sum(_count(t) for t in archive.TABLES)
    archive.run(AppConfig(), NOW)
    again = archive.run(AppConfig(), NOW)
    assert again["moved"] == 0
    db = archive.archive_path()
    live_after = sum(_count(t) for t in archive.TABLES) - 2      # the two archive job_runs rows the runs added
    assert live_after + sum(_count(t, db) for t in archive.TABLES) == total


def test_a_half_finished_move_is_completed_without_duplicates():
    """A crash after the copy but before the delete leaves the rows in both files: the next run deletes the live copy only."""
    _seed()
    archive.run(AppConfig(), NOW)
    with get_db() as conn:                                          # put one archived row back, as if its delete never ran
        conn.execute("INSERT INTO tips_quotes (hint_id, t, ltp) VALUES (1, ?, 5)", (f"{OLD}T10:00:00+05:30",))
    r = archive.run(AppConfig(), NOW)
    assert r["moved_by_table"]["tips_quotes"] == 1 and _count("tips_quotes", archive.archive_path()) == 1


def test_respects_archive_days_and_vacuum_off():
    _seed()
    cfg = AppConfig()
    cfg.maintenance.archive_days = 365
    cfg.maintenance.vacuum = False
    r = archive.run(cfg, NOW)
    assert r["moved"] == 0 and r["vacuumed"] is False


def test_busy_while_running():
    archive._lock.acquire()
    try:
        with pytest.raises(archive.Busy):
            archive.run(AppConfig(), NOW)
    finally:
        archive._lock.release()


def test_due_once_a_week_at_the_set_time():
    cfg = AppConfig()
    sun = NOW.replace(hour=2, minute=59)
    assert archive.due(sun, cfg, set()) == []
    assert archive.due(sun.replace(hour=3, minute=0), cfg, set()) == ["archive"]
    assert archive.due(sun.replace(hour=22), cfg, set()) == ["archive"]                # missed: catches up that day
    assert archive.due(sun.replace(hour=4), cfg, {"archive"}) == []                     # once
    assert archive.due(sun + timedelta(days=1), cfg, set()) == []                       # Monday
    cfg.maintenance.enabled = False
    assert archive.due(sun.replace(hour=4), cfg, set()) == []
    cfg.maintenance.enabled, cfg.maintenance.day, cfg.maintenance.time = True, "monday", "01:30"
    assert archive.due((sun + timedelta(days=1)).replace(hour=1, minute=30), cfg, set()) == ["archive"]
    assert archive.next_at(AppConfig(), sun) == "2026-11-15T03:00:00+05:30"
    assert archive.next_at(AppConfig(), sun.replace(hour=5), {"at": "2026-11-15T03:00:00+05:30"}) == "2026-11-22T03:00:00+05:30"


def test_scheduler_runs_the_job_and_records_it(monkeypatch):
    from backend.alerts.jobs import Scheduler

    class St:
        config = AppConfig()
    _seed()
    s = Scheduler(St())
    out = s.run_archive(NOW)
    assert out["status"] == "ok" and out["moved"] == 7
    assert "archive" in Scheduler.done_today(NOW.date())
    with get_db() as conn:
        msg = conn.execute("SELECT message FROM job_runs WHERE job = 'archive'").fetchone()[0]
    assert "moved 7 rows" in msg and "options_chain_snap 3" in msg and "archive" in msg and "MB" in msg


def test_settings_section_and_validation():
    cfg = AppConfig()
    assert cfg.section("maintenance").archive_days == 120 and cfg.maintenance.vacuum is True
    assert cfg.maintenance.day == "sunday" and cfg.maintenance.time == "03:00" and cfg.maintenance.enabled is True
    with pytest.raises(ValueError):
        cfg.update_section("maintenance", {"day": "funday"})
    with pytest.raises(ValueError):
        cfg.update_section("maintenance", {"time": "25:00"})
    assert cfg.maintenance.day == "sunday" and cfg.maintenance.time == "03:00"     # rolled back
    assert cfg.update_section("maintenance", {"day": "Saturday"})["day"] == "saturday"


def test_health_row():
    from backend.engine import health
    row = {c["key"]: c for c in health.checks(AppConfig(), has_token=False)}["archive"]
    assert row["ok"] and not row["warn"] and row["state"] == "not run yet"
    archive.run(AppConfig(), NOW)
    assert archive.health_row(AppConfig(), NOW.date() + timedelta(days=3))["warn"] is False
    old = archive.health_row(AppConfig(), NOW.date() + timedelta(days=11))
    assert old["warn"] is True and "11 d ago" in old["state"]


def test_asif_window_capped_at_archive_days():
    from backend.options import asif
    cfg = AppConfig()
    cfg.maintenance.archive_days = 60
    assert asif.report(cfg, 90, today=NOW.date())["days"] == 60
    assert asif.report(AppConfig(), 90, today=NOW.date())["days"] == 90


def test_api_status_run_and_409(api):  # noqa: F811
    tc, routes = api
    _seed(datetime.now(IST))
    s = tc.get("/api/maintenance").json()
    assert s["last_run"] is None and s["settings"]["archive_days"] == 120 and s["next_at"] and s["running"] is False
    r = tc.post("/api/maintenance/archive")
    assert r.status_code == 200 and r.json()["moved"] == 7          # the cutoff is from today: the 200-day-old rows go
    s = tc.get("/api/maintenance").json()
    assert s["last_run"]["moved"] == 7 and s["moved_by_table"]["options_chain_snap"] == 3
    assert s["archive_mb"] > 0 and s["live_mb"] > 0
    started, release = threading.Event(), threading.Event()

    def slow(config, now):
        started.set()
        release.wait(5)
        return {}
    import backend.database.archive as mod
    orig = mod._run
    mod._run = slow
    try:
        t = threading.Thread(target=lambda: mod.run(AppConfig()))
        t.start()
        started.wait(5)
        assert tc.post("/api/maintenance/archive").status_code == 409
        assert tc.get("/api/maintenance").json()["running"] is True
        release.set()
        t.join(5)
    finally:
        mod._run = orig
