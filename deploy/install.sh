#!/usr/bin/env bash
# VPS installer. Run as root on a fresh Debian/Ubuntu box:
#   bash deploy/install.sh
set -euo pipefail

APP=/opt/fleet
USER=fleet

echo "==> packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync >/dev/null

echo "==> user and directory"
id -u "$USER" >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin "$USER"
mkdir -p "$APP"
rsync -a --exclude .venv --exclude '*.db' --exclude .git \
      "$(cd "$(dirname "$0")/.." && pwd)/" "$APP/"

echo "==> virtualenv"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

if [ ! -f "$APP/.env" ]; then
  cp "$APP/.env.example" "$APP/.env"
  echo "    -> created $APP/.env — fill in the tokens before starting"
fi
chown -R "$USER:$USER" "$APP"
chmod 600 "$APP/.env"

echo "==> systemd"
cp "$APP/deploy/fleet.service" /etc/systemd/system/fleet.service
systemctl daemon-reload
systemctl enable fleet >/dev/null

cat <<'MSG'

Installed.

  1. nano /opt/fleet/.env                 # tokens, chat id, API key
  2. sudo -u fleet /opt/fleet/.venv/bin/python -m fleet doctor
  3. systemctl start fleet
  4. journalctl -u fleet -f

MSG
