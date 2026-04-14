CREATE TABLE IF NOT EXISTS failure_events (
    id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    endpoint_id TEXT,
    job_id TEXT NOT NULL,
    group_id TEXT,
    failure_type TEXT NOT NULL,
    error_msg TEXT,
    action_taken TEXT NOT NULL,
    reassigned_job_id TEXT,
    reassigned_to_endpoint TEXT,
    resolved BOOLEAN DEFAULT false NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS job_assignments (
    id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    segment_index INTEGER NOT NULL,
    original_machine_id TEXT NOT NULL,
    current_machine_id TEXT NOT NULL,
    power_score REAL DEFAULT 0 NOT NULL,
    frame_start INTEGER NOT NULL,
    frame_end INTEGER NOT NULL,
    frame_step INTEGER DEFAULT 1 NOT NULL,
    active_frame_start INTEGER NOT NULL,
    active_frame_end INTEGER NOT NULL,
    planned_frames INTEGER NOT NULL,
    rendered_frames INTEGER DEFAULT 0 NOT NULL,
    current_frame INTEGER,
    status TEXT DEFAULT 'pending'::text NOT NULL,
    last_heartbeat_at TEXT,
    lease_expires_at TEXT,
    attempt_count INTEGER DEFAULT 0 NOT NULL,
    error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    input_filename TEXT NOT NULL,
    status TEXT DEFAULT 'pending'::text NOT NULL,
    output_files TEXT DEFAULT '[]'::text NOT NULL,
    submitted_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT,
    total_frames INTEGER,
    rendered_frames INTEGER DEFAULT 0 NOT NULL,
    frame_start INTEGER,
    frame_end INTEGER,
    frame_step INTEGER,
    requested_machine_ids TEXT DEFAULT '[]'::text NOT NULL,
    group_id TEXT,
    render_overrides_json TEXT DEFAULT '{}'::text NOT NULL,
    attempt INTEGER DEFAULT 0 NOT NULL,
    max_retries INTEGER DEFAULT 0 NOT NULL,
    priority INTEGER DEFAULT 0 NOT NULL,
    chunk_index INTEGER,
    chunk_size_frames INTEGER,
    runpod_job_id TEXT,
    user_id TEXT,
    last_heartbeat_at TEXT,
    heartbeat_phase TEXT,
    actual_gpu_name TEXT,
    actual_gpu_vram_gb REAL,
    heartbeat_phase_started_at TEXT,
    modal_function_call_id TEXT,
    PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS machines (
    id TEXT NOT NULL,
    machine_key TEXT,
    gpu_model TEXT NOT NULL,
    gpu_vram_gb REAL NOT NULL,
    cpu_cores INTEGER NOT NULL,
    ram_gb REAL NOT NULL,
    status TEXT DEFAULT 'idle'::text NOT NULL,
    registered_at TEXT NOT NULL,
    last_seen_at TEXT,
    os_version TEXT,
    nvidia_driver TEXT,
    machine_type TEXT DEFAULT 'windows'::text NOT NULL,
    user_id TEXT,
    render_speed REAL DEFAULT 1.0 NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS render_groups (
    id TEXT NOT NULL,
    input_filename TEXT NOT NULL,
    r2_input_key TEXT NOT NULL,
    total_frames INTEGER DEFAULT 0 NOT NULL,
    frame_start INTEGER DEFAULT 1 NOT NULL,
    frame_end INTEGER DEFAULT 1 NOT NULL,
    frame_step INTEGER DEFAULT 1 NOT NULL,
    status TEXT DEFAULT 'uploading'::text NOT NULL,
    submitted_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT,
    render_overrides_json TEXT DEFAULT '{}'::text NOT NULL,
    scheduling_json TEXT DEFAULT '{}'::text NOT NULL,
    analysis_snapshot_json TEXT DEFAULT '{}'::text NOT NULL,
    analysis_warnings_json TEXT DEFAULT '[]'::text NOT NULL,
    user_id TEXT,
    source_asset_id TEXT,
    allowed_machine_types_json TEXT,
    PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS user_input_files (
    id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    input_filename TEXT NOT NULL,
    r2_key TEXT NOT NULL,
    frame_start INTEGER,
    frame_end INTEGER,
    frame_step INTEGER,
    analysis_snapshot_json TEXT DEFAULT '{}'::text NOT NULL,
    render_overrides_json TEXT DEFAULT '{}'::text NOT NULL,
    scheduling_json TEXT DEFAULT '{}'::text NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    PRIMARY KEY (id)
);

CREATE INDEX idx_failure_events_job_id ON public.failure_events USING btree (job_id);

CREATE INDEX idx_failure_events_occurred_at ON public.failure_events USING btree (occurred_at DESC);

CREATE INDEX idx_failure_events_provider ON public.failure_events USING btree (provider);

CREATE INDEX idx_job_assignments_lease ON public.job_assignments USING btree (status, lease_expires_at);

CREATE INDEX idx_job_assignments_machine_status ON public.job_assignments USING btree (current_machine_id, status);

CREATE INDEX idx_jobs_machine_pending_priority ON public.jobs USING btree (machine_id, status, priority DESC, submitted_at);

CREATE INDEX idx_jobs_user_id ON public.jobs USING btree (user_id);

CREATE INDEX idx_machines_user_id ON public.machines USING btree (user_id);

CREATE INDEX idx_render_groups_source_asset_id ON public.render_groups USING btree (source_asset_id);

CREATE INDEX idx_render_groups_user_id ON public.render_groups USING btree (user_id);

CREATE INDEX idx_user_input_files_user_last_used ON public.user_input_files USING btree (user_id, last_used_at DESC);

CREATE UNIQUE INDEX uq_user_input_files_user_r2_key ON public.user_input_files (user_id, r2_key);