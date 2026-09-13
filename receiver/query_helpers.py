"""
Query building helpers shared by log and export endpoints.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import lookups
import services
from search_query import parse_search

logger = logging.getLogger(__name__)

# Lookup tables, injected by deps.py once the database is connected.
#
# Filters resolve text to id lists here in Python rather than joining in SQL.
# The tables hold tens of rows, so the resolution is a dict scan, and doing it
# outside SQL keeps the glob and substring semantics the text columns had.
_LOOKUPS = None


def set_lookups(lk):
    """Bind the lookup tables. Called once at start-up, and by tests."""
    global _LOOKUPS
    _LOOKUPS = lk


def _table(name):
    """One lookup table, or None before injection (routes guard against that)."""
    return getattr(_LOOKUPS, name, None) if _LOOKUPS is not None else None


def _ids_matching(table_name, pattern):
    """Ids in a lookup table whose text matches the pattern."""
    table = _table(table_name)
    return table.ids_matching(pattern) if table is not None else []


def _id_condition(column, ids, negated=False):
    """An IN / NOT IN test over resolved ids, as (sql, params).

    An empty id list means the pattern matched no known value. For a positive
    filter that is an empty result; for a negated one it is no restriction at
    all — the same outcome the text columns produced.
    """
    if not ids:
        return ("1=0", []) if not negated else ("1=1", [])
    placeholders = ','.join(['%s'] * len(ids))
    if negated:
        return (f"({column} NOT IN ({placeholders}) OR {column} IS NULL)", list(ids))
    return (f"{column} IN ({placeholders})", list(ids))


def _add_id_filter(conditions, params, column, ids, negated=False):
    sql, bound = _id_condition(column, ids, negated)
    conditions.append(sql)
    params.extend(bound)

# Single source of truth for valid time ranges and their deltas
_TIME_RANGE_DELTAS = {
    '1h': timedelta(hours=1),
    '6h': timedelta(hours=6),
    '24h': timedelta(hours=24),
    '7d': timedelta(days=7),
    '30d': timedelta(days=30),
    '60d': timedelta(days=60),
    '90d': timedelta(days=90),
    '180d': timedelta(days=180),
    '365d': timedelta(days=365),
}
VALID_TIME_RANGES = set(_TIME_RANGE_DELTAS)


def validate_time_params(
    time_range: Optional[str],
    time_from: Optional[str],
    time_to: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Validate and sanitize time parameters."""
    if time_range and time_range not in VALID_TIME_RANGES:
        time_range = '24h'
    if not time_range and not time_from:
        time_range = '24h'
    if time_from:
        try:
            datetime.fromisoformat(time_from.replace('Z', '+00:00'))
            # Valid custom time_from takes precedence — clear any preset range
            # so build_time_conditions() doesn't emit conflicting bounds.
            time_range = None
        except (ValueError, AttributeError):
            time_from = None
    if time_to:
        try:
            datetime.fromisoformat(time_to.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            time_to = None
    # Re-apply default: time_from may have been supplied but failed validation above
    if not time_range and not time_from:
        time_range = '24h'
    return time_range, time_from, time_to


def _parse_negation(value: str) -> tuple[bool, str]:
    """Check if a filter value is negated (prefixed with '!').
    Returns (is_negated, clean_value).
    """
    if value.startswith('!'):
        return True, value[1:]
    return False, value


def _parse_port(value: str) -> tuple[bool, int | None]:
    """Parse a port filter value, supporting '!' prefix for negation.
    Returns (is_negated, port_int_or_None).
    """
    negated, clean = _parse_negation(value)
    try:
        port = int(clean)
        if 1 <= port <= 65535:
            return negated, port
        logger.debug("Port value out of range (1-65535): %r", value)
    except (ValueError, TypeError):
        logger.debug("Non-numeric port value: %r", value)
    return negated, None


def parse_time_range(time_range: str) -> Optional[datetime]:
    """Convert time range string to a datetime cutoff."""
    now = datetime.now(timezone.utc)
    delta = _TIME_RANGE_DELTAS.get(time_range)
    return now - delta if delta else None


def build_time_conditions(
    time_range: Optional[str],
    time_from: Optional[str],
    time_to: Optional[str],
) -> tuple[list[str], list]:
    """Build timestamp WHERE clauses from time parameters.

    Returns (conditions, params) lists to AND into a query.
    Caller must call validate_time_params() first.
    """
    conditions = []
    params = []
    if time_range:
        cutoff = parse_time_range(time_range)
        if cutoff:
            conditions.append("timestamp >= %s")
            params.append(cutoff)
    if time_from:
        conditions.append("timestamp >= %s")
        params.append(time_from)
    if time_to:
        conditions.append("timestamp <= %s")
        params.append(time_to)
    if not conditions:
        conditions.append("timestamp >= %s")
        params.append(datetime.now(timezone.utc) - timedelta(hours=24))
    return conditions, params


def build_log_query(
    log_type: Optional[str],
    time_range: Optional[str],
    time_from: Optional[str],
    time_to: Optional[str],
    src_ip: Optional[str],
    dst_ip: Optional[str],
    ip: Optional[str],
    direction: Optional[str],
    rule_action: Optional[str],
    rule_name: Optional[str],
    country: Optional[str],
    threat_min: Optional[int],
    search: Optional[str],
    service: Optional[str],
    interface: Optional[str],
    vpn_only: bool = False,
    asn: Optional[str] = None,
    dst_port: Optional[str] = None,
    src_port: Optional[str] = None,
    protocol: Optional[str] = None,
    since: Optional[int] = None,
    before_id: Optional[int] = None,
) -> tuple[str, list]:
    """Build WHERE clause and params from filters.

    `since` and `before_id` are id cursors, not filters: they bound the window
    rather than describe the rows. A request carrying either needs no COUNT(*)
    and no OFFSET, which is what makes the log stream cheap to keep open.
    """
    conditions = []
    params = []

    # 0 means "no cursor yet" — the UI sends it before it has seen a row.
    if since:
        conditions.append("id > %s")
        params.append(int(since))
    if before_id:
        conditions.append("id < %s")
        params.append(int(before_id))

    if log_type:
        types = [t.strip() for t in log_type.split(',')]
        ids = [i for i in (lookups.log_type_id(t) for t in types) if i is not None]
        _add_id_filter(conditions, params, 'log_type_id', ids)

    time_conds, time_params = build_time_conditions(time_range, time_from, time_to)
    conditions.extend(time_conds)
    params.extend(time_params)

    for value, columns in ((src_ip, ('src_ip',)),
                           (dst_ip, ('dst_ip',)),
                           (ip, ('src_ip', 'dst_ip'))):
        if value:
            sql, bound = _address_filter(value, columns)
            conditions.append(sql)
            params.extend(bound)

    if direction:
        directions = [d.strip() for d in direction.split(',')]
        # When VPN filter is active, always include 'vpn' direction so
        # VPN↔LAN traffic isn't excluded by the direction filter.
        if vpn_only and 'vpn' not in directions:
            directions.append('vpn')
        ids = [i for i in (lookups.direction_id(d) for d in directions) if i is not None]
        _add_id_filter(conditions, params, 'direction_id', ids)

    if rule_action:
        negated, val = _parse_negation(rule_action)
        actions = [a.strip() for a in val.split(',')]
        # 'unknown' is not a stored value — it means "no action recorded", so it
        # maps to NULL rather than to an id.
        has_unknown = 'unknown' in actions
        ids = [i for i in (lookups.rule_action_id(a) for a in actions if a != 'unknown')
               if i is not None]
        if negated:
            placeholders = ','.join(['%s'] * len(ids))
            if has_unknown and ids:
                conditions.append(f"(rule_action_id NOT IN ({placeholders}) AND rule_action_id IS NOT NULL)")
            elif has_unknown:
                conditions.append("rule_action_id IS NOT NULL")
            elif ids:
                conditions.append(f"(rule_action_id NOT IN ({placeholders}) OR rule_action_id IS NULL)")
            else:
                conditions.append("1=1")
            params.extend(ids)
        else:
            parts = []
            if ids:
                placeholders = ','.join(['%s'] * len(ids))
                parts.append(f"rule_action_id IN ({placeholders})")
                params.extend(ids)
            if has_unknown:
                parts.append("rule_action_id IS NULL")
            conditions.append(f"({' OR '.join(parts)})" if parts else "1=0")

    if rule_name:
        negated, val = _parse_negation(rule_name)
        # The UI adds a space after ']' for display ("[WAN_LOCAL] Allow All")
        # while the gateway sends it without ("[WAN_LOCAL]Allow All"), so both
        # spellings are tried. Matching runs over the rules table, which covers
        # name and description in one pass.
        ids = set(_ids_matching('rules', val))
        if '] ' in val:
            ids |= set(_ids_matching('rules', val.replace('] ', ']')))
        _add_id_filter(conditions, params, 'rule_id', sorted(ids), negated)

    if country:
        negated, val = _parse_negation(country)
        countries = [c.strip().upper() for c in val.split(',')]
        placeholders = ','.join(['%s'] * len(countries))
        keyword = "NOT IN" if negated else "IN"
        condition = f"geo_country {keyword} ({placeholders})"
        if negated:
            condition = f"({condition} OR geo_country IS NULL)"
        conditions.append(condition)
        params.extend(countries)

    if threat_min is not None:
        conditions.append("threat_score >= %s")
        params.append(threat_min)

    if search:
        # No _parse_negation here: the search parser handles '!' per term, so
        # stripping a leading one first would turn "!443 tcp" into "not (443 and
        # tcp)" instead of "not 443, and tcp".
        sql, bound = _search_condition(search, negated=False)
        conditions.append(sql)
        params.extend(bound)

    if service:
        negated, val = _parse_negation(service)
        # service_name is no longer stored — it is a function of dst_port, so the
        # filter resolves to the ports that carry the service. That hits the port
        # index instead of scanning a text column.
        ports = sorted({
            port
            for name in val.split(',')
            for port in services.ports_for_service(name.strip())
        })
        _add_id_filter(conditions, params, 'dst_port', ports, negated)

    if interface:
        ids = sorted({
            i
            for name in interface.split(',')
            for i in _ids_matching('interfaces', name.strip())
        })
        if ids:
            placeholders = ','.join(['%s'] * len(ids))
            conditions.append(
                f"(iface_in_id IN ({placeholders}) OR iface_out_id IN ({placeholders}))"
            )
            params.extend(ids)
            params.extend(ids)  # Twice: once for each side
        else:
            conditions.append("1=0")

    if asn:
        negated, val = _parse_negation(asn)
        escaped_asn = _escape_like(val)
        op = "NOT ILIKE" if negated else "ILIKE"
        if negated:
            conditions.append(f"(asn_name {op} %s ESCAPE '\\' OR asn_name IS NULL)")
        else:
            conditions.append(f"asn_name {op} %s ESCAPE '\\'") 
        params.append(f"%{escaped_asn}%")

    if dst_port:
        negated, port_val = _parse_port(dst_port)
        if port_val is not None:
            if negated:
                conditions.append("(dst_port != %s OR dst_port IS NULL)")
            else:
                conditions.append("dst_port = %s")
            params.append(port_val)

    if src_port:
        negated, port_val = _parse_port(src_port)
        if port_val is not None:
            if negated:
                conditions.append("(src_port != %s OR src_port IS NULL)")
            else:
                conditions.append("src_port = %s")
            params.append(port_val)

    if protocol:
        negated, val = _parse_negation(protocol)
        ids = sorted({
            i
            for name in val.split(',')
            for i in _ids_matching('protocols', name.strip())
        })
        _add_id_filter(conditions, params, 'protocol_id', ids, negated)

    if vpn_only:
        from parsers import VPN_INTERFACE_PREFIXES
        vpn_ids = sorted({
            i
            for pfx in VPN_INTERFACE_PREFIXES
            for i in _ids_matching('interfaces', f"{pfx}*")
        })
        if vpn_ids:
            placeholders = ','.join(['%s'] * len(vpn_ids))
            conditions.append(
                f"(iface_in_id IN ({placeholders}) OR iface_out_id IN ({placeholders}))"
            )
            params.extend(vpn_ids)
            params.extend(vpn_ids)
        else:
            conditions.append("1=0")

    where = " AND ".join(conditions) if conditions else "1=1"
    return where, params


# Text columns a plain word is searched against. Everything that is not text —
# addresses, ports, protocols, rules, device names — is matched by type instead,
# so it can use an index.
_TEXT_COLUMNS = (
    'rdns', 'geo_country', 'geo_city', 'asn_name',
    'dns_query', 'dns_answer', 'dhcp_event', 'wifi_event',
)

# Lookup-backed columns a word may name. Resolved to id lists in Python.
_TEXT_LOOKUPS = (
    ('rules', 'rule_id'),
    ('protocols', 'protocol_id'),
    ('interfaces', 'iface_in_id'),
    ('interfaces', 'iface_out_id'),
    ('device_names', 'src_device_id'),
    ('device_names', 'dst_device_id'),
    ('device_names', 'hostname_id'),
)

# Closed sets, matched against the words the UI displays.
_TEXT_CLOSED = (
    ('log_type', 'log_type_id'),
    ('rule_action', 'rule_action_id'),
    ('direction', 'direction_id'),
)

# Which columns a field-scoped term restricts to.
_SCOPE_COLUMNS = {
    'src_ip': ('src_ip',),
    'dst_ip': ('dst_ip',),
    'ip': ('src_ip', 'dst_ip'),
    'src_port': ('src_port',),
    'dst_port': ('dst_port',),
    'port': ('src_port', 'dst_port'),
}


def _address_filter(value: str, columns: tuple) -> tuple[str, list]:
    """Build the condition for one of the dedicated address filters.

    The value is classified the same way a search term is, so an address is
    compared as an address: 10.10.10.10 no longer matches 10.10.10.100, and the
    inet index applies. Anything that is not an address — a hostname, a partial
    word — still falls back to a text match, since the field accepts those too.
    """
    negated, raw = _parse_negation(value)
    terms = parse_search(raw)
    term = terms[0] if terms else None

    if term is not None and term.kind in ('ip', 'cidr'):
        operator = '=' if term.kind == 'ip' else '<<='
        parts = [f"{c} {operator} %s" for c in columns]
        params = [term.value] * len(columns)
        clause = f"({' OR '.join(parts)})"
    else:
        pattern = f"%{_escape_like(raw)}%"
        parts = [f"{c}::text ILIKE %s ESCAPE '\\'" for c in columns]
        params = [pattern] * len(columns)
        clause = f"({' OR '.join(parts)})"

    if negated:
        # A row with no address in these columns was not excluded by the user,
        # and `NOT (col = x)` is NULL rather than true when col is NULL.
        return (f"NOT COALESCE({clause}, FALSE)", params)
    return (clause, params)


def _address_condition(term) -> tuple[str, list]:
    """Compare an address as an address.

    An exact term uses equality and a subnet uses containment; both are
    answerable from the inet index. Comparing the text form instead — which is
    what a substring search does — both misses the index and makes 10.10.10.10
    match 10.10.10.100.
    """
    columns = _SCOPE_COLUMNS.get(term.field) or ('src_ip', 'dst_ip')
    operator = '=' if term.kind == 'ip' else '<<='
    parts, params = [], []
    for column in columns:
        parts.append(f"{column} {operator} %s")
        params.append(term.value)
    return f"({' OR '.join(parts)})", params


def _port_condition(term) -> tuple[str, list]:
    """Ports are numbers: 443 must not also match 4430."""
    columns = _SCOPE_COLUMNS.get(term.field) or ('src_port', 'dst_port')
    parts, params = [], []
    for column in columns:
        parts.append(f"{column} = %s")
        params.append(term.value)
    return f"({' OR '.join(parts)})", params


def _mac_condition(term) -> tuple[str, list]:
    return ("mac_address = %s", [term.value])


def _scoped_text_condition(term) -> tuple[str, list]:
    """A word restricted to one field by a prefix."""
    value = str(term.value)
    if term.field == 'rule':
        return _id_condition('rule_id', _ids_matching('rules', value))
    if term.field == 'protocol':
        return _id_condition('protocol_id', _ids_matching('protocols', value))
    if term.field == 'interface':
        ids = _ids_matching('interfaces', value)
        sql, params = _id_condition('iface_in_id', ids)
        sql2, params2 = _id_condition('iface_out_id', ids)
        return f"({sql} OR {sql2})", params + params2
    if term.field == 'host':
        ids = _ids_matching('device_names', value)
        parts, params = [], []
        for column in ('src_device_id', 'dst_device_id', 'hostname_id'):
            sql, bound = _id_condition(column, ids)
            parts.append(sql)
            params.extend(bound)
        return f"({' OR '.join(parts)})", params
    if term.field == 'action':
        return _id_condition('rule_action_id', closed_ids_containing('rule_action', value))
    if term.field == 'log_type':
        return _id_condition('log_type_id', closed_ids_containing('log_type', value))
    if term.field == 'country':
        return ("geo_country = %s", [value.upper()])
    if term.field == 'asn':
        return ("asn_name ILIKE %s ESCAPE '\\'", [f"%{_escape_like(value)}%"])
    return _free_text_condition(term)


def closed_ids_containing(kind, value):
    """Ids of a closed set whose display word contains the term."""
    needle = str(value).lower()
    return [i for text, i in lookups.CLOSED_SETS[kind].items() if needle in text.lower()]


def _free_text_condition(term) -> tuple[str, list]:
    """An unscoped word: anywhere it is displayed."""
    value = str(term.value)
    parts, params = [], []

    if term.glob:
        pattern = _escape_like(value).replace('*', '%')
    else:
        pattern = f"%{_escape_like(value)}%"

    for column in _TEXT_COLUMNS:
        parts.append(f"{column} ILIKE %s ESCAPE '\\'")
        params.append(pattern)

    for table_name, column in _TEXT_LOOKUPS:
        ids = _ids_matching(table_name, value)
        if ids:
            placeholders = ','.join(['%s'] * len(ids))
            parts.append(f"{column} IN ({placeholders})")
            params.extend(ids)

    for kind, column in _TEXT_CLOSED:
        ids = closed_ids_containing(kind, value)
        if ids:
            placeholders = ','.join(['%s'] * len(ids))
            parts.append(f"{column} IN ({placeholders})")
            params.extend(ids)

    # raw_log is only populated for lines the parser could not read — but those
    # are exactly the ones with nothing else to search.
    parts.append("raw_log ILIKE %s ESCAPE '\\'")
    params.append(pattern)

    return f"({' OR '.join(parts)})", params


def _term_condition(term) -> tuple[str, list]:
    if term.kind in ('ip', 'cidr'):
        return _address_condition(term)
    if term.kind == 'port':
        return _port_condition(term)
    if term.kind == 'mac':
        return _mac_condition(term)
    if term.field:
        return _scoped_text_condition(term)
    return _free_text_condition(term)


def _search_condition(value: str, negated: bool) -> tuple[str, list]:
    """Turn the search box's contents into a WHERE fragment.

    Terms are ANDed: each one narrows the result, so the box doubles as a way
    to stack filters without opening the filter panel.
    """
    terms = parse_search(value)
    if not terms:
        return ("1=1", [])

    clauses, params = [], []
    for term in terms:
        sql, bound = _term_condition(term)
        if term.negated:
            # COALESCE, not a bare NOT: in SQL a comparison against NULL is
            # NULL, not true, so `NOT (dst_port = 443)` drops every row without
            # a port. Excluding 443 should not also hide ICMP.
            sql = f"NOT COALESCE({sql}, FALSE)"
        clauses.append(sql)
        params.extend(bound)

    clause = ' AND '.join(clauses)
    if negated:
        return (f"NOT ({clause})", params)
    return (f"({clause})", params)


def _escape_like(value: str) -> str:
    """Escape LIKE wildcard characters in user input."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ── Device-name SQL fragments ────────────────────────────────────────────────

def device_name_client_lateral(ip_expr: str, alias: str = 'c', recency_expr: Optional[str] = None) -> str:
    """LATERAL join for latest unifi_clients row by IP.

    ip_expr: trusted SQL column reference (e.g. 'page.dst_ip', 't.src_ip').
             Interpolated directly into SQL — must never come from user input.
    recency_expr: optional SQL param/expr for recency guard (e.g. '%s').
    Returns SQL fragment: LEFT JOIN LATERAL (...) <alias> ON true
    """
    recency = f" AND last_seen >= {recency_expr} - INTERVAL '1 day'" if recency_expr else ""
    return (f"LEFT JOIN LATERAL ("
            f"    SELECT device_name, hostname, oui"
            f"    FROM unifi_clients WHERE ip = {ip_expr}{recency}"
            f"    ORDER BY last_seen DESC NULLS LAST LIMIT 1"
            f") {alias} ON true")


def device_name_device_lateral(ip_expr: str, alias: str = 'd') -> str:
    """LATERAL join for latest unifi_devices row by IP.

    ip_expr: trusted SQL column reference — interpolated directly, never from user input.
    Returns SQL fragment: LEFT JOIN LATERAL (...) <alias> ON true
    """
    return (f"LEFT JOIN LATERAL ("
            f"    SELECT device_name, model"
            f"    FROM unifi_devices WHERE ip = {ip_expr}"
            f"    ORDER BY updated_at DESC NULLS LAST LIMIT 1"
            f") {alias} ON true")


def device_name_coalesce(
    client_alias: str = 'c',
    device_alias: Optional[str] = None,
    column_alias: str = 'device_name',
    existing_expr: Optional[str] = None,
) -> str:
    """COALESCE expression for device name resolution.

    client_alias: alias for unifi_clients LATERAL (columns: device_name, hostname, oui).
    device_alias: alias for unifi_devices LATERAL (columns: device_name, model). Optional.
    existing_expr: SQL expression to prefer over lookups (e.g. 'page.src_device_name').
    Returns SQL expression for use in SELECT clauses.
    """
    parts = []
    if existing_expr:
        parts.append(existing_expr)
    parts.extend([
        f"{client_alias}.device_name", f"{client_alias}.hostname", f"{client_alias}.oui",
    ])
    if device_alias:
        parts.extend([f"{device_alias}.device_name", f"{device_alias}.model"])
    return f"COALESCE({', '.join(parts)}) AS {column_alias}"


# ── Saved view filter validation ─────────────────────────────────────────────

# Canonical dimension set — single source of truth for flows + saved view validation
ALLOWED_DIMENSIONS = {
    'src_ip', 'dst_ip', 'dst_port', 'protocol',
    'service_name', 'direction', 'interface_in', 'interface_out',
}
_VALID_ACTIONS = {'allow', 'block'}
_VALID_DIRECTIONS = {'inbound', 'outbound', 'inter_vlan', 'nat', 'local', 'vpn'}


def validate_view_filters(filters: dict) -> str | None:
    """Validate saved view filters against canonical backend enums.

    Returns None if valid, or an error message string if invalid.
    Shared by routes/views.py and routes/setup.py (config import).
    """
    if not isinstance(filters, dict):
        return "filters must be a JSON object"

    dims = filters.get('dims')
    if not isinstance(dims, list) or len(dims) != 3:
        return "dims must be an array of exactly 3 values"
    if len(set(dims)) != 3:
        return "dims must contain 3 unique values"
    for d in dims:
        if d not in ALLOWED_DIMENSIONS:
            return f"Invalid dimension: {d}. Allowed: {sorted(ALLOWED_DIMENSIONS)}"

    top_n = filters.get('topN')
    if not isinstance(top_n, int) or top_n < 3 or top_n > 50:
        return "topN must be an integer between 3 and 50"

    actions = filters.get('activeActions')
    if not isinstance(actions, list) or not actions:
        return "activeActions must be a non-empty array"
    if not set(actions).issubset(_VALID_ACTIONS):
        return f"activeActions must be a subset of {sorted(_VALID_ACTIONS)}"

    directions = filters.get('activeDirections')
    if not isinstance(directions, list) or not directions:
        return "activeDirections must be a non-empty array"
    if not set(directions).issubset(_VALID_DIRECTIONS):
        return f"activeDirections must be a subset of {sorted(_VALID_DIRECTIONS)}"

    time_range = filters.get('timeRange')
    if time_range is not None and time_range not in VALID_TIME_RANGES:
        return f"timeRange must be one of {sorted(VALID_TIME_RANGES)} or null"

    return None


# ── CSV export sanitization ─────────────────────────────────────────────────

_CSV_FORMULA_PREFIXES = ('=', '+', '@', ';', '\t', '\r', '\n', '\0')


def sanitize_csv_cell(value: str) -> str:
    """Neutralize spreadsheet formula injection by prepending a single quote."""
    if not value:
        return value
    ch = value[0]
    if ch in _CSV_FORMULA_PREFIXES:
        return "'" + value
    # '-' is only dangerous when NOT followed by a digit or decimal point
    if ch == '-':
        rest = value[1:]
        if not rest or not (rest[0].isdigit() or (rest[0] == '.' and len(rest) > 1 and rest[1].isdigit())):
            return "'" + value
    return value
