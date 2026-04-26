# Tiered Allocation — Reference

Cost-aware frame allocation across community / vast / modal fleets, with three user-visible tiers (**Economy** / **Standard** / **Premium**).

This file documents what was built. Use it as a reference; the design is settled and the implementation matches.

**Status:** all 9 phases shipped on the `runpod-scaling`/`orchestration` branch. Premium tier is reserved (UI disabled, allocator not implemented — see "Future work" at the bottom).

---

## Goal

Today the allocator picks targets purely by speed (`render_speed`) and capacity (`fleet_max_parallel`). After this work, it also accounts for **cost** (`price_per_hour`) so users can pick a tier matching their budget vs. wall-time preference.

| Tier | Optimization | Behaviour |
|---|---|---|
| **Economy** | Cost first, speed second | Few machines (cap 6), big chunks, cheapest fleets preferred. Slow but cheap. |
| **Standard** | Speed first, cost as tie-breaker | Up to 12 machines, smaller chunks, soft budget cap (1.5× single-A6000-cost yardstick). |
| **Premium** | Speed only, cost-blind (future) | All-fastest mix, no cost ceiling. **UI disabled — allocator not built.** |

---

## Decisions taken (one-time, locked in)

| # | Question | Decision |
|---|---|---|
| 1 | Community machine pricing | **Flat $1.00/hr**, loaded from `config.json` (`community.price_per_hour`). No per-machine override yet. |
| 2 | Tier name for modified Fast | **Standard**. Internal class stays `FastRenderAllocationStrategy`. Hard-cap stays 12 machines. |
| 3 | Economy max-machines cap | **6** |
| 4 | Anti-burden rule | **Soft** — prefer to spread across enabled fleets, but cheapest still wins. No strict floor. |
| 5 | Empirical calibration | **Yes** — store actual seconds-per-frame per `(chunk_size, fleet, machine)` in `render_telemetry` (JSONB heaviness column). |
| 6 | Budget cap behavior on Standard | **Soft** — drop most expensive targets and re-estimate; never hard-fail with "over budget". |
| 7 | Cost estimate format to user | **Range** (e.g. "$2.40 — $3.80") with ±25% confidence band by default. |

---

## Architectural shape

```
                   ┌──────────────────────────────┐
                   │   API: /render-groups/create │
                   │   payload.tier = "economy"   │
                   └──────────────┬───────────────┘
                                  │
                                  v
                   ┌──────────────────────────────┐
                   │   RenderLifecycle.plan(tier) │
                   │   _pick_strategy(tier, ...)  │
                   │   _tier_budget(tier, ...)    │
                   └──┬───────────┬────────────┬──┘
                      │           │            │
                      v           v            v
         ┌────────────────┐  ┌──────────┐  ┌──────────┐
         │  Economy       │  │ Standard │  │ Premium  │
         │  (Phase 6)     │  │ (=Fast,  │  │ (future) │
         │                │  │  P7-aware)│  │          │
         └────┬───────────┘  └────┬─────┘  └────┬─────┘
              │                   │             │
              └─────────┬─────────┴─────────────┘
                        v
              ┌─────────────────────┐
              │   FrameAllocator    │ ← interface
              │   .allocate_initial │
              │   .allocate_retry   │
              └─────────┬───────────┘
                        v
                ┌───────────────┐
                │ TimeAnalyzer  │ ← pure  (Phase 3)
                │ CostAnalyzer  │ ← pure  (Phase 4)
                └───────────────┘
                        ▲
                        │ feeds back periodically
                ┌───────────────┐
                │  Telemetry    │ ← actual seconds-per-frame  (Phase 5)
                │  (render_     │   per (chunk_size, fleet, machine)
                │   telemetry)  │
                └───────────────┘
```

---

## Phase 1 — Pricing data on config

**What:** Stamp a `price_per_hour` on every fleet target so the allocator can read prices without lookups.

