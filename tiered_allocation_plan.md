# Tiered Allocation Plan

Cost-aware frame allocation across community / vast / modal fleets, with three user-visible tiers (**Economy** / **Standard** / **Premium**).

This file is the durable plan — refer back to it when the chat gets long.

---

## Goal

Today the allocator picks targets purely by speed (`render_speed`) and capacity (`fleet_max_parallel`). We want to also account for **cost** (`price_per_hour`) so users can pick a tier that fits their budget vs. wall-time preference.

| Tier | Optimization | Rough behaviour |
|---|---|---|
| **Economy** (new) | Cost first, speed second | Few machines (cap 6), big chunks, cheapest fleets preferred. Slow but cheap. |
| **Standard** (today's `FastRenderAllocationStrategy`, modified) | Speed first, cost as tie-breaker | Up to 12 machines, smaller chunks, soft budget cap. |
| **Premium** (future) | Speed only, cost-blind | All-fastest mix, no cost ceiling. **Out of scope for this plan.** |

---

## Decisions taken (from chat)

| # | Question | Decision |
|---|---|---|
| 1 | Community machine pricing | **Flat $1.00/hr**, loaded from `config.json` (`community.price_per_hour`). No per-machine override yet. |
| 2 | Tier name for modified Fast | **Standard**. Internal class name stays `FastRenderAllocationStrategy`. Hard-cap stays 12 machines. |
| 3 | Economy max-machines cap | **6** |
| 4 | Anti-burden rule | **Soft** — prefer to spread across enabled fleets, but cheapest still wins. No strict floor. |
| 5 | Empirical calibration | **Yes — store actual seconds-per-frame per (chunk_size, fleet, machine) tuple** for future cost-time tuning. JSON snapshot OK. |
| 6 | Budget cap behavior on Standard | **Soft** — drop most expensive targets and re-estimate; never hard-fail with "over budget". |
| 7 | Cost estimate format to user | **Range** (e.g. "$2.40 — $3.80"). |

---

## Naming

| Concept | Internal class | User-visible label |
|---|---|---|
| New cheap-first allocator | `EconomyAllocationStrategy` | Economy |
| Existing speed-first allocator (modified) | `FastRenderAllocationStrategy` (unchanged) | Standard |
| Future cost-blind allocator | `PremiumAllocationStrategy` (TBD) | Premium |
| Today's tiny-render fallback | `DefaultAllocationStrategy` (unchanged) | (internal — backstop for renders that don't merit tier logic) |

---

## Phases

Each phase is independently mergeable. Plan → approve → execute, one at a time.

### Phase 1 — Pricing data on config (foundation, no behavior change) — **next**

Add `price_per_hour` to every fleet capability and community machine. Plumb through config → endpoints → `FleetCapability` / `CommunityMachine`. Allocators don't read it yet.

**Files:** `config.json`, `config.py`, `core/models.py`, `bootstrap.py`, possibly `machine_repository.py`. Detailed plan in chat before executing.

### Phase 2 — Stronger blend analysis

Extend `blend_parser` to extract:
- `vertex_count_total` (visible objects in render scene)
- `samples_per_pixel`
- `resolution_pixels`
- `texture_count` + `texture_total_bytes`
- `material_count`
- Heavy-feature flags: `uses_subdivision`, `uses_displacement`, `uses_particles`, `uses_volumetrics`
- `output_format`

Stored in `render_groups.analysis_snapshot_json` (already exists, additive).

Files: `services/blend_parser/parser.py` and the underlying Blender-side analysis script.

### Phase 3 — Time estimator (pure module) — **done**

Lives at `serverV2/orchestrator/allocation/analyzers/time_analyzer.py` (new
`analyzers/` sub-package; the cost analyzer and any future analyzers will
sit here too).  Two public entry points:

- ``estimate_seconds_per_frame(heaviness, render_speed)`` — fast path,
  takes the already-defaulted heaviness dict.
- ``estimate_seconds_per_frame_from_snapshot(snapshot, render_speed)`` —
  convenience wrapper that calls ``parse_analysis_heaviness`` first.

