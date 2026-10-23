"""Shared fixtures: isolated SQLite DB per test and a fake Kite SDK."""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import IST  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("BP_DB_PATH", str(db))
    from backend.database.models import init_db
    init_db()
    return db


def make_series(start: date, end: date, base: float = 100.0, step: float = 1.0):
    """Weekday bars with close = base + step*i (dates as tz-aware datetimes, like the SDK)."""
    rows, d, i = [], start, 0
    while d <= end:
        if d.weekday() < 5:
            c = base + step * i
            rows.append({"date": datetime(d.year, d.month, d.day, tzinfo=IST),
                         "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000 + i})
            i += 1
        d += timedelta(days=1)
    return rows


class FakeKite:
    """Mimics the subset of kiteconnect.KiteConnect we use."""

    def __init__(self, series=None, instruments=None):
        self.series = series or {}          # token -> list of SDK-style rows
        self._instruments = instruments or []
        self.calls = []
        self.fail_queue = []                # exceptions raised by the next calls, in order
        self.access_token = None

    def set_access_token(self, t):
        self.access_token = t

    def _maybe_fail(self):
        if self.fail_queue:
            raise self.fail_queue.pop(0)

    def historical_data(self, token, from_s, to_s, interval, continuous=False, oi=False):
        self.calls.append(("historical_data", token, from_s, to_s, interval))
        self._maybe_fail()
        lo, hi = from_s[:10], to_s[:10]
        return [r for r in self.series.get(token, []) if lo <= r["date"].date().isoformat() <= hi]

    def instruments(self, exchange=None):
        self.calls.append(("instruments", exchange))
        self._maybe_fail()
        return self._instruments

    def profile(self):
        self._maybe_fail()
        return {"user_id": "AB1234"}

    def generate_session(self, request_token, api_secret):
        self._maybe_fail()
        return {"access_token": "tok-" + request_token, "user_id": "AB1234"}


@pytest.fixture
def fake_kite():
    return FakeKite()


@pytest.fixture
def make_client():
    from backend.config import IngestConfig, KiteConfig
    from backend.kite_api.client import KiteClient

    def _make(kite, **ingest_overrides):
        cfg = IngestConfig(**{"backoff_base_sec": 0.0, **ingest_overrides})
        return KiteClient(KiteConfig(api_key="key123", api_secret="sec"), cfg, kite=kite, sleep=lambda s: None)
    return _make
