# Allocation module extraction — plan + UML

Carve allocation + dispatch out of `orchestrator/` into a new top-level
sibling module `serverV2/allocation/`.

**Boundary rules.**
- One way **in**: `AllocationFacade` (only public class, **thin** — no
  business logic, just delegations to services).
- One way **out**: `AllocationDispatchQueueRepository` (the only DB
  exit for allocation-owned tables).
- Orchestrator never imports `AllocationFacade` directly. It calls
  `AllocationClient`, the orchestrator-side gateway and the only thing
  in the entire codebase allowed to import the facade.
- Strategy choice (Default / FastRender / Economy) is **orchestration
  logic** — orchestrator picks by tier and passes the strategy name.
- **No drain calls anywhere.** Dispatch is tick-driven by an internal
  background daemon. Lifecycle's terminal handlers do `release` on the
  shared `InProgressChunkRepository` directly and walk away.
- **Every class inside `allocation/` is prefixed with `Allocation`.**
  No bare `Dispatcher` / `Tiers` / `ChunkRequest` / `Daemon` / etc.

---

## Module shape

```
serverV2/
├── allocation/
│   ├── __init__.py                                     ← exports AllocationFacade only
│   ├── allocation_facade.py                            ← AllocationFacade (3 thin delegations)
│   ├── services/
│   │   ├── allocation_planning_service.py              ← AllocationPlanningService
│   │   └── allocation_dispatch_queue_service.py        ← AllocationDispatchQueueService
│   ├── strategies/
│   │   ├── default_allocation_strategy.py
│   │   ├── fast_render_allocation_strategy.py
│   │   ├── economy_allocation_strategy.py
│   │   ├── allocation_chunk_request.py                 ← AllocationChunkRequest
│   │   ├── allocation_heaviness_bands.py               ← AllocationHeavinessBands
│   │   ├── allocation_tiers.py                         ← AllocationTiers
│   │   ├── validators/
│   │   │   ├── allocation_target_validator.py
│   │   │   ├── allocation_engine_compatibility_validator.py
│   │   │   └── allocation_validation_context.py
│   │   └── analyzers/
│   │       ├── allocation_cost_analyzer.py
│   │       └── allocation_time_analyzer.py
│   ├── allocation_dispatch_queue_daemon.py             ← AllocationDispatchQueueDaemon
│   ├── allocation_dispatcher.py                        ← AllocationDispatcher
│   ├── allocation_blend_url_resolver.py                ← AllocationBlendUrlResolver
│   └── allocation_dispatch_queue_repository.py         ← AllocationDispatchQueueRepository (MOVED from repositories/)
│
├── orchestrator/
│   ├── allocation_client.py                            ← AllocationClient (orchestrator-side gateway)
│   ├── lifecycle.py
│   ├── lifecycle_job_retry/
│   ├── lifecycle_job_termination/
│   ├── anti_affinity/
│   └── chunk_progress/
│
├── repositories/
│   ├── job_repository.py                               ← STAYS. Allocation reads via Callable only.
│   ├── in_progress_chunk_repository.py                 ← STAYS. Shared.
│   └── ...
│
└── fleets/
```

**No factory.** `AllocationFacade` is a process-singleton.

**No `AllocationRepository` wrapper.** With only one allocation-owned
repo, a wrapper would be ceremony.

---

## Layered design (SRP per class)

```
AllocationFacade  (entry, 3 delegations, no logic)
   ├── AllocationPlanningService        plan_initial / plan_retry
   │     └── strategies/...             DefaultAllocationStrategy etc.
   └── AllocationDispatchQueueService   enqueue / dispatch_pending_for_fleet
         ├── AllocationDispatchQueueRepository    queue table (allocation-owned)
         ├── InProgressChunkRepository            claim_or_replace, current_job_for (shared)
         ├── AllocationDispatcher                 FleetRegistry routing
         ├── AllocationBlendUrlResolver
         └── active_count_by_fleet                Callable bound to JobRepository

AllocationDispatchQueueDaemon  (background, singleton via Redis lock)
   └── AllocationDispatchQueueService.dispatch_pending_for_fleet(f) every tick
```

---

## Tick-driven dispatch

`AllocationFacade.enqueue(group_id, tasks, ctx)` is a pure write. Rows
hit `dispatch_queue`, that's it. `AllocationDispatchQueueDaemon` wakes
every ~2s and runs `dispatch_pending_for_fleet(f)` for each enabled
fleet:

```
while cap_has_room(fleet) AND queue_has_items(fleet):
    pop one item → claim ledger → dispatch via fleet strategy
```

Singleton across Cloud Run replicas via Redis lock (same pattern as
`MonitorLockSweeper` / `CommunityMonitor`).