Heuristic:
```
seconds_per_frame =
    BASELINE_SEC                          # CYCLES, 1024 samples, 1080p, simple
  * (samples / 1024)
  * (pixels / 1920*1080)
  * geometry_factor(vertex_count_total)
  * texture_factor(texture_total_bytes)
  * heavy_features_factor(...)
  / target.render_speed
```

Tunables live as module-level constants. Calibrated empirically over time using Phase 5 data.

### Phase 4 — Cost estimator (pure module) — **done**

Lives at `serverV2/orchestrator/allocation/analyzers/cost_analyzer.py`.
Composes Phase 3's time analyzer (per-frame seconds + per-chunk startup)
with target prices.  See "Phase 4 — completed" section below for the
full summary.
```
estimated_cost = sum_for_target_in_mix(
    time_per_frame * frames_assigned / 3600 * target.price_per_hour
)
```

Returns `(seconds_total, cost_usd_range)` for any candidate mix. Range comes from a confidence band around the estimator (e.g. ±25%).

### Phase 5 — Empirical telemetry

Decisions taken:
- Log per-job actual seconds per frame and inferred cost.
- Tuple key = `(chunk_size, fleet, machine_or_gpu_type)`.
- Storage: a JSON column on `jobs` (`telemetry_json`) OR a new `render_telemetry` table — decide at phase-plan time.

Required additions:
- `jobs.started_at` (when render actually began, not when dispatched) if not already present
- Recorded by the worker handler / monitor on first-frame signal

Used by Phase 3 for periodic recalibration of `BASELINE_SEC` and the factor functions. Also surfaces "your last render of similar weight cost $X" hints in the UI.

### Phase 6 — `EconomyAllocationStrategy`

New strategy realizing `FrameAllocator`. Algorithm:

```
1. Eligibles = community + serverless (validators applied; VRAM floor from heaviness band)
2. Sort by price_per_hour ASC
3. Soft fleet diversification: prefer mixing fleets but allow up to ~70% from one
   if cost dominates. NO strict per-fleet floor (per decision #4).
4. Pick up to 6 cheapest distinct targets (capped at total_frames)
5. Distribute frames evenly (NOT speed-weighted — economy doesn't optimize wall time)
6. Return list of PlannedTask
```

`allocate_retry`: same approach as today (anti-affinity filter + cheapest remaining).

File: `serverV2/orchestrator/allocation/economy_allocation_strategy.py`.

### Phase 7 — Budget-aware `FastRenderAllocationStrategy` (Standard)

Edits inside the existing class:

- Score becomes `compute_power_score / sqrt(price_per_hour)` (sqrt softens cost so fast machines still win when worth it).
- Soft budget cap: if `cost_estimator(mix) > tier_budget * 1.5`, drop the most expensive target and re-estimate. Loop until within budget or only one target left.
- HARD_CAP stays 12.

### Phase 8 — Tier selection plumbing (UI + API)

- Frontend submit form: radio Economy / Standard / Premium (Premium grayed out).
- `CreateRenderGroupPayload`: new `tier` field, default `"standard"`.
- `render_groups`: new `tier TEXT` column (additive migration via `init_db()` ALTER pattern).
- `RenderLifecycle._pick_strategy`: switches on tier first; falls back to today's heaviness heuristic only when tier is missing.
- `RenderOrchestrator.plan(...)`: new `tier` param plumbed from API → service → orchestrator.

### Phase 9 — Cost preview API

`POST /render-groups/{id}/estimate?tier=economy` → returns `{ seconds: ..., cost_usd_range: [..., ...] }`. Frontend calls this for all three tiers after blend analysis completes, displays inline on the submit page so the user can pick informed.

### Future — Premium tier

Out of scope. Slot reserved.

---

## Architectural shape after all phases