**Files:**
- [`serverV2/config.json`](serverV2/config.json) — `price_per_hour` added to all 4 vast_instances + 3 modal_instances + new top-level `community` block.
- [`serverV2/config.py`](serverV2/config.py) — `price_per_hour: float` on `VastEndpoint` and `ModalEndpoint` dataclasses (required at parse time). New `_load_community_price_per_hour()` helper. New `AppConfig.community_price_per_hour` (defaults to 1.0).
- [`serverV2/core/models.py`](serverV2/core/models.py) — `price_per_hour` on `FleetCapability` (default 0.0) and `CommunityMachine` (default 1.0). `CommunityMachine.from_row` accepts a `price_per_hour` kwarg.
- [`serverV2/repositories/machine_repository.py`](serverV2/repositories/machine_repository.py) — constructor takes `community_price_per_hour`, stamps it on every returned `CommunityMachine` (3 callsites).
- [`serverV2/bootstrap.py`](serverV2/bootstrap.py) — wires the loaded community price into `MachineRepository`; passes `ep.price_per_hour` into both `FleetCapability` constructions in `_resource_picker`.

**Initial price values:**
- Vast: 4090 $0.45, A6000 $0.55, A5000 $0.30, 3090 $0.25
- Modal: L4 $1.10, L40S $2.10, H100 $4.50
- Community: $1.00 flat (overridable via config.json)

**Behavior change:** none. Pricing flows end-to-end but no consumer reads it yet.

---

## Phase 2 — Stronger blend analysis

**What:** Extend the desktop's local Blender analyzer to extract 19 heaviness signals beyond the existing frame-range info, and add a server-side helper that pulls the sub-dict out with safe defaults.

**Files:**
- [`desktop/src-tauri/src/commands.rs`](desktop/src-tauri/src/commands.rs) — `ANALYZE_BLEND_PY` extended with a `payload["heaviness"]` sub-dict. Each section wrapped in try/except so a Blender API mismatch in one area degrades just those fields rather than failing the whole analysis.
- [`serverV2/core/value_objects.py`](serverV2/core/value_objects.py) — `parse_analysis_heaviness(snapshot, *, file_size_bytes=None)` reads the sub-dict with safe defaults for every key. Phase 3+ consumers can dot through without None-checks.

**The 19 fields, all under `analysis_snapshot["heaviness"]`:**

- **Render settings:** `render_engine`, `resolution_x`, `resolution_y`, `resolution_percentage`, `effective_pixels`, `samples` (engine-aware)
- **Geometry:** `vertex_count_total` (sum across visible MESH objects), `object_count`, `mesh_count`
- **Assets:** `material_count`, `texture_count`, `texture_total_bytes` (estimated VRAM cost via `w × h × channels`), `shader_node_count_total`
- **Heavy-feature flags:** `uses_subdivision`, `uses_displacement`, `uses_particles`, `uses_geometry_nodes`, `geometry_nodes_complexity`, `uses_subsurface_scattering`, `uses_volumetrics`
- **Server-side stamp** (added by `parse_analysis_heaviness`, not by the Blender script): `file_size_bytes`

**Behavior change:** none. Fields land in `analysis_snapshot_json` automatically (additive JSON column). Older desktop builds continue to work.

**Rebuild:** Tauri compiles `ANALYZE_BLEND_PY` via `include_str!` — the change ships only with a new desktop binary.

---

## Phase 3 — Time analyzer (pure module)

**What:** Heuristic seconds-per-frame estimator. Pure module: no I/O, no DB, no network. The output is a *ranking signal*, not a wall-time prediction.

**Files:**
- **NEW** [`serverV2/orchestrator/allocation/analyzers/__init__.py`](serverV2/orchestrator/allocation/analyzers/__init__.py) — package docstring.
- **NEW** [`serverV2/orchestrator/allocation/analyzers/time_analyzer.py`](serverV2/orchestrator/allocation/analyzers/time_analyzer.py) — the analyzer.