---

## Performance: parallel availability + Redis-cached snapshot

The availability check (Vast HTTPS probes, Modal Redis-or-PG cap math,
Community DB query) currently happens via `FleetAvailabilityBuilder`
in `serverV2/fleets/fleet_availability/`. With a 2s tick and
potentially many queued chunks per tick, we cannot afford to (a) run
the three fleet probes sequentially, or (b) repeat the probe per chunk.

**Three changes, all inside `serverV2/fleets/fleet_availability/`** —
allocation just consumes a snapshot, same as today.

### 1. Parallel per-fleet builds inside `FleetAvailabilityBuilder.build()`

Today `build()` runs `vast.build()`, `modal.build()`, `community.build()`,
`in_progress_serverless.build()` sequentially. Change to a
`ThreadPoolExecutor` (max_workers=4) that submits each flagged step in
parallel and `.result()`s them when assembling the snapshot. Vast's
existing internal `ThreadPoolExecutor` for parallel `/bundles/` probes
is unaffected — it nests under the new outer pool.

Net latency: `max(vast, modal, community)` instead of `sum(...)`.

### 2. Redis-cached snapshot with 60s TTL — `FleetAvailabilitySnapshotCache`

New class at
`serverV2/fleets/fleet_availability/fleet_availability_snapshot_cache.py`.

```python
class FleetAvailabilitySnapshotCache:
    KEY = "fleet:availability:snapshot"
    TTL_S = 60

    def get_or_build(self) -> FleetAvailabilitySnapshot:
        cached = self._redis.get(self.KEY)
        if cached:
            return self._deserialize(cached)
        snapshot = self._builder.new() \
            .check_vast().check_modal() \
            .check_community().check_in_progress_serverless_fleet() \
            .build()                                    # parallel internally
        self._redis.set(self.KEY, self._serialize(snapshot), ex=self.TTL_S)
        return snapshot

    def persist(self, snapshot: FleetAvailabilitySnapshot) -> None:
        # Update value, preserve TTL. If TTL has expired, no-op —
        # next get_or_build() rebuilds and resets TTL.
        self._redis.set(self.KEY, self._serialize(snapshot), keepttl=True, xx=True)
```

- `XX` flag means "only set if key exists" — handles the race where TTL
  expired between read and write.
- `KEEPTTL` flag preserves the existing TTL so we don't accidentally
  refresh the freshness window on every mid-tick mutation.
- Source of truth refresh cadence: **every 60s**, regardless of how
  many ticks fire in between.

### 3. One read per tick, mutate in memory, write back at end

`AllocationDispatchQueueDaemon.tick()`:

```python
def tick(self):
    snapshot = self._cache.get_or_build()                # 1 read per tick
    mutable = snapshot.to_mutable()
    for fleet in self._enabled_fleets:
        self._dispatch_queue.dispatch_pending_for_fleet(fleet, mutable)
        # ^ mutates `mutable` in place as it dispatches each chunk
    self._cache.persist(mutable.to_frozen())             # 1 write per tick
```

`AllocationDispatchQueueService.dispatch_pending_for_fleet(fleet, snapshot)`
takes the mutable snapshot and updates it after each successful
dispatch:

| Fleet | Mutation on dispatch |
|---|---|
| Vast | drop the chosen GPU offer from `snapshot.vast_available` if it's now exhausted; bump `serverless_in_flight["vast_serverless"]` |
| Modal | bump `serverless_in_flight["modal_serverless"]`; remove modal endpoint from `modal_available` if its per-gpu cap is now hit |
| Community | drop the dispatched machine from `snapshot.community_available` |

**Initial-dispatch path (Lifecycle.start_render → AllocationClient.plan_initial)**
also reads from the cache — same `get_or_build()` call — but does not
mutate. Worst-case staleness for planning is 60s, which is fine
because the daemon's per-dispatch cap-gating (via
`active_count_by_fleet` Callable) is real-time.

### What's added

| File | Class |
|---|---|
| `serverV2/fleets/fleet_availability/fleet_availability_snapshot_cache.py` | `FleetAvailabilitySnapshotCache` |
| `serverV2/fleets/fleet_availability/mutable_fleet_availability_snapshot.py` | `MutableFleetAvailabilitySnapshot` (with `to_frozen()`) |
| Modified: `fleet_availability_builder.py` | `build()` uses inner `ThreadPoolExecutor` |
| Modified: `fleet_availability_snapshot.py` | adds `to_mutable()` |

**Call sites deleted under the tick model:**

