"""Nifty 500 universe: NSE constituents CSV + Kite instruments mapping.

The NSE CSV (Company Name, Industry, Symbol, Series, ISIN Code) is stored as a
dated snapshot. Kite's instruments dump has no ISIN, so the join to
instrument_token is on tradingsymbol; unmatched symbols are reported.
If NSE blocks the download, drop the CSV at config/ind_nifty500list.csv.
"""

import csv
import io
from datetime import datetime
from typing import Optional

import httpx

from backend.config import CONFIG_DIR, IST, IngestConfig
from backend.database.models import (find_instrument, get_db, latest_universe, replace_instruments,
                                     save_universe_snapshot)
from backend.logging_config.logger import setup_logger

logger = setup_logger("universe")

INDEX_NAME = "NIFTY 500"
FALLBACK_CSV = CONFIG_DIR / "ind_nifty500list.csv"
REQUIRED_COLUMNS = {"Company Name", "Industry", "Symbol", "Series", "ISIN Code"}
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
           "Accept": "text/csv,*/*"}


class UniverseError(Exception):
    pass


def parse_constituents_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    cols = {c.strip() for c in (reader.fieldnames or [])}
    missing = REQUIRED_COLUMNS - cols
    if missing:
        raise UniverseError(f"Constituents CSV missing columns {sorted(missing)}; got {sorted(cols)}")
    members = []
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if not row.get("Symbol"):
            continue
        members.append({"symbol": row["Symbol"], "company": row["Company Name"], "industry": row["Industry"],
                        "series": row["Series"], "isin": row["ISIN Code"]})
    if not members:
        raise UniverseError("Constituents CSV has no rows")
    return members


def fetch_constituents(cfg: IngestConfig, http_get=None) -> tuple[list[dict], str]:
    """Download from the configured URLs, else use the local fallback CSV. Returns (members, source)."""
    http_get = http_get or (lambda url: httpx.get(url, headers=HEADERS, timeout=30, follow_redirects=True))
    errors = []
    for url in cfg.universe_urls:
        try:
            resp = http_get(url)
            resp.raise_for_status()
            return parse_constituents_csv(resp.text), url
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            logger.warning("Universe download failed from %s: %s", url, exc)
    if FALLBACK_CSV.exists():
        logger.warning("Using local fallback %s", FALLBACK_CSV)
        return parse_constituents_csv(FALLBACK_CSV.read_text(encoding="utf-8")), str(FALLBACK_CSV)
    raise UniverseError("Could not get Nifty 500 list. " + " | ".join(errors) +
                        f" | Fallback: save the CSV to {FALLBACK_CSV}")


def filter_instruments(rows: list[dict]) -> list[dict]:
    """Keep NSE cash equities (EQ) and NSE indices from Kite's NSE dump."""
    keep = []
    for r in rows:
        seg = r.get("segment")
        if seg == "INDICES" or (seg == "NSE" and r.get("instrument_type") == "EQ"):
            keep.append(r)
    return keep


def refresh_instruments(client) -> int:
    rows = filter_instruments(client.instruments("NSE"))
    if not rows:
        raise UniverseError("Kite returned no NSE instruments")
    with get_db() as conn:
        n = replace_instruments(conn, rows)
    logger.info("Stored %d NSE EQ/index instruments", n)
    return n


def refresh_universe(client, cfg: IngestConfig, http_get=None) -> dict:
    """Download constituents, snapshot them, refresh instruments, report mapping."""
    members, source = fetch_constituents(cfg, http_get)
    snapshot_date = datetime.now(IST).date().isoformat()
    refresh_instruments(client)
    with get_db() as conn:
        save_universe_snapshot(conn, snapshot_date, INDEX_NAME, members)
    resolved = resolve_universe(cfg)
    summary = {
        "snapshot_date": snapshot_date, "source": source, "members": len(members),
        "matched": len(resolved["stocks"]), "unmatched": resolved["unmatched"],
        "excluded": resolved["excluded"],
        "indices": {i["symbol"]: i["instrument_token"] for i in resolved["indices"]},
        "missing_indices": resolved["missing_indices"],
    }
    logger.info("Universe %s: %d members, %d mapped to Kite, %d unmatched %s, %d excluded by series %s",
                snapshot_date, len(members), summary["matched"], len(summary["unmatched"]),
                summary["unmatched"][:10], len(summary["excluded"]), summary["excluded"])
    return summary


def _kite_symbol_candidates(member: dict) -> list[str]:
    """NSE symbol first; non-EQ series are listed on Kite as SYMBOL-SERIES (e.g. HFCL-BE)."""
    sym, series = member["symbol"], (member.get("series") or "EQ").upper()
    return [sym] if series == "EQ" else [f"{sym}-{series}", sym]


def resolve_universe(cfg: IngestConfig, index_name: str = INDEX_NAME) -> dict:
    """Latest snapshot joined to Kite tokens: {snapshot_date, stocks, indices, unmatched, missing_indices}."""
    with get_db() as conn:
        snapshot_date, members = latest_universe(conn, index_name)
        stocks, unmatched, excluded = [], [], []
        series_ok = {s.upper() for s in cfg.include_series}
        for m in members:
            if m["symbol"].upper().startswith("DUMMY") or (m.get("series") or "EQ").upper() not in series_ok:
                # DUMMY*: NSE placeholder rows in index files, not stocks
                excluded.append(m["symbol"])
                continue
            inst = None
            for candidate in _kite_symbol_candidates(m):
                inst = find_instrument(conn, candidate, "NSE")
                if inst:
                    break
            if inst:
                stocks.append({**m, "instrument_token": inst["instrument_token"],
                               "tradingsymbol": inst["tradingsymbol"]})
            else:
                unmatched.append(m["symbol"])
        indices, missing = [], []
        for sym in cfg.index_symbols:
            inst = find_instrument(conn, sym, "INDICES")
            if inst:
                indices.append({"symbol": sym, "instrument_token": inst["instrument_token"]})
            else:
                missing.append(sym)
    return {"snapshot_date": snapshot_date, "stocks": stocks, "indices": indices,
            "unmatched": unmatched, "excluded": excluded, "missing_indices": missing}


def universe_ready(cfg: IngestConfig) -> Optional[str]:
    """None if a snapshot and instruments exist, else a reason string."""
    with get_db() as conn:
        snap, members = latest_universe(conn, INDEX_NAME)
        n_inst = conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
    if not members:
        return "No universe snapshot yet: run universe refresh."
    if not n_inst:
        return "No Kite instruments stored yet: run universe refresh."
    return None