```
                   ┌──────────────────────────────┐
                   │   API: /render-groups/create │
                   │   payload.tier = "economy"   │
                   └──────────────┬───────────────┘
                                  │
                                  v
                   ┌──────────────────────────────┐
                   │   RenderLifecycle.plan(tier) │
                   │   _pick_strategy_by_tier(...) │
                   └──┬───────────┬────────────┬──┘
                      │           │            │
                      v           v            v
         ┌────────────────┐  ┌──────────┐  ┌──────────┐
         │   Economy      │  │ Standard │  │ Premium  │
         │   (new)        │  │ (=Fast,  │  │ (future) │
         │                │  │  modified)│  │          │
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
                │ TimeEstimator │ ← pure
                │ CostEstimator │ ← pure
                └───────────────┘
                        ▲
                        │ feeds back periodically
                ┌───────────────┐
                │  Telemetry    │ ← actual seconds-per-frame
                │  (jobs table  │   per (chunk_size, fleet, machine)
                │   or sibling) │
                └───────────────┘
```

---

## Open questions still to resolve in later phases

- **Phase 4 confidence band** — fixed ±25% or derived from telemetry variance?
- **Phase 5 telemetry storage** — JSON column on `jobs` vs separate `render_telemetry` table?
- **Phase 7 soft-cap multiplier** — 1.5× tier budget? 2×? Tunable.
- **Phase 8 tier picker UX** — radio vs dropdown vs "smart suggested" by default?

These can wait until their phase comes up.

---

## Status

| Phase | Status |
|---|---|
| 1 — Pricing data on config | **done** |
| 2 — Stronger blend analysis | **done** |
| 3 — Time estimator | **done** |
| 4 — Cost estimator | **done** |
| 5 — Telemetry | **done** |
| 6 — EconomyAllocationStrategy | **done** |
| 7 — Budget-aware Standard | **done** |
| 8 — Tier selection plumbing | not started |
| 9 — Cost preview API | not started |

---

## Phase 1 — completed (summary)

Files changed (5):
- `serverV2/config.json` — `price_per_hour` added to all 4 vast_instances + 3 modal_instances + new top-level `community` block.
- `serverV2/config.py` — `price_per_hour` field added to `VastEndpoint` and `ModalEndpoint` dataclasses and their parsers (required, raises at boot if missing). New helper `_load_community_price_per_hour()`. New `community_price_per_hour` field on `AppConfig`, defaults to 1.0.
- `serverV2/core/models.py` — `price_per_hour` field added to `FleetCapability` (default 0.0) and `CommunityMachine` (default 1.0). `CommunityMachine.from_row` accepts `price_per_hour` kwarg.
- `serverV2/repositories/machine_repository.py` — constructor takes `community_price_per_hour`, stamps it on every `CommunityMachine` returned (3 callsites).
- `serverV2/bootstrap.py` — `MachineRepository` constructed with the loaded community price; `FleetCapability` instances built in `_resource_picker` carry `ep.price_per_hour`.

Behavior change: **none**. No allocator/dispatch/lifecycle/callback file touched. Pricing is plumbed end-to-end but no consumer reads it yet.

Initial price values (sanity-check before deploy):
- Vast: 4090 $0.45, A6000 $0.55, A5000 $0.30, 3090 $0.25
- Modal: L4 $1.10, L40S $2.10, H100 $4.50
- Community: $1.00 flat

---

## Phase 2 — completed (summary)

Files changed (2):
- `desktop/src-tauri/src/commands.rs` — `ANALYZE_BLEND_PY` constant extended with 18 heaviness fields, grouped under `payload["heaviness"]`. Each section wrapped in try/except so a Blender API mismatch in one area degrades just those fields.
- `serverV2/core/value_objects.py` — new `parse_analysis_heaviness(snapshot)` helper that reads the `heaviness` sub-dict from `analysis_snapshot` with safe defaults for every key. Cost / time estimators (Phase 3+) consume this; they never need to None-check individual fields.

The 18 heaviness fields:

**Render settings:** `render_engine`, `resolution_x`, `resolution_y`, `resolution_percentage`, `effective_pixels`, `samples` (engine-aware: Cycles or EEVEE)

**Geometry:** `vertex_count_total` (sum across visible MESH objects in active scene), `object_count`, `mesh_count`

**Assets:** `material_count`, `texture_count`, `texture_total_bytes` (estimated VRAM cost from `width × height × channels`), `shader_node_count_total`

