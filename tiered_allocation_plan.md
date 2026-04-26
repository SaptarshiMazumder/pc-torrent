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

### Phase 3 — Time estimator (pure module)

`serverV2/orchestrator/allocation/time_estimator.py` — takes `(analysis_snapshot, target.render_speed)` → `seconds_per_frame_estimate`.

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

### Phase 4 — Cost estimator (pure module)

`serverV2/orchestrator/allocation/cost_estimator.py` — wraps Phase 3:
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
| 3 — Time estimator | not started |
| 4 — Cost estimator | not started |
| 5 — Telemetry | not started |
| 6 — EconomyAllocationStrategy | not started |
| 7 — Budget-aware Standard | not started |
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
