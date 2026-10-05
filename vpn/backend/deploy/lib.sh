#!/usr/bin/env bash
# Shared helpers for Harbor's server scripts. Source it; don't run it.
# Everything here must work unattended on a server's first boot (cloud-init):
# no terminal, possibly no HOME, apt possibly busy with first-boot updates.

export HOME="${HOME:-/root}"
export DEBIAN_FRONTEND=noninteractive
GO_VERSION=1.24.7
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"

log() { printf '\n==> %s\n' "$*"; }

# apt that waits for the first-boot unattended-upgrades run instead of failing.
apt_get() { apt-get -o DPkg::Lock::Timeout=900 -yq "$@"; }

ensure_go() {
  if /usr/local/go/bin/go version 2>/dev/null | grep -q "go$GO_VERSION "; then return; fi
  log "Installing Go $GO_VERSION"
  local arch
  arch="$(dpkg --print-architecture)" # amd64 or arm64
  curl -fsSL --retry 5 "https://go.dev/dl/go$GO_VERSION.linux-$arch.tar.gz" -o /tmp/go.tgz
  rm -rf /usr/local/go && tar -C /usr/local -xzf /tmp/go.tgz && rm -f /tmp/go.tgz
}

build_harbor() {
  log "Building Harbor"
  (cd "$BACKEND_DIR" && GOTOOLCHAIN=local GOCACHE=/root/.cache/go-build GOPATH=/root/go \
    /usr/local/go/bin/go build -trimpath -o bin/ ./cmd/...)
}

public_ipv4() {
  curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null ||
    ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}'
}

public_ipv6() {
  ip -6 addr show scope global 2>/dev/null | awk '/inet6/ {sub(/\/.*/, "", $2); print $2; exit}'
}

# Sets COUNTRY_CODE, COUNTRY, CITY, LATITUDE, LONGITUDE for an IP, keeping any
# values already set in the environment.
locate() {
  local geo
  geo="$(curl -fsS --max-time 8 "https://ipinfo.io/$1/json" 2>/dev/null || echo '{}')"
  eval "$(python3 - "$geo" <<'EOF'
import json, shlex, sys
g = json.loads(sys.argv[1] or "{}")
names = {"ar":"Argentina","at":"Austria","au":"Australia","be":"Belgium","bg":"Bulgaria","br":"Brazil",
  "ca":"Canada","ch":"Switzerland","cl":"Chile","co":"Colombia","cz":"Czechia","de":"Germany","dk":"Denmark",
  "es":"Spain","fi":"Finland","fr":"France","gb":"United Kingdom","gr":"Greece","hk":"Hong Kong","ie":"Ireland",
  "il":"Israel","in":"India","id":"Indonesia","it":"Italy","jp":"Japan","kr":"South Korea","mx":"Mexico",
  "my":"Malaysia","nl":"Netherlands","no":"Norway","nz":"New Zealand","pe":"Peru","ph":"Philippines",
  "pl":"Poland","pt":"Portugal","ro":"Romania","rs":"Serbia","se":"Sweden","sg":"Singapore","th":"Thailand",
  "tr":"Turkey","tw":"Taiwan","ua":"Ukraine","us":"United States","za":"South Africa","ae":"United Arab Emirates"}
cc = (g.get("country") or "xx").lower()
lat, _, lon = (g.get("loc") or "0,0").partition(",")
out = {"_CC": cc, "_COUNTRY": names.get(cc, cc.upper()), "_CITY": g.get("city") or "My Server",
       "_LAT": lat or "0", "_LON": lon or "0"}
for k, v in out.items():
    print(f"{k}={shlex.quote(v)}")
EOF
)"
  COUNTRY_CODE="${COUNTRY_CODE:-$_CC}"
  COUNTRY="${COUNTRY:-$_COUNTRY}"
  CITY="${CITY:-$_CITY}"
  LATITUDE="${LATITUDE:-$_LAT}"
  LONGITUDE="${LONGITUDE:-$_LON}"
}

# enroll_node API_URL SECRET: adds this server to the Harbor server list.
# Retries for up to 30 minutes: a brand-new main server may still be
# installing or waiting for its HTTPS certificate.
enroll_node() {
  local api="$1" secret="$2" ip ip6 body
  ip="$(public_ipv4)"
  ip6="$(public_ipv6 || true)"
  locate "$ip"
  # shellcheck disable=SC1091
  . /etc/harbor/node.env
  body="$(python3 - "$ip" "$ip6" "$(wg pubkey </etc/harbor/wg.key)" \
    "$(printf %s "$HARBOR_NODE_TOKEN" | sha256sum | cut -d' ' -f1)" \
    "$COUNTRY_CODE" "$COUNTRY" "$CITY" "$LATITUDE" "$LONGITUDE" <<'EOF'
import json, sys
ip, ip6, pub, token, cc, country, city, lat, lon = sys.argv[1:]
node = {"ipv4": ip, "public_key": pub, "node_token_sha256": token, "country_code": cc,
        "country": country, "city": city, "latitude": float(lat or 0), "longitude": float(lon or 0),
        "hostname": ip, "port": 51820, "free": True, "capacity_peers": 500, "link_mbps": 1000}
if ip6:
    node["ipv6"] = ip6
print(json.dumps(node))
EOF
)"
  log "Adding $CITY, $COUNTRY ($ip) to Harbor at $api"
  for _ in $(seq 1 180); do
    if curl -fsS --max-time 15 -X POST -H "Authorization: Bearer $secret" \
      -H "Content-Type: application/json" -d "$body" "$api/v1/node/enroll"; then
      echo
      return 0
    fi
    sleep 10
  done
  echo "could not reach $api to add this server" >&2
  return 1
}