**Heavy-feature flags:** `uses_subdivision`, `uses_displacement`, `uses_particles`, `uses_geometry_nodes`, `geometry_nodes_complexity` (sum of GN modifier node-group sizes), `uses_subsurface_scattering`, `uses_volumetrics` (world + materials shader graph walk)

Behavior change: **none** — fields land in `analysis_snapshot_json` (additive JSON column). No consumer reads them yet. Older desktop installs continue working without the new fields; `parse_analysis_heaviness` returns defaults for them.

Rebuild: desktop app needs `tauri build` to embed the updated `ANALYZE_BLEND_PY`. Backend deploy not strictly required for this phase, but `parse_analysis_heaviness` is available there for Phase 3+.

---

## Phase 3 — completed (summary)

Files added (2):
- `serverV2/orchestrator/allocation/analyzers/__init__.py` — new sub-package for pure heuristic analyzers (time, cost, future ones).
- `serverV2/orchestrator/allocation/analyzers/time_analyzer.py` — heuristic seconds-per-frame estimator.

Two public entry points: `estimate_seconds_per_frame(heaviness, render_speed)` and `estimate_seconds_per_frame_from_snapshot(snapshot, render_speed)`.

Heuristic = product of factor functions, each ≥ 1.0 above baseline:
- `_sample_factor` — Cycles linear in samples / 1024; EEVEE TAA / 64 (cheaper per-sample)
- `_pixel_factor` — linear in effective pixels / (1920×1080)
- `_geometry_factor` — sub-linear (log10): 100k verts = 1.0, 10M = ~2.0
- `_texture_factor` — mild ramp above 256 MB tex bytes, capped at 2.0
- `_shader_factor` — node-count ramp + SSS (1.4×) + volumetrics (2.0×)
- `_feature_factor` — subdivision (1.3×), displacement (1.3×), particles (1.5×), geometry-nodes (1.2-2.0× scaled by complexity)

Final: `BASELINE_SEC * multiplier / max(MIN_RENDER_SPEED, render_speed)`. All thresholds and exponents are module-level constants — easy to tune once Phase 5 telemetry exists.

Smoke test in `__main__` block exercises 16 assertions (baseline, EEVEE engine, 4× samples, 4K, 10M verts, each heavy flag, kitchen-sink, legacy snapshot, render-speed floor, monotonicity in both directions). Run via:

```
python -m serverV2.orchestrator.allocation.analyzers.time_analyzer
```

Behavior change: **none** — pure module, no consumer imports it yet. Wired up by Phase 4 (cost analyzer).

---

## Phase 4 — completed (summary)

