"""Tests for query_helpers.py — filter building, validation, and helper functions."""

import pytest

import lookups

from query_helpers import (
    _escape_like,
    _parse_negation,
    _parse_port,
    build_log_query,
    device_name_client_lateral,
    device_name_coalesce,
    device_name_device_lateral,
    sanitize_csv_cell,
    validate_time_params,
    validate_view_filters,
)


# ── _parse_negation ──────────────────────────────────────────────────────────

class TestParseNegation:
    def test_normal_value(self):
        assert _parse_negation('allow') == (False, 'allow')

    def test_negated_value(self):
        assert _parse_negation('!block') == (True, 'block')

    def test_empty_after_bang(self):
        assert _parse_negation('!') == (True, '')


# ── _parse_port ──────────────────────────────────────────────────────────────

class TestParsePort:
    def test_valid_port(self):
        assert _parse_port('443') == (False, 443)

    def test_negated_port(self):
        assert _parse_port('!80') == (True, 80)

    def test_out_of_range_high(self):
        assert _parse_port('70000') == (False, None)

    def test_out_of_range_zero(self):
        assert _parse_port('0') == (False, None)

    def test_non_numeric(self):
        assert _parse_port('abc') == (False, None)

    def test_boundary_1(self):
        assert _parse_port('1') == (False, 1)

    def test_boundary_65535(self):
        assert _parse_port('65535') == (False, 65535)


# ── _escape_like ─────────────────────────────────────────────────────────────

class TestEscapeLike:
    def test_percent(self):
        assert _escape_like('100%') == '100\\%'

    def test_underscore(self):
        assert _escape_like('a_b') == 'a\\_b'

    def test_backslash(self):
        assert _escape_like('a\\b') == 'a\\\\b'

    def test_clean_string(self):
        assert _escape_like('hello') == 'hello'


# ── validate_time_params ─────────────────────────────────────────────────────

class TestValidateTimeParams:
    def test_valid_range(self):
        r, f, t = validate_time_params('24h', None, None)
        assert r == '24h'

    def test_invalid_range_defaults(self):
        r, f, t = validate_time_params('999z', None, None)
        assert r == '24h'

    def test_no_range_no_from_defaults(self):
        r, f, t = validate_time_params(None, None, None)
        assert r == '24h'

    def test_valid_iso_from(self):
        r, f, t = validate_time_params(None, '2026-01-01T00:00:00Z', None)
        assert r is None
        assert f == '2026-01-01T00:00:00Z'

    def test_invalid_from_resets(self):
        r, f, t = validate_time_params(None, 'not-a-date', None)
        # time_from is invalid → cleared → falls back to default range
        assert r == '24h'
        assert f is None

    def test_valid_to(self):
        r, f, t = validate_time_params(None, '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')
        assert t == '2026-01-02T00:00:00Z'

    def test_time_from_takes_precedence_over_range(self):
        r, f, t = validate_time_params(None, '2026-01-01T00:00:00+00:00', None)
        assert r is None
        assert f is not None

    def test_valid_from_clears_preset_range(self):
        """Simulates FastAPI injecting time_range='24h' while frontend sends custom dates."""
        r, f, t = validate_time_params('24h', '2026-01-01T00:00:00Z', '2026-01-10T00:00:00Z')
        assert r is None, "time_range must be cleared when valid time_from is present"
        assert f == '2026-01-01T00:00:00Z'
        assert t == '2026-01-10T00:00:00Z'

    def test_invalid_from_with_range_keeps_range(self):
        """Invalid time_from should not suppress the preset range fallback."""
        r, f, t = validate_time_params('24h', 'not-a-date', None)
        assert r == '24h'
        assert f is None


# ── build_log_query ──────────────────────────────────────────────────────────

class FakeLookupDB:
    """Serves canned lookup rows; see tests/test_lookups.py for the contract."""

    def __init__(self, tables):
        self.tables = tables

    def fetch_lookup(self, table, columns):
        return list(self.tables.get(table, []))

    def insert_lookup(self, table, columns, values):
        rows = self.tables.setdefault(table, [])
        new_id = len(rows) + 1
        rows.append((new_id, *values))
        return new_id


