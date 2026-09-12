#!/usr/bin/env bash
#
# UniFi Insights Plus — native installer for a Linux container (Proxmox LXC).
#
# Run this INSIDE an Ubuntu 24.04 container as root. It is the systemd equivalent
# of Dockerfile + entrypoint.sh + supervisord.conf.
#
#   ./lxc/install.sh [--source DIR] [--repo URL] [--ref REF]
#                    [--tz ZONE] [--keep-node] [--no-start]
#                    [--force-unsupported]
#
# Re-running the script is safe: it upgrades an existing installation and never
# touches an initialised database.
#
set -euo pipefail

APP_DIR=/app
SRC_DIR=""
ENV_FILE=/etc/unifi-insights-plus.env
SERVICE_USER=uip
PG_VERSION=16
PG_CONF_DIR="/etc/postgresql/${PG_VERSION}/main"
GEOIPUPDATE_VERSION=7.1.1
NODE_MAJOR=20
KEEP_NODE=0
START_SERVICES=1
FORCE_UNSUPPORTED=0
TZ_ARG=""
SRC_REPO=""
SRC_REF=""
SOURCE_FILE=/etc/unifi-insights-plus.source

msg()  { echo -e "\033[1;34m[install]\033[0m $*"; }
ok()   { echo -e "\033[1;32m[  ok  ]\033[0m $*"; }
warn() { echo -e "\033[1;33m[ warn ]\033[0m $*" >&2; }
die()  { echo -e "\033[1;31m[fatal ]\033[0m $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --source)    SRC_DIR="$2"; shift 2 ;;
        --repo)      SRC_REPO="$2"; shift 2 ;;
        --ref)       SRC_REF="$2"; shift 2 ;;
        --keep-node) KEEP_NODE=1; shift ;;
        --no-start)  START_SERVICES=0; shift ;;
        --tz)        TZ_ARG="$2"; shift 2 ;;
        --force-unsupported) FORCE_UNSUPPORTED=1; shift ;;
        -h|--help)   sed -n '2,15p' "$0"; exit 0 ;;
        *)           die "Unknown argument: $1" ;;
    esac
done

# Default source is the repository this script lives in.
if [ -z "$SRC_DIR" ]; then
    SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

# ── Preflight ────────────────────────────────────────────────────────────────

[ "$(id -u)" -eq 0 ] || die "Must run as root."
[ -d /run/systemd/system ] || die "systemd is not running — this installer targets a systemd container."

for f in receiver/main.py receiver/requirements.txt init.sql VERSION ui/package.json; do
    [ -e "$SRC_DIR/$f" ] || die "Source tree incomplete: $SRC_DIR/$f not found. Pass --source /path/to/repo."
done

# This installer belongs INSIDE a container. On a Proxmox VE node it would write
# PostgreSQL, a virtualenv, Node and systemd units onto the hypervisor itself.
if [ "$FORCE_UNSUPPORTED" = "0" ] && { [ -d /etc/pve ] || command -v pveversion >/dev/null 2>&1; }; then
    echo >&2
    die "This looks like a Proxmox VE host, not a container.

  install.sh runs INSIDE the container. To create one and install into it:

      ./lxc/proxmox-lxc.sh

  To update a container that already exists:

      ./lxc/proxmox-update.sh <ctid>

  If you really mean to install onto this machine, pass --force-unsupported."
fi

if [ -r /etc/os-release ]; then
    . /etc/os-release
    if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "24.04" ]; then
        if [ "$FORCE_UNSUPPORTED" = "1" ]; then
            warn "Expected Ubuntu 24.04; found ${PRETTY_NAME:-unknown}. Continuing because --force-unsupported was given."
            warn "apt must be able to resolve postgresql-${PG_VERSION}, or this will fail."
        else
            die "Expected Ubuntu 24.04, found ${PRETTY_NAME:-unknown}.

  postgresql-${PG_VERSION} is in the Ubuntu 24.04 archive, matching the Docker
  image. Debian 13 ships PostgreSQL 17 and Debian 12 ships 15, so apt here
  cannot resolve the package this installer asks for.

  Create an Ubuntu 24.04 container, or pass --force-unsupported if you have
  arranged for postgresql-${PG_VERSION} to be installable (PGDG, for example)."
        fi
    fi
