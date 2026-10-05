#!/usr/bin/env bash
# Give an account Harbor Plus (every location, 7 devices) without the App Store.
#   sudo harbor-grant 1234567890123456 [days]
# harbor-api holds its database in memory, so it is stopped for a moment.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
[[ $# -ge 1 ]] || { echo "usage: harbor-grant <account number> [days]" >&2; exit 1; }
set -a
# shellcheck disable=SC1091
. /etc/harbor/api.env
set +a
systemctl stop harbor-api
trap 'systemctl start harbor-api' EXIT
# DynamicUser keeps the database under /var/lib/private.
DB=/var/lib/harbor/db.json
[[ -f $DB ]] || DB=/var/lib/private/harbor/db.json
harbor-api grant -db "$DB" -days "${2:-365}" "$1"
# The rewrite was done as root; hand the file back to the service user.
chown --reference="$(dirname "$DB")" "$DB"