**Public API:**
- `estimate_seconds_per_frame(heaviness, render_speed) -> float` — fast path.
- `estimate_seconds_per_frame_from_snapshot(snapshot, render_speed)` — convenience wrapper.

**Heuristic:**
```
seconds_per_frame =
    BASELINE_SEC                         # 30s baseline (CYCLES, 1024 samples, 1080p, simple)
  * sample_factor(samples, engine)        # Cycles linear, EEVEE TAA cheaper
  * pixel_factor(effective_pixels)        # linear in pixels / 1080p
  * geometry_factor(vertex_count_total)   # log10: 100k=1.0, 1M=~1.5, 10M=~2.0
  * texture_factor(texture_total_bytes)   # mild VRAM-pressure ramp above 256MB
  * shader_factor(node_count, sss, vol)   # node-count ramp + SSS×1.4 + volumetrics×2.0
  * feature_factor(subdiv, displ, ...)    # subdivision×1.3, particles×1.5, etc.
  / max(MIN_RENDER_SPEED=0.1, render_speed)
```

All thresholds and exponents are module-level constants — easy to tune once Phase 5 telemetry accumulates real seconds-per-frame data.

**Smoke test:** 16 hard-asserted scenarios in `__main__` covering baseline, EEVEE engine, 4× samples, 4K, 10M verts, each heavy flag, kitchen-sink, legacy snapshot, render-speed floor, monotonicity in both directions.

```
python -m serverV2.orchestrator.allocation.analyzers.time_analyzer
```

**Behavior change:** none — pure module, no consumer imports it yet.

---

## Phase 4 — Cost analyzer (pure module) + startup overhead

**What:** Estimate `(wall_time, cost_range)` for a candidate mix. Composes Phase 3 with target prices.

**Files:**
- **NEW** [`serverV2/orchestrator/allocation/analyzers/cost_analyzer.py`](serverV2/orchestrator/allocation/analyzers/cost_analyzer.py) — the analyzer.
- [`serverV2/orchestrator/allocation/analyzers/time_analyzer.py`](serverV2/orchestrator/allocation/analyzers/time_analyzer.py) — extended with `estimate_startup_seconds(heaviness)` (per-chunk overhead estimate, scaled by heaviness).

**Public API:**
- `MixSlot(render_speed, price_per_hour, frames_assigned)` — one slot per target getting some share of frames.
- `CostEstimate(wall_time_seconds, cost_mid_usd, cost_low_usd, cost_high_usd)` — output dataclass with `.cost_range` property.
- `estimate_cost_for_mix(heaviness, mix, *, confidence_band=0.25) -> CostEstimate` — one call site per mix.
- `estimate_cost_from_snapshot(snapshot, mix, *, file_size_bytes, confidence_band)` — convenience wrapper.

**Cost model:**
```
For each MixSlot (one chunk per slot):
    spf      = estimate_seconds_per_frame(heaviness, slot.render_speed)
    startup  = estimate_startup_seconds(heaviness)          # heaviness includes file_size_bytes
    seconds  = startup + spf * slot.frames_assigned
    cost     = seconds / 3600 * slot.price_per_hour

wall_time = max(seconds across slots)        — chunks run in parallel
cost_mid  = sum(cost across slots)            — every machine bills
cost_low  = cost_mid * (1 - band)             — band defaults to 0.25
cost_high = cost_mid * (1 + band)              — band clamped to [0, 0.95]
```

