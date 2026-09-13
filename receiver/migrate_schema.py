#!/usr/bin/env python3
"""One-shot migration of the logs table to the normalised schema.

Measured on a live install at 61 rows/s: 745 bytes per row, of which raw_log is
262 and the rule name and description together are 53. This rebuilds the table
with SMALLINT keys into lookup tables, in alignment order, and without the
columns that duplicate what is already derivable.

    /app/venv/bin/python /app/migrate_schema.py [--dry-run] [--batch 50000]

The receiver and API must be stopped: the swap at the end renames the table, and
an insert in flight would land in the old one.

    systemctl stop uip-receiver uip-api
    /app/venv/bin/python /app/migrate_schema.py
    systemctl start uip-receiver uip-api

Restartable. The copy records its cursor after every batch, so an interrupted
run continues where it stopped rather than starting over. Nothing is destroyed
until the copy has verified its row count: the old table is kept as logs_old for
you to drop once the app looks right.
"""

import argparse
import logging
import sys
import time

import psycopg2
from psycopg2 import extras

import lookups
from db import build_conn_params

logging.basicConfig(level=logging.INFO, format='%(asctime)s [migrate] %(levelname)s: %(message)s')
logger = logging.getLogger('migrate')

CURSOR_KEY = 'schema_v2_copy_cursor'


# ── Column translation ───────────────────────────────────────────────────────

def case_expr(column: str, kind: str) -> str:
    """A CASE mapping a text column to its closed-set id.

    Generated from lookups.CLOSED_SETS so the migration cannot assign ids that
    differ from the ones the application will read back.
    """
    whens = ' '.join(
        f"WHEN '{name}' THEN {value}" for name, value in lookups.CLOSED_SETS[kind].items()
    )
    return f"CASE lower(trim({column})) {whens} ELSE NULL END"


# Old column → new column. Everything not listed is translated explicitly below.
PASSTHROUGH = [
    'id', 'timestamp', 'abuse_last_reported', 'src_port', 'dst_port',
    'asn_number', 'threat_score', 'abuse_total_reports',
    'abuse_is_whitelisted', 'abuse_is_tor',
    'src_ip', 'dst_ip', 'remote_ip', 'mac_address',
    'geo_country', 'geo_city', 'geo_lat', 'geo_lon', 'asn_name',
    'threat_categories', 'rdns', 'abuse_usage_type', 'abuse_hostnames',
    'dns_query', 'dns_type', 'dns_answer', 'dhcp_event', 'wifi_event',
]

TRANSLATED = [
    ('log_type_id', lambda: case_expr('l.log_type', 'log_type')),
    ('direction_id', lambda: case_expr('l.direction', 'direction')),
    ('rule_action_id', lambda: case_expr('l.rule_action', 'rule_action')),
    ('rule_id', lambda: 'r.id'),
    ('protocol_id', lambda: 'p.id'),
    ('iface_in_id', lambda: 'ii.id'),
    ('iface_out_id', lambda: 'io.id'),
    ('hostname_id', lambda: 'hn.id'),
    ('src_device_id', lambda: 'sdn.id'),
    ('dst_device_id', lambda: 'ddn.id'),
    # Kept only for lines the parser could not read; for everything else the
    # parsed columns already carry the content.
    ('raw_log', lambda: "CASE WHEN lower(trim(l.log_type)) = 'unknown' THEN l.raw_log END"),
]


def copy_sql() -> str:
    columns = PASSTHROUGH + [name for name, _ in TRANSLATED]
    values = [f'l.{c}' for c in PASSTHROUGH] + [expr() for _, expr in TRANSLATED]
    return f"""
        INSERT INTO logs_new ({', '.join(columns)})
        SELECT {', '.join(values)}
        FROM logs l
        LEFT JOIN rules r
          ON COALESCE(lower(r.name), '')  = COALESCE(lower(l.rule_name), '')
         AND COALESCE(lower(r.descr), '') = COALESCE(lower(l.rule_desc), '')
         AND NOT (l.rule_name IS NULL AND l.rule_desc IS NULL)
        LEFT JOIN protocols    p   ON lower(p.name)   = lower(l.protocol)
        LEFT JOIN interfaces   ii  ON lower(ii.name)  = lower(l.interface_in)
        LEFT JOIN interfaces   io  ON lower(io.name)  = lower(l.interface_out)
        LEFT JOIN device_names hn  ON lower(hn.name)  = lower(l.hostname)
        LEFT JOIN device_names sdn ON lower(sdn.name) = lower(l.src_device_name)
        LEFT JOIN device_names ddn ON lower(ddn.name) = lower(l.dst_device_name)
        WHERE l.id > %s
        ORDER BY l.id
        LIMIT %s
    """


