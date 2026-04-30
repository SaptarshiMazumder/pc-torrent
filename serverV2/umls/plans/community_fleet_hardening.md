# Community Fleet — Production Hardening + UI Parity

> Active roadmap. Tracks the gaps between "community fleet works in v2" (where we are) and "community is on parity with Vast/Modal" (target state).

## Where we are

The orchestration layer is wired correctly (commit `366abbe`). Community failures route through the orchestrator's retry / drain / reconcile flow same as Vast/Modal monitors. Manual retry button works for stuck chunks.

The remaining work falls into three buckets:

| Bucket | Goal |
|---|---|
| **A · Correctness** | Renders work end-to-end; no double-assignment; nothing hangs forever |
| **B · UI parity** | Community is visible in the render-detail page; per-instance control |
| **C · Architecture** | Routing symmetry between fleets |

## Findings driving the design

- **`set_processing()` is dead code.** Defined at `serverV2/repositories/machine_repository.py:67-68` but never called. Allocator can double-assign the same community PC because `get_available_community()` keeps returning a machine that's actively rendering.
- **Per-job cancel primitives already exist.** Vast (`instances.destroy`) and Modal (`FunctionCall.cancel()`) already have working `strategy.cancel(provider_job_id)`. `stop_monitoring(job_id)` halts per-job monitor threads via `threading.Event`. Community uses the agent-poll path. Plumbing exists; we just need to expose a single-job cancel entrypoint.
- **No "do not retry this chunk" state exists.** `_try_dispatch_retry` only checks group cancellation. Per-instance cancel needs a way to suppress auto-retry for a specific chunk.
- **Frontend `InstancePanel` is provider-strategy driven.** Adding community = one more panel file with the same shape.
- **`LivenessCheck.is_stale()` short-circuits before first frame** — stuck downloads / scene-prep have no detector across all fleets.
- **Manual-retry's "active sibling" check** reads only the in-progress ledger. Pre-fix stuck chunks have ledger entries pointing at terminal jobs and refuse with `active_sibling_exists`.
- **Community success path** doesn't fire `handle_chunk_succeeded`. Pure-community render groups don't transition to `done`.

---

## A. Correctness

### A1 · Engine-aware Docker images for community ❌ ABANDONED

> Premise was: split the community image into Cycles / EEVEE variants so EEVEE renders get the libEGL/Vulkan stack they need.  After building both an EGL variant and a Vulkan variant, we proved empirically that **Docker Desktop + WSL2 + NVIDIA Container Toolkit ships only compute libraries to containers** — no EGL, no GLX, no Vulkan ICD, regardless of `NVIDIA_DRIVER_CAPABILITIES`.  Diagnostic from `nvidia/cuda:12.2.0` with `--gpus all` and `NVIDIA_DRIVER_CAPABILITIES=all`: `find / -iname '*vulkan*'` and `find / -iname 'libGLX*'` both returned **zero matches**.

**Decision.** Community = Cycles only.  EEVEE for community is gated off in `EngineCompatibilityValidator` alongside Modal.  The Cycles-only image (`pcrent-community-worker-cycles`) is the single image the agent pulls.  Native-Windows Blender (out of Docker, with a separate security stack) is a future option but out of scope here.

**Cleanup performed when this was abandoned:** `community_worker/Dockerfile.eevee` deleted, `community_worker/render.eevee.sh` deleted, `push-worker.sh` `community-eevee` variant removed, `agent.py` `image_for_engine` / `engine_for_job` helpers removed in favor of a single `COMMUNITY_IMAGE` constant.

### A2 · Lock community machine on claim

> `set_processing()` is defined but never called. The job claim flips the *job* to running but never touches `machines.status`. The allocator double-assigns.

| Change | File |
|---|---|
| Job-claim SQL also runs `UPDATE machines SET status='processing' WHERE id = %s` (single transaction with the job claim) | `serverV2/repositories/job_repository.py` |
| Stale-demotion query covers `processing` too (handles hard agent crash) | `serverV2/repositories/machine_repository.py` |
| `handle_chunk_succeeded` / `handle_chunk_failed` / cancel paths flip the machine back to `available` after the job ends | `serverV2/orchestrator/lifecycle.py` |

