# Allocator Redesign — Phased Plan

Sequential plan for moving the allocator from the current single-strategy,
machine-row-per-endpoint model to a capability-based, queue-aware,
heaviness-aware multi-strategy model.

Last updated 2026-04-24.

---

## Why this exists

The current allocator misrepresents serverless capacity.  A row in the
`machines` table implies "this is an instance with identity," which is
fiction for Modal and Vast — they're elastic blueprints, not finite slots.
This causes:

* **Pre-decided machine count.**  We hardcode "one row per endpoint" and
  the allocator can only pick a given GPU type once per render.
* **Spaghetti naming.**  `Machine` is overloaded — sometimes a real PC,
  sometimes a serverless capability descriptor.
* **No fleet-level capacity caps.**  A render could in theory provision
  unlimited Modal containers.
* **No fan-out per fleet.**  The (now-deleted) `serverless_expander` was
  a half-solution that lived in the wrong place.
* **No heaviness-aware allocation.**  The 5 GB alice blend gets sent to
  the A10G (24 GB VRAM) and OOMs because the scorer is generic.

Three phases fix this.  Ship and observe between each.

---

## Decisions already locked in

| Question | Decision |
|---|---|
| Strategy choice mechanism | Auto, by file size + frame count |
| Heaviness signal | `r2_input_size_bytes` (stored on `render_groups` at upload-confirm) |
| Modal `fleet_max_parallel` | 15 |
| Vast `fleet_max_parallel` | 15 |
| Community queues | Per-machine, one job at a time (status quo) |
| In-flight count | DB query at allocation time only — not continuous |
| Anti-affinity on retry | Yes — exclude failed `(fleet, gpu_type)` for serverless, `machine_id` for community |
| Naming | Retire `Machine` class.  Introduce `CommunityMachine` + `FleetCapability` |
| Strategy class names | `DefaultAllocationStrategy` (renamed from `DefaultFrameAllocator`) + `FastRenderAllocationStrategy` (new) |
| Speed scoring | Normalized `render_speed` (0–1) per machine in `config.json`.  Modal entries gain it. |
| `dispatch_queue` table | Reused for fleet-cap queueing in Phase 2 |
| Frontend | No changes for Phase 1.  `tasks` API stays per-job. |
| Registrars | `ModalMachineRegistrar` + `VastMachineRegistrar` deleted in Phase 1.  Recovery + per-job monitors are independent and stay. |

---

## Architectural target (end state after Phase 3)

```
┌────────────────────────────────────────────────────────────────┐
│ config.json                                                    │
│   modal.max_parallel = 15                                      │
│   vast.max_parallel  = 15                                      │
│   modal_instances  [ {gpu_type, vram_gb, render_speed, ...} ]  │
│   vast_instances   [ {gpu_name,  vram_gb, render_speed, ...} ] │
└──────────────────┬─────────────────────────────────────────────┘
                   │
        ┌──────────▼────────────┐
        │ AvailableResources    │   built per-allocation in
        │  community_machines   │   RenderLifecycle._resource_picker
        │  serverless_capabs    │
        │  serverless_in_flight │
        └──────────┬────────────┘
                   │
         ┌─────────▼──────────┐
         │ FrameAllocator     │   Protocol
         │  (Strategy)        │
         └─────────┬──────────┘
                   │ realised by
       ┌───────────┼────────────────────────┐
       ▼                                    ▼
┌──────────────────────────┐   ┌────────────────────────────────┐
│ DefaultAllocationStrategy│   │ FastRenderAllocationStrategy   │
│ (current behaviour,      │   │ (heaviness-aware, fleet-cap-   │
│  rename only in Phase 1) │   │  aware, fan-out, anti-affinity)│
└──────────────────────────┘   └────────────────────────────────┘
                   │
                   ▼ produces
        ┌──────────────────────────────┐
        │ list[PlannedTask]            │
        │ ─ fleet                      │
        │ ─ machine_id  (community)    │
        │ ─ gpu_type    (serverless)   │
        │ ─ frame range, attempt, ...  │
        └─────────────┬────────────────┘
                      │
                      ▼
              DispatchCoordinator
                  │  (Phase 2: fleet-cap check; queue overflow)
                  ▼
                Dispatcher → Strategy → provider
```

---

## Phase 1 — Capacity model split (foundation)

**Goal:** stop pretending Modal/Vast endpoints are machine rows.  Split
the model cleanly between concrete community machines and serverless
capability descriptors.

### Files affected