# ── Schema ───────────────────────────────────────────────────────────────────

LOOKUP_DDL = [
    """CREATE TABLE IF NOT EXISTS rules (
        id SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name VARCHAR(100), descr VARCHAR(255))""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_rules_key
        ON rules (COALESCE(lower(name), ''), COALESCE(lower(descr), ''))""",
    """CREATE TABLE IF NOT EXISTS interfaces (
        id SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name VARCHAR(20) NOT NULL)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_interfaces_key ON interfaces (lower(name))",
    """CREATE TABLE IF NOT EXISTS device_names (
        id SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name TEXT NOT NULL)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_names_key ON device_names (lower(name))",
    """CREATE TABLE IF NOT EXISTS protocols (
        id SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name VARCHAR(10) NOT NULL)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_protocols_key ON protocols (lower(name))",
    """CREATE TABLE IF NOT EXISTS services (
        port INTEGER NOT NULL, proto VARCHAR(10) NOT NULL, name TEXT NOT NULL,
        PRIMARY KEY (port, proto))""",
]

SEED_LOOKUPS = [
    ("""INSERT INTO rules (name, descr)
        SELECT DISTINCT rule_name, rule_desc FROM logs
        WHERE rule_name IS NOT NULL OR rule_desc IS NOT NULL
        ON CONFLICT DO NOTHING""", 'rules'),
    ("""INSERT INTO interfaces (name)
        SELECT DISTINCT iface FROM (
            SELECT interface_in AS iface FROM logs WHERE interface_in IS NOT NULL
            UNION SELECT interface_out FROM logs WHERE interface_out IS NOT NULL
        ) s ON CONFLICT DO NOTHING""", 'interfaces'),
    ("""INSERT INTO device_names (name)
        SELECT DISTINCT name FROM (
            SELECT src_device_name AS name FROM logs WHERE src_device_name IS NOT NULL
            UNION SELECT dst_device_name FROM logs WHERE dst_device_name IS NOT NULL
            UNION SELECT hostname FROM logs WHERE hostname IS NOT NULL
        ) s ON CONFLICT DO NOTHING""", 'device_names'),
    ("""INSERT INTO protocols (name)
        SELECT DISTINCT lower(protocol) FROM logs WHERE protocol IS NOT NULL
        ON CONFLICT DO NOTHING""", 'protocols'),
]

NEW_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS logs_new (
        id                   BIGINT PRIMARY KEY,
        timestamp            TIMESTAMPTZ NOT NULL,
        abuse_last_reported  TIMESTAMPTZ,
        src_port             INTEGER,
        dst_port             INTEGER,
        asn_number           INTEGER,
        threat_score         INTEGER,
        abuse_total_reports  INTEGER,
        log_type_id          SMALLINT,
        direction_id         SMALLINT,
        rule_id              SMALLINT,
        rule_action_id       SMALLINT,
        protocol_id          SMALLINT,
        iface_in_id          SMALLINT,
        iface_out_id         SMALLINT,
        hostname_id          SMALLINT,
        src_device_id        SMALLINT,
        dst_device_id        SMALLINT,
        abuse_is_whitelisted BOOLEAN,
        abuse_is_tor         BOOLEAN,
        src_ip               INET,
        dst_ip               INET,
        remote_ip            INET,
        mac_address          MACADDR,
        geo_country          VARCHAR(2),
        geo_city             VARCHAR(100),
        geo_lat              DECIMAL(9,6),
        geo_lon              DECIMAL(9,6),
        asn_name             VARCHAR(255),
        threat_categories    TEXT[],
        rdns                 VARCHAR(255),
        abuse_usage_type     TEXT,
        abuse_hostnames      TEXT,
        dns_query            VARCHAR(255),
        dns_type             VARCHAR(10),
        dns_answer           VARCHAR(255),
        dhcp_event           VARCHAR(20),
        wifi_event           VARCHAR(50),
        raw_log              TEXT
    )
"""

# Built after the copy: maintaining them during it would roughly double its cost.
POST_COPY_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_logs_timestamp    ON logs (timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_logs_src_ip       ON logs (src_ip)",
    "CREATE INDEX IF NOT EXISTS idx_logs_dst_ip       ON logs (dst_ip)",
    "CREATE INDEX IF NOT EXISTS idx_logs_direction    ON logs (direction_id)",
    "CREATE INDEX IF NOT EXISTS idx_logs_threat_score ON logs (threat_score) WHERE threat_score IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_logs_type_time    ON logs (log_type_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_logs_action_time  ON logs (rule_action_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_logs_dst_port     ON logs (dst_port) WHERE dst_port IS NOT NULL",
]


# ── Steps ────────────────────────────────────────────────────────────────────

def already_migrated(cur) -> bool:
    cur.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'logs' AND column_name = 'log_type_id'""")
    return cur.fetchone() is not None


def table_size(cur, table) -> str:
    cur.execute("SELECT pg_size_pretty(pg_total_relation_size(%s))", [table])
    return cur.fetchone()[0]


def read_cursor(cur) -> int:
    cur.execute("SELECT value FROM system_config WHERE key = %s", [CURSOR_KEY])
    row = cur.fetchone()
    return int(row[0]) if row else 0


def write_cursor(cur, value: int):
    cur.execute(
        "INSERT INTO system_config (key, value, updated_at) VALUES (%s, %s::jsonb, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
        [CURSOR_KEY, str(value)],
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true',
                    help='report what would happen and exit without writing')
    ap.add_argument('--batch', type=int, default=50000, help='rows per copy batch')
    args = ap.parse_args()

    conn = psycopg2.connect(**build_conn_params())
    conn.autocommit = False
    cur = conn.cursor()

    if already_migrated(cur):
        logger.info("logs already has log_type_id — nothing to do.")
        return 0

    cur.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM logs")
    total, max_id = cur.fetchone()
    logger.info("Source: %s rows, %s", f"{total:,}", table_size(cur, 'logs'))

    if args.dry_run:
        for sql, name in SEED_LOOKUPS:
            probe = sql.split('ON CONFLICT')[0].replace('INSERT INTO', '-- INSERT INTO')
            logger.info("would seed %s", name)
        logger.info("would copy %s rows in batches of %s", f"{total:,}", f"{args.batch:,}")
        logger.info("would swap logs -> logs_old and logs_new -> logs")
        logger.info("Dry run: nothing was written.")
        return 0

    # Fail fast if the services are still writing: the swap would lose their rows.
    cur.execute("""SELECT count(*) FROM pg_stat_activity
                   WHERE datname = current_database()
                     AND pid <> pg_backend_pid()
                     AND state <> 'idle'""")
    busy = cur.fetchone()[0]
    if busy:
        logger.error("%d other active session(s) on this database. Stop uip-receiver "
                     "and uip-api first, or rows written during the copy will be lost.", busy)
        return 1

    logger.info("Creating lookup tables...")
    for sql in LOOKUP_DDL:
        cur.execute(sql)
    conn.commit()

    logger.info("Seeding lookup tables from the existing rows...")
    for sql, name in SEED_LOOKUPS:
        cur.execute(sql)
        cur.execute(f"SELECT COUNT(*) FROM {name}")
        logger.info("  %-13s %d distinct values", name, cur.fetchone()[0])
    conn.commit()

    cur.execute(NEW_TABLE_DDL)
    conn.commit()

    last_id = read_cursor(cur)
    if last_id:
        logger.info("Resuming from id %s", f"{last_id:,}")

    sql = copy_sql()
    copied = 0
    started = time.time()
    while last_id < max_id:
        cur.execute(sql, [last_id, args.batch])
        moved = cur.rowcount
        if moved == 0:
            break
        cur.execute("SELECT COALESCE(MAX(id), %s) FROM logs_new", [last_id])
        last_id = cur.fetchone()[0]
        write_cursor(cur, last_id)
        conn.commit()
        copied += moved
        pct = 100.0 * copied / total if total else 100.0
        logger.info("  copied %s / %s (%.1f%%)", f"{copied:,}", f"{total:,}", pct)

    elapsed = time.time() - started
    cur.execute("SELECT COUNT(*) FROM logs_new")
    new_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM logs WHERE id <= %s", [max_id])
    old_count = cur.fetchone()[0]

    if new_count != old_count:
        logger.error("Row count mismatch: %s copied, %s expected. Nothing was swapped; "
                     "logs_new is left in place for inspection.", f"{new_count:,}", f"{old_count:,}")
        return 1

    logger.info("Copied %s rows in %.0fs. New table: %s (was %s)",
                f"{new_count:,}", elapsed, table_size(cur, 'logs_new'), table_size(cur, 'logs'))

    logger.info("Swapping tables...")
    cur.execute("ALTER TABLE logs RENAME TO logs_old")
    cur.execute("ALTER TABLE logs_new RENAME TO logs")

    # Renaming a table does not rename its indexes: logs_old still holds
    # idx_logs_timestamp, idx_logs_action_time and the rest. Index names are
    # unique per schema, so every CREATE INDEX IF NOT EXISTS below would find
    # the name taken and silently do nothing — leaving the new table with only
    # its primary key, and every filtered query on a sequential scan.
    cur.execute("""
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN SELECT indexname FROM pg_indexes
                     WHERE tablename = 'logs_old' AND indexname NOT LIKE '%_old'
            LOOP
                EXECUTE format('ALTER INDEX %I RENAME TO %I',
                               r.indexname, left(r.indexname, 55) || '_old');
            END LOOP;
        END $$
    """)
    cur.execute("ALTER INDEX IF EXISTS logs_new_pkey RENAME TO logs_pkey")
    # logs_new was created with a plain BIGINT id so the copy could carry the
    # original ids; the sequence is attached here and set past the high-water mark.
    #
    # CREATE SEQUENCE IF NOT EXISTS would be wrong here for the same reason the
    # index names were: the original table's sequence still exists under this
    # name, now owned by logs_old.id. The IF NOT EXISTS would skip, leaving the
    # new table using a sequence PostgreSQL considers part of the old one — and
    # DROP TABLE logs_old would then refuse, or with CASCADE take the sequence
    # with it and break every insert.
    cur.execute("SELECT 1 FROM pg_class WHERE relname = 'logs_id_seq' AND relkind = 'S'")
    if cur.fetchone() is None:
        cur.execute("CREATE SEQUENCE logs_id_seq")
    cur.execute("ALTER SEQUENCE logs_id_seq OWNED BY logs.id")
    cur.execute("ALTER TABLE logs ALTER COLUMN id SET DEFAULT nextval('logs_id_seq')")
    cur.execute("SELECT setval('logs_id_seq', %s)", [max_id + 1])
    conn.commit()

    logger.info("Building indexes...")
    for sql in POST_COPY_INDEXES:
        logger.info("  %s", sql.split(' ON ')[0].replace('CREATE INDEX IF NOT EXISTS ', ''))
        cur.execute(sql)
    conn.commit()

    cur.execute("ANALYZE logs")
    conn.commit()

    # Verify rather than assume: a CREATE INDEX that silently did nothing is
    # invisible until a query runs slowly weeks later.
    expected = {sql.split('idx_')[1].split()[0] for sql in POST_COPY_INDEXES}
    expected = {'idx_' + n for n in expected}
    cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'logs'")
    present = {row[0] for row in cur.fetchall()}
    missing = expected - present
    if missing:
        logger.error("These indexes were not created on the new table: %s",
                     ', '.join(sorted(missing)))
        logger.error("Queries will fall back to sequential scans. Create them by hand "
                     "before considering the migration finished.")
        return 1
    cur.execute("""SELECT pg_size_pretty(pg_indexes_size('logs'))""")
    logger.info("Indexes on logs: %d, %s", len(present), cur.fetchone()[0])

    cur.execute("""
        SELECT c.relname
        FROM pg_depend d
        JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S'
        JOIN pg_class c ON c.oid = d.refobjid
        WHERE s.relname = 'logs_id_seq' AND d.deptype = 'a'
    """)
    owner = cur.fetchone()
    if not owner or owner[0] != 'logs':
        logger.error("logs_id_seq is owned by %s, not logs. Dropping logs_old would "
                     "take the sequence with it. Fix with: "
                     "ALTER SEQUENCE logs_id_seq OWNED BY logs.id",
                     owner[0] if owner else 'nothing')
        return 1

    cur.execute("SELECT last_value FROM logs_id_seq")
    seq_value = cur.fetchone()[0]
    if seq_value <= max_id:
        logger.error("logs_id_seq is at %s but the highest id is %s — the next insert "
                     "would collide.", f"{seq_value:,}", f"{max_id:,}")
        return 1

    logger.info("Done. The previous table is kept as logs_old (%s).", table_size(cur, 'logs_old'))
    logger.info("Start the services, check the dashboard, then drop it:")
    logger.info("    sudo -u postgres psql -d unifi_logs -c 'DROP TABLE logs_old'")
    return 0


if __name__ == '__main__':
    sys.exit(main())
