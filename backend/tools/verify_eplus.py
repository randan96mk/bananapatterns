"""Read-only check of E+ (E with the late trail) on the stored data (saves nothing, sends nothing):

    ./docker.sh verify-eplus

1. the backtest of A, E (no late trail) and E+ next to the expected numbers (Sep 2020 -> Oct 2026, your data);
2. the trades the late trail closed: entry, best close, stop, exit;
3. every open E position in paper and real: entry, best close (backfilled from the stored candles), stop, trail;
4. a dry run of tonight: the stops the late trail would raise for the next session."""

import sys
from dataclasses import replace

from backend.config import load_config
from backend.database.models import init_db
from backend.engine import paper, real
from backend.engine import trend_breakout as tb

EXPECTED = {
    "A": {"cagr": 16.5, "stress_cagr": 14.7, "drawdown": 27, "trades": 388, "win_rate": 30, "profit_factor": 1.62,
          "splits": "+76.8 / +16.9 / +24.0", "stress": "+71 / +13 / +21", "robust": "6/14", "trail": 0},
    "E": {"cagr": 19.4, "stress_cagr": 17.1, "drawdown": 28, "trades": 413, "win_rate": 31, "profit_factor": 1.69,
          "splits": "+68.3 / +16.4 / +52.0", "stress": "+59 / +13 / +47", "robust": "10/14", "trail": 0,
          "avg": "7,430 / -2,015", "best": 440},
    "E+": {"cagr": 21.2, "stress_cagr": 18.2, "drawdown": 25, "trades": 414, "win_rate": 31, "profit_factor": 1.75,
           "splits": "+78.1 / +20.7 / +51.7", "stress": "+60 / +19 / +47", "robust": "14/18", "trail": 2,
           "avg": "8,145 / -2,135", "best": 440,
           "years": {"2020": -3.9, "2021": 43.3, "2022": -1.6, "2023": 54.8, "2024": 16.4, "2025": 2.1, "2026": 30.8}},
}


def _line(label, got, exp):
    print(f"   {label:<26} {str(got):<26} expected {exp}")


def main() -> None:
    init_db()
    config = load_config()
    data = tb.load(config)
    if "error" in data:
        sys.exit(data["error"])
    cal, close = data["cal"], data["index_close"]
    first = config.breakout.warmup_sessions
    n = len(cal) - first
    nifty = [close[k] / close[first - 1] * config.breakout.capital for k in range(first, len(cal))]
    nifty_cagr = tb.perf(nifty, config.breakout.capital, n)[1]
    print(f"\n1) Backtest {cal[first]} -> {cal[-1]} · NIFTY 50 CAGR {nifty_cagr:.1f}% (this takes several minutes)")
    pe_plus = tb.rules(config, "E")
    runs = {"A": tb.rules(config, "A"), "E": replace(pe_plus, late_at=0.0), "E+": pe_plus}
    out = {}
    for name, p in runs.items():
        v = tb._variant(data, p, "A" if name == "A" else "E", first, nifty, nifty_cagr)
        out[name] = v
        s, st, e = v["normal"], v["stress"], EXPECTED[name]
        print(f"\n   {v['name']}")
        _line("CAGR after costs", f"{s['cagr']:.1f}%", f"{e['cagr']}%")
        _line("CAGR stressed", f"{st['cagr']:.1f}%", f"{e['stress_cagr']}%")
        _line("worst drawdown", f"-{s['drawdown']:.0f}%", f"-{e['drawdown']}%")
        _line("trades · won", f"{s['trades']} · {s['win_rate']:.0f}%", f"{e['trades']} · {e['win_rate']}%")
        _line("profit factor", s["profit_factor"], e["profit_factor"])
        _line("dev / val / oos", " / ".join(f"{x['net']:+.1f}" for x in v["splits"]), e["splits"])
        _line("stressed dev / val / oos", " / ".join(f"{x['stress']:+.0f}" for x in v["splits"]), e["stress"])
        _line("robust", f"{v['robust_pass']}/{len(v['robust'])}", e["robust"])
        _line("exits by late trail", s["exits"].get("trail", 0), e["trail"])
        if "avg" in e:
            _line("average win / loss", f"{s['avg_win']:,.0f} / {s['avg_loss']:,.0f}", e["avg"])
            _line("biggest winner", f"+{s['best_trade_pct']:.0f}%", f"+{e['best']}%")
        if "years" in e:
            _line("by year", " ".join(f"{y['year']} {y['net']:+.1f}" for y in v["years"]),
                  " ".join(f"{k} {x:+.1f}" for k, x in e["years"].items()))

    print("\n2) Trades closed by the late trail (E+)")
    tr = [t for t in out["E+"]["trades"] if t["why"] == "trail"]
    for t in tr:
        print(f"   {t['sym']:<12} in {t['in']} at {t['buy']:,.2f} · best close {t['best']:,.2f} "
              f"(+{(t['best'] / t['buy'] - 1) * 100:.0f}%) · stop {t['stop']:,.2f} · out {t['out']} at {t['sell']:,.2f} "
              f"({(t['sell'] / t['buy'] - 1) * 100:+.0f}%) · net {t['net']:+,.0f}")
    if not tr:
        print("   none")

    print("\n3) Open E positions now, and 4) tonight's dry run (nothing saved, nothing sent)")
    day = cal[-1]
    for label, st, only_e in (("paper E", (paper._states() or {}).get("E"), False), ("real", real._state(), True)):
        if not st:
            print(f"   {label}: not started")
            continue
        pos = {s: x for s, x in st["positions"].items() if not only_e or x.get("variant") == "E"}
        print(f"   {label}: {len(pos)} open E position(s) · E+ since {st.get('eplus_since') or 'not yet (first evening after the update)'}")
        for sym, x in sorted(pos.items()):
            best = max(paper.best_since(data["S"], sym, x["d"], day) or x["px"], x.get("best") or x["px"])
            want = tb.late_stop(x["px"], best, pe_plus)
            gain = (best / x["px"] - 1) * 100
            tail = (f"trail ACTIVE · would raise the stop to {want:,.2f} for the next session" if want and want > x["stop"]
                    else "trail active, stop already at or over it" if want else f"trail starts at +{pe_plus.late_at:g}%")
            print(f"     {sym:<12} entry {x['d']} at {x['px']:,.2f} · best close {best:,.2f} ({gain:+.0f}%) · "
                  f"stop {x['stop']:,.2f} · {tail}")


if __name__ == "__main__":
    main()
