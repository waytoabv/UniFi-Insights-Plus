"""Tests for hiding the collector's own syslog traffic.

The gateway sends its logs to this host, and those packets are themselves
firewall events: at 61 rows/s they are a visible share of every view while
saying nothing about the network. The setting removes them from what is shown
without removing them from the database, so it takes effect the moment it is
switched.
"""

import pytest

from query_helpers import build_log_query, set_lookups, syslog_exclusion


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


class TestExclusionClause:
    def test_off_by_default(self):
        assert syslog_exclusion(enabled=False, port=514, collector_ips=[]) == (None, [])

    def test_matches_the_syslog_port(self):
        sql, params = syslog_exclusion(enabled=True, port=514, collector_ips=[])
        assert 'dst_port' in sql
        assert 514 in params

    def test_narrows_to_the_collector_when_known(self):
        """Port alone would also hide syslog between other hosts."""
        sql, params = syslog_exclusion(enabled=True, port=514, collector_ips=['10.10.10.106'])
        assert 'dst_ip' in sql
        assert '10.10.10.106' in params

    def test_keeps_rows_without_a_port(self):
        """ICMP has no port; excluding syslog must not exclude it."""
        sql, _ = syslog_exclusion(enabled=True, port=514, collector_ips=[])
        assert 'COALESCE' in sql or 'IS NULL' in sql

    def test_several_collector_addresses(self):
        sql, params = syslog_exclusion(
            enabled=True, port=514, collector_ips=['10.0.0.1', '10.0.0.2'])
        assert '10.0.0.1' in params and '10.0.0.2' in params

    def test_a_custom_port(self):
        _, params = syslog_exclusion(enabled=True, port=5141, collector_ips=[])
        assert 5141 in params


class TestInQueries:
    def test_absent_when_disabled(self):
        where, _ = build(hide_syslog=False)
        assert 'dst_port' not in where

    def test_present_when_enabled(self):
        where, params = build(hide_syslog=True, syslog_collector_ips=['10.10.10.106'])
        assert '10.10.10.106' in params
        assert 514 in params

    def test_combines_with_other_filters(self):
        where, params = build(hide_syslog=True, rule_action='block')
        assert 514 in params
        assert where.count('AND') >= 1

    def test_a_deliberate_syslog_filter_still_works(self):
        """Asking for port 514 explicitly should show it, not fight the setting."""
        where, params = build(hide_syslog=True, dst_port='514')
        assert params.count(514) >= 1
