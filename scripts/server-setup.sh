#!/bin/bash
# Prepare a fresh Ubuntu 24.04 server (Oracle Cloud A1, ARM) for Banana Patterns. Safe to run again.
#   from the Mac:   scp scripts/server-setup.sh ubuntu@<public-ip>:      (the repo isn't on the server yet)
#   on the server:  bash server-setup.sh     updates, IST clock, swap, Docker, Tailscale, deploy key, checks
# Steps already done are skipped. See docs/CLOUD.md for the full walk-through.
set -euo pipefail
[ "$(id -u)" -ne 0 ] || { echo "Run as the normal user (ubuntu), not root: the script uses sudo itself."; exit 1; }
APT="sudo NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt-get -y -q"
NAME="${BP_HOSTNAME:-banana}"
step() { printf '\n==== %s ====\n' "$*"; }

step "1/6 Ubuntu updates, tools, IST clock, hostname"
$APT update
$APT full-upgrade
$APT install git sqlite3 curl ca-certificates rsync
sudo timedatectl set-timezone Asia/Kolkata
[ "$(hostname)" = "$NAME" ] || sudo hostnamectl set-hostname "$NAME"

step "2/6 Swap (2 GB)"
if swapon --show | grep -q /swapfile; then echo "already on"
else
    sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

step "3/6 Docker (official packages)"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then echo "already installed"
else
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
    $APT update
    $APT install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
sudo systemctl enable --now docker
id -nG | grep -qw docker || { sudo usermod -aG docker "$USER"; echo "Added $USER to the docker group: log out and in again."; }

step "4/6 Tailscale"
command -v tailscale >/dev/null 2>&1 || curl -fsSL https://tailscale.com/install.sh | sh
if tailscale status >/dev/null 2>&1; then echo "already logged in: $(tailscale ip -4 | head -1)"
else
    echo "Open the link below in a browser and log in with the SAME account as your Mac / phone:"
    sudo tailscale up --hostname="$NAME"
fi

step "5/6 GitHub deploy key (read-only clone)"
[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N "" -C "$USER@$NAME" -f ~/.ssh/id_ed25519
echo "If not added yet: GitHub -> bananapatterns -> Settings -> Deploy keys -> Add (leave write access OFF):"
cat ~/.ssh/id_ed25519.pub

step "6/6 Reachability (NSE / Kite / Telegram)"
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
code() { curl -s -o /dev/null -m 20 -w '%{http_code}' "$@" || true; }
echo "NSE events API: $(code -A "$UA" -H 'Referer: https://www.nseindia.com/' https://www.nseindia.com/api/event-calendar)  (want 200)"
echo "NSE bulk csv:   $(code -A "$UA" https://nsearchives.nseindia.com/content/equities/bulk.csv)  (want 200)"
echo "Kite API:       $(code https://api.kite.trade/)  (anything but 000)"
echo "Telegram:       $(code https://api.telegram.org/)  (200 or 302)"

printf '\nDone. Next (docs/CLOUD.md): clone the repo, add .env, ./docker.sh up, sudo tailscale serve --bg 8000.\n'
