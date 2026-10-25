from datetime import datetime

from backend.config import IST
from backend.database.models import get_db
from backend.kite_api import auth


def ist(*a):
    return datetime(*a, tzinfo=IST)


def test_boundary_before_and_after_six():
    assert auth.last_expiry_boundary(ist(2026, 10, 23, 5, 59)) == ist(2026, 10, 22, 6, 0)
    assert auth.last_expiry_boundary(ist(2026, 10, 23, 6, 0)) == ist(2026, 10, 23, 6, 0)


def test_token_issued_after_midnight_dies_at_six():
    issued = ist(2026, 10, 23, 1, 0)  # issued after midnight: still dies at 06:00 that morning
    assert auth.is_fresh(issued, ist(2026, 10, 23, 5, 30))
    assert not auth.is_fresh(issued, ist(2026, 10, 23, 6, 1))
    assert auth.expiry_for(issued) == ist(2026, 10, 23, 6, 0)


def test_evening_token_survives_midnight():
    issued = ist(2026, 10, 23, 19, 0)
    assert auth.is_fresh(issued, ist(2026, 10, 24, 2, 0))
    assert auth.expiry_for(issued) == ist(2026, 10, 24, 6, 0)


def test_store_and_load_fresh():
    with get_db() as conn:
        auth.store_token(conn, "abc", ist(2026, 10, 23, 9, 0))
        assert auth.load_fresh_token(conn, ist(2026, 10, 23, 18, 0)) == "abc"
        assert auth.load_fresh_token(conn, ist(2026, 10, 24, 7, 0)) is None
