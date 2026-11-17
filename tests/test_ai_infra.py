"""🤖 Local AI, PR 1 (infrastructure): the compose service and the config section, the Ollama client against a fake
service (the client's one HTTP function is replaced), the guard's decisions, the queue, the controls (enable refused
without models, the full stop, the downloads, the test prompt), the routes and the stream topic, the Telegram
commands, the health rows, the attention items and the scheduler's tick gauge. No network anywhere."""

import json
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from backend.ai import client, control, guard
from backend.ai import queue as q
from backend.config import IST, AIConfig, AlertsConfig, AppConfig, PERSISTED_SECTIONS
from backend.database.models import get_db, now_ist

ROOT = Path(__file__).resolve().parent.parent
NIGHT, DAY = "qwen2.5:7b-instruct", "qwen2.5:3b-instruct"
BROKERS = ("Zerodha", "Tradetron", "Kotak")


def at(hhmm: str, day=(2026, 11, 17)) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(*day, h, m, tzinfo=IST)


class FakeOllama:
    """The subset of Ollama's REST API the client uses, as `_http(method, path, body, timeout, stream)`."""

    def __init__(self, models=(NIGHT, DAY), loaded=(), reply='{"ok": true, "word": "banana"}', fail=None):
        self.models = list(models)
        self.loaded = list(loaded)
        self.reply, self.fail, self.calls = reply, fail, []
        self.pull_fails = set()

    def __call__(self, method, path, body=None, timeout=5, stream=False):
        self.calls.append((method, path, body))
        if self.fail:
            raise client.AIError(self.fail)
        if path == "/api/version":
            return json.dumps({"version": "0.40.16"})
        if path == "/api/tags":
            return json.dumps({"models": [{"name": m, "size": 4_700_000_000 if "7b" in m else 1_900_000_000} for m in self.models]})
        if path == "/api/ps":
            return json.dumps({"models": [{"name": m, "size": 5_000_000_000, "expires_at": "2026-11-17T10:10:00+05:30"} for m in self.loaded]})
        if path == "/api/pull":
            return self._pull(body["name"])
        if path == "/api/generate":
            if body.get("keep_alive") == 0:
                self.loaded = [m for m in self.loaded if m != body["model"]]
                return json.dumps({"model": body["model"], "done": True})
            if body["model"] not in self.models:
                return json.dumps({"error": f"model '{body['model']}' not found"})
            self.loaded = [body["model"]]
            return json.dumps({"model": body["model"], "response": self.reply, "eval_count": 9, "prompt_eval_count": 31,
                               "total_duration": 850_000_000, "load_duration": 400_000_000, "eval_duration": 300_000_000})
        raise client.AIError(f"HTTP 404 on {path}")

    def _pull(self, name):
        if name in self.pull_fails:
            lines = [json.dumps({"status": "pulling manifest"}), json.dumps({"error": "pull model manifest: file does not exist"})]
        else:
            lines = [json.dumps({"status": "pulling manifest"}), json.dumps({"status": "pulling", "total": 1000, "completed": 250}),
                     json.dumps({"status": "pulling", "total": 1000, "completed": 1000}), "", json.dumps({"status": "success"})]
            self.models.append(name)
        return iter(lines)


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setattr(client, "_http", fake)
    return fake


@pytest.fixture
def cfg():
    c = AppConfig()
    c.ai = AIConfig(enabled=True)
    c.alerts = AlertsConfig(chat_id="42", intraday=False, filings=False)
    return c


@pytest.fixture(autouse=True)
def calm(monkeypatch):
    """A quiet box: plenty of RAM, a quick tick, no breaker, no stop, an empty pull status."""
    monkeypatch.setattr(guard, "_meminfo", lambda: {"ram_total_gb": 23.0, "ram_free_gb": 12.0})
    monkeypatch.setattr(guard, "_load1", lambda: 0.5)
    monkeypatch.setattr(guard, "_cpu_pct", lambda: 12.0)
    guard.record_tick(120)
    control._pull.update(running=False, model=None, pct=0.0, done=[], error=None, at=None)
    q.clear("test setup")


def no_brokers(t: str) -> bool:
    return not any(b in t for b in BROKERS)


# --- compose, config, tables ------------------------------------------------------------------------------------------

