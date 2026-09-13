"""Tests for translating a parsed log dict into the normalised column tuple."""

import pytest

import lookups
from db import INSERT_COLUMNS, LogLookups, build_log_row, should_store_raw


class FakeDB:
    """Stand-in for Database, shared with test_lookups.py's fixtures."""

    def __init__(self):
        self.rows = {}
        self._next = {}

    def fetch_lookup(self, table, columns):
        return list(self.rows.get(table, []))

    def insert_lookup(self, table, columns, values):
        rows = self.rows.setdefault(table, [])
        new_id = self._next.get(table, 0) + 1
        self._next[table] = new_id
        rows.append((new_id, *values))
        return new_id


@pytest.fixture
def lk():
    return LogLookups(FakeDB())


def parsed(**overrides):
    """A realistic parsed firewall entry; override individual fields per test."""
    base = {
        'timestamp': '2026-09-13T10:00:00+02:00',
        'log_type': 'firewall',
        'direction': 'outbound',
        'src_ip': '10.10.30.5',
        'src_port': 51234,
        'dst_ip': '17.250.64.12',
        'dst_port': 443,
        'protocol': 'tcp',
        'rule_name': 'LAN-to-WAN',
        'rule_desc': 'allow established',
        'rule_action': 'allow',
        'interface_in': 'eth0',
        'interface_out': 'ppp0',
        'mac_address': 'aa:bb:cc:dd:ee:ff',
        'hostname': None,
        'src_device_name': 'apple-tv',
        'dst_device_name': None,
        'raw_log': '<134>Sep 13 10:00:00 gateway kernel: [FW]...',
    }
    base.update(overrides)
    return base


def column(row, name):
    return row[INSERT_COLUMNS.index(name)]


# ── Column list ──────────────────────────────────────────────────────────────

class TestInsertColumns:
    def test_text_columns_are_gone(self):
        for dropped in ('rule_name', 'rule_desc', 'rule_action', 'log_type',
                        'direction', 'protocol', 'interface_in', 'interface_out',
                        'hostname', 'src_device_name', 'dst_device_name',
                        'service_name'):
            assert dropped not in INSERT_COLUMNS

    def test_id_columns_are_present(self):
        for added in ('rule_id', 'rule_action_id', 'log_type_id', 'direction_id',
                      'protocol_id', 'iface_in_id', 'iface_out_id', 'hostname_id',
                      'src_device_id', 'dst_device_id'):
            assert added in INSERT_COLUMNS

    def test_enrichment_columns_survive(self):
        """Geo/ASN/abuse stay denormalised — measured at 6 bytes per row
        because they are NULL on the 96% of rows that are internal allows."""
        for kept in ('geo_country', 'geo_city', 'asn_name', 'threat_score',
                     'threat_categories', 'rdns', 'abuse_usage_type',
                     'abuse_is_tor', 'remote_ip'):
            assert kept in INSERT_COLUMNS

    def test_row_length_matches_columns(self, lk):
        assert len(build_log_row(parsed(), lk)) == len(INSERT_COLUMNS)


# ── Closed sets ──────────────────────────────────────────────────────────────

class TestClosedSetColumns:
    def test_log_type(self, lk):
        row = build_log_row(parsed(log_type='firewall'), lk)
        assert column(row, 'log_type_id') == lookups.log_type_id('firewall')

    def test_rule_action(self, lk):
        row = build_log_row(parsed(rule_action='block'), lk)
        assert column(row, 'rule_action_id') == lookups.rule_action_id('block')

    def test_direction(self, lk):
        row = build_log_row(parsed(direction='inter_vlan'), lk)
        assert column(row, 'direction_id') == lookups.direction_id('inter_vlan')

    def test_missing_direction_is_null(self, lk):
        row = build_log_row(parsed(direction=None), lk)
        assert column(row, 'direction_id') is None

    def test_unknown_value_becomes_null_not_an_error(self, lk):
        """An unmapped value must not drop the whole row — the rest of the
        entry is still worth keeping."""
        row = build_log_row(parsed(rule_action='teleport'), lk)
        assert column(row, 'rule_action_id') is None
        assert column(row, 'src_ip') == '10.10.30.5'


# ── Open sets ────────────────────────────────────────────────────────────────

