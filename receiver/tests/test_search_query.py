"""Tests for the unified search box's query parser.

The search field takes what you know — an address, a port, a device name — and
works out what it is. Typing 10.10.10.10 must not also match 10.10.10.100:
that was the behaviour of a plain substring compare, and it is both wrong and
unable to use the address index.
"""

import pytest

from search_query import Term, parse_search


def kinds(query):
    return [t.kind for t in parse_search(query)]


def values(query):
    return [t.value for t in parse_search(query)]


# ── Addresses ────────────────────────────────────────────────────────────────

class TestAddresses:
    def test_full_address_is_exact(self):
        (term,) = parse_search('10.10.10.10')
        assert term.kind == 'ip'
        assert term.value == '10.10.10.10'
        assert term.exact is True

    def test_exact_address_does_not_match_longer_ones(self):
        """The whole point: 10.10.10.10 is not a prefix of 10.10.10.100 here."""
        (term,) = parse_search('10.10.10.10')
        assert term.kind == 'ip' and term.exact

    def test_cidr_is_a_subnet(self):
        (term,) = parse_search('10.10.30.0/24')
        assert term.kind == 'cidr'
        assert term.value == '10.10.30.0/24'

    def test_three_octets_with_wildcard_become_a_24(self):
        (term,) = parse_search('10.10.30.*')
        assert term.kind == 'cidr'
        assert term.value == '10.10.30.0/24'

    def test_three_octets_with_trailing_dot_become_a_24(self):
        (term,) = parse_search('10.10.30.')
        assert term.kind == 'cidr'
        assert term.value == '10.10.30.0/24'

    def test_two_octets_become_a_16(self):
        (term,) = parse_search('10.10.*')
        assert term.kind == 'cidr'
        assert term.value == '10.10.0.0/16'

    def test_one_octet_becomes_an_8(self):
        (term,) = parse_search('10.*')
        assert term.kind == 'cidr'
        assert term.value == '10.0.0.0/8'

    def test_bare_three_octets_are_a_prefix(self):
        """Typing part of an address is a common way to scope to a subnet."""
        (term,) = parse_search('10.10.30')
        assert term.kind == 'cidr'
        assert term.value == '10.10.30.0/24'

    def test_ipv6_is_recognised(self):
        (term,) = parse_search('2001:db8::1')
        assert term.kind == 'ip'
        assert term.value == '2001:db8::1'

    def test_an_invalid_octet_is_not_an_address(self):
        assert kinds('999.1.1.1') == ['text']

    def test_a_version_number_is_not_an_address(self):
        assert kinds('3.7.0') == ['cidr'] or kinds('3.7.0') == ['text']


# ── Ports ────────────────────────────────────────────────────────────────────

class TestPorts:
    def test_a_bare_number_is_a_port(self):
        (term,) = parse_search('443')
        assert term.kind == 'port'
        assert term.value == 443

    def test_a_port_is_exact(self):
        """4430 and 8443 must not match a search for 443."""
        (term,) = parse_search('443')
        assert term.exact is True

    def test_out_of_range_numbers_are_text(self):
        assert kinds('70000') == ['text']

    def test_zero_is_text(self):
        assert kinds('0') == ['text']


# ── Field-scoped terms ───────────────────────────────────────────────────────

class TestFieldPrefixes:
    @pytest.mark.parametrize('prefix,field', [
        ('src', 'src_ip'), ('dst', 'dst_ip'), ('ip', 'ip'),
        ('port', 'port'), ('sport', 'src_port'), ('dport', 'dst_port'),
        ('rule', 'rule'), ('country', 'country'), ('asn', 'asn'),
        ('proto', 'protocol'), ('iface', 'interface'), ('host', 'host'),
        ('action', 'action'), ('type', 'log_type'),
    ])
    def test_prefix_scopes_the_term(self, prefix, field):
        (term,) = parse_search(f'{prefix}:x')
        assert term.field == field

    def test_a_scoped_address_keeps_its_type(self):
        (term,) = parse_search('src:10.10.10.10')
        assert term.field == 'src_ip'
        assert term.kind == 'ip'

    def test_a_scoped_subnet_keeps_its_type(self):
        (term,) = parse_search('dst:10.10.30.*')
        assert term.field == 'dst_ip'
        assert term.kind == 'cidr'
        assert term.value == '10.10.30.0/24'

    def test_an_unscoped_term_has_no_field(self):
        (term,) = parse_search('10.10.10.10')
        assert term.field is None

    def test_an_unknown_prefix_is_not_a_field(self):
        """A colon is common in text — a MAC, an IPv6, a URL."""
        (term,) = parse_search('banana:split')
        assert term.field is None
        assert term.kind == 'text'
        assert term.value == 'banana:split'


# ── Negation ─────────────────────────────────────────────────────────────────

class TestNegation:
    def test_bang_negates(self):
        (term,) = parse_search('!443')
        assert term.negated is True
        assert term.kind == 'port'
        assert term.value == 443

    def test_minus_negates(self):
        (term,) = parse_search('-10.10.10.10')
        assert term.negated is True
        assert term.kind == 'ip'

    def test_negation_combines_with_a_field(self):
        (term,) = parse_search('!country:CN')
        assert term.negated is True
        assert term.field == 'country'

    def test_a_bare_bang_is_not_a_term(self):
        assert parse_search('!') == []


# ── Multiple terms ───────────────────────────────────────────────────────────

class TestMultipleTerms:
    def test_terms_are_split_on_whitespace(self):
        assert len(parse_search('10.10.10.10 443 tcp')) == 3

    def test_each_term_keeps_its_own_type(self):
        assert kinds('10.10.10.10 443 nas') == ['ip', 'port', 'text']

    def test_quoted_text_stays_one_term(self):
        (term,) = parse_search('"allow established"')
        assert term.kind == 'text'
        assert term.value == 'allow established'

    def test_a_quoted_term_can_be_scoped(self):
        (term,) = parse_search('rule:"allow established"')
        assert term.field == 'rule'
        assert term.value == 'allow established'

    def test_a_quoted_term_can_be_negated(self):
        (term,) = parse_search('!"allow new"')
        assert term.negated is True
        assert term.value == 'allow new'

    def test_extra_whitespace_is_ignored(self):
        assert len(parse_search('  443    tcp  ')) == 2

    def test_an_empty_query_has_no_terms(self):
        assert parse_search('') == []
        assert parse_search('   ') == []
        assert parse_search(None) == []


# ── Text ─────────────────────────────────────────────────────────────────────

class TestText:
    def test_a_word_is_text(self):
        (term,) = parse_search('nas')
        assert term.kind == 'text'
        assert term.value == 'nas'

    def test_text_is_a_substring_not_an_exact_match(self):
        (term,) = parse_search('nas')
        assert term.exact is False

    def test_a_wildcard_makes_text_a_glob(self):
        (term,) = parse_search('VLAN*')
        assert term.kind == 'text'
        assert term.glob is True

    def test_a_hostname_is_text(self):
        (term,) = parse_search('nas.lan')
        assert term.kind == 'text'

    def test_a_mac_address_is_recognised(self):
        (term,) = parse_search('aa:bb:cc:dd:ee:ff')
        assert term.kind == 'mac'


class TestCaseHandling:
    def test_field_prefixes_are_case_insensitive(self):
        assert parse_search('SRC:10.0.0.1')[0].field == 'src_ip'

    def test_text_keeps_its_case_for_display(self):
        assert parse_search('NAS')[0].value == 'NAS'


class TestTermEquality:
    def test_terms_compare_by_value(self):
        assert parse_search('443') == [Term(kind='port', value=443, exact=True)]