fi

# main.py binds ('::', 514) for dual-stack receive. Without IPv6 the bind fails at
# startup and no syslog is ever ingested, so fail here rather than in a log file.
if [ "$(sysctl -n net.ipv6.conf.all.disable_ipv6 2>/dev/null || echo 0)" = "1" ]; then
    die "IPv6 is disabled in this container, but the syslog receiver binds '::' (receiver/main.py). Enable IPv6 or set net.ipv6.conf.all.disable_ipv6=0."
fi

TOTAL_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
[ "$TOTAL_MB" -ge 2048 ] || warn "Only ${TOTAL_MB} MB RAM detected. The UI build needs ~1 GB and the app expects 4 GB; expect OOM kills."

msg "Source:      $SRC_DIR"
msg "Target:      $APP_DIR"
msg "Config:      $ENV_FILE"

# ── Packages ─────────────────────────────────────────────────────────────────

export DEBIAN_FRONTEND=noninteractive

msg "Installing system packages..."
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    ca-certificates curl gnupg tzdata rsync procps \
    python3 python3-venv \
    "postgresql-${PG_VERSION}" "postgresql-client-${PG_VERSION}"
ok "System packages installed."

# geoipupdate is not packaged in Ubuntu; same release the Dockerfile pins.
if ! command -v geoipupdate >/dev/null 2>&1; then
    msg "Installing geoipupdate ${GEOIPUPDATE_VERSION}..."
    ARCH=$(dpkg --print-architecture)
    curl -fsSL "https://github.com/maxmind/geoipupdate/releases/download/v${GEOIPUPDATE_VERSION}/geoipupdate_${GEOIPUPDATE_VERSION}_linux_${ARCH}.deb" \
        -o /tmp/geoipupdate.deb
    dpkg -i /tmp/geoipupdate.deb
    rm -f /tmp/geoipupdate.deb
    ok "geoipupdate installed."
fi

# ── Service account ──────────────────────────────────────────────────────────

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    msg "Creating system user '$SERVICE_USER'..."
    useradd --system --home-dir "$APP_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# ── Application files ────────────────────────────────────────────────────────

msg "Deploying application to $APP_DIR..."
mkdir -p "$APP_DIR" "$APP_DIR/maxmind"

# Mirrors the Dockerfile COPY layout. venv/, static/ and maxmind/ are produced
# here rather than copied, so they are excluded from the sync.
rsync -a --delete \
    --exclude 'tests/' \
    --exclude '__pycache__/' \
    --exclude 'pytest.ini' \
    --exclude 'requirements-test.txt' \
    --filter 'protect venv/' \
    --filter 'protect static/' \
    --filter 'protect maxmind/' \
    "$SRC_DIR/receiver/" "$APP_DIR/"

install -m 0644 "$SRC_DIR/VERSION"   "$APP_DIR/VERSION"
install -m 0644 "$SRC_DIR/init.sql"  "$APP_DIR/init.sql"
install -m 0755 "$SRC_DIR/geoip-update.sh"      "$APP_DIR/geoip-update.sh"
install -m 0755 "$SRC_DIR/lxc/geoip-refresh.sh" "$APP_DIR/geoip-refresh.sh"
install -m 0755 "$SRC_DIR/lxc/uip-api-start.sh" "$APP_DIR/uip-api-start.sh"
ok "Application files deployed ($(cat "$APP_DIR/VERSION"))."

# Record the origin of this install. proxmox-lxc.sh delivers a tarball without
# .git, so the container cannot work out on its own which repository to update
# from — and on a fork that matters, since upstream main carries no lxc/.
if [ -z "$SRC_REPO" ] && [ -d "$SRC_DIR/.git" ]; then
    SRC_REPO=$(git -C "$SRC_DIR" remote get-url origin 2>/dev/null || true)
    SRC_REF=${SRC_REF:-$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)}
