#!/bin/bash
# Copy the server's nightly backups to this Mac (over Tailscale). Run it whenever you like, e.g. weekly.
#   scripts/pull-backups.sh            from ubuntu@banana into ~/banana-backups-cloud/
# Overrides: BP_SERVER (ubuntu@banana), BP_PULL_DIR (~/banana-backups-cloud).
set -euo pipefail
SERVER="${BP_SERVER:-ubuntu@banana}"
TO="${BP_PULL_DIR:-$HOME/banana-backups-cloud}"
mkdir -p "$TO" && chmod 700 "$TO"
rsync -av "$SERVER:banana-backups/" "$TO/"
echo "Backups on this Mac: $TO"
ls -1 "$TO" | tail -5
