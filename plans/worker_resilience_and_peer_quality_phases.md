# Allocation Refactor — Per-Offer Ranking, No Cost, Single Strategy

Replaces the prior "Worker Resilience + Peer Quality" plan. The OPTIX→CUDA
fallback (formerly Stage A) was already implemented in shell — see the
`log_should_fallback_device` predicates in `vast_worker/scripts/render.sh`,
`render.eevee.sh`, and `community_worker/render.sh`. The OS / CUDA peer-quality
concept (formerly Stage D) is now broader: a full allocation pipeline rewrite.

The original Stage B (410 Gone on terminal jobs) and Stage C (worker
self-terminate trio) for the zombie restart loop are NOT in this plan and
remain deferred — see "Deferred" at the bottom.

Last updated: 2026-05-05.

---

## Why

Current allocation:
- One `FleetCapability` per Vast `gpu_type`, hardcoded `price_per_hour` in `config.json`
- Three tier strategies (Economy / Standard / Premium) with different `speed_weight`/`cost_weight`
- Composite scorer = `speed_w × speed_factor + cost_w × cost_factor`

Production pain:
1. **Hardcoded Vast prices are fictional.** Vast is a marketplace; real `dph_total` per offer differs from config. Cost telemetry is wrong.
2. **No driver-quality signal.** RTX A5000 hosts skew toward old NVIDIA drivers (525.x / 535.x), causing repeated OPTIX kernel compile failures. Planner picks them anyway because they score well on cost.
3. **No OS signal.** EEVEE on Windows Vast peers fails silently (broken EGL surface init in headless mode).
4. **Tier-as-strategy is the wrong axis.** Cost-weighted scoring forces Economy renders to pick cheap-flaky GPUs. Cost belongs on the *dispatch* axis (queue priority), not the *allocation* axis (what to pick).
5. **Per-gpu_type aggregation hides individual offer quality.** Two RTX 4090 offers can have very different drivers; today the planner sees them as identical.

The fix: **rank every live Vast offer individually**. Drop cost from scoring. Collapse three strategies to one. Cost re-enters at the dispatch queue (Phase F, future).

---

## Phase A — Per-offer Vast capabilities (data layer)

**Goal:** every live Vast offer becomes its own `FleetCapability`, carrying its own `dph_total`, `cuda_version`, `host_os`, and `offer_id` for direct dispatch.

**Files:**
- `serverV2/fleets/vast/client.py` — `VastOfferSearcher.search()` returns `list[VastOffer]`. Each `VastOffer` parses from a Vast bundle entry:
  - `id` → `offer_id`
  - `dph_total` → marketplace price USD/hr
  - `cuda_max_good` → driver-supported CUDA version (e.g. `13.0`)
  - `driver_version` → NVIDIA driver string (e.g. `"580.126.09"`) — captured for telemetry; not used in scoring (CUDA version is the higher-signal proxy)
  - `os_version` → normalised to `"Linux <version>"`. Vast hosts are virtually all Linux (especially under `secure_cloud_only=true` — datacenter only), so we treat any non-null `os_version` as Linux. The string format keeps downstream `"linux" in host_os.lower()` matching honest. Verified against live `/bundles/` 2026-05-05.
- `serverV2/fleets/fleet_availability/steps/vast_availability_builder.py` — emits **one `FleetCapability` per offer** (was one per `gpu_type`). Per-gpu_type metadata (vram_gb, render_speed, cpu/ram) still comes from `config.json`'s lookup; per-offer fields (cuda, os, price) come from `VastOffer`.
- `serverV2/core/models.py` — `FleetCapability` adds:
  - `host_os: str | None`
  - `cuda_version: str | None`
  - `offer_id: int | None` (Vast-only)
  - `price_per_hour: float` becomes per-offer for Vast (Modal/community keep config-derived value)
- `serverV2/fleets/vast/strategy.py` — at dispatch:
  - Use the picked capability's `offer_id` directly; no second offer search
  - Stamp `price_per_hour_at_dispatch` from the capability's actual `dph_total`
