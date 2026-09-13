"""Parser for the unified search box.

One field takes whatever you know — an address, a port, a device name — and
works out what it is, rather than comparing everything as text.

That distinction is not cosmetic. Comparing as text made a search for
10.10.10.10 also return 10.10.10.100, and it put the address columns beyond the
reach of their index: a term with few matches had to scan the whole time
window. Recognising the address instead gives an exact, indexed comparison.

Several terms separated by spaces all have to match, so the box doubles as a
way to stack filters:

    10.10.30.0/24 443 !tcp        that subnet, port 443, not TCP
    src:10.0.0.5 rule:"LAN to"    scoped to one field each
    nas denied                    both words, anywhere they are displayed

Grammar, informally:

    query   := term*
    term    := ('!' | '-')? (field ':')? value
    value   := '"' ... '"' | bare
    field   := src | dst | ip | port | sport | dport | rule | country
             | asn | proto | iface | host | action | type
"""

import ipaddress
import re
import shlex
from dataclasses import dataclass
from typing import Optional

# Search prefix → the filter it scopes to. Deliberately short: these are typed
# often, and the long forms are already available as dedicated filter inputs.
FIELD_ALIASES = {
    'src': 'src_ip',
    'source': 'src_ip',
    'dst': 'dst_ip',
    'dest': 'dst_ip',
    'ip': 'ip',
    'port': 'port',
    'sport': 'src_port',
    'dport': 'dst_port',
    'rule': 'rule',
    'country': 'country',
    'asn': 'asn',
    'proto': 'protocol',
    'protocol': 'protocol',
    'iface': 'interface',
    'interface': 'interface',
    'host': 'host',
    'hostname': 'host',
    'action': 'action',
    'type': 'log_type',
}

_MAC_RE = re.compile(r'^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$', re.I)
_PREFIX_RE = re.compile(r'^(\d{1,3}(?:\.\d{1,3}){0,2})\.?\*?$')


@dataclass(frozen=True)
class Term:
    """One search term, classified.

    kind    what the value is: ip, cidr, port, mac or text
    value   normalised — a CIDR string, an int port, the raw text
    field   the filter it was scoped to with a prefix, or None for "anywhere"
    negated the term must not match
    exact   compare for equality rather than containment
    glob    the text contains '*' and should be matched as a pattern
    """
    kind: str
    value: object
    field: Optional[str] = None
    negated: bool = False
    exact: bool = False
    glob: bool = False


def parse_search(query: Optional[str]) -> list[Term]:
    """Split a search string into classified terms. Never raises."""
    if not query or not query.strip():
        return []

    try:
        tokens = shlex.split(query)
    except ValueError:
        # An unbalanced quote while the user is still typing. Fall back to
        # whitespace splitting so the box keeps working mid-keystroke.
        tokens = query.split()

    return [term for token in tokens if (term := _classify(token)) is not None]


def _classify(token: str) -> Optional[Term]:
    negated = False
    if token[:1] in ('!', '-'):
        # A leading '-' is also how a negative is written, but a bare sign is
        # not a term — the user is mid-keystroke.
        rest = token[1:]
        if not rest:
            return None
        # Keep '-' as text when it is part of a word: -foo negates, a-b does not.
        negated = True
        token = rest

    field = None
    if ':' in token:
        prefix, _, remainder = token.partition(':')
        candidate = FIELD_ALIASES.get(prefix.strip().lower())
        # Only a known prefix scopes: colons also appear in MACs, IPv6
        # addresses and ordinary text.
        if candidate and remainder:
            field, token = candidate, remainder

    if not token:
        return None

    kind, value, exact, glob = _typed(token)
    return Term(kind=kind, value=value, field=field, negated=negated,
                exact=exact, glob=glob)


def _typed(token: str):
    """Work out what a bare value is. Returns (kind, value, exact, glob)."""
    # Explicit subnet.
    if '/' in token:
        try:
            network = ipaddress.ip_network(token, strict=False)
            return 'cidr', str(network), False, False
        except ValueError:
            pass

    # Full address.
    try:
        return 'ip', str(ipaddress.ip_address(token)), True, False
    except ValueError:
        pass

    # Partial address — a prefix is how you scope to a subnet by typing.
    network = _prefix_to_network(token)
    if network is not None:
        return 'cidr', network, False, False

    if _MAC_RE.match(token):
        return 'mac', token.lower(), True, False

    if token.isdigit():
        port = int(token)
        if 0 < port <= 65535:
            return 'port', port, True, False

    return 'text', token, False, '*' in token


def _prefix_to_network(token: str) -> Optional[str]:
    """'10.10.30.*', '10.10.30.' or '10.10.30' → '10.10.30.0/24'."""
    match = _PREFIX_RE.match(token)
    if not match:
        return None
    octets = match.group(1).split('.')
    if not all(0 <= int(o) <= 255 for o in octets):
        return None
    # A single octet with no dot or star is a number, not an address.
    if len(octets) == 1 and token == octets[0]:
        return None
    prefix_length = 8 * len(octets)
    padded = '.'.join(octets + ['0'] * (4 - len(octets)))
    try:
        return str(ipaddress.ip_network(f'{padded}/{prefix_length}', strict=False))
    except ValueError:
        return None
