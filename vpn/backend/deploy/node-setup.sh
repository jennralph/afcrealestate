#!/usr/bin/env bash
# Turn a fresh Ubuntu 24.04 server into a Harbor VPN node.
#
#   sudo HARBOR_API=https://api.example.net ./node-setup.sh
#
# It prints the server's WireGuard public key and a node token; put both in
# the API's servers.json (the token as its SHA-256, which this also prints).
#
# What it sets up:
#   * wg0 on UDP 51820 holding the gateway addresses 10.64.0.1-3
#   * NAT to the internet, with device-to-device traffic blocked
#   * unbound answering on .1 (plain), .2 (ads/trackers blocked) and
#     .3 (ads/trackers/malware/phishing blocked), refreshed daily
#   * harbor-node syncing peers from the API
#   * no persistent logs: journald in RAM only, unbound query logging off
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
: "${HARBOR_API:?set HARBOR_API to the control plane URL}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
WAN="$(ip -o -4 route show to default | awk '{print $5; exit}')"
V4=10.64.0
V6=fd68:6172:626f:7200

apt_get update
apt_get install wireguard-tools nftables unbound curl python3

# Our nftables rules below are the firewall. Many cloud images ship ufw
# allowing only SSH, which would block the VPN and its forwarding.
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw --force disable
fi

# ---- logging: keep nothing on disk -----------------------------------------
mkdir -p /etc/systemd/journald.conf.d
cat >/etc/systemd/journald.conf.d/harbor.conf <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=16M
MaxRetentionSec=1h
EOF
systemctl restart systemd-journald

# ---- WireGuard -------------------------------------------------------------
install -d -m 700 /etc/harbor
[[ -f /etc/harbor/wg.key ]] || (umask 077 && wg genkey >/etc/harbor/wg.key)
cat >/etc/wireguard/wg0.conf <<EOF
[Interface]
Address = ${V4}.1/10, ${V4}.2/32, ${V4}.3/32, ${V6}::1/64, ${V6}::2/128, ${V6}::3/128
ListenPort = 51820
PostUp = wg set %i private-key /etc/harbor/wg.key
# Peers are managed by harbor-node; never saved to this file.
SaveConfig = false
EOF
chmod 600 /etc/wireguard/wg0.conf

cat >/etc/sysctl.d/90-harbor.conf <<EOF
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
# Forwarding normally makes Linux ignore router advertisements, which is how
# most clouds hand out the server's own IPv6 route. Keep accepting them.
net.ipv6.conf.${WAN}.accept_ra = 2
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
EOF
sysctl --system >/dev/null

cat >/etc/nftables.conf <<EOF
#!/usr/sbin/nft -f
flush ruleset
table inet harbor {
  chain forward {
    type filter hook forward priority 0; policy drop;
    ct state established,related accept
    # Devices may reach the internet but never each other.
    iifname "wg0" oifname "${WAN}" accept
  }
  chain postrouting {
    type nat hook postrouting priority 100;
    oifname "${WAN}" ip saddr 10.64.0.0/10 masquerade
    oifname "${WAN}" ip6 saddr ${V6}::/64 masquerade
  }
  chain input {
    type filter hook input priority 0; policy drop;
    iif "lo" accept
    ct state established,related accept
    ct state invalid drop
    meta l4proto { icmp, ipv6-icmp } accept
    tcp dport { 22, 80, 443 } accept
    udp dport 51820 accept
    # DNS on the gateway only from inside the tunnel.
    iifname "wg0" udp dport 53 accept
    iifname "wg0" tcp dport 53 accept
  }
}
EOF
systemctl enable --now nftables
nft -f /etc/nftables.conf
systemctl enable --now wg-quick@wg0

# ---- unbound with three filtering views -------------------------------------
install -d /etc/unbound/harbor
touch /etc/unbound/harbor/ads.conf /etc/unbound/harbor/malware.conf
cat >/etc/unbound/unbound.conf.d/harbor.conf <<EOF
server:
  interface: ${V4}.1
  interface: ${V4}.2
  interface: ${V4}.3
  interface: ${V6}::1
  interface: ${V6}::2
  interface: ${V6}::3
  access-control: 10.64.0.0/10 allow
  access-control: ${V6}::/64 allow
  interface-view: ${V4}.2 ads
  interface-view: ${V6}::2 ads
  interface-view: ${V4}.3 malware
  interface-view: ${V6}::3 malware
  # Bind the tunnel addresses even if wg0 isn't up yet at boot.
  ip-freebind: yes
  verbosity: 0
  log-queries: no
  log-replies: no
  hide-identity: yes
  hide-version: yes
  qname-minimisation: yes
  aggressive-nsec: yes
  prefetch: yes
  num-threads: $(nproc)

view:
  name: "ads"
  view-first: yes
  include: /etc/unbound/harbor/ads.conf

view:
  name: "malware"
  view-first: yes
  include: /etc/unbound/harbor/ads.conf
  include: /etc/unbound/harbor/malware.conf
EOF
install -m 755 "$HERE/refresh-blocklists.sh" /usr/local/sbin/harbor-refresh-blocklists
/usr/local/sbin/harbor-refresh-blocklists || echo "blocklist download failed; filtering views start empty" >&2
cat >/etc/systemd/system/harbor-blocklists.service <<'EOF'
[Unit]
Description=Refresh Harbor DNS blocklists
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/harbor-refresh-blocklists
EOF
cat >/etc/systemd/system/harbor-blocklists.timer <<'EOF'
[Unit]
Description=Daily Harbor DNS blocklist refresh
[Timer]
OnCalendar=daily
RandomizedDelaySec=2h
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl enable --now unbound harbor-blocklists.timer
systemctl restart unbound

# ---- harbor-node agent -------------------------------------------------------
install -m 755 "$HERE/../bin/harbor-node" /usr/local/bin/harbor-node 2>/dev/null \
  || { echo "build harbor-node first: (cd backend && go build -o bin/ ./cmd/harbor-node)" >&2; exit 1; }
[[ -f /etc/harbor/node.env ]] || {
  TOKEN="$(head -c 32 /dev/urandom | base64 | tr -d '/+=')"
  umask 077
  printf 'HARBOR_NODE_TOKEN=%s\nHARBOR_API=%s\n' "$TOKEN" "$HARBOR_API" >/etc/harbor/node.env
}
cat >/etc/systemd/system/harbor-node.service <<'EOF'
[Unit]
Description=Harbor VPN peer sync
After=wg-quick@wg0.service network-online.target
Wants=network-online.target
[Service]
EnvironmentFile=/etc/harbor/node.env
ExecStart=/usr/local/bin/harbor-node -api ${HARBOR_API} -iface wg0
Restart=always
RestartSec=5
CapabilityBoundingSet=CAP_NET_ADMIN
AmbientCapabilities=CAP_NET_ADMIN
DynamicUser=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now harbor-node

# shellcheck disable=SC1091
. /etc/harbor/node.env
echo
echo "Node ready. Add this to servers.json on the API:"
echo "  \"public_key\": \"$(wg pubkey </etc/harbor/wg.key)\","
echo "  \"node_token_sha256\": \"$(printf %s "$HARBOR_NODE_TOKEN" | sha256sum | cut -d' ' -f1)\""
