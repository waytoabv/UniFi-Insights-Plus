"""Tests for routes/stats.py — /api/stats/overview, /api/stats/charts, /api/stats/tables.

Critical: deps.py creates DB connections at import time.
We must mock the deps module BEFORE importing api.py.
"""

import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def client(monkeypatch):
    """Create a FastAPI TestClient with mocked deps module."""
    for mod_name in list(sys.modules):
        if mod_name.startswith('routes'):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)

    mock_deps = MagicMock()
    mock_deps.get_conn = MagicMock()
    mock_deps.put_conn = MagicMock()
    mock_deps.enricher_db = MagicMock()

    monkeypatch.setitem(sys.modules, 'deps', mock_deps)

    mock_db_module = MagicMock()
    mock_db_module.get_config = MagicMock(return_value=None)
    mock_db_module.get_wan_ips_from_config = MagicMock(return_value=[])
    monkeypatch.setitem(sys.modules, 'db', mock_db_module)

    mock_ip_identity = MagicMock()
    mock_ip_identity.load_identity_config = MagicMock(return_value={})
    mock_ip_identity.annotate_ip = MagicMock(return_value=(None, None, None))
    mock_ip_identity.annotate_record = MagicMock()
    monkeypatch.setitem(sys.modules, 'ip_identity', mock_ip_identity)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.stats import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app), mock_deps, mock_db_module


def _mock_cursor_results(mock_deps, results_sequence):
    """Set up a mock connection/cursor that returns results_sequence in order.

    Each entry in results_sequence is either:
    - a dict (fetchone) → returned via fetchone()
    - a list of dicts (fetchall) → returned via fetchall()
    """
    mock_conn = MagicMock()
    mock_cursor = MagicMock()

    call_iter = iter(results_sequence)

    def fetchone_side_effect():
        try:
            val = next(call_iter)
        except StopIteration:
            raise AssertionError("_mock_cursor_results: iterator exhausted — not enough results provided")
        if not isinstance(val, dict):
            raise AssertionError(f"_mock_cursor_results: fetchone() expected dict, got {type(val).__name__}")
        return val

    def fetchall_side_effect():
        try:
            val = next(call_iter)
        except StopIteration:
            raise AssertionError("_mock_cursor_results: iterator exhausted — not enough results provided")
        if not isinstance(val, list):
            raise AssertionError(f"_mock_cursor_results: fetchall() expected list, got {type(val).__name__}")
        return val

    mock_cursor.fetchone = MagicMock(side_effect=fetchone_side_effect)
    mock_cursor.fetchall = MagicMock(side_effect=fetchall_side_effect)
    mock_cursor.execute = MagicMock()

    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_deps.get_conn.return_value = mock_conn
    return mock_conn, mock_cursor


class TestStatsOverview:
    def test_overview_returns_expected_keys(self, client):
        test_client, mock_deps, _ = client

        # overview runs: 1) scalar counts (fetchone), 2) by_direction (fetchall), 3) by_type (fetchall)
        _mock_cursor_results(mock_deps, [
            {'total': 1000, 'allowed': 500, 'blocked': 300, 'threats': 10},  # fetchone
            # Grouped on ids now — the names are attached in Python, over the
            # handful of groups rather than every row in the window.
            [{'direction_id': 1, 'count': 600}, {'direction_id': 2, 'count': 400}],  # fetchall
            [{'log_type_id': 1, 'count': 800}, {'log_type_id': 2, 'count': 200}],  # fetchall
        ])

        resp = test_client.get('/api/stats/overview?time_range=24h')
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 1000
        assert data['allowed'] == 500
        assert data['blocked'] == 300
        assert data['threats'] == 10
        assert data['by_direction'] == {'inbound': 600, 'outbound': 400}
        assert data['by_type'] == {'firewall': 800, 'dns': 200}
        assert data['time_range'] == '24h'

    def test_overview_default_time_range(self, client):
        test_client, mock_deps, _ = client
        _mock_cursor_results(mock_deps, [
            {'total': 0, 'allowed': 0, 'blocked': 0, 'threats': 0},
            [],
            [],
        ])

        resp = test_client.get('/api/stats/overview')
        assert resp.status_code == 200
        assert resp.json()['time_range'] == '24h'

    def test_overview_db_failure(self, client):
        test_client, mock_deps, _ = client

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Exception('DB error')
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_deps.get_conn.return_value = mock_conn

        resp = test_client.get('/api/stats/overview?time_range=24h')
        assert resp.status_code == 500
        assert 'detail' in resp.json()


class TestStatsCharts:
    def test_charts_returns_expected_keys(self, client):
        test_client, mock_deps, _ = client

        ts = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
        # The two series run concurrently now, so results are matched by what
        # the query selects rather than by call order.
        TestStatsTables._by_query(mock_deps, {
            'rule_action_id, COUNT(*)': [
                {'period': ts, 'rule_action_id': 1, 'count': 80},
                {'period': ts, 'rule_action_id': 2, 'count': 20},
            ],
        }, default=[{'period': ts, 'count': 100}])

        resp = test_client.get('/api/stats/charts?time_range=24h')
        assert resp.status_code == 200
        data = resp.json()
        assert 'logs_over_time' in data
        assert 'logs_per_hour' in data  # backward-compat alias
        assert 'traffic_by_action' in data
        assert len(data['logs_over_time']) == 1
        assert data['logs_over_time'] == data['logs_per_hour']
        assert data['traffic_by_action'][0]['allow'] == 80
        assert data['traffic_by_action'][0]['block'] == 20

    def test_charts_db_failure(self, client):
        test_client, mock_deps, _ = client

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Exception('DB error')
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_deps.get_conn.return_value = mock_conn

        resp = test_client.get('/api/stats/charts?time_range=24h')
        assert resp.status_code == 500
        assert 'detail' in resp.json()