fi
if [ -n "$SRC_REPO" ]; then
    printf 'UIP_REPO=%s\nUIP_REF=%s\n' "$SRC_REPO" "${SRC_REF:-main}" > "$SOURCE_FILE"
    chmod 0644 "$SOURCE_FILE"
fi

# ── Python virtualenv ────────────────────────────────────────────────────────

if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    msg "Creating Python virtualenv..."
    python3 -m venv "$APP_DIR/venv"
fi
msg "Installing Python dependencies..."
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet --no-cache-dir -r "$SRC_DIR/receiver/requirements.txt"
ok "Python dependencies installed."

# ── UI build ─────────────────────────────────────────────────────────────────

msg "Building the React UI (this takes a few minutes)..."
if ! command -v node >/dev/null 2>&1 || [ "$(node -v | sed 's/v\([0-9]*\).*/\1/')" -lt "$NODE_MAJOR" ]; then
    msg "Installing Node.js ${NODE_MAJOR} from NodeSource..."
    NODE_INSTALLED_HERE=1
    mkdir -p /etc/apt/keyrings
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list
    apt-get update -qq
    apt-get install -y -qq nodejs
else
    NODE_INSTALLED_HERE=0
fi

BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT
cp -a "$SRC_DIR/ui/." "$BUILD_DIR/"
rm -rf "$BUILD_DIR/node_modules"
(
    cd "$BUILD_DIR"
    if [ -f package-lock.json ]; then
        npm ci --no-audit --no-fund --loglevel=error
    else
        npm install --no-audit --no-fund --loglevel=error
    fi
    npm run build
)
rm -rf "$APP_DIR/static"
cp -a "$BUILD_DIR/dist" "$APP_DIR/static"
ok "UI built into $APP_DIR/static."

if [ "$NODE_INSTALLED_HERE" = "1" ] && [ "$KEEP_NODE" = "0" ]; then
    msg "Removing Node.js (pass --keep-node to keep it)..."
    apt-get remove -y -qq nodejs >/dev/null
    apt-get autoremove -y -qq >/dev/null
    rm -f /etc/apt/sources.list.d/nodesource.list
    apt-get update -qq
fi

# ── Configuration ────────────────────────────────────────────────────────────

# Syslog lines carry a bare local time with no zone, so the receiver has to
# interpret them in the gateway's zone. Defaulting to UTC — the placeholder in
# .env.example — silently shifts every timestamp for anyone not on UTC, so
# inherit the host's zone where we can. proxmox-lxc.sh passes the PVE node's.
if [ -z "$TZ_ARG" ]; then
    if command -v timedatectl >/dev/null 2>&1; then
        TZ_ARG=$(timedatectl show -p Timezone --value 2>/dev/null || true)
    fi
    [ -n "$TZ_ARG" ] || TZ_ARG=$(cat /etc/timezone 2>/dev/null || true)
    [ -n "$TZ_ARG" ] || TZ_ARG=UTC
fi
if [ ! -f "/usr/share/zoneinfo/$TZ_ARG" ]; then
    warn "Unknown timezone '$TZ_ARG'; falling back to UTC."
    TZ_ARG=UTC
fi

# Keep the container clock aligned with it too, so journal timestamps and the
# application's view of "now" agree.
ln -sf "/usr/share/zoneinfo/$TZ_ARG" /etc/localtime
echo "$TZ_ARG" > /etc/timezone

if [ ! -f "$ENV_FILE" ]; then
    msg "Generating $ENV_FILE (timezone: $TZ_ARG)..."
    GENERATED_PASSWORD=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
    GENERATED_SECRET=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
    cat > "$ENV_FILE" <<EOF
# UniFi Insights Plus — configuration
# Full reference: see .env.example in the repository.
# Apply changes with: systemctl restart uip-api uip-receiver

