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

### A3 · Download-phase timeout (file-size proportional)

> Worker stuck on download / scene-prep is invisible to the system because `is_stale()` returns False until the first frame uploads. Affects all fleets — the Vast container that hung for 35 min was this exact case.

**Approach.** Not a hard global timeout. Compute the timeout from `r2_input_size_bytes` on the render group, so a 5 GB blend gets ~10 min, a 50 MB blend gets the floor (~2 min), a 50 GB blend caps at the ceiling. The check only applies pre-first-frame — once frames are uploading the existing `is_stale()` logic takes over.

**Formula:**

```
DOWNLOAD_SECS_PER_GB    = 120     # assumes ~8 MB/s worst-case bandwidth
MIN_DOWNLOAD_TIMEOUT_S  = 120     # 2-min floor for tiny files
MAX_DOWNLOAD_TIMEOUT_S  = 1800    # 30-min ceiling for huge files

timeout = clamp(file_size_gb * 120, MIN, MAX)
```

Examples: 5 GB → 600 s (10 min); 100 MB → 120 s; 50 GB → 1800 s (30 min cap).

| Change | File |
|---|---|
| `LivenessCheck` accepts `download_timeout_sec` (computed per-job at construction). Pre-first-frame `is_stale()` returns True when `time - _grace_anchor > download_timeout_sec`. | `serverV2/fleets/shared/liveness_check.py` |
| Vast/Modal callback handlers read `r2_input_size_bytes` from the group at monitor-start, compute the timeout, pass it into `LivenessCheck`. | `serverV2/fleets/{vast,modal}/callback/*_callback_handler.py` |
| Constants for the formula. Override-able via env if needed (`DOWNLOAD_SECS_PER_GB`, `MIN_DOWNLOAD_TIMEOUT_SEC`, `MAX_DOWNLOAD_TIMEOUT_SEC`). | `serverV2/config.py` |
| Optional: same formula on the worker side as a `requests.get(... timeout=N)` so the container fails loudly on its own. | `cloud_worker/scripts/handler.py` |

**Note.** Community coverage requires extending `CommunityMonitor` to track first-frame time per job using the same formula. Deferred — Vast/Modal is the bigger value.

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

## Execution order (approved)

1. ✅ **A1** — engine-aware images. Unblocks EEVEE rendering. *Done.*
2. ✅ **B1** — community instance panel. UI visibility. *Done (re-prioritized ahead of A2).*
3. **A2** — machine locking. Prevents double-assignment.
4. **B3** — community success path. Closes the symmetric gap to A's failure work.
5. **C1** — `CallbackRouter` routing. Bundle with B3 since both touch agent-callback endpoints.
6. **B2** — per-card cancel. Biggest single change, has DB migration.
7. **A4** — smarter ledger check. Small.
8. **A3** — download-phase timeout. Independent; slots in last.

## Verification

| Item | Test |
|---|---|
| **A1** | Submit EEVEE render → community pulls `pcrent-community-worker-eevee` → frames produced. |
| **A2** | Two simultaneous renders with one community PC online → second routes to Vast/Modal. SQL: `machines.status='processing'` while a job is in flight. |
| **A3** | Stub a Vast container that hangs in download for >10 min → monitor fires failure within `IN_PROGRESS_FIRST_FRAME_SEC + interval`. |
| **A4** | Click Retry on an existing stuck chunk → succeeds (currently fails with `active_sibling_exists`). |
| **B1** | Render with a community chunk in flight → Community Instances panel appears with an active card. |
| **B2** | Click a Vast card's Cancel button → instance destroyed within seconds, job `cancelled`, no retry fires. Repeat for Modal and community. |
| **B3** | Submit a community-only render → group transitions to `done`. |
| **C1** | Same as B3 — endpoints go through `CallbackRouter`, double-fire from monitor + worker is filtered by terminal-guard. |

## Resolved decisions

- **B2 schema** — DB column `jobs.cancelled_at` (survives restart, single source of truth).
- **A1 image naming** — rename `pcrent-community-worker` → `pcrent-community-worker-cycles`; new `pcrent-community-worker-eevee`.
- **A3 timeout style** — file-size proportional, NOT a hard 10-min check. Formula above.
- **A3 community first-frame coverage** — deferred to a later round.
