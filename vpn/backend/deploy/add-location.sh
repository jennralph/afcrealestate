#!/usr/bin/env bash
# Turn a fresh Ubuntu 24.04 server into one more Harbor location. It joins the
# main server and appears in everyone's location list within a minute.
#
#   sudo HARBOR_API=https://<main server> HARBOR_ENROLL_SECRET=<secret> ./deploy/add-location.sh
#
# The owner's Account page in the web app shows this with both values filled in.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
: "${HARBOR_API:?set HARBOR_API to the main server, e.g. https://203-0-113-10.sslip.io}"
: "${HARBOR_ENROLL_SECRET:?set HARBOR_ENROLL_SECRET (shown to the owner in the web app)}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

apt_get update
apt_get install curl ca-certificates git python3
ensure_go
build_harbor
"$HERE/node-setup.sh"
enroll_node "${HARBOR_API%/}" "$HARBOR_ENROLL_SECRET"
systemctl restart harbor-node
log "This server is now a Harbor location."
