#!/usr/bin/env bash
# Download the ad/tracker and malware blocklists and turn them into unbound
# local-zone rules. Lists come from HaGeZi (https://github.com/hagezi/dns-blocklists),
# which are curated to keep false positives low.
set -euo pipefail
OUT=/etc/unbound/harbor
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
BASE=https://raw.githubusercontent.com/hagezi/dns-blocklists/main/domains

to_unbound() {
  # Keep plain hostnames only; drop comments and anything malformed.
  grep -E '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$' "$1" \
    | awk '{ printf "local-zone: \"%s.\" always_nxdomain\n", $1 }'
}

curl -fsSL --retry 3 "$BASE/pro.txt" -o "$TMP/ads.txt"
curl -fsSL --retry 3 "$BASE/tif.txt" -o "$TMP/malware.txt"
to_unbound "$TMP/ads.txt" >"$TMP/ads.conf"
to_unbound "$TMP/malware.txt" >"$TMP/malware.conf"

# Refuse to install suspiciously small lists (truncated download).
for f in ads malware; do
  [[ $(wc -l <"$TMP/$f.conf") -gt 1000 ]] || { echo "$f list too small, keeping old one" >&2; exit 1; }
done
cp "$OUT/ads.conf" "$TMP/ads.prev" 2>/dev/null || true
cp "$OUT/malware.conf" "$TMP/malware.prev" 2>/dev/null || true
install -m 644 "$TMP/ads.conf" "$OUT/ads.conf"
install -m 644 "$TMP/malware.conf" "$OUT/malware.conf"
if ! unbound-checkconf >/dev/null; then
  echo "new lists broke unbound config; restoring previous" >&2
  install -m 644 "$TMP/ads.prev" "$OUT/ads.conf" 2>/dev/null || : >"$OUT/ads.conf"
  install -m 644 "$TMP/malware.prev" "$OUT/malware.conf" 2>/dev/null || : >"$OUT/malware.conf"
  exit 1
fi
systemctl reload unbound 2>/dev/null || systemctl restart unbound
