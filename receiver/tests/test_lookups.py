"""Tests for the ID ↔ text mapping layer of the normalised log schema."""

import pytest

import lookups
from lookups import (
    CLOSED_SETS,
    LookupTable,
    direction_id,
    direction_text,
    log_type_id,
    log_type_text,
    rule_action_id,
    rule_action_text,
)


# ── Closed sets ──────────────────────────────────────────────────────────────

class TestClosedSets:
    """log_type, rule_action and direction are produced by our own parsers."""

    def test_log_type_roundtrip(self):
        for text in ('firewall', 'dns', 'dhcp', 'wifi', 'system', 'unknown'):
            assert log_type_text(log_type_id(text)) == text

    def test_rule_action_roundtrip(self):
        for text in ('allow', 'block', 'redirect'):
            assert rule_action_text(rule_action_id(text)) == text

    def test_direction_roundtrip(self):
        for text in ('inbound', 'outbound', 'inter_vlan', 'nat', 'local', 'vpn'):
            assert direction_text(direction_id(text)) == text

    def test_none_maps_to_none(self):
        assert log_type_id(None) is None
        assert rule_action_id(None) is None
        assert direction_id(None) is None
        assert log_type_text(None) is None

    def test_unknown_text_returns_none(self):
        assert rule_action_id('teleport') is None
        assert direction_id('sideways') is None

    def test_unknown_id_returns_none(self):
        assert log_type_text(9999) is None

    def test_ids_are_stable(self):
        """IDs are persisted in every row — reassigning one silently rewrites
        history, so the mapping is pinned here on purpose."""
        assert CLOSED_SETS['log_type'] == {
            'firewall': 1, 'dns': 2, 'dhcp': 3, 'wifi': 4, 'system': 5, 'unknown': 6,
        }
        assert CLOSED_SETS['rule_action'] == {'allow': 1, 'block': 2, 'redirect': 3}
        assert CLOSED_SETS['direction'] == {
            'inbound': 1, 'outbound': 2, 'inter_vlan': 3, 'nat': 4, 'local': 5, 'vpn': 6,
        }

    def test_ids_fit_smallint(self):
        for mapping in CLOSED_SETS.values():
            assert all(0 < i < 32768 for i in mapping.values())

    def test_lookup_is_case_insensitive(self):
        assert log_type_id('FIREWALL') == log_type_id('firewall')
        assert rule_action_id('Block') == rule_action_id('block')


class TestClosedSetCoverage:
    """Guards against a parser learning a value the mapping does not know."""

    def test_parser_log_types_are_all_mapped(self):
        import parsers
        for value in _string_literals_assigned_to(parsers, 'log_type'):
            assert log_type_id(value) is not None, \
                f"parsers.py emits log_type={value!r} but lookups.py has no id for it"

    def test_parser_rule_actions_are_all_mapped(self):
        import parsers
        for value in _string_literals_assigned_to(parsers, 'rule_action'):
            assert rule_action_id(value) is not None, \
                f"parsers.py emits rule_action={value!r} but lookups.py has no id for it"


def _string_literals_assigned_to(module, key):
    """Collect every string literal assigned to `key` anywhere in a module.

    Reads the module's source rather than calling it, so the check stays honest
    even for branches the test suite never exercises.
    """
    import ast
    import inspect

    found = set()
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        # {'log_type': 'firewall'}
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == key
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    found.add(v.value)
        # entry['log_type'] = 'firewall'
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == key):
                    found.add(node.value.value)
    return found


# ── Open sets ────────────────────────────────────────────────────────────────