- `serverV2/fleets/fleet_availability/steps/modal_availability_builder.py` — **skipped.** Modal is fully managed, every container is identical, drivers / OS are controlled by us. Leave `cuda_version=None` and `host_os=None` on Modal capabilities and let the scorer's "None means trust" rule give Modal full credit.
- `serverV2/services/machines/machine_repository.py` — `CommunityMachine` already gets `cuda_version=None, host_os=None` via FleetCapability defaults. Agent self-report is a follow-up if we ever want to discriminate within the community pool.

**No scoring changes in Phase A.** Existing scorer keeps working with the new shape because its API is per-target.

**Smoke check:** boot orchestrator, hit fleet-availability endpoint, confirm Vast targets list shows individual offers with non-null `cuda_version` and `dph_total`.

---

## Phase B — Single strategy, cost stripped, knapsack-aware fan-out

**Goal:** one strategy, no cost in scoring, intelligent K (chunk count) selection. **`max_targets` is an upper bound, not a goal.**

### Strategy collapse

- **Delete**: `economy_allocation_strategy.py`, `standard_allocation_strategy.py`, `premium_allocation_strategy.py`
- **Delete**: `allocation_helpers/allocation_strategy_selector.py`
- `allocation_weights.py` — collapse to a single `DEFAULT` bundle. Drop `speed_weight`/`cost_weight`. Fields:
  - `max_targets: int = 60` — UPPER BOUND
  - `min_frames_per_chunk: int = 4`
  - `gpu_type_diversification_cap: float = 0.40` — prevent failure-correlated concentration
  - `vram_safety_factor: float = 1.20`
  - `startup_amortization_ratio: float = 0.5` — NEW. Knapsack knob.
- `allocation_facade.py` — drop strategy selector; one `AllocationStrategy` always.
- `tier` column on render rows stays. Allocator ignores it for now. Phase F revives it as queue priority.

### Knapsack-aware fan-out (THE CRITICAL PIECE)

`max_targets=60` does NOT mean every render uses 60 GPUs. For each render the planner computes K based on amortizing per-chunk startup over real render work:

```
total_render_seconds  = total_frames × representative_spf
K_max_amortized       = total_render_seconds / (startup_seconds × ratio)
                      # ratio defaults to 0.5 → render must be ≥ 50% of startup
K = min(max_targets, K_max_amortized, total_frames // min_frames_per_chunk)
K = max(1, K)
```

Where:
- `representative_spf` = seconds per frame on the median-speed available capability
- `startup_seconds` = `estimate_startup_seconds(heaviness, fleet="vast")` — worst-case fleet, since allocation is fleet-agnostic at K-decision time
- `ratio = 0.5` means per-chunk render time must be at least half the startup; lower ratio → more fragmentation allowed → faster wall time at cost of more startup tax

Concrete cases (assuming Vast startup ≈ 180s after Phase D):

| Render | spf (s) | total_render (s) | K_max_amortized | K (after caps) |
|---|---|---|---|---|
| 5 frames, light scene | 5 | 25 | 0.28 | **1** |
| 50 frames, light scene | 5 | 250 | 2.78 | **2** |
| 50 frames, medium scene | 10 | 500 | 5.56 | **5** |
| 50 frames, heavy scene | 60 | 3000 | 33.3 | **12** (cap by `frames/min_frames`) |
| 200 frames, medium scene | 10 | 2000 | 22.2 | **22** |
| 500 frames, medium scene | 10 | 5000 | 55.6 | **55** |
| 1000 frames, medium scene | 10 | 10000 | 111 | **60** (cap by `max_targets`) |

The 50-frame case lands at K=2..12 depending on scene heaviness — far below the 60 cap. That is the intended behaviour: no over-fan for small renders, no startup tax inflation.

### Scorer rewrite

`composite_scorer` becomes:

```
score = speed_weight × speed_factor
      + cuda_weight  × cuda_factor    # Phase C populates the data
      + os_weight    × os_factor      # Phase C populates the data
```

Default weights: `speed=0.70, cuda=0.20, os=0.10`. Tunable.

`chunk_cost_for()` stays — telemetry still records per-chunk cost — but it is no longer a scoring factor.

