"""Tests for the dedicated address filters.

Same defect the search box had: comparing the text form meant a filter for
10.10.10.10 also returned 10.10.10.100, and the inet index could not be used.
"""

import pytest

from query_helpers import build_log_query, set_lookups


@pytest.fixture(autouse=True)
def _no_lookups():
    set_lookups(None)
    yield
    set_lookups(None)


def build(**kw):
    defaults = dict(
        log_type=None, time_range=None, time_from=None, time_to=None,
        src_ip=None, dst_ip=None, ip=None, direction=None,
        rule_action=None, rule_name=None, country=None, threat_min=None,
        search=None, service=None, interface=None,
        dst_port=None, src_port=None, protocol=None, vpn_only=None, asn=None,
    )
    defaults.update(kw)
    return build_log_query(**defaults)


class TestExactAddress:
    def test_a_full_address_is_exact(self):
        where, params = build(src_ip='10.10.10.10')
        assert 'src_ip = %s' in where
        assert '10.10.10.10' in params

    def test_it_does_not_compare_as_text(self):
        where, _ = build(src_ip='10.10.10.10')
        assert '::text' not in where
        assert 'LIKE' not in where

    def test_dst_side_too(self):
        where, params = build(dst_ip='1.1.1.1')
        assert 'dst_ip = %s' in where

    def test_either_side(self):
        where, params = build(ip='10.10.10.10')
        assert 'src_ip = %s' in where and 'dst_ip = %s' in where


class TestSubnet:
    def test_cidr_is_containment(self):
        where, params = build(src_ip='10.10.30.0/24')
        assert '<<=' in where
        assert '10.10.30.0/24' in params

    def test_wildcard_becomes_a_subnet(self):
        where, params = build(src_ip='10.10.30.*')
        assert '<<=' in where
        assert '10.10.30.0/24' in params

    def test_a_typed_prefix_becomes_a_subnet(self):
        where, params = build(ip='10.10.30')
        assert '<<=' in where
        assert '10.10.30.0/24' in params


class TestNegation:
    def test_negating_an_address(self):
        where, params = build(src_ip='!10.10.10.10')
        assert 'NOT' in where
        assert '10.10.10.10' in params

    def test_negation_keeps_rows_without_an_address(self):
        """A row with no source address has not been excluded by the user."""
        where, _ = build(src_ip='!10.10.10.10')
        assert 'IS NULL' in where or 'COALESCE' in where


class TestNonAddressInput:
    def test_a_partial_word_still_matches_as_text(self):
        """Hostnames and partial input the parser cannot classify must keep
        working — the field accepts more than addresses."""
        where, _ = build(ip='nas')
        assert 'ILIKE' in where or 'LIKE' in where

    def test_nonsense_does_not_crash(self):
        where, _ = build(src_ip='999.999.999.999')
        assert where
