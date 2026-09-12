#!/usr/bin/env bash
#
# UniFi Insights Plus — create and provision a Proxmox LXC container.
#
# Run this on the PROXMOX VE HOST as root. It creates an unprivileged Ubuntu
# 24.04 container, copies this repository into it, and runs lxc/install.sh.
#
#   ./lxc/proxmox-lxc.sh [options]
#
#   --ctid N            container ID          (default: next free)
#   --hostname NAME     container hostname    (default: unifi-insights)
#   --cores N           CPU cores             (default: 4)
#   --memory MB         RAM in MB             (default: 4096)
#   --swap MB           swap in MB            (default: 512)
#   --disk GB           root disk in GB       (default: 16)
#   --storage NAME      rootfs storage        (default: auto-detected)
#   --template-storage NAME  template storage (default: auto-detected)
#   --bridge NAME       network bridge        (default: vmbr0)
#   --ip CIDR           static IP, e.g. 192.168.1.50/24 (default: dhcp)
#   --gateway IP        gateway, required with --ip
#   --source DIR        repository to install (default: this checkout)
#   --git-ref REF       clone this ref from GitHub instead of using --source
#   --no-destroy-on-fail  keep a half-built container for debugging
#
set -euo pipefail

CTID=""
HOSTNAME_="unifi-insights"
CORES=4
MEMORY=4096
SWAP=512
DISK=16
STORAGE=""
TEMPLATE_STORAGE=""
BRIDGE=vmbr0
IPCONF="dhcp"
GATEWAY=""
SRC_DIR=""
GIT_REF=""
DESTROY_ON_FAIL=1
REPO_URL="https://github.com/jmasarweh/UniFi-Insights-Plus.git"

msg()  { echo -e "\033[1;34m[pve]\033[0m $*"; }
ok()   { echo -e "\033[1;32m[ ok ]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*" >&2; }
die()  { echo -e "\033[1;31m[fail]\033[0m $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --ctid)             CTID="$2"; shift 2 ;;
        --hostname)         HOSTNAME_="$2"; shift 2 ;;
        --cores)            CORES="$2"; shift 2 ;;
        --memory)           MEMORY="$2"; shift 2 ;;
        --swap)             SWAP="$2"; shift 2 ;;
        --disk)             DISK="$2"; shift 2 ;;
        --storage)          STORAGE="$2"; shift 2 ;;
        --template-storage) TEMPLATE_STORAGE="$2"; shift 2 ;;
        --bridge)           BRIDGE="$2"; shift 2 ;;
        --ip)               IPCONF="$2"; shift 2 ;;
        --gateway)          GATEWAY="$2"; shift 2 ;;
        --source)           SRC_DIR="$2"; shift 2 ;;
        --git-ref)          GIT_REF="$2"; shift 2 ;;
        --no-destroy-on-fail) DESTROY_ON_FAIL=0; shift ;;
        -h|--help)          sed -n '2,24p' "$0"; exit 0 ;;
        *)                  die "Unknown argument: $1" ;;
    esac
done

# ── Preflight ────────────────────────────────────────────────────────────────

[ "$(id -u)" -eq 0 ] || die "Must run as root on the Proxmox VE host."
command -v pct    >/dev/null || die "'pct' not found — run this on a Proxmox VE host, not inside a container."
command -v pveam  >/dev/null || die "'pveam' not found."

if [ "$IPCONF" != "dhcp" ] && [ -z "$GATEWAY" ]; then
    die "--ip requires --gateway."
fi

if [ -z "$GIT_REF" ] && [ -z "$SRC_DIR" ]; then
    SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
if [ -n "$SRC_DIR" ] && [ ! -f "$SRC_DIR/lxc/install.sh" ]; then
    die "No installer at $SRC_DIR/lxc/install.sh. Pass --source /path/to/repo or --git-ref main."
fi

[ -n "$CTID" ] || CTID=$(pvesh get /cluster/nextid)
pct status "$CTID" >/dev/null 2>&1 && die "Container $CTID already exists."

# ── Storage selection ────────────────────────────────────────────────────────

# 'rootdir' storages can hold container volumes; 'vztmpl' storages hold templates.
pick_storage() {
    local content="$1"
    pvesm status --content "$content" 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}'
}

[ -n "$STORAGE" ]          || STORAGE=$(pick_storage rootdir)
[ -n "$TEMPLATE_STORAGE" ] || TEMPLATE_STORAGE=$(pick_storage vztmpl)
[ -n "$STORAGE" ]          || die "No active storage with content type 'rootdir'. Pass --storage."
[ -n "$TEMPLATE_STORAGE" ] || die "No active storage with content type 'vztmpl'. Pass --template-storage."

# ── Template ─────────────────────────────────────────────────────────────────

msg "Resolving the Ubuntu 24.04 template..."
pveam update >/dev/null 2>&1 || warn "'pveam update' failed — using the cached template index."

TEMPLATE=$(pveam available --section system \
    | awk '{print $2}' \
    | grep -E '^ubuntu-24\.04-standard' \
    | sort -V | tail -1)
