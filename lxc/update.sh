#!/usr/bin/env bash
#
# UniFi Insights Plus — update an LXC installation in place.
#
# Run INSIDE the container as root:
#
#   /opt/uip-src/lxc/update.sh [--ref TAG_OR_BRANCH] [--source DIR]
#
# Refreshes the source tree, rebuilds the UI, syncs Python dependencies, and
# restarts the services. /etc/unifi-insights-plus.env and the database are left
# alone — the application migrates its own schema on start.
#
set -euo pipefail

SRC_DIR=/opt/uip-src
REF=""
REPO_URL="https://github.com/jmasarweh/UniFi-Insights-Plus.git"

msg()  { echo -e "\033[1;34m[update]\033[0m $*"; }
ok()   { echo -e "\033[1;32m[  ok  ]\033[0m $*"; }
warn() { echo -e "\033[1;33m[ warn ]\033[0m $*" >&2; }
die()  { echo -e "\033[1;31m[fatal ]\033[0m $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --ref)     REF="$2"; shift 2 ;;
        --source)  SRC_DIR="$2"; shift 2 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *)         die "Unknown argument: $1" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "Must run as root."

OLD_VERSION=$(cat /app/VERSION 2>/dev/null || echo unknown)

if [ -d "$SRC_DIR/.git" ]; then
    msg "Updating the git checkout at $SRC_DIR..."
    command -v git >/dev/null || { apt-get update -qq && apt-get install -y -qq git; }
    git -C "$SRC_DIR" fetch --tags --depth 1 origin "${REF:-HEAD}"
    git -C "$SRC_DIR" checkout --force "${REF:-FETCH_HEAD}"
elif [ -n "$REF" ]; then
    msg "Cloning $REPO_URL ($REF) into $SRC_DIR..."
    command -v git >/dev/null || { apt-get update -qq && apt-get install -y -qq git; }
    rm -rf "$SRC_DIR"
    git clone --depth 1 --branch "$REF" "$REPO_URL" "$SRC_DIR"
else
    warn "$SRC_DIR is not a git checkout and no --ref was given."
    warn "Reinstalling from the existing source tree. To pull a release, pass --ref v3.7.0."
fi

[ -f "$SRC_DIR/lxc/install.sh" ] || die "No installer at $SRC_DIR/lxc/install.sh."

msg "Stopping services..."
systemctl stop uip-api.service uip-receiver.service || true

# install.sh is idempotent: it re-syncs /app, rebuilds the UI, refreshes the venv,
# reinstalls the units, and skips database init because the database exists.
bash "$SRC_DIR/lxc/install.sh" --source "$SRC_DIR"

echo
ok "Updated: $OLD_VERSION → $(cat /app/VERSION)"