@pytest.fixture(autouse=True)
def _lookups():
    """Bind lookup tables for the whole module.

    build_log_query resolves text filters to ids through these, so the id
    numbers the assertions below expect come from this fixture, not from a
    live database.
    """
    import lookups as lookups_mod
    from query_helpers import set_lookups

    db = FakeLookupDB({
        'rules': [
            (1, 'LAN-to-WAN', 'allow established'),
            (2, '[WAN_LOCAL]Allow All Traffic', None),
            (3, 'IOT-block', 'drop all'),
        ],
        'interfaces': [(1, 'eth0'), (2, 'ppp0'), (3, 'wgsrv0'), (4, 'vti64')],
        'device_names': [(1, 'apple-tv'), (2, 'nas')],
        'protocols': [(1, 'tcp'), (2, 'udp'), (3, 'icmp')],
    })

    class Bundle:
        rules = lookups_mod.LookupTable(db, 'rules', ('name', 'descr'))
        interfaces = lookups_mod.LookupTable(db, 'interfaces', ('name',))
        device_names = lookups_mod.LookupTable(db, 'device_names', ('name',))
        protocols = lookups_mod.LookupTable(db, 'protocols', ('name',))

    set_lookups(Bundle)
    yield Bundle
    set_lookups(None)


class TestBuildLogQuery:
    def _build(self, **kw):
        defaults = dict(
            log_type=None, time_range=None, time_from=None, time_to=None,
            src_ip=None, dst_ip=None, ip=None, direction=None,
            rule_action=None, rule_name=None, country=None,
            threat_min=None, search=None, service=None, interface=None,
            dst_port=None, src_port=None, protocol=None, vpn_only=None,
            asn=None,
        )
        defaults.update(kw)
        return build_log_query(**defaults)

    def test_no_filters(self):
        where, params = self._build()
        # build_time_conditions() always adds a 24h fallback when no time params are given
        assert 'timestamp >= %s' in where
        assert len(params) == 1

    def test_single_log_type(self):
        where, params = self._build(log_type='firewall')
        assert 'log_type_id IN' in where
        assert lookups.log_type_id('firewall') in params

    def test_multiple_log_types(self):
        where, params = self._build(log_type='firewall,dns')
        # 2 log_type placeholders + 1 time fallback
        assert where.count('%s') == 3
        assert lookups.log_type_id('firewall') in params
        assert lookups.log_type_id('dns') in params

    def test_negated_ip(self):
        where, params = self._build(ip='!1.2.3.4')
        assert 'NOT LIKE' in where

    def test_src_ip_filter(self):
        where, params = self._build(src_ip='10.0.0.1')
        assert 'src_ip::text LIKE' in where

    def test_negated_rule_action(self):
        where, params = self._build(rule_action='!block')
        assert 'NOT IN' in where
        assert lookups.rule_action_id('block') in params

    def test_unknown_action_filter(self):
        where, params = self._build(rule_action='unknown')
        assert 'rule_action_id IS NULL' in where
        assert 'unknown' not in params

    def test_unknown_with_block_action_filter(self):
        where, params = self._build(rule_action='block,unknown')
        assert 'rule_action_id IN' in where
        assert 'rule_action_id IS NULL' in where
        assert 'OR' in where
        assert lookups.rule_action_id('block') in params

    def test_negated_unknown_action_filter(self):
        where, params = self._build(rule_action='!unknown')
        assert 'rule_action_id IS NOT NULL' in where

    def test_negated_unknown_with_block(self):
        where, params = self._build(rule_action='!block,unknown')
        assert 'NOT IN' in where
        assert 'IS NOT NULL' in where
        assert lookups.rule_action_id('block') in params

    def test_negated_country(self):
        where, params = self._build(country='!US,CN')
        assert 'NOT IN' in where
        assert 'US' in params
        assert 'CN' in params

    def test_dst_port_valid(self):
        where, params = self._build(dst_port='443')
        assert 'dst_port = %s' in where
        assert 443 in params

    def test_dst_port_invalid_ignored(self):
        where, params = self._build(dst_port='abc')
        # Invalid port ignored; only the 24h time fallback remains
        assert 'dst_port' not in where
        assert 'timestamp >= %s' in where

    def test_negated_protocol(self):
        where, params = self._build(protocol='!tcp')
        assert 'protocol_id NOT IN' in where
        assert 1 in params  # id of 'tcp' in the fixture

    def test_unknown_protocol_matches_nothing(self):
        """A protocol never seen on the wire has no id, so a positive filter
        for it must return no rows rather than every row."""
        where, _ = self._build(protocol='sctp')
        assert '1=0' in where

    def test_vpn_only(self):
        where, params = self._build(vpn_only=True)
        assert 'iface_in_id IN' in where
        assert 'iface_out_id IN' in where
        # wgsrv0 and vti64 match VPN_INTERFACE_PREFIXES; eth0 and ppp0 do not.
        assert 3 in params and 4 in params
        assert 1 not in params and 2 not in params

    def test_combined_filters(self):
        where, params = self._build(
            log_type='firewall', direction='inbound', threat_min=50
        )
        assert 'log_type_id IN' in where
        assert 'direction_id IN' in where
        assert 'threat_score >= %s' in where


