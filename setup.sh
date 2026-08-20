#!/usr/bin/env sh
# Zero-admin meeting recorder — setup for macOS and Linux.
#
#   ./setup.sh
#
# Creates .venv, installs what this platform needs, builds the macOS helper
# when Swift is available, and checks permissions. No sudo at any point.
set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "Python 3.9+ is required but was not found on PATH."
    echo "  macOS: install it from python.org or with 'brew install python'"
    echo "  Linux: install your distribution's python3 package"
    exit 1
fi

exec "$PYTHON" bootstrap.py "$@"
