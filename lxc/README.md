# UniFi Insights Plus in a Proxmox LXC container

A native, Docker-free installation for Proxmox VE. Everything the image does —
PostgreSQL 16, the syslog receiver, the API, the dashboard, and the scheduled
GeoLite2 update — runs under systemd inside an unprivileged Ubuntu 24.04 container.

## Quick start (Proxmox host)

```bash
git clone https://github.com/jmasarweh/UniFi-Insights-Plus.git
cd UniFi-Insights-Plus
./lxc/proxmox-lxc.sh
```

A setup dialog opens. Pick **Defaults** to answer only the two questions that
depend on your host — which storage the container's disk goes on, and whether it
gets a static address — or **Advanced** to set container ID, hostname, cores,
memory, swap, disk size, and bridge yourself. A summary appears before anything
is created.

The script then downloads the Ubuntu 24.04 template if needed, creates the
container, and runs the installer. It prints the dashboard URL when it finishes.

To skip the dialog entirely, pass `--defaults`, or give the values as options —
anything supplied on the command line is used as-is:

```bash
./lxc/proxmox-lxc.sh \
  --ctid 150 \
  --hostname unifi-insights \
  --ip 192.168.1.50/24 --gateway 192.168.1.1 \
  --storage local-lvm \
  --memory 8192 --disk 32
```

`./lxc/proxmox-lxc.sh --help` lists all of them. Note that `--ip` takes CIDR
notation — `192.168.1.50` alone is rejected, `192.168.1.50/24` is what `pct`
expects.

A static address is worth setting: the UniFi gateway sends syslog to a fixed
address, and a DHCP lease can move the container out from under it.

To provision from GitHub rather than a local checkout — useful when running the
script straight off a Proxmox shell:

```bash
./lxc/proxmox-lxc.sh --git-ref main
```

## Installing into a container you created yourself

Create an **unprivileged Ubuntu 24.04** container (4 cores / 4 GB RAM / 16 GB disk
minimum), then inside it:

```bash
apt update && apt install -y git
git clone https://github.com/jmasarweh/UniFi-Insights-Plus.git /opt/uip-src
/opt/uip-src/lxc/install.sh
```

The installer also works on a plain Ubuntu 24.04 VM or bare-metal host.

## After installation

1. Open `http://<container-ip>:8000` and complete the setup wizard.
2. Point your UniFi gateway's remote syslog at `<container-ip>`, UDP port 514.
3. Add your MaxMind and AbuseIPDB credentials:

   ```bash
   nano /etc/unifi-insights-plus.env
   systemctl restart uip-api uip-receiver
   ```

Credentials can also be entered in the UI; environment variables take precedence
over values stored in the database.

## What runs where

| | |
|---|---|
| Application | `/app` (same layout as the container image) |
| Configuration | `/etc/unifi-insights-plus.env` |
| Database | local PostgreSQL 16 cluster, `/var/lib/postgresql/16/main` |
| Service account | `uip` |
| Source tree | `/opt/uip-src` |

| Unit | Replaces |
|---|---|
| `uip-receiver.service` | `[program:receiver]` — syslog on UDP 514 |
| `uip-api.service` | `[program:api]` — uvicorn on port 8000 |
| `uip-geoip.timer` → `uip-geoip.service` | the `geoipupdate` cron entry (Wed & Sat 07:00 UTC) |
| `postgresql@16-main.service` | `[program:postgresql]` |

```bash
systemctl status uip-api uip-receiver
journalctl -u uip-api -u uip-receiver -f
systemctl list-timers uip-geoip.timer
systemctl start uip-geoip.service     # force a GeoLite2 update now
```

## Updating

```bash
/opt/uip-src/lxc/update.sh --ref v3.7.0
```

Or track a branch with `--ref main`. The configuration file and the database are
preserved; the application migrates its own schema on start.

## External PostgreSQL

Set `DB_HOST` (and the other `DB_*` values) in `/etc/unifi-insights-plus.env` and
re-run `lxc/install.sh`. The local cluster is then disabled, mirroring how
`entrypoint.sh` skips the embedded database in the image.

## Migrating from an existing Docker deployment

Dump on the Docker host, restore in the container:

```bash
# Docker host
docker exec unifi-log-insight pg_dump -U unifi -Fc unifi_logs > uip.dump

# Proxmox host
pct push <ctid> uip.dump /tmp/uip.dump

# Inside the container
systemctl stop uip-api uip-receiver
sudo -u postgres dropdb unifi_logs
sudo -u postgres createdb -O unifi unifi_logs
sudo -u postgres pg_restore -d unifi_logs /tmp/uip.dump
systemctl start uip-receiver uip-api
```

Copy `SECRET_KEY` (or `POSTGRES_PASSWORD`, whichever the Docker deployment used as
the encryption key) into `/etc/unifi-insights-plus.env` before starting, otherwise
the stored API keys cannot be decrypted and must be re-entered.

## Notes and gotchas

- **Resources.** PostgreSQL, the receiver, and the API run concurrently; 4 cores and
  4 GB RAM are the realistic floor. The UI build alone peaks around 1 GB.
- **IPv6 must stay enabled** in the container. The receiver binds `('::', 514)` for
  dual-stack receive, so a container with `disable_ipv6=1` ingests nothing. The
  installer refuses to continue in that case.
- **Do not add `PrivateTmp=yes`** to the units. The API and receiver exchange
  config-reload requests and AbuseIPDB counters through files in `/tmp`; a private
  `/tmp` breaks both without any error message.
- **Both units must run as the same user.** The API signals the receiver with
  `pkill -SIGUSR2 -f /app/main.py`.
- **PostgreSQL tuning** lives in `/etc/postgresql/16/main/conf.d/10-unifi-insights.conf`
  so it survives package upgrades. Raise `shared_buffers` and `effective_cache_size`
  there if you give the container more RAM.
- **Timezone.** Set `TZ` in the environment file to match your UniFi gateway, or
  syslog timestamps will be offset.
- **Backups.** Proxmox `vzdump` snapshots of the container cover the database too.
  Stop `uip-receiver` and `uip-api` first for a `stop`-mode backup, or rely on
  PostgreSQL crash recovery with `snapshot` mode.
- **The dashboard is on port 8000, not 8090.** The published `docker-compose.yml`
  maps `8090:8000`; there is no port mapping here, so the container's own port is
  what you open.
- **`Migration skipped (insufficient privilege)` in the API log** means the app
  role does not own the tables it is migrating. Re-run `lxc/install.sh` — it
  transfers ownership of the whole `public` schema on every run.