def test_compose_service_and_env_line():
    text = (ROOT / "docker-compose.yml").read_text()
    block = text[text.index("  ollama:"):]
    for line in ("image: ollama/ollama:0.40.16", "container_name: bananapatterns-ai", "restart: unless-stopped", "./data/ai:/root/.ollama",
                 "OLLAMA_KEEP_ALIVE: 10m", 'OLLAMA_NUM_PARALLEL: "1"', 'OLLAMA_MAX_LOADED_MODELS: "1"', 'OLLAMA_NUM_THREAD: "3"',
                 'cpus: "2.5"', "mem_limit: 12g", "cpu_shares: 256", 'max-size: "10m"', 'max-file: "3"'):
        assert line in block, line
    assert "ports:" not in block                                                       # nothing published
    assert "OLLAMA_URL: http://ollama:11434" in text[:text.index("  ollama:")]        # the app gets the address
    assert re.search(r"^# OLLAMA_URL=http://ollama:11434", (ROOT / ".env.example").read_text(), re.M)


def test_config_defaults_and_section():
    c = AIConfig()
    assert (c.enabled, c.night_batch, c.day_answers, c.night_model, c.day_model) == (False, True, True, NIGHT, DAY)
    assert (c.timeout_s, c.keep_alive, c.min_free_ram_gb, c.slow_tick_s, c.breaker_failures, c.breaker_minutes, c.night_end, c.max_queue) == \
        (180, "10m", 4.0, 2.0, 3, 60, "06:00", 200)
    assert PERSISTED_SECTIONS["ai"] is AIConfig and AppConfig().section("ai").enabled is False
    cfg = AppConfig()
    out = cfg.update_section("ai", {"timeout_s": "120", "night_batch": "false", "min_free_ram_gb": "6"})
    assert out["timeout_s"] == 120 and cfg.ai.night_batch is False and cfg.ai.min_free_ram_gb == 6.0
    from backend.config import OLLAMA_URL, load_config, save_config
    save_config(cfg)
    assert load_config(AppConfig()).ai.timeout_s == 120 and OLLAMA_URL.startswith("http")


def test_tables_exist_and_are_empty():
    with get_db() as conn:
        cols = lambda t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        assert cols("ai_jobs") == ["id", "created", "started", "finished", "kind", "key", "model", "status", "ms", "tokens", "error", "note"]
        assert cols("ai_notes") == ["id", "created", "day", "kind", "symbol", "book", "severity", "title", "text", "data", "model", "prompt_version", "ms", "source_key"]
        assert conn.execute("SELECT COUNT(*) FROM ai_notes").fetchone()[0] == 0
        with pytest.raises(Exception):                                                 # the status check holds
            conn.execute("INSERT INTO ai_jobs (created, kind, key, status) VALUES ('x', 'k', 'k', 'bogus')")


# --- the client -----------------------------------------------------------------------------------------------------------

def test_client_health_models_ps(ollama):
    assert client.health() == {"ok": True, "version": "0.40.16", "error": None}
    assert client.models() == [{"name": NIGHT, "size_gb": 4.7, "modified": None}, {"name": DAY, "size_gb": 1.9, "modified": None}]
    assert client.ps() == []
    ollama.loaded = [DAY]
    assert client.ps() == [{"name": DAY, "size_gb": 5.0, "ram_mb": 5000, "until": "2026-11-17T10:10:00+05:30"}]
    assert client.url() == "http://ollama:11434"


def test_client_pull_streams_progress(ollama):
    seen = []
    client.pull("qwen2.5:1b", lambda pct, status: seen.append((pct, status)))
    assert seen == [(0.0, "pulling manifest"), (25.0, "pulling"), (100.0, "pulling"), (100.0, "success")] and "qwen2.5:1b" in ollama.models
    ollama.pull_fails.add("nope:1b")
    with pytest.raises(client.AIError, match="manifest"):
        client.pull("nope:1b")


def test_client_generate_and_unload(ollama):
    r = client.generate(DAY, "hi", system="be brief", json_mode=True, timeout=30, keep_alive="5m")
    assert r == {"text": '{"ok": true, "word": "banana"}', "tokens": 9, "prompt_tokens": 31, "ms": 850, "load_ms": 400, "eval_ms": 300, "model": DAY}
    body = ollama.calls[-1][2]
    assert body["format"] == "json" and body["system"] == "be brief" and body["stream"] is False and body["keep_alive"] == "5m"
    assert ollama.loaded == [DAY]
    client.unload(DAY)
    assert ollama.loaded == [] and ollama.calls[-1][2] == {"model": DAY, "keep_alive": 0}
    with pytest.raises(client.AIError, match="not found"):
        client.generate("ghost:1b", "hi")


