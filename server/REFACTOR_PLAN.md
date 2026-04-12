# Render-group + scheduling refactor plan

This plan is a handoff for a future agent. It covers two tracks that share a
final target structure:

1. **Domain extractions** — pull pure business logic out of routers/services
   into `server/domain/`. Zero behavior change per step.
2. **Render-job orchestration + failover** — replace the spaghetti dispatch
   code in `render_groups.py` and `_check_failover` with a single per-group
   `RenderJobOrchestrator`, a `PriorityPolicy`, and a background
   `FailoverScanner`.

RunPod is being **removed**. Do not carry its code forward. Only Vast, Modal,
and community (Windows desktop) remain.

---

## Final target layout

```
server/
  api/routers/
    render_groups.py              ~250 lines — HTTP only: validate -> service -> serialize
  domain/
    value_objects.py              (existing)
    status_aggregator.py          (DONE — pure group-status aggregation)
    render_group.py               RenderGroup entity + state transitions
    render_job.py                 RenderJob entity + state transitions (stale, heartbeat, retries)
    frame_distribution.py         pure frame math (moved from scheduling/frame_distributor.py)
    priority_policy.py            Vast > Modal > community ordering
    failover_policy.py            "Modal fails -> prefer Vast", "Vast fails -> another Vast", etc.
  services/render_groups/
    __init__.py
    service.py                    RenderGroupService: create, confirm, cancel, rerender
    frame_planning.py             payload -> timeline override -> blend parse -> frame range
    upload_coordinator.py         multipart + single-PUT flow
  scheduling/
    orchestrator.py               RenderJobOrchestrator: plan + execute + handle_failure
    failover_scanner.py           background task: every 30s, detects stale/orphaned jobs
    dispatch_coordinator.py       (kept for now; orchestrator wraps it)
    frame_distributor.py          (kept as thin shim that re-exports from domain.frame_distribution)
    strategies/                   (Modal, Vast, Community — RunPod deleted)
```

---

## Status at time of handoff

