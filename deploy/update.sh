#!/usr/bin/env bash
# Gulai Store — deploy code update to VPS
# Run from LOCAL machine (not on VPS):
#   bash deploy/update.sh root@YOUR_SERVER_IP
#
# What it does:
#   - rsync changed source files to /opt/gulaistore
#   - reinstall Python dependencies if requirements.txt changed
#   - restart the bot service

set -euo pipefail

SERVER="${1:?Usage: $0 user@server}"
APP_DIR="/opt/gulaistore"

echo "=== Syncing files to $SERVER:$APP_DIR ==="
rsync -az --progress \
    --exclude='.venv' --exclude='*.db' --exclude='*.log' \
    --exclude='.env' --exclude='.idea' --exclude='__pycache__' \
    --exclude='.pytest_cache' --exclude='apirules' \
    ./ "$SERVER:$APP_DIR/"

echo "=== Updating dependencies (if requirements.txt changed) ==="
ssh "$SERVER" "
    $APP_DIR/.venv/bin/pip install --quiet -r $APP_DIR/requirements.txt
    chown -R gulaistore:gulaistore $APP_DIR
"

echo "=== Restarting service ==="
ssh "$SERVER" "systemctl restart gulaistore && sleep 1 && systemctl status gulaistore --no-pager"

echo "=== Done. Live logs: ==="
echo "    ssh $SERVER 'journalctl -u gulaistore -f'"