class TestStatsTables:
    """The tables endpoint runs its aggregates concurrently, so results are
    matched to queries by what they select rather than by call order."""

    @staticmethod
    def _by_query(mock_deps, routes, default=None):
        """Return canned rows based on a fragment of the SQL being executed."""
        fallback = default if default is not None else []

        def make_conn(*_a, **_kw):
            cursor = MagicMock()
            state = {'rows': fallback}

            def execute(sql, params=None):
                text = sql if isinstance(sql, str) else str(sql)
                state['rows'] = next(
                    (rows for fragment, rows in routes.items() if fragment in text),
                    fallback,
                )

            cursor.execute = MagicMock(side_effect=execute)
            cursor.fetchall = MagicMock(side_effect=lambda: state['rows'])
            cursor.fetchone = MagicMock(
                side_effect=lambda: state['rows'][0] if state['rows'] else None)

            conn = MagicMock()
            conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
            conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            return conn

        # stats.py bound get_conn at import time, so the existing mock is
        # reconfigured rather than replaced — rebinding the attribute would
        # leave the module pointing at the old one.
        mock_deps.get_conn.side_effect = make_conn
        mock_deps.get_conn.return_value = None

    def test_tables_returns_expected_keys(self, client):
        test_client, mock_deps, _ = client

        self._by_query(mock_deps, {
            'geo_country AS country': [
                {'country': 'US', 'rule_action_id': 2, 'count': 50},
                {'country': 'CN', 'rule_action_id': 1, 'count': 30},
            ],
            'dst_port, protocol_id, rule_action_id': [
                {'dst_port': 22, 'protocol_id': 1, 'rule_action_id': 2, 'count': 40},
            ],
            'dns_query': [{'dns_query': 'example.com', 'count': 150}],
        }, default=[{'ip': '1.2.3.4', 'count': 100, 'country': 'US',
                     'asn': 'AS1234', 'threat_score': 90, 'device_name': 'PC1'}])

        resp = test_client.get('/api/stats/tables?time_range=7d')
        assert resp.status_code == 200
        data = resp.json()

        for key in ('top_blocked_countries', 'top_blocked_ips', 'top_blocked_internal_ips',
                    'top_threat_ips', 'top_blocked_services', 'top_allowed_destinations',
                    'top_allowed_countries', 'top_allowed_services',
                    'top_active_internal_ips', 'top_dns'):
            assert key in data, f"Missing key: {key}"

        assert data['top_blocked_countries'] == [{'country': 'US', 'count': 50}]
        assert data['top_allowed_countries'] == [{'country': 'CN', 'count': 30}]
        assert data['top_dns'][0]['dns_query'] == 'example.com'

    def test_services_are_resolved_from_port_and_protocol(self, client):
        """service_name is no longer stored, so the aggregate groups by the
        columns that are and maps afterwards — over the surviving groups
        rather than every row in the window."""
        test_client, mock_deps, _ = client

        # The protocol id has to resolve to a real name for the IANA lookup.
        mock_deps.enricher_db.lookups.protocols.text_for = MagicMock(return_value='tcp')

        self._by_query(mock_deps, {
            'dst_port, protocol_id, rule_action_id': [
                {'dst_port': 22, 'protocol_id': 1, 'rule_action_id': 2, 'count': 40},
            ],
        })

        data = test_client.get('/api/stats/tables?time_range=24h').json()
        assert data['top_blocked_services'] == [{'service_name': 'ssh', 'count': 40}]

    def test_one_failing_query_does_not_empty_the_dashboard(self, client):
        """Ten panels, ten queries: one failing should cost one panel rather
        than the whole response."""
        test_client, mock_deps, _ = client

        def make_conn(*_a, **_kw):
            cursor = MagicMock()

            def execute(sql, params=None):
                if 'dns_query' in str(sql):
                    raise Exception('DB error')

            cursor.execute = MagicMock(side_effect=execute)
            cursor.fetchall = MagicMock(return_value=[])
            cursor.fetchone = MagicMock(return_value=None)

            conn = MagicMock()
            conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
            conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            return conn

        # stats.py bound get_conn at import time, so the existing mock is
        # reconfigured rather than replaced — rebinding the attribute would
        # leave the module pointing at the old one.
        mock_deps.get_conn.side_effect = make_conn
        mock_deps.get_conn.return_value = None

        resp = test_client.get('/api/stats/tables?time_range=24h')
        assert resp.status_code == 200
        assert resp.json()['top_dns'] == []

    def test_every_connection_is_returned_to_the_pool(self, client):
        """Eight concurrent queries against a pool of ten — leaking one would
        starve the log stream after a few dashboard loads."""
        test_client, mock_deps, _ = client
        self._by_query(mock_deps, {})

        test_client.get('/api/stats/tables?time_range=24h')

        assert mock_deps.put_conn.call_count == mock_deps.get_conn.call_count
