#!/usr/bin/env bash
#
# UniFi Insights Plus — create and provision a Proxmox LXC container.
#
# Run this on the PROXMOX VE HOST as root. It creates an unprivileged Ubuntu
# 24.04 container, copies this repository into it, and runs lxc/install.sh.
#
#   ./lxc/proxmox-lxc.sh [options]
#
# With no options it opens a setup dialog. Any option given on the command line
# is used as-is and pre-fills the dialog.
#
#   --defaults          skip the dialog, use the defaults below
#   --ctid N            container ID          (default: next free)
#   --hostname NAME     container hostname    (default: unifi-insights)
#   --cores N           CPU cores             (default: 4)
#   --memory MB         RAM in MB             (default: 4096)
#   --swap MB           swap in MB            (default: 512)
#   --disk GB           root disk in GB       (default: 16)
#   --storage NAME      rootfs storage        (default: asked / auto-detected)
#   --template-storage NAME  template storage (default: asked / auto-detected)
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
INTERACTIVE=auto
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
        --defaults)         INTERACTIVE=no; shift ;;
        --no-destroy-on-fail) DESTROY_ON_FAIL=0; shift ;;
        -h|--help)          sed -n '2,28p' "$0"; exit 0 ;;
        *)                  die "Unknown argument: $1" ;;
    esac
done

# ── Preflight ────────────────────────────────────────────────────────────────

[ "$(id -u)" -eq 0 ] || die "Must run as root on the Proxmox VE host."
command -v pct    >/dev/null || die "'pct' not found — run this on a Proxmox VE host, not inside a container."
command -v pveam  >/dev/null || die "'pveam' not found."

if [ -z "$GIT_REF" ] && [ -z "$SRC_DIR" ]; then
    SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
if [ -n "$SRC_DIR" ] && [ ! -f "$SRC_DIR/lxc/install.sh" ]; then
    die "No installer at $SRC_DIR/lxc/install.sh. Pass --source /path/to/repo or --git-ref main."
fi

# ── Host inventory ───────────────────────────────────────────────────────────

# 'rootdir' storages hold container volumes, 'vztmpl' storages hold templates.
# Columns of `pvesm status`: Name Type Status Total Used Available %  (in KiB)
list_storages() {
    pvesm status --content "$1" 2>/dev/null | awk 'NR>1 && $3=="active" {print $1, $2, $6}'
}
pick_storage() { list_storages "$1" | awk 'NR==1 {print $1; exit}'; }
list_bridges() {
    local iface
    for iface in /sys/class/net/vmbr*; do
        [ -e "$iface" ] || continue
        basename "$iface"
    done
}

CTID_GIVEN=$([ -n "$CTID" ] && echo 1 || echo "")
[ -n "$CTID" ] || CTID=$(pvesh get /cluster/nextid)

# ── Validation ───────────────────────────────────────────────────────────────

