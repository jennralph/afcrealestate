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

# Everything is inside main() so bash has read the whole file before any
# command runs; with `curl ... | bash`, a child reading stdin could
# otherwise swallow the rest of the script.
main() {
  local mode="${1:?usage: bootstrap.sh all-in-one|add-location}"
  local repo="${HARBOR_REPO:-https://github.com/jennralph/afcrealestate.git}"
  local src=/opt/harbor

  exec > >(tee -a /var/log/harbor-setup.log) 2>&1 </dev/null
  echo "== Harbor $mode setup started $(date -u)"
  export HOME="${HOME:-/root}" DEBIAN_FRONTEND=noninteractive
  if ! command -v git >/dev/null; then
    apt-get -o DPkg::Lock::Timeout=900 -yq update
    apt-get -o DPkg::Lock::Timeout=900 -yq install git
  fi
  if [[ -d $src/.git ]]; then
    git -C "$src" pull --ff-only
  else
    git clone --depth 1 "$repo" "$src"
  fi
  case "$mode" in
    all-in-one) "$src/vpn/backend/deploy/all-in-one.sh" ;;
    add-location) "$src/vpn/backend/deploy/add-location.sh" ;;
    *) echo "unknown mode $mode" >&2; return 1 ;;
  esac
  echo "== Harbor $mode setup finished $(date -u)"
}

main "$@"