# ── validate_view_filters ────────────────────────────────────────────────────

class TestValidateViewFilters:
    VALID = {
        'dims': ['src_ip', 'dst_ip', 'dst_port'],
        'topN': 10,
        'activeActions': ['allow', 'block'],
        'activeDirections': ['inbound', 'outbound'],
    }

    def test_valid(self):
        assert validate_view_filters(self.VALID) is None

    def test_not_dict(self):
        assert validate_view_filters('not a dict') is not None

    def test_wrong_dims_count(self):
        f = {**self.VALID, 'dims': ['src_ip', 'dst_ip']}
        assert 'exactly 3' in validate_view_filters(f)

    def test_duplicate_dims(self):
        f = {**self.VALID, 'dims': ['src_ip', 'src_ip', 'dst_ip']}
        assert 'unique' in validate_view_filters(f)

    def test_invalid_dimension(self):
        f = {**self.VALID, 'dims': ['src_ip', 'dst_ip', 'invalid_dim']}
        assert 'Invalid dimension' in validate_view_filters(f)

    def test_topn_too_low(self):
        f = {**self.VALID, 'topN': 1}
        assert 'topN' in validate_view_filters(f)

    def test_topn_too_high(self):
        f = {**self.VALID, 'topN': 100}
        assert 'topN' in validate_view_filters(f)

    def test_invalid_action(self):
        f = {**self.VALID, 'activeActions': ['invalid']}
        assert 'activeActions' in validate_view_filters(f)

    def test_invalid_direction(self):
        f = {**self.VALID, 'activeDirections': ['invalid']}
        assert 'activeDirections' in validate_view_filters(f)

    def test_empty_actions(self):
        f = {**self.VALID, 'activeActions': []}
        assert 'non-empty' in validate_view_filters(f)

    def test_optional_time_range(self):
        f = {**self.VALID, 'timeRange': '7d'}
        assert validate_view_filters(f) is None

    def test_invalid_time_range(self):
        f = {**self.VALID, 'timeRange': 'bad'}
        assert 'timeRange' in validate_view_filters(f)


# ── device_name_client_lateral ──────────────────────────────────────────────

class TestDeviceNameClientLateral:
    def test_basic_lateral(self):
        sql = device_name_client_lateral('t.src_ip')
        assert 'LEFT JOIN LATERAL' in sql
        assert 'unifi_clients' in sql
        assert 'ip = t.src_ip' in sql
        assert 'ORDER BY last_seen DESC NULLS LAST LIMIT 1' in sql
        assert ') c ON true' in sql

    def test_custom_alias(self):
        sql = device_name_client_lateral('t.dst_ip', alias='c2')
        assert ') c2 ON true' in sql
        assert 'ip = t.dst_ip' in sql

    def test_recency_expr(self):
        sql = device_name_client_lateral('t.src_ip', recency_expr='%s')
        assert "last_seen >= %s - INTERVAL '1 day'" in sql

    def test_no_recency_by_default(self):
        sql = device_name_client_lateral('t.src_ip')
        assert "last_seen >=" not in sql

    def test_selects_name_hostname_oui(self):
        sql = device_name_client_lateral('x.ip')
        assert 'SELECT device_name, hostname, oui' in sql


