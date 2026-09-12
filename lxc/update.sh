#!/usr/bin/env bash
#
# UniFi Insights Plus — update an LXC installation in place.
#
# Run INSIDE the container as root:
#
#   /opt/uip-src/lxc/update.sh [--ref TAG_OR_BRANCH] [--repo URL] [--source DIR]
#
# The repository defaults to whichever one this container was installed from,
# recorded by install.sh in /etc/unifi-insights-plus.source.
#
# Refreshes the source tree, rebuilds the UI, syncs Python dependencies, and
# restarts the services. /etc/unifi-insights-plus.env and the database are left
# alone — the application migrates its own schema on start.
#
set -euo pipefail

SRC_DIR=/opt/uip-src
REF=""
REPO_URL=""
SOURCE_FILE=/etc/unifi-insights-plus.source

# install.sh records the repository this container was built from. A fork carries
# lxc/, upstream main does not, so guessing the URL here would break fork users.
if [ -r "$SOURCE_FILE" ]; then
    # shellcheck source=/dev/null
    . "$SOURCE_FILE"
    REPO_URL="${UIP_REPO:-}"
    REF="${UIP_REF:-}"
fi

msg()  { echo -e "\033[1;34m[update]\033[0m $*"; }
ok()   { echo -e "\033[1;32m[  ok  ]\033[0m $*"; }
warn() { echo -e "\033[1;33m[ warn ]\033[0m $*" >&2; }
die()  { echo -e "\033[1;31m[fatal ]\033[0m $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --ref)     REF="$2"; shift 2 ;;
        --repo)    REPO_URL="$2"; shift 2 ;;
        --source)  SRC_DIR="$2"; shift 2 ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        *)         die "Unknown argument: $1" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "Must run as root."

OLD_VERSION=$(cat /app/VERSION 2>/dev/null || echo unknown)

ensure_git() {
    command -v git >/dev/null && return 0
    msg "Installing git..."
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends git ca-certificates
}

if [ -d "$SRC_DIR/.git" ]; then
    msg "Updating the git checkout at $SRC_DIR..."
    ensure_git
    git -C "$SRC_DIR" fetch --tags --depth 1 origin "${REF:-HEAD}"
    git -C "$SRC_DIR" checkout --force "${REF:-FETCH_HEAD}"
elif [ -n "$REPO_URL" ]; then
    # proxmox-lxc.sh delivers a tarball, so /opt/uip-src has no .git. Replace it
    # with a real clone of the repository this container was installed from.
    msg "Cloning $REPO_URL (${REF:-main}) into $SRC_DIR..."
    ensure_git
    rm -rf "$SRC_DIR"
    git clone --depth 1 --branch "${REF:-main}" "$REPO_URL" "$SRC_DIR"
else
    warn "$SRC_DIR is not a git checkout and no repository is recorded in $SOURCE_FILE."
    warn "Reinstalling from the existing source tree — nothing will be fetched."
    warn "Pass --repo URL --ref BRANCH, or update from the Proxmox host with lxc/proxmox-update.sh."
fi

[ -f "$SRC_DIR/lxc/install.sh" ] || die "No installer at $SRC_DIR/lxc/install.sh."

msg "Stopping services..."
systemctl stop uip-api.service uip-receiver.service || true

# install.sh is idempotent: it re-syncs /app, rebuilds the UI, refreshes the venv,
# reinstalls the units, and skips database init because the database exists.
bash "$SRC_DIR/lxc/install.sh" --source "$SRC_DIR"

echo
ok "Updated: $OLD_VERSION → $(cat /app/VERSION)"
