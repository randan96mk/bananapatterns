"""matplotlib for the Telegram alert charts (drawn in backend/alerts/decision.py), headless (Agg).
matplotlib is optional: without it CHARTS_AVAILABLE is False and alerts go out as text only
(install with `.venv/bin/pip install -r requirements.txt`).
"""

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    CHARTS_AVAILABLE = True
except ImportError:                                    # optional dependency
    plt = Rectangle = None
    CHARTS_AVAILABLE = False