# Password of the local 'unifi' PostgreSQL role. Changing it here alone will not
# change it in PostgreSQL — use ALTER ROLE if you need to rotate it.
POSTGRES_PASSWORD=${GENERATED_PASSWORD}

# Encryption key for API keys stored in the database. Changing it after setup
# requires re-entering every API key in the UI.
SECRET_KEY=${GENERATED_SECRET}

# Must match your UniFi gateway's local time: syslog lines carry a bare local
# time with no zone, so a mismatch shifts every timestamp.
TZ=${TZ_ARG}

LOG_LEVEL=INFO

# Port the dashboard listens on (LXC-only setting; the image hardcodes 8000).
API_PORT=8000

# ── Optional integrations ──
ABUSEIPDB_API_KEY=
MAXMIND_ACCOUNT_ID=
MAXMIND_LICENSE_KEY=

UNIFI_HOST=
UNIFI_API_KEY=
UNIFI_SITE=default
UNIFI_VERIFY_SSL=true

# ── External PostgreSQL ──
# Set DB_HOST to a non-localhost value to use an external database. The local
# cluster is then left unused; re-run install.sh afterwards to disable it.
#DB_HOST=
#DB_PORT=5432
#DB_NAME=unifi_logs
#DB_USER=unifi
#DB_PASSWORD=
#DB_SSLMODE=require
EOF
    chmod 0640 "$ENV_FILE"
    chown root:"$SERVICE_USER" "$ENV_FILE"
    ok "Configuration written with a generated database password."
else
    ok "Keeping existing $ENV_FILE."
fi

set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

# ── PostgreSQL ───────────────────────────────────────────────────────────────

# Same localhost test as entrypoint.sh and db.py:is_external_db().
_db_host_lower=$(echo "${DB_HOST:-127.0.0.1}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')
EXTERNAL_DB=false
case "$_db_host_lower" in
    127.0.0.1|localhost|localhost.localdomain|::1|"") ;;
    *) EXTERNAL_DB=true ;;
esac

if [ "$EXTERNAL_DB" = "true" ]; then
    msg "External database mode (DB_HOST=$DB_HOST) — skipping the local cluster."
    systemctl disable --now postgresql "postgresql@${PG_VERSION}-main" >/dev/null 2>&1 || true
else
    msg "Configuring the local PostgreSQL ${PG_VERSION} cluster..."

    # Tuning from entrypoint.sh, as a conf.d drop-in so package upgrades keep it.
    mkdir -p "${PG_CONF_DIR}/conf.d"
    cat > "${PG_CONF_DIR}/conf.d/10-unifi-insights.conf" <<'EOF'
