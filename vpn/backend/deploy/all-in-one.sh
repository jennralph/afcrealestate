#!/usr/bin/env bash
# Run all of Harbor on ONE fresh Ubuntu 24.04 server: the VPN node, the API
# and the web app, with HTTPS. Good for a personal or trial setup.
#
#   git clone https://github.com/jennralph/afcrealestate.git
#   cd afcrealestate/vpn/backend
#   sudo ./deploy/all-in-one.sh
#
# Optional settings (environment variables):
#   HARBOR_HOST   domain pointing at this server. Default: <ip>.sslip.io,
#                 a free name that resolves to this server, so no domain needed.
#   COUNTRY_CODE, COUNTRY, CITY   how the location is shown in the app.
#
# Open TCP 80 and 443 and UDP 51820 in your cloud provider's firewall.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$(cd "$HERE/.." && pwd)"
GO_VERSION=1.24.7

PUBLIC_IP="$(curl -4 -fsS --max-time 5 https://api.ipify.org || ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1); exit}')"
[[ -n "$PUBLIC_IP" ]] || { echo "could not determine this server's public IPv4 address" >&2; exit 1; }
HOST="${HARBOR_HOST:-${PUBLIC_IP//./-}.sslip.io}"

if [[ -z "${COUNTRY_CODE:-}" ]]; then
  # Best-effort lookup of where this server is, for the location label.
  GEO="$(curl -fsS --max-time 5 "https://ipinfo.io/$PUBLIC_IP/json" || true)"
  COUNTRY_CODE="$(sed -n 's/.*"country": *"\([A-Z][A-Z]\)".*/\1/p' <<<"$GEO" | tr 'A-Z' 'a-z')"
  CITY="${CITY:-$(sed -n 's/.*"city": *"\([^"]*\)".*/\1/p' <<<"$GEO")}"
fi
COUNTRY_CODE="${COUNTRY_CODE:-xx}"
CITY="${CITY:-My Server}"
COUNTRY="${COUNTRY:-$(python3 -c "import sys
names={'us':'United States','gb':'United Kingdom','de':'Germany','nl':'Netherlands','fr':'France','se':'Sweden','ch':'Switzerland','ca':'Canada','jp':'Japan','sg':'Singapore','au':'Australia','fi':'Finland','es':'Spain','it':'Italy','br':'Brazil','in':'India','pl':'Poland','at':'Austria'}
print(names.get(sys.argv[1], sys.argv[1].upper()))" "$COUNTRY_CODE")}"
echo "==> Harbor at https://$HOST — location: $CITY, $COUNTRY"

# ---- Go toolchain (Ubuntu's is too old) ----------------------------------------
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -yq curl ca-certificates python3 caddy
if ! /usr/local/go/bin/go version 2>/dev/null | grep -q "go$GO_VERSION"; then
  ARCH="$(dpkg --print-architecture)" # amd64 or arm64
  curl -fsSL "https://go.dev/dl/go$GO_VERSION.linux-$ARCH.tar.gz" -o /tmp/go.tgz
  rm -rf /usr/local/go && tar -C /usr/local -xzf /tmp/go.tgz && rm /tmp/go.tgz
fi
(cd "$BACKEND" && GOTOOLCHAIN=local /usr/local/go/bin/go build -trimpath -o bin/ ./cmd/...)

# ---- VPN node (WireGuard, DNS filtering, peer sync) --------------------------------
HARBOR_API="https://$HOST" "$HERE/node-setup.sh"

# ---- API + web app ------------------------------------------------------------------
install -m 755 "$BACKEND/bin/harbor-api" /usr/local/bin/harbor-api
install -m 755 "$HERE/grant.sh" /usr/local/sbin/harbor-grant
[[ -f /etc/harbor/api.env ]] || (umask 077 && echo "HARBOR_ACCOUNT_SECRET=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" >/etc/harbor/api.env)

# shellcheck disable=SC1091
. /etc/harbor/node.env
PUBKEY="$(wg pubkey </etc/harbor/wg.key)"
TOKEN_HASH="$(printf %s "$HARBOR_NODE_TOKEN" | sha256sum | cut -d' ' -f1)"
python3 - "$PUBKEY" "$TOKEN_HASH" "$PUBLIC_IP" "$HOST" "$COUNTRY_CODE" "$COUNTRY" "$CITY" >/etc/harbor/servers.json <<'EOF'
import json, sys
pub, token, ip, host, cc, country, city = sys.argv[1:]
print(json.dumps({"servers": [{
    "id": f"{cc}-01", "country_code": cc, "country": country, "city": city,
    "latitude": 0, "longitude": 0, "hostname": host, "ipv4": ip, "port": 51820,
    "public_key": pub, "free": True, "features": [],
    "capacity_peers": 500, "link_mbps": 1000, "node_token_sha256": token,
}]}, indent=2))
EOF

cat >/etc/systemd/system/harbor-api.service <<'EOF'
[Unit]
Description=Harbor VPN control plane and web app
After=network-online.target
Wants=network-online.target
[Service]
EnvironmentFile=/etc/harbor/api.env
ExecStart=/usr/local/bin/harbor-api -listen 127.0.0.1:8080 -trust-proxy -db /var/lib/harbor/db.json -servers /etc/harbor/servers.json
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

# ---- HTTPS (Caddy gets a Let's Encrypt certificate automatically) ----------------------
cat >/etc/caddy/Caddyfile <<EOF
$HOST {
	reverse_proxy 127.0.0.1:8080
	header -Server
	header Strict-Transport-Security "max-age=31536000"
}
EOF
systemctl enable caddy
systemctl restart caddy
systemctl restart harbor-node

echo
echo "Harbor is running. On your iPhone, open:"
echo
echo "    https://$HOST"
echo
echo "Create an account there. To lift the 1-device limit on your own account:"
echo "    sudo harbor-grant <your account number> 3650"
