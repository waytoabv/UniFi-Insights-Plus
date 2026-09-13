"""Tests for turning parsed search terms into SQL.

The point of classifying terms is that each kind gets the comparison its type
deserves: addresses are matched as addresses so the inet index applies and
10.10.10.10 does not match 10.10.10.100.
"""

import pytest

import lookups
from query_helpers import build_log_query, set_lookups


class FakeLookupDB:
    def __init__(self, tables):
        self.tables = tables

    def fetch_lookup(self, table, columns):
        return list(self.tables.get(table, []))

    def insert_lookup(self, table, columns, values):
        rows = self.tables.setdefault(table, [])
        rows.append((len(rows) + 1, *values))
        return len(rows)


@pytest.fixture(autouse=True)
def _lookups():
    db = FakeLookupDB({
        'rules': [(1, 'LAN-to-WAN', 'allow established'), (2, 'IOT-block', 'drop all')],
        'interfaces': [(1, 'eth0'), (2, 'ppp0')],
        'device_names': [(1, 'nas'), (2, 'apple-tv')],
        'protocols': [(1, 'tcp'), (2, 'udp')],
    })

    class Bundle:
        rules = lookups.LookupTable(db, 'rules', ('name', 'descr'))
        interfaces = lookups.LookupTable(db, 'interfaces', ('name',))
        device_names = lookups.LookupTable(db, 'device_names', ('name',))
        protocols = lookups.LookupTable(db, 'protocols', ('name',))

    set_lookups(Bundle)
    yield Bundle
    set_lookups(None)


def build(search):
    return build_log_query(
        log_type=None, time_range=None, time_from=None, time_to=None,
        src_ip=None, dst_ip=None, ip=None, direction=None,
        rule_action=None, rule_name=None, country=None, threat_min=None,
        search=search, service=None, interface=None,
        dst_port=None, src_port=None, protocol=None, vpn_only=None, asn=None,
    )


class TestAddressTerms:
    def test_a_full_address_compares_as_an_address(self):
        where, params = build('10.10.10.10')
        assert 'src_ip = %s' in where and 'dst_ip = %s' in where
        assert '10.10.10.10' in params

    def test_a_full_address_does_not_use_like(self):
        """LIKE on the text form is what made 10.10.10.10 match 10.10.10.100."""
        where, _ = build('10.10.10.10')
        assert 'ILIKE' not in where
        assert '::text' not in where

    def test_a_subnet_uses_containment(self):
        where, params = build('10.10.30.0/24')
        assert '<<=' in where
        assert '10.10.30.0/24' in params

    def test_a_typed_prefix_becomes_containment(self):
        where, params = build('10.10.30.*')
        assert '<<=' in where
        assert '10.10.30.0/24' in params

    def test_scoping_restricts_to_one_side(self):
        where, _ = build('src:10.10.10.10')
        assert 'src_ip = %s' in where
        assert 'dst_ip' not in where


class TestPortTerms:
    def test_a_port_compares_as_a_number(self):
        where, params = build('443')
        assert 'dst_port = %s' in where
        assert 443 in params

    def test_a_port_does_not_match_by_prefix(self):
        where, _ = build('443')
        assert '::text' not in where

    def test_a_port_covers_both_ends(self):
        where, _ = build('443')
        assert 'src_port = %s' in where and 'dst_port = %s' in where

    def test_scoping_restricts_to_one_end(self):
        where, _ = build('dport:443')
        assert 'dst_port = %s' in where
        assert 'src_port' not in where


class TestTextTerms:
    def test_text_searches_the_displayed_columns(self):
        where, _ = build('example')
        assert 'rdns ILIKE' in where

    def test_text_resolves_lookups(self):
        where, params = build('nas')
        assert 'src_device_id IN' in where
        assert 1 in params

    def test_a_protocol_name_resolves_to_its_id(self):
        where, params = build('tcp')
        assert 'protocol_id IN' in where

    def test_an_action_word_matches(self):
        where, params = build('block')
        assert 'rule_action_id IN' in where
        assert lookups.rule_action_id('block') in params


class TestMultipleTerms:
    def test_terms_are_combined_with_and(self):
        where, params = build('10.10.10.10 443')
        assert '10.10.10.10' in params and 443 in params
        # Two independent clauses, not one OR
        assert where.count('AND') >= 1

    def test_a_negated_term_excludes(self):
        where, _ = build('!443')
        assert 'NOT' in where

    def test_mixing_scoped_and_bare_terms(self):
        where, params = build('src:10.0.0.5 443')
        assert '10.0.0.5' in params and 443 in params


class TestEmptyAndEdgeCases:
    def test_an_empty_search_adds_nothing(self):
        where, _ = build('')
        assert 'ILIKE' not in where

    def test_a_term_matching_no_lookup_still_searches_text(self):
        """An unknown word must not silently match everything."""
        where, _ = build('zzzznotathing')
        assert 'rdns ILIKE' in where
        assert 'rule_id IN' not in where


class TestNegationIsNullSafe:
    """In SQL a comparison against NULL is NULL, not true, so a bare NOT drops
    every row where the column is empty. Excluding port 443 must not also hide
    ICMP traffic, which has no port at all."""

    def test_negated_terms_are_coalesced(self):
        where, _ = build('!443')
        assert 'NOT COALESCE(' in where

    def test_negated_address_is_coalesced(self):
        where, _ = build('!10.10.10.10')
        assert 'NOT COALESCE(' in where

    def test_positive_terms_are_not_coalesced(self):
        where, _ = build('443')
        assert 'COALESCE' not in where
