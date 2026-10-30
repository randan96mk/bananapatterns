#!/bin/bash
# Banana Patterns with Docker.
#   ./docker.sh up        start in the background (builds the image the first time; restarts on its own)
#   ./docker.sh down      stop
#   ./docker.sh logs      follow the logs (Ctrl+C to stop following)
#   ./docker.sh status    container state and health
#   ./docker.sh update    git pull, rebuild and restart (after a PR is merged)
#   ./docker.sh restart   restart without rebuilding
#   ./docker.sh rebuild   rebuild from the code as it is now (no git pull), e.g. after checking out an older version
#   ./docker.sh verify-e300   read-only check of strategy E's top-300 universe (prints, saves nothing, sends nothing)
set -e
cd "$(dirname "$0")"
PORT="${BP_PORT:-8000}"
export BP_UID="$(id -u)" BP_GID="$(id -g)"   # the container runs as you, so it can write data/ and logs/

# Docker Desktop's "user" install puts the docker command in ~/.docker/bin, which a terminal opened earlier (or
# one whose PATH lacks it) can't see; look in the usual Docker Desktop / OrbStack / Homebrew places too.
for d in "$HOME/.docker/bin" "/Applications/Docker.app/Contents/Resources/bin" "$HOME/.orbstack/bin" \
         "/usr/local/bin" "/opt/homebrew/bin"; do
    [ -x "$d/docker" ] && case ":$PATH:" in *":$d:"*) ;; *) PATH="$PATH:$d" ;; esac
done
export PATH
command -v docker >/dev/null 2>&1 || {
    echo "Docker not found. Install Docker Desktop (or OrbStack) and open it; if it is installed, choose"
    echo "Docker Desktop -> Settings -> Advanced -> System (requires password), then reopen this terminal."
    exit 1
}
docker info >/dev/null 2>&1 || { echo "Docker is not running: open Docker Desktop (or OrbStack) and try again."; exit 1; }

running() { [ "$(docker inspect -f '{{.State.Running}}' bananapatterns 2>/dev/null)" = "true" ]; }

wait_healthy() {
    printf "Waiting for the app"
    for _ in $(seq 60); do
        state=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' bananapatterns 2>/dev/null || true)
        if [ "$state" = "healthy" ] || curl -fs "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
            echo " ✓"; echo "Dashboard: http://localhost:$PORT"; return 0
        fi
        printf "."; sleep 2
    done
    echo ""; echo "Not healthy yet: check ./docker.sh logs"; return 1
}

up() {   # $1: extra flag for compose (update passes --build)
    [ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example: add your Kite (and Telegram) keys, then run again."; exit 1; }
    mkdir -p data logs
    if ! running && command -v lsof >/dev/null 2>&1 && lsof -ti "tcp:$PORT" >/dev/null 2>&1; then
        echo "Port $PORT is already in use (./start.sh still running?). Stop it first: two copies would send"
        echo "every alert twice."
        exit 1
    fi
    docker compose up -d $1
    wait_healthy
}

case "${1:-up}" in
    up) up ;;
    down) docker compose down ;;
    logs) docker compose logs -f --tail 200 ;;
    status) docker compose ps; curl -fs "http://127.0.0.1:$PORT/health" && echo "" || echo "health: not answering" ;;
    restart) docker compose restart && wait_healthy ;;
    update) git pull --ff-only && up --build ;;
    rebuild) up --build ;;
    verify-e300) docker compose exec banana python -m backend.tools.verify_e300 ;;
    *) sed -n '2,9p' "$0"; exit 1 ;;
esac
