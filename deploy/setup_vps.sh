#!/usr/bin/env bash
# Gulai Store — one-time VPS setup script
# Run as root on a fresh Ubuntu 22.04 / 24.04 server:
#   bash setup_vps.sh YOUR_DOMAIN
#
# What it does:
#   1. Install Python 3.11, nginx, certbot
#   2. Create system user 'gulaistore'
#   3. Deploy app to /opt/gulaistore
#   4. Create virtualenv and install dependencies
#   5. Install systemd service
#   6. Configure nginx + obtain Let's Encrypt certificate
#   7. Start the bot

set -euo pipefail

DOMAIN="${1:?Usage: $0 YOUR_DOMAIN}"
APP_DIR="/opt/gulaistore"
APP_USER="gulaistore"

echo "=== [1/7] Installing system packages ==="
apt-get update -q
apt-get install -y -q python3.11 python3.11-venv python3-pip nginx certbot python3-certbot-nginx git

echo "=== [2/7] Creating system user '$APP_USER' ==="
id "$APP_USER" &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo "=== [3/7] Deploying app to $APP_DIR ==="
mkdir -p "$APP_DIR"

# Copy project files from current directory (run this script from the repo root)
rsync -a --exclude='.venv' --exclude='*.db' --exclude='*.log' --exclude='.env' \
      --exclude='.idea' --exclude='__pycache__' --exclude='.pytest_cache' \
      ./ "$APP_DIR/"

# .env must be created manually on the server — never committed to git
if [[ ! -f "$APP_DIR/.env" ]]; then
    echo ""
    echo "!!! IMPORTANT: copy your .env to the server:"
    echo "    scp .env root@YOUR_SERVER:$APP_DIR/.env"
    echo "!!! Then re-run this script, or manually:"
    echo "    chmod 600 $APP_DIR/.env"
    echo "    chown $APP_USER:$APP_USER $APP_DIR/.env"
    echo ""
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 750 "$APP_DIR"

echo "=== [4/7] Creating virtualenv and installing dependencies ==="
sudo -u "$APP_USER" python3.11 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "=== [5/7] Installing systemd service ==="
cp "$APP_DIR/deploy/gulaistore.service" /etc/systemd/system/gulaistore.service
systemctl daemon-reload
systemctl enable gulaistore

echo "=== [6/7] Configuring nginx ==="
# Replace placeholder domain in nginx config
sed "s/YOUR_DOMAIN/$DOMAIN/g" "$APP_DIR/deploy/nginx.conf" \
    > /etc/nginx/sites-available/gulaistore
ln -sf /etc/nginx/sites-available/gulaistore /etc/nginx/sites-enabled/gulaistore
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "Obtaining Let's Encrypt certificate for $DOMAIN ..."
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "admin@$DOMAIN" \
    --redirect || echo "WARNING: certbot failed — check DNS and try: certbot --nginx -d $DOMAIN"

echo "=== [7/7] Starting Gulai Store bot ==="
systemctl start gulaistore
sleep 2
systemctl status gulaistore --no-pager

echo ""
echo "=== Done! ==="
echo "Useful commands:"
echo "  journalctl -u gulaistore -f          # live logs"
echo "  systemctl restart gulaistore          # restart bot"
echo "  systemctl status gulaistore           # service status"
echo "  curl https://$DOMAIN/health           # health check"
