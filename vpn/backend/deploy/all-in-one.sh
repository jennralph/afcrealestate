#!/usr/bin/env bash
# Run all of Harbor on ONE fresh Ubuntu 24.04 server: a VPN location, the API
# and the web app, with HTTPS. This is the "main" server; more countries are
# added later with add-location.sh (the web app shows the owner a ready-made
# script for it).
#
#   sudo ./deploy/all-in-one.sh
#
# Optional settings (environment variables):
#   HARBOR_HOST    domain for this server. Default: <ip>.sslip.io, a free
#                  name that always points at the IP, so no domain is needed.
#   MAX_ACCOUNTS   sign-ups close after this many accounts (default 5). The
#                  first account becomes the owner, with every feature.
#   COUNTRY_CODE, COUNTRY, CITY   override the detected location label.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

PUBLIC_IP="$(public_ipv4)"
[[ -n "$PUBLIC_IP" ]] || { echo "could not determine this server's public IPv4 address" >&2; exit 1; }
HOST="${HARBOR_HOST:-${PUBLIC_IP//./-}.sslip.io}"
log "Setting up Harbor at https://$HOST"

apt_get update
apt_get install curl ca-certificates git python3 caddy
ensure_go
build_harbor

# ---- this server as a VPN location ---------------------------------------------
# The agent talks to the API on the same machine directly, so it works
# before the HTTPS certificate exists.
HARBOR_API="http://127.0.0.1:8080" "$HERE/node-setup.sh"

# ---- API + web app --------------------------------------------------------------------
install -m 755 "$BACKEND_DIR/bin/harbor-api" /usr/local/bin/harbor-api
install -m 755 "$HERE/grant.sh" /usr/local/sbin/harbor-grant
if [[ ! -f /etc/harbor/api.env ]]; then
  (
    umask 077
    echo "HARBOR_ACCOUNT_SECRET=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    echo "HARBOR_ENROLL_SECRET=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  ) >/etc/harbor/api.env
fi

cat >/etc/systemd/system/harbor-api.service <<EOF
[Unit]
Description=Harbor VPN control plane and web app
After=network-online.target
Wants=network-online.target
[Service]
EnvironmentFile=/etc/harbor/api.env
ExecStart=/usr/local/bin/harbor-api -listen 127.0.0.1:8080 -trust-proxy \\
  -db /var/lib/harbor/db.json -servers /var/lib/harbor/servers.json \\
  -max-accounts ${MAX_ACCOUNTS:-5} -owner-days 3650
StateDirectory=harbor
DynamicUser=yes
Restart=always
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable harbor-api
systemctl restart harbor-api

# ---- HTTPS (Caddy gets a free certificate automatically) -------------------------------
cat >/etc/caddy/Caddyfile <<EOF
$HOST {
	reverse_proxy 127.0.0.1:8080
	header -Server
	header Strict-Transport-Security "max-age=31536000"
}
EOF
systemctl enable caddy
systemctl restart caddy

# ---- list this server as a location (through the local API, no TLS needed) -----------
for _ in $(seq 1 30); do
  curl -fsS -o /dev/null http://127.0.0.1:8080/healthz && break
  sleep 1
done
# shellcheck disable=SC1091
. /etc/harbor/api.env
enroll_node http://127.0.0.1:8080 "$HARBOR_ENROLL_SECRET"
systemctl restart harbor-node

echo "https://$HOST" >/etc/harbor/ADDRESS
cat <<EOF

============================================================
 Harbor is running.  On your iPhone, open in Safari:

     https://$HOST

 The first account you create there is the owner: every
 location, 7 devices, and the "Add a location" button.
============================================================
EOF
