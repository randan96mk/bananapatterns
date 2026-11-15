"""The controls the page and the Telegram commands use: `switches` / `set_switches` (enabled may only turn on once
both models are present), `full_stop` (unload every model, empty the queue: the RAM and CPU are free at once; the
container itself keeps idling at ~50 MB because the app has no docker socket), `start`, `pull_models` (both
downloads in one background thread, progress in `pull_status`), `test_prompt` (a tiny JSON prompt on the day model
through the queue) and `summary` (everything GET /api/ai returns)."""

import json
import threading
from dataclasses import asdict
from datetime import datetime
from typing import Optional

from backend.ai import client, guard
from backend.ai import queue as q
from backend.config import IST, AppConfig
from backend.database.models import get_db, get_setting, now_ist, set_setting
from backend.logging_config.logger import setup_logger

logger = setup_logger("ai")

LIMITS = {"cpus": 2.5, "mem_gb": 12}                    # the compose service's limits (docker-compose.yml)
NEEDS_MODELS = "download both models first"
TEST_PROMPT = 'Reply with exactly this JSON and nothing else: {"ok": true, "word": "banana"}'
NEVER = [
    "It never places, changes or cancels an order, in any book, paper or real.",
    "It never changes a setting, a switch, a stop or a capital figure.",
    "It never sends anything off this server: the model runs in the container next to the app.",
    "It never deletes data: every job and every note stays in the database.",
    "It never runs while the app is busy, the RAM is short or the breaker is open.",
]

_lock = threading.Lock()
_pull: dict = {"running": False, "model": None, "pct": 0.0, "done": [], "error": None, "at": None}


# --- switches ------------------------------------------------------------------------------------------------------------

def switches(config: AppConfig) -> dict:
    c = config.ai
    return {"enabled": bool(c.enabled), "night_batch": bool(c.night_batch), "day_answers": bool(c.day_answers),
            "stopped": guard.stopped()}


def can_enable(config: AppConfig, models: Optional[dict] = None) -> bool:
    """The enable switch may go on only when both models are on disk."""
    m = models or guard.models_present(config)
    return bool(m["night"]["present"] and m["day"]["present"])


def set_switches(config: AppConfig, **flags) -> dict:
    """enabled / night_batch / day_answers; enabled=True without both models -> ValueError. Saved with the settings."""
    from backend.config import save_config
    if flags.get("enabled") and not config.ai.enabled and not can_enable(config):
        raise ValueError(NEEDS_MODELS)
    for k in ("enabled", "night_batch", "day_answers"):
        if flags.get(k) is not None:
            setattr(config.ai, k, bool(flags[k]))
    save_config(config)
    return switches(config)


# --- the full stop --------------------------------------------------------------------------------------------------------

def _unload_all() -> list[str]:
    """Unload every loaded model; the names freed (an unreachable service frees nothing)."""
    try:
        loaded = client.ps()
    except client.AIError:
        return []
    out = []
    for m in loaded:
        try:
            client.unload(m["name"])
            out.append(m["name"])
        except client.AIError as exc:
            logger.warning("Unload of %s failed: %s", m["name"], exc)
    return out


def full_stop() -> dict:
    """ai.stopped=1, every model unloaded, every queued job skipped: {stopped, unloaded, skipped, ram_freed_mb}."""
    before = guard.server().get("ai_ram_mb") or 0
    guard.set_stopped(True)
    skipped = q.clear("full stop")
    unloaded = _unload_all()
    after = guard.server().get("ai_ram_mb") or 0
    logger.info("AI full stop: %d job(s) skipped, unloaded %s", skipped, unloaded or "nothing")
    return {"stopped": True, "unloaded": unloaded, "skipped": skipped, "ram_freed_mb": max(0, before - after)}


def start() -> dict:
    """Clear the full stop (the switches decide what runs from here)."""
    guard.set_stopped(False)
    return {"stopped": False}


# --- the model downloads ----------------------------------------------------------------------------------------------------

def pull_status() -> dict:
    """{running, model, pct, done, error, at}: the background download's progress."""
    return dict(_pull)


def _pull_worker(names: list[str]) -> None:
    for name in names:
        _pull.update(model=name, pct=0.0)
        try:
            client.pull(name, lambda pct, status: _pull.update(pct=pct))
        except client.AIError as exc:
            _pull.update(running=False, error=f"{name}: {exc}", at=now_ist())
            logger.warning("AI model pull %s failed: %s", name, exc)
            return
        _pull["done"] = [*_pull["done"], name]
    _pull.update(running=False, model=None, pct=100.0, at=now_ist())


def pull_models(config: AppConfig) -> dict:
    """Download the missing models (night, then day) in a background thread; the status at once."""
    with _lock:
        if _pull["running"]:
            return pull_status()
        m = guard.models_present(config)
        if m.get("error"):
            raise ValueError(f"service unreachable: {m['error']}")
        names = [m[k]["name"] for k in ("night", "day") if not m[k]["present"]]
        _pull.update(running=bool(names), model=None, pct=0.0, done=[], error=None, at=now_ist())
        if names:
            threading.Thread(target=_pull_worker, args=(names,), name="ai-pull", daemon=True).start()
    return pull_status()