valid_cidr() { echo "$1" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$'; }
valid_ipv4() { echo "$1" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; }
valid_int()  { echo "$1" | grep -qE '^[1-9][0-9]*$'; }

check_network_args() {
    [ "$IPCONF" = "dhcp" ] && return 0
    [ -n "$GATEWAY" ] || die "A static IP requires a gateway (--gateway)."
    # pct rejects a bare address: the prefix length is part of the net0 format.
    valid_cidr "$IPCONF"  || die "The IP must be in CIDR notation, e.g. 192.168.1.50/24 (got: $IPCONF)."
    valid_ipv4 "$GATEWAY" || die "The gateway must be a plain IPv4 address (got: $GATEWAY)."
}

# ── Setup dialog ─────────────────────────────────────────────────────────────

if [ "$INTERACTIVE" = "auto" ]; then
    if [ -t 0 ] && [ -t 1 ] && command -v whiptail >/dev/null 2>&1; then
        INTERACTIVE=yes
    else
        INTERACTIVE=no
    fi
fi

# whiptail draws the UI on stdout and returns the value on stderr; swap them.
wt() { whiptail --backtitle "UniFi Insights Plus — Proxmox LXC" "$@" 3>&1 1>&2 2>&3; }

ask_text() {   # <title> <prompt> <default>
    wt --title "$1" --inputbox "$2" 11 74 "$3"
}

ask_int() {    # <title> <prompt> <default> — re-asks until the answer is a number
    local value
    while :; do
        value=$(ask_text "$1" "$2" "$3") || return 1
        valid_int "$value" && { echo "$value"; return 0; }
        wt --title "Invalid" --msgbox "'$value' is not a positive number." 9 60 || true
    done
}

ask_storage() {  # <content-type> <title> <prompt>
    local entries=() name type avail
    while read -r name type avail; do
        [ -n "$name" ] || continue
        entries+=("$name" "$(printf '%-9s %8.1f GiB free' "$type" "$(awk -v k="$avail" 'BEGIN{print k/1048576}')")")
    done < <(list_storages "$1")

    [ "${#entries[@]}" -gt 0 ] || die "No active storage with content type '$1'."
    if [ "${#entries[@]}" -eq 2 ]; then   # only one candidate, nothing to choose
        echo "${entries[0]}"; return 0
    fi
    local count=$(( ${#entries[@]} / 2 ))
    wt --title "$2" --menu "$3" $(( count + 10 )) 74 "$count" "${entries[@]}"
}

ask_bridge() {
    local entries=() b addr
    while read -r b; do
        [ -n "$b" ] || continue
        addr=$(ip -4 -brief addr show "$b" 2>/dev/null | awk '{print $3}' | head -1)
        entries+=("$b" "${addr:-no address}")
    done < <(list_bridges)
    [ "${#entries[@]}" -gt 0 ] || { echo "$BRIDGE"; return 0; }
    if [ "${#entries[@]}" -eq 2 ]; then
        echo "${entries[0]}"; return 0
    fi
    local count=$(( ${#entries[@]} / 2 ))
    wt --title "Network bridge" --menu "Which bridge should the container attach to?" \
       $(( count + 10 )) 74 "$count" "${entries[@]}"
}

ask_network() {
    [ "$IPCONF" = "dhcp" ] || return 0   # already given on the command line

    # A static address is worth two prompts: the UniFi gateway sends syslog to a
    # fixed address, and a DHCP lease can move it.
    wt --title "IP address" --yesno \
"Assign a static IP address?

Recommended. Your UniFi gateway sends syslog to a fixed
address, and a DHCP lease can move the container out from
under it.

Choose No for DHCP." 15 74 || return 0

    while :; do
        IPCONF=$(ask_text "IP address" "Address in CIDR notation — the prefix length is required:\n\n    192.168.1.50/24" "") || return 1
        valid_cidr "$IPCONF" && break
        wt --title "Invalid" --msgbox "'$IPCONF' is not CIDR notation.\n\nInclude the prefix length, for example 192.168.1.50/24" 11 66 || true
    done
    while :; do
        GATEWAY=$(ask_text "Gateway" "Gateway address:" \
            "$(echo "$IPCONF" | sed 's#\([0-9]*\.[0-9]*\.[0-9]*\)\..*#\1.1#')") || return 1
        valid_ipv4 "$GATEWAY" && break
        wt --title "Invalid" --msgbox "'$GATEWAY' is not an IPv4 address." 9 60 || true
    done
}

run_wizard() {
    local mode
    mode=$(wt --title "Setup" --menu \
"Creates an unprivileged Ubuntu 24.04 container and installs
UniFi Insights Plus into it.

Defaults: ${CORES} cores, ${MEMORY} MB RAM, ${DISK} GB disk." \
        17 74 2 \
        "default"  "Defaults — ask only about storage and network" \
        "advanced" "Set every value myself") || die "Cancelled."

    if [ "$mode" = "advanced" ]; then
        [ -n "$CTID_GIVEN" ] || CTID=$(ask_int "Container ID" "Numeric ID for the new container:" "$CTID") || die "Cancelled."
        HOSTNAME_=$(ask_text "Hostname" "Hostname for the container:" "$HOSTNAME_") || die "Cancelled."
        CORES=$(ask_int "CPU cores" "PostgreSQL, the receiver and the API run concurrently.\n4 cores is the realistic floor." "$CORES") || die "Cancelled."
        MEMORY=$(ask_int "Memory" "RAM in MB. The UI build alone peaks near 1 GB;\n4096 is the realistic floor." "$MEMORY") || die "Cancelled."
        SWAP=$(ask_int "Swap" "Swap in MB:" "$SWAP") || die "Cancelled."
        DISK=$(ask_int "Disk" "Root disk in GB. Retained logs live here — 16 GB covers\na small network at the default retention." "$DISK") || die "Cancelled."
    fi

    # Storage is asked in both modes. The auto-pick is merely the first active
    # candidate, which is rarely where you want a write-heavy database.
    if [ -z "$STORAGE" ]; then
        STORAGE=$(ask_storage rootdir "Root disk storage" \
"Where should the container's disk live?

This holds PostgreSQL and grows with log retention, so prefer
local storage over a network share.") || die "Cancelled."
    fi
    if [ -z "$TEMPLATE_STORAGE" ]; then
        TEMPLATE_STORAGE=$(ask_storage vztmpl "Template storage" \
"Where should the Ubuntu 24.04 template be cached?") || die "Cancelled."
    fi

    if [ "$mode" = "advanced" ]; then
        BRIDGE=$(ask_bridge) || die "Cancelled."
    fi

    ask_network || die "Cancelled."

    pct status "$CTID" >/dev/null 2>&1 && die "Container $CTID already exists."

    local net_summary
    if [ "$IPCONF" = "dhcp" ]; then
        net_summary="DHCP on $BRIDGE"
    else
        net_summary="$IPCONF via $GATEWAY on $BRIDGE"
    fi

    wt --title "Confirm" --yesno \
"  ID           $CTID
  Hostname     $HOSTNAME_
  Resources    ${CORES} cores · ${MEMORY} MB RAM · ${SWAP} MB swap
  Root disk    ${DISK} GB on ${STORAGE}
  Template     ${TEMPLATE_STORAGE}
  Network      ${net_summary}

Create the container and install? Takes 5-10 minutes." 18 74 || die "Cancelled."
}

if [ "$INTERACTIVE" = "yes" ]; then
    run_wizard
fi

check_network_args

[ -n "$STORAGE" ]          || STORAGE=$(pick_storage rootdir)
[ -n "$TEMPLATE_STORAGE" ] || TEMPLATE_STORAGE=$(pick_storage vztmpl)
[ -n "$STORAGE" ]          || die "No active storage with content type 'rootdir'. Pass --storage."
[ -n "$TEMPLATE_STORAGE" ] || die "No active storage with content type 'vztmpl'. Pass --template-storage."

pct status "$CTID" >/dev/null 2>&1 && die "Container $CTID already exists."

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
    || die "No working DNS inside container $CTID. Check the bridge, DHCP, or the static IP and gateway."
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