**Outcome.** Two simultaneous renders with one community PC online → the second routes to Vast/Modal instead of stacking on the same PC.

### A3 · Stuck-worker detection (process-activity-based, phase-aware)

> Worker stuck pre-first-frame is invisible because `is_stale()` returns False until frames upload. Heartbeats prove the python loop is alive but DON'T prove the work is progressing — a 50-min scene load on a heavy blend looks identical to a Python deadlock from the heartbeat alone. The earlier file-size timeout was a heuristic that false-positives on slow R2. The right architecture is to base the decision on **whether the worker process is actively doing work**, with phase context.

**Approach.** Worker self-samples its own process activity (CPU%, RSS, plus a phase-specific progress counter) and includes those in every heartbeat. The server's `LivenessCheck` examines a sliding window of recent heartbeats and decides "stuck" from real signals, not from elapsed-time heuristics.

The worker progresses through known phases: `download` → `extract` (zip only) → `loading` (Blender opening the file) → `rendering` (frames). Each phase has its own progress signal:

| Phase | Healthy signal | Stuck signal |
|---|---|---|
| `download` | `bytes_progressed` strictly increasing | counter flat for `DOWNLOAD_BYTES_STALL_SEC` |
| `extract` | `cpu_percent` non-trivial | CPU near zero for `CPU_STALL_SEC` |
| `loading` | `cpu_percent` non-trivial OR `rss_bytes` growing | CPU near zero AND RSS unchanged for `CPU_STALL_SEC` |
| `rendering` | existing `rendered_frames` increases (current logic) | existing `is_stale()` check |

**Three detection rules.** Run by `LivenessCheck` against the last N heartbeats (sliding window):

**Rule 1 — CPU-stall (universal, all pre-rendering phases):**
```
window      = last STALL_WINDOW_SEC of heartbeats (e.g., 120s)
avg_cpu     = mean(cpu_percent over window)
rss_changed = max(rss_bytes in window) - min(rss_bytes in window) > RSS_NOISE_BYTES
if avg_cpu < CPU_STALL_THRESHOLD_PCT and not rss_changed:
    → stuck
```
Catches Python deadlocks, kernel syscall hangs, container OS-level freezes. A heavy 50-min scene load passes this trivially because Blender is at 60-100% CPU during BVH build / shader compile / mesh load.

**Rule 2 — Download bytes-stall (only when `phase=download`):**
```
if phase == "download" and bytes_progressed unchanged for DOWNLOAD_BYTES_STALL_SEC (e.g., 60s):
    → stuck
```
Catches TCP-level network blocks where the connection is up but no bytes flow. Range-resume in the worker handles connection drops; this rule catches the slower "connection alive but pipe dead" case.

**Rule 3 — Download outer ceiling (file-size-proportional, only when `phase=download`):**
```
DOWNLOAD_SECS_PER_GB        = 120     # assumes ~8 MB/s worst-case
MIN_DOWNLOAD_PHASE_TIMEOUT  = 120     # 2-min floor
MAX_DOWNLOAD_PHASE_TIMEOUT  = 1800    # 30-min ceiling
download_max_sec = clamp(file_size_gb * 120, MIN, MAX)

if phase == "download" for longer than download_max_sec:
    → stuck
```
Catches the case where bytes ARE flowing but at a rate too slow to ever complete (drip-feed at 100 KB/s on a 5 GB file would take 14 hours and never trigger Rule 2). This is the original A3 logic, retained but now scoped specifically to the download phase — we know we're in download because the worker tells us so. For other phases (loading, rendering), there's no file-size-derived ceiling because their duration isn't bounded by file size.

Outside the rules: a hard outer per-chunk ceiling (`HARD_MAX_CHUNK_SEC`, e.g., 6 hours) catches anything not covered by the three rules. Last-resort sanity, not a primary detector.

**Worker-side changes.**

