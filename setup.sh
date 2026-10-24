#!/bin/bash
# One-time setup: Python venv + backend deps, frontend deps, .env from template.
set -e
cd "$(dirname "$0")"

# Find Python >= 3.12: $BP_PYTHON, then versioned names on PATH, Homebrew (Apple silicon / Intel),
# pyenv shims and python.org installs. macOS's own python3 (Xcode tools) is 3.9, too old.
is_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; }
PYTHON=""
CANDIDATES="${BP_PYTHON:-}"
for v in 3.14 3.13 3.12; do
    CANDIDATES="$CANDIDATES python$v /opt/homebrew/bin/python$v /usr/local/bin/python$v \
        /opt/homebrew/opt/python@$v/bin/python$v /usr/local/opt/python@$v/bin/python$v \
        $HOME/.pyenv/shims/python$v /Library/Frameworks/Python.framework/Versions/$v/bin/python$v"
done
CANDIDATES="$CANDIDATES python3 /opt/homebrew/bin/python3 /usr/local/bin/python3"
for p in $CANDIDATES; do
    if command -v "$p" >/dev/null 2>&1 && is_ok "$p"; then PYTHON="$(command -v "$p")"; break; fi
done
if [ -z "$PYTHON" ]; then
    echo "Python 3.12+ not found."
    echo "  python3 on PATH: $(command -v python3 || echo none) ($(python3 --version 2>&1))"
    echo "  Fix:  brew install python@3.12   then re-run ./setup.sh"
    echo "  Or point to one explicitly:  BP_PYTHON=/path/to/python3.12 ./setup.sh"
    exit 1
fi
echo "Using $PYTHON ($($PYTHON --version))"

if [ -d .venv ] && ! is_ok .venv/bin/python; then
    echo "Existing .venv uses an old Python; recreating"
    rm -rf .venv
fi
[ -d .venv ] || "$PYTHON" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
cksum < requirements.txt > .venv/.requirements.stamp     # start.sh reinstalls when this changes
echo "Backend dependencies installed"

command -v npm >/dev/null 2>&1 || { echo "Node.js 18+ required (brew install node)"; exit 1; }
(cd frontend && npm install --no-audit --no-fund && cksum < package.json > node_modules/.package.stamp)
echo "Frontend dependencies installed"

mkdir -p data logs
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env — fill in KITE_API_KEY and KITE_API_SECRET"
fi
.venv/bin/python -c "from backend.database.models import init_db; init_db(); print('Database ready: data/bananapatterns.db')"
echo "Setup complete. Start with ./start.sh"
