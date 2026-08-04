#!/usr/bin/env bash
# Install yt-listen-later on a Debian/Ubuntu VPS as a dedicated user, with a
# systemd timer doing the syncing. Run as root from a checkout:
#
#   sudo ./deploy/install.sh
#
# Idempotent: safe to re-run after pulling new commits.
set -euo pipefail

APP_DIR=${APP_DIR:-/srv/yt-listen-later}
APP_USER=${APP_USER:-ytll}
UV_BIN=${UV_BIN:-/usr/local/bin/uv}
SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

if [[ $EUID -ne 0 ]]; then
	echo "run this as root (sudo $0)" >&2
	exit 1
fi

echo "==> installing ffmpeg"
if command -v apt-get >/dev/null; then
	apt-get update -qq
	apt-get install -y -qq ffmpeg ca-certificates curl
else
	echo "not a Debian/Ubuntu box — install ffmpeg yourself, continuing" >&2
fi

if [[ ! -x $UV_BIN ]]; then
	echo "==> installing uv to $UV_BIN"
	# UV_INSTALL_DIR controls where the installer puts the binary.
	curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=$(dirname "$UV_BIN") sh
fi

if ! id "$APP_USER" >/dev/null 2>&1; then
	echo "==> creating user $APP_USER"
	useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

echo "==> syncing code to $APP_DIR"
mkdir -p "$APP_DIR/public/media" "$APP_DIR/.cache"
install -m 755 "$SRC_DIR/yt_listen_later.py" "$APP_DIR/yt_listen_later.py"
install -m 644 "$SRC_DIR/test_yt_listen_later.py" "$APP_DIR/test_yt_listen_later.py"
install -m 644 "$SRC_DIR/.env.example" "$APP_DIR/.env.example"

if [[ ! -f "$APP_DIR/.env" ]]; then
	install -m 600 "$SRC_DIR/.env.example" "$APP_DIR/.env"
	NEEDS_CONFIG=1
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> installing systemd units"
for unit in yt-listen-later.service yt-listen-later-sync.service yt-listen-later-sync.timer; do
	install -m 644 "$SRC_DIR/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now yt-listen-later-sync.timer

cat <<EOF

Installed to $APP_DIR, syncing on a 30 minute timer.

Next:
  1. ${NEEDS_CONFIG:+edit $APP_DIR/.env — set YOUTUBE_PLAYLIST_URL and BASE_URL}${NEEDS_CONFIG:-$APP_DIR/.env kept as-is}
  2. point a reverse proxy at $APP_DIR/public (see deploy/Caddyfile)
  3. first sync:   systemctl start yt-listen-later-sync.service
     watch it:     journalctl -fu yt-listen-later-sync.service
  4. subscribe in Overcast to \$BASE_URL/feed.xml

Serving the files with Caddy directly needs no other service. To use the
built-in server instead: systemctl enable --now yt-listen-later.service
EOF