def test_client_errors_are_ai_errors(monkeypatch):
    assert client.health()["ok"] is False and "unreachable" in client.health()["error"]      # conftest: no service in tests
    with pytest.raises(client.AIError):
        client.models()
    monkeypatch.setattr(client, "_http", lambda *a, **k: "not json")
    with pytest.raises(client.AIError, match="unreadable"):
        client.models()


# --- the guard ---------------------------------------------------------------------------------------------------------------

def test_server_gauge_and_tick(ollama):
    ollama.loaded = [NIGHT]
    s = guard.server()
    assert s == {"ram_total_gb": 23.0, "ram_free_gb": 12.0, "load1": 0.5, "cpu_pct": 12.0, "ai_ram_mb": 5000, "app_tick_ms": 120.0}
    guard.record_tick(2500.4)
    assert guard.tick_ms() == 2500.4 and guard.server()["app_tick_ms"] == 2500.4
    ollama.fail = "down"
    assert guard.server()["ai_ram_mb"] is None


def test_guard_switch_stop_and_models(ollama, cfg):
    assert guard.allowed("day", at("10:00"), cfg) == (True, "ok")
    cfg.ai.enabled = False
    assert guard.allowed("day", at("10:00"), cfg) == (False, "AI switched off")
    cfg.ai.enabled = True
    guard.set_stopped(True)
    assert guard.allowed("day", at("10:00"), cfg) == (False, "full stop") and guard.stopped()
    guard.set_stopped(False)
    ollama.models = [NIGHT]
    assert guard.allowed("day", at("10:00"), cfg) == (False, "models missing")
    ollama.fail = "down"
    assert guard.allowed("day", at("10:00"), cfg) == (False, "service unreachable")
    assert guard.models_present(cfg)["night"]["present"] is False and guard.models_present(cfg)["error"]


def test_guard_free_ram_and_slow_tick(ollama, cfg):
    srv = {"ram_free_gb": 3.2, "app_tick_ms": 100}
    assert guard.allowed("day", at("10:00"), cfg, srv) == (False, "free RAM 3.2 GB under 4 GB")
    srv = {"ram_free_gb": 9.0, "app_tick_ms": 2600}
    assert guard.allowed("day", at("10:00"), cfg, srv) == (False, "app tick 2.6 s over 2 s")
    assert guard.allowed("day", at("10:00"), cfg, {"ram_free_gb": None, "app_tick_ms": None}) == (True, "ok")   # off Linux: no gauge, no block


def test_guard_windows(ollama, cfg):
    assert guard.allowed("day", at("08:59"), cfg) == (False, "outside 9:00 AM-3:40 PM")
    assert guard.allowed("day", at("15:40"), cfg) == (True, "ok")
    cfg.ai.day_answers = False
    assert guard.allowed("day", at("10:00"), cfg) == (False, "day answers off")
    assert guard.allowed("night", at("02:00"), cfg) == (True, "ok")                                 # before night_end
    assert guard.allowed("night", at("21:00"), cfg) == (False, "outside the night window (evening job's end to 6:00 AM)")
    with get_db() as conn:                                                                          # the evening job ended
        conn.execute("INSERT INTO job_runs (job, run_date, started_at, finished_at, status, message) VALUES ('eod', '2026-11-17', ?, ?, 'ok', '')",
                     (now_ist(), now_ist()))
    assert guard.allowed("night", at("21:00"), cfg) == (True, "ok") and guard.allowed("night", at("12:00"), cfg)[0] is False
    cfg.ai.night_batch = False
    assert guard.allowed("night", at("21:00"), cfg) == (False, "night batch off")
    assert guard.allowed("test", at("21:00"), cfg) == (True, "ok")                                  # a test has no window


def test_breaker_opens_after_three_and_closes_on_success(ollama, cfg):
    now = at("10:00")
    assert guard.breaker(now) == {"open": False, "until": None, "failures": 0}
    guard.fail(cfg, now)
    guard.fail(cfg, now)
    assert guard.breaker(now)["open"] is False and guard.breaker(now)["failures"] == 2
    b = guard.fail(cfg, now)
    assert b["open"] and b["until"] == "2026-11-17T11:00:00+05:30" and guard.breaker_text(b) == "open until 11:00 AM, 17 Nov"
    assert guard.allowed("day", now, cfg) == (False, "breaker open until 11:00 AM, 17 Nov")
    assert guard.breaker(at("11:01"))["open"] is False                                            # it times out ...
    guard.ok()                                                                                       # ... or a success closes it
    assert guard.breaker(now) == {"open": False, "until": None, "failures": 0} and guard.allowed("day", now, cfg)[0]


