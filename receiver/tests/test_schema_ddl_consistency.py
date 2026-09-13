"""Guards the schema DDL against references to columns that no longer exist.

A stale column name in _ensure_schema does not fail any unit test — it fails at
start-up, in a restart loop, after the migration has already renamed the old
table. That happened once; this makes it fail here instead.
"""

import ast
import inspect
import re
import textwrap

import pytest

import db


# Columns the normalisation removed from `logs`. A DDL statement naming one of
# these against that table is a leftover.
REMOVED_COLUMNS = (
    'log_type', 'direction', 'protocol', 'rule_name', 'rule_desc', 'rule_action',
    'interface_in', 'interface_out', 'service_name', 'src_device_name',
    'dst_device_name', 'created_at',
)

# Tables that legitimately still have columns by these names.
OTHER_TABLES = ('unifi_clients', 'unifi_devices', 'rdns_cache', 'ip_threats',
                'system_config', 'services', 'logs_text', 'protocols')


def _sql_literals(func):
    """Every string constant in a function's source.

    textwrap.dedent rather than inspect.cleandoc: the latter also strips the
    docstring's leading whitespace, which leaves the body unparseable.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _statements_touching_logs():
    out = []
    for literal in _sql_literals(db.Database._ensure_schema):
        if not re.search(r'\b(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE)\b', literal, re.I):
            continue
        if not re.search(r'\blogs\b', literal):
            continue
        if any(t in literal for t in OTHER_TABLES):
            continue
        out.append(literal)
    for entry in db.Database._POST_BOOT_INDEXES:
        out.append(entry['sql'])
    for _name, sql in db.Database._POST_BOOT_DROPS:
        out.append(sql)
    return out


class TestNoRemovedColumns:
    @pytest.mark.parametrize('column', REMOVED_COLUMNS)
    def test_ddl_never_names_a_removed_column(self, column):
        offenders = [
            sql for sql in _statements_touching_logs()
            # word boundary that also rejects the _id suffix
            if re.search(rf'\b{column}\b(?!_id)', sql)
        ]
        assert not offenders, (
            f"DDL still references logs.{column}:\n" + "\n".join(s[:160] for s in offenders)
        )

    def test_no_unrendered_placeholders(self):
        """An f-string that lost its prefix ships '{_LT_FIREWALL}' as SQL text."""
        offenders = [sql for sql in _statements_touching_logs() if re.search(r'\{_[A-Z]', sql)]
        assert not offenders, (
            "DDL contains an unrendered placeholder — the literal is missing its f prefix:\n"
            + "\n".join(s[:160] for s in offenders)
        )


class TestPostBootIndexes:
    def test_dropped_indexes_are_not_recreated(self):
        """Indexes the diet removed must not come back through the post-boot path."""
        recreated = {e['name'] for e in db.Database._POST_BOOT_INDEXES}
        for dropped in ('idx_logs_type_id', 'idx_logs_src_port',
                        'idx_logs_protocol', 'idx_logs_service_name'):
            assert dropped not in recreated


class TestNoUnrenderedPlaceholders:
    """A literal that lost its f prefix ships '{_RA_BLOCK}' to PostgreSQL.

    It is silent until the query runs, and grep cannot tell a broken literal
    from a working f-string, so the check walks the AST instead: an f-string is
    a JoinedStr and cannot carry an unrendered placeholder, while a plain
    Constant containing one is always a bug.
    """

    @pytest.mark.parametrize('module_path', [
        'db.py', 'query_helpers.py', 'routes/stats.py', 'routes/flows.py',
        'routes/logs.py', 'routes/setup.py',
    ])
    def test_no_constant_carries_a_placeholder(self, module_path):
        import pathlib
        source = (pathlib.Path(__file__).parent.parent / module_path).read_text(encoding='utf-8')
        offenders = [
            node.lineno
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and re.search(r'\{_[A-Z][A-Z_]*\}', node.value)
        ]
        assert not offenders, (
            f"{module_path}: literal with an unrendered placeholder at "
            f"line(s) {offenders} — the f prefix is missing"
        )
