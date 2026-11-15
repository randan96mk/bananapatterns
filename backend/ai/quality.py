"""🤖 The quality test set: 30 hand-written filings in the style of NSE texts (config/ai_testset.json), each with the
category, severity and direction a careful reader expects. `run_testset(config)` sends them through the queue as kind
"test" (allowed outside the night window; the NIGHT model, so the score reflects the night work), scores how often the
model's category / severity / direction match, and keeps the result in app_settings `ai.testset.last` with the best
run so far in `ai.testset.best`. The page shows last vs best. `start(config)` runs it in a background thread (30
prompts take a few minutes on the box); `status()` is what GET /api/ai returns."""

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from backend.ai import notes
from backend.ai import queue as q
from backend.config import IST, AppConfig
from backend.database.models import get_db, get_setting, now_ist, set_setting
from backend.logging_config.logger import setup_logger

logger = setup_logger("ai")

TESTSET = Path(__file__).resolve().parents[2] / "config" / "ai_testset.json"
FIELDS = ("category", "severity", "direction")

_lock = threading.Lock()
_run: dict = {"running": False, "done": 0, "n": 0, "at": None, "error": None}


class TestsetRunning(RuntimeError):
    """A run is already going."""


def load(path: Optional[Path] = None) -> list[dict]:
    """The items: [{id, symbol, category (the exchange's), kind, text, scope, expected: {category, severity, direction}}]."""
    return json.loads((path or TESTSET).read_text())["items"]


def _source(item: dict) -> dict:
    """A test item as a filing source for the prompt builder (no row is written for it)."""
    f = {"seq_id": f"test-{item['id']}", "symbol": item["symbol"], "dt": item.get("dt") or "2026-11-14T18:30:00",
         "category": item["category"], "text": item["text"], "url": "", "kind": item.get("kind") or "update"}
    book = item.get("scope") or "held"
    return {"kind": "filing", "key": f["seq_id"], "filing": f, "scope": {"book": book, "why": notes.BOOK_WHY[book], "rank": None}}


def score_item(item: dict, note: Optional[dict]) -> dict:
    """{id, symbol, expected, got, ok: {category, severity, direction}}; a missing note matches nothing."""
    got = {"category": note["category"], "severity": note["severity_model"], "direction": note["direction"]} if note else \
        {k: None for k in FIELDS}
    return {"id": item["id"], "symbol": item["symbol"], "expected": item["expected"], "got": got,
            "ok": {k: got[k] == item["expected"][k] for k in FIELDS}}


def score(rows: list[dict]) -> dict:
    """{n, category_ok, severity_ok, direction_ok}: how many of the rows match on each field."""
    return {"n": len(rows), **{f"{k}_ok": sum(1 for r in rows if r["ok"][k]) for k in FIELDS}}


def _one(item: dict, config: AppConfig, timeout_s: float) -> tuple[Optional[dict], dict]:
    """One item through the queue (kind "test"): (the validated note or None, the job row)."""
    src = _source(item)
    out: dict = {}

    def work():
        system, user, numbers = notes.build_prompt(src)
        note, r = notes.ask(config, system, user, item["text"], numbers)
        out.update(note=note, ms=r["ms"])
        return {"tokens": r["tokens"], "note": f"test {item['id']} {item['symbol']}"}

    job = q.enqueue(config, "test", f"testset:{item['id']}:{now_ist()}", work, model=config.ai.night_model, note=f"test set {item['id']}")
    row = dict(q.wait(job["id"], timeout_s) or {})
    if out.get("ms") is not None:
        row["ms"] = out["ms"]                                # the model's own time (the row's is the wall time)
    return out.get("note"), row


def run_testset(config: AppConfig, items: Optional[list[dict]] = None, timeout_s: float = 180, claimed: bool = False) -> dict:
    """Every item through the night model, scored and saved: {at, model, prompt_version, n, category_ok, severity_ok,
    direction_ok, ms_avg, failed, rows}. Raises TestsetRunning when a run is going already (`claimed`: `start` took
    the running flag for this call)."""
    with _lock:
        if _run["running"] and not claimed:
            raise TestsetRunning("the test set is running")
        items = items if items is not None else load()
        _run.update(running=True, done=0, n=len(items), at=now_ist(), error=None)
    rows, ms, failed = [], [], 0
    try:
        for item in items:
            note, job = _one(item, config, timeout_s)
            if job.get("status") != "done":
                failed += 1
            if job.get("ms") is not None:
                ms.append(job["ms"])
            rows.append(dict(score_item(item, note), ms=job.get("ms"), status=job.get("status"), error=job.get("error")))
            _run["done"] = len(rows)
        res = {"at": now_ist(), "model": config.ai.night_model, "prompt_version": notes.PROMPT_V, **score(rows),
               "ms_avg": round(sum(ms) / len(ms)) if ms else None, "failed": failed, "rows": rows}
        save(res)
        return res
    finally:
        _run.update(running=False)


def _points(r: dict) -> int:
    return int(r.get("category_ok") or 0) + int(r.get("severity_ok") or 0) + int(r.get("direction_ok") or 0)


def save(res: dict) -> None:
    """`ai.testset.last` = this run; `ai.testset.best` = the better of the previous best and this one (fewer failures
    break a tie)."""
    with get_db() as conn:
        set_setting(conn, "ai.testset.last", json.dumps(res))
        raw = get_setting(conn, "ai.testset.best")
        best = json.loads(raw) if raw else None
        if not best or (_points(res), -res.get("failed", 0)) >= (_points(best), -best.get("failed", 0)):
            set_setting(conn, "ai.testset.best", json.dumps(res))


def _setting(key: str) -> Optional[dict]:
    with get_db() as conn:
        raw = get_setting(conn, key)
    return json.loads(raw) if raw else None


def status() -> dict:
    """{last, best, running, progress: {done, n, at, error}}: what the Quality · test set panel shows."""
    return {"last": _setting("ai.testset.last"), "best": _setting("ai.testset.best"), "running": bool(_run["running"]),
            "progress": {k: _run[k] for k in ("done", "n", "at", "error")}}


def start(config: AppConfig, now: Optional[datetime] = None) -> dict:
    """Run the test set in a background thread; the status at once. Raises TestsetRunning."""
    with _lock:
        if _run["running"]:
            raise TestsetRunning("the test set is running")

    def go():
        try:
            run_testset(config, claimed=True)
        except Exception as exc:
            _run.update(error=str(exc)[:200])
            logger.exception("AI test set failed")

    _run.update(running=True, done=0, n=0, at=(now or datetime.now(IST)).isoformat(timespec="seconds"), error=None)
    threading.Thread(target=go, name="ai-testset", daemon=True).start()
    return status()
