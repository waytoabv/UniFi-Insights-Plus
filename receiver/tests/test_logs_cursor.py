"""Tests for cursor-based windowing on /api/logs.

Page-number pagination costs a COUNT(*) over the whole filtered set on every
request — measured at 371 ms for fifty rows on 4.7M — and it forces the UI to
replace the visible page every refresh, which is what makes the list jump while
you are reading it. A cursor answers "what is newer than X" and "what is older
than Y" instead, which needs no total and no OFFSET.
"""

import sys
from unittest.mock import MagicMock

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
        rule_action=None, rule_name=None, country=None,
        threat_min=None, search=None, service=None, interface=None,
        dst_port=None, src_port=None, protocol=None, vpn_only=None, asn=None,
    )
    defaults.update(kw)
    return build_log_query(**defaults)


class TestCursorConditions:
    def test_since_selects_newer_rows(self):
        where, params = build(since=1000)
        assert 'id > %s' in where
        assert 1000 in params

    def test_before_id_selects_older_rows(self):
        where, params = build(before_id=500)
        assert 'id < %s' in where
        assert 500 in params

    def test_both_bound_a_window(self):
        where, params = build(since=100, before_id=200)
        assert 'id > %s' in where and 'id < %s' in where
        assert 100 in params and 200 in params

    def test_absent_by_default(self):
        where, _ = build()
        assert 'id > %s' not in where
        assert 'id < %s' not in where

    def test_zero_is_treated_as_absent(self):
        """The UI sends 0 before it has seen any row."""
        where, _ = build(since=0, before_id=0)
        assert 'id > %s' not in where
        assert 'id < %s' not in where

    def test_combines_with_filters(self):
        where, params = build(since=1000, threat_min=50)
        assert 'id > %s' in where
        assert 'threat_score >= %s' in where


class TestRoute:
    @pytest.fixture
    def client(self, monkeypatch):
        for mod_name in list(sys.modules):
            if mod_name.startswith('routes'):
                monkeypatch.delitem(sys.modules, mod_name, raising=False)

        mock_deps = MagicMock()
        mock_deps.get_conn = MagicMock()
        mock_deps.put_conn = MagicMock()
        # Lookup resolution must return real shapes: rules is a (name, descr)
        # pair, the rest single strings.
        mock_deps.enricher_db = MagicMock()
        lk = mock_deps.enricher_db.lookups
        lk.rules.text_for = MagicMock(return_value=None)
        for table in ('protocols', 'interfaces', 'device_names'):
            getattr(lk, table).text_for = MagicMock(return_value=None)
        monkeypatch.setitem(sys.modules, 'deps', mock_deps)

        mock_db = MagicMock()
        mock_db.get_config = MagicMock(return_value=None)
        mock_db.get_wan_ips_from_config = MagicMock(return_value=[])
        monkeypatch.setitem(sys.modules, 'db', mock_db)

        mock_identity = MagicMock()
        mock_identity.load_identity_config = MagicMock(return_value={})
        mock_identity.annotate_record = MagicMock()
        monkeypatch.setitem(sys.modules, 'ip_identity', mock_identity)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes.logs import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app), mock_deps

    @staticmethod
    def _rows(cursor, rows):
        cursor.fetchall.return_value = rows
        cursor.fetchone.return_value = {'total': len(rows)}

    def test_cursor_mode_issues_no_count(self, client):
        tc, deps = client
        conn = deps.get_conn.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        self._rows(cur, [])

        tc.get('/api/logs?since=42')

        executed = ' '.join(str(c) for c in cur.execute.call_args_list)
        assert 'COUNT(*)' not in executed, \
            "a cursor request must not pay for a total it does not use"

    def test_page_mode_still_counts(self, client):
        tc, deps = client
        conn = deps.get_conn.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        self._rows(cur, [])

        tc.get('/api/logs')

        executed = ' '.join(str(c) for c in cur.execute.call_args_list)
        assert 'COUNT(*)' in executed

    def test_cursor_mode_uses_no_offset(self, client):
        tc, deps = client
        conn = deps.get_conn.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        self._rows(cur, [])

        tc.get('/api/logs?before_id=999')

        executed = ' '.join(str(c) for c in cur.execute.call_args_list)
        assert 'OFFSET' not in executed

    def test_cursor_mode_orders_by_id(self, client):
        """Timestamps tie at this ingest rate; ids are the stable ordering."""
        tc, deps = client
        conn = deps.get_conn.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        self._rows(cur, [])

        tc.get('/api/logs?before_id=999')

        executed = ' '.join(str(c) for c in cur.execute.call_args_list)
        assert 'ORDER BY id DESC' in executed

    def test_response_carries_the_window_bounds(self, client):
        tc, deps = client
        conn = deps.get_conn.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        self._rows(cur, [{'id': 30}, {'id': 20}, {'id': 10}])

        body = tc.get('/api/logs?since=5&per_page=50').json()

        assert body['newest_id'] == 30
        assert body['oldest_id'] == 10
        assert body['has_more'] is False

    def test_has_more_is_reported_without_a_count(self, client):
        """One row beyond the page size answers "is there more" on its own."""
        tc, deps = client
        conn = deps.get_conn.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        self._rows(cur, [{'id': i} for i in range(3, 0, -1)])

        body = tc.get('/api/logs?before_id=99&per_page=2').json()

        assert body['has_more'] is True
        assert len(body['data']) == 2, "the probe row must not be returned"

    def test_empty_window_reports_null_bounds(self, client):
        tc, deps = client
        conn = deps.get_conn.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        self._rows(cur, [])

        body = tc.get('/api/logs?since=999999').json()

        assert body['data'] == []
        assert body['newest_id'] is None
        assert body['oldest_id'] is None
        assert body['has_more'] is False