[ -n "$TEMPLATE" ] || die "ubuntu-24.04-standard is not offered by 'pveam available'. Check the host's template index."

if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
    msg "Downloading $TEMPLATE to $TEMPLATE_STORAGE..."
    pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi
ok "Template: $TEMPLATE"

# ── Create ───────────────────────────────────────────────────────────────────

if [ "$IPCONF" = "dhcp" ]; then
    NETCONF="name=eth0,bridge=${BRIDGE},ip=dhcp,ip6=auto"
else
    NETCONF="name=eth0,bridge=${BRIDGE},ip=${IPCONF},gw=${GATEWAY},ip6=auto"
fi

msg "Creating container $CTID ($HOSTNAME_): ${CORES} cores, ${MEMORY} MB RAM, ${DISK} GB on $STORAGE"
pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
    --hostname "$HOSTNAME_" \
    --cores "$CORES" \
    --memory "$MEMORY" \
    --swap "$SWAP" \
    --rootfs "${STORAGE}:${DISK}" \
    --net0 "$NETCONF" \
    --unprivileged 1 \
    --features nesting=1 \
    --onboot 1 \
    --ostype ubuntu \
    --description "UniFi Insights Plus — see /etc/unifi-insights-plus.env"

cleanup_on_fail() {
    local rc=$?
    if [ "$rc" -ne 0 ] && [ "$DESTROY_ON_FAIL" = "1" ]; then
        warn "Provisioning failed — destroying container $CTID. Pass --no-destroy-on-fail to keep it."
        pct stop "$CTID" >/dev/null 2>&1 || true
        pct destroy "$CTID" >/dev/null 2>&1 || true
    fi
    exit $rc
}
trap cleanup_on_fail EXIT

msg "Starting container $CTID..."
pct start "$CTID"

# Wait for systemd inside the container, then for working DNS/routing — apt and
# the NodeSource/MaxMind downloads all need both.
msg "Waiting for the container to come up..."
for _ in $(seq 1 60); do
    pct exec "$CTID" -- test -d /run/systemd/system >/dev/null 2>&1 && break
    sleep 2
done
pct exec "$CTID" -- test -d /run/systemd/system >/dev/null 2>&1 \
    || die "systemd did not start inside container $CTID."

for _ in $(seq 1 60); do
    pct exec "$CTID" -- getent hosts archive.ubuntu.com >/dev/null 2>&1 && break
    sleep 2
done
pct exec "$CTID" -- getent hosts archive.ubuntu.com >/dev/null 2>&1 \
    || die "No working DNS inside container $CTID. Check the bridge, DHCP, or pass --ip/--gateway."
ok "Container is up."

# ── Deliver the source ───────────────────────────────────────────────────────

CT_SRC=/opt/uip-src

if [ -n "$GIT_REF" ]; then
    msg "Cloning $REPO_URL ($GIT_REF) inside the container..."
    pct exec "$CTID" -- bash -c "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq --no-install-recommends git ca-certificates"
    pct exec "$CTID" -- bash -c "rm -rf $CT_SRC && git clone --depth 1 --branch '$GIT_REF' '$REPO_URL' $CT_SRC"
else
    msg "Copying $SRC_DIR into the container..."
    TARBALL=$(mktemp /tmp/uip-src-XXXXXX.tar.gz)
    tar -czf "$TARBALL" \
        --exclude='.git' \
        --exclude='node_modules' \
        --exclude='ui/dist' \
        --exclude='__pycache__' \
        -C "$SRC_DIR" .
    pct exec "$CTID" -- mkdir -p "$CT_SRC"
    pct push "$CTID" "$TARBALL" /tmp/uip-src.tar.gz
    pct exec "$CTID" -- tar -xzf /tmp/uip-src.tar.gz -C "$CT_SRC"
    pct exec "$CTID" -- rm -f /tmp/uip-src.tar.gz
    rm -f "$TARBALL"
fi
ok "Source delivered to $CT_SRC."

# ── Install ──────────────────────────────────────────────────────────────────

msg "Running the installer inside the container (several minutes: apt, pip, UI build)..."
pct exec "$CTID" -- chmod +x "$CT_SRC/lxc/install.sh"
pct exec "$CTID" -- "$CT_SRC/lxc/install.sh" --source "$CT_SRC"

trap - EXIT

# ── Summary ──────────────────────────────────────────────────────────────────

CT_IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')

echo
ok "Container $CTID ($HOSTNAME_) is ready."
echo
echo "  Dashboard        http://${CT_IP:-unknown}:8000"
echo "  Syslog target    ${CT_IP:-unknown}  UDP 514"
echo "  Configuration    pct exec $CTID -- nano /etc/unifi-insights-plus.env"
echo "  Logs             pct exec $CTID -- journalctl -u uip-api -u uip-receiver -f"
echo "  Shell            pct enter $CTID"
echo
echo "  Next: point your UniFi gateway's remote syslog at ${CT_IP:-the container IP}:514,"
echo "  then add your MaxMind and AbuseIPDB credentials to the configuration file"
echo "  and run: pct exec $CTID -- systemctl restart uip-api uip-receiver"
echo