**Startup overhead heuristic** (per-chunk, calibrated to the user's observed 5–20-min range for heavy files):
```
startup_sec = BASELINE_STARTUP_SEC                        # 90s
            + file_size_bytes / GB        * 30             # download
            + vertex_count_total / 1M     * 3              # BVH build
            + texture_total_bytes / GB    * 100            # VRAM upload
            + 5 + nodes * 0.05                              # shader compile
clamped to MAX_STARTUP_SEC = 30 minutes
```

| Scene | Resulting startup |
|---|---|
| Tiny (default cube, 0 file size) | 90s |
| Moderate (1 GB, 10M verts, 500 MB tex, 200 nodes) | ~3.6 min |
| Heavy (5 GB, 50M verts, 2 GB tex, 500 nodes) | ~10.3 min |
| Very heavy (10 GB, 100M verts, 4 GB tex, 1000 nodes) | ~19.1 min |

**Smoke test:** 15 assertions including empty mix, single-slot arithmetic, parallel wall-time, mismatched speeds, unbalanced frames, confidence-band clamping, scene heaviness propagation, price scaling, file-size effect, snapshot wrapper, frame-count amortization, zero-frame skip, negative-price clamp.

```
python -m serverV2.orchestrator.allocation.analyzers.cost_analyzer
```

**Behavior change:** none — pure module.

---

## Phase 5 — Empirical telemetry collection

**What:** Persist per-chunk completion data so we can later calibrate Phase 3+4 heuristics against real renders.

**Files:**
- [`serverV2/infrastructure/db.py`](serverV2/infrastructure/db.py) — Phase-5 migrations: `jobs.started_at`, `jobs.price_per_hour_at_dispatch`, new `render_telemetry` table + 2 indexes (`fleet, gpu_type`; `group_id`). All `IF NOT EXISTS` / idempotent.
- [`serverV2/core/models.py`](serverV2/core/models.py) — `CreateJobParams.price_per_hour_at_dispatch: float | None = None`.
- [`serverV2/repositories/job_repository.py`](serverV2/repositories/job_repository.py) — `create()` SQL persists the new column. New `mark_started(job_id)` method (idempotent NULL check).
- **NEW** [`serverV2/repositories/telemetry_repository.py`](serverV2/repositories/telemetry_repository.py) — `record_chunk(...)` writes one row per successful chunk.
- [`serverV2/callbacks/router.py`](serverV2/callbacks/router.py) — PROGRESS branch calls `mark_started` on first frame.
- [`serverV2/fleets/modal/strategy.py`](serverV2/fleets/modal/strategy.py) + [`serverV2/fleets/vast/strategy.py`](serverV2/fleets/vast/strategy.py) — look up `price_per_hour` from `self._cfg.endpoints` and stamp on `CreateJobParams`.
- [`serverV2/callbacks/success_handler.py`](serverV2/callbacks/success_handler.py) — after `mark_done`, writes a telemetry row. Skipped silently for legacy jobs missing the price stamp (community + pre-Phase-5). Wrapped in try/except — never breaks the success path.
- [`serverV2/bootstrap.py`](serverV2/bootstrap.py) — instantiates `TelemetryRepository`, passes it (plus `group_repo`) into `SuccessHandler`.

**`render_telemetry` schema:**
```sql
CREATE TABLE render_telemetry (
    id                  TEXT PRIMARY KEY,
    job_id              TEXT NOT NULL,
    group_id            TEXT NOT NULL,
    fleet               TEXT NOT NULL,    -- community / modal_serverless / vast_serverless
    gpu_type            TEXT,             -- "h100", "RTX 4090" (NULL for community)
    machine_id          TEXT,             -- only for community
    chunk_size          INTEGER NOT NULL,
    rendered_frames     INTEGER NOT NULL,
    started_at          TIMESTAMP WITH TIME ZONE NOT NULL,    -- first PROGRESS callback
    completed_at        TIMESTAMP WITH TIME ZONE NOT NULL,
    seconds_total       INTEGER NOT NULL,
    price_per_hour      NUMERIC(10, 4) NOT NULL,
    cost_actual_usd     NUMERIC(10, 4) NOT NULL,
    seconds_estimated   INTEGER,                              -- nullable until calibration loop wires this
    cost_estimated_usd  NUMERIC(10, 4),                       -- nullable until calibration loop wires this
    heaviness_json      JSONB NOT NULL DEFAULT '{}',
    file_size_bytes     BIGINT,
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
```

**Calibration query examples** (illustrative; consumed by future tooling):

```sql
-- "Average actual seconds-per-frame for Vast 4090 on Cycles renders without volumetrics?"
SELECT AVG(seconds_total::float / NULLIF(rendered_frames, 0))
FROM render_telemetry
WHERE fleet = 'vast_serverless' AND gpu_type = 'RTX 4090'
  AND heaviness_json->>'render_engine' = 'CYCLES'
  AND COALESCE((heaviness_json->>'uses_volumetrics')::boolean, false) = false;
```

**What's logged:** every successful chunk completion on Vast / Modal.

**What's NOT logged (defensive):**
- Cancelled chunks (data unreliable)
- Failed chunks (decision: skip in v1)
- Community jobs (no price stamp in v1)
- Legacy jobs from before Phase 5 deploy

**Latency:** one extra Postgres INSERT per chunk completion (~150 ms cross-Pacific). Wrapped in try/except — telemetry failures log a warning and never break the user-visible flow.

---

## Phase 6 — `EconomyAllocationStrategy`

**What:** New `FrameAllocator` realization that picks cheapest targets first, soft fleet diversification, max 6 machines, even frame distribution.

**Files:**
- **NEW** [`serverV2/orchestrator/allocation/heaviness_bands.py`](serverV2/orchestrator/allocation/heaviness_bands.py) — extracted from FastRender. Public: `band_for(...)`, `vram_floor_for(...)`, `frames_per_machine_for(...)`. Same table; shared by FastRender + Economy.
- [`serverV2/orchestrator/allocation/fast_render_allocation_strategy.py`](serverV2/orchestrator/allocation/fast_render_allocation_strategy.py) — imports from `heaviness_bands` instead of inline.
- **NEW** [`serverV2/orchestrator/allocation/economy_allocation_strategy.py`](serverV2/orchestrator/allocation/economy_allocation_strategy.py) — the strategy.

**Algorithm (`allocate_initial`):**

```
1. Build eligibles (community + serverless slots; validators + VRAM floor)
2. Sort cheapest-first   primary: price_per_hour ASC
                         tiebreaker: render_speed DESC  (free wall-time win for same price)
3. Soft diversification pick:
     fleet_cap = max(1, floor(ECONOMY_HARD_CAP * 0.70)) = 4
     - skip targets when their fleet has fleet_cap picks → push to rejected
     - if diversification leaves us short, backfill from rejected
4. Trim by min(ECONOMY_HARD_CAP=6, total_frames // ECONOMY_MIN_FRAMES_PER_CHUNK=3)
5. Distribute frames evenly via in-module _distribute_evenly helper
   (NOT speed-weighted — preserves cheapest-first order)
```

**`allocate_retry`:** cheapest-first single target after anti-affinity exclusion; tiebreaker by speed.

**Constants:**
```python
ECONOMY_HARD_CAP = 6
ECONOMY_FLEET_DIVERSIFICATION_CAP = 0.70
ECONOMY_MIN_FRAMES_PER_CHUNK = 3
```

**Smoke test:** 13 assertions covering cheapest-first across fleets, cap behavior, diversification, VRAM floor + fallback, even distribution with residual, tiebreaker, retry path, EEVEE engine filter, fleet capacity respect.

```
python -m serverV2.orchestrator.allocation.economy_allocation_strategy
```

**Behavior change:** none — class created and importable, but only routed to live traffic by Phase 8.

---

## Phase 7 — Budget-aware Standard

**What:** Two surgical edits inside `FastRenderAllocationStrategy`:
1. Selection becomes **value-aware** — sort by `compute_power_score / sqrt(price_per_hour)` instead of raw speed.
2. Soft budget cap — if estimated cost exceeds `tier_budget * 1.5`, drop the most expensive target and re-distribute. Loop until within budget or only one target remains.

**Files:**
- [`serverV2/orchestrator/allocation/fast_render_allocation_strategy.py`](serverV2/orchestrator/allocation/fast_render_allocation_strategy.py) — added 3 tunables, new `_value_score(target)` helper, rewired `_select_mix` and `allocate_retry`, added `tier_budget_usd` + `heaviness` kwargs to `allocate_initial`, new `_apply_soft_budget_cap` method, `__main__` smoke.
- [`serverV2/orchestrator/allocation/frame_allocator.py`](serverV2/orchestrator/allocation/frame_allocator.py) — Protocol gains `tier_budget_usd: float | None` and `heaviness: dict | None`.
- [`serverV2/orchestrator/allocation/default_allocation_strategy.py`](serverV2/orchestrator/allocation/default_allocation_strategy.py) + [`serverV2/orchestrator/allocation/economy_allocation_strategy.py`](serverV2/orchestrator/allocation/economy_allocation_strategy.py) — accept and ignore the new kwargs.

**Constants:**
```python
PRICE_SOFTENING_EXPONENT = 0.5   # sqrt; 1.0 = strict price/speed; 0.0 = price-blind
BUDGET_CAP_MULTIPLIER = 1.5      # soft headroom before trimming
_MIN_PRICE_FOR_SCORE = 0.001     # divide-by-zero guard
```

**Value-score effect on the current fleet:**

| Target | speed | $/hr | value (`speed / √$`) | Old rank | New rank |
|---|---|---|---|---|---|
| Vast A6000 (48 GB) | 1.30 | $0.55 | 186.51 | (4th) | **1st** |
| Vast 4090 | 1.45 | $0.45 | 126.23 | (2nd) | 2nd |
| Modal L40S | 1.70 | $2.10 | 124.82 | (1st) | 3rd |
| Modal H100 | 1.10 | $4.50 | 88.36  | (3rd) | last |

Scores include `compute_power_score`'s VRAM term — A6000's 48 GB beats 4090's 24 GB on geometry, which dominates here. **L40S falling behind A6000/4090 fixes the H100/L40S-over-cheaper-Vast bug** observed in production logs (the `runpod-scaling` H100-stuck job).

**Soft-budget-cap algorithm:**
```
threshold = tier_budget_usd * BUDGET_CAP_MULTIPLIER
while len(shares) > 1:
    cost_mid = estimate_cost_for_mix(heaviness, mix_slots)
    if cost_mid <= threshold: stop
    drop the share with highest price_per_hour
    re-distribute frames across remaining targets
```
Bounded at most `HARD_CAP - 1` iterations.

**Smoke test:** 9 assertions including value-score ranking, `_select_mix` value-aware selection, retry-after-exclusion, no-budget legacy path, defensive `heaviness=None` skip, soft cap drops H100 under tight budget, pathologically tiny budget terminates at ≥1, generous budget keeps full mix.

```
python -m serverV2.orchestrator.allocation.fast_render_allocation_strategy
```

---

### Phase 7-refactor — `file_size_bytes` consolidation

After Phase 7 landed, the analyzer/allocator API took `heaviness` and `file_size_bytes` as two separate kwargs at every layer. The split made sense at parse time (the desktop analyzer doesn't know on-disk size; the server does) but became wart at every caller. Refactored so:

- `parse_analysis_heaviness(snapshot, *, file_size_bytes=...)` stamps file size into the returned dict.
- Analyzers and allocators read it via `heaviness["file_size_bytes"]`.
- Single object travels end-to-end: `time_analyzer`, `cost_analyzer`, `FrameAllocator.allocate_initial`, `RenderLifecycle.plan`, `RenderOrchestrator.plan`.
- `service.py` builds heaviness once; allocators stop receiving the redundant kwarg.

Snapshot convenience wrappers (`*_from_snapshot`) keep `file_size_bytes` as an explicit kwarg since callers at that entry point typically have the snapshot from the DB but file size from a separate column.

No behavior change. Smoke tests updated and all-green.

---

## Phase 8 — Tier selection plumbing

**What:** End-to-end tier flow: payload → DB column → lifecycle routing → strategy selection.

**Files:**
- [`serverV2/infrastructure/db.py`](serverV2/infrastructure/db.py) — `tier TEXT` column on `render_groups`, idempotent ALTER.
- **NEW** [`serverV2/orchestrator/allocation/tiers.py`](serverV2/orchestrator/allocation/tiers.py) — `ECONOMY` / `STANDARD` / `PREMIUM` constants + `normalize()` (defaults to STANDARD for None/unknown) + `is_implemented()`.
- [`serverV2/api/schemas/render_group.py`](serverV2/api/schemas/render_group.py) — `tier: str | None` on `ConfirmRenderGroupPayload` + `ReRenderPayload`.
- [`serverV2/orchestrator/lifecycle.py`](serverV2/orchestrator/lifecycle.py) — `plan(...)` takes `tier`. `_pick_strategy(tier, ...)` switches on tier first; falls back to today's heaviness heuristic for STANDARD. Premium falls through to STANDARD with a warning. New `_tier_budget(tier, heaviness, total_frames)` derives Standard's cap from "what would a single A6000 cost" — scales with scene weight.
- [`serverV2/orchestrator/orchestrator.py`](serverV2/orchestrator/orchestrator.py) — `plan(..., tier=...)` plumbed through.
- [`serverV2/bootstrap.py`](serverV2/bootstrap.py) — instantiates `EconomyAllocationStrategy` and passes to `RenderLifecycle`.
- [`serverV2/services/render_groups/service.py`](serverV2/services/render_groups/service.py) — `confirm_upload` + `rerender` read `payload.tier`, normalize, persist via `full_update`, pass to `orchestrator.plan`. Rerender inherits original group's tier when payload omits it. Tier surfaces on list + detail response DTOs.
- [`serverV2/core/models.py`](serverV2/core/models.py) + 3 allocator strategies — `PlannedTask.price_per_hour` field; copied from the target by `_target_to_task` in Default / FastRender / Economy. (Required by the Phase 9 cost preview to build `MixSlot`s without re-looking-up prices.)

**Tier-to-budget mapping** (in `RenderLifecycle._tier_budget`):
- **ECONOMY** → no budget cap (cost-first selection is its own ceiling).
- **STANDARD** → 1.0× single-A6000-equivalent cost, derived from `cost_analyzer.estimate_cost_for_mix(heaviness, [a6000_slot])`. With Phase 7's `BUDGET_CAP_MULTIPLIER=1.5` layered on top, total headroom is ~1.5× single-A6000-cost.
- **PREMIUM** → no budget cap (logged warning + falls through to Standard's strategy selection).

**Behavior change:** **Standard tier renders are now cost-aware in production.** Lifecycle passes `tier_budget_usd` and `heaviness` to FastRender, activating Phase 7's soft cap. Economy tier is now a real user-selectable option.

---

## Phase 9 — Cost preview API

**What:** Honest dry-run cost estimate per implemented tier, surfaced on the submit page so users can pick informed.

**Files:**
- **NEW** [`serverV2/services/render_groups/cost_preview.py`](serverV2/services/render_groups/cost_preview.py) — `estimate(orchestrator, group_repo, group_id)` does a dry-run `orchestrator.plan(...)` per tier and feeds the result through `cost_analyzer.estimate_cost_for_mix`. No DB writes.
- [`serverV2/services/render_groups/service.py`](serverV2/services/render_groups/service.py) — `RenderGroupService.estimate_cost(group_id, user_id)` wraps `cost_preview.estimate` with auth check.
- [`serverV2/api/routers/render_groups.py`](serverV2/api/routers/render_groups.py) — `POST /render-groups/{group_id}/estimate` route.
- [`desktop/src/services/api.js`](desktop/src/services/api.js) — `confirmDistributedJob(...)` accepts `tier`; new `estimateRenderGroup(baseUrl, groupId)` API call.
- [`desktop/src/pages/CreateRenderPage.jsx`](desktop/src/pages/CreateRenderPage.jsx) — `useState` for `tier` + `tierEstimate`. `useEffect` fetches estimate after `groupId` lands in CONFIGURING stage. Tier picker (3 radios — Premium disabled) above the Start button with live `$X-$Y` range + ETA per tier.
- [`desktop/src/App.css`](desktop/src/App.css) — `.cr-tier-picker` + `.cr-tier-option` styles.

**Response shape:**
```json
{
  "group_id": "...",
  "tiers": {
    "economy":  { "wall_time_seconds": 720, "cost_low_usd": 0.12, "cost_mid_usd": 0.16, "cost_high_usd": 0.20, "machines": 4 },
    "standard": { "wall_time_seconds": 240, "cost_low_usd": 0.45, "cost_mid_usd": 0.60, "cost_high_usd": 0.75, "machines": 8 },
    "premium":  null
  }
}
```

**Honesty:** the preview uses the same `orchestrator.plan(...)` code path the real submit will use, so the picked mix and the cost estimate match what production will dispatch. No separate calculation that could drift.

**Frontend behavior:** on `groupId` becoming truthy in CONFIGURING stage, fires one estimate call. Doesn't repeatedly poll — refetch happens only on stage transitions or component re-mount. (Could be extended to refetch on frame-range edits if precision matters there.)

---

## Future work / deferred

- **Premium allocator** — UI slot reserved, allocator not built. When implemented: `PremiumAllocationStrategy` picks the all-fastest mix cost-blind, no soft cap, `_tier_budget` returns `None`. Wire into `_pick_strategy` and `is_implemented()`.
- **Calibration tooling against `render_telemetry`** — the data is being collected. Future work: a script (or Cloud Run job) that aggregates by `(fleet, gpu_type, heaviness band)`, fits residuals against the time-analyzer's BASELINE_SEC + factor exponents, and emits a recommended retune.
- **Stamp `seconds_estimated` / `cost_estimated_usd` on telemetry rows** — `cost_preview.estimate` could be called from the dispatcher to compute predictions and pass them to `record_chunk(...)`. Then telemetry has predicted-vs-actual ratios per (fleet, gpu_type, heaviness), which is the canonical calibration target.
- **Per-user budget overrides** — a user-set absolute dollar cap that overrides the tier-derived budget.
- **Tier badge in the My Jobs list** — backend already returns `tier` on every group; frontend doesn't render it yet.
- **Cost-preview caching** — small enough to recompute on demand. If we ever serve thousands of estimate calls per minute we can cache by `(group_id, frame_range_hash)`.

---

## Commits on this branch

```
cf4823e  Add cost-aware allocation foundations (Phases 1-2)
48eb6e4  Add cost-aware allocation backend (Phases 3-7)
e0e734f  Consolidate file_size_bytes into the heaviness dict
a35efe7  Wire tier selection + cost preview end-to-end (Phases 8-9)
```

## Verification commands

```bash
# Smoke tests (run from project root with serverV2's psycopg2 venv)
python -m serverV2.orchestrator.allocation.analyzers.time_analyzer
python -m serverV2.orchestrator.allocation.analyzers.cost_analyzer
python -m serverV2.orchestrator.allocation.economy_allocation_strategy
python -m serverV2.orchestrator.allocation.fast_render_allocation_strategy

# After deploy:
# 1. Submit a render via the rebuilt desktop app -> tier picker shows three options
# 2. Pick Economy -> SELECT tier FROM render_groups ORDER BY submitted_at DESC LIMIT 1; -> 'economy'
# 3. Server log: 'plan: tier=economy strategy=EconomyAllocationStrategy ...'
# 4. After completion: SELECT * FROM render_telemetry ORDER BY created_at DESC LIMIT 1;
#    has cost_actual_usd computed from real seconds_total * price_per_hour.
```