class FakeDB:
    """Minimal stand-in for Database: records SQL and replays canned rows."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.inserted = []
        self._next_id = max((r[0] for r in self.rows), default=0) + 1

    def fetch_lookup(self, table, columns):
        return list(self.rows)

    def insert_lookup(self, table, columns, values):
        new_id = self._next_id
        self._next_id += 1
        self.rows.append((new_id, *values))
        self.inserted.append((table, values))
        return new_id


class TestLookupTable:
    def test_resolves_existing_value(self):
        db = FakeDB([(1, 'eth0'), (2, 'eth1')])
        t = LookupTable(db, 'interfaces', ('name',))
        assert t.id_for('eth0') == 1
        assert t.id_for('eth1') == 2
        assert db.inserted == []

    def test_creates_missing_value(self):
        db = FakeDB([(1, 'eth0')])
        t = LookupTable(db, 'interfaces', ('name',))
        assert t.id_for('wg0') == 2
        assert db.inserted == [('interfaces', ('wg0',))]

    def test_created_value_is_cached(self):
        db = FakeDB()
        t = LookupTable(db, 'interfaces', ('name',))
        first = t.id_for('wg0')
        second = t.id_for('wg0')
        assert first == second
        assert len(db.inserted) == 1

    def test_text_for_resolves_back(self):
        db = FakeDB([(1, 'eth0')])
        t = LookupTable(db, 'interfaces', ('name',))
        assert t.text_for(1) == 'eth0'

    def test_text_for_unknown_id_returns_none(self):
        t = LookupTable(FakeDB(), 'interfaces', ('name',))
        assert t.text_for(42) is None

    def test_none_is_not_stored(self):
        db = FakeDB()
        t = LookupTable(db, 'interfaces', ('name',))
        assert t.id_for(None) is None
        assert db.inserted == []

    def test_empty_string_is_not_stored(self):
        db = FakeDB()
        t = LookupTable(db, 'interfaces', ('name',))
        assert t.id_for('') is None
        assert db.inserted == []

    def test_composite_key(self):
        """rules is keyed on (name, descr) — two rules may share a name."""
        db = FakeDB([(1, 'LAN-to-WAN', 'allow established')])
        t = LookupTable(db, 'rules', ('name', 'descr'))
        assert t.id_for('LAN-to-WAN', 'allow established') == 1
        assert t.id_for('LAN-to-WAN', 'allow new') == 2
        assert t.text_for(1) == ('LAN-to-WAN', 'allow established')

    def test_composite_key_with_null_part(self):
        db = FakeDB()
        t = LookupTable(db, 'rules', ('name', 'descr'))
        assert t.id_for('LAN-to-WAN', None) is not None

    def test_composite_key_all_null_is_not_stored(self):
        db = FakeDB()
        t = LookupTable(db, 'rules', ('name', 'descr'))
        assert t.id_for(None, None) is None
        assert db.inserted == []


class TestLookupTableFiltering:
    """Filters resolve text patterns to id lists in Python, never in SQL."""

    @pytest.fixture
    def rules(self):
        return LookupTable(FakeDB([
            (1, 'LAN-to-WAN', 'allow established'),
            (2, 'LAN-to-WAN', 'allow new'),
            (3, 'GUEST-to-WAN', 'allow web'),
            (4, 'IOT-block', 'drop all'),
        ]), 'rules', ('name', 'descr'))

    def test_exact_match(self, rules):
        assert sorted(rules.ids_matching('IOT-block')) == [4]

    def test_substring_match(self, rules):
        assert sorted(rules.ids_matching('to-WAN')) == [1, 2, 3]

    def test_case_insensitive(self, rules):
        assert sorted(rules.ids_matching('iot-BLOCK')) == [4]

    def test_wildcard(self, rules):
        assert sorted(rules.ids_matching('LAN-*')) == [1, 2]

    def test_wildcard_matches_whole_value(self, rules):
        assert rules.ids_matching('*-to-WAN') == [] or sorted(rules.ids_matching('*-to-WAN')) == [1, 2, 3]

    def test_matches_any_key_column(self, rules):
        """Searching the description finds the rule too — it is displayed."""
        assert sorted(rules.ids_matching('drop all')) == [4]

    def test_no_match_returns_empty(self, rules):
        assert rules.ids_matching('nonexistent') == []

    def test_empty_pattern_returns_all(self, rules):
        assert sorted(rules.ids_matching('')) == [1, 2, 3, 4]


class TestLookupTableReload:
    def test_reload_picks_up_external_rows(self):
        db = FakeDB([(1, 'eth0')])
        t = LookupTable(db, 'interfaces', ('name',))
        assert t.text_for(2) is None
        db.rows.append((2, 'eth1'))
        t.reload()
        assert t.text_for(2) == 'eth1'

    def test_reload_drops_stale_entries(self):
        db = FakeDB([(1, 'eth0'), (2, 'eth1')])
        t = LookupTable(db, 'interfaces', ('name',))
        db.rows = [(1, 'eth0')]
        t.reload()
        assert t.text_for(2) is None


class TestSqlValuesClause:
    """The compatibility view builds its closed-set joins from CLOSED_SETS, so
    the SQL cannot drift from the Python mapping."""

    def test_contains_every_member(self):
        clause = lookups.sql_values_clause('rule_action')
        for name, value in CLOSED_SETS['rule_action'].items():
            assert f"({value},'{name}')" in clause

    def test_is_a_values_list(self):
        assert lookups.sql_values_clause('direction').startswith('(VALUES ')

    def test_rejects_a_member_that_would_need_quoting(self, monkeypatch):
        monkeypatch.setitem(CLOSED_SETS, 'bogus', {"o'brien": 1})
        with pytest.raises(ValueError):
            lookups.sql_values_clause('bogus')
