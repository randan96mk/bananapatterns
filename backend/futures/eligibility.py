"""Eligibility of one signal for the futures book: every rule in the plan, each failure a short reason string the
page shows next to the rejected signal ("lot too big ₹9.0 L > ₹6 L", "risk 6.4% > 6%", "slots full", "in ban",
"results Thu", "held in cash", ...). Nothing here reads the broker: the held sets and the open lots come from the
app's own books."""

from datetime import date
from typing import Optional

from backend.config import AppConfig, FuturesConfig
from backend.database.models import now_ist
from backend.futures import ban, instruments


def lakh(x: float, cap: bool = False) -> str:
    """₹ in lakhs, short. A measured value keeps one decimal (904500 -> '₹9.0 L'); a configured cap is compact
    (600000 -> '₹6 L', 450000 -> '₹4.5 L')."""
    v = x / 1e5
    return f"₹{round(v, 2):g} L" if cap else f"₹{v:.1f} L"


def pct(x: float) -> str:
    return f"{x:g}%" if float(x).is_integer() else f"{x:.1f}%"


def margin_estimate(cfg: FuturesConfig, lot_value: float) -> float:
    """The margin an exchange would roughly ask for one lot: margin_pct of the lot value (an estimate, configurable)."""
    return round(lot_value * cfg.margin_pct / 100, 2)


def results_within(symbol: str, sessions: int, today: Optional[str] = None) -> Optional[dict]:
    """The stock's next results {date, sessions} when within `sessions` trading sessions, from the stored calendar."""
    if sessions <= 0:
        return None
    from backend.ingest import events
    r = events.next_results(symbol, today or now_ist()[:10])
    return r if r and r["sessions"] <= sessions else None


def check(config, symbol: str, side: str, entry: float, stop: float, held_cash: set, held_fut: set,
          open_lots: list, today: Optional[str] = None, cash: Optional[float] = None,
          contract: Optional[dict] = None) -> dict:
    """Can this signal take one lot? config: AppConfig or FuturesConfig. entry / stop are futures prices; open_lots are
    the book's open positions [{symbol, lot_value, margin, risk}]; cash overrides capital − margin used as free cash.
    Returns {ok, reasons, lot_size, lot_value, risk, risk_pct, margin_est, contract}."""
    cfg: FuturesConfig = config.futures if isinstance(config, AppConfig) else config
    symbol = symbol.upper()
    today = today or now_ist()[:10]
    reasons: list[str] = []
    contract = contract if contract is not None else instruments.near(symbol)
    out = {"ok": False, "reasons": reasons, "lot_size": None, "lot_value": None, "risk": None, "risk_pct": None,
           "margin_est": None, "contract": contract}
    if not contract:
        reasons.append("no future")
        return out
    lot = int(contract["lot_size"] or 0)
    lot_value = round(entry * lot, 2)
    risk = round(abs(entry - stop) * lot, 2)
    risk_pct = round(risk / cfg.capital * 100, 2) if cfg.capital else 0.0
    margin = margin_estimate(cfg, lot_value)
    out.update({"lot_size": lot, "lot_value": lot_value, "risk": risk, "risk_pct": risk_pct, "margin_est": margin})

    if lot_value > cfg.max_lot_value:
        reasons.append(f"lot too big {lakh(lot_value)} > {lakh(cfg.max_lot_value, True)}")
    if risk_pct > cfg.max_risk_pct:
        reasons.append(f"risk {pct(risk_pct)} > {pct(cfg.max_risk_pct)}")
    rank = contract.get("oi_rank")
    if rank is not None and cfg.top_oi and rank > cfg.top_oi:
        reasons.append(f"OI rank {rank} > {cfg.top_oi}")
    if ban.banned(symbol, today):
        reasons.append("in ban")
    r = results_within(symbol, cfg.results_guard_sessions, today)
    if r:
        reasons.append(f"results {date.fromisoformat(r['date']).strftime('%a')}")
    if symbol in {s.upper() for s in held_cash}:
        reasons.append("held in cash")
    if symbol in {s.upper() for s in held_fut} or any((x.get("symbol") or "").upper() == symbol for x in open_lots):
        reasons.append("held in futures")

    n_open = len(open_lots)
    if n_open >= cfg.max_pos:
        small = all((x.get("lot_value") or 0) < cfg.small_lot_value for x in open_lots) and lot_value < cfg.small_lot_value
        if not (n_open == cfg.max_pos and small):
            reasons.append("slots full")
    notional = sum(x.get("lot_value") or 0 for x in open_lots) + lot_value
    cap_notional = cfg.max_notional_x * cfg.capital
    if notional > cap_notional:
        reasons.append(f"notional {lakh(notional)} > {lakh(cap_notional, True)}")
    margin_used = sum(x.get("margin") or 0 for x in open_lots)
    free = (cash if cash is not None else cfg.capital - margin_used) - margin
    if free < cfg.cash_floor:
        reasons.append(f"cash {lakh(max(free, 0))} < {lakh(cfg.cash_floor, True)}")
    if cfg.capital and (margin_used + margin) / cfg.capital * 100 >= 80:
        reasons.append(f"margin {pct(round((margin_used + margin) / cfg.capital * 100, 1))} ≥ 80%")
    heat = (sum(x.get("risk") or 0 for x in open_lots) + risk) / cfg.capital * 100 if cfg.capital else 0.0
    if heat > cfg.heat_pct:
        reasons.append(f"heat {pct(round(heat, 1))} > {pct(cfg.heat_pct)}")
    out["ok"] = not reasons
    return out