| Change | File |
|---|---|
| New thread / module that samples `psutil.Process().cpu_percent(interval=...)` and `memory_info().rss` once per heartbeat, tracks `phase` explicitly, exposes a `bytes_progressed` counter that download / extract code increments. | `cloud_worker/scripts/heartbeat.py` (new helper) and `cloud_worker/scripts/handler.py` (sets phase + increments counter) |
| Heartbeat HTTP payload extended to include `phase`, `cpu_percent`, `rss_bytes`, `bytes_progressed`. Backwards-compatible — server treats missing fields as no-signal (skip rule). | `cloud_worker/scripts/heartbeat.py`, agent equivalent in `agent/agent.py` |
| Add `psutil` to worker images (cloud_worker/Dockerfile.* and community_worker/Dockerfile.cycles). It's already a transitive dep of some Blender tooling but explicit pin is safer. | `cloud_worker/Dockerfile.{cycles,eevee}`, `community_worker/Dockerfile.cycles` |
| **Range-resume on download.** Wrap the `iter_content` loop in a try/except that catches `ChunkedEncodingError`/`ConnectionError`, retries the GET with `Range: bytes={downloaded}-`, appends to the existing file, max `DOWNLOAD_RETRY_ATTEMPTS` (e.g., 5). Same pattern in agent.py. | `cloud_worker/scripts/handler.py`, `agent/agent.py` |

**Server-side changes.**

| Change | File |
|---|---|
| Heartbeat schema in Pydantic accepts the new optional fields. | `serverV2/api/schemas/job.py` `JobHeartbeatPayload` |
| Heartbeat repository stores last N heartbeats per job (Redis list with TTL) instead of just a single key. Used by the sliding-window stall detector. Cap the list length so memory bounded. | `serverV2/repositories/heartbeat_repository.py` |
| `LivenessCheck` extends with `is_stalled()` that runs Rules 1, 2, 3 against the heartbeat window. Existing `is_stale()` (frame-count-based) stays for the `rendering` phase. | `serverV2/fleets/shared/liveness_check.py` |
| Vast/Modal callback handlers and `CommunityMonitor` invoke `is_stalled()` once per poll tick. On stall → `on_failure(job_id, "stalled in phase=X")`. | `serverV2/fleets/{vast,modal}/callback/*_callback_handler.py`, `serverV2/fleets/community/community_monitor.py` |
| Tunable constants exposed via env (`CPU_STALL_THRESHOLD_PCT`, `STALL_WINDOW_SEC`, `DOWNLOAD_BYTES_STALL_SEC`, `DOWNLOAD_SECS_PER_GB`, `MIN_DOWNLOAD_PHASE_TIMEOUT`, `MAX_DOWNLOAD_PHASE_TIMEOUT`, `HARD_MAX_CHUNK_SEC`). | `serverV2/config.py` |

**Why this beats the old A3 timeout.**

| Failure mode | Old A3 (file-size timeout) | New A3 (process-activity) |
|---|---|---|
| Download connection drops mid-stream | Range-resume catches in worker; old A3 redundant timeout | Range-resume catches in worker; Rule 2 covers if drops repeat |
| Download stuck in TCP backoff (no bytes flowing) | timeout fires only after MAX_DOWNLOAD time | Rule 2 fires in 60s |
| Download dripping slowly (1 MB/s on 5 GB file) | timeout fires at 10 min (correct catch) | Rule 3 fires at 10 min (same catch — Rule 3 IS the old A3 logic, scoped to the right phase) |
| Heavy 50-min scene load | false positive — old A3 fires even though Blender is healthy | passes — high CPU, growing RSS, no rule triggers |
| Blender python deadlock during scene-prep | old A3 catches via outer timeout (eventually) | Rule 1 fires within 2 min |
| Container kernel-level wedge | not caught by old A3 unless before first frame | Rule 1 fires within 2 min |
| R2 genuinely slow but blend completes | false positive — old A3 fires | passes — bytes still flowing → no rule triggers |

**Phase ordering and rule applicability.**