# --- the queue -----------------------------------------------------------------------------------------------------------------

def test_queue_runs_a_job_once_and_dedupes(ollama, cfg):
    ran = []
    fn = lambda: ran.append(1) or client.generate(DAY, "hi")
    a = q.enqueue(cfg, "test", "k1", fn, model=DAY)
    row = q.wait(a["id"])
    assert a["dedup"] is False and row["status"] == "done" and row["tokens"] == 9 and row["ms"] is not None and row["model"] == DAY
    assert row["started"] and row["finished"] and ran == [1]
    b = q.enqueue(cfg, "test", "k2", lambda: q.wait(0, 0.3) and None)                               # a slow one ...
    c = q.enqueue(cfg, "test", "k2", fn)                                                             # ... the same key again: not twice
    assert c == {"id": b["id"], "dedup": True}
    q.wait(b["id"])
    assert ran == [1] and q.recent(5)[0]["id"] == b["id"] and len(q.recent(5)) == 2
    st = q.status("2026-11-17")
    assert st["queued"] == 0 and st["running"] == 0 and st["job"] is None
    assert q.status()["done_today"] == 2 and q.status()["failed_today"] == 0


def test_queue_records_failures_and_the_breaker(ollama, cfg):
    def boom():
        raise client.AIError("timed out")
    for _ in range(3):
        row = q.wait(q.enqueue(cfg, "test", now_ist() + str(_), boom)["id"])
        assert row["status"] == "failed" and row["error"] == "AI: timed out"
    assert guard.breaker()["open"] and q.status()["failed_today"] == 3
    row = q.wait(q.enqueue(cfg, "test", "after", lambda: {"tokens": 1})["id"])
    assert row["status"] == "skipped" and row["error"].startswith("breaker open until")


def test_queue_skips_when_the_guard_says_no_and_refuses_when_full(ollama, cfg):
    cfg.ai.enabled = False
    row = q.wait(q.enqueue(cfg, "day", "x", lambda: {"tokens": 1})["id"])
    assert row["status"] == "skipped" and row["error"] == "AI switched off" and row["ms"] is None
    cfg.ai.enabled = True
    cfg.ai.max_queue = 1
    with get_db() as conn:
        conn.execute("INSERT INTO ai_jobs (created, kind, key, status) VALUES (?, 'night', 'waiting', 'queued')", (now_ist(),))
    with pytest.raises(q.QueueFull, match="queue full"):
        q.enqueue(cfg, "night", "another", lambda: None)
    assert q.clear("full stop") == 1 and q.recent(1)[0]["status"] == "skipped" and q.recent(1)[0]["error"] == "full stop"


# --- the controls ----------------------------------------------------------------------------------------------------------------

def test_enable_refused_without_models(ollama, cfg):
    cfg.ai.enabled = False
    ollama.models = [NIGHT]
    with pytest.raises(ValueError, match="download both models first"):
        control.set_switches(cfg, enabled=True)
    assert cfg.ai.enabled is False and control.can_enable(cfg) is False
    assert control.set_switches(cfg, night_batch=False) == {"enabled": False, "night_batch": False, "day_answers": True, "stopped": False}
    ollama.models = [NIGHT, DAY]
    assert control.set_switches(cfg, enabled=True)["enabled"] is True
    from backend.config import load_config
    assert load_config(AppConfig()).ai.enabled is True and load_config(AppConfig()).ai.night_batch is False


