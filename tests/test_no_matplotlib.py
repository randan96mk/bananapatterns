"""The app must run, and alerts must still go out (as text), when matplotlib is missing."""

import os
import subprocess
import sys
from pathlib import Path

from backend.alerts import charts
from backend.alerts.dispatch import Notifier, dispatch_scan
from backend.alerts.samples import build_samples
from backend.config import AlertsConfig, AppConfig, BaseConfig
from tests.test_alerts import FakeTG, scanned_db

ROOT = Path(__file__).resolve().parent.parent


def test_backend_imports_without_matplotlib(tmp_path):
    code = ("import sys; sys.modules['matplotlib'] = None\n"      # makes `import matplotlib` raise ImportError
            "import backend.main\n"
            "from backend.alerts import charts\n"
            "assert charts.CHARTS_AVAILABLE is False\n"
            "print('ok')")
    env = dict(os.environ, BP_DB_PATH=str(tmp_path / "x.db"), BP_NO_SCHEDULER="1")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip().endswith("ok")


def test_alerts_fall_back_to_text(monkeypatch):
    monkeypatch.setattr(charts, "CHARTS_AVAILABLE", False)
    as_of, tokens = scanned_db()
    cfg = AlertsConfig(chat_id="42", tiering=False, screens=["bases"], near_pivot_pct=0.05)
    fake = FakeTG()
    sent = dispatch_scan(as_of, cfg, BaseConfig(reset_ma=0), Notifier(cfg, fake), tokens)
    assert sent["stage"] >= 1 and all(kind == "msg" for kind, _ in fake.sent)        # no photos
    assert any("broke out" in html for _, html in fake.sent)


def test_samples_without_charts(monkeypatch):
    monkeypatch.setattr(charts, "CHARTS_AVAILABLE", False)
    c = AppConfig()
    c.alerts = AlertsConfig(chat_id="42")
    assert all(png is None for _, _, png in build_samples("all", c, {}))