| File | Change |
|---|---|
| `serverV2/core/models.py` | **Add** `CommunityMachine`, `FleetCapability`, `AvailableResources`.  **Retire** `Machine`. |
| `serverV2/core/interfaces.py` | `IFleetStrategy.machine_type` → `fleet`.  Drop `cancel(machine_id)` arg.  Drop `min_frames_per_instance` if unused after this. |
| `serverV2/orchestrator/allocation/frame_allocator.py` | New signatures: take `AvailableResources`, not `list[Machine]`. |
| `serverV2/orchestrator/allocation/default_frame_allocator.py` → `default_allocation_strategy.py` | **File rename.**  Class `DefaultFrameAllocator` → `DefaultAllocationStrategy`.  Update `allocate_initial` / `allocate_retry` to consume `AvailableResources` and produce extended `PlannedTask`. |
| `serverV2/orchestrator/allocation/chunk_request.py` | Add `excluded_serverless_capabilities: tuple[tuple[str, str], ...]`. |
| `serverV2/core/models.py` (PlannedTask) | Extend with `fleet`, `machine_id` (optional), `gpu_type` (optional).  Keep frame fields as-is. |
| `serverV2/orchestrator/lifecycle.py` | Rename `_machine_picker` → `_resource_picker`.  Build `AvailableResources` from: community DB + config + in-flight count query.  Pass to allocator. |
| `serverV2/orchestrator/dispatch/coordinator.py` | Read `task.fleet` instead of inferring from `machine_id`.  Dispatch as before. |
| `serverV2/orchestrator/dispatch/dispatcher.py` | Route by `task.fleet`. |
| `serverV2/fleets/registry.py` | Already cleaned; verify nothing references `Machine`. |
| `serverV2/fleets/modal/strategy.py` | Read `task.gpu_type` directly.  Drop `gpu_type_lookup` parameter and field. |
| `serverV2/fleets/vast/strategy.py` | Same — read `task.gpu_type` directly.  Drop `gpu_name_lookup`. |
| `serverV2/fleets/community/strategy.py` | Read `task.machine_id` (already does). |
| `serverV2/fleets/modal/machine_registrar.py` | **DELETE FILE.** |
| `serverV2/fleets/vast/machine_registrar.py` | **DELETE FILE.** |
| `serverV2/fleets/modal/recovery.py` | Verify it doesn't import the registrar.  No other change. |
| `serverV2/fleets/vast/recovery.py` | Same. |
| `serverV2/repositories/machine_repository.py` | Narrow to community.  Rename `get_available()` → `get_available_community()` if explicit clarity helps.  Strip `machine_type='*_serverless'` branches. |
| `serverV2/services/machines/service.py` | Adjust to community-only.  Probably trim machine endpoints showing serverless. |
| `serverV2/api/routers/machines.py` | Returns only community machines.  Document the contract change. |
| `serverV2/services/render_groups/service.py` | If it calls `_machine_picker` directly, update to `_resource_picker`. |
| `serverV2/bootstrap.py` | Drop `ModalMachineRegistrar`, `VastMachineRegistrar` construction.  Drop `gpu_type_lookup` / `gpu_name_lookup` injection.  Drop their `start_heartbeat()` from `_register_all_machines` / leader-only daemons.  Add resource-picker wiring. |
| `serverV2/main.py` | If anything references registrars, update. |
| `serverV2/config.py` | `ModalEndpoint` and `VastEndpoint` already have `vram_gb`, `cpu_cores`, `ram_gb`.  Add normalized `render_speed` to Modal endpoints if missing.  Already on Vast. |
| `serverV2/config.json` | No structural change.  Optionally normalize all `render_speed` to a 0–1 scale (was 1.3, 1.0, 0.95 — recalibrate to e.g. 0.70, 0.55, 0.50 against RTX 4090 = 1.0). |

### New types — exact shape

```python
@dataclass(frozen=True)
class CommunityMachine:
    id: str
    gpu_model: str
    gpu_vram_gb: float
    cpu_cores: int
    ram_gb: float
    render_speed: float
    status: str
    last_seen_at: str | None

@dataclass(frozen=True)
class FleetCapability:
    fleet: str               # "modal_serverless" | "vast_serverless"
    gpu_type: str            # "h100", "rtx_a6000", ...
    label: str
    vram_gb: float
    cpu_cores: int
    ram_gb: float
    render_speed: float
    fleet_max_parallel: int

@dataclass(frozen=True)
class AvailableResources:
    community_machines: list[CommunityMachine]
    serverless_capabilities: list[FleetCapability]
    serverless_in_flight: dict[str, int]   # fleet -> active+pending count

@dataclass(frozen=True)
class PlannedTask:
    fleet: str
    machine_id: str | None
    gpu_type: str | None
    label: str
    vram_gb: float
    render_speed: float
    frame_start: int
    frame_end: int
    frame_step: int
    total_frames: int
    chunk_index: int | None = None
    attempt: int = 0
```

