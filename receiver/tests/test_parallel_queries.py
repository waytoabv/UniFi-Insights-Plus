"""Tests for running independent aggregate queries concurrently.

The dashboard's tables endpoint ran ten queries one after another, so its
latency was their sum — measured at 12.5 s on a live install. They share
nothing, so the wall time should be the slowest one rather than the total.
"""

import threading
import time
from unittest.mock import MagicMock

import pytest

from parallel import run_queries


class TestResults:
    def test_returns_each_result_under_its_key(self):
        out = run_queries({
            'a': lambda cur: 1,
            'b': lambda cur: 2,
        }, connect=lambda: MagicMock())
        assert out == {'a': 1, 'b': 2}

    def test_handles_an_empty_set(self):
        assert run_queries({}, connect=lambda: MagicMock()) == {}

    def test_each_query_gets_its_own_connection(self):
        seen = []

        def connect():
            conn = MagicMock()
            seen.append(conn)
            return conn

        run_queries({k: (lambda cur: None) for k in 'abc'}, connect=connect)
        assert len(seen) == 3
        assert len({id(c) for c in seen}) == 3


class TestConcurrency:
    def test_runs_in_parallel_not_in_sequence(self):
        started = threading.Barrier(3, timeout=2)

        def slow(cur):
            # Only returns if all three are in flight at once.
            started.wait()
            return 'ok'

        out = run_queries({k: slow for k in 'abc'},
                          connect=lambda: MagicMock(), max_workers=3)
        assert out == {k: 'ok' for k in 'abc'}

    def test_bounds_concurrency_to_the_pool(self):
        """More queries than workers must queue rather than exhaust the pool."""
        live = []
        peak = [0]
        lock = threading.Lock()

        def track(cur):
            with lock:
                live.append(1)
                peak[0] = max(peak[0], len(live))
            time.sleep(0.01)
            with lock:
                live.pop()

        run_queries({str(i): track for i in range(8)},
                    connect=lambda: MagicMock(), max_workers=3)
        assert peak[0] <= 3


class TestFailures:
    def test_one_failure_does_not_lose_the_others(self):
        def boom(cur):
            raise RuntimeError('nope')

        out = run_queries({'good': lambda cur: 'value', 'bad': boom},
                          connect=lambda: MagicMock())
        assert out['good'] == 'value'
        assert out['bad'] is None

    def test_a_failed_query_rolls_back_its_own_connection(self):
        conns = []

        def connect():
            conn = MagicMock()
            conns.append(conn)
            return conn

        def boom(cur):
            raise RuntimeError('nope')

        run_queries({'bad': boom}, connect=connect)
        conns[0].rollback.assert_called_once()

    def test_every_connection_is_returned(self):
        released = []

        def connect():
            return MagicMock()

        run_queries({'a': lambda cur: 1, 'b': lambda cur: 2},
                    connect=connect, release=released.append)
        assert len(released) == 2

    def test_connections_are_returned_even_when_a_query_raises(self):
        released = []

        def boom(cur):
            raise RuntimeError('nope')

        run_queries({'a': boom}, connect=lambda: MagicMock(), release=released.append)
        assert len(released) == 1


class TestRequiredQueries:
    """A panel among ten may fail quietly; a figure the view is built on may not.

    Returning zeros for a failed count reads as "no traffic" rather than "could
    not load", which is worse than an error.
    """

    def test_a_required_failure_is_raised(self):
        def boom(cur):
            raise RuntimeError('nope')

        with pytest.raises(RuntimeError):
            run_queries({'counts': boom}, connect=lambda: MagicMock(),
                        required=('counts',))

    def test_an_optional_failure_beside_it_is_still_quiet(self):
        def boom(cur):
            raise RuntimeError('nope')

        out = run_queries({'panel': boom, 'counts': lambda cur: 5},
                          connect=lambda: MagicMock(), required=('counts',))
        assert out == {'panel': None, 'counts': 5}

    def test_connections_are_returned_before_raising(self):
        released = []

        def boom(cur):
            raise RuntimeError('nope')

        with pytest.raises(RuntimeError):
            run_queries({'counts': boom}, connect=lambda: MagicMock(),
                        release=released.append, required=('counts',))
        assert len(released) == 1

    def test_the_original_error_is_preserved(self):
        class Specific(Exception):
            pass

        def boom(cur):
            raise Specific('the real cause')

        with pytest.raises(Specific, match='the real cause'):
            run_queries({'counts': boom}, connect=lambda: MagicMock(),
                        required=('counts',))
