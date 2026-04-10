import os
import threading
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
            1, 30, DATABASE_URL
        )
    return _pool


@contextmanager
def get_conn():
    """Get a connection from the pool. Auto-commits on success, rolls back on error."""
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


# Thread-local storage so all DB calls within one request share one connection
_local = threading.local()


@contextmanager
def request_conn():
    """
    Context manager that pins ONE connection for the duration of a request.
    All query_one / query_all / execute calls inside will reuse it.
    """
    if getattr(_local, "conn", None) is not None:
        # Already inside a request_conn block — reuse it
        yield _local.conn
        return

    with get_conn() as conn:
        _local.conn = conn
        try:
            yield conn
        finally:
            _local.conn = None


def _get_request_conn():
    """Return the pinned connection if inside request_conn(), else get a fresh one."""
    return _local.conn if getattr(_local, "conn", None) is not None else None


def query_one(sql, params=None):
    """Execute a query and return one row as a dict, or None."""
    pinned = _get_request_conn()
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


def query_all(sql, params=None):
    """Execute a query and return all rows as list of dicts."""
    pinned = _get_request_conn()
    if pinned:
        with pinned.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def execute(sql, params=None):
    """Execute a statement (INSERT, UPDATE, DELETE)."""
    pinned = _get_request_conn()
    if pinned:
        with pinned.cursor() as cur:
            cur.execute(sql, params)
        return
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
                    total_frames INTEGER,
                    rendered_frames INTEGER NOT NULL DEFAULT 0,
                    output_files TEXT NOT NULL DEFAULT '[]',
                    submitted_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT,
                    FOREIGN KEY (machine_id) REFERENCES machines(id)
                );

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS total_frames INTEGER;

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS rendered_frames INTEGER NOT NULL DEFAULT 0;

                CREATE UNIQUE INDEX IF NOT EXISTS idx_machines_machine_key_unique
                ON machines(machine_key)
                WHERE machine_key IS NOT NULL AND machine_key != '';

                CREATE TABLE IF NOT EXISTS render_groups (
                    id TEXT PRIMARY KEY,
                    input_filename TEXT NOT NULL,
                    r2_input_key TEXT NOT NULL,
                    total_frames INTEGER NOT NULL DEFAULT 0,
                    frame_start INTEGER NOT NULL DEFAULT 1,
                    frame_end INTEGER NOT NULL DEFAULT 1,
                    frame_step INTEGER NOT NULL DEFAULT 1,
                    render_overrides_json TEXT NOT NULL DEFAULT '{}',
                    scheduling_json TEXT NOT NULL DEFAULT '{}',
                    analysis_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    analysis_warnings_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'uploading',
                    submitted_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT
                );

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS group_id TEXT REFERENCES render_groups(id);

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS frame_start INTEGER;

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS frame_end INTEGER;

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS frame_step INTEGER DEFAULT 1;

                ALTER TABLE render_groups
                ADD COLUMN IF NOT EXISTS render_overrides_json TEXT NOT NULL DEFAULT '{}';

                ALTER TABLE render_groups
                ADD COLUMN IF NOT EXISTS scheduling_json TEXT NOT NULL DEFAULT '{}';

                ALTER TABLE render_groups
                ADD COLUMN IF NOT EXISTS analysis_snapshot_json TEXT NOT NULL DEFAULT '{}';

                ALTER TABLE render_groups
                ADD COLUMN IF NOT EXISTS analysis_warnings_json TEXT NOT NULL DEFAULT '[]';

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS render_overrides_json TEXT NOT NULL DEFAULT '{}';

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 0;

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS max_retries INTEGER NOT NULL DEFAULT 0;

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0;

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS chunk_index INTEGER;

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS chunk_size_frames INTEGER;

                ALTER TABLE machines
                ADD COLUMN IF NOT EXISTS machine_type TEXT NOT NULL DEFAULT 'windows';

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS runpod_job_id TEXT;

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS modal_function_call_id TEXT;

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS last_heartbeat_at TEXT;

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS heartbeat_phase TEXT;

                CREATE INDEX IF NOT EXISTS idx_jobs_machine_pending_priority
                ON jobs(machine_id, status, priority DESC, submitted_at ASC);

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS user_id TEXT;

                ALTER TABLE render_groups
                ADD COLUMN IF NOT EXISTS user_id TEXT;

                CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id);
                CREATE INDEX IF NOT EXISTS idx_render_groups_user_id ON render_groups(user_id);

                ALTER TABLE machines ADD COLUMN IF NOT EXISTS user_id TEXT;
                CREATE INDEX IF NOT EXISTS idx_machines_user_id ON machines(user_id);

                CREATE TABLE IF NOT EXISTS user_input_files (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    input_filename TEXT NOT NULL,
                    r2_key TEXT NOT NULL,
                    frame_start INTEGER,
                    frame_end INTEGER,
                    frame_step INTEGER,
                    analysis_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    render_overrides_json TEXT NOT NULL DEFAULT '{}',
                    scheduling_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_user_input_files_user_r2_unique
                ON user_input_files(user_id, r2_key);

                CREATE INDEX IF NOT EXISTS idx_user_input_files_user_last_used
                ON user_input_files(user_id, last_used_at DESC);

                ALTER TABLE render_groups
                ADD COLUMN IF NOT EXISTS source_asset_id TEXT;

                CREATE INDEX IF NOT EXISTS idx_render_groups_source_asset_id
                ON render_groups(source_asset_id);

                ALTER TABLE render_groups
                ADD COLUMN IF NOT EXISTS allowed_machine_types_json TEXT;

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS actual_gpu_name TEXT;

                ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS actual_gpu_vram_gb REAL;

                ALTER TABLE machines
                ADD COLUMN IF NOT EXISTS render_speed REAL NOT NULL DEFAULT 1.0;
                """
            )
