"""Who is buying and selling: NSE bulk / block deals and big-holder disclosures (SEBI SAST Reg 29), plus the
red flags and fund-buying signals built from them and from the exchange filings.

Sources (NSE's own public data, fetched with the filings job):
  bulk.csv / block.csv   https://nsearchives.nseindia.com/content/equities/ : the latest session's deals
  SAST Reg 29            https://www.nseindia.com/api/corporate-sast-reg29 : holders crossing / moving 2%+
NSE's pledge and insider-trading (PIT) feeds answer scripts with empty data, so pledges are not covered.

Signals for a stock (`signals`):
  red    within red_flag_days: promoter sold >= stake_change_min_pct (SAST), a regulatory / legal filing (not
         one "in favour" of the company), auditor resignation, credit-rating downgrade. Holds the stock at tier B.
  buy    within deal_days: a fund / institution net-bought >= deal_min_cr in bulk / block deals, or raised its
         stake by >= stake_change_min_pct (SAST acquisition by a non-promoter institution)
  sell   the same, selling (shown as a caution, not a red flag)
Prop traders who buy and sell the same stock on the same day net out and are ignored.
"""

import csv
import io
from datetime import date, datetime, timedelta
from typing import Optional

import httpx

from backend.database.models import get_db, now_ist
from backend.ingest.holidays import NSE_HOME, UA
from backend.logging_config.logger import setup_logger

logger = setup_logger("holders")

BULK_CSV = "https://nsearchives.nseindia.com/content/equities/bulk.csv"
BLOCK_CSV = "https://nsearchives.nseindia.com/content/equities/block.csv"
SAST_API = "https://www.nseindia.com/api/corporate-sast-reg29"
KEEP_DAYS = 120

INSTITUTION_WORDS = ("MUTUAL FUND", " FUND", "FUNDS", "INSURANCE", "PENSION", "ASSET MANAGEMENT", " AMC",
                     "INVESTMENT TRUST", "FMR LLC", "FIDELITY", "VANGUARD", "BLACKROCK", "GOLDMAN SACHS",
                     "MORGAN STANLEY", "SOCIETE GENERALE", "BNP PARIBAS", "CITIGROUP", "NOMURA", "HSBC", "UBS ",
                     "J P MORGAN", "JPMORGAN", "GOVERNMENT OF", "MONETARY AUTHORITY", "NORGES", "ABU DHABI",
                     "KUWAIT", "CAPITAL GROUP", "INVESCO", "NIPPON", "TEMPLETON", "ABERDEEN", "SCHRODER")


def institutional(name: str) -> bool:
    n = f" {name.upper()} "
    return any(w in n for w in INSTITUTION_WORDS)


def _num(v) -> Optional[float]:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_deals_csv(text: str, source: str) -> list[dict]:
    """bulk.csv / block.csv -> [{date, symbol, client, side, qty, price, source}]."""
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 7 or row[0].strip().lower() == "date":
            continue
        try:
            d = datetime.strptime(row[0].strip(), "%d-%b-%Y").date().isoformat()
        except ValueError:
            continue                                           # "NO RECORDS"
        qty, price = _num(row[5]), _num(row[6])
        side = row[4].strip().upper()
        if not row[1].strip() or qty is None or price is None or side not in ("BUY", "SELL"):
            continue
        out.append({"date": d, "symbol": row[1].strip().upper(), "client": " ".join(row[3].split()), "side": side,
                    "qty": qty, "price": price, "source": source, "pct": None, "after_pct": None, "promoter": 0,
                    "url": ""})
    return out


def parse_sast(payload: dict) -> list[dict]:
    """SAST Reg 29 JSON -> deals rows (source 'sast'; pct = the stake bought / sold, after_pct = the holding after)."""
    out = []
    for r in (payload or {}).get("data", []):
        kind = (r.get("acqSaleType") or "").lower()
        if kind not in ("acquisition", "sale"):
            continue
        try:
            d = datetime.strptime((r.get("timestamp") or "")[:11], "%d-%b-%Y").date().isoformat()
        except ValueError:
            continue
        sym = (r.get("symbol") or "").strip().upper()
        pct = _num(r.get("totAcqShare") if kind == "acquisition" else r.get("totSaleShare"))
        if not sym or pct is None:
            continue
        out.append({"date": d, "symbol": sym, "client": " ".join((r.get("acquirerName") or "").split()),
                    "side": "BUY" if kind == "acquisition" else "SELL", "qty": _num(r.get("noOfShareAcq")
                    if kind == "acquisition" else r.get("noOfShareSale")) or 0.0, "price": 0.0, "source": "sast",
                    "pct": pct, "after_pct": _num(r.get("totAftShare")), "promoter": int(r.get("promoterType") == "Y"),
                    "url": r.get("attachement") or ""})
    return out


