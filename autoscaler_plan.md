# RunPod Autoscaler + Failure Detection Plan

---

## My Questions (verbatim)

> "ok so we need to make a system wide failure tracker, as well as the problem with runpod is that the workers are often stuck on initializing perpetually which is why the workers never get to running, and lets say there are 3 workers assigned to a runpod endpoint — 2 of the workers started running and while 1 is stuck on initializing (for whatever the fuck reason idk) then when one of the 2 workers finishes, the stuck worker's task is picked up by it, essentially wasting time cause the whole point of many workers is parallel rendering"

> "i need constant polling from each worker to make sure that ANY WORKER STUCK ON INITIALIZING for >45 secs is KILLED IMMEDIATELY and immediately a new worker is found somewhere else, same endpoint or different endpoint or different farm idk wherever available"

> "the system wide failure tracker assigns this — I need to see ALL THESE FAILURE AND REASSIGNMENT IN LOGS, and i should be able to view them from an endpoint"

> "i need an autoscaler for each individual service also, for now lets focus on making for runpod — this autoscaler as soon as runpod is receiving job, it immediately makes each runpod endpoint min workers to 2, and max to 4, and when job is finished it scales both back to 0"

> "this autoscaler shouldn't get stuck between jobs, like for failed jobs, cancelled jobs etc it should never keep it hanging to 2 and 4! it will cost me money!!"

> "so i need the cancel-all-jobs endpoint to work flawlessly, as well as failure detection from runpod to be extremely robust and catch failures for any cases (like stuck in initializing >45s, errored out or timed out or job was cancelled or error in code or OOM or anything at all)"

> "even if rendering frame takes time, polling should still be there right like hey im running but im still alive"

---

## Core Problems

### Problem 1: Workers stuck initializing with no detection
RunPod transitions a job to `IN_PROGRESS` status the moment the container starts — **before your handler code runs**. The worker then has to:
1. Download the `.blend` file (can take 30s+ for large files, can hang silently)
2. Extract if zip
3. Start Blender

During all of this, `rendered_frames = 0`. The current stale detection (`IN_PROGRESS_STALE_SEC = 5400`) only triggers after **90 minutes**. A worker with a hung blend download will sit there for 90 minutes before the server notices.

There is also **zero heartbeat** from the RunPod worker. It calls `_mark_running` once, then silence until frames start arriving. The server cannot distinguish "downloading blend" from "container hung and dead."

### Problem 2: Stuck IN_QUEUE workers wait too long
`IN_QUEUE_TIMEOUT_SEC = 120`. A worker stuck queuing wastes 2 full minutes before the server kills it.

