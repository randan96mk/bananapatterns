"""The Ollama REST client (urllib, like the webhook senders): `health`, `models` (/api/tags), `ps` (/api/ps),
`pull` (streaming /api/pull), `generate` (/api/generate, non-streaming) and `unload` (keep_alive 0). Every problem
is raised as `AIError`; nothing here retries on its own. `_http` is the one place a request is made (tests replace
it with a fake Ollama)."""

import json
import socket
import urllib.error
import urllib.request
from typing import Callable, Iterator, Optional, Union

from backend import config as cfg

CONNECT_TIMEOUT_S = 5            # health / tags / ps: the service answers at once or it is down
PULL_TIMEOUT_S = 600             # one quiet stretch of a download (the whole pull may take much longer)
USER_AGENT = "bananapatterns-ai/1.0"


class AIError(Exception):
    """The service is unreachable, answered with an error or sent something unreadable."""


def url() -> str:
    """The service's base URL (OLLAMA_URL in .env; http://ollama:11434 under docker compose)."""
    return cfg.OLLAMA_URL.rstrip("/")


def _http(method: str, path: str, body: Optional[dict] = None, timeout: float = CONNECT_TIMEOUT_S,
          stream: bool = False) -> Union[str, Iterator[str]]:
    """One request. Non-streaming: the response text. Streaming: an iterator of the response's lines."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url() + path, data, {"Content-Type": "application/json", "User-Agent": USER_AGENT},
                                 method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise AIError(f"HTTP {exc.code} on {path}: {exc.read()[:200].decode(errors='replace')}") from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        raise AIError(f"service unreachable at {url()} ({getattr(exc, 'reason', None) or exc})") from exc
    if stream:
        return (line.decode(errors="replace") for line in resp)
    try:
        return resp.read().decode()
    except (TimeoutError, socket.timeout, OSError) as exc:
        raise AIError(f"no answer from {path} within {timeout:g} s") from exc


def _json(text: str) -> dict:
    try:
        out = json.loads(text)
    except ValueError as exc:
        raise AIError(f"unreadable answer: {text[:120]!r}") from exc
    if isinstance(out, dict) and out.get("error"):
        raise AIError(str(out["error"])[:200])
    return out


def _gb(n) -> float:
    return round((n or 0) / 1e9, 1)


def health() -> dict:
    """{ok, version, error}: GET /api/version."""
    try:
        v = _json(_http("GET", "/api/version"))
    except AIError as exc:
        return {"ok": False, "version": None, "error": str(exc)}
    return {"ok": True, "version": v.get("version"), "error": None}


def models() -> list[dict]:
    """The models on disk: [{name, size_gb, modified}] from /api/tags."""
    rows = _json(_http("GET", "/api/tags")).get("models") or []
    return [{"name": m.get("name") or m.get("model"), "size_gb": _gb(m.get("size")), "modified": m.get("modified_at")}
            for m in rows]


def ps() -> list[dict]:
    """The models loaded in RAM right now: [{name, size_gb, ram_mb, until}] from /api/ps."""
    rows = _json(_http("GET", "/api/ps")).get("models") or []
    return [{"name": m.get("name") or m.get("model"), "size_gb": _gb(m.get("size")),
             "ram_mb": round((m.get("size") or 0) / 1e6), "until": m.get("expires_at")} for m in rows]


def pull(model: str, progress_cb: Optional[Callable[[float, str], None]] = None) -> None:
    """Download a model (streaming /api/pull); `progress_cb(pct, status)` on every line. Raises AIError on a failure."""
    pct = 0.0
    for line in _http("POST", "/api/pull", {"name": model, "stream": True}, timeout=PULL_TIMEOUT_S, stream=True):
        if not line.strip():
            continue
        d = _json(line)
        if d.get("total"):
            pct = round(100.0 * (d.get("completed") or 0) / d["total"], 1)
        if d.get("status") == "success":
            pct = 100.0
        if progress_cb:
            progress_cb(pct, str(d.get("status") or ""))


def generate(model: str, prompt: str, system: Optional[str] = None, json_mode: bool = True, timeout: float = 90,
             keep_alive: Union[str, int] = "10m", options: Optional[dict] = None) -> dict:
    """One non-streaming /api/generate call: {text, tokens, prompt_tokens, ms, load_ms, eval_ms, model}. `options` are
    Ollama's generation options (temperature, num_predict, num_ctx, ...)."""
    body = {"model": model, "prompt": prompt, "stream": False, "keep_alive": keep_alive}
    if system:
        body["system"] = system
    if options:
        body["options"] = dict(options)
    if json_mode:
        body["format"] = "json"
    d = _json(_http("POST", "/api/generate", body, timeout=timeout))
    ns = lambda k: round((d.get(k) or 0) / 1e6)
    return {"text": d.get("response") or "", "tokens": int(d.get("eval_count") or 0),
            "prompt_tokens": int(d.get("prompt_eval_count") or 0), "ms": ns("total_duration"),
            "load_ms": ns("load_duration"), "eval_ms": ns("eval_duration"), "model": d.get("model") or model}


def unload(model: str) -> None:
    """Free the model's RAM at once (a generate with keep_alive 0 and no prompt)."""
    _json(_http("POST", "/api/generate", {"model": model, "keep_alive": 0}, timeout=CONNECT_TIMEOUT_S * 4))