### Resource-picker contract

```python
def _resource_picker(self) -> AvailableResources:
    community = self._machine_repo.get_available_community()
    serverless = [
        FleetCapability(
            fleet="modal_serverless", gpu_type=ep.gpu_type, label=ep.label,
            vram_gb=ep.vram_gb, cpu_cores=ep.cpu_cores, ram_gb=ep.ram_gb,
            render_speed=ep.render_speed,
            fleet_max_parallel=self._cfg.modal.max_parallel,
        )
        for ep in self._cfg.modal.endpoints
    ] + [
        FleetCapability(
            fleet="vast_serverless", gpu_type=ep.gpu_name, label=ep.label,
            vram_gb=ep.vram_gb, cpu_cores=ep.cpu_cores, ram_gb=ep.ram_gb,
            render_speed=ep.render_speed,
            fleet_max_parallel=self._cfg.vast.max_parallel,
        )
        for ep in self._cfg.vast.endpoints
    ]
    in_flight = self._job_repo.count_active_by_fleet()  # NEW repo method
    return AvailableResources(community, serverless, in_flight)
```

### Order of execution

Atomic — must ship together.  Suggested sequence to keep type-checker
loud at every step:

1. Add new types in `core/models.py` (additive).
2. Add `count_active_by_fleet` to `JobRepository`.
3. Add `_resource_picker` in `RenderLifecycle` alongside the old `_machine_picker`.
4. Update `FrameAllocator` Protocol and `DefaultAllocationStrategy`.
5. Update strategies (Modal, Vast, Community) to read from new `PlannedTask`.
6. Update coordinator/dispatcher.
7. Wire `_resource_picker` into the lifecycle, drop `_machine_picker`.
8. Drop `ModalMachineRegistrar`, `VastMachineRegistrar` files.
9. Drop registrar wiring from `bootstrap.py`.
10. Retire old `Machine` class (delete after the type-checker is green).

### Risks / verification

* **Type errors at every site that imported `Machine`** — that's the
  point.  Use them as a checklist.
* **`/machines` API contract changes** — confirm no client (UI, scripts)
  reads serverless rows.
* **`render_speed` recalibration** — old values were ad-hoc.  Normalize
  to 0–1 against RTX 4090.  Document the new scale in `config.json` as
  a comment-equivalent (a `_README` field or a separate doc).

### Phase 1 done when

* Modal/Vast have **zero rows** in the `machines` DB table.
* `Machine` class no longer exists.
* `ModalMachineRegistrar` + `VastMachineRegistrar` files deleted.
* Allocator takes `AvailableResources`, returns `PlannedTask` with
  fleet-discriminant.
* All strategies dispatch using `task.gpu_type` (serverless) or
  `task.machine_id` (community) — no lookup callables.
* End-to-end render still works exactly as before.

---

## Phase 2 — Fleet caps + queue draining

**Goal:** enforce `fleet_max_parallel` per fleet.  When a fleet is at
cap, new tasks land in the existing `dispatch_queue` and are drained as
running jobs complete.

### Files affected

| File | Change |
|---|---|
| `serverV2/config.py` | Add top-level `modal.max_parallel`, `vast.max_parallel` parsing.  Default 15. |
| `serverV2/config.json` | Add `{ "modal": { "max_parallel": 15 }, "vast": { "max_parallel": 15 } }` blocks. |
| `serverV2/orchestrator/dispatch/coordinator.py` | Before dispatching each task, query `serverless_in_flight[fleet]`; if at cap, leave task in `dispatch_queue` instead of calling dispatcher.  Mark queued items with `(fleet, gpu_type)` tags so we can drain them by fleet. |
| `serverV2/repositories/dispatch_queue_repository.py` | Add columns / fields to record `fleet` and `gpu_type` per queued item.  Add `drain_for_fleet(fleet)` and `peek_next_for_fleet(fleet)` methods. |
| `serverV2/callbacks/success_handler.py` | After `mark_done`, call `coordinator.drain_for_fleet(fleet)`. |
| `serverV2/callbacks/failure_handler.py` | After non-retried failure, also call `coordinator.drain_for_fleet(fleet)`. |
| `serverV2/orchestrator/dispatch/coordinator.py` | Implement `drain_for_fleet(fleet)`: pop next queued item if any; re-check capacity; dispatch.  No-op if queue empty. |

