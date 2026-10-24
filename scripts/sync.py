"""CLI for the data pipeline (the same code the API runs).

  python -m scripts.sync status              # token + data summary
  python -m scripts.sync token <ACCESS_TOKEN> # store a token obtained elsewhere
  python -m scripts.sync universe            # refresh Nifty 500 list + Kite instruments
  python -m scripts.sync candles [--full] [SYMBOL ...]   # backfill / incremental sync (then scans)
  python -m scripts.sync scan                # recompute the stage board from stored candles

Log in first via the dashboard (or http://localhost:8000/api/kite/login-url).
"""

import argparse
import sys
import threading
import time

from backend.config import AppConfig, load_config
from backend.database.models import get_db, get_runs, init_db
from backend.ingest import candles as cd
from backend.engine.scan import run_scan
from backend.ingest import universe as uni
from backend.kite_api import auth
from backend.kite_api.client import KiteClient


def make_client() -> tuple[AppConfig, KiteClient]:
    init_db()
    config = load_config(AppConfig())
    client = KiteClient(config.kite, config.ingest)
    with get_db() as conn:
        token = auth.load_fresh_token(conn)
    if token:
        client.set_access_token(token)
    elif not config.kite.access_token:
        sys.exit("No valid Kite token (expires 06:00 IST). Log in via the dashboard first.")
    return config, client


def cmd_status(_):
    init_db()
    with get_db() as conn:
        _, issued = auth.load_token(conn)
        n_bars, n_sym = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT instrument_token) FROM candles_daily").fetchone()
        runs = get_runs(conn, 3)
    fresh = auth.is_fresh(issued)
    print(f"Kite token: {'valid until ' + auth.expiry_for(issued).isoformat() if fresh else 'missing/expired'}")
    print(f"Candles: {n_bars:,} bars across {n_sym} instruments")
    for r in runs:
        print(f"  run {r['id']} {r['kind']} {r['status']} {r['started_at']} — {r['message']}")


def cmd_token(args):
    init_db()
    with get_db() as conn:
        issued = auth.store_token(conn, args.token)
    print(f"Stored. Expires {auth.expiry_for(issued).isoformat()}")


def cmd_universe(_):
    config, client = make_client()
    s = uni.refresh_universe(client, config.ingest)
    print(f"{s['members']} members ({s['source']}), {s['matched']} mapped; unmatched: {s['unmatched']}")
    print(f"indices: {s['indices']} missing: {s['missing_indices']}")


def cmd_candles(args):
    config, client = make_client()
    state = cd.JobState()
    t = threading.Thread(target=cd.run_sync, args=(client, config.ingest, state),
                         kwargs={"force_full": args.full, "symbols": args.symbols or None}, daemon=True)
    t.start()
    try:
        while t.is_alive():
            s = state.snapshot()
            print(f"\r  {s['done']}/{s['total']} failed={s['failed']} {s['current']:<20}", end="", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        state.cancel = True
        t.join()
    s = state.snapshot()
    print(f"\n{s['status']}: {s['message']}")
    if s["status"] in ("ok", "partial"):
        cmd_scan(None)
    for e in s["errors"]:
        print(f"  {e['symbol']}: {e['error']}")
    sys.exit(0 if s["status"] in ("ok", "partial") else 1)


def cmd_scan(_):
    init_db()
    config = load_config(AppConfig())
    out = run_scan(config.bases, config.screen, uni.resolve_universe(config.ingest)["stocks"], config.trend,
                   config.vcp)
    print(f"as of {out['as_of']}: {out['scanned']} scanned, {out.get('stale', 0)} stale")
    for screen, counts in out["counts"].items():
        print(f"  {screen}: " + ", ".join(f"{k} {v}" for k, v in counts.items())
              + f"; {out['changes'].get(screen, 0)} changes")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    t = sub.add_parser("token")
    t.add_argument("token")
    t.set_defaults(fn=cmd_token)
    sub.add_parser("universe").set_defaults(fn=cmd_universe)
    sub.add_parser("scan").set_defaults(fn=cmd_scan)
    c = sub.add_parser("candles")
    c.add_argument("--full", action="store_true", help="refetch full history for every symbol")
    c.add_argument("symbols", nargs="*")
    c.set_defaults(fn=cmd_candles)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