class TestOpenSetColumns:
    def test_rule_is_interned(self, lk):
        row = build_log_row(parsed(), lk)
        assert column(row, 'rule_id') == lk.rules.id_for('LAN-to-WAN', 'allow established')

    def test_same_rule_reuses_id(self, lk):
        a = build_log_row(parsed(), lk)
        b = build_log_row(parsed(src_ip='10.10.30.6'), lk)
        assert column(a, 'rule_id') == column(b, 'rule_id')

    def test_different_description_is_a_different_rule(self, lk):
        a = build_log_row(parsed(rule_desc='allow established'), lk)
        b = build_log_row(parsed(rule_desc='allow new'), lk)
        assert column(a, 'rule_id') != column(b, 'rule_id')

    def test_interfaces_are_interned_separately(self, lk):
        row = build_log_row(parsed(), lk)
        assert column(row, 'iface_in_id') == lk.interfaces.id_for('eth0')
        assert column(row, 'iface_out_id') == lk.interfaces.id_for('ppp0')

    def test_same_interface_in_and_out_shares_one_id(self, lk):
        row = build_log_row(parsed(interface_in='eth0', interface_out='eth0'), lk)
        assert column(row, 'iface_in_id') == column(row, 'iface_out_id')

    def test_protocol_is_interned(self, lk):
        row = build_log_row(parsed(protocol='tcp'), lk)
        assert column(row, 'protocol_id') == lk.protocols.id_for('tcp')

    def test_unseen_protocol_is_stored_not_dropped(self, lk):
        """protocol arrives from the wire — a new one must not lose data."""
        row = build_log_row(parsed(protocol='sctp'), lk)
        assert column(row, 'protocol_id') is not None
        assert lk.protocols.text_for(column(row, 'protocol_id')) == 'sctp'

    def test_device_names_are_interned(self, lk):
        row = build_log_row(parsed(src_device_name='apple-tv'), lk)
        assert column(row, 'src_device_id') == lk.device_names.id_for('apple-tv')

    def test_hostname_shares_the_device_name_table(self, lk):
        row = build_log_row(parsed(hostname='nas', src_device_name='nas'), lk)
        assert column(row, 'hostname_id') == column(row, 'src_device_id')

    def test_missing_optional_is_null(self, lk):
        row = build_log_row(parsed(src_device_name=None, interface_in=None), lk)
        assert column(row, 'src_device_id') is None
        assert column(row, 'iface_in_id') is None


# ── raw_log policy ───────────────────────────────────────────────────────────

class TestRawLogPolicy:
    def test_default_keeps_only_unparsed(self, monkeypatch):
        monkeypatch.delenv('STORE_RAW_LOG', raising=False)
        assert should_store_raw('unknown') is True
        assert should_store_raw('firewall') is False

    def test_always(self, monkeypatch):
        monkeypatch.setenv('STORE_RAW_LOG', 'always')
        assert should_store_raw('firewall') is True
        assert should_store_raw('unknown') is True

    def test_never(self, monkeypatch):
        monkeypatch.setenv('STORE_RAW_LOG', 'never')
        assert should_store_raw('firewall') is False
        assert should_store_raw('unknown') is False

    def test_unrecognised_setting_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv('STORE_RAW_LOG', 'sometimes')
        assert should_store_raw('unknown') is True
        assert should_store_raw('firewall') is False

    def test_parsed_row_drops_the_raw_line(self, lk, monkeypatch):
        monkeypatch.delenv('STORE_RAW_LOG', raising=False)
        row = build_log_row(parsed(log_type='firewall'), lk)
        assert column(row, 'raw_log') is None

    def test_unparsed_row_keeps_the_raw_line(self, lk, monkeypatch):
        monkeypatch.delenv('STORE_RAW_LOG', raising=False)
        row = build_log_row(parsed(log_type='unknown'), lk)
        assert column(row, 'raw_log') == parsed()['raw_log']


# ── Untouched columns ────────────────────────────────────────────────────────

class TestPassThrough:
    def test_core_fields_are_copied_verbatim(self, lk):
        row = build_log_row(parsed(), lk)
        assert column(row, 'src_ip') == '10.10.30.5'
        assert column(row, 'dst_ip') == '17.250.64.12'
        assert column(row, 'src_port') == 51234
        assert column(row, 'dst_port') == 443
        assert column(row, 'mac_address') == 'aa:bb:cc:dd:ee:ff'

    def test_absent_keys_become_null(self, lk):
        row = build_log_row({'log_type': 'system'}, lk)
        assert column(row, 'src_ip') is None
        assert column(row, 'geo_country') is None
