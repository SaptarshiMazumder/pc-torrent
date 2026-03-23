import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://localhost:5432/pcrent",
)

# Connection pool (min 1, max 10 connections)
_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            1, 10, DATABASE_URL
        )
    return _pool


@contextmanager
def get_conn():
    """Get a connection from the pool. Auto-commits on success, rolls back on error."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def query_one(sql, params=None):
    """Execute a query and return one row as a dict, or None."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None


def query_all(sql, params=None):
    """Execute a query and return all rows as list of dicts."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def execute(sql, params=None):
    """Execute a statement (INSERT, UPDATE, DELETE)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def init_db():
    """Create tables if they don't exist."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS machines (
                    id TEXT PRIMARY KEY,
                    machine_key TEXT,
                    gpu_model TEXT NOT NULL,
                    gpu_vram_gb REAL NOT NULL,
                    cpu_cores INTEGER NOT NULL,
                    ram_gb REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'idle',
                    registered_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    os_version TEXT,
                    nvidia_driver TEXT
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    machine_id TEXT NOT NULL,
                    input_filename TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    output_files TEXT NOT NULL DEFAULT '[]',
                    submitted_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT,
                    FOREIGN KEY (machine_id) REFERENCES machines(id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_machines_machine_key_unique
                ON machines(machine_key)
                WHERE machine_key IS NOT NULL AND machine_key != '';
                """
            )