```
download  → Rule 1 (CPU stall)  + Rule 2 (bytes stall)  + Rule 3 (file-size ceiling)
extract   → Rule 1
loading   → Rule 1
rendering → existing is_stale() (frame-count-based) — A3 rules don't run here
```

Once `phase=rendering` arrives the existing logic handles staleness. A3 only governs pre-first-frame.

**Community coverage.** The `CommunityMonitor` (single scanning daemon, not per-job threads like Vast/Modal) needs the same `is_stalled()` invocation against community heartbeats. Same code, just plumbed through the scanning loop. Not deferred — community is a fleet that contributes most of the chunk volume; missing this catches bugs cheaper.

**Verification.**
- Submit a render with a 10 GB blend on a Vast host with throttled network; observe Rule 3 fire at the file-size-derived ceiling.
- Run a render against a host with a stub `time.sleep(99999)` injected pre-render; Rule 1 fires within `STALL_WINDOW_SEC + heartbeat interval`.
- Disconnect the worker's network mid-download; Rule 2 fires within `DOWNLOAD_BYTES_STALL_SEC`.
- Run a render with a heavy scene that legitimately takes 30+ min in `phase=loading`; observe NO rule fires (CPU stays non-trivial throughout).
- Run a normal render end-to-end; no spurious failures, completes through `rendering`.

### A4 · Smarter ledger check on manual retry

> Pre-fix stuck chunks have ledger entries pointing at terminal jobs. The Retry button refuses with `active_sibling_exists` because the check is too strict.

| Change | File |
|---|---|
| `retry_chunk_manually` — if the ledger entry points at a terminal job, treat as stale and release. Continue to retry dispatch. | `serverV2/orchestrator/lifecycle.py` |

```python
ledger_job = self._in_progress.current_job_for(group_id, chunk_index)
if ledger_job is not None:
    ledger_row = self._job_repo.get_raw_by_id(ledger_job)
    if ledger_row and (ledger_row.get("status") or "") in ("pending", "running"):
        raise ManualRetryError("active_sibling_exists")
    self._in_progress.release(group_id, chunk_index)
```

**Outcome.** Existing stuck chunks become unblockable via the Retry button. Side benefit — every manual retry self-cleans one stale row.

---

## B. UI parity + per-instance control

### B1 · Community instances panel in render-detail UI ✅ DONE

> Community jobs aren't visible in the GPU Instances section today — only Vast and Modal. The user can't see what their PC is actually doing for a render group.

| Change | File |
|---|---|
| New panel mirroring `VastInstancePanel.jsx` / `ModalInstancePanel.jsx`. Provider strategy filters `machine_type === 'community'`, no live serverless polling, builds card data from the task row. | `desktop/src/components/jobs/CommunityInstancePanel.jsx` (new) |
| Mount in `RenderGroupDetail` between Vast and Modal panels | `desktop/src/components/jobs/JobDetailView.jsx` |

**Backend.** No changes — the existing serializer is fleet-agnostic. Tasks already expose `machine_gpu`, frame range, rendered/total, error.

### B2 · Per-instance cancel button

> Today only group-level cancel exists. We want a Cancel button on each `ActiveCard` that kills exactly that container/job and prevents auto-retry of its chunk.

**Backend (lifecycle change).**

```
cancel_one_job(job_id):
    1. Read job → look up provider_job_id, machine_type, chunk_index, group_id
    2. strategy.cancel(provider_job_id)   # Vast destroys / Modal cancels / Community: agent's poll picks up DB cancel
    3. strategy.stop_monitoring(job_id)
    4. Mark job 'cancelled' in DB
    5. Mark chunk_index as do-not-retry (new column on jobs table)
    6. Release in-progress ledger
    7. reconcile_group_status
```

| Change | File |
|---|---|
| New `cancel_one_job(job_id)` lifecycle method | `serverV2/orchestrator/lifecycle.py` |
| `_try_dispatch_retry` skips chunks with the do-not-retry flag set | `serverV2/orchestrator/lifecycle.py` |
| Facade method | `serverV2/orchestrator/orchestrator.py` |
| New route `POST /jobs/{job_id}/cancel` | `serverV2/api/routers/jobs.py` |
| New column `jobs.cancelled_at TIMESTAMPTZ NULL` (DB migration) | `serverV2/repositories/job_repository.py` |
| `ActiveCard` gains a Cancel button. Provider strategies pass `task.job_id` through `extractCardData`. | `desktop/src/components/jobs/InstancePanel.jsx` |
| `cancelJob(baseUrl, jobId)` API helper | `desktop/src/services/api.js` |

