from datetime import date, datetime

import pytest
from kiteconnect import exceptions as kex

from backend.config import IST, IngestConfig
from backend.database.models import get_candles, get_db, get_ingest_status, get_runs
from backend.ingest import candles as cd
from backend.ingest import universe as uni
from tests.conftest import make_series
from tests.test_universe import CSV, INSTRUMENTS, Resp

END = date(2026, 10, 23)


def test_eod_cutoff():
    cfg = IngestConfig()
    assert cd.last_complete_session_date(cfg, datetime(2026, 10, 23, 11, 0, tzinfo=IST)) == date(2026, 10, 22)
    assert cd.last_complete_session_date(cfg, datetime(2026, 10, 23, 16, 0, tzinfo=IST)) == date(2026, 10, 23)


def test_full_then_incremental(fake_kite, make_client):
    fake_kite.series[7] = make_series(date(2018, 1, 1), date(2026, 10, 20))
    client = make_client(fake_kite)
    r1 = cd.sync_symbol(client, client.ingest, 7, "X", date(2026, 10, 20))
    assert r1["outcome"] == "full" and r1["bars"] > 2000
    n_hist = len([c for c in fake_kite.calls if c[0] == "historical_data"])
    assert n_hist == 2  # 8 years = 2 chunks

    fake_kite.series[7] = make_series(date(2018, 1, 1), END)  # same history + 3 new bars
    r2 = cd.sync_symbol(client, client.ingest, 7, "X", END)
    assert r2["outcome"] == "incremental" and r2["added"] == 3
    with get_db() as conn:
        assert get_candles(conn, 7, limit=1)[0]["date"] == "2026-10-23"
        st = get_ingest_status(conn, 7)
    assert st["bars"] == r2["bars"] and st["last_error"] is None

    r3 = cd.sync_symbol(client, client.ingest, 7, "X", END)
    assert r3["outcome"] == "uptodate"


def test_split_triggers_full_refetch(fake_kite, make_client):
    fake_kite.series[7] = make_series(date(2024, 1, 1), date(2026, 10, 20), base=1000)
    client = make_client(fake_kite, history_years=3)
    cd.sync_symbol(client, client.ingest, 7, "X", date(2026, 10, 20))
    # 1:2 split => Kite now returns all history halved
    fake_kite.series[7] = make_series(date(2024, 1, 1), END, base=500, step=0.5)
    r = cd.sync_symbol(client, client.ingest, 7, "X", END)
    assert r["outcome"] == "refetched"
    with get_db() as conn:
        first = get_candles(conn, 7)[0]
    assert first["close"] == pytest.approx(500.0)


def test_empty_refetch_keeps_history(fake_kite, make_client):
    fake_kite.series[7] = make_series(date(2025, 1, 1), date(2026, 10, 20))
    client = make_client(fake_kite, history_years=2)
    cd.sync_symbol(client, client.ingest, 7, "X", date(2026, 10, 20))
    fake_kite.series[7] = []
    with pytest.raises(RuntimeError):
        cd.sync_symbol(client, client.ingest, 7, "X", END, force_full=True)
    with get_db() as conn:
        assert len(get_candles(conn, 7)) > 300


def _setup_universe(fake_kite, make_client, **kw):
    fake_kite._instruments = INSTRUMENTS
    client = make_client(fake_kite, history_years=1, **kw)
    uni.refresh_universe(client, client.ingest, http_get=lambda u: Resp(CSV))
    for tok in (3329, 408065, 268041):
        fake_kite.series[tok] = make_series(date(2025, 10, 1), END)
    return client


def test_run_sync_records_per_symbol_failure_and_continues(fake_kite, make_client):
    client = _setup_universe(fake_kite, make_client, max_retries=0)
    original = fake_kite.historical_data

    def flaky(token, *a, **k):
        if token == 3329:
            raise kex.InputException("invalid instrument")
        return original(token, *a, **k)
    fake_kite.historical_data = flaky
    state = cd.JobState()
    snap = cd.run_sync(client, client.ingest, state, now=datetime(2026, 10, 23, 18, 0, tzinfo=IST))
    assert snap["status"] == "partial" and snap["total"] == 3 and snap["failed"] == 1
    assert snap["errors"][0]["symbol"] == "ABB"
    with get_db() as conn:
        assert "invalid instrument" in get_ingest_status(conn, 3329)["last_error"]
        assert get_runs(conn)[0]["status"] == "partial"


def test_run_sync_aborts_on_token_expiry(fake_kite, make_client):
    client = _setup_universe(fake_kite, make_client)
    fake_kite.fail_queue = [kex.TokenException("expired")]
    snap = cd.run_sync(client, client.ingest, cd.JobState(), now=datetime(2026, 10, 23, 18, 0, tzinfo=IST))
    assert snap["status"] == "aborted" and "Log in again" in snap["message"]
    assert snap["done"] == 0