# --- the test prompt ----------------------------------------------------------------------------------------------------------

def last_test() -> Optional[dict]:
    with get_db() as conn:
        raw = get_setting(conn, "ai.last_test")
    return json.loads(raw) if raw else None


def _save_test(t: dict) -> dict:
    with get_db() as conn:
        set_setting(conn, "ai.last_test", json.dumps(t))
    return t


def _looks_ok(text: str) -> bool:
    try:
        return json.loads(text).get("ok") is True
    except (ValueError, AttributeError):
        return False


def test_prompt(config: AppConfig, timeout_s: float = 120) -> dict:
    """The day model answers a tiny JSON prompt through the queue: {at, ms, text, ok, status, error}, kept as the
    page's "last test"."""
    c = config.ai
    out = {"text": None, "tokens": 0}

    def work():
        r = client.generate(c.day_model, TEST_PROMPT, json_mode=True, timeout=float(c.timeout_s), keep_alive=c.keep_alive)
        out.update(r)
        return r

    job = q.enqueue(config, "test", now_ist(), work, model=c.day_model, note="test prompt")
    row = q.wait(job["id"], timeout_s) or {}
    text = (out.get("text") or "").strip()
    return _save_test({"at": now_ist(), "ms": row.get("ms"), "text": text[:300], "ok": row.get("status") == "done" and _looks_ok(text),
                       "status": row.get("status"), "error": row.get("error"), "model": c.day_model})


# --- the page ------------------------------------------------------------------------------------------------------------------

def _notes_count() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM ai_notes").fetchone()[0]


def _loaded() -> list[dict]:
    try:
        return client.ps()
    except client.AIError:
        return []


def _brief_status() -> dict:
    """🤖 brief: {last_at, lines, day, id} of the newest morning brief (never raises: the page comes first)."""
    try:
        from backend.ai import brief
        return brief.status()
    except Exception:
        return {"last_at": None, "lines": [], "day": None, "id": None}


def _asks_stats(now: datetime) -> dict:
    """🤖 ask: {today, last} (never raises)."""
    try:
        from backend.ai import ask
        return ask.stats(now)
    except Exception:
        return {"today": 0, "last": None}


def _view_summary(config: AppConfig) -> Optional[dict]:
    """🤖 AI view (never raises)."""
    try:
        from backend.ai import view
        return view.summary(config)
    except Exception:
        return None


def summary(config: AppConfig, now: Optional[datetime] = None) -> dict:
    """Everything the AI page needs: switches, server, models, breaker, queue (with tonight's note rows and the night
    verdict), jobs, last_test, notes (today / high / last night), testset (last / best / running), settings, limits,
    url_ok, version, notes_count, now, never, and whether the enable switch may go on (and why not)."""
    from backend.ai import notes, quality
    from backend.ai import suggest                                         # 🤖 suggestions
    now = now or datetime.now(IST)
    h = client.health()
    models = guard.models_present(config)
    srv = guard.server()
    b = guard.breaker(now)
    ok_now, why = guard.allowed("day" if guard.day_window(now) else "night", now, config, srv, models)
    both = can_enable(config, models)
    night_ok, night_why = guard.allowed("night", now, config, srv, models)
    queue = q.status(now.date().isoformat())
    queue["tonight"] = notes.tonight(config, now)                          # 🤖 PR 2: the Queue · tonight rows
    queue["night_allowed"], queue["night_note"] = night_ok, night_why
    return {"switches": switches(config), "server": srv,
            "models": {"night": models["night"], "day": models["day"], "loaded": _loaded(), "pull": pull_status(), "error": models["error"]},
            "breaker": {**b, "text": guard.breaker_text(b)},
            "queue": queue, "jobs": q.recent(20), "last_test": last_test(),
            "notes": notes.stats(now, config), "testset": quality.status(),
            "suggestions": suggest.summary(config),                        # 🤖 suggestions: {open, by_kind, scorecard_month, muted, max_open}
            "brief": _brief_status(), "asks": _asks_stats(now),                   # 🤖 brief / 🤖 ask
            "view": _view_summary(config),                                    # 🤖 AI view: {on, last_night, record_summary, switches}
            "settings": asdict(config.ai), "limits": LIMITS, "url_ok": h["ok"], "version": h["version"], "url": client.url(),
            "notes_count": _notes_count(), "now": now.isoformat(timespec="seconds"),
            "can_enable": both, "enable_note": None if both else NEEDS_MODELS, "allowed": ok_now, "allowed_note": why,
            "never": NEVER, "container_note": "the container itself keeps idling at ~50 MB (the app has no docker socket)"}
