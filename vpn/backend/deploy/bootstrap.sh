#!/usr/bin/env bash
# First-boot entry point: paste a two-line script that pipes this file into
# bash into your cloud provider's "startup script" / "cloud-init user data"
# box, and the server installs itself. No SSH needed.
#
#   bootstrap.sh all-in-one      the main server (VPN + API + web app)
#   bootstrap.sh add-location    another country (needs HARBOR_API and
#                                HARBOR_ENROLL_SECRET in the environment)
#
# Progress is logged to /var/log/harbor-setup.log.
set -euo pipefail
MODE="${1:?usage: bootstrap.sh all-in-one|add-location}"
REPO="${HARBOR_REPO:-https://github.com/jennralph/afcrealestate.git}"
SRC=/opt/harbor

exec > >(tee -a /var/log/harbor-setup.log) 2>&1
echo "== Harbor $MODE setup started $(date -u)"
export HOME="${HOME:-/root}" DEBIAN_FRONTEND=noninteractive
command -v git >/dev/null || apt-get -o DPkg::Lock::Timeout=900 -yq update
command -v git >/dev/null || apt-get -o DPkg::Lock::Timeout=900 -yq install git
if [[ -d $SRC/.git ]]; then
  git -C "$SRC" pull --ff-only
else
  git clone --depth 1 "$REPO" "$SRC"
fi
case "$MODE" in
  all-in-one) "$SRC/vpn/backend/deploy/all-in-one.sh" ;;
  add-location) "$SRC/vpn/backend/deploy/add-location.sh" ;;
  *) echo "unknown mode $MODE" >&2; exit 1 ;;
esac
echo "== Harbor $MODE setup finished $(date -u)"
