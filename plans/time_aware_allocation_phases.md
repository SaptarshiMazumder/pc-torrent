# Time-aware allocation across fleets

Factor each machine's stated availability window into the allocation planner so a chunk is never assigned to a target that won't be around long enough to finish it, and so targets with comfortable headroom outrank targets that barely fit.

Plan is phased so each phase is shippable on its own. Phases compound: every later phase assumes earlier ones have landed and depends on their data shape.

---

## Phase 0 — Verify Vast surfaces an availability field

Pure investigation, no code. Drives Phase 3's data model.

`VastOfferSearcher.search()` at [serverV2/fleets/vast/client.py:27-49](../serverV2/fleets/vast/client.py) hits `GET {api_base}/bundles/` and parses each entry through `VastOffer.from_bundle`. Per Vast's published bundle schema, each offer should carry either:

- `duration` — host-side commitment in seconds (rentable window), or
- `end_date` — epoch when the offer expires.

**Action**: with the existing `VAST_API_KEY`, one curl against the same filter dict `search()` builds (e.g. `gpu_name=RTX 4090`). Dump full JSON of one bundle to inspect actual field names.

**Outcomes**:
- Both fields present → Phase 3 uses `duration` directly, falls back to `end_date - now()` when null.
- Neither present → Phase 3 treats Vast availability as unlimited (None on the capability). Planner's time filter just doesn't apply to Vast targets.

No commit comes out of this phase — it informs Phase 3's implementation.

---

## Phase 1 — Data model + persistence foundation

Adds the `available_seconds` slot on every target type and the DB column behind community commitment. Nothing reads it yet; all three fleets stamp `None`. Lands first because every later phase writes into this shape.

**Files**:
- [serverV2/core/models.py](../serverV2/core/models.py) — `CommunityMachine` and `FleetCapability` each gain `available_seconds: float | None = None`.
- [serverV2/repositories/machine_repository.py](../serverV2/repositories/machine_repository.py) — `from_row` computes `available_seconds = max(0, commitment_end_at - now())` when the column is non-null, else `None`.
- New SQL migration: `ALTER TABLE machines ADD COLUMN commitment_end_at TIMESTAMP NULL`.
- [serverV2/api/routers/machines.py](../serverV2/api/routers/machines.py) — `/machines/available` response includes `available_seconds` on each fleet's entries.

**Acceptance**: server boots, migration runs, `/machines/available` returns the new key as `null` everywhere. Planner is untouched, so behaviour is identical to today.

---

## Phase 2 — Modal availability from Firestore config

Hardcoded-but-tunable. Lives in the same admin-config Firestore doc as every other Modal knob.

**Server**:
- [serverV2/config.py](../serverV2/config.py) `ModalConfig` adds `availability_sec: float` — required field from `modal` block. Boot-time validation fails loud if missing (matches `provisioning_enabled` pattern).
- `config.json` seed default gains `"availability_sec": 14400` (4h) under `modal`.
- Modal capability provider (wherever it builds `FleetCapability` for the snapshot) stamps `available_seconds = config.modal.availability_sec`.

**Desktop**:
- [desktop/src/pages/ConfigurationPage.jsx](../desktop/src/pages/ConfigurationPage.jsx) `ModalSection` — add a `NumberField label="availability_sec"` with `step={60}`. Same Firestore-backed write path as the other Modal fields.

**Acceptance**: edit the value in the ConfigurationPage UI, hit Save, the next allocation tick's `/machines/available` reflects the new number on every Modal entry.

---

## Phase 3 — Vast per-offer availability

Threads each offer's commitment window from the bundle response through to `FleetCapability`.

Depends on Phase 0's outcome:

- **If `duration` exists** ([serverV2/fleets/vast/vast_offer.py](../serverV2/fleets/vast/vast_offer.py)):
  - `VastOffer` gains `duration_sec: float | None`.
  - `from_bundle` reads `bundle.get("duration")`; if null, computes from `end_date - now()`; else `None`.
  - Vast capability provider passes `available_seconds = offer.duration_sec` to `FleetCapability`.
