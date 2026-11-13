"""Read-only probe for the stock-futures module: what Kite gives us for NFO stock futures.

Run on the server with a logged-in Kite session:  python -m backend.tools.probe_futures [--names 12] [--years 3]

Prints, for the near-month future of the most liquid F&O stocks: lot size, expiry, lot value, continuous daily
history depth (rows per year, first date), the size of the price jump at each monthly roll (basis), today's OI and
the cash-vs-futures basis. Nothing is ordered; no holdings, orders or positions are read: instruments, quotes and
historical candles only. Paste the output back to plan the eligibility filter with real numbers.
"""

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta

from backend.config import load_config
from backend.database.models import get_db
from backend.kite_api.auth import load_fresh_token
from backend.kite_api.client import KiteClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", type=int, default=12, help="how many stocks (by futures OI) to probe")
    ap.add_argument("--years", type=int, default=3, help="years of continuous history to request")
    ap.add_argument("--list", type=float, default=0, help="only print the eligible underlyings: lot value <= this many rupees (e.g. 600000), by OI")
    a = ap.parse_args()
    cfg = load_config()
    kc = KiteClient(cfg.kite, cfg.ingest)
    if not kc.has_token:                                   # the dashboard login keeps the token in the database
        with get_db() as conn:
            tok = load_fresh_token(conn)
        if tok:
            kc.set_access_token(tok)
    if not kc.has_token:
        print("Kite: not logged in (log in from the dashboard first)")
        return 1
    kite = kc.kite

    nfo = kite.instruments("NFO")
    futs = [i for i in nfo if i["instrument_type"] == "FUT" and i["segment"] == "NFO-FUT"]
    by_name = defaultdict(list)
    for i in futs:
        by_name[i["name"]].append(i)
    stock_names = sorted(n for n in by_name if n not in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"))
    print(f"NFO futures: {len(futs)} contracts · {len(stock_names)} stocks with futures")

    # the near-month contract of every stock, then today's OI via quote (1 req/s, 500 symbols a call)
    near = {}
    for n in stock_names:
        cs = sorted(by_name[n], key=lambda i: i["expiry"])
        if cs:
            near[n] = cs[0]
    quotes = {}
    syms = [f"NFO:{c['tradingsymbol']}" for c in near.values()]
    for k in range(0, len(syms), 400):
        quotes.update(kite.quote(syms[k:k + 400]))
    rows = []
    for n, c in near.items():
        q = quotes.get(f"NFO:{c['tradingsymbol']}") or {}
        px = q.get("last_price") or 0
        rows.append((n, c, px, q.get("oi") or 0, px * c["lot_size"]))
    rows.sort(key=lambda r: -r[3])
    if a.list:                                             # the names to put in the Tradetron list, one per line
        ok = [r for r in rows if 0 < r[4] <= a.list and r[3] > 0]
        print(f"\nEligible underlyings (near-month lot value <= Rs {a.list:,.0f}, with open interest): {len(ok)} of {len(rows)}")
        print(f"{'stock':<14}{'contract':<22}{'lot':>6}{'lot value':>12}{'OI':>14}")
        for n, c, px, oi, lv in ok:
            print(f"{n:<14}{c['tradingsymbol']:<22}{c['lot_size']:>6}{lv:>12,.0f}{oi:>14,}")
        print("\nComma-separated (underlying names):\n" + ",".join(n for n, *_ in ok))
        return 0
    print(f"\nTop {a.names} by futures OI (near month {rows[0][1]['expiry'] if rows else '?'}):")
    print(f"{'stock':<14}{'contract':<22}{'lot':>6}{'price':>10}{'lot value':>14}{'OI':>12}{'margin~20%':>12}")
    cash = {}
    try:
        cq = kite.quote([f"NSE:{r[0]}" for r in rows[:a.names]])
        cash = {k.split(":", 1)[1]: v.get("last_price") for k, v in cq.items()}
    except Exception as exc:                                   # a name may differ on NSE; the basis column stays blank
        print(f"(cash quotes: {exc})")
    for n, c, px, oi, lv in rows[:a.names]:
        print(f"{n:<14}{c['tradingsymbol']:<22}{c['lot_size']:>6}{px:>10.2f}{lv:>14,.0f}{oi:>12,}{lv * 0.2:>12,.0f}")

    print(f"\nContinuous daily history ({a.years} y) · roll jumps = |close − previous close| on expiry-change days:")
    end, start = date.today(), date.today() - timedelta(days=365 * a.years)
    for n, c, px, oi, lv in rows[:a.names]:
        try:
            bars = kite.historical_data(c["instrument_token"], f"{start} 00:00:00", f"{end} 23:59:59", "day", continuous=True, oi=True)
        except Exception as exc:
            print(f"{n:<14} continuous history failed: {exc}")
            continue
        if not bars:
            print(f"{n:<14} no bars")
            continue
        closes = [float(b["close"]) for b in bars]
        jumps = []
        for i in range(1, len(bars)):                          # a jump of 1%+ overnight with no cash gap is mostly a roll
            ch = closes[i] / closes[i - 1] - 1
            if abs(ch) > 0.01:
                jumps.append(ch * 100)
        per_year = len(bars) / max(1, (end - start).days / 365)
        basis = f"{(px / cash[n] - 1) * 100:+.2f}%" if cash.get(n) and px else "—"
        med_jump = f"{statistics.median([abs(j) for j in jumps]):.2f}%" if jumps else "—"
        print(f"{n:<14} rows {len(bars):>5} ({per_year:5.0f}/y) from {bars[0]['date'].date() if hasattr(bars[0]['date'], 'date') else bars[0]['date']}"
              f" · 1%+ overnight moves {len(jumps):>3} (median {med_jump}) · basis vs cash {basis}")
    print("\nDone. Nothing was ordered or read beyond instruments, quotes and candles.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