def test_full_stop_unloads_and_clears_then_start(ollama, cfg):
    ollama.loaded = [NIGHT]
    with get_db() as conn:
        conn.execute("INSERT INTO ai_jobs (created, kind, key, status) VALUES (?, 'night', 'w1', 'queued')", (now_ist(),))
        conn.execute("INSERT INTO ai_jobs (created, kind, key, status) VALUES (?, 'night', 'w2', 'queued')", (now_ist(),))
    r = control.full_stop()
    assert r == {"stopped": True, "unloaded": [NIGHT], "skipped": 2, "ram_freed_mb": 5000} and ollama.loaded == []
    assert guard.stopped() and control.switches(cfg)["stopped"] is True
    assert all(j["status"] == "skipped" and j["error"] == "full stop" for j in q.recent(5))
    assert guard.allowed("day", at("10:00"), cfg) == (False, "full stop")
    assert control.start() == {"stopped": False} and guard.allowed("day", at("10:00"), cfg) == (True, "ok")
    ollama.fail = "down"
    assert control.full_stop()["unloaded"] == []                                                      # nothing to free, no error


def test_pull_models_with_progress(ollama, cfg):
    ollama.models = []
    seen = control.pull_models(cfg)
    assert seen["at"] and (seen["running"] is True or seen["done"])                                   # the fake may finish at once
    import time
    for _ in range(200):
        if not control.pull_status()["running"]:
            break
        time.sleep(0.01)
    p = control.pull_status()
    assert p["running"] is False and p["done"] == [NIGHT, DAY] and p["pct"] == 100.0 and p["error"] is None and ollama.models == [NIGHT, DAY]
    assert control.pull_models(cfg)["running"] is False                                              # nothing missing: nothing starts
    ollama.models, ollama.pull_fails = [DAY], {NIGHT}
    control.pull_models(cfg)
    for _ in range(200):
        if not control.pull_status()["running"]:
            break
        time.sleep(0.01)
    assert control.pull_status()["error"].startswith(f"{NIGHT}: pull model manifest")
    ollama.fail = "down"
    with pytest.raises(ValueError, match="service unreachable"):
        control.pull_models(cfg)


def test_test_prompt(ollama, cfg):
    t = control.test_prompt(cfg)
    assert t["ok"] is True and t["text"] == '{"ok": true, "word": "banana"}' and t["status"] == "done" and t["ms"] is not None
    assert control.last_test() == t and ollama.calls[-1][2]["model"] == DAY and ollama.calls[-1][2]["keep_alive"] == "10m"
    assert q.recent(1)[0]["kind"] == "test" and q.recent(1)[0]["note"] == "test prompt"
    cfg.ai.enabled = False
    t = control.test_prompt(cfg)
    assert t["ok"] is False and t["status"] == "skipped" and t["error"] == "AI switched off"


def test_summary_shape(ollama, cfg):
    s = control.summary(cfg, at("10:00"))
    assert set(s) >= {"switches", "server", "models", "breaker", "queue", "jobs", "last_test", "settings", "limits", "url_ok", "version",
                      "url", "notes_count", "now", "can_enable", "enable_note", "allowed", "allowed_note", "never", "container_note",
                      "notes", "testset"}                                     # PR 2: notes, testset; PR 3 adds its own keys below
    assert {"brief", "asks"} <= set(s)                                                                   # 🤖 brief / 🤖 ask
    assert s["switches"] == {"enabled": True, "night_batch": True, "day_answers": True, "stopped": False}
    assert set(s["server"]) == {"ram_total_gb", "ram_free_gb", "load1", "cpu_pct", "ai_ram_mb", "app_tick_ms"}
    assert s["models"]["night"] == {"name": NIGHT, "present": True, "size_gb": 4.7} and s["models"]["day"]["present"] and s["models"]["loaded"] == []
    assert set(s["models"]["pull"]) == {"running", "model", "pct", "done", "error", "at"}
    assert s["breaker"] == {"open": False, "until": None, "failures": 0, "text": "closed"}
    assert set(s["queue"]) == {"queued", "running", "done_today", "failed_today", "job", "tonight", "night_allowed", "night_note"}
    assert s["jobs"] == [] and s["last_test"] is None
    assert s["settings"] == {**AIConfig().__dict__, "enabled": True} and s["limits"] == {"cpus": 2.5, "mem_gb": 12}
    assert s["url_ok"] and s["version"] == "0.40.16" and s["notes_count"] == 0 and s["now"] == "2026-11-17T10:00:00+05:30"
    assert s["can_enable"] and s["enable_note"] is None and s["allowed"] and s["allowed_note"] == "ok" and len(s["never"]) == 5
    assert all(no_brokers(x) for x in s["never"]) and "50 MB" in s["container_note"]
    ollama.fail = "down"
    s = control.summary(cfg, at("10:00"))
    assert s["url_ok"] is False and s["version"] is None and s["can_enable"] is False and s["enable_note"] == "download both models first"
    assert s["models"]["error"] and s["allowed"] is False and s["allowed_note"] == "service unreachable"


