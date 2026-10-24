#!/bin/bash
# Start Banana Patterns.
#   ./start.sh            dev:  backend :8000 (auto-reload) + Vite dev server :3000
#   ./start.sh --prod     build frontend, backend :8000 serves everything
#   ./start.sh --backend  backend only
# Shares port 8000 and the Kite app with AlgoTrade-v3: stop that first.
set -e
cd "$(dirname "$0")"
MODE="${1:-dev}"

[ -x .venv/bin/python ] || { echo "Run ./setup.sh first"; exit 1; }
[ -f .env ] || { cp .env.example .env; echo "Created .env — add your Kite credentials"; }
mkdir -p data logs

# Free our ports only (not every uvicorn on the machine)
for port in 8000 3000; do
    pids=$(lsof -ti tcp:$port 2>/dev/null || true)
    if [ -n "$pids" ]; then echo "Stopping process on :$port ($pids)"; kill $pids 2>/dev/null || true; fi
done
sleep 1

API_KEY=$(grep -E '^KITE_API_KEY=' .env | cut -d= -f2 | tr -d "\"' ")
if [ -n "$API_KEY" ] && [ "$API_KEY" != "your_api_key" ]; then
    echo ""
    echo "Daily Kite login (token expires 06:00 IST):"
    echo "  https://kite.zerodha.com/connect/login?v=3&api_key=$API_KEY"
    echo "  or use the Login button in the dashboard."
    echo ""
else
    echo "KITE_API_KEY not set in .env — dashboard works, data sync needs Kite."
fi

# macOS: keep the Mac from idle-sleeping while the backend runs, so the evening job,
# reminders and intraday alerts fire. caffeinate -w watches the backend PID and exits
# with it (closing the lid on battery still sleeps the Mac).
keep_awake() {
    if command -v caffeinate >/dev/null 2>&1; then
        caffeinate -i -w "$1" &
        echo "Keeping the Mac awake while the backend runs (caffeinate)"
    fi
}

cleanup() { [ -n "$BACK" ] && kill "$BACK" 2>/dev/null; [ -n "$FRONT" ] && kill "$FRONT" 2>/dev/null; exit 0; }
trap cleanup INT TERM

case "$MODE" in
    --prod)
        (cd frontend && npm run build)
        echo "Dashboard: http://localhost:8000"
        .venv/bin/python run.py & BACK=$!
        keep_awake "$BACK"
        wait $BACK ;;
    --backend)
        .venv/bin/python run.py & BACK=$!
        keep_awake "$BACK"
        wait $BACK ;;
    *)
        BP_DEV=1 .venv/bin/python run.py & BACK=$!
        keep_awake "$BACK"
        sleep 2
        (cd frontend && npm run dev) & FRONT=$!
        echo "Dashboard (dev): http://localhost:3000   API docs: http://localhost:8000/docs"
        wait $BACK $FRONT ;;
esac
