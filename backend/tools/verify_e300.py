"""Read-only check of E's top-300 universe on the stored data (saves nothing, sends nothing):

    ./docker.sh verify-e300

1. a sample day: A's universe size, E's, and the stocks ranked 201-300 (E can pick them, A can't);
2. the backtest of A and E next to the expected numbers (Sep 2020 -> Oct 2026 on the user's data);
3. a dry run of the paper and real evening plans: tomorrow's E candidates, marking those from ranks 201-300."""

import copy
import sys
from dataclasses import replace

from backend.config import load_config
from backend.database.models import init_db
from backend.engine import paper
from backend.engine import real
from backend.engine import trend_breakout as tb

EXPECTED = {"A": {"trades": 388, "cagr": 16.5, "stress_cagr": 14.7, "pf": 1.62, "dd": 27, "robust": "6-7/14"},
            "E": {"trades": 413, "cagr": 19.4, "stress_cagr": 17.1, "pf": 1.69, "dd": 28, "robust": "10/14"}}


def main() -> None:
    init_db()
    config = load_config()
    data = tb.load(config)
    if "error" in data:
        sys.exit(data["error"])
    cal, S = data["cal"], data["S"]
    i = len(cal) - 1
    pa, pe = tb.rules(config, "A"), replace(tb.rules(config, "E"), late_at=0.0)   # E before E+ (no late trail)
    order = tb.ranked_by_turnover(S, i, pe.min_price)
    ua, ue = tb.liquid(S, i, pa.top), tb.liquid(S, i, pe.top)
    print(f"\n1) Sample day {cal[i]}: A universe {len(ua)} (top {pa.top}) · E universe {len(ue)} (top {pe.top}) · "
          f"A inside E: {ua <= ue}")
    print(f"   Ranked {pa.top + 1}-{pe.top} (E only): " + ", ".join(order[pa.top:pe.top]))
    if data["missing"]:
        print(f"   ⚠️ {len(data['missing'])} stocks without enough candles: {', '.join(data['missing'])}")

    print("\n2) Backtest (this takes a few minutes) ...")
    first = config.breakout.warmup_sessions
    n = len(cal) - first
    close = data["index_close"]
    nifty = [close[k] / close[first - 1] * config.breakout.capital for k in range(first, len(cal))]
    nifty_cagr = tb.perf(nifty, config.breakout.capital, n)[1]
    print(f"   {cal[first]} -> {cal[-1]} · NIFTY 50 CAGR {nifty_cagr:.1f}%")
    for key, p in (("A", pa), ("E", pe)):
        r = tb.evaluate(p, cal, S, data["index_ok"], first, nifty_cagr, data["filters"])
        nb = sum(tb.evaluate(q, cal, S, data["index_ok"], first, nifty_cagr, data["filters"])["ok"]
                 for _, q in tb.neighbours(p))
        e = EXPECTED[key]
        print(f"   {tb.variant_name(key, p.top, bool(p.late_at)):<32} trades {r['trades']} (exp {e['trades']}) · CAGR {r['cagr']:.1f}% (exp {e['cagr']}) · "
              f"stressed {r['stress_cagr']:.1f}% (exp {e['stress_cagr']}) · PF {r['profit_factor']} (exp {e['pf']}) · "
              f"worst DD -{r['drawdown']:.0f}% (exp -{e['dd']}%) · robust {nb}/14 (exp {e['robust']})")

    print("\n3) Dry run of the evening plans (nothing saved, nothing sent)")
    for label, st in (("paper E", (paper._states() or {}).get("E")), ("real", real._state())):
        if not st:
            print(f"   {label}: not started")
            continue
        st = copy.deepcopy(st)
        if label == "paper E":
            paper._decide(st, data, i, pe)
        else:
            real._plan(st, data, i, config)
        buys = [o for o in st["orders"] if o["action"] == "BUY"]
        ranks = {sym: k + 1 for k, sym in enumerate(order)}
        print(f"   {label}: {len(buys)} BUY candidate(s) for the next open")
        for o in buys:
            rk = ranks.get(o["symbol"])
            print(f"     {o['symbol']:<12} turnover rank {rk}{'  <- from ranks 201-300 (E only)' if rk and rk > pa.top else ''}")


if __name__ == "__main__":
    main()