def refresh_from_nse(today: Optional[date] = None, client: Optional[httpx.Client] = None) -> list[dict]:
    """Latest bulk + block deals and the last 7 days of SAST disclosures. Returns the rows new to the table."""
    today = today or date.fromisoformat(now_ist()[:10])
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": UA, "Accept": "*/*", "Referer": "https://www.nseindia.com/"},
                                    timeout=30, follow_redirects=True)
    rows, errors = [], []
    try:
        for url, source in ((BULK_CSV, "bulk"), (BLOCK_CSV, "block")):
            try:
                resp = client.get(url)
                resp.raise_for_status()
                rows += parse_deals_csv(resp.text, source)
            except httpx.HTTPError as exc:
                errors.append(f"{source}: {exc}")
        try:
            try:
                client.get(NSE_HOME)                # sets cookies; may 403, the API often still answers
            except httpx.HTTPError:
                pass
            fmt = "%d-%m-%Y"
            resp = client.get(SAST_API, params={"index": "equities",
                                                "from_date": (today - timedelta(days=7)).strftime(fmt),
                                                "to_date": today.strftime(fmt)})
            resp.raise_for_status()
            rows += parse_sast(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(f"sast: {exc}")
    finally:
        if own:
            client.close()
    if errors and not rows:
        raise RuntimeError("; ".join(errors))
    if errors:
        logger.warning("Deals partly fetched: %s", "; ".join(errors))
    return store(rows)


def store(rows: list[dict]) -> list[dict]:
    now, new = now_ist(), []
    with get_db() as conn:
        for r in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO deals (date, symbol, client, side, qty, price, source, pct, after_pct, "
                "promoter, url, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["date"], r["symbol"], r["client"], r["side"], r["qty"], r["price"], r["source"], r["pct"],
                 r["after_pct"], r["promoter"], r["url"], now))
            if cur.rowcount:
                new.append(r)
        conn.execute("DELETE FROM deals WHERE date < ?",
                     ((date.fromisoformat(now[:10]) - timedelta(days=KEEP_DAYS)).isoformat(),))
    logger.info("Deals: %d fetched, %d new", len(rows), len(new))
    return new


def last_fetch() -> Optional[str]:
    with get_db() as conn:
        return conn.execute("SELECT MAX(fetched_at) FROM deals").fetchone()[0]


def for_symbol(symbol: str, since: str) -> list[dict]:
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT date, symbol, client, side, qty, price, source, pct, after_pct, promoter, url FROM deals "
            "WHERE symbol = ? AND date >= ? ORDER BY date DESC", (symbol, since))]


def _short(d: str) -> str:
    x = date.fromisoformat(d[:10])
    return f"{x.day} {x.strftime('%b')}"


def _fund_moves(rows: list[dict], min_cr: float) -> list[dict]:
    """Bulk / block deals netted per (date, client): institutions only, prop round-trips dropped."""
    net: dict[tuple, dict] = {}
    for r in rows:
        if r["source"] not in ("bulk", "block") or not institutional(r["client"]):
            continue
        k = (r["date"], r["client"])
        e = net.setdefault(k, {"date": r["date"], "client": r["client"], "buy": 0.0, "sell": 0.0,
                               "source": r["source"]})
        e["buy" if r["side"] == "BUY" else "sell"] += r["qty"] * r["price"] / 1e7          # ₹ crore
    out = []
    for e in net.values():
        value = e["buy"] - e["sell"]
        if abs(value) >= min_cr and abs(value) >= 0.5 * (e["buy"] + e["sell"]):
            out.append({"date": e["date"], "side": "BUY" if value > 0 else "SELL", "client": e["client"],
                        "label": f"{'Fund bought' if value > 0 else 'Fund sold'} ₹{abs(value):,.0f} Cr",
                        "text": f"{e['client']} {'bought' if value > 0 else 'sold'} ₹{abs(value):,.1f} Cr "
                                f"({e['source']} deal, {_short(e['date'])})"})
    return out


def filing_red_label(f: dict) -> Optional[str]:
    """The red-flag label for a filing (legal action not in the company's favour, auditor resignation, rating
    downgrade), else None."""
    c, txt = f["category"].lower(), (f["text"] or "").lower()
    if f["kind"] == "legal" and "in favour" not in txt and "in favor" not in txt:
        return "Regulatory / legal"
    if f["kind"] == "management" and "auditor" in f"{c} {txt}" and "resign" in f"{c} {txt}":
        return "Auditor resigned"
    if f["kind"] == "rating" and "downgrad" in f"{c} {txt}":
        return "Rating downgrade"
    return None


def signals(symbols: list[str], cfg, today: Optional[str] = None) -> dict[str, dict]:
    """{symbol: {red: [...], buy: [...], sell: [...]}} (each item {date, label, text}), only symbols with any."""
    if not symbols or not (cfg.red_flags or cfg.deals):
        return {}
    today = today or now_ist()[:10]
    t = date.fromisoformat(today)
    red_since = (t - timedelta(days=cfg.red_flag_days)).isoformat()
    deal_since = (t - timedelta(days=cfg.deal_days)).isoformat()
    qs = ",".join("?" * len(symbols))
    with get_db() as conn:
        deals = [dict(r) for r in conn.execute(f"SELECT * FROM deals WHERE symbol IN ({qs}) AND date >= ?",
                                               (*symbols, min(red_since, deal_since)))]
        fil = [dict(r) for r in conn.execute(
            f"SELECT symbol, dt, kind, category, text, url FROM filings WHERE symbol IN ({qs}) AND dt >= ? "
            f"AND kind IN ('legal', 'management', 'rating')", (*symbols, red_since))] if cfg.red_flags else []
    out: dict[str, dict] = {}

    def add(sym, key, item):
        out.setdefault(sym, {"red": [], "buy": [], "sell": []})[key].append(item)

    if cfg.red_flags:
        for d in deals:
            if d["source"] == "sast" and d["promoter"] and d["side"] == "SELL" and d["date"] >= red_since \
                    and (d["pct"] or 0) >= cfg.stake_change_min_pct:
                after = f" (now {d['after_pct']:.1f}%)" if d["after_pct"] is not None else ""
                add(d["symbol"], "red", {"date": d["date"], "label": f"Promoter sold {d['pct']:.1f}%",
                                         "text": f"{d['client']} sold {d['pct']:.2f}%{after}, disclosed "
                                                 f"{_short(d['date'])}", "url": d["url"]})
        for f in fil:
            label = filing_red_label(f)
            if label:
                add(f["symbol"], "red", {"date": f["dt"][:10], "label": label, "text": f["text"] or f["category"],
                                         "url": f.get("url") or ""})
    if cfg.deals:
        recent = [d for d in deals if d["date"] >= deal_since]
        by_sym: dict[str, list] = {}
        for d in recent:
            by_sym.setdefault(d["symbol"], []).append(d)
        for sym, rows in by_sym.items():
            for m in _fund_moves(rows, cfg.deal_min_cr):
                add(sym, "buy" if m["side"] == "BUY" else "sell", m)
            for d in rows:
                if d["source"] == "sast" and not d["promoter"] and institutional(d["client"]) \
                        and (d["pct"] or 0) >= cfg.stake_change_min_pct:
                    after = d["after_pct"] if d["after_pct"] is not None else d["pct"]
                    verb = "raised its stake" if d["side"] == "BUY" else "cut its stake"
                    add(sym, "buy" if d["side"] == "BUY" else "sell", {
                        "date": d["date"], "label": f"Fund {'stake up' if d['side'] == 'BUY' else 'stake down'} "
                                                    f"to {after:.1f}%",
                        "text": f"{d['client']} {verb} to {after:.2f}% (disclosed {_short(d['date'])})"})
    for v in out.values():
        for k in v:
            v[k].sort(key=lambda i: i["date"], reverse=True)
    return out


def has_red(symbol: str, cfg) -> bool:
    return bool(cfg.red_flags and signals([symbol], cfg).get(symbol, {}).get("red"))
