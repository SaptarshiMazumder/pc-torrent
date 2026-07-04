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
        # 100 max conns — Neon's pgbouncer upstream tolerates much more.
        # Sized for Cloud-Run-Tokyo ↔ Neon-us-east-1 cross-region latency
        # (~150 ms RTT) where each query pins a conn for at least that long;
        # bursty monitor + heartbeat traffic was hitting the previous 30 cap.
        _pool = psycopg2.pool.ThreadedConnectionPool(
            1, 100, DATABASE_URL,
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
        # Neon's transaction-mode pooler doesn't honor per-database /
        # per-role search_path defaults, so set it explicitly at the
        # start of every checkout.  Same transaction as the caller's
        # query → same backend → search_path is in effect when the
        # query runs.
        with conn.cursor() as cur:
            cur.execute("SET search_path = public")
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
                # Phase 1 of allocator redesign — store machine_type directly
                # on the job so we don't need to JOIN against the machines
                # table (serverless jobs no longer have machine rows).
                cur.execute(
                    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS machine_type TEXT"
                )
                cur.execute(
                    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS gpu_type TEXT"
                )
                # Phase-11 data-richness: workers push their collected
                # telemetry to PUT /jobs/{id}/telemetry; we stash the
                # opaque dict here, and the orchestrator's
                # ``_record_telemetry`` reads it at chunk completion to
                # populate the final ``render_telemetry`` row.
                cur.execute(
                    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS worker_telemetry_json JSONB"
                )
                # jobs_logger: R2 object key for this attempt's worker
                # stdout log.  Stamped by JobsLoggerService.write_logs_to_r2()
                # after the log is drained from Redis into R2.  One row
                # per attempt (retries create fresh job_ids) so this
                # covers success AND failure -- render_telemetry only
                # gets a row on chunk success.
                cur.execute(
                    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS worker_log_url TEXT"
                )
                # jobs_logger: PCR_* env dict stamped on the row at
                # dispatch time so community agents (which poll for
                # their job later, from another machine) can read the
                # signed URL + identity headers and stamp them onto
                # docker run's -e flags.  Cloud fleets (vast/modal) also
                # populate this for debugging parity, but they consume
                # log_streamer_env directly from context at dispatch.
                cur.execute(
                    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS log_streamer_env_json TEXT"
                )
                # render_telemetry.worker_log_url was declared for the
                # same purpose but is unreachable for failed attempts
                # (no telemetry row).  jobs.worker_log_url is the single
                # source of truth now; drop the telemetry column.
                cur.execute(
                    "ALTER TABLE render_telemetry DROP COLUMN IF EXISTS worker_log_url"
                )
                # Serverless jobs (modal/vast) have no machine row, so
                # machine_id must be NULL on those rows.  Drop the legacy
                # NOT NULL constraint that pre-dates Phase 1.
                cur.execute(
                    "ALTER TABLE jobs ALTER COLUMN machine_id DROP NOT NULL"
                )
                # Phase 2 — fleet-cap-aware dispatch queue.  Each queue row
                # carries enough context to be dispatched later, when a
                # slot opens up in the target fleet.
                # job_id added in the tick-driven dispatch refactor: the
                # caller pre-generates the UUID at enqueue time so the
                # synchronous start_render contract returns DispatchResults
                # with stable IDs even though dispatch fires asynchronously.
                for column_def in (
                    "fleet TEXT",
                    "gpu_type TEXT",
                    "machine_id TEXT",
                    "input_filename TEXT",
                    "render_overrides_json TEXT",
                    "max_retries INTEGER DEFAULT 0",
                    "priority INTEGER DEFAULT 0",
                    "job_id TEXT",
                ):
                    cur.execute(
                        f"ALTER TABLE dispatch_queue ADD COLUMN IF NOT EXISTS {column_def}"
                    )
                # Phase 3 — heaviness signal stored on the render group, used
                # by FastRenderAllocationStrategy to decide chunk count and
                # VRAM filter.
                cur.execute(
                    "ALTER TABLE render_groups ADD COLUMN IF NOT EXISTS r2_input_size_bytes BIGINT"
                )
                # Phase 4 — terminal-state snapshot.  When a group transitions
                # to done/failed/cancelled, the lifecycle layer writes these
                # columns from the children once.  The list endpoint reads
                # them straight from the row for terminal groups so it does
                # not need to re-fetch jobs+machines on every refresh.
                for column_def in (
                    "tasks_count INTEGER",
                    "latest_output_file TEXT",
                    "latest_output_job_id TEXT",
                    "available_output_files_count INTEGER",
                    "overall_rendered_frames INTEGER",
                    # Priority-adjusted actual cost (USD) summed across the
                    # group's chunks, frozen at terminal transition so the
                    # list endpoint can show a finished group's cost without
                    # re-loading its children.  Service projects to credits.
                    "total_actual_cost_usd NUMERIC(10, 4)",
                ):
                    cur.execute(
                        f"ALTER TABLE render_groups ADD COLUMN IF NOT EXISTS {column_def}"
                    )
                # Phase 5 — empirical telemetry.  ``started_at`` is stamped on
                # the first PROGRESS callback (so wall-time excludes queue +
                # provisioning).  ``price_per_hour_at_dispatch`` is a snapshot
                # of the rental rate at job-creation time, immune to later
                # config edits.  ``render_telemetry`` rows are written once
                # per successful chunk for future calibration of the
                # time/cost analyzers.
                cur.execute(
                    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMP WITH TIME ZONE"
                )
                cur.execute(
                    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS price_per_hour_at_dispatch NUMERIC(10, 4)"
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS render_telemetry (
                        id                  TEXT PRIMARY KEY,
                        job_id              TEXT NOT NULL,
                        group_id            TEXT NOT NULL,
                        fleet               TEXT NOT NULL,
                        gpu_type            TEXT,
                        machine_id          TEXT,
                        chunk_size          INTEGER NOT NULL,
                        rendered_frames     INTEGER NOT NULL,
                        started_at          TIMESTAMP WITH TIME ZONE NOT NULL,
                        completed_at        TIMESTAMP WITH TIME ZONE NOT NULL,
                        seconds_total       INTEGER NOT NULL,
                        price_per_hour      NUMERIC(10, 4) NOT NULL,
                        cost_actual_usd     NUMERIC(10, 4) NOT NULL,
                        seconds_estimated   INTEGER,
                        cost_estimated_usd  NUMERIC(10, 4),
                        heaviness_json      JSONB NOT NULL DEFAULT '{}',
                        file_size_bytes     BIGINT,
                        created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS render_telemetry_fleet_gpu "
                    "ON render_telemetry (fleet, gpu_type)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS render_telemetry_group_id "
                    "ON render_telemetry (group_id)"
                )
                # Phase 11 — data-richness additions for alpha-testing
                # telemetry collection.  All NULLABLE; the worker payload
                # is the source for most of these (Phase 1 of the rollout).
                # Server-side fields (retry_count, gpu_model_normalized)
                # populate from Phase 0 on; the rest stay NULL until the
                # new worker image ships.  None of these are read by the
                # render path -- pure collection for future estimation.
                _telemetry_alters = (
                    "gpu_model_normalized   TEXT",
                    "device_used            TEXT",        # OPTIX/CUDA/HIP/CPU/EEVEE
                    "blender_version        TEXT",        # e.g. '5.0.1'
                    "worker_image_version   TEXT",        # e.g. 'dev-v0.0.16'
                    "peak_vram_mb           INTEGER",     # parsed from Blender's Peak:NNNN
                    "denoiser_used          TEXT",        # OPTIX/OPENIMAGEDENOISE/none
                    "gpu_count              INTEGER",     # number of active GPU devices
                    "cpu_cores              INTEGER",
                    "ram_gb                 NUMERIC(6,1)",
                    "startup_seconds        INTEGER",     # before first frame Saved:
                    "render_seconds         INTEGER",     # first-to-last frame Saved:
                    "retry_count            INTEGER",     # jobs.attempt - 1
                    "failure_reason         TEXT",        # oom/timeout/cuda_error/...
                    "gpu_specs_json         JSONB",       # free-form catchall
                )
                for col_def in _telemetry_alters:
                    cur.execute(
                        f"ALTER TABLE render_telemetry "
                        f"ADD COLUMN IF NOT EXISTS {col_def}"
                    )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS render_telemetry_gpu_normalized "
                    "ON render_telemetry (gpu_model_normalized)"
                )
                # Phase 8 — tier selection (Economy / Standard / Premium).
                # Routes a render to the matching allocator and budget.
                # Default 'standard' for legacy rows; new rows are stamped
                # by RenderGroupService at create/confirm time.
                cur.execute(
                    "ALTER TABLE render_groups ADD COLUMN IF NOT EXISTS tier TEXT"
                )
                # Phase 9 — output frames as their own table.  Replaces the
                # jobs.output_files JSON column.  PRIMARY KEY (group_id,
                # filename) enforces frame uniqueness at the DB level:
                # sibling retries that uploaded the same frame collapse to
                # one row at INSERT time, so any caller can count or list
                # frames with straight SQL and never double-count.
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS output_frames (
                        group_id   TEXT        NOT NULL,
                        filename   TEXT        NOT NULL,
                        job_id     TEXT        NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (group_id, filename)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS output_frames_job_idx "
                    "ON output_frames (job_id)"
                )
                # Camera-grouped outputs.  Worker frame files are named
                # ``{camera}_frame####.<ext>`` (always-on grouping), so the
                # frame's identity can no longer be the exact filename.
                # ``frame_number`` is the frame index parsed out of the
                # filename's ``frame####`` token -- a STORED GENERATED
                # column, so it's a DB-enforced projection of the filename
                # (cannot diverge; auto-backfills existing rows of any
                # extension -- ``frame0045.png``, ``Camera_frame0045.exr`` --
                # to 45).  Completion + retry math read this instead of
                # reconstructing/parsing filenames.  NULL for any filename
                # without a frame token (excluded from range counts, which
                # matches the old "silently drop non-matching" behaviour).
                # Pattern requires the trailing ``.`` (the extension
                # boundary) so a camera literally named ``frameNN`` can't be
                # mistaken for the frame token -- the real token is always
                # ``frame####`` immediately followed by the file extension.
                cur.execute(
                    "ALTER TABLE output_frames ADD COLUMN IF NOT EXISTS frame_number INT "
                    "GENERATED ALWAYS AS ((substring(filename from 'frame(\\d+)\\.'))::int) STORED"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS output_frames_group_frame_idx "
                    "ON output_frames (group_id, frame_number)"
                )
                # Primary-output flag.  The worker tags the main render
                # (scene.render.filepath) true and File Output node passes
                # false; the preview selector prefers is_primary so the
                # thumbnail / detail preview show the beauty, not a data
                # pass.  Defaults false for legacy rows + workers not yet
                # redeployed (selector falls back to naming/latest there).
                cur.execute(
                    "ALTER TABLE output_frames ADD COLUMN IF NOT EXISTS "
                    "is_primary BOOLEAN NOT NULL DEFAULT FALSE"
                )
                # Pending allocation queue — chunks/groups that couldn't
                # be allocated to a fleet because nothing eligible existed
                # at allocation time.  Dispatch daemon's per-tick re-eval
                # iterates these and feeds them back through the same
                # planning service strategies (allocate_initial /
                # allocate_retry) until allocation succeeds.  On success,
                # the row is converted into a dispatch_queue entry and
                # deleted.  ``type`` discriminates between full-group
                # initial-plan re-attempts and single-chunk retry
                # re-attempts.
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pending_allocation_queue (
                        id              SERIAL PRIMARY KEY,
                        type            TEXT NOT NULL CHECK (type IN ('initial_group', 'retry_chunk')),
                        group_id        TEXT NOT NULL,
                        chunk_index     INTEGER,
                        attempt         INTEGER,
                        frame_start     INTEGER NOT NULL,
                        frame_end       INTEGER NOT NULL,
                        frame_step      INTEGER NOT NULL,
                        total_frames    INTEGER NOT NULL,
                        engine          TEXT,
                        tier            TEXT,
                        excluded_machine_ids                JSONB NOT NULL DEFAULT '[]'::jsonb,
                        excluded_serverless_capabilities    JSONB NOT NULL DEFAULT '[]'::jsonb,
                        render_overrides_json TEXT,
                        max_retries     INTEGER NOT NULL DEFAULT 0,
                        priority        INTEGER NOT NULL DEFAULT 0,
                        machine_ids     JSONB NOT NULL DEFAULT '[]'::jsonb,
                        input_filename  TEXT,
                        created_at      TEXT NOT NULL,
                        last_attempted_at TEXT
                    )
                    """
                )
                # Drop legacy ``file_size_bytes`` column on existing
                # deployments.  After Phase E, the pending tick reads
                # full heaviness from ``render_groups.resolved_scene_json``;
                # the per-row file size shortcut is redundant.
                cur.execute(
                    "ALTER TABLE pending_allocation_queue "
                    "DROP COLUMN IF EXISTS file_size_bytes"
                )
                # Per-group lookup is the hot path for the detail-page
                # poller's Redis-fallback query.
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS pending_allocation_queue_group_id "
                    "ON pending_allocation_queue (group_id)"
                )

                # Pre-submit / submit boundary refactor — render_groups now
                # carries a single ``resolved_scene_json`` column that is
                # the merge of the analyzer snapshot and the user's
                # render_overrides, computed once at confirm-upload time.
                # The three legacy raw-JSON columns are gone; they were a
                # two-source-of-truth trap (every reader had to remember
                # to merge them, and the estimate path forgot).  The
                # ``analysis_snapshot_json`` canonical home is on the
                # ``user_input_files`` asset row, where it belongs.
                cur.execute(
                    "ALTER TABLE render_groups DROP COLUMN IF EXISTS analysis_snapshot_json"
                )
                cur.execute(
                    "ALTER TABLE render_groups DROP COLUMN IF EXISTS render_overrides_json"
                )
                cur.execute(
                    "ALTER TABLE render_groups DROP COLUMN IF EXISTS analysis_warnings_json"
                )
                cur.execute(
                    "ALTER TABLE render_groups ADD COLUMN IF NOT EXISTS "
                    "resolved_scene_json TEXT NOT NULL DEFAULT '{}'"
                )

                # force_retry on dispatch_queue was a workaround for the
                # old "manual retry needs to bypass the terminal-group
                # guard" problem.  Replaced by the much simpler:
                # ``ManualRetryFlipGroupPendingStep`` flips the group out
                # of terminal state before enqueueing, so the guard
                # never has to be bypassed.
                cur.execute(
                    "ALTER TABLE dispatch_queue DROP COLUMN IF EXISTS force_retry"
                )

                # ``runpod_job_id`` is a misnomer — the column holds Vast.ai
                # instance ids, not RunPod ids.  Rename idempotently: the
                # information_schema check makes this a no-op after the
                # first boot, so deploys are safe to re-run.
                cur.execute(
                    """
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.columns
                             WHERE table_name = 'jobs'
                               AND column_name = 'runpod_job_id'
                        ) AND NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                             WHERE table_name = 'jobs'
                               AND column_name = 'vast_job_id'
                        ) THEN
                            ALTER TABLE jobs RENAME COLUMN runpod_job_id TO vast_job_id;
                        END IF;
                    END $$
                    """
                )

                # Per-chunk cost & time estimates from AllocationPlanner.
                # Stamped on the jobs row at dispatch so the cost service
                # can SUM across a group for "estimated total" and
                # combine with telemetry for live-projection.  The
                # dispatch_queue rows carry the same fields between
                # planner-side stamping and dispatch-side persistence.
                cur.execute(
                    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS "
                    "estimated_seconds DOUBLE PRECISION"
                )
                cur.execute(
                    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS "
                    "estimated_cost_usd DOUBLE PRECISION"
                )
                cur.execute(
                    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS "
                    "estimated_seconds_per_frame DOUBLE PRECISION"
                )
                cur.execute(
                    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS "
                    "estimated_startup_seconds DOUBLE PRECISION"
                )
                # Resolved kill-time deadlines stamped on the row at
                # dispatch.  Single source of truth for both runtime
                # stall enforcement (read by fleet singletons each tick)
                # and UI display (read by /jobs/{id}/allowed-stall-times).
                cur.execute(
                    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS "
                    "allowed_stall_times JSONB"
                )
                cur.execute(
                    "ALTER TABLE dispatch_queue ADD COLUMN IF NOT EXISTS "
                    "price_per_hour DOUBLE PRECISION"
                )
                cur.execute(
                    "ALTER TABLE dispatch_queue ADD COLUMN IF NOT EXISTS "
                    "estimated_seconds DOUBLE PRECISION"
                )
                cur.execute(
                    "ALTER TABLE dispatch_queue ADD COLUMN IF NOT EXISTS "
                    "estimated_cost_usd DOUBLE PRECISION"
                )
                cur.execute(
                    "ALTER TABLE dispatch_queue ADD COLUMN IF NOT EXISTS "
                    "estimated_seconds_per_frame DOUBLE PRECISION"
                )
                cur.execute(
                    "ALTER TABLE dispatch_queue ADD COLUMN IF NOT EXISTS "
                    "estimated_startup_seconds DOUBLE PRECISION"
                )
                # Per-offer Vast fields — captured at planning time so the
                # dispatch handler can rent THIS exact offer (offer_id) and
                # log its driver/OS metadata.  All NULL for Modal/community.
                cur.execute(
                    "ALTER TABLE dispatch_queue ADD COLUMN IF NOT EXISTS "
                    "offer_id BIGINT"
                )
                cur.execute(
                    "ALTER TABLE dispatch_queue ADD COLUMN IF NOT EXISTS "
                    "cuda_version TEXT"
                )
                cur.execute(
                    "ALTER TABLE dispatch_queue ADD COLUMN IF NOT EXISTS "
                    "host_os TEXT"
                )

                # Phase 9 — dispatch_queue uniqueness.  Two concurrent
                # retry signals for the same chunk used to insert two
                # rows; with this constraint the second INSERT is a
                # no-op (paired with ON CONFLICT DO NOTHING in
                # DispatchQueueRepository.enqueue).
                cur.execute(
                    "ALTER TABLE dispatch_queue ALTER COLUMN chunk_index SET NOT NULL"
                )
                cur.execute(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                             WHERE conname = 'dispatch_queue_chunk_unique'
                        ) THEN
                            ALTER TABLE dispatch_queue
                              ADD CONSTRAINT dispatch_queue_chunk_unique
                              UNIQUE (group_id, chunk_index);
                        END IF;
                    END $$
                    """
                )

                # LLM-derived cost estimate snapshot taken at submit
                # time.  Frozen for the lifetime of the group; the
                # UI's "ESTIMATED COST" reads this when present and
                # falls back to SUM(jobs.estimated_cost_usd) when
                # NULL (legacy groups submitted before this column
                # existed).
                cur.execute(
                    "ALTER TABLE render_groups ADD COLUMN IF NOT EXISTS "
                    "pre_render_cost_estimate_usd DOUBLE PRECISION"
                )

                # Per-scene render-time formulas emitted by the LLM
                # cost estimator.  Keyed by an sha256 over the
                # heaviness fields that don't change with render
                # tunables (poly/material/light counts, heavy-feature
                # booleans, engine).  Single row per unique .blend;
                # re-submitting the same scene with different samples
                # or resolution hits the same row.
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS allocation_cost_file_formulas (
                        scene_hash    TEXT PRIMARY KEY,
                        formula       JSONB NOT NULL,
                        llm_provider  TEXT NOT NULL,
                        llm_model     TEXT NOT NULL,
                        created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                    )
                    """
                )

                # Phase 10 -- community commitment window.  When a desktop
                # connects via the agent it sends a user-chosen duration;
                # the server stamps ``commitment_end_at = now + duration``.
                # The allocation planner reads ``available_seconds`` off
                # CommunityMachine (computed at row-read time) to drop
                # targets whose window can't fit a chunk.  NULL on pre-
                # migration rows -- planner treats NULL as "skip the check"
                # so legacy rows keep working until they re-register.
                cur.execute(
                    "ALTER TABLE machines ADD COLUMN IF NOT EXISTS "
                    "commitment_end_at TIMESTAMP WITH TIME ZONE"
                )
                # Cost observability — append-only LLM token usage.  One row per
                # LLM call (recorded by LLMFacade, fail-open), read by the admin
                # cost dashboard's LLM provider.  Additive; nothing else reads
                # or writes it, so it can never affect an existing flow.
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS llm_usage (
                        id            TEXT PRIMARY KEY,
                        occurred_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                        provider      TEXT NOT NULL,
                        model         TEXT NOT NULL,
                        input_tokens  INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS llm_usage_occurred_at "
                    "ON llm_usage (occurred_at DESC)"
                )
        log.info("Database connection pool initialized")
    except Exception as exc:
        log.error("Failed to initialize database: %s", exc)
        raise
