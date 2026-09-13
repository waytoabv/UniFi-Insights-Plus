"""Guards the shape of the logs_text view.

The view resolves eleven lookups. For the log stream that is fifty rows and
free; for a dashboard aggregate it is eleven joins across millions of rows
before the GROUP BY, which is what made the dashboard slow.

PostgreSQL drops a LEFT JOIN whose right side is unique on the join key and
whose columns the query does not use, so an aggregate that only needs
geo_country becomes a plain scan of logs again. That only holds while the view
keeps a shape the planner can reason about — which is what these assert.
"""

import inspect
import re
import textwrap

import db


def view_sql():
    source = inspect.getsource(db.Database._ensure_schema)
    match = re.search(r'CREATE OR REPLACE VIEW logs_text AS(.*?)"""', source, re.S)
    assert match, "view definition not found"
    return match.group(1)


class TestJoinsAreRemovable:
    def test_every_join_is_a_left_join(self):
        """An inner join filters, so it can never be dropped."""
        sql = view_sql()
        joins = re.findall(r'(\w+)\s+JOIN\s', sql)
        assert all(j == 'LEFT' for j in joins), f"non-LEFT join in the view: {set(joins)}"

    def test_no_inline_values_lists(self):
        """A VALUES list has no unique constraint the planner can rely on, so a
        join against one is never removable."""
        assert 'VALUES' not in view_sql().upper()

    def test_closed_sets_join_against_tables(self):
        sql = view_sql()
        for table in ('log_types', 'rule_actions', 'directions'):
            assert f'LEFT JOIN {table}' in sql

    def test_every_join_is_on_a_primary_key(self):
        """Removal needs the join column to be provably unique."""
        sql = view_sql()
        for right, column in re.findall(r'LEFT JOIN\s+(\w+)\s+\w+\s+ON\s+\w+\.(\w+)\s*=', sql):
            if right == 'services':
                continue  # composite key, asserted separately
            assert column == 'id', f"{right} is joined on {column}, not its primary key"

    def test_services_does_not_depend_on_another_join(self):
        """Joining services on the protocol's *name* would chain it to the
        protocols join, and a chained join cannot be dropped on its own."""
        sql = view_sql()
        services = re.search(r'LEFT JOIN\s+services\s+\w+\s+ON(.*?)(?:LEFT JOIN|$)', sql, re.S)
        assert services, "services join not found"
        condition = services.group(1)
        assert 'proto_id' in condition
        assert 'p.name' not in condition


class TestReferenceTables:
    def test_closed_sets_are_seeded_from_the_python_mapping(self):
        source = inspect.getsource(db.Database._populate_reference_tables)
        assert 'lookups.CLOSED_SETS' in source, \
            "the tables must mirror the mapping, not repeat it"

    def test_seeding_is_idempotent(self):
        source = inspect.getsource(db.Database._populate_reference_tables)
        assert 'ON CONFLICT' in source

    def test_a_renamed_member_is_corrected_not_duplicated(self):
        source = inspect.getsource(db.Database._populate_reference_tables)
        assert 'DO UPDATE SET name' in source
