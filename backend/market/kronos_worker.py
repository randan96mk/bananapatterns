"""📈 The candle model worker: a separate process (python -m backend.market.kronos_worker --port 8765) that loads
Kronos-mini once and answers on 127.0.0.1 only. It runs at nice 10 with 2 torch threads so the app comes first.

  GET  /health  {state loading|ready|error, version, load_s, error}
  POST /read    {symbol, interval 1m|5m|1d, candles [{at, o, h, l, c, v}], horizon, horizons?, samples, levels?}
                (the interval sets the timestamps of the steps ahead: minutes, 5 minutes or weekdays; horizons are steps)
                -> {p_up, exp_range, paths_summary {q10, q50, q90}, touch {level: prob}, ms, version, reads [one per
                horizon]} (the top-level keys are the longest horizon's). The sampled paths are summarised by
                backend/market/paths.py; nothing else is computed here and nothing leaves the server.

The weights: data/ai/kronos/<repo> (vendored at first run and kept forever): copied from the image's baked copy
(BP_KRONOS_BAKED, the Dockerfile downloads them at build time so the container starts offline), else from the local
Hugging Face cache, else downloaded once with huggingface_hub."""

import argparse
import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

VERSION = "kronos-mini-v1"
MODEL_REPO, TOKENIZER_REPO, MAX_CONTEXT = "NeoQuasar/Kronos-mini", "NeoQuasar/Kronos-Tokenizer-2k", 2048
CONTEXT = 120                      # the last 120 one-minute candles (2 hours) go in: 30 steps × 10 paths in ~1.7 s on 2 x86 threads (400 took 6.3 s)
THREADS = 2
ROOT = Path(__file__).resolve().parents[2]

_state = {"state": "loading", "version": VERSION, "load_s": None, "error": None}
_model: dict = {}
_lock = threading.Lock()           # one read at a time (the 2 threads are the read's)


def weights_dir() -> Path:
    db = os.getenv("BP_DB_PATH")
    base = Path(db).parent if db else ROOT / "data"
    return Path(os.getenv("BP_KRONOS_DIR") or base / "ai" / "kronos")


def resolve(repo: str) -> Path:
    """The local folder of one repo's weights (config.json + model.safetensors), vendored once under weights_dir()."""
    name = repo.split("/")[-1]
    dest = weights_dir() / name
    if (dest / "config.json").exists():
        return dest
    baked = os.getenv("BP_KRONOS_BAKED")
    baked_dir = Path(baked) / name if baked else None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        writable = os.access(dest.parent, os.W_OK)
    except OSError:
        writable = False                 # e.g. data/ai owned by another user on the server (PermissionError 13)
    if baked_dir and (baked_dir / "config.json").exists():
        if not writable:
            return baked_dir             # read the image's copy in place; nothing needs writing
        try:
            shutil.copytree(baked_dir, dest, dirs_exist_ok=True)
            return dest
        except OSError:
            return baked_dir
    from huggingface_hub import snapshot_download
    try:
        src = Path(snapshot_download(repo, local_files_only=True))       # the local Hugging Face cache
    except Exception:
        src = None
    if src is not None:
        if not writable:
            return src
        try:
            shutil.copytree(src, dest, dirs_exist_ok=True)
            return dest
        except OSError:
            return src
    if not writable:
        raise RuntimeError(f"no weights for {repo}: {dest.parent} is not writable and no baked copy or cache exists")
    snapshot_download(repo, local_dir=str(dest))                          # once; kept forever
    return dest


def load() -> None:
    t0 = time.monotonic()
    try:
        import torch
        torch.set_num_threads(THREADS)
        from backend.market.kronos import Kronos, KronosTokenizer
        tok = KronosTokenizer.from_pretrained(str(resolve(TOKENIZER_REPO)))
        model = Kronos.from_pretrained(str(resolve(MODEL_REPO)))
        tok.eval()
        model.eval()
        _model.update(tok=tok, model=model)
        _state.update(state="ready", load_s=round(time.monotonic() - t0, 2))
    except Exception as exc:                                              # the app shows "no model read"
        _state.update(state="error", error=f"{exc.__class__.__name__}: {str(exc)[:300]}")


def future_stamps(last, steps: int, interval: str = "1m"):
    """The timestamps of the steps ahead: 1-minute (the index / hint reads), 5-minute, or the next weekdays for daily
    candles ("1d": the Real positions' nightly read)."""
    import pandas as pd
    if interval == "1d":
        return pd.Series(pd.bdate_range(last.normalize() + pd.Timedelta(days=1), periods=steps))
    step = {"5m": 5}.get(interval, 1)
    return pd.Series(pd.date_range(last + pd.Timedelta(minutes=step), periods=steps, freq=f"{step}min"))