### Smoke tests for Phase B

Standalone script: `scripts/smoke_allocation_strategy.py`. Builds synthetic scenes + capability lists, runs the planner, prints K + picks, asserts expected behaviour. Each scenario is a single function with named inputs so failures point to the rule that broke.

Scenarios:

1. **Tiny render** — 5 frames, light scene → expect K=1
2. **Short light** — 50 frames, simple scene → expect K ≈ 2-5
3. **Short heavy** — 50 frames, heavy scene (50M verts, 4096 samples, volumetrics) → expect K capped at `total_frames / min_frames_per_chunk` = 12
4. **Medium** — 200 frames, medium scene → expect K ≈ 15-25
5. **Big** — 1000 frames, medium scene → expect K = 60 (max_targets cap)
6. **CUDA discrimination** — 50 frames, two RTX 4090 offers identical except CUDA 11.8 vs 12.8 → expect 12.8 wins
7. **OS discrimination (EEVEE)** — 50 frames EEVEE, two L40 offers identical except Linux vs Windows → expect Linux wins by clear margin (Phase C makes the EEVEE penalty harsh)
8. **Diversification cap** — 50 frames, available pool has 10 RTX 4090 with great scores + 5 of other classes; with cap=0.40 and K=12, expect ≤5 RTX 4090 picks

Run on every Phase B/C/D code change. Failures = real algorithm bugs caught pre-deploy.

---

## Phase C — CUDA + OS scoring (the ranking axes)

**Goal:** populate `cuda_factor` and `os_factor` from Phase B's scorer with real driver / OS quality logic.

**"None means trust" rule:** absence of a CUDA / OS signal means full credit (1.0), not neutral (0.5). Penalties only kick in when we have data showing the offer is bad. This way Modal and community (always None) score on speed alone, and only Vast offers that actually report old drivers / Windows get marked down.

### `cuda_factor`

```python
def cuda_factor(cuda_version: str | None) -> float:
    if cuda_version is None:
        return 1.0      # no signal = trust (Modal/community)
    try:
        v = float(cuda_version)
    except ValueError:
        return 1.0
    return max(0.0, min(1.0, v - 12.0))   # 12.0 → 0.0, 13.0 → 1.0
```

CUDA <12.0 = old Hopper-era drivers, OPTIX-failure-prone. CUDA 12.8+ = current.

### `os_factor`

```python
def os_factor(host_os: str | None, engine: str | None) -> float:
    if host_os is None:
        return 1.0      # no signal = trust (Modal/community)
    s = host_os.lower()
    is_linux = "linux" in s or "ubuntu" in s or "debian" in s
    if engine in {"BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"}:
        return 1.0 if is_linux else 0.0   # hard penalty for Windows on EEVEE
    return 1.0 if is_linux else 0.7        # soft penalty otherwise
```

Subsumes the old "Stage E" EEVEE-Linux-only filter — penalty is harsh enough that a Windows EEVEE offer's combined score loses even with good speed/cuda.

### Optional hard validator (defer)

If the soft EEVEE+Windows penalty is insufficient in practice (worst case: no Linux offers, planner falls back to Windows + black frames), add `EeveeLinuxOnlyValidator` that excludes non-Linux Vast capabilities for EEVEE renders. Ship soft-only, observe telemetry, harden if needed.

---

## Phase D — Vast load-up time buffer

**Goal:** model Vast provisioning latency in `estimate_startup_seconds`. Today's estimate counts download + BVH + shader compile (~20-90s); Vast adds another 1-3 min for offer-accepted-to-container-running. Without it, short Vast chunks score better than reality.

**Files:**
- `serverV2/allocation/allocation_strategies/analyzers/allocation_time_analyzer.py` — `estimate_startup_seconds(heaviness, fleet=None)` gains a `fleet` kwarg, adds a fleet additive on top of the heaviness-based baseline:
  - vast: +180s, modal: +120s, community: +0s
  - Buffer values come from config, not constants
- `serverV2/config.json` — new block:
  ```json
  "startup_buffer_sec": { "vast": 180, "modal": 120, "community": 0 }
  ```
