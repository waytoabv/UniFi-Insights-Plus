"""Tests for the one-shot schema migration.

The migration writes the same table the application then reads, so the two
column sets have to agree. A mismatch would only surface as a crash after the
swap, with the old table already renamed.
"""

import inspect
import re

import lookups
import migrate_schema as m
from db import INSERT_COLUMNS


def _insert_columns(sql):
    body = sql.split('INSERT INTO logs_new (', 1)[1].split(')', 1)[0]
    return [c.strip() for c in body.split(',')]


class TestColumnAgreement:
    def test_migration_covers_every_runtime_column(self):
        migrated = set(_insert_columns(m.copy_sql()))
        missing = set(INSERT_COLUMNS) - migrated
        assert not missing, f"migration does not carry over: {sorted(missing)}"

    def test_migration_adds_only_the_primary_key(self):
        migrated = set(_insert_columns(m.copy_sql()))
        extra = migrated - set(INSERT_COLUMNS)
        assert extra == {'id'}, f"unexpected extra columns: {sorted(extra - {'id'})}"

    def test_new_table_declares_every_migrated_column(self):
        declared = set(re.findall(r'^\s+([a-z_]+)\s+[A-Z]', m.NEW_TABLE_DDL, re.M))
        for column in _insert_columns(m.copy_sql()):
            assert column in declared, f"{column} is inserted but not declared"

    def test_select_list_matches_the_column_list(self):
        sql = m.copy_sql()
        columns = _insert_columns(sql)
        select = sql.split('SELECT ', 1)[1].split('\n        FROM logs l', 1)[0]
        # Commas inside CASE expressions would break a naive count, so compare
        # against the top-level split the query itself uses.
        assert select.count('CASE') == 4  # three closed sets plus the raw_log guard
        assert len(columns) == len(m.PASSTHROUGH) + len(m.TRANSLATED)


class TestClosedSetMapping:
    def test_case_expressions_use_the_canonical_ids(self):
        expr = m.case_expr('l.log_type', 'log_type')
        for name, value in lookups.CLOSED_SETS['log_type'].items():
            assert f"WHEN '{name}' THEN {value}" in expr

    def test_unmapped_value_becomes_null(self):
        assert m.case_expr('l.direction', 'direction').endswith('ELSE NULL END')

    def test_comparison_is_normalised(self):
        """Values are trimmed and lowercased, matching lookups._closed_id."""
        assert m.case_expr('l.log_type', 'log_type').startswith('CASE lower(trim(l.log_type))')


class TestRawLogPolicy:
    def test_only_unparsed_lines_keep_their_raw_text(self):
        expr = dict((n, e()) for n, e in m.TRANSLATED)['raw_log']
        assert "lower(trim(l.log_type)) = 'unknown'" in expr
        assert 'l.raw_log' in expr


class TestSeeds:
    def test_every_lookup_table_is_seeded(self):
        seeded = {name for _, name in m.SEED_LOOKUPS}
        assert seeded == {'rules', 'interfaces', 'device_names', 'protocols'}

    def test_device_names_seed_includes_hostname(self):
        """hostname shares the device_names table, so it has to be seeded too."""
        sql = dict((n, s) for s, n in m.SEED_LOOKUPS)['device_names']
        assert 'hostname' in sql

    def test_seeds_are_idempotent(self):
        for sql, _ in m.SEED_LOOKUPS:
            assert 'ON CONFLICT DO NOTHING' in sql


class TestIndexNameCollision:
    """Renaming a table leaves its indexes under their original names.

    The first run of this migration swapped logs → logs_old, then issued
    CREATE INDEX IF NOT EXISTS for each index on the new table. Every one of
    those names was still held by an index on logs_old, so all eight did
    nothing and the new table was left with only its primary key — every
    filtered query fell back to a sequential scan.
    """

    def test_old_indexes_are_renamed_before_new_ones_are_created(self):
        source = inspect.getsource(m.main)
        rename_at = source.index("ALTER INDEX %I RENAME TO %I")
        create_at = source.index("for sql in POST_COPY_INDEXES")
        assert rename_at < create_at, \
            "old index names must be freed before the new indexes are created"

    def test_rename_skips_already_renamed_indexes(self):
        """The step has to be idempotent — the migration is restartable."""
        source = inspect.getsource(m.main)
        assert "indexname NOT LIKE '%_old'" in source

    def test_rename_stays_within_the_identifier_limit(self):
        """Appending a suffix to a 63-character identifier would truncate it
        into a collision with another renamed index."""
        source = inspect.getsource(m.main)
        assert "left(r.indexname, 55)" in source

    def test_result_is_verified_not_assumed(self):
        source = inspect.getsource(m.main)
        assert "were not created on the new table" in source