**Schema decision.** `jobs.cancelled_at` column over an in-memory set — survives restart, single source of truth.

**Why one mechanism for all fleets.** The lifecycle method dispatches to `strategy.cancel`. Each fleet's strategy already knows how to cancel (Vast destroy, Modal cancel, community via DB-status poll). The frontend gets one button on every active card regardless of fleet.

### B3 · Community success path

> Pure-community render groups don't transition to `done`. The `JobService.update_status` worker callback is just a DB write — no equivalent of the Vast/Modal monitor's `_on_success → orchestrator → handle_chunk_succeeded` path that owns telemetry, drain, group rollup.

**Approach.** Mirror the agent-failure pattern with a sibling success endpoint.

| Change | File |
|---|---|
| New endpoint `POST /jobs/{job_id}/agent-success` calls `orchestrator.on_job_succeeded` | `serverV2/api/routers/jobs.py` |
| `notify_orchestrator_success(job_id)` helper paired with the `update_job_status(... "done" ...)` call | `agent/agent.py` |
| Same wiring on the sidecar's image-not-loaded fallback path | `agent/sidecar_main.py` |
| `handle_chunk_succeeded` idempotency guard — `if (raw.get("status") or "") == "done": return` at the top | `serverV2/orchestrator/lifecycle.py` |

**Idempotency.** For Vast/Modal, the monitor's path also fires success. The guard prevents double telemetry / double drain when both arrive.

---

## C. Architecture polish

### C1 · Route `agent-failure` and `agent-success` through `CallbackRouter`

> Today `agent-failure` calls `orchestrator.on_job_failed` directly, bypassing `CallbackRouter`. Vast/Modal monitors go through the router. Inconsistent.

| Change | File |
|---|---|
| Expose `callback_router` on `Container` | `serverV2/bootstrap.py` |
| Pass it to `jobs.init` | `serverV2/main.py` |
| `agent_failure` / `agent_success` endpoints call `callback_router.route(... outcome=FAILURE/SUCCESS)` | `serverV2/api/routers/jobs.py` |

**Side benefit.** Picks up the `is_job_terminal` short-circuit guard in `CallbackRouter` for free — duplicate failure/success reports get filtered without duplicated handling logic.

---

## Status snapshot

### Done

| Item | What |
|---|---|
| ❌ **A1** | Abandoned — WSL2 + NVCT ships only compute libs to containers. Community = Cycles-only; EEVEE for community gated off in `EngineCompatibilityValidator`. |
| ✅ **B1** | Community instance panel on render-detail page. |
| ✅ **A2** | Community machine locked to `'processing'` on claim, released on terminal. Stale-sweep covers `'processing'` for crashed-agent recovery. No more double-assignment. |
| ✅ **B3 (re-shaped)** | `JobService.register_outputs` detects `output_files >= total_frames` → fires `success_notifier` → `CallbackRouter.route(SUCCESS)`. Workers can't self-report `done`; the schema rejects it. |
| ✅ **C1 / C2** | Every failure entry point routes through `CallbackRouter` + `is_job_terminal` guard. Migrated: `agent-failure`, `internal/orphan`, `CommunityMonitor`. Only `FailureHandler.handle` calls `orchestrator.on_job_failed` directly now. |
| ✅ Engine boundary fix | Resolved engine written into `render_overrides_json` at `confirm_upload` / `rerender` before persistence. Lifecycle reads strict, no fallback. |
| ✅ overrides_b64 → overrides_json | Base64 only inside `vast/client.py`, `modal/client.py`, and the agent's docker-run env. JSON everywhere upstream. `dispatch_queue` column renamed. |
| ✅ Defensive-code purge | Silent `except: return {}` patterns gone from agent's `parse_job_render_overrides`, lifecycle retry, recovery paths. Boundary code (HTTP/SDK) stays defensive. |
| ✅ Dead-code removal | `agent/ssh_agent.py`, `agent/provisioner.py`, `agent/provisioner_state.json` deleted (~2400 lines, legacy SSH path). |
| ✅ Frontend retry-refresh | Clicking Retry on a failed chunk now triggers `onRefresh` through `MyJobsPage → JobDetailView → RenderGroupDetail → FailedChunksPanel`. Local cache flips `failed → running`, polling resumes. |
| ✅ UML refresh | `allocation_fleet_orchestration.puml` color-coded by source module. `c2_agent_failure_routing.puml` shows all 5 failure sources. |

