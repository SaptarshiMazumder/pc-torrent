"""Standalone database access layer — serverV2's own connection pool.

No imports from the legacy server/ package.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/pcrent")

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            1, 30, DATABASE_URL,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )
    return _pool


_CHECKOUT_RETRIES = 2


def _checkout_healthy_conn(pool: psycopg2.pool.ThreadedConnectionPool):
    last_exc: Exception | None = None
    for _ in range(_CHECKOUT_RETRIES + 1):
        conn = pool.getconn()
        if conn.closed:
            pool.putconn(conn, close=True)
            continue
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.rollback()
            return conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            last_exc = exc
            pool.putconn(conn, close=True)
    raise last_exc if last_exc else psycopg2.OperationalError("Could not obtain a healthy DB connection")


@contextmanager
def get_conn():
    pool = _get_pool()
    conn = _checkout_healthy_conn(pool)
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            if not conn.closed:
                conn.rollback()
        finally:
            pool.putconn(conn, close=True)
            conn = None
        raise
    finally:
        if conn is not None:
            pool.putconn(conn)


_LEADER_LOCK_KEY = 7_391_823
_leader_conn = None


def try_acquire_leader_lock() -> bool:
    """Acquire a Postgres advisory lock so only one Cloud Run instance runs
    background daemons.  Released automatically when the process exits."""
    global _leader_conn
    import logging
    log = logging.getLogger(__name__)
    try:
        pool = _get_pool()
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_LEADER_LOCK_KEY,))
            row = cur.fetchone()
            acquired = bool(row and row[0])
        conn.commit()
        if acquired:
            _leader_conn = conn
            log.info("Leader election: this instance is leader")
        else:
            pool.putconn(conn)
            log.info("Leader election: another instance is leader — skipping daemons")
        return acquired
    except Exception:
        log.exception("Leader election failed — defaulting to leader")
        return True


_local = threading.local()


@contextmanager
def request_conn():
    if getattr(_local, "conn", None) is not None:
        yield _local.conn
        return
    with get_conn() as conn:
        _local.conn = conn
        try:
            yield conn
        finally:
            _local.conn = None


def _pinned():
    return _local.conn if getattr(_local, "conn", None) is not None else None


def query_one(sql: str, params: tuple | None = None) -> dict[str, Any] | None:
    pinned = _pinned()
    if pinned:
        with pinned.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None


def query_all(sql: str, params: tuple | None = None) -> list[dict[str, Any]]:
    pinned = _pinned()
    if pinned:
        with pinned.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def execute(sql: str, params: tuple | None = None) -> None:
    pinned = _pinned()
    if pinned:
        with pinned.cursor() as cur:
            cur.execute(sql, params)
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def execute_returning(sql: str, params: tuple | None = None) -> dict[str, Any] | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None


def init_db() -> None:
    """Initialize the connection pool, verify connectivity, and ensure any
    serverV2-owned tables exist.  Legacy tables are inherited from v1."""
    import logging
    log = logging.getLogger(__name__)
    try:
        _get_pool()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS in_progress_chunks (
                        group_id       TEXT NOT NULL,
                        chunk_index    INTEGER NOT NULL,
                        current_job_id TEXT NOT NULL,
                        attempt        INTEGER NOT NULL,
                        updated_at     TEXT NOT NULL,
                        PRIMARY KEY (group_id, chunk_index)
                    )
                    """
                )
        log.info("Database connection pool initialized")
    except Exception as exc:
        log.error("Failed to initialize database: %s", exc)
        raise