# UniFi Log Insight tuning — mirrors entrypoint.sh
listen_addresses = '127.0.0.1'
shared_buffers = 128MB
work_mem = 8MB
maintenance_work_mem = 64MB
effective_cache_size = 256MB
wal_buffers = 8MB
checkpoint_completion_target = 0.9
max_wal_size = 512MB
synchronous_commit = off
EOF
    chown postgres:postgres "${PG_CONF_DIR}/conf.d/10-unifi-insights.conf"

    systemctl enable --now postgresql >/dev/null 2>&1 || true
    systemctl restart "postgresql@${PG_VERSION}-main"

    for _ in $(seq 1 30); do
        su - postgres -c "psql -tAc 'SELECT 1'" >/dev/null 2>&1 && break
        sleep 1
    done
    su - postgres -c "psql -tAc 'SELECT 1'" >/dev/null 2>&1 \
        || die "PostgreSQL did not become ready. Check: journalctl -u postgresql@${PG_VERSION}-main"

    DB_NAME_LOCAL=${DB_NAME:-unifi_logs}
    DB_USER_LOCAL=${DB_USER:-unifi}

    role_exists=$(su - postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='${DB_USER_LOCAL}'\"")
    if [ "$role_exists" != "1" ]; then
        msg "Creating role '${DB_USER_LOCAL}'..."
        su - postgres -c "psql -v ON_ERROR_STOP=1 -c \"CREATE USER ${DB_USER_LOCAL} WITH PASSWORD '${POSTGRES_PASSWORD}';\""
    fi

    db_exists=$(su - postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='${DB_NAME_LOCAL}'\"")
    if [ "$db_exists" != "1" ]; then
        # Encoding must be stated explicitly. A container image carries no UTF-8
        # locale, so postgresql-common initialises the cluster as SQL_ASCII and
        # template1 inherits it. Any non-ASCII byte — a UniFi network named
        # "Gäste", a device with an accent — then fails the insert outright with
        # "Unicode escape value could not be translated". template0 is used
        # because a database may only deviate from the template's encoding when
        # copying from template0.
        msg "Creating database '${DB_NAME_LOCAL}' (UTF8) and applying init.sql..."
        su - postgres -c "psql -v ON_ERROR_STOP=1 -c \"CREATE DATABASE ${DB_NAME_LOCAL} OWNER ${DB_USER_LOCAL} ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8' TEMPLATE template0;\""
        su - postgres -c "psql -v ON_ERROR_STOP=1 -d ${DB_NAME_LOCAL} -f ${APP_DIR}/init.sql"
        ok "Database initialised."
    else
        ok "Database '${DB_NAME_LOCAL}' already exists — keeping its contents."
    fi

    # Encoding cannot be altered in place, so an existing non-UTF8 database can
    # only be reported, not repaired.
    DB_ENCODING=$(su - postgres -c "psql -tAc \"SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname='${DB_NAME_LOCAL}'\"")
    if [ "$DB_ENCODING" != "UTF8" ]; then
        warn "Database '${DB_NAME_LOCAL}' has encoding ${DB_ENCODING}, not UTF8."
        warn "Non-ASCII names (VLANs, devices, hostnames) will fail to insert."
        warn "Encoding is fixed at creation time. To convert, dump and reload:"
        warn "    systemctl stop uip-api uip-receiver"
        warn "    sudo -u postgres pg_dump -Fc ${DB_NAME_LOCAL} > /root/uip.dump"
        warn "    sudo -u postgres dropdb ${DB_NAME_LOCAL}"
        warn "    sudo -u postgres psql -c \"CREATE DATABASE ${DB_NAME_LOCAL} OWNER ${DB_USER_LOCAL} ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8' TEMPLATE template0;\""
        warn "    sudo -u postgres pg_restore -d ${DB_NAME_LOCAL} /root/uip.dump"
        warn "    then re-run this installer"
    fi

    # Privileges are re-applied on every run, not just on a fresh database.
    #
    # init.sql runs as the postgres superuser, so every object it creates is owned
    # by postgres. The application then issues its own ALTER TABLE / CREATE INDEX
    # migrations as the app role, which PostgreSQL refuses for objects it does not
    # own — the API logs these as "Migration skipped (insufficient privilege)".
    # entrypoint.sh hands over a hardcoded list of five tables and misses the rest
    # (sessions, api_tokens, audit_log, users, roles, threat_backfill_queue), so
    # the whole public schema is transferred here instead. Running this every time
    # also repairs installations made before this block existed, and covers tables
    # that a later release adds to init.sql.
    msg "Applying privileges and object ownership to '${DB_USER_LOCAL}'..."
    GRANTS_SQL=$(mktemp /tmp/uip-grants-XXXXXX.sql)
    chmod 0644 "$GRANTS_SQL"
    cat > "$GRANTS_SQL" <<EOF
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO ${DB_USER_LOCAL};
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO ${DB_USER_LOCAL};
GRANT CREATE, USAGE ON SCHEMA public TO ${DB_USER_LOCAL};

DO \$\$
DECLARE
    obj record;