### Problem 3: Queue-failed workers retry on the same broken endpoint (the wrong retry logic)
In [`runpod_dispatch.py:548`](server/services/runpod_dispatch.py#L548), when any job fails (regardless of failure reason), the code first retries on the **same endpoint**. For a queue timeout or throttle, the endpoint infrastructure itself is the problem — retrying there immediately wastes another 45-120 seconds and almost certainly fails again.

The current code conflates two very different failure types:
- **Infrastructure failure** (IN_QUEUE, throttle): endpoint is broken → should skip same-endpoint retry, go straight to failover
- **Worker-level failure** (OOM, render crash, code error): endpoint is fine → retry on same endpoint makes sense

With 3 workers on 1 endpoint and `max_retries = 1`, a queue stall burns `2 × 45s = 90s` per worker before each reaches failover. Total wasted time for 3 workers: `3 × 90s = 4.5 minutes`.

### Problem 4: No autoscaler — endpoint stays at min=2/max=4 forever
Currently there is no mechanism to scale RunPod endpoints up when jobs arrive or down when jobs finish. If anyone dispatches a render, workers spin up and stay at whatever static config you set — billing you for idle GPU time indefinitely.

### Problem 5: cancel-all-jobs does not scale down
[`cancel_render_group`](server/main.py#L2606) cancels RunPod jobs via API and marks DB rows cancelled. It does **not** trigger autoscaler scale-down. After cancellation, the endpoint continues billing at whatever `workersMin` is set.

### Problem 6: No failure visibility
There is no unified view of what failed, why, and what happened next. Failures are scattered across log lines in per-job polling threads. There is no API endpoint to query failure history.

---

## What RunPod FlashBoot Is (Free)

When I said "45s is reasonable if your image is pre-cached (FlashBoot)" — FlashBoot is RunPod's cold-start optimization. It pre-caches your container image on workers so they start in under 2 seconds instead of 20-45 seconds.

**It is completely free.** You enable it by setting `flashBootType: "FLASHBOOT"` on your endpoint config via the GraphQL `saveEndpoint` mutation — which the autoscaler will call anyway when scaling. With FlashBoot + autoscaler pre-warming workers to `min=2` before jobs land, most workers skip cold start entirely.

---

## Solution Overview

Four components, in implementation priority order:

| # | Component | File(s) | Impact |
|---|-----------|---------|--------|
| 1 | Autoscaler + scale-down safety sweep | `server/services/runpod_autoscaler.py` | Stops money leak |
| 2 | Aggressive init/queue timeout + fix queue-retry bug | `server/services/runpod_dispatch.py` | Stops the stuck worker problem |
| 3 | System-wide failure tracker + API endpoint | `server/services/failure_tracker.py`, `server/main.py` | Full visibility |
| 4 | Worker heartbeat | `runpod_worker/handler.py` | Precise init stall detection |

---

## Component 1: RunPod Autoscaler

### File: `server/services/runpod_autoscaler.py`

#### How RunPod autoscaling works
RunPod exposes a GraphQL `saveEndpoint` mutation that lets you set `workersMin` and `workersMax` at any time:

```graphql
mutation {
  saveEndpoint(input: {
    id: "<endpoint_id>",
    workersMin: 2,
    workersMax: 4,
    flashBootType: "FLASHBOOT"
  }) {
    id
    workersMin
    workersMax
  }
}
```

This is a simple HTTP POST to `https://api.runpod.io/graphql?api_key=<key>`. There is no SDK needed — plain `httpx` works.

#### Scale-up logic
Called immediately when jobs are dispatched to a RunPod endpoint (hook into the existing dispatch loop in [`main.py:2477-2512`](server/main.py#L2477-L2512)):

```python
# Pseudocode — before dispatching jobs:
for endpoint in affected_runpod_endpoints:
    autoscaler.scale_up(endpoint_id)   # sets min=2, max=4, flashBootType=FLASHBOOT
```

Config (env vars):
- `RUNPOD_AUTOSCALE_MIN_WORKERS` (default: `2`)
- `RUNPOD_AUTOSCALE_MAX_WORKERS` (default: `4`)

#### Scale-down logic (THE CRITICAL PART)
Scale-down must fire from **multiple places** so that no failure mode leaves workers running.

**Trigger points:**

1. **Job completion hook** — inside `start_polling_thread` when any job reaches `COMPLETED`, `FAILED`, or `CANCELLED`. After updating DB, check: does this endpoint have any remaining active jobs? If no → scale down.

2. **Cancel-all-jobs** — at the end of `cancel_render_group` and `cancel_all_render_groups`, explicitly call `autoscaler.scale_down(endpoint_id)` for every affected endpoint.

3. **Safety sweep timer** — a background thread that runs every 60 seconds and checks every RunPod endpoint:
   ```sql
   SELECT COUNT(*) FROM jobs j
   JOIN machines m ON j.machine_id = m.id
   WHERE m.machine_key = 'runpod-serverless-<endpoint_id>'
     AND j.status IN ('pending', 'running')
   ```
   If count = 0 but the endpoint is still scaled up (tracked in memory) → scale down immediately. This is the backstop that catches every edge case.

4. **On server startup** — scale all endpoints to 0 on startup. If the server crashed while workers were running, this prevents orphaned billing.

#### Scale-down guard: "is the endpoint actually idle?"
Before calling scale-down, verify two conditions:
1. No jobs in DB with `status IN ('pending', 'running')` for this endpoint's `machine_id`
2. No active polling threads for this endpoint (tracked in an in-memory set `_active_jobs_per_endpoint: dict[str, set]`)

Only when both are empty: call `saveEndpoint(workersMin=0, workersMax=0)`.

#### In-memory state tracked by autoscaler
```python
_endpoint_scaled_up: dict[str, bool]        # endpoint_id -> currently scaled up?
_active_jobs_per_endpoint: dict[str, set]    # endpoint_id -> set of active job_ids
_scale_lock: threading.Lock                  # prevent race conditions
```

#### Handling failures in the autoscaler itself
If the GraphQL call to scale down fails (network error, RunPod API down), log it as an error and retry in the next safety sweep. Never silently skip it.

---

## Component 2: Aggressive Timeout + Fix Queue-Retry Bug

### File: `server/services/runpod_dispatch.py`

#### Change 1: Lower IN_QUEUE timeout from 120s to 45s
```python
# Before:
IN_QUEUE_TIMEOUT_SEC = _env_float("IN_QUEUE_TIMEOUT_SEC", 120)

# After:
IN_QUEUE_TIMEOUT_SEC = _env_float("IN_QUEUE_TIMEOUT_SEC", 45)
```

With FlashBoot enabled, 45s is generous. Workers with cached images start in 1-2 seconds. 45s allows for the rare true cold start while eliminating the 2-minute wait for stuck workers.

#### Change 2: Add "initializing stall" detection (new timeout tier)
This is the new tier that catches workers stuck in `IN_PROGRESS` before any frames render.

Inside the polling loop, after RunPod reports `IN_PROGRESS` for the first time, start a separate `init_deadline`:

```python
# New state variables added to _poll():
first_in_progress_at: float | None = None
INIT_STALL_SEC = 60  # kill if IN_PROGRESS but rendered_frames == 0 for 60s

# Inside the polling loop, in the elif rp_status == "IN_PROGRESS": block:
if first_in_progress_at is None:
    first_in_progress_at = time.monotonic()

cur_frames = job.get("rendered_frames") or 0
if cur_frames == 0 and (time.monotonic() - first_in_progress_at) > INIT_STALL_SEC:
    init_error = (
        f"Worker IN_PROGRESS for {INIT_STALL_SEC}s but rendered_frames still 0 "
        f"- likely stuck on initialization (blend download/extract/Blender start)"
    )
    log.warning(f"Job {job_id}: {init_error}")
    cancel_job(runpod_job_id, machine_id)
    rp_status = "FAILED"
    data["error"] = init_error
```

This fires after 60 seconds with no frame progress from `IN_PROGRESS`, independently of the 90-minute stale timer (which becomes irrelevant for the init case).

Config env var: `RUNPOD_INIT_STALL_SEC` (default: `60`)

#### Change 3: Fix the queue-retry bug — skip same-endpoint retry for infrastructure failures

The current code at [line 548-602](server/services/runpod_dispatch.py#L548-L602) retries all failures on the same endpoint first. This is wrong for queue stalls and throttles.

**New logic:**
```python
# Classify the failure type
is_infrastructure_failure = any(phrase in error.lower() for phrase in [
    "stuck in queue",
    "stuck on initialization",
    "throttled",
    "throttl",
])

# Only retry on same endpoint for worker-level failures (OOM, crash, render error)
if attempt < max_retries and not is_infrastructure_failure and blend_url and remaining_start <= remaining_end:
    # same-endpoint retry (unchanged from current code)
    ...
else:
    # Go straight to cross-machine failover
    _handle_failover(...)
```

This means:
- Queue timeout → immediate failover to a different endpoint/farm
- Init stall → immediate failover
- Throttle → immediate failover
- OOM / render crash / code error → retry on same endpoint first (correct behavior)

#### Change 4: Pass `failure_type` through to failure tracker
Tag each failure when routing to the FAILED path:

```python
failure_type = "queue_timeout"     # for IN_QUEUE stalls
failure_type = "init_stall"        # for IN_PROGRESS with no frames
failure_type = "stale_render"      # for 90-min stale
failure_type = "throttle"          # for throttle detection
failure_type = "oom"               # if "out of memory" in error
failure_type = "crash"             # all other FAILED/TIMED_OUT
```

Pass this to the failure tracker (Component 3) at every failure point.

#### Updated timeout tiers summary

| Phase | Condition | Timeout | Action |
|-------|-----------|---------|--------|
| Queued | `IN_QUEUE` / `QUEUED` / `THROTTLED` | **45s** | Cancel → failure tracker → straight to failover |
| Throttle | "THROTTL" in status hint | **Immediate** | Cancel → failure tracker → straight to failover |
| Initializing | `IN_PROGRESS`, `rendered_frames == 0` | **60s** | Cancel → failure tracker → straight to failover |
| Rendering stale | `IN_PROGRESS`, frames stopped advancing | **5400s (90 min)** | Cancel → failure tracker → failover |

---

## Component 3: System-Wide Failure Tracker

### File: `server/services/failure_tracker.py`

#### Database table: `failure_events`
```sql
CREATE TABLE IF NOT EXISTS failure_events (
    id          TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    provider    TEXT NOT NULL,         -- 'runpod', 'modal', 'pc'
    endpoint_id TEXT,                  -- RunPod endpoint ID or Modal app name
    job_id      TEXT NOT NULL,
    group_id    TEXT,
    failure_type TEXT NOT NULL,        -- queue_timeout, init_stall, stale_render, throttle, oom, crash, cancelled
    error_msg   TEXT,
    action_taken TEXT NOT NULL,        -- 'retried_same', 'failed_over', 'abandoned', 'cancelled_by_user'
    reassigned_job_id TEXT,            -- new job ID if failed over
    reassigned_to_endpoint TEXT,       -- where the replacement was sent
    resolved     BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_failure_events_occurred_at ON failure_events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_failure_events_provider ON failure_events(provider);
CREATE INDEX IF NOT EXISTS idx_failure_events_job_id ON failure_events(job_id);
```

#### In-memory ring buffer for the log endpoint
Same pattern as the existing [`_log_buffer`](server/main.py#L44) already in `main.py`:

```python
_failure_buffer: deque[dict] = deque(maxlen=500)
```

Populated every time a failure event is recorded. The SSE log endpoint broadcasts these in real time.

#### Public API of the failure tracker module

```python
def record_failure(
    provider: str,
    endpoint_id: str,
    job_id: str,
    group_id: str,
    failure_type: str,      # queue_timeout, init_stall, stale_render, throttle, oom, crash, cancelled
    error_msg: str,
    action_taken: str,      # retried_same, failed_over, abandoned, cancelled_by_user
    reassigned_job_id: str | None = None,
    reassigned_to_endpoint: str | None = None,
) -> None:
    """Write to DB, push to in-memory ring buffer, and emit a structured log line."""
    ...

def get_recent_failures(
    limit: int = 100,
    provider: str | None = None,
    endpoint_id: str | None = None,
) -> list[dict]:
    """Query failure_events table with optional filters."""
    ...

def endpoint_failure_rate(endpoint_id: str, window_minutes: int = 5) -> float:
    """Return failure rate (0.0–1.0) for an endpoint over the last N minutes.
    Used by circuit breaker logic."""
    ...
```

#### How every failure path gets instrumented

Every place in `runpod_dispatch.py` that currently does:
```python
log.warning(f"Job {job_id}: {some_error} -> cancelling and routing to failover logic")
```

Gets replaced / supplemented with:
```python
failure_tracker.record_failure(
    provider="runpod",
    endpoint_id=endpoint_id,
    job_id=job_id,
    group_id=group_id,
    failure_type="queue_timeout",  # or init_stall, throttle, etc.
    error_msg=queue_error,
    action_taken="failed_over",
    reassigned_job_id=new_job_id,
    reassigned_to_endpoint=failover_endpoint_id,
)
```

The log line emitted by `record_failure` is structured and distinct:
```
[FAILURE] provider=runpod endpoint=abc123 job=def456 type=queue_timeout
          action=failed_over → new_job=ghi789 on endpoint=xyz999
          error: "Worker stuck in queue for 47s (status=IN_QUEUE)"
```

#### API endpoint: `GET /failure-events`

Added to `main.py`:

```python
@app.get("/failure-events")
def get_failure_events(
    limit: int = 100,
    provider: str | None = None,
    endpoint_id: str | None = None,
    failure_type: str | None = None,
) -> list[dict]:
    """Return recent failure events. No auth required (admin utility)."""
    return failure_tracker.get_recent_failures(limit, provider, endpoint_id)
```

Returns JSON like:
```json
[
  {
    "id": "...",
    "occurred_at": "2026-04-09T12:34:56Z",
    "provider": "runpod",
    "endpoint_id": "abc123",
    "job_id": "def456...",
    "group_id": "ghi789...",
    "failure_type": "init_stall",
    "error_msg": "Worker IN_PROGRESS for 60s but rendered_frames still 0",
    "action_taken": "failed_over",
    "reassigned_job_id": "jkl012...",
    "reassigned_to_endpoint": "xyz999"
  },
  ...
]
```

#### SSE stream: `/logs` (already exists, extend it)
The existing log SSE endpoint in `main.py` already broadcasts Python log records. Since `record_failure` emits a structured log line, failures will automatically appear there. No extra work needed for real-time streaming.

---

## Component 4: Worker Heartbeat (RunPod Worker)

### File: `runpod_worker/handler.py`

This requires redeploying the RunPod worker Docker image. It provides the most precise detection of init stalls — Components 1-3 give you good coverage without it, but the heartbeat makes detection exact.

#### New backend endpoint: `PUT /jobs/{job_id}/heartbeat`

Added to `main.py`:
```python
class HeartbeatPayload(BaseModel):
    phase: str      # "downloading", "extracting", "starting_blender", "rendering", "uploading"
    detail: str = ""

@app.put("/jobs/{job_id}/heartbeat")
def job_heartbeat(job_id: str, payload: HeartbeatPayload):
    execute(
        "UPDATE jobs SET last_heartbeat_at = %s, heartbeat_phase = %s WHERE id = %s",
        (now_iso(), payload.phase, job_id)
    )
    return {"ok": True}
```

Two new columns on `jobs` table:
```sql
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS last_heartbeat_at TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS heartbeat_phase TEXT;
```

#### Worker-side heartbeat thread

In `handler.py`, add alongside the existing `IncrementalOutputUploader`:

```python
HEARTBEAT_INTERVAL = 10  # seconds

class WorkerHeartbeat:
    def __init__(self, backend_url: str, job_id: str):
        self.backend_url = backend_url
        self.job_id = job_id
        self._phase = "downloading"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def set_phase(self, phase: str):
        self._phase = phase
        # Push immediately on phase change, don't wait for next tick
        self._push()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"heartbeat-{self.job_id[:8]}")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _push(self):
        try:
            requests.put(
                f"{self.backend_url}/jobs/{self.job_id}/heartbeat",
                json={"phase": self._phase},
                timeout=10,
            )
        except Exception as e:
            log.warning(f"Heartbeat push failed: {e}")

    def _run(self):
        while not self._stop.is_set():
            self._push()
            self._stop.wait(HEARTBEAT_INTERVAL)
```

Used in `handler()`:
```python
heartbeat = WorkerHeartbeat(backend_url, job_id)
heartbeat.start()

heartbeat.set_phase("downloading")
# ... download blend file ...

heartbeat.set_phase("extracting")
# ... extract zip ...

heartbeat.set_phase("starting_blender")
proc = subprocess.Popen(...)

heartbeat.set_phase("rendering")
# ... progress loop ...

heartbeat.set_phase("uploading")
# ... upload outputs ...

heartbeat.stop()
```

#### Server-side: use heartbeat for better init stall detection

With the heartbeat, the polling thread can use `last_heartbeat_at` instead of `first_in_progress_at` for init stall detection:

```python
# If heartbeat has stopped for > INIT_STALL_SEC while still at 0 frames:
last_hb = job.get("last_heartbeat_at")
if last_hb and cur_frames == 0:
    hb_age = (datetime.now(timezone.utc) - parse_iso(last_hb)).total_seconds()
    if hb_age > INIT_STALL_SEC:
        # Worker died mid-initialization
        ...
```

This distinguishes "legitimate slow blend download" (heartbeat still coming, phase="downloading") from "container died" (heartbeat stopped).

---

## Execution Plan

### Phase 1 — Autoscaler (stops money leak first)

1. Create `server/services/runpod_autoscaler.py`:
   - `scale_up(endpoint_id)` → GraphQL mutation with `workersMin=2, workersMax=4, flashBootType="FLASHBOOT"`
   - `scale_down(endpoint_id)` → GraphQL mutation with `workersMin=0, workersMax=0`
   - `maybe_scale_down(endpoint_id, db_query_one)` → checks active job count, calls scale_down only if zero
   - `_active_jobs_per_endpoint` in-memory set, updated on dispatch and job completion
   - Safety sweep thread: every 60 seconds, for each endpoint, if `_endpoint_scaled_up[ep] == True` and DB shows 0 active jobs → force scale down + log it

2. Hook scale-up into `main.py` dispatch loop at [`line 2477`](server/main.py#L2477):
   ```python
   # Before dispatching RunPod jobs:
   affected_endpoints = {_endpoint_id_for_machine(t["machine_id"]) for t in runpod_tasks}
   for ep_id in affected_endpoints:
       autoscaler.scale_up(ep_id)
   ```

3. Hook scale-down into `runpod_dispatch.py` polling thread — at every `break` in the COMPLETED / FAILED / CANCELLED path:
   ```python
   autoscaler.notify_job_done(job_id, endpoint_id)
   # Inside notify_job_done: remove from _active_jobs_per_endpoint, then maybe_scale_down
   ```

4. Hook scale-down into `cancel_render_group` in `main.py`:
   ```python
   # After all jobs marked cancelled:
   for ep_id in affected_runpod_endpoints:
       autoscaler.scale_down(ep_id)
   ```

5. Add scale-down-all on server startup:
   ```python
   # In startup code, after register_virtual_machines:
   for ep in runpod_dispatch.ENDPOINTS:
       autoscaler.scale_down(ep["id"])
   ```

### Phase 2 — Timeout Fixes + Queue-Retry Bug

1. In `runpod_dispatch.py`:
   - Lower `IN_QUEUE_TIMEOUT_SEC` default from 120 to 45
   - Add `RUNPOD_INIT_STALL_SEC` env var (default 60)
   - Add `first_in_progress_at` tracking in `_poll()`
   - Add init stall check: `IN_PROGRESS` + `rendered_frames == 0` + `elapsed > INIT_STALL_SEC` → kill + failover
   - Fix queue-retry bug: add `is_infrastructure_failure` classification, skip same-endpoint retry for those

2. Test matrix to verify (manually or with mocked RunPod responses):
   - Job stuck IN_QUEUE 46s → killed, failover fires
   - Job IN_PROGRESS, rendered_frames stuck at 0 for 61s → killed, failover fires
   - Job OOM (render crash) → retries on same endpoint first (correct)
   - Job throttled → immediate kill, no same-endpoint retry

### Phase 3 — Failure Tracker

1. Add `failure_events` table to `db.py` schema
2. Create `server/services/failure_tracker.py` with `record_failure`, `get_recent_failures`, `endpoint_failure_rate`
3. Instrument every failure path in `runpod_dispatch.py` with `failure_tracker.record_failure()`
4. Instrument `cancel_render_group` and `cancel_all_render_groups` in `main.py`
5. Add `GET /failure-events` endpoint to `main.py`

### Phase 4 — Worker Heartbeat (requires image redeploy)

1. Add `PUT /jobs/{job_id}/heartbeat` to `main.py`
2. Add `last_heartbeat_at`, `heartbeat_phase` columns to `jobs` table in `db.py`
3. Add `WorkerHeartbeat` class to `runpod_worker/handler.py`
4. Integrate heartbeat into `handler()` with phase transitions
5. Update polling thread in `runpod_dispatch.py` to use `last_heartbeat_at` for init stall detection if available (falls back to timer-based detection if heartbeat column is null, for backward compat)
6. Rebuild and push RunPod worker image

---

## End-to-End Flow After All Changes

```
User submits render (3 workers → RunPod endpoint X)
  │
  ├─ Autoscaler: scale UP endpoint X → min=2, max=4, FlashBoot=on
  ├─ 3 polling threads start, 3 job IDs added to _active_jobs_per_endpoint["X"]
  │
  ├─ Worker 1: IN_QUEUE → 1s (FlashBoot) → IN_PROGRESS → heartbeat: "downloading"
  │            → heartbeat: "rendering" → frames flowing → COMPLETED
  │            → Failure tracker: no event (success)
  │            → Autoscaler: notify_job_done(job1, X) → 2 jobs still active → stay scaled up
  │
  ├─ Worker 2: IN_QUEUE → IN_PROGRESS → heartbeat stops (container died)
  │            → Polling thread: INIT_STALL_SEC (60s) fires, rendered_frames==0
  │            → Cancel RunPod job 2
  │            → Failure tracker: record failure_type=init_stall, action=failed_over
  │            → Log: "[FAILURE] provider=runpod endpoint=X job=2 type=init_stall → new_job=2b on endpoint=Y"
  │            → Failover: dispatch new job 2b to endpoint Y (or Modal or PC)
  │            → Autoscaler: scale UP endpoint Y
  │            → Autoscaler: notify_job_done(job2, X) → 1 job still active on X → stay scaled up
  │
  ├─ Worker 3: IN_QUEUE for 47s (no worker available)
  │            → IN_QUEUE_TIMEOUT_SEC (45s) fires
  │            → is_infrastructure_failure = True → SKIP same-endpoint retry
  │            → Cancel RunPod job 3
  │            → Failure tracker: record failure_type=queue_timeout, action=failed_over
  │            → Log: "[FAILURE] provider=runpod endpoint=X job=3 type=queue_timeout → new_job=3b on endpoint=Y"
  │            → Failover: dispatch new job 3b to endpoint Y
  │            → Autoscaler: notify_job_done(job3, X) → 0 jobs active on X → scale DOWN X → min=0, max=0
  │
  ├─ Job 2b and 3b complete on endpoint Y
  │  → Autoscaler: notify_job_done for each → 0 active on Y → scale DOWN Y → min=0, max=0
  │
  └─ Safety sweep (60s timer): confirms all endpoints at 0 active jobs → no action needed

OR: User hits cancel-all
  │
  ├─ cancel_all_render_groups: cancels all RunPod jobs via API, marks DB cancelled
  ├─ Failure tracker: records cancelled events for all jobs
  ├─ Autoscaler: scale_down() called explicitly for every affected endpoint
  ├─ Polling threads: check DB on next iteration, see cancelled, exit
  └─ Safety sweep (worst case 60s later): confirms scale-down happened
```

---

## Config Reference (all env vars)

| Variable | Default | Description |
|----------|---------|-------------|
| `RUNPOD_API_KEY` | — | Required |
| `RUNPOD_ENDPOINTS` | — | `id1:Label1,id2:Label2` |
| `JOB_STATUS_POLL_INTERVAL_SEC` | `5` | How often polling thread checks RunPod |
| `IN_QUEUE_TIMEOUT_SEC` | `45` ← **was 120** | Kill if stuck queuing |
| `RUNPOD_INIT_STALL_SEC` | `60` ← **new** | Kill if IN_PROGRESS with no frames |
| `IN_PROGRESS_STALE_SEC` | `5400` | Kill if frames stop advancing (90 min) |
| `RUNPOD_KILL_THROTTLED_IMMEDIATELY` | `true` | Kill throttled jobs immediately |
| `RUNPOD_AUTOSCALE_MIN_WORKERS` | `2` ← **new** | Workers to scale up to on job receive |
| `RUNPOD_AUTOSCALE_MAX_WORKERS` | `4` ← **new** | Max workers during active jobs |
| `RUNPOD_AUTOSCALE_SAFETY_SWEEP_SEC` | `60` ← **new** | Safety sweep interval |

---

## Files Changed / Created

| File | Change Type | What |
|------|-------------|------|
| `server/services/runpod_autoscaler.py` | **NEW** | Scale up/down logic, safety sweep, in-memory state |
| `server/services/failure_tracker.py` | **NEW** | Record/query failure events, ring buffer |
| `server/services/runpod_dispatch.py` | **MODIFIED** | Lower timeouts, add init stall tier, fix queue-retry bug, instrument with failure tracker, notify autoscaler |
| `server/main.py` | **MODIFIED** | Hook scale-up into dispatch, scale-down into cancel, add /failure-events endpoint, add /jobs/{id}/heartbeat endpoint, startup scale-down |
| `server/infrastructure/db.py` | **MODIFIED** | Add `failure_events` table, add `last_heartbeat_at` + `heartbeat_phase` cols to `jobs` |
| `runpod_worker/handler.py` | **MODIFIED** | Add `WorkerHeartbeat` class, phase transitions throughout handler |