# ── device_name_device_lateral ──────────────────────────────────────────────

class TestDeviceNameDeviceLateral:
    def test_basic_lateral(self):
        sql = device_name_device_lateral('t.src_ip')
        assert 'LEFT JOIN LATERAL' in sql
        assert 'unifi_devices' in sql
        assert 'ip = t.src_ip' in sql
        assert 'ORDER BY updated_at DESC NULLS LAST LIMIT 1' in sql
        assert ') d ON true' in sql

    def test_custom_alias(self):
        sql = device_name_device_lateral('t.ip', alias='d2')
        assert ') d2 ON true' in sql

    def test_selects_name_and_model(self):
        sql = device_name_device_lateral('x.ip')
        assert 'SELECT device_name, model' in sql


# ── device_name_coalesce ────────────────────────────────────────────────────

class TestDeviceNameCoalesce:
    def test_client_only(self):
        sql = device_name_coalesce()
        assert sql == 'COALESCE(c.device_name, c.hostname, c.oui) AS device_name'

    def test_client_and_device(self):
        sql = device_name_coalesce(device_alias='d')
        assert sql == 'COALESCE(c.device_name, c.hostname, c.oui, d.device_name, d.model) AS device_name'

    def test_custom_aliases(self):
        sql = device_name_coalesce(client_alias='c2', device_alias='d2', column_alias='src_name')
        assert 'c2.device_name' in sql
        assert 'd2.device_name' in sql
        assert 'AS src_name' in sql

    def test_existing_expr_first(self):
        sql = device_name_coalesce(existing_expr='page.src_device_name')
        assert sql.startswith('COALESCE(page.src_device_name, c.device_name')

    def test_existing_expr_with_device_alias(self):
        sql = device_name_coalesce(existing_expr='page.dst_device_name', device_alias='d')
        parts = sql.split('COALESCE(')[1].split(')')[0].split(', ')
        assert parts[0] == 'page.dst_device_name'
        assert parts[1] == 'c.device_name'
        assert 'd.device_name' in parts
        assert 'd.model' in parts


# ── sanitize_csv_cell ───────────────────────────────────────────────────────

class TestSanitizeCsvCell:
    @pytest.mark.parametrize("prefix", ['=', '+', '@', ';', '\t', '\r', '\n', '\0'])
    def test_formula_prefixes_sanitized(self, prefix):
        value = f"{prefix}cmd|'/C calc'"
        assert sanitize_csv_cell(value) == "'" + value

    def test_dash_non_digit_sanitized(self):
        assert sanitize_csv_cell("-cmd|'/C calc'") == "'-cmd|'/C calc'"

    def test_dash_digit_preserved(self):
        assert sanitize_csv_cell("-5") == "-5"
        assert sanitize_csv_cell("-123.45") == "-123.45"
        assert sanitize_csv_cell("-.5") == "-.5"

    def test_dash_dot_no_digit_sanitized(self):
        # "-." is not a negative number — gets sanitized
        assert sanitize_csv_cell("-.") == "'-."

    def test_dash_alone_sanitized(self):
        assert sanitize_csv_cell("-") == "'-"

    def test_normal_strings_unchanged(self):
        assert sanitize_csv_cell("hello") == "hello"
        assert sanitize_csv_cell("192.168.1.1") == "192.168.1.1"
        assert sanitize_csv_cell("2024-01-01") == "2024-01-01"

    def test_empty_string_unchanged(self):
        assert sanitize_csv_cell("") == ""

    def test_formula_chars_mid_string_unchanged(self):
        assert sanitize_csv_cell("hello=1") == "hello=1"
        assert sanitize_csv_cell("value+2") == "value+2"
        assert sanitize_csv_cell("a@b.com") == "a@b.com"
        assert sanitize_csv_cell("key;val") == "key;val"

    def test_numeric_strings_unchanged(self):
        assert sanitize_csv_cell("42") == "42"
        assert sanitize_csv_cell("0") == "0"

    def test_single_quote_prefix_unchanged(self):
        # Single quote is not in _CSV_FORMULA_PREFIXES — not a formula trigger
        assert sanitize_csv_cell("'hello") == "'hello"

    def test_whitespace_only_unchanged(self):
        assert sanitize_csv_cell("   ") == "   "

    def test_none_passthrough(self):
        assert sanitize_csv_cell(None) is None

    def test_non_string_raises(self):
        with pytest.raises(TypeError):
            sanitize_csv_cell(42)