BEGIN
    FOR obj IN SELECT tablename AS name FROM pg_tables WHERE schemaname = 'public' LOOP
        EXECUTE format('ALTER TABLE public.%I OWNER TO %I', obj.name, '${DB_USER_LOCAL}');
    END LOOP;
    FOR obj IN SELECT sequencename AS name FROM pg_sequences WHERE schemaname = 'public' LOOP
        EXECUTE format('ALTER SEQUENCE public.%I OWNER TO %I', obj.name, '${DB_USER_LOCAL}');
    END LOOP;
    FOR obj IN SELECT viewname AS name FROM pg_views WHERE schemaname = 'public' LOOP
        EXECUTE format('ALTER VIEW public.%I OWNER TO %I', obj.name, '${DB_USER_LOCAL}');
    END LOOP;
END
\$\$;
EOF
    su - postgres -c "psql -v ON_ERROR_STOP=1 -d ${DB_NAME_LOCAL} -f $GRANTS_SQL" >/dev/null
    rm -f "$GRANTS_SQL"
    ok "Privileges applied."

    # routes/health.py reads disk usage from the image's PGDATA path. The
    # distribution cluster lives elsewhere, so point the old path at it rather
    # than patch the application.
    if [ ! -e /var/lib/postgresql/data ]; then
        ln -s "/var/lib/postgresql/${PG_VERSION}/main" /var/lib/postgresql/data
    fi
fi

# ── Ownership ────────────────────────────────────────────────────────────────

chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
# Scripts invoked by the root-run geoip unit stay root-owned.
chown root:root "$APP_DIR/geoip-update.sh" "$APP_DIR/geoip-refresh.sh"

# ── systemd units ────────────────────────────────────────────────────────────

msg "Installing systemd units..."
install -m 0644 "$SRC_DIR/lxc/systemd/uip-receiver.service" /etc/systemd/system/
install -m 0644 "$SRC_DIR/lxc/systemd/uip-api.service"      /etc/systemd/system/
install -m 0644 "$SRC_DIR/lxc/systemd/uip-geoip.service"    /etc/systemd/system/
install -m 0644 "$SRC_DIR/lxc/systemd/uip-geoip.timer"      /etc/systemd/system/
systemctl daemon-reload
systemctl enable uip-receiver.service uip-api.service uip-geoip.timer >/dev/null
ok "Units installed and enabled."

# ── GeoIP seed ───────────────────────────────────────────────────────────────

if [ -n "${MAXMIND_ACCOUNT_ID:-}" ] && [ -n "${MAXMIND_LICENSE_KEY:-}" ]; then
    if [ ! -f "$APP_DIR/maxmind/GeoLite2-City.mmdb" ]; then
        msg "Downloading GeoLite2 databases..."
        "$APP_DIR/geoip-refresh.sh" || warn "GeoLite2 download failed — check the credentials in $ENV_FILE."
    fi
else
    warn "MAXMIND_ACCOUNT_ID / MAXMIND_LICENSE_KEY are unset — GeoIP enrichment stays disabled until you set them in $ENV_FILE."
fi

# ── Start ────────────────────────────────────────────────────────────────────

if [ "$START_SERVICES" = "1" ]; then
    msg "Starting services..."
    systemctl restart uip-receiver.service uip-api.service
    systemctl start uip-geoip.timer

    for _ in $(seq 1 30); do
        if curl -fsS "http://127.0.0.1:${API_PORT:-8000}/api/health" >/dev/null 2>&1; then
            HEALTH_OK=1; break
        fi
        sleep 2
    done
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
SHM=$(df -h /dev/shm --output=size 2>/dev/null | tail -1 | tr -d ' ')

echo
ok "UniFi Insights Plus $(cat "$APP_DIR/VERSION") installed."
echo
echo "  Dashboard   http://${IP:-<container-ip>}:${API_PORT:-8000}"
echo "  Syslog      UDP ${IP:-<container-ip>}:514  (point your UniFi gateway here)"
echo "  Config      $ENV_FILE"
echo "  Logs        journalctl -u uip-api -u uip-receiver -f"
echo "  /dev/shm    ${SHM:-unknown} (compose asks for 256M)"
echo
if [ "${START_SERVICES}" = "1" ] && [ -z "${HEALTH_OK:-}" ]; then
    warn "The health endpoint did not respond yet. Inspect: journalctl -u uip-api -n 50"
fi
