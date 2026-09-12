# Proxmox LXC Port — Design

Date: 2026-09-12
Status: approved

## Goal

Run UniFi Insights Plus natively inside a Proxmox LXC container, without Docker,
with the same behaviour and the same on-disk layout as the published image.

Two entry points:

1. `lxc/proxmox-lxc.sh` — runs on the **Proxmox VE host**, creates the container and
   provisions it end to end.
2. `lxc/install.sh` — runs **inside** any Ubuntu 24.04 container (or VM/bare metal),
   for users who prefer to create the container themselves.

## Constraint that drives the layout

The application hardcodes absolute `/app` paths:

| Path | Source |
|---|---|
| `/app/static` | `receiver/api.py:308` (`STATIC_DIR`) |
| `/app/maxmind` | `receiver/enrichment.py:227` (default `db_dir`) |
| `/app/maxmind/GeoLite2-City.mmdb` | `receiver/routes/health.py:52` |
| `/app/VERSION` | `receiver/deps.py:27` |
| `/app/main.py` | `receiver/deps.py:155` (`pkill -SIGUSR2 -f /app/main.py`) |

The port therefore installs into `/app`, mirroring the Docker image exactly. **No
application source changes are required**, which keeps the LXC path in sync with
upstream releases for free.

## Base OS

**Ubuntu 24.04 LTS** (`ubuntu-24.04-standard` LXC template), unprivileged.

Rationale: `postgresql-16` is in the noble archive, matching `Dockerfile` stage 2
(`FROM ubuntu:24.04` + `apt-get install postgresql-16`). Debian 12 ships PostgreSQL 15
and would require the PGDG repository — more moving parts for no benefit.

## Filesystem layout

```
/app/                          application root (mirrors the container image)
  main.py, api.py, db.py, ...  from receiver/
  data/                        IANA service CSV (services.py resolves it relative to __file__)
  venv/                        Python virtualenv
  static/                      built React UI (ui/dist)
  maxmind/                     GeoLite2 databases
  VERSION
  init.sql
  geoip-update.sh              upstream script, unmodified
  geoip-refresh.sh             LXC wrapper: renders /etc/GeoIP.conf, then calls the above
/etc/unifi-insights-plus.env   configuration, consumed via systemd EnvironmentFile
```

PostgreSQL uses the distribution cluster (`/var/lib/postgresql/16/main`) rather than the
image's custom `PGDATA`, so `pg_ctlcluster`, `pg_upgradecluster` and unattended security
updates keep working.

## Process supervision: supervisord → systemd

| `supervisord.conf` | LXC unit |
|---|---|
| `[program:postgresql]` | `postgresql@16-main.service` (distribution unit) |
| `[program:receiver]` | `uip-receiver.service` |
| `[program:api]` | `uip-api.service` |
| `[program:cron]` + `/etc/cron.d/geoipupdate` | `uip-geoip.timer` → `uip-geoip.service` |

The geoipupdate schedule from `entrypoint.sh` (`0 7 * * 3,6`) becomes
`OnCalendar=Wed,Sat 07:00 UTC` with `Persistent=true`, so a container that was powered off
at the scheduled time catches up on next boot — an improvement over plain cron.

## Service accounts and capabilities

Both application units run as the system user `uip`.

- `uip-receiver` binds UDP 514, granted via `AmbientCapabilities=CAP_NET_BIND_SERVICE`
  (`CapabilityBoundingSet` restricted to the same), so no root is needed.
- Both units must share one UID: `deps.py:155` signals the receiver with
  `pkill -SIGUSR2 -f /app/main.py` from the API process.
- `uip-geoip.service` runs as root because it renders `/etc/GeoIP.conf` from the
  environment file; it chowns `/app/maxmind` back to `uip` afterwards, and its
  `kill -USR1` to the receiver works from root.

## Hazards addressed explicitly

- **`PrivateTmp=false` is mandatory.** The API and receiver communicate through
  `/tmp/config_update_requested` (`deps.py:157`) and `/tmp/abuseipdb_stats.json`
  (`enrichment.py:124`). systemd's per-unit `/tmp` would sever this silently — config
  reloads and AbuseIPDB stats would stop working with no error.
- **IPv6 must be enabled in the container.** `main.py:111` binds `('::', 514)` for
  dual-stack receive. The installer fails loudly if `net.ipv6.conf.all.disable_ipv6=1`.
- **PostgreSQL tuning is a `conf.d` drop-in**, not an append to `postgresql.conf`, so it
  survives package upgrades. Values are copied verbatim from `entrypoint.sh`
  (`shared_buffers=128MB`, `work_mem=8MB`, `synchronous_commit=off`, …).
- **`shm_size`**: the compose file requests 256 MB; LXC mounts `/dev/shm` sized from the
  container's memory limit, which at the recommended 4 GB exceeds that. No action needed,
  but the installer reports the effective size.
- **Table ownership transfers** from `entrypoint.sh` are replicated, otherwise the `unifi`
  role cannot run the application's `ALTER TABLE` migrations.
- **External database mode** (`DB_HOST` set to a non-localhost value): the installer skips
  cluster setup entirely and leaves `postgresql` disabled, matching the `sed` that
  `entrypoint.sh` applies to the supervisord config.

## UI build

Node 20 is installed from NodeSource, `npm ci && npm run build` runs in `/opt/uip-src/ui`,
`dist/` is copied to `/app/static`, and Node is removed again unless `--keep-node` is
passed. This keeps the runtime image small and avoids depending on release artifacts.

Peak build requirement is roughly 1 GB RAM; the installer checks available memory first.

## Updates

`lxc/update.sh` refreshes the source tree (git pull, or a release tarball), rebuilds the
UI, syncs Python dependencies, re-applies unit files, and restarts the services. The
database is untouched — the application performs its own schema migrations on start.

## Out of scope

- Migrating an existing Docker `pgdata` volume into the LXC (documented as a manual
  `pg_dump`/`pg_restore` in `lxc/README.md`).
- Privileged containers, LXC-on-non-Proxmox, and Debian hosts.