# --- the routes and the stream -------------------------------------------------------------------------------------------------------

@pytest.fixture
def api(fake_kite, make_client, monkeypatch, ollama):
    monkeypatch.setenv("BP_NO_STATIC", "1")
    monkeypatch.setenv("BP_NO_SCHEDULER", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("KITE_API_KEY", "key123")
    monkeypatch.setenv("KITE_API_SECRET", "sec")
    from fastapi.testclient import TestClient
    from backend import main
    kc = make_client(fake_kite, history_years=1)
    kc.config.access_token = ""
    monkeypatch.setattr(main, "KiteClient", lambda kc_, ic: kc)
    with TestClient(main.app) as tc:
        yield tc


def test_routes_and_stream(api, ollama):
    tc = api
    d = tc.get("/api/ai").json()
    assert set(d) >= {"switches", "server", "models", "breaker", "queue", "jobs", "last_test", "settings", "limits", "url_ok", "version", "notes_count", "now"}
    assert d["switches"] == {"enabled": False, "night_batch": True, "day_answers": True, "stopped": False} and d["url_ok"] and d["can_enable"]
    # enabling without the models: 400 on both PUTs
    ollama.models = [DAY]
    r = tc.put("/api/ai/settings", json={"enabled": True})
    assert r.status_code == 400 and r.json()["detail"] == "download both models first"
    r = tc.put("/api/config/ai", json={"enabled": True})
    assert r.status_code == 400 and r.json()["detail"] == "download both models first"
    assert tc.get("/api/config/ai").json()["enabled"] is False
    # the downloads, then the switch goes on
    p = tc.post("/api/ai/pull").json()
    assert p["running"] is True or p["done"] == [NIGHT]
    import time
    for _ in range(200):
        if not tc.get("/api/ai").json()["models"]["pull"]["running"]:
            break
        time.sleep(0.01)
    assert tc.get("/api/ai").json()["models"]["night"]["present"] is True
    assert tc.put("/api/ai/settings", json={"enabled": True, "night_batch": False}).json() == {"enabled": True, "night_batch": False, "day_answers": True, "stopped": False}
    assert tc.put("/api/config/ai", json={"timeout_s": 60}).json()["timeout_s"] == 60 and tc.get("/api/ai").json()["settings"]["timeout_s"] == 60
    # the test prompt, the jobs list
    t = tc.post("/api/ai/test").json()
    assert t["ok"] is True and t["text"] == '{"ok": true, "word": "banana"}' and tc.get("/api/ai").json()["last_test"]["ok"] is True
    jobs = tc.get("/api/ai/jobs?limit=5").json()["rows"]
    assert len(jobs) == 1 and jobs[0]["kind"] == "test" and jobs[0]["status"] == "done" and tc.get("/api/ai").json()["jobs"][0]["id"] == jobs[0]["id"]
    # the full stop and the start
    ollama.loaded = [DAY]
    s = tc.post("/api/ai/stop").json()
    assert s["stopped"] is True and s["unloaded"] == [DAY] and tc.get("/api/ai").json()["switches"]["stopped"] is True
    assert tc.post("/api/ai/start").json() == {"stopped": False} and tc.get("/api/ai").json()["switches"]["stopped"] is False
    # the stream topic
    text = tc.get("/api/stream?topics=ai,schedule&once=1").text
    assert "event: ai\n" in text and "event: schedule\n" in text
    ollama.fail = "down"
    assert tc.post("/api/ai/pull").status_code == 502
    h = {c["key"]: c for c in tc.get("/api/health").json()["checks"]}
    assert {"ai_service", "ai_models", "ai_breaker"} <= set(h) and h["ai_service"]["ok"] is False      # on, and the service is gone: red
    assert "ai" in tc.get("/api/attention").json()
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == 1                           # nothing deleted along the way


# --- the Telegram commands --------------------------------------------------------------------------------------------------------------

class St:
    def __init__(self, cfg):
        self.config = cfg
        self.client = type("K", (), {"has_token": False})()
        self.token_source = None

    def token_valid(self):
        return False


def test_ai_commands_and_help(ollama, cfg):
    from backend.alerts.commands import BotCommands
    from backend.alerts.dispatch import Notifier
    from tests.test_login import BotTG, update
    when = at("10:12")
    ollama.loaded = [DAY]
    with get_db() as conn:
        conn.execute("INSERT INTO ai_jobs (created, kind, key, status) VALUES (?, 'night', 'w1', 'queued')", (now_ist(),))
    st, fake = St(cfg), BotTG([update(1, "ai", at=when), update(2, "AI stop", at=when), update(3, "/ai", at=when), update(4, "ai start", at=when),
                              update(5, "/help", at=when), update(6, "ai", chat=999, at=when)])
    b, n = BotCommands(st, clock=lambda: when), Notifier(cfg.alerts, fake)
    assert b.poll_once(n) == 5 and len(fake.sent) == 5
    status, stopped, status2, started, help_ = (h for _, h in fake.sent)
    assert status.startswith("🤖 <b>AI · on</b> · night qwen2.5:7b-instruct ✓ · day qwen2.5:3b-instruct ✓") and "RAM 5.0 GB in use" in status
    assert "queue 1 waiting" in status and "breaker closed" in status and "ai stop" in status and "ai start" in status
    assert stopped.startswith("⏹ <b>AI full stop</b> · 5.0 GB freed · 1 queued job(s) skipped · unloaded qwen2.5:3b-instruct") and "50 MB" in stopped
    assert status2.startswith("🤖 <b>AI · ⏹ full stop</b>") and "nothing loaded" in status2 and "queue 0 waiting" in status2
    assert started.startswith("▶️ AI full stop cleared.\n🤖 <b>AI · on</b>") and guard.stopped() is False
    assert "ai / ai stop / ai start" in help_
    assert all(no_brokers(t) for t in (status, stopped, status2, started))
    cfg.ai.enabled = False
    ollama.fail = "down"
    assert "🤖 <b>AI · off</b>" in b.ai("/ai") and "missing" in b.ai("/ai") and "service unreachable" in b.ai("/ai")


# --- health, attention, the scheduler's tick ---------------------------------------------------------------------------------------------

class _Clock(datetime):
    """guard.breaker() reads the wall clock when no `now` is given: pin it inside the seeded breaker window."""
    @classmethod
    def now(cls, tz=None):
        return at("10:30")


def _pin_clock(monkeypatch):
    monkeypatch.setattr(guard, "datetime", _Clock)


def test_health_rows(ollama, cfg, monkeypatch):
    _pin_clock(monkeypatch)
    from backend.engine import health
    rows = lambda c: {x["key"]: x for x in health.checks(c, has_token=False)}
    cfg.ai.enabled = False
    h = rows(cfg)
    assert h["ai_service"] == {"key": "ai_service", "name": "AI service", "ok": True, "warn": False, "state": "AI off · version 0.40.16",
                               "detail": "the local model next to the app", "at": None}
    assert h["ai_models"]["state"] == "AI off · both present" and h["ai_models"]["ok"] and not h["ai_models"]["warn"]
    assert h["ai_breaker"]["state"] == "AI off · closed" and h["ai_breaker"]["ok"] and "opens at 3" in h["ai_breaker"]["detail"]
    ollama.models = [DAY]
    ollama.fail = None
    h = rows(cfg)
    assert h["ai_models"]["state"] == f"AI off · missing: {NIGHT}" and h["ai_models"]["warn"] is False     # off: a note, not amber
    cfg.ai.enabled = True
    guard.fail(cfg, at("10:00")), guard.fail(cfg, at("10:00")), guard.fail(cfg, at("10:00"))
    h = rows(cfg)
    assert h["ai_models"]["warn"] is True and h["ai_models"]["ok"] is True
    assert h["ai_breaker"]["warn"] is True and h["ai_breaker"]["state"] == "open until 11:00 AM, 17 Nov" and h["ai_breaker"]["at"] == "2026-11-17T11:00:00+05:30"
    ollama.fail = "down"
    h = rows(cfg)
    assert h["ai_service"]["ok"] is False and h["ai_service"]["state"] == "unreachable" and h["ai_service"]["detail"] == "down"
    cfg.ai.enabled = False
    h = rows(cfg)
    assert h["ai_service"]["ok"] is True and h["ai_service"]["warn"] is True and h["ai_service"]["state"] == "AI off · unreachable"


def test_attention_items(ollama, cfg, monkeypatch):
    _pin_clock(monkeypatch)
    from backend.engine import attention
    assert attention.signals(cfg, True)["ai"] == {"level": None, "items": [], "sig": ""}
    assert attention._ai(cfg) == []
    for _ in range(3):
        guard.fail(cfg, at("10:00"))
    items = attention._ai(cfg)
    assert items == [("warn", "AI breaker open until 11:00 AM, 17 Nov: jobs are skipped until then")]
    control._pull.update(error=f"{NIGHT}: pull model manifest: file does not exist")
    items = attention._ai(cfg)
    assert [lvl for lvl, _ in items] == ["warn", "warn"] and items[1][1].startswith("AI model download failed: qwen2.5:7b-instruct")
    guard.ok()
    sig = attention.signals(cfg, True)["ai"]
    assert sig["level"] == "warn" and len(sig["items"]) == 1 and sig["sig"]


def test_scheduler_tick_records_its_duration(monkeypatch, cfg):
    from backend.alerts import jobs
    guard.record_tick(0)
    sch = jobs.Scheduler(St(cfg), notifier_factory=lambda c: type("N", (), {"send": lambda *a, **k: False, "system": lambda *a, **k: None, "hold": None})())
    monkeypatch.setattr(jobs, "due_jobs", lambda *a, **k: [])
    assert sch.tick(at("12:00")) == []
    ms = guard.tick_ms()
    assert ms is not None and 0 <= ms < 5000 and guard.server()["app_tick_ms"] == ms
    monkeypatch.setattr(jobs, "due_jobs", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    guard.record_tick(0)
    with pytest.raises(RuntimeError):                                                   # a failing tick still records its time
        sch.tick(at("12:01"))
    assert guard.tick_ms() is not None and guard.tick_ms() >= 0


def test_no_broker_names_in_the_module_texts():
    for p in (ROOT / "backend" / "ai").glob("*.py"):
        assert no_brokers(p.read_text()), p
    assert no_brokers(" ".join(control.NEVER)) and all(not re.search(r"\b\d{2}:\d{2}\b", x) for x in control.NEVER)
    cfg = AppConfig()
    cfg.ai = replace(cfg.ai, enabled=True, night_end="18:00")
    srv, models = {"ram_free_gb": 10, "app_tick_ms": 1}, {"night": {"present": True}, "day": {"present": True}, "error": None}
    assert guard.allowed("night", at("17:00"), cfg, srv, models) == (True, "ok")                       # before night_end
    assert guard.allowed("night", at("19:00"), cfg, srv, models)[1].endswith("to 6:00 PM)")           # 12-hour times in every text


def test_warm_loads_the_model_and_timeouts_get_one_retry(ollama, cfg):
    """The night batch warms the model once (an empty-prompt generate) and a source that failed once on a timeout is
    offered again; a second failure or a non-timeout failure is not."""
    from backend.ai import client, notes
    r = client.warm("qwen2.5:7b-instruct")
    assert r["loaded"] is True and ollama.calls[-1][1] == "/api/generate" and ollama.calls[-1][2]["prompt"] == ""
    with get_db() as conn:
        for key, err in (("note:filing:1", "no answer from /api/generate within 180 s"), ("note:filing:2", "NoteInvalid: x"),
                         ("note:filing:3", "no answer from /api/generate within 180 s"), ("note:filing:3", "no answer from /api/generate within 180 s")):
            conn.execute("INSERT INTO ai_jobs (created, kind, key, status, error) VALUES (?, 'night', ?, 'failed', ?)", (now_ist(), key, err))
    failed = notes._failed_keys("2000-01-01")
    assert "note:filing:1" not in failed and "note:filing:2" in failed and "note:filing:3" in failed
    assert cfg.ai.timeout_s == 180


def test_slow_generation_is_reported_as_a_timeout_not_a_dead_service(monkeypatch):
    """urlopen raises URLError("timed out") when Ollama is still generating when the timeout passes: that is a slow
    answer, reported as "no answer … within", so the one-retry rule for timeouts applies."""
    import importlib.util
    import urllib.error
    from backend.ai import client
    spec = importlib.util.spec_from_file_location("ai_client_real", client.__file__)   # conftest replaces _http: load a real copy
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)

    def boom(*a, **k):
        raise urllib.error.URLError("timed out")
    monkeypatch.setattr(real.urllib.request, "urlopen", boom)
    with pytest.raises(real.AIError, match="no answer from /api/generate within"):
        real.generate("m", "p", timeout=7)