Files added/modified (2):
- `serverV2/orchestrator/allocation/analyzers/cost_analyzer.py` — **NEW**.  Exposes `MixSlot` (target's share of a candidate mix), `CostEstimate` (wall-time + cost-mid + cost-low + cost-high), and two public functions: `estimate_cost_for_mix(heaviness, mix, *, file_size_bytes, confidence_band)` and `estimate_cost_from_snapshot(snapshot, mix, *, file_size_bytes, confidence_band)`.
- `serverV2/orchestrator/allocation/analyzers/time_analyzer.py` — **MODIFIED**.  Added `estimate_startup_seconds(heaviness, file_size_bytes)` and a snapshot wrapper.  New tunables: `BASELINE_STARTUP_SEC=90`, `DOWNLOAD_SEC_PER_GB=30`, `BVH_SEC_PER_MILLION_VERTS=3`, `TEX_UPLOAD_SEC_PER_GB=100`, `SHADER_COMPILE_SEC_BASE=5`, `SHADER_COMPILE_SEC_PER_NODE=0.05`, `MAX_STARTUP_SEC=1800`.

Cost model:

```
For each MixSlot (one chunk per slot):
    spf      = estimate_seconds_per_frame(heaviness, slot.render_speed)
    startup  = estimate_startup_seconds(heaviness, file_size_bytes)
    seconds  = startup + spf * slot.frames_assigned
    cost     = seconds / 3600 * slot.price_per_hour

wall_time = max(seconds across slots)        — chunks run in parallel
cost_mid  = sum(cost across slots)            — every chunk's machine bills
cost_low  = cost_mid * (1 - band)             — band defaults to 0.25
cost_high = cost_mid * (1 + band)              — band clamped to [0, 0.95]
```

Startup heuristic calibrated against the user's 5-20 minute observed range for heavy files:

| Scene profile | Resulting startup |
|---|---|
| Tiny (default cube, 0 file size) | 90s |
| Moderate (1 GB, 10M verts, 500 MB tex, 200 nodes) | ~3.6 min |
| Heavy (5 GB, 50M verts, 2 GB tex, 500 nodes) | ~10.3 min |
| Very heavy (10 GB, 100M verts, 4 GB tex, 1000 nodes) | ~19.1 min |
| Pathological inputs | capped at MAX_STARTUP_SEC = 30 min |

Smoke tests (`time_analyzer`: 22 assertions including 7 startup-specific; `cost_analyzer`: 15 assertions covering empty mix, single-slot arithmetic, parallel wall-time, mismatched speeds, unbalanced frames, confidence-band clamping, scene heaviness propagation, price scaling, file-size effect, snapshot wrapper, frame-count amortization, zero-frame skip, negative-price clamp).

Run:
```
python -m serverV2.orchestrator.allocation.analyzers.time_analyzer
python -m serverV2.orchestrator.allocation.analyzers.cost_analyzer
```

Behavior change: **none** — both pure modules, no consumer imports them yet.  Wired up by Phase 6 (Economy strategy) and Phase 7 (budget-aware Standard).

---

## Phase 5 — completed (summary)

Files added/modified (9):
- `serverV2/repositories/telemetry_repository.py` — **NEW**.  `TelemetryRepository.record_chunk(...)` writes one row per successful chunk.  Phase 5 v1 is write-only; read methods come with calibration tooling later.
- `serverV2/infrastructure/db.py` — **MODIFIED**.  Phase-5 migrations: `jobs.started_at` (TIMESTAMPTZ), `jobs.price_per_hour_at_dispatch` (NUMERIC), new `render_telemetry` table + 2 indexes (`fleet, gpu_type`; `group_id`).  All `IF NOT EXISTS` / idempotent.
- `serverV2/core/models.py` — **MODIFIED**.  `CreateJobParams.price_per_hour_at_dispatch: float | None = None` added.
- `serverV2/repositories/job_repository.py` — **MODIFIED**.  `create()` SQL persists the new column.  New `mark_started(job_id)` method — idempotent NULL check, only the first call wins.
- `serverV2/callbacks/router.py` — **MODIFIED**.  In the PROGRESS branch where the job flips pending→running, also calls `job_repo.mark_started(job_id)`.
- `serverV2/fleets/modal/strategy.py` — **MODIFIED**.  Looks up `price_per_hour` from `self._cfg.endpoints` matching `task.gpu_type`, passes to `CreateJobParams`.
- `serverV2/fleets/vast/strategy.py` — **MODIFIED**.  Same pattern, matching `ep.gpu_name`.
- `serverV2/callbacks/success_handler.py` — **MODIFIED**.  After `mark_done`, writes a telemetry row.  Skipped silently for jobs missing `price_per_hour_at_dispatch` (community in v1, all legacy jobs) or `started_at` (defensive).  Heaviness pulled from `render_groups.analysis_snapshot_json["heaviness"]`; `file_size_bytes` from `render_groups.r2_input_size_bytes`.  Telemetry write wrapped in try/except — never breaks the success path.
- `serverV2/bootstrap.py` — **MODIFIED**.  Instantiates `TelemetryRepository`, passes it (plus the existing `group_repo`) into `SuccessHandler`.

What gets logged per successful chunk:
- Tuple key: `(fleet, gpu_type, machine_id, chunk_size)`
- Time: `started_at` (first PROGRESS event), `completed_at` (mark_done), `seconds_total` (denormalized)
- Cost: `price_per_hour` (snapshot from job), `cost_actual_usd` (computed)
- Heaviness: full `heaviness_json` (JSONB) from the group's analysis snapshot
- File size: `file_size_bytes` from the group's `r2_input_size_bytes`
- Predictions (`seconds_estimated`, `cost_estimated_usd`): NULL until Phase 6/7 wires the cost analyzer into dispatch.

What's intentionally NOT logged:
- Cancelled chunks (data unreliable)
- Failed chunks (data unreliable; v1 scope per decision #2)
- Community jobs (no `price_per_hour_at_dispatch` stamp — defensive null check skips them)
- Legacy jobs from before Phase 5 (same skip path)

Verification after deploy:
1. `init_db()` runs migrations idempotently.  Sanity: `\d render_telemetry` shows the table; `\d jobs | grep started_at` shows the column.
2. Submit a fresh render to Vast or Modal.  After completion, `SELECT * FROM render_telemetry ORDER BY created_at DESC LIMIT 1;` shows one row per completed chunk with non-null `seconds_total`, `price_per_hour`, `cost_actual_usd`, `started_at < completed_at`.
3. `SELECT fleet, gpu_type, COUNT(*) FROM render_telemetry GROUP BY 1, 2;` after a few renders to confirm coverage.
4. Cancel a render mid-flight: no telemetry rows for the cancelled chunks.
5. Cause a chunk to fail (and not retry): no telemetry row.

Behavior change: telemetry inserts add ~one cross-Pacific round-trip per chunk completion to the SuccessHandler — synchronous but wrapped in try/except so failures don't break the success path.  Bounded latency.

Forward-compat: Phase 6/7 will populate `seconds_estimated` and `cost_estimated_usd` once the cost analyzer is wired into the dispatch path.  No further schema changes needed.

---

## Phase 6 — completed (summary)

Files added/modified (3):
- `serverV2/orchestrator/allocation/heaviness_bands.py` — **NEW**.  Extracted from FastRender; shared lookup table mapping file size to `(vram_floor_gb, frames_per_machine)`.  Public surface: `band_for(...)`, `vram_floor_for(...)`, `frames_per_machine_for(...)`.  Same bands as before — no behavior change.
- `serverV2/orchestrator/allocation/fast_render_allocation_strategy.py` — **MODIFIED**.  Imports `band_for` from the shared module; deleted the inline `_HEAVINESS_BANDS` table and `_band_for` function.  Same semantics.
- `serverV2/orchestrator/allocation/economy_allocation_strategy.py` — **NEW**.  Full `EconomyAllocationStrategy` realizing `FrameAllocator`.

Algorithm (allocate_initial):
1. Build eligibles (community machines + ``headroom`` virtual slots per serverless capability), filter by validators + VRAM floor; fall back to no floor if needed.
2. Sort cheapest-first (`price_per_hour` ASC; `render_speed` DESC as tiebreaker — free wall-time win for same-priced targets).
3. Soft fleet diversification: cap of `floor(ECONOMY_HARD_CAP * 0.70) = 4` per fleet; rejected targets backfill if the cap leaves us short.
4. Trim by `min(ECONOMY_HARD_CAP=6, total_frames // ECONOMY_MIN_FRAMES_PER_CHUNK=3)` — fewer-bigger-chunks is the Economy philosophy.
5. Distribute frames evenly via the in-module `_distribute_evenly` helper (NOT speed-weighted; preserves cheapest-first order).

Algorithm (allocate_retry): cheapest-first single target after anti-affinity exclusion; tiebreaker by speed; same VRAM-floor logic as initial.

13 smoke-test assertions pass:
- Cheapest-first across mixed fleets
- Cap at 6 distinct targets
- Min-frames-per-chunk guard (7 frames -> 2 chunks of [4,3])
- Diversification cap (4 vast + 2 modal)
- Backfill when one fleet dominates (6 vast, soft cap degrades)
- VRAM-floor filtering and fallback
- Even frame distribution with residual (100/6 -> [17,17,17,17,16,16])
- Render-speed tiebreaker for same-priced targets
- `allocate_retry` cheapest-after-exclusion
- `allocate_retry` returns None when all excluded
- EEVEE engine filters out `modal_serverless`
- Fleet capacity respected (`fleet_max_parallel - in_flight`)

Run via `python -m serverV2.orchestrator.allocation.economy_allocation_strategy`.

What's NOT done:
- Wiring into `RenderLifecycle._pick_strategy` — Phase 8 (tier selection).
- Cost-analyzer-aware ranking (use `cost_analyzer.estimate_cost_for_mix` as tiebreaker) — could be added to the picker later if telemetry shows raw `price_per_hour` ranking is too crude.
- Premium tier — out of scope.

Behavior change: **none** — class is created and importable but no live code path uses it yet.

---

## Phase 7 — completed (summary)

Files modified (4):
- `serverV2/orchestrator/allocation/fast_render_allocation_strategy.py` — added `PRICE_SOFTENING_EXPONENT=0.5`, `BUDGET_CAP_MULTIPLIER=1.5`, `_MIN_PRICE_FOR_SCORE=0.001` constants; new `_value_score(target)` helper; `_select_mix` and `allocate_retry` now sort/pick by `_value_score`; `allocate_initial` accepts new `tier_budget_usd` + `heaviness` kwargs; new `_apply_soft_budget_cap` method runs after distribution and drops the most expensive target while cost exceeds `tier_budget * 1.5`; smoke test in `__main__`.
- `serverV2/orchestrator/allocation/frame_allocator.py` — Protocol signature gains `tier_budget_usd: float | None = None` and `heaviness: dict | None = None`.
- `serverV2/orchestrator/allocation/default_allocation_strategy.py` — accepts (and ignores) the new kwargs.
- `serverV2/orchestrator/allocation/economy_allocation_strategy.py` — accepts (and ignores) the new kwargs.

Value-score effect on the current fleet (today's prices and render_speeds):

| Target | speed | $/hr | value (`speed / sqrt($)`) | Old rank | New rank |
|---|---|---|---|---|---|
| Vast A6000 | 1.30 | $0.55 | 186.51 | (4th) | **1st** |
| Vast 4090 | 1.45 | $0.45 | 126.23 | (2nd) | 2nd |
| Modal L40S | 1.70 | $2.10 | 124.82 | (1st) | 3rd |
| Modal H100 | 1.10 | $4.50 | 88.36  | (3rd) | last |

(Scores include `compute_power_score`'s VRAM term — A6000's 48 GB beats 4090's 24 GB on geometry, which dominates here.  L40S falling behind A6000/4090 fixes the H100/L40S-over-cheaper-Vast bug we observed.)

Soft-budget-cap algorithm:
```
threshold = tier_budget_usd * 1.5
while len(shares) > 1:
    cost_mid = estimate_cost_for_mix(heaviness, mix_slots, file_size_bytes)
    if cost_mid <= threshold: stop
    drop the share with highest price_per_hour
    re-distribute frames across remaining targets
```
Bounded at most `HARD_CAP - 1` iterations.

10 smoke-test assertions pass:
- Value score ranking (Vast > Modal as expected; L40S beats H100)
- `_select_mix` picks all-Vast over Modal
- Retry picks next-best by value after exclusion
- `tier_budget=None` is no-op (legacy compat)
- `heaviness=None` is no-op (defensive)
- Soft cap drops H100 under tight $0.30 budget
- Pathologically tiny budget terminates at >=1 target
- Generous budget keeps the full mix
- HARD_CAP unchanged at 12

Run via `python -m serverV2.orchestrator.allocation.fast_render_allocation_strategy`.

What's NOT done:
- `RenderLifecycle.plan` doesn't pass `tier_budget_usd` or `heaviness` yet — Phase 8.
- Tier-to-budget mapping (Economy = $X, Standard = $Y) — Phase 8.
- Frame distribution within a value-selected mix still uses raw `compute_power_score` via `distribute_frames`.  Could be value-weighted later if telemetry shows the mismatch matters.

Behavior change: **none in the live path** — lifecycle still passes `tier_budget_usd=None`, so the soft cap is a no-op.  The score change is unconditional but ranking improvements are strict (no regressions for cases where speed-only and value rank agree, and corrections in cases like H100/L40S where they disagreed).
