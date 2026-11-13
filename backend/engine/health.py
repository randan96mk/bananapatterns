"""🩺 Health: is every source fresh, is anything failing? One list of checks for the strip on Home and the Data
page: {key, name, ok, warn, state, detail, at}. ok=False is red, warn=True is amber, else green."""

import shutil
from datetime import datetime, timedelta

from backend.config import IST, AppConfig, db_path
from backend.database.models import get_db, get_runs, now_ist


def _ago(iso):
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=IST)
        return round((datetime.now(IST) - t).total_seconds() / 3600, 1)
    except ValueError:
        return None


def checks(config: AppConfig, has_token: bool, follow_as_of=None) -> list[dict]:
    out = []
    with get_db() as conn:
        one = lambda q, *a: conn.execute(q, a).fetchone()[0]
        day = one("SELECT MAX(date) FROM candles_daily")
        n_stocks = one("SELECT COUNT(DISTINCT instrument_token) FROM candles_daily")
        run = (get_runs(conn, 1) or [None])[0]
        scan = one("SELECT MAX(as_of) FROM scan_runs")
        uni = one("SELECT MAX(snapshot_date) FROM universe_snapshots")
        fil = one("SELECT MAX(fetched_at) FROM filings")
        intra = one("SELECT MAX(day) FROM candles_intraday")
        n_corp = one("SELECT COUNT(*) FROM corp_actions")
        since = (datetime.now(IST) - timedelta(hours=24)).isoformat(timespec="seconds")
        errs = one("SELECT COUNT(*) FROM system_logs WHERE level = 'ERROR' AND timestamp >= ?", since)
        last_err = conn.execute("SELECT timestamp, source, message FROM system_logs WHERE level = 'ERROR' ORDER BY id DESC LIMIT 1").fetchone()
        fut_as_of = one("SELECT MAX(as_of) FROM futures_contracts")
        n_fut = one("SELECT COUNT(*) FROM futures_contracts WHERE as_of = ?", fut_as_of) if fut_as_of else 0
        fut_day = one("SELECT MAX(date) FROM candles_fut_daily")
        n_fut_c = one("SELECT COUNT(DISTINCT underlying) FROM candles_fut_daily")
    today = now_ist()[:10]
    stale_days = (datetime.fromisoformat(today) - datetime.fromisoformat(day)).days if day else None
    out.append({"key": "kite", "name": "Kite", "ok": has_token, "warn": False,
                "state": "logged in" if has_token else "log in needed", "detail": "market data and candles stop without it", "at": None})
    out.append({"key": "candles", "name": "Daily candles", "ok": bool(day), "warn": bool(stale_days is not None and stale_days > 4),
                "state": f"to {day}" if day else "none", "detail": f"{n_stocks} stocks" + (f" · last sync {run['status']}" if run else ""),
                "at": run.get("finished_at") if run else None})
    out.append({"key": "scan", "name": "Evening scan", "ok": bool(scan), "warn": bool(scan and day and scan < day),
                "state": f"as of {scan}" if scan else "never", "detail": "behind the candles" if scan and day and scan < day else "matches the candles", "at": None})
    out.append({"key": "universe", "name": "Nifty 500 list", "ok": bool(uni), "warn": bool(uni and (datetime.fromisoformat(today) - datetime.fromisoformat(uni)).days > 45),
                "state": f"snapshot {uni}" if uni else "none", "detail": "refreshed from NSE", "at": None})
    h = _ago(fil)
    out.append({"key": "filings", "name": "Filings", "ok": fil is not None, "warn": bool(h is not None and h > 30),
                "state": f"{h:g} h ago" if h is not None else "never", "detail": "NSE corporate announcements", "at": fil})
    out.append({"key": "intraday", "name": "1-minute candles", "ok": True, "warn": bool(intra and day and intra < day),
                "state": f"to {intra}" if intra else "none yet", "detail": "saved by the live feed and the backtest fetch", "at": None})
    out.append({"key": "corp", "name": "Splits / bonuses", "ok": True, "warn": False, "state": f"{n_corp} on file",
                "detail": "found in the candles after each sync; prices before each are adjusted", "at": None})
    out.append({"key": "errors", "name": "Errors · 24 h", "ok": errs == 0, "warn": False, "state": str(errs),
                "detail": f"last: {last_err[1]} · {last_err[2][:80]}" if last_err else "none on record", "at": last_err[0] if last_err else None})
    try:
        u = shutil.disk_usage(db_path().parent)
        free_gb = u.free / 1e9
        out.append({"key": "disk", "name": "Disk", "ok": free_gb > 1, "warn": free_gb < 3, "state": f"{free_gb:.1f} GB free",
                    "detail": f"database {db_path().stat().st_size / 1e6:.0f} MB" if db_path().exists() else "", "at": None})
    except OSError:
        pass
    _days = lambda d: (datetime.fromisoformat(today) - datetime.fromisoformat(d)).days if d else None
    fut_age = _days(fut_as_of)
    out.append({"key": "fut_contracts", "name": "Futures contracts", "ok": True, "warn": bool(fut_age is not None and fut_age > 4),
                "state": f"as of {fut_as_of}" + (f" · {fut_age} d old" if fut_age else "") if fut_as_of else "none yet",
                "detail": f"{n_fut} NFO stock futures rows (near / next month)" if fut_as_of else "filled by the evening job's futures data step", "at": None})
    out.append({"key": "fut_candles", "name": "Futures candles", "ok": True, "warn": bool(fut_day and day and fut_day < day),
                "state": f"to {fut_day}" if fut_day else "none yet", "detail": f"{n_fut_c} underlyings · continuous near month with OI", "at": None})
    try:
        from backend.futures import ban as fo_ban
        b = fo_ban.summary(today)
    except Exception:
        b = {"date": None, "symbols": [], "stale_days": None}
    out.append({"key": "fo_ban", "name": "F&O ban list", "ok": True, "warn": bool(b["stale_days"] is not None and b["stale_days"] > 3),
                "state": (f"{b['date']} · {len(b['symbols'])} in ban" + (" · stale" if b["stale_days"] and b["stale_days"] > 3 else "")) if b["date"] else "none yet",
                "detail": "NSE MWPL ban list; a failed fetch keeps the last good day", "at": None})
    out.append({"key": "follow", "name": "Follow book", "ok": True, "warn": bool(follow_as_of and day and follow_as_of < day),
                "state": f"as of {follow_as_of}" if follow_as_of else "not built", "detail": "steps after the evening scan", "at": None})
    return out
