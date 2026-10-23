from datetime import date

import pytest
from kiteconnect import exceptions as kex

from backend.kite_api.client import (KiteAuthError, KitePermissionError, KiteRequestError, _date_chunks)
from tests.conftest import make_series


def test_date_chunks_cover_range_without_overlap():
    chunks = _date_chunks(date(2018, 1, 1), date(2026, 10, 23), 2000)
    assert chunks[0][0] == date(2018, 1, 1) and chunks[-1][1] == date(2026, 10, 23)
    assert all((b - a).days + 1 <= 2000 for a, b in chunks)
    assert all((n[0] - p[1]).days == 1 for p, n in zip(chunks, chunks[1:]))
    assert len(chunks) == 2  # 8 years = 2 calls per symbol


def test_historical_daily_chunks_and_dedupes(fake_kite, make_client):
    fake_kite.series[1] = make_series(date(2020, 1, 1), date(2020, 3, 31))
    client = make_client(fake_kite, chunk_days=30)
    out = client.historical_daily(1, date(2020, 1, 1), date(2020, 3, 31))
    assert len([c for c in fake_kite.calls if c[0] == "historical_data"]) == 4
    assert [c["date"] for c in out] == sorted({c["date"] for c in out})
    assert out[0]["date"] == "2020-01-01" and isinstance(out[0]["volume"], int)


def test_retries_network_and_429_then_succeeds(fake_kite, make_client):
    fake_kite.series[1] = make_series(date(2020, 1, 1), date(2020, 1, 10))
    fake_kite.fail_queue = [kex.NetworkException("Too many requests", code=429),
                            kex.GeneralException("bad gateway", code=502)]
    out = make_client(fake_kite).historical_daily(1, date(2020, 1, 1), date(2020, 1, 10))
    assert len(out) == 8


def test_gives_up_loudly_after_retries(fake_kite, make_client):
    fake_kite.fail_queue = [kex.NetworkException("down")] * 10
    with pytest.raises(KiteRequestError):
        make_client(fake_kite, max_retries=2).historical_daily(1, date(2020, 1, 1), date(2020, 1, 10))


def test_token_error_is_fatal_and_not_retried(fake_kite, make_client):
    fake_kite.fail_queue = [kex.TokenException("expired")]
    with pytest.raises(KiteAuthError):
        make_client(fake_kite).historical_daily(1, date(2020, 1, 1), date(2020, 1, 10))
    assert len(fake_kite.calls) == 1


def test_permission_error_is_fatal(fake_kite, make_client):
    fake_kite.fail_queue = [kex.PermissionException("no historical")]
    with pytest.raises(KitePermissionError):
        make_client(fake_kite).historical_daily(1, date(2020, 1, 1), date(2020, 1, 10))


def test_input_error_not_retried(fake_kite, make_client):
    fake_kite.fail_queue = [kex.InputException("invalid token")]
    with pytest.raises(KiteRequestError):
        make_client(fake_kite).historical_daily(1, date(2020, 1, 1), date(2020, 1, 10))
    assert len(fake_kite.calls) == 1