### Pending — current priorities

1. **A3** — stuck-worker detection (process-activity-based, phase-aware) + range-resume on download. **Re-designed this session** — see the A3 section above. Replaces the old file-size-only timeout with a robust process-metric-based detector that doesn't false-positive on slow R2 or heavy scene loads.
2. **A4** — smarter ledger check on manual retry. Small.
3. **B2** — per-card cancel. Biggest remaining change, has DB migration. Defer until A3/A4 ship.

## Verification

| Item | Test |
|---|---|
| **A1** | n/a — abandoned. Validator gates EEVEE off community; submit an EEVEE render and confirm it allocates to Vast only (Modal also gated). |
| **A2** | Two simultaneous renders with one community PC online → second routes to Vast/Modal. SQL: `machines.status='processing'` while a job is in flight. |
| **A3** | (1) Submit a render against a host with throttled network (drip-feed below `1 / DOWNLOAD_SECS_PER_GB` rate) → Rule 3 fires at the file-size-derived ceiling. (2) Inject a `time.sleep(99999)` pre-render in the worker → Rule 1 fires within `STALL_WINDOW_SEC + heartbeat interval`. (3) Disconnect worker network mid-download → Rule 2 fires within `DOWNLOAD_BYTES_STALL_SEC`. (4) Render a heavy scene that genuinely takes 30+ min in `loading` phase → no rule fires (CPU stays non-trivial). (5) Normal render end-to-end → no spurious failures. |
| **A4** | Click Retry on an existing stuck chunk → succeeds (currently fails with `active_sibling_exists`). |
| **B1** | Render with a community chunk in flight → Community Instances panel appears with an active card. ✅ verified shipping. |
| **B2** | Click a Vast card's Cancel button → instance destroyed within seconds, job `cancelled`, no retry fires. Repeat for Modal and community. |
| **B3** | Submit a community-only render → group transitions to `done`. ✅ verified shipping (via register-outputs path). |
| **C1 (failure)** | After failure-half ships: agent-failure goes through `CallbackRouter`, double-fire from monitor + worker is filtered by terminal-guard. |

## Resolved decisions

- **A1 outcome** — abandoned. WSL2 + NVCT does not expose graphics libraries to containers regardless of `NVIDIA_DRIVER_CAPABILITIES`. Community is Cycles-only at the validator level.
- **A3 architecture** — process-activity-based (CPU + RSS + bytes-progressed) with phase-aware rules. The original file-size-only timeout retained as Rule 3 but scoped to `phase=download` only, where its assumptions hold.
- **A3 community coverage** — included in scope, not deferred. Community contributes too much chunk volume to be skipped.
- **B3 mechanism** — server-side success detection from `register_outputs` instead of a worker-call sibling endpoint. Schema (`UpdateJobStatusPayload` restricted to `running` / `failed`) confirmed correct as designed; agent's `update_job_status("done", ...)` calls removed.
- **B2 schema** — DB column `jobs.cancelled_at` (survives restart, single source of truth). Unchanged.
- **Render-overrides format** — JSON everywhere upstream of the wire boundary. Base64 only inside the env-var/Modal-payload encoders. Column names match content (`render_overrides_json` is JSON; the `render_overrides_b64` local variable is the only b64-named identifier and lives only inside the encoders).