| Site today | Fate |
|---|---|
| [main.py:133](../../main.py#L133) — boot drain loop | deleted; daemon's first tick covers it |
| [lifecycle.py:466](../../orchestrator/lifecycle.py#L466) — drain on success | deleted; daemon picks up freed slot next tick |
| [drain_fleet_step.py:30](../../orchestrator/lifecycle_job_termination/steps/drain_fleet_step.py#L30) — termination pipeline drain | step deleted |
| `coordinator.enqueue_and_flush` | becomes `AllocationDispatchQueueService.enqueue` (pure write) |
| `DispatchCoordinator` class | gone — split into the two services |

---

## Ownership

| Entity | Owner | Notes |
|---|---|---|
| `jobs` table (`JobRepository`) | **shared** — fleets create, lifecycle transitions | Allocation reads `count_active_by_fleet` via injected Callable. |
| `dispatch_queue` table (`AllocationDispatchQueueRepository`) | **allocation** | Repo physically moves into `serverV2/allocation/`. |
| `in_progress_chunks` table (`InProgressChunkRepository`) | **shared** | Allocation writes `claim_or_replace`. Orchestrator owns `release`, `release_all`, `release_if_owner`, sibling reads. |
| Strategy choice | **orchestrator** | Tier-driven; passed in as `strategy_name: str`. |
| Allocation strategy implementations | **allocation** | Internal classes. |
| `FleetAvailabilitySnapshot` | **fleets** | Built by orchestrator before each call to allocation. |
| Dispatch tick + cap-gated dispatch loop | **allocation** | `AllocationDispatchQueueDaemon`, internal class, Redis-locked singleton. |

---

## Who reaches what

**Inbound to orchestrator** — everything lands on `Lifecycle`:
- `JobsRouter` → `JobService` → `Lifecycle`
- `RenderGroupsRouter` → `RenderGroupService` → `Lifecycle`
- `ModalCallbackHandler` → `Lifecycle`
- `CommunityMonitor` → `Lifecycle.cancel_one_job`
- `JobMonitorSweep` → `Lifecycle.handle_chunk_failed`

**Inside orchestrator, only TWO classes call `AllocationClient`:**
1. `Lifecycle` — `start_render`: plan + enqueue
2. `RetryPipelineRunner` — `plan_retry` + `enqueue`

`TerminationPipelineRunner` does NOT call `AllocationClient`. Its
ledger releases go straight to the shared `InProgressChunkRepository`.

**No one calls drain.** The `AllocationDispatchQueueDaemon` ticks
autonomously.

---

## Diagrams

- [allocation_module_internals.puml](allocation_module_internals.puml) — facade + services + strategies + daemon + dispatcher; strict layering and shared-repo boundary.
- [orchestrator_allocation_client.puml](orchestrator_allocation_client.puml) — inbound callers to orchestrator; the single `AllocationClient` seam outbound.
- [dispatch_runtime_flow.puml](dispatch_runtime_flow.puml) — three sequences: terminal callback → release; failure → plan + enqueue; daemon tick → dispatch.

---

## Migration phases

| Phase | Action | Reversible? |
|---|---|---|
| **A** | Create `serverV2/allocation/`. Copy strategies, dispatcher, blend_url_resolver (renamed to module-prefix). Build `AllocationPlanningService`, `AllocationDispatchQueueService`, `AllocationFacade`, `AllocationDispatchQueueDaemon`. **Move + rename** `dispatch_queue_repository.py` from `serverV2/repositories/` to `serverV2/allocation/allocation_dispatch_queue_repository.py` (sole consumer). **Don't start the daemon.** Wire alongside the existing coordinator. New module is constructible but inert. | yes — revert + restore repo file |
| **B** | Atomic cutover commit:<br>• Add `AllocationClient` to orchestrator.<br>• Switch `Lifecycle.start_render` to `AllocationClient.plan_initial_for_group` + `enqueue`.<br>• Switch `Lifecycle.handle_chunk_succeeded` / `handle_chunk_failed` to `release` directly on `InProgressChunkRepository` (delete `drain_for_fleet`).<br>• Delete the boot drain in [main.py:133](../../main.py#L133).<br>• Start the `AllocationDispatchQueueDaemon` at boot (Redis-locked).<br>Test a real render. | yes — revert the commit |
| **C** | Switch retry pipeline steps to `AllocationClient`. Test forced retry. | yes |
| **D** | Remove `DrainFleetStep` from termination pipeline. | yes |
| **E** | Delete `serverV2/orchestrator/allocation/`, `serverV2/orchestrator/dispatch/`, `serverV2/orchestrator/blend_url_resolver.py`. Single commit. | only via revert |

Each phase compiles and runs. Phase B is the largest atomic step.

---

Approve and I'll start Phase A.
