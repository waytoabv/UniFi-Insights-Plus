#!/usr/bin/env bash
#
# UniFi Insights Plus — update an existing LXC container from the Proxmox host.
#
# Run this on the PROXMOX VE HOST as root, from the repository checkout you want
# to deploy. It copies the current working tree into the container and re-runs
# the installer.
#
#   ./lxc/proxmox-update.sh <ctid> [--source DIR] [--no-pull]
#
# Unlike lxc/update.sh (which runs inside the container and needs git there),
# this needs nothing in the container but a running systemd — the source is
# pushed from the host, the same way proxmox-lxc.sh provisions it.
#
# Configuration and database are untouched; the application migrates its own
# schema on start.
#
set -euo pipefail

CTID=""
SRC_DIR=""
PULL=1
CT_SRC=/opt/uip-src

msg()  { echo -e "\033[1;34m[pve]\033[0m $*"; }
ok()   { echo -e "\033[1;32m[ ok ]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*" >&2; }
die()  { echo -e "\033[1;31m[fail]\033[0m $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --source)  SRC_DIR="$2"; shift 2 ;;
        --no-pull) PULL=0; shift ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        -*)        die "Unknown argument: $1" ;;
        *)         CTID="$1"; shift ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "Must run as root on the Proxmox VE host."
command -v pct >/dev/null || die "'pct' not found — run this on a Proxmox VE host."
[ -n "$CTID" ] || die "Usage: $0 <ctid> [--source DIR] [--no-pull]"

[ -n "$SRC_DIR" ] || SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$SRC_DIR/lxc/install.sh" ] || die "No installer at $SRC_DIR/lxc/install.sh."

pct status "$CTID" >/dev/null 2>&1 || die "Container $CTID does not exist."
[ "$(pct status "$CTID")" = "status: running" ] || die "Container $CTID is not running. Start it with: pct start $CTID"

# ── Refresh the host checkout ────────────────────────────────────────────────

REPO_URL=""
REPO_REF=""
if [ -d "$SRC_DIR/.git" ]; then
    REPO_URL=$(git -C "$SRC_DIR" remote get-url origin 2>/dev/null || true)
    REPO_REF=$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
    if [ "$PULL" = "1" ]; then
        msg "Pulling $REPO_REF in $SRC_DIR..."
        git -C "$SRC_DIR" pull --ff-only
    fi
    ok "Deploying $(git -C "$SRC_DIR" describe --always --dirty 2>/dev/null) from $REPO_REF"
else
    warn "$SRC_DIR is not a git checkout — deploying it as-is."
fi

OLD_VERSION=$(pct exec "$CTID" -- cat /app/VERSION 2>/dev/null || echo unknown)

# ── Deliver and install ──────────────────────────────────────────────────────

msg "Copying $SRC_DIR into container $CTID..."
TARBALL=$(mktemp /tmp/uip-src-XXXXXX.tar.gz)
trap 'rm -f "$TARBALL"' EXIT
tar -czf "$TARBALL" \
    --exclude='.git' \
    --exclude='node_modules' \
    --exclude='ui/dist' \
    --exclude='__pycache__' \
    -C "$SRC_DIR" .

pct exec "$CTID" -- rm -rf "$CT_SRC"
pct exec "$CTID" -- mkdir -p "$CT_SRC"
pct push "$CTID" "$TARBALL" /tmp/uip-src.tar.gz
pct exec "$CTID" -- tar -xzf /tmp/uip-src.tar.gz -C "$CT_SRC"
pct exec "$CTID" -- rm -f /tmp/uip-src.tar.gz
ok "Source delivered."

msg "Running the installer (several minutes: the UI is rebuilt)..."
pct exec "$CTID" -- chmod +x "$CT_SRC/lxc/install.sh"
if [ -n "$REPO_URL" ]; then
    pct exec "$CTID" -- "$CT_SRC/lxc/install.sh" --source "$CT_SRC" --repo "$REPO_URL" --ref "${REPO_REF:-main}"
else
    pct exec "$CTID" -- "$CT_SRC/lxc/install.sh" --source "$CT_SRC"
fi

NEW_VERSION=$(pct exec "$CTID" -- cat /app/VERSION 2>/dev/null || echo unknown)
CT_IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')

echo
ok "Container $CTID updated: ${OLD_VERSION} → ${NEW_VERSION}"
echo
echo "  Dashboard   http://${CT_IP:-unknown}:8000"
echo "  Logs        pct exec $CTID -- journalctl -u uip-api -u uip-receiver -f"
echo