# ── Free-text search ─────────────────────────────────────────────────────────

class TestSearchCondition:
    """The search box covers every displayed field, not just the raw line.

    Replaces the previous `raw_log ILIKE`, which stopped matching once raw_log
    was no longer stored for parsed rows, and which could never find anything
    the parser derived.
    """

    def _build(self, **kw):
        defaults = dict(
            log_type=None, time_range=None, time_from=None, time_to=None,
            src_ip=None, dst_ip=None, ip=None, direction=None,
            rule_action=None, rule_name=None, country=None,
            threat_min=None, search=None, service=None, interface=None,
            dst_port=None, src_port=None, protocol=None, vpn_only=None,
            asn=None,
        )
        defaults.update(kw)
        return build_log_query(**defaults)

    def test_an_address_prefix_matches_the_subnet(self):
        """Superseded substring matching: '10.10.30' now scopes to
        10.10.30.0/24, which is both what was meant and indexable."""
        where, params = self._build(search='10.10.30')
        assert '<<=' in where
        assert '10.10.30.0/24' in params

    def test_covers_enrichment_columns(self):
        where, _ = self._build(search='Google')
        for column in ('rdns', 'geo_country', 'geo_city', 'asn_name'):
            assert f'{column} ILIKE' in where

    def test_a_port_matches_exactly(self):
        """Superseded substring matching: searching 443 no longer returns
        4430 and 8443."""
        where, params = self._build(search='443')
        assert 'dst_port = %s' in where
        assert 443 in params

    def test_matches_rule_names_through_the_lookup(self):
        where, params = self._build(search='IOT')
        assert 'rule_id IN' in where
        assert 3 in params  # IOT-block from the fixture

    def test_matches_rule_descriptions_too(self):
        """The description is displayed next to the name, so it is searchable."""
        where, params = self._build(search='drop all')
        assert 'rule_id IN' in where
        assert 3 in params

    def test_matches_device_names(self):
        where, params = self._build(search='apple')
        assert 'src_device_id IN' in where
        assert 'dst_device_id IN' in where

    def test_matches_protocol(self):
        where, params = self._build(search='udp')
        assert 'protocol_id IN' in where
        assert 2 in params

    def test_matches_action_words(self):
        """Searching 'block' finds blocked traffic — the word the UI shows."""
        where, params = self._build(search='block')
        assert 'rule_action_id IN' in where
        assert lookups.rule_action_id('block') in params

    def test_matches_log_type_words(self):
        where, params = self._build(search='firewall')
        assert 'log_type_id IN' in where
        assert lookups.log_type_id('firewall') in params

    def test_unmatched_lookup_adds_no_clause(self):
        """A term matching no rule must not widen the search to every rule."""
        where, _ = self._build(search='zzzznotarule')
        assert 'rule_id IN' not in where

    def test_still_searches_raw_log(self):
        """Unparsed lines keep their raw text — it is all they have."""
        where, _ = self._build(search='anything')
        assert 'raw_log ILIKE' in where

    def test_negation_applies_per_term(self):
        """'!' now binds to its own term, so "!tcp 443" means "not TCP, and
        port 443" rather than "not (TCP and 443)"."""
        where, _ = self._build(search='!tcp')
        assert 'NOT COALESCE(' in where

    def test_only_the_negated_term_is_excluded(self):
        where, params = self._build(search='!tcp 443')
        assert 'NOT COALESCE(' in where
        assert 443 in params

    def test_terms_are_escaped(self):
        where, params = self._build(search='100%')
        assert all('100\\%' in p for p in params if isinstance(p, str) and '100' in p)