def sample_paths(candles: list[dict], steps: int, samples: int, seed: Optional[int] = None, interval: str = "1m") -> tuple[list, list, list]:
    """Kronos sampled paths (not their mean): the vendored predictor's own normalisation and autoregressive loop
    (backend/market/kronos/kronos.py auto_regressive_inference, MIT) with the per-sample decode kept. Returns
    (closes, highs, lows), each samples × steps."""
    import numpy as np
    import pandas as pd
    import torch
    from backend.market.kronos import calc_time_stamps
    from backend.market.kronos.kronos import sample_from_logits
    tok, model = _model["tok"], _model["model"]
    cs = candles[-CONTEXT:]
    ts = pd.Series(pd.to_datetime([c["at"] for c in cs]))
    fut = future_stamps(ts.iloc[-1], steps, interval)
    x = np.array([[c["o"], c["h"], c["l"], c["c"], float(c.get("v") or 0), float(c.get("v") or 0) * c["c"]] for c in cs], dtype=np.float32)
    mean, std = x.mean(axis=0), x.std(axis=0)
    xn = np.clip((x - mean) / (std + 1e-5), -5, 5)
    if seed is not None:
        torch.manual_seed(seed)
    with torch.no_grad():
        xt = torch.from_numpy(xn[np.newaxis]).repeat(samples, 1, 1)
        xs = torch.from_numpy(calc_time_stamps(ts).values.astype(np.float32)[np.newaxis]).repeat(samples, 1, 1)
        ys = torch.from_numpy(calc_time_stamps(fut).values.astype(np.float32)[np.newaxis]).repeat(samples, 1, 1)
        x_token = tok.encode(xt, half=True)
        pre, post = x_token[0], x_token[1]
        stamp = torch.cat([xs, ys], dim=1)
        n0 = xt.size(1)
        for i in range(steps):
            L = n0 + i
            a = max(0, L - MAX_CONTEXT)
            s1_logits, ctx = model.decode_s1(pre[:, a:L], post[:, a:L], stamp[:, a:L, :].contiguous())
            s1 = sample_from_logits(s1_logits[:, -1, :], temperature=1.0, top_k=0, top_p=0.9, sample_logits=True)
            s2_logits = model.decode_s2(ctx, s1)
            s2 = sample_from_logits(s2_logits[:, -1, :], temperature=1.0, top_k=0, top_p=0.9, sample_logits=True)
            pre = torch.cat([pre, s1], dim=1)
            post = torch.cat([post, s2], dim=1)
        a = max(0, n0 + steps - MAX_CONTEXT)
        z = tok.decode([pre[:, a:].contiguous(), post[:, a:].contiguous()], half=True).cpu().numpy()
    z = z[:, -steps:, :] * (std + 1e-5) + mean                           # samples × steps × (o h l c v a)
    hi = np.maximum(z[:, :, 1], np.maximum(z[:, :, 0], z[:, :, 3]))
    lo = np.minimum(z[:, :, 2], np.minimum(z[:, :, 0], z[:, :, 3]))
    return z[:, :, 3].tolist(), hi.tolist(), lo.tolist()


def read(req: dict) -> dict:
    from backend.market.paths import summarise
    if _state["state"] != "ready":
        raise RuntimeError(f"model {_state['state']}")
    candles = req.get("candles") or []
    if len(candles) < 30:
        raise ValueError("fewer than 30 candles")
    hs = sorted({int(x) for x in (req.get("horizons") or [req.get("horizon") or 30])} | {int(req.get("horizon") or 30)})
    steps = max(hs)
    samples = max(1, min(int(req.get("samples") or 10), 30))
    t0 = time.monotonic()
    with _lock:
        closes, highs, lows = sample_paths(candles, steps, samples, req.get("seed"), req.get("interval") or "1m")
    last = float(candles[-1]["c"])
    reads = summarise(last, closes, highs, lows, req.get("levels") or [], hs)
    ms = round((time.monotonic() - t0) * 1000)
    top = next(r for r in reads if r["horizon"] == int(req.get("horizon") or steps))
    return {**top, "reads": reads, "ms": ms, "version": VERSION, "last": last, "symbol": req.get("symbol"),
            "interval": req.get("interval") or "1m", "context": min(len(candles), CONTEXT)}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/health"):
            return self._send(200, dict(_state))
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self.path.startswith("/read"):
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            self._send(200, read(req))
        except Exception as exc:
            self._send(500, {"error": f"{exc.__class__.__name__}: {str(exc)[:300]}"})

    def log_message(self, *a):                                           # no request log lines (nothing secret, just noise)
        return


def main(argv: Optional[list] = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    try:
        os.nice(10)
    except OSError:
        pass
    os.environ.setdefault("OMP_NUM_THREADS", str(THREADS))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    threading.Thread(target=load, name="kronos-load", daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"kronos worker on 127.0.0.1:{args.port} ({VERSION})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
