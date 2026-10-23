"""Entry point: python run.py  (BP_DEV=1 for auto-reload)."""

import os

import uvicorn

from backend.config import ServerConfig


def main():
    cfg = ServerConfig()
    print(f"""
  Banana Patterns — personal VCP screener (NSE, Nifty 500)
  API:       http://localhost:{cfg.backend_port}      docs: http://localhost:{cfg.backend_port}/docs
  Dashboard: http://localhost:{cfg.frontend_port} (dev)  or  http://localhost:{cfg.backend_port} (built)
""")
    uvicorn.run("backend.main:app", host=cfg.host, port=cfg.backend_port,
                reload=os.getenv("BP_DEV") == "1", log_level="info")


if __name__ == "__main__":
    main()