- `serverV2/config.py` — `StartupBufferConfig` dataclass loaded from that block. No env vars (per house rule).

**Smoke check:** with two identical capabilities except `fleet`, score them on a 5-frame render. Vast scores lower because the unamortized startup buffer dominates.

---

## Phase E — Config cleanup

**Goal:** stop carrying hardcoded Vast prices, drop GPUs that don't pull their weight, fix the RTX 4080 SUPER VRAM typo, add modern GPUs Vast offers.

**Files:**
- `serverV2/config.json`:
  - **Drop** `RTX A5000` (failure-prone, old-driver Ampere workstation)
  - **Drop** `RTX A4000` (same story + 16GB VRAM is too tight)
  - **Fix** `RTX 4080S` `vram_gb` 32 → 16 (RTX 4080 SUPER is a 16GB card; current value lets the planner pick it for scenes that won't fit and OOM at runtime)
  - **Add** `RTX 5090` (32GB, Blackwell consumer — best $/perf for non-datacenter)
  - **Add** `A100 SXM` 80GB and `A100 PCIE` 80GB (VRAM-hungry-scene candidates)
  - **Add** `RTX 6000 Pro` (96GB, Blackwell workstation — newest drivers)
  - **Drop** `price_per_hour` from every `vast_instances[]` entry — Vast pricing is per-offer now (Phase A)
  - **Add** the `startup_buffer_sec` block (Phase D)
- `serverV2/config.py`:
  - `VastEndpoint.price_per_hour` removed (or kept optional/None and ignored). Modal/community price fields stay.
  - `StartupBufferConfig` added.

**Verification:** boot server with new config, hit fleet-availability, confirm A5000/A4000 are gone, RTX 5090 / A100 80GB / RTX 6000 Pro appear with `price_per_hour` populated from real Vast offers (not config). Existing render preview cards may need follow-up if the UI reads config price for tier estimates — flag during execution.

---

## Phase F — Future: tier as dispatch priority

**Out of scope for this plan.** Sketch for when it returns:

`tier` column on render rows becomes a queue priority, not a strategy selector:
- `pending_allocation_queue` gains a `priority` column derived from `render.tier`: premium=2, standard=1, economy=0
- Dequeue order: `priority DESC, queued_at ASC` — premium jumps the queue, FIFO within tier
- Allocator unchanged — same single strategy regardless of tier
- Pricing recovers economic differentiation: premium pays more because they jump the queue
- Optionally: tier-specific `startup_amortization_ratio` (premium=0.25 fans out wider for faster wall time, economy=1.0 fans out narrowest)

---

## Cross-stage notes

### Deploy ordering

A → B → C → D → E.
- A is foundational; everything else uses the new `FleetCapability` shape.
- B's smoke tests are more realistic with A's data in place.
- C plugs into B's scorer.
- D is independent but smoke tests benefit from it.
- E is the final cleanup pass; doing it earlier risks dropping GPUs before the new scoring proves itself.

### Telemetry

Grep-able log lines after the refactor:

```
[ALLOC] knapsack: total_frames=N spf=Xs startup=Ys K=Z
[ALLOC] picked: <fleet> <gpu> cuda=X.Y os=<linux|windows> price=$Z/hr offer=<id>
[ALLOC] cuda_factor=0.X os_factor=0.Y for <gpu>@<offer_id>
```

Daily greps tell us whether the new ranking axes are actually exercised or whether the score collapses to a single dimension.

---

## Deferred (not in this plan)

The original "Worker Resilience" plan included two stages still unfinished. Tracking here for traceability:

- **Old Stage B — terminal-callback rejection (410 Gone).** Heartbeat / progress / request-upload-urls return 410 if the job is in a terminal state. Worker exits clean on 410. Breaks the live-zombie feedback loop (cause: Cloud Run latency spikes orphan-flagged jobs that were rendering fine).
- **Old Stage C — worker self-terminate trio.** Worker handles 410, checks status at startup, self-destroys its Vast instance after success/failure. Breaks the Docker `--restart=always` zombie respawn cycle.

Independent of the allocation refactor. Don't conflict with anything in Phases A–F.
