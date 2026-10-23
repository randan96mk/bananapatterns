#!/bin/bash
# One-time setup: Python venv + backend deps, frontend deps, .env from template.
set -e
cd "$(dirname "$0")"

PYTHON=""
for p in python3.13 python3.12 python3; do
    if command -v "$p" >/dev/null 2>&1; then
        if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then PYTHON="$p"; break; fi
    fi
done
[ -z "$PYTHON" ] && { echo "Python 3.12+ required (brew install python@3.12)"; exit 1; }
echo "Using $($PYTHON --version)"

[ -d .venv ] || "$PYTHON" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "Backend dependencies installed"

command -v npm >/dev/null 2>&1 || { echo "Node.js 18+ required (brew install node)"; exit 1; }
(cd frontend && npm install --no-audit --no-fund)
echo "Frontend dependencies installed"

mkdir -p data logs
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env — fill in KITE_API_KEY and KITE_API_SECRET"
fi
.venv/bin/python -c "from backend.database.models import init_db; init_db(); print('Database ready: data/bananapatterns.db')"
echo "Setup complete. Start with ./start.sh"