- **If not exposed**: this phase becomes a one-line documentation comment in `VastOffer` noting that Vast doesn't surface per-offer windows; capability stays `None`. Planner treats `None` as unlimited (see Phase 5).

**Acceptance**: `/machines/available` returns concrete numbers on Vast entries when the field is exposed; `null` consistently when it isn't.

---

## Phase 4 — Community commitment (user-driven time picker)

The user **must** explicitly choose how long they're keeping their PC available. No default, no silent extension behind their back.

### Desktop UI

The pre-flight checklist gate currently sits between "click Connect" and the actual `connectAgent` call. Insert a commitment step right before the connect call fires.

**[desktop/src/components/dashboard/ConnectButton.jsx](../desktop/src/components/dashboard/ConnectButton.jsx)** — extract the click handler so it opens a small modal/popover before `connectAgent` runs. The popover contains:

- A label: "I'll keep my PC available until…"
- Native `<input type="datetime-local">` — the picker proper. Default value: empty (forces the user to choose).
- A row of quick-preset chips below the input: `1h`, `4h`, `8h`, `24h`. Clicking a preset fills the datetime input with `now + N hours` (rounded to the next 5min for tidiness).
- Validation: chosen datetime must be > now + 15min. Otherwise the Connect button in the modal stays disabled.
- Cancel + Connect buttons.

On Connect-confirm: compute `commitment_seconds = Math.floor((chosen.getTime() - Date.now()) / 1000)` and pass it into `connectAgent(...)` alongside the existing token + backendUrl.

### Desktop agent

The Tauri agent's `connect` invocation already takes `backendUrl` and `authToken`. Add a `commitment_seconds` arg, forwarded into the POST body of `/machines/register`.

While the agent is connected, slide the window forward:
- Every `commitment_seconds * 0.4` interval (so we extend before half the window has elapsed), agent fires `PUT /machines/{id}/commitment` with the same `commitment_seconds` from the original picker. Server resets `commitment_end_at = now + commitment_seconds`.
- On graceful disconnect, agent fires `PUT /machines/{id}/commitment` with `commitment_seconds = 0` so `commitment_end_at` snaps to `now` and the planner stops considering this machine.

If the user wants to extend mid-session, they re-pick from the dashboard (re-open the picker, choose new datetime). For Phase 4, we don't build that yet — the agent's automatic extension is sufficient. Manual re-pick can come in a follow-up phase.

### Server

- [serverV2/api/schemas/machine.py](../serverV2/api/schemas/machine.py) `RegisterMachinePayload` adds `commitment_seconds: float` (required, must be > 0).
- New schema `MachineCommitmentPayload { commitment_seconds: float }`.
- [serverV2/api/routers/machines.py](../serverV2/api/routers/machines.py) adds `PUT /machines/{machine_id}/commitment` calling `MachineService.set_commitment(machine_id, commitment_seconds, user)`.
- `MachineService.register` writes `commitment_end_at = now + commitment_seconds` on row insert.
- `MachineService.set_commitment` updates the column; rejects 403 if `machine.user_id != user.uid`; `commitment_seconds == 0` snaps to `now` (effectively retires the row from planning).

### Acceptance

- Click Connect on the dashboard → modal pops up → user can't proceed without picking a future datetime.
- After connecting, `/machines/available` returns this machine with `available_seconds` close to the picked value, ticking down over time, refreshed back up at each agent extension.
- Disconnect → `available_seconds` becomes 0 on the next snapshot.

---

## Phase 5 — Planner integration

The actual time factor in [serverV2/allocation/allocation_strategies/allocation_planner.py](../serverV2/allocation/allocation_strategies/allocation_planner.py).

**Filter step** (between current Step 1 — VRAM — and Step 2 — knapsack K-decision):

For each eligible target, compute a rough `expected_chunk_seconds`:

```
spf            = estimate_seconds_per_frame(heaviness, target.render_speed)
chunk_frames   = total_frames / current_K_estimate  # use median K from initial pass
chunk_seconds  = startup_for(target) + spf * chunk_frames
```