- **DONE**: [server/domain/status_aggregator.py](server/domain/status_aggregator.py) extracted.
  `_get_render_group_inner` in [server/api/routers/render_groups.py:481-506](server/api/routers/render_groups.py#L481-L506) now calls it. Pure function with no I/O.
- **DONE**: Modal handler watchdog rework
  ([modal_worker/handler.py](modal_worker/handler.py)) and GPU regression fix
  in [modal_worker/app.py](modal_worker/app.py). Both require
  `modal deploy modal_worker/app.py` to take effect.
- **TODO**: Everything below.

---

## Invariants to preserve across the whole refactor

These behaviors exist today and MUST still be true after the refactor. If the
new code cannot express one of these, flag it before merging — do not silently
drop it.

1. **Render group statuses**: `uploading` -> `pending` -> `running` -> `done`
   or `failed`. `cancelled` can be set by user action from any non-terminal.
   A cancelled/failed group is promoted to `done` only if every frame was
   actually rendered across failover hops (see `status_aggregator.py`).
2. **Job statuses**: `pending` -> `running` -> `done`/`failed`/`cancelled`.
   `rendered_frames` only counts non-failed jobs — otherwise failover hops
   double-count.
3. **Failover priority**:
   - `modal_serverless` fails -> prefer `vast_serverless`; fall back to any
     non-Modal serverless; fall back to community machines.
   - `vast_serverless` fails -> prefer another Vast machine; fall back to
     community.
   - Community machine fails -> prefer any available serverless (Vast first).
4. **Retry vs failover**: If `attempt < max_retries`, first retry on the same
   endpoint with `frame_start = frame_start + rendered * step`. Only after
   retries are exhausted do we create a new job on a different machine.
5. **Serverless max retries cap**: A given `(frame_start, frame_end)` range
   can have at most `SERVERLESS_MAX_RETRIES = 5` failed attempts before it
   stops getting re-dispatched.
6. **`blend_url` is provider-specific**: Modal/Vast/community each read
   `PUBLIC_BACKEND_URL` from their own services module. The orchestrator must
   format the URL using the correct provider module per task.
7. **Modal dispatches in background threads**: The current code spawns a
   daemon thread per Modal dispatch because HTTP endpoint cold-start can take
   10-30s and we do not want to block the HTTP request. Vast also runs its
   dispatches on a background thread, sequentially with a 2s gap.
8. **Cancel path**: A cancelled group must cancel every non-terminal job, and
   for serverless jobs it must call the provider's cancel API
   (`modal.FunctionCall.from_id(id).cancel(terminate_containers=True)` for
   Modal; Vast has its own). Do not leak containers.

---

## Track 1 — domain extractions

Each step in this track is a self-contained commit that does not change
behavior. Land them in order; each can be validated independently.

### 1.1 `domain/frame_distribution.py` — move pure frame math

**What to move (from [server/scheduling/frame_distributor.py](server/scheduling/frame_distributor.py)):**

- `compute_power_score` — [frame_distributor.py:32](server/scheduling/frame_distributor.py#L32)
- `filter_enabled_machines` — [frame_distributor.py:48](server/scheduling/frame_distributor.py#L48)
- `max_workers_for_frame_budget` — [frame_distributor.py:64](server/scheduling/frame_distributor.py#L64)
- `limit_machines_for_frame_budget` — [frame_distributor.py:85](server/scheduling/frame_distributor.py#L85)
- `distribute_frames` — [frame_distributor.py:118](server/scheduling/frame_distributor.py#L118)
- `distribute_frames_by_chunk_size` — [frame_distributor.py:170](server/scheduling/frame_distributor.py#L170)
- `expand_serverless_assignments` — [frame_distributor.py:242](server/scheduling/frame_distributor.py#L242)

**What to leave where it is** (these touch the DB or depend on scheduler state):

- `get_available_machines` — hits `SELECT * FROM machines`, keep in scheduling.
- `choose_retry_machine` — hits the DB, belongs in `failover_policy.py`
  (step 1.5).

**Procedure:**

1. Create `server/domain/frame_distribution.py` with the functions above,
   verbatim.
2. Replace `server/scheduling/frame_distributor.py` with a thin shim:
   ```python
   from domain.frame_distribution import (
       compute_power_score,
       filter_enabled_machines,
       ...
   )
   from scheduling._machines import get_available_machines  # new file
   ```
   This keeps existing imports working.
3. Move `get_available_machines` into `scheduling/_machines.py` (new file).
4. Delete `choose_retry_machine` (it is a duplicate of
   `_find_failover_machine`, handled in step 1.5).

**Test:** Existing callers should still resolve. Run
`python -c "from scheduling.frame_distributor import distribute_frames; print('ok')"`.

### 1.2 `domain/render_group.py` — RenderGroup entity + transitions

Define the render group as a dataclass with methods that answer questions
without touching the database.

```python
@dataclass(frozen=True)
class RenderGroup:
    group_id: str
    status: str  # "uploading" | "pending" | "running" | "done" | "failed" | "cancelled"
    total_frames: int
    frame_start: int
    frame_end: int
    frame_step: int
    input_filename: str
    r2_input_key: str | None
    submitted_at: str
    completed_at: str | None
    error: str | None
    user_id: str
    # ... render_overrides, scheduling, analysis_* deserialized

    @classmethod
    def from_row(cls, row: dict) -> "RenderGroup": ...

    def is_terminal(self) -> bool:
        return self.status in ("done", "failed", "cancelled")

    def can_dispatch(self) -> bool:
        return self.status == "pending"

    def can_cancel(self) -> bool:
        return self.status not in ("done", "cancelled")

    def can_rerender(self) -> bool:
        return self.status in ("done", "failed", "cancelled")
```

**Why**: Every place that reads `group["status"]` and manually checks
"is it in one of these states" should ask the entity instead. This is
where the group's *rules* live.

**Callers to update (search for `group["status"] in`):**
- [render_groups.py:442](server/api/routers/render_groups.py#L442) — orphan cleanup gate
- [render_groups.py:468](server/api/routers/render_groups.py#L468) — failover check gate
- [render_groups.py:494](server/api/routers/render_groups.py#L494) — aggregator branch (already moved to status_aggregator)
- Cancel handler, rerender handler — various

### 1.3 `domain/render_job.py` — RenderJob entity + stale/heartbeat logic

```python
@dataclass(frozen=True)
class RenderJob:
    job_id: str
    group_id: str
    machine_id: str
    machine_type: str
    status: str
    frame_start: int
    frame_end: int
    frame_step: int
    rendered_frames: int
    total_frames: int
    attempt: int
    max_retries: int
    submitted_at: str
    last_heartbeat_at: str | None

    @classmethod
    def from_row(cls, row: dict) -> "RenderJob": ...

    def is_terminal(self) -> bool: ...
    def is_serverless(self) -> bool: ...  # uses domain.value_objects.is_serverless
    def remaining_frames(self) -> tuple[int, int] | None:
        """Returns (new_start, frame_end) for the unrendered tail, or None if complete."""

    def can_retry_same_endpoint(self) -> bool:
        return self.attempt < self.max_retries

    def is_heartbeat_dead(self, grace_sec: int, timeout_sec: int) -> bool: ...
    def is_stuck_pending(self, serverless_cutoff_iso: str) -> bool: ...
    def is_complete_by_frames(self) -> bool:
        return self.total_frames > 0 and self.rendered_frames >= self.total_frames
```

**Sources of logic to absorb:**
- `_check_failover`'s "serverless stuck in pending" check —
  [render_groups.py:211-222](server/api/routers/render_groups.py#L211-L222)
- `_check_failover`'s "running job with all frames rendered" check —
  [render_groups.py:195-208](server/api/routers/render_groups.py#L195-L208)
- `monitor.py`'s `_is_heartbeat_dead` and `_is_stale` —
  [services/modal/monitor.py:350-374](server/services/modal/monitor.py#L350-L374)
- `dispatch_coordinator.handle_failure`'s remaining-range math —
  [dispatch_coordinator.py:77-80](server/scheduling/dispatch_coordinator.py#L77-L80)

All these today re-derive the same math (`frame_start + rendered * step`) in
different places. Pull it into `remaining_frames()` once.

### 1.4 `domain/priority_policy.py` — single source of truth for ordering

```python
PROVIDER_PRIORITY = ("vast_serverless", "modal_serverless")  # community is "available" but not preferred

def sort_machines_by_preference(
    machines: list[dict],
    *,
    prefer_non_provider: str | None = None,
) -> list[dict]:
    """Return machines in dispatch-priority order.

    prefer_non_provider: when failover is triggered from a given provider,
    push machines of that provider to the back so we try others first.
    """
```

**Replaces**:
- Implicit ordering in `render_groups.py` lines ~1001-1104 (three separate
  `if xxx_strategy.is_enabled(): for task in tasks_of_type_xxx` blocks).
- Hard-coded branch logic in
  [dispatch_coordinator._find_failover_machine](server/scheduling/dispatch_coordinator.py#L240-L310).

The orchestrator (step 2.3) will call this to decide which tasks go to which
providers first.

### 1.5 `domain/failover_policy.py` — failover rules

Pure function that takes a list of candidate machines and the failed
machine's type, returns the best failover target.

```python
def choose_failover_machine(
    candidates: list[dict],
    *,
    failed_machine_id: str,
    failed_machine_type: str,
) -> dict | None:
    ...
```

**Rules** (lifted from
[dispatch_coordinator.py:240-310](server/scheduling/dispatch_coordinator.py#L240-L310)):

- Exclude `failed_machine_id`.
- If failed was Modal: Vast > non-Modal serverless > community.
- If failed was Vast: other Vast > community.
- If failed was community: any serverless (Vast first) > other community.

**Delete** after this: `choose_retry_machine` in
`scheduling/frame_distributor.py` (duplicate of the same logic).

---

## Track 2 — orchestration + failover refactor

This is the bigger, higher-risk track. Land it only after Track 1 steps 1.1,
1.2, 1.4, 1.5 are in.

### 2.1 `services/render_groups/upload_coordinator.py` — extract multipart

Move these four routes' bodies into a module with no HTTP dependencies, so
the router just does `payload -> coordinator.xxx() -> response`:

- [init_render_group_multipart_upload](server/api/routers/render_groups.py#L644)
- [render_group_multipart_part_urls](server/api/routers/render_groups.py#L683)
- [complete_render_group_multipart_upload](server/api/routers/render_groups.py#L709)
- [abort_render_group_multipart_upload](server/api/routers/render_groups.py#L741)

Signature shape:

```python
class UploadCoordinator:
    def init_multipart(self, group_id: str, user_id: str, total_bytes: int) -> dict: ...
    def part_urls(self, group_id: str, user_id: str, part_numbers: list[int]) -> dict: ...
    def complete(self, group_id: str, user_id: str, parts: list[dict]) -> dict: ...
    def abort(self, group_id: str, user_id: str) -> None: ...
```

Raise a small `UploadError` with an HTTP-friendly code that the router maps to
`HTTPException`. Do NOT import `HTTPException` from this module.

### 2.2 `services/render_groups/frame_planning.py` — resolve frame range

Pull the "what frame range are we actually rendering" logic out of
`confirm_render_group_upload`. Inputs:

- the payload's requested `frame_start`/`frame_end`/`frame_step`
- the timeline override in `render_overrides`
- the blend-file analysis snapshot (`scene.frame_start`, `frame_end`)

Output: `(frame_start, frame_end, frame_step, total_frames)` plus the
warnings list. Pure function — no DB, no HTTP.

### 2.3 `scheduling/orchestrator.py` — RenderJobOrchestrator

This is the centerpiece. One instance per render group. It replaces:

- The three copy-pasted dispatch blocks in
  [render_groups.py:1001-1104](server/api/routers/render_groups.py#L1001-L1104).
- The copy-pasted dispatch in [rerender_group](server/api/routers/render_groups.py#L1307).
- The serverless-retry dispatch block in
  [_check_failover:324-415](server/api/routers/render_groups.py#L324-L415).

Proposed API:

```python
class RenderJobOrchestrator:
    def __init__(
        self,
        group: RenderGroup,
        strategies: Mapping[str, ProvisionStrategy],
        priority: PriorityPolicy,
        failover: FailoverPolicy,
    ): ...

    def plan(self, machines: list[dict]) -> list[PlannedTask]:
        """Pure: compute task assignments for this group's frame range."""

    def execute(self, tasks: list[PlannedTask]) -> list[DispatchResult]:
        """
        Persist tasks to DB, dispatch each to the right provider, return
        per-task status.  Modal/Vast dispatches run on background threads;
        community dispatches are immediate no-ops.
        """

    def handle_failure(self, job: RenderJob, error: str) -> RenderJob | None:
        """
        Retry on same endpoint if attempts remain, otherwise pick a failover
        target via FailoverPolicy and dispatch a new job.  Returns the new
        RenderJob (could be the retried one or a new failover job) or None
        if no failover was possible.
        """
```

`PlannedTask` and `DispatchResult` are small dataclasses — do not reuse the
loose `dict` shape that `distribute_frames` returns today.

**Key decisions to pin down**:

- The orchestrator is **stateless between calls**. Every call re-reads the
  group + jobs from the DB. This avoids the "which orchestrator instance
  owns this job" problem.
- The orchestrator is the **only** caller of `DispatchCoordinator`. Nothing
  else should import `coordinator` once this lands. (That includes
  `services/modal/monitor.py` — its `_handle_failure` should call the
  orchestrator instead.)
- Provider `blend_url` composition moves into the strategy. Today the
  router hand-rolls `f"{modal_dispatch.PUBLIC_BACKEND_URL}/render-groups/{group_id}/input/{filename}"`
  three times. The strategy should expose `blend_url_for(group)`.

### 2.4 `services/render_groups/service.py` — RenderGroupService

Thin coordinator that the HTTP router talks to. Holds references to the
orchestrator factory, upload coordinator, and frame planner.

```python
class RenderGroupService:
    def create(self, payload: CreateRenderGroupPayload, user: dict) -> RenderGroup: ...
    def confirm_upload(self, group_id: str, payload: ConfirmRenderGroupPayload, user: dict) -> dict:
        """Resolves frame range, plans tasks, persists jobs, triggers dispatch."""
    def cancel(self, group_id: str, user: dict) -> dict: ...
    def rerender(self, group_id: str, payload: ReRenderPayload, user: dict) -> dict: ...
    def get_status(self, group_id: str, user: dict) -> dict:
        """Read-only: serialize group + jobs.  Does NOT mutate.  Does NOT call failover."""
```

`get_status` is **read-only** — this is the big win. Today
`_get_render_group_inner` mutates the DB (orphan cleanup, failover,
status promotion) from a GET handler. After this refactor those mutations
happen in the background scanner (step 2.5) and `get_status` just serializes.

### 2.5 `scheduling/failover_scanner.py` — background task

Replaces `_check_failover`. Runs every 30s in a daemon thread.

```python
class FailoverScanner:
    def __init__(self, interval_sec: int = 30): ...
    def start(self) -> None: ...
    def _tick(self) -> None:
        for group in query_all("SELECT * FROM render_groups WHERE status IN ('pending','running')"):
            jobs = query_all("SELECT * FROM jobs WHERE group_id = %s", (group["id"],))
            self._scan_orphans(group, jobs)
            self._scan_stale(group, jobs)
            self._scan_failed_retries(group, jobs)
```

Each `_scan_*` method is a small pure-ish function that picks out the jobs
that need action and calls `orchestrator.handle_failure(job, reason)`.

**Start the scanner from [server/main.py](server/main.py)** next to the
existing scheduler startup. Use a single global instance.

**After this lands, remove:**
- `_check_failover` from `render_groups.py` entirely.
- The failover block from `_get_render_group_inner`.
- `services/modal/monitor.py`'s `_handle_failure` dispatches to the
  orchestrator (already a TODO under step 2.3).

### 2.6 Strip RunPod

Only do this once Track 2 is green on Modal + Vast + community.

- Delete `scheduling/strategies/runpod_strategy.py`.
- Remove `runpod_serverless` from `domain/value_objects.SERVERLESS_TYPES`.
- Delete `services/runpod_dispatch.py` and `services/runpod/` directory.
- Delete the RunPod dispatch block in `render_groups.py` (will already be
  gone after 2.3, but double-check).
- Delete any `runpod_*` columns/values if they exist in fixtures.
- Remove `RUNPOD_*` env vars from `.env.example` and config.
- Desktop app: remove RunPod UI affordances in [desktop/src/](desktop/src/)
  if any exist (grep for `runpod` case-insensitive).

### 2.7 Router becomes thin

Final shape of [server/api/routers/render_groups.py](server/api/routers/render_groups.py):

```python
router = APIRouter()
service = RenderGroupService(...)
upload = UploadCoordinator(...)

@router.post("/render-groups/create")
def create_render_group(payload: CreateRenderGroupPayload, user = Depends(get_current_user)):
    return service.create(payload, user)

@router.post("/render-groups/{group_id}/multipart-upload/init")
def init_multipart(group_id, payload, user = Depends(get_current_user)):
    try:
        return upload.init_multipart(group_id, user["uid"], payload.total_bytes)
    except UploadError as e:
        raise HTTPException(e.status, e.message)

# ... etc
```

Target: **under 300 lines**, no `execute(...)` or `query_one(...)` in the
router file, no `threading` imports.

---

## Recommended order + checkpoints

Each numbered step should be a separate commit. Run the server and exercise
the golden path (upload -> confirm -> render -> watch progress -> done) after
each step.

1. `frame_distribution.py` move (Track 1.1)
2. `render_group.py` entity (Track 1.2)
3. `render_job.py` entity (Track 1.3)
4. `priority_policy.py` + `failover_policy.py` (Track 1.4, 1.5)
5. `upload_coordinator.py` (Track 2.1) — router still does dispatch
6. `frame_planning.py` (Track 2.2)
7. `orchestrator.py` + `service.py` (Track 2.3, 2.4) — biggest commit; the
   three dispatch blocks collapse into one call
8. `failover_scanner.py` (Track 2.5) — delete `_check_failover`; make GET
   read-only
9. Strip RunPod (Track 2.6)
10. Final router cleanup (Track 2.7)

---

## Things to verify before calling it done

- [ ] `confirm_render_group_upload` end-to-end: upload, confirm, see jobs
      dispatched, progress ticks, group goes to `done`.
- [ ] Manual cancel mid-render: all non-terminal jobs end up `cancelled`,
      no leaked Modal function calls (`modal function-call list` should be
      empty for the group).
- [ ] Rerender of a failed group: new jobs created, old jobs untouched.
- [ ] Modal failover: kill a Modal container mid-render (or let the watchdog
      fire), scanner should pick it up within 30s and dispatch to Vast.
- [ ] Vast failover: simulate a Vast failure, scanner should dispatch to
      another Vast instance.
- [ ] Community failover: unplug a desktop worker, scanner should failover
      to serverless.
- [ ] `GET /render-groups/{id}` does not mutate the database. Run with a
      second process tailing `UPDATE` statements to verify.
- [ ] No imports of `services.runpod_dispatch` anywhere under
      `server/`.
- [ ] No `threading.Thread(target=_dispatch_...)` anywhere outside the
      orchestrator.
- [ ] `render_groups.py` is under 300 lines.

---

## Gotchas

- **Cancel must use the captured `function_call_id`, not a synthetic id.**
  The Modal dispatch must return the real `function_call_id` or
  `dispatcher.cancel()` will log `MODAL CONTAINER LEAK`. This is already
  fixed in [modal_worker/app.py](modal_worker/app.py) — do not regress it
  during the refactor.
- **Background threads + tests.** Tests should be able to run the
  orchestrator's `execute()` with a synchronous strategy. Make the
  "dispatch in background thread" behavior a strategy-level decision, not
  an orchestrator-level one, so tests can swap it.
- **`PUBLIC_BACKEND_URL` per provider**. Each provider's services module
  reads its own env var. Do not collapse them into a single
  `blend_url_base` in the orchestrator — ask the strategy for the URL.
- **The heartbeat grace period is per-job, not per-group.** `monitor.py`
  tracks `started_at` in-memory. If the scanner takes over heartbeat dead
  detection, it needs a DB-backed equivalent (use `jobs.submitted_at`).
- **The serverless retry cap is per-range, not per-job.** A failed range
  with 5 failed attempts stops being retried even if a new failover target
  appears. Preserve this — uncapped retry of a broken blend wastes GPU
  budget.