### Subtleties

* **Race conditions on the cap.**  Two simultaneous renders both seeing
  "13 in flight, cap is 15" can both dispatch and overshoot.  Mitigate
  with a single transaction: count + dispatch under DB lock, OR redis
  `INCR` with rollback if dispatch fails.  Start with the lock approach
  for clarity; switch to Redis if perf becomes an issue.
* **Drain ordering.**  When a job completes, drain the queue
  **for the same fleet** the completed job ran on.  Other fleets'
  queues are independent.
* **Community queueing unchanged.**  Each community machine gets its
  own queue (today's behaviour).  `fleet_max_parallel` doesn't apply.

### Verification

* Submit 20 simultaneous Modal jobs.  Confirm only 15 dispatch, 5 sit
  in the queue.  As each completes, the next one starts.
* Same test for Vast.
* Confirm community renders are unaffected.

---

## Phase 3 — `FastRenderAllocationStrategy`

**Goal:** allocator that picks **mix + count** of machines, gated by
heaviness, with anti-affinity on retry.

### Prerequisites

Phase 1 (capability model) and Phase 2 (caps + queues) both shipped.

### Files affected

| File | Change |
|---|---|
| `serverV2/orchestrator/allocation/fast_render_allocation_strategy.py` | **NEW.**  Class `FastRenderAllocationStrategy` implementing `FrameAllocator`. |
| `serverV2/services/render_groups/service.py` | Add `r2_input_size_bytes` to render group at upload confirm time. |
| `serverV2/repositories/render_group_repository.py` | Persist + read the size column. |
| DB migration | Add `r2_input_size_bytes BIGINT` to `render_groups`. |
| `serverV2/orchestrator/lifecycle.py` | Strategy picker: `pick_strategy(file_size, total_frames) → FrameAllocator`. |
| `serverV2/bootstrap.py` | Construct both strategies; pass picker to lifecycle. |
| `serverV2/orchestrator/allocation/chunk_request.py` | Already has `excluded_serverless_capabilities` from Phase 1. |
| `serverV2/orchestrator/lifecycle.py::handle_chunk_failure` | When building `ChunkRequest` for retry, populate excluded capabilities from the failed task. |

### `FastRenderAllocationStrategy.allocate_initial` logic

```
input: frame_start, frame_end, frame_step, total_frames, resources

1. Determine heaviness band:
   - light    : file_size_bytes < 500MB
   - medium   : 500MB - 2GB
   - heavy    : > 2GB
   (band carried via instance state — strategy is constructed with
    file_size, OR ChunkRequest carries it forward)

2. Compute scene_min_vram from heaviness:
   - light  → 8GB
   - medium → 24GB
   - heavy  → 48GB

3. Filter eligible capabilities + community:
   eligible = [m for m in resources.all_machines() if m.vram_gb >= scene_min_vram]
   if not eligible: fall back to all (no machine meets it; warn)

4. Compute target frames per machine from heaviness:
   - light  → 8
   - medium → 5
   - heavy  → 3

5. Compute desired machine count:
   ideal_count = ceil(total_frames / target_frames_per_machine)
   ideal_count = clamp(ideal_count, min=1, max=hard_cap)
   hard_cap from config.json (suggest 12)

6. Score eligible by render_speed (highest first).

7. Pick mix, respecting fleet caps:
   selected = []
   in_flight = dict(resources.serverless_in_flight)   # mutate locally
   for capability in sorted_by_speed:
       while len(selected) < ideal_count:
           if capability.fleet in in_flight:
               if in_flight[capability.fleet] >= capability.fleet_max_parallel:
                   break  # this fleet is full; move on
               in_flight[capability.fleet] += 1
           selected.append(capability)
       if len(selected) >= ideal_count: break

8. Distribute frames across selected (weighted by render_speed),
   produce list[PlannedTask] with chunk_index 0..N-1.
```

### `FastRenderAllocationStrategy.allocate_retry` logic

```
input: chunk_request (carries excluded_serverless_capabilities and
       excluded_machine_ids), resources

1. Filter resources by exclusions.
2. Pick highest-speed eligible.
3. Single PlannedTask for the chunk's remaining frames.
```

### Strategy picker

```python
def pick_strategy(
    file_size_bytes: int, total_frames: int,
) -> FrameAllocator:
    if file_size_bytes >= 2 * 1024**3 or total_frames >= 30:
        return self._fast_render_strategy
    return self._default_strategy
```

Lives in lifecycle (or a tiny helper).  One method, one rule, easy to
extend.

### Phase 3 done when

* `FastRenderAllocationStrategy` exists and passes for heavy scenes.
* `r2_input_size_bytes` populated on every confirmed render group.
* Auto-selection works: small/light renders use Default, big/heavy use
  FastRender.
* Retry with anti-affinity verified: failing on H100 routes the retry
  to A6000 (or anything not H100) when an alternative exists.

### Open questions to revisit at Phase 3 time

1. **Heaviness bands — exact thresholds.**  500MB / 2GB are guesses.
   Calibrate with a few real scenes after Phase 2 ships.
2. **Fan-out hard cap.**  12 was a guess.  Capacity test on Modal:
   what's the max realistic concurrent provisioning before
   diminishing returns?
3. **Anti-affinity scope.**  Exclude just the failed `(fleet, gpu_type)`
   pair, or the whole fleet?  Today suggesting just the pair —
   `(modal, h100)` failure can still retry to `(modal, l40s)`.
4. **Cost-aware fallback strategy.**  Current pick is speed-first.  If
   cost ever matters, add `cost_score` to the data model and a
   `CostOptimizedAllocationStrategy`.  Defer.

---

## Cross-cutting things to watch

* **`Machine` references that survive Phase 1.**  Anything in
  `services/machines/service.py`, frontend serializers, or the
  `machine_repository` that still pretends serverless rows exist will
  silently return empty after Phase 1.  Audit by grep.
* **`PlannedTask.machine_id is not None` invariant.**  Anything reading
  `task.machine_id` blindly will break on serverless tasks.  Audit:
  `serialize_task`, dispatcher, anywhere logging.
* **Config drift.**  After Phase 1 the `machines` DB table is
  effectively documentation for community only.  After Phase 2 the
  `dispatch_queue` carries fleet/gpu_type tags; don't read it without
  filtering.

---

## Files that will exist at end of Phase 3

```
serverV2/orchestrator/allocation/
    frame_allocator.py                       Protocol
    chunk_request.py                         (extended with excluded caps)
    default_allocation_strategy.py           renamed from default_frame_allocator.py
    fast_render_allocation_strategy.py       NEW
serverV2/core/
    models.py                                CommunityMachine, FleetCapability,
                                             AvailableResources, PlannedTask (extended)
serverV2/orchestrator/dispatch/
    coordinator.py                           Phase 2 — fleet-cap-aware
    dispatcher.py                            fleet-discriminant
serverV2/orchestrator/
    lifecycle.py                             _resource_picker + pick_strategy
serverV2/fleets/
    registry.py                              (slimmed)
    modal/
        callback/                            unchanged
        client.py                            unchanged
        recovery.py                          unchanged
        strategy.py                          reads task.gpu_type
        # machine_registrar.py removed
    vast/
        callback/                            unchanged
        client.py                            unchanged
        recovery.py                          unchanged
        strategy.py                          reads task.gpu_type
        # machine_registrar.py removed
    community/
        community_monitor.py                 unchanged
        strategy.py                          reads task.machine_id
serverV2/repositories/
    machine_repository.py                    community-only
    dispatch_queue_repository.py             Phase 2 — fleet-tagged queue
    job_repository.py                        + count_active_by_fleet
    render_group_repository.py               + r2_input_size_bytes (Phase 3)
serverV2/config.json                         + modal.max_parallel, vast.max_parallel
                                             + normalized render_speed
serverV2/.env                                 (no Modal/Vast machine envs)
```

---

## Glossary

* **Fleet**: a provider type (`modal_serverless`, `vast_serverless`,
  `community`).  Determines dispatch path.
* **Capability**: a description of "we can provision this kind of
  machine."  Used for serverless.  Lives in config.json.
* **CommunityMachine**: a real PC with stable identity.  Used for the
  community fleet.  Lives in the DB.
* **`PlannedTask`**: the unit of dispatch.  Carries `fleet` plus either
  `machine_id` (community) or `gpu_type` (serverless).
* **Chunk**: a contiguous frame range assigned to one task.  Identified
  by `chunk_index` within a render group, stable across retries.
* **Heaviness band**: light / medium / heavy, derived from file size.
  Drives how aggressive parallelisation is.
* **Anti-affinity**: when retrying a failed chunk, exclude the original
  `(fleet, gpu_type)` (serverless) or `machine_id` (community) so we
  don't immediately reproduce the failure.
