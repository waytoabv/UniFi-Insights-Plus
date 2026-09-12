#!/bin/bash
# LXC wrapper around the upstream geoip-update.sh.
#
# In the Docker image, entrypoint.sh writes /etc/GeoIP.conf once at container
# start. Under systemd there is no such start hook, and the credentials may have
# been edited in /etc/unifi-insights-plus.env since install time, so the config is
# rendered on every run before handing off to the unmodified upstream script.
set -e

LOG_PREFIX="[geoip-refresh]"

if [ -z "$MAXMIND_ACCOUNT_ID" ] || [ -z "$MAXMIND_LICENSE_KEY" ]; then
    echo "$LOG_PREFIX MaxMind credentials not set in /etc/unifi-insights-plus.env, skipping"
    exit 0
fi

cat > /etc/GeoIP.conf <<EOF
AccountID $MAXMIND_ACCOUNT_ID
LicenseKey $MAXMIND_LICENSE_KEY
EditionIDs GeoLite2-City GeoLite2-ASN
DatabaseDirectory /app/maxmind
EOF
chmod 0600 /etc/GeoIP.conf

mkdir -p /app/maxmind

rc=0
/app/geoip-update.sh || rc=$?

# geoipupdate runs as root here; hand the databases back to the service account
# so the receiver can read them after a hot reload.
chown -R uip:uip /app/maxmind
chmod 0644 /app/maxmind/*.mmdb 2>/dev/null || true

exit $rc
