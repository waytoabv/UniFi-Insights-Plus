"""ID ↔ text mapping for the normalised log schema.

The logs table stores small integer keys instead of repeated text. This module
owns both directions of that mapping so no other module has to know whether a
given column is backed by a constant or by a table.

Two kinds of value sets, deliberately handled differently:

**Closed sets** — log_type, rule_action, direction. These are produced by our
own parsers, so the complete set is known at build time and lives here as a
constant. Ids are pinned by a test: they are written into every row, and
reassigning one would silently rewrite history.

**Open sets** — protocol, interfaces, rule names, device names. These arrive
from the network. A new VLAN, a new firewall rule or an unusual protocol must
never lose data, so they get a lookup table and are assigned an id on first
sight.

Resolution happens in Python on both sides. A filter turns text into a list of
ids before the WHERE clause is built, and serialisation turns ids back into
text after the rows are read — so a lookup never costs a SQL join, and glob and
negation semantics stay identical to what the text columns supported.
"""

import logging
import re
import threading

logger = logging.getLogger('lookups')


# ── Closed sets ──────────────────────────────────────────────────────────────

# Pinned by tests/test_lookups.py::test_ids_are_stable. Add new members at the
# end with the next free number; never renumber an existing one.
CLOSED_SETS = {
    'log_type': {
        'firewall': 1,
        'dns': 2,
        'dhcp': 3,
        'wifi': 4,
        'system': 5,
        'unknown': 6,
    },
    'rule_action': {
        'allow': 1,
        'block': 2,
        'redirect': 3,
    },
    'direction': {
        'inbound': 1,
        'outbound': 2,
        'inter_vlan': 3,
        'nat': 4,
        'local': 5,
        'vpn': 6,
    },
}

_REVERSE = {name: {v: k for k, v in mapping.items()} for name, mapping in CLOSED_SETS.items()}


def _closed_id(kind, text):
    if text is None:
        return None
    value = CLOSED_SETS[kind].get(str(text).strip().lower())
    if value is None:
        logger.warning("Unknown %s value %r — storing NULL. Add it to CLOSED_SETS.", kind, text)
    return value


def _closed_text(kind, value):
    if value is None:
        return None
    return _REVERSE[kind].get(value)


def log_type_id(text):
    return _closed_id('log_type', text)


def log_type_text(value):
    return _closed_text('log_type', value)


def rule_action_id(text):
    return _closed_id('rule_action', text)


def rule_action_text(value):
    return _closed_text('rule_action', value)


def direction_id(text):
    return _closed_id('direction', text)


def direction_text(value):
    return _closed_text('direction', value)


def closed_ids_matching(kind, pattern):
    """Ids whose text matches `pattern`, with the same glob/substring rules as
    LookupTable.ids_matching. Used by filters on closed-set columns."""
    matcher = _matcher(pattern)
    if matcher is None:
        return list(CLOSED_SETS[kind].values())
    return [v for text, v in CLOSED_SETS[kind].items() if matcher(text)]


# ── Matching ─────────────────────────────────────────────────────────────────

def _matcher(pattern):
    """Build a predicate for a filter pattern, or None to match everything.

    A pattern containing '*' is anchored and globbed; anything else matches as a
    case-insensitive substring. This mirrors what the text columns did before
    normalisation, so filter behaviour is unchanged by the schema.
    """
    if pattern is None or pattern == '':
        return None
    pattern = str(pattern)
    if '*' in pattern:
        escaped = re.escape(pattern).replace(r'\*', '.*')
        rx = re.compile(f'^{escaped}$', re.IGNORECASE)
        return lambda value: bool(rx.match(value))
    needle = pattern.lower()
    return lambda value: needle in value.lower()


# ── Open sets ────────────────────────────────────────────────────────────────

class LookupTable:
    """In-memory mirror of a small (id, value…) table, with get-or-create.

    The tables this wraps hold tens of rows, not thousands, so the whole thing
    is cached and every lookup is a dict access. Writes go through the database
    first so a concurrent process sees the same id.
    """

    def __init__(self, db, table, columns):
        self._db = db
        self._table = table
        self._columns = tuple(columns)
        self._lock = threading.Lock()
        self._by_key = {}
        self._by_id = {}
        self.reload()

    # ── reading ──

    def reload(self):
        """Re-read the table, discarding anything no longer present."""
        by_key, by_id = {}, {}
        for row in self._db.fetch_lookup(self._table, self._columns):
            row_id, values = row[0], tuple(row[1:])
            key = self._key(values)
            by_key[key] = row_id
            by_id[row_id] = values[0] if len(values) == 1 else values
        with self._lock:
            self._by_key, self._by_id = by_key, by_id

    def text_for(self, row_id):
        """The stored value for an id — a string, or a tuple for composite keys."""
        if row_id is None:
            return None
        return self._by_id.get(row_id)

    def ids_matching(self, pattern):
        """Every id whose value matches the pattern. An empty pattern matches all.

        For composite keys any column may match, so a search for a rule
        description finds the rule — the description is displayed alongside it.
        """
        matcher = _matcher(pattern)
        if matcher is None:
            return list(self._by_id.keys())
        hits = []
        for row_id, value in self._by_id.items():
            parts = value if isinstance(value, tuple) else (value,)
            if any(p is not None and matcher(str(p)) for p in parts):
                hits.append(row_id)
        return hits

    # ── writing ──

    def id_for(self, *values):
        """The id for these values, creating the row on first sight.

        Returns None when every part is empty — an all-NULL key carries no
        information and would collide with every other empty row.
        """
        if all(v is None or v == '' for v in values):
            return None
        normalised = tuple(None if v == '' else v for v in values)
        key = self._key(normalised)

        existing = self._by_key.get(key)
        if existing is not None:
            return existing

        with self._lock:
            existing = self._by_key.get(key)
            if existing is not None:
                return existing
            row_id = self._db.insert_lookup(self._table, self._columns, normalised)
            self._by_key[key] = row_id
            self._by_id[row_id] = normalised[0] if len(normalised) == 1 else normalised
            return row_id

    # ── internals ──

    def _key(self, values):
        return tuple(None if v is None else str(v).strip().lower() for v in values)

    def __len__(self):
        return len(self._by_id)
