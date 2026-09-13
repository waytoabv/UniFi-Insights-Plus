"""Run independent read queries concurrently.

The dashboard endpoints issue eight to ten aggregates that share nothing but a
time window. Run one after another their latency is the sum — 12.5 s on a live
install at 4.7M rows. Run together it is the slowest of them.

Each query gets its own connection, because a PostgreSQL connection carries one
transaction and one statement at a time. Concurrency is bounded below the pool
size so a dashboard load cannot starve the log stream of connections.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from psycopg2.extras import RealDictCursor

logger = logging.getLogger('parallel')

# Four of the ten connections in the pool. Enough to collapse the sum into a
# few rounds, while leaving the log stream and the receiver room to work.
DEFAULT_WORKERS = 4


def run_queries(queries: dict, connect, release=None,
                max_workers: int = DEFAULT_WORKERS, required=()) -> dict:
    """Run `queries` concurrently, returning {name: result}.

    Each value is a callable taking a cursor.

    A query that raises yields None for its key. For a panel among ten that is
    the right outcome — one empty card beats an empty dashboard. For a figure
    the whole view is built on it is not: zeros would read as "no traffic"
    rather than "could not load". Name those in `required` and their failure is
    re-raised.
    """
    if not queries:
        return {}

    failures = {}

    def run_one(item):
        name, fn = item
        conn = connect()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                result = fn(cur)
            conn.commit()
            return name, result
        except Exception as exc:
            logger.exception("Dashboard query %r failed", name)
            failures[name] = exc
            try:
                conn.rollback()
            except Exception:
                logger.debug("Rollback failed for %r", name, exc_info=True)
            return name, None
        finally:
            if release is not None:
                release(conn)

    workers = max(1, min(max_workers, len(queries)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='stats') as pool:
        results = dict(pool.map(run_one, queries.items()))

    for name in required:
        if name in failures:
            raise failures[name]
    return results
