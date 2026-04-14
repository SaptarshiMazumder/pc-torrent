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
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 30, DATABASE_URL)
    return _pool


@contextmanager
def get_conn():
    pool = _get_pool()
    conn = pool.getconn()
    if conn.closed:
        pool.putconn(conn, close=True)
        conn = pool.getconn()
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
    """Initialize the connection pool and verify connectivity."""
    import logging
    log = logging.getLogger(__name__)
    try:
        _get_pool()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        log.info("Database connection pool initialized")
    except Exception as exc:
        log.error("Failed to initialize database: %s", exc)
        raise