Drop the target if `target.available_seconds is not None and chunk_seconds * weights.time_safety_factor > target.available_seconds`. `None` (Vast without `duration`, or community pre-commitment migration row) passes the filter unconditionally.

**Score factor** in [allocation_composite_scorer.py](../serverV2/allocation/allocation_strategies/analyzers/allocation_composite_scorer.py):

Add `time_headroom_factor`:

```
if target.available_seconds is None:
    headroom_factor = 1.0
else:
    ratio = target.available_seconds / max(1, chunk_seconds)
    headroom_factor = min(1.0, ratio / (1 + weights.time_headroom_falloff))
```

Multiply into the final score (alongside speed/cuda/os).

**Step 5 — time-balanced distribution** clamps each selected target's share at `floor((available_seconds - startup) / spf)` frames when `available_seconds` is finite. If a target's window is tight, frames spill over to the next-best target.

**Weights config**:
- [serverV2/allocation/allocation_strategies/allocation_weights.py](../serverV2/allocation/allocation_strategies/allocation_weights.py) gains `time_safety_factor: float` (default 1.5) and `time_headroom_falloff: float` (default 0.5).
- Both are wired into the Firestore admin-config schema and surfaced in [ConfigurationPage.jsx](../desktop/src/pages/ConfigurationPage.jsx)'s Frame Allocation → Weights section as `RatioSlider` controls.

### Acceptance

- A render whose chunks would each take ~10min, dispatched against a community machine committed for 5min, never gets that machine assigned.
- A render against a Modal capability with 4h availability scores higher than the same capability with 30min availability, all else equal.
- Tuning `time_safety_factor` from the UI changes allocation behaviour on the next planning tick without restart.

---

## Phase 6 — Surface remaining time in the UI

Last phase: read-only display polish. Doesn't change planner behaviour.

**[desktop/src/pages/AvailableMachinesPage.jsx](../desktop/src/pages/AvailableMachinesPage.jsx)** — add an "Available for" column per row, rendering `formatDuration(available_seconds)` (e.g. "3h 14m"). Null shows as "—". Community rows that have ticked down close to zero get a subtle warning style.

**Job detail view** — instance panels (Vast/Modal/Community) already show GPU + status; add the remaining-window for the underlying capability so users understand why a chunk landed where it did.

### Acceptance

- Available Machines page shows time-remaining per row, ticking down once per minute.
- Disconnecting a community PC clears the row from the listing on the next snapshot.

---

## What stays untouched

- Job detail listing logic, terminal cache, the loader component, the section split, gallery batching, every UI polish change so far.
- Allocation dispatch path — capabilities flow in, dispatches go out. Strategies aren't time-aware; only the planner's *selection* is.
- Existing weights (`speed_weight`, `cuda_weight`, `os_weight`, `vram_safety_factor`, etc.) — the time factor multiplies into the existing composite, doesn't replace anything.

## Cross-cutting principles

- **Required fields fail loud**: Modal `availability_sec` and the community `commitment_seconds` payload are both required. Missing config → boot fails. Missing field on register → 400.
- **Vast `None` is real semantic**, not a swallowed default. It means "marketplace didn't tell us." Planner respects that by skipping the filter.
- **Firestore-tunable config** per the existing pattern — every new knob (`availability_sec`, `time_safety_factor`, `time_headroom_falloff`) lives in `config/global` and is editable from ConfigurationPage. `config.json` only seeds defaults.
- **Single source of truth**: `available_seconds` is computed in exactly one place per fleet (the capability provider for Modal/Vast, the repo for community). The planner never recomputes it.

## Order of execution

Recommended pacing — one phase at a time, each lands as its own PR, no phase blocks the others except by the data-model dependency on Phase 1.

1. Phase 0 — curl + decision (no commit)
2. Phase 1 — data model + migration (server only)
3. Phase 2 — Modal config (server + small ConfigurationPage change)
4. Phase 3 — Vast offer field (server only, contingent on Phase 0)
5. Phase 4 — Community commitment (server + desktop dashboard work)
6. Phase 5 — Planner integration + weight tunables (server + small ConfigurationPage change)
7. Phase 6 — UI surfacing (desktop only)
