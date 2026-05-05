# Worker Resilience + Peer Quality — Phased Plan

Sequential plan for closing three production-pain gaps observed during
2026-05-05 renders:

1. **Vast peer host OptiX kernel compile failures** → wasted retries
2. **Container restart loops on Vast** (same job_id rendered twice
   on the same instance after the worker exited) → wasted compute
   billed to us, dashboard stays out of sync with reality
3. **Cost / time blow-ups when peer drivers are old** — manifests as
   the OptiX failure above, but root cause is "we rented a peer
   without checking driver vintage"

The fix space spans both the **worker repo** (lives outside this
codebase — wherever `render_driver.py` and the heartbeat sender are)
and the **orchestrator repo** (this codebase). Stages are ordered by
implementation order; cross-stage dependencies called out per stage.

Last updated: 2026-05-05.

---

## Stage A — OPTIX → CUDA fallback in the worker `[URGENT]`

**Where:** worker repo only. Single file: the render driver script
that Blender's `--python` flag invokes (`render_driver.py`).

**What:** when `bpy.ops.render.render(...)` fails with an OptiX kernel
compile error, switch Cycles to CUDA and retry the same render once.
CUDA is ~15–25% slower than OPTIX on the same GPU but doesn't trip
the kernel-compile bug — saves the chunk on the same Vast instance
instead of paying for a retry on a different peer.

**Triggering error patterns** (from production logs):

```
OPTIX_ERROR_INTERNAL_COMPILER_ERROR
Failed to load OptiX kernel
COMPILE ERROR: Module compilation failed
```

Match any of those in either the raised `RuntimeError`'s message OR
the captured Blender log lines from this render attempt.

**Implementation outline:**

```python
# render_driver.py (sketch)
_OPTIX_KERNEL_COMPILE_PATTERNS = (
    re.compile(r"OPTIX_ERROR_INTERNAL_COMPILER_ERROR"),
    re.compile(r"Failed to load OptiX kernel"),
    re.compile(r"COMPILE ERROR:\s+Module compilation failed"),
)

def _is_optix_kernel_compile_error(exc, recent_log_lines):
    haystack = str(exc) + "\n" + "\n".join(recent_log_lines)
    return any(p.search(haystack) for p in _OPTIX_KERNEL_COMPILE_PATTERNS)

def _activate_devices(compute_type):
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = compute_type     # "OPTIX" | "CUDA"
    prefs.get_devices()
    for device in prefs.devices:
        device.use = (device.type == compute_type)
    bpy.context.scene.cycles.device = "GPU"

def render_with_fallback(scene, *, output_path, frames, ...):
    # First attempt: OPTIX (current default)
    _activate_devices("OPTIX")
    log.info("[RENDER_DRIVER] Rendering with: OPTIX (GPU)")
    try:
        bpy.ops.render.render(animation=True, ...)
        log.info("[RENDER_DRIVER] compiled_with=optix")
        return
    except RuntimeError as exc:
        if not _is_optix_kernel_compile_error(exc, _recent_blender_logs()):
            raise
        log.warning(
            "[RENDER_DRIVER] OPTIX kernel compile failed: %s; "
            "falling back to CUDA", exc,
        )

    # Reset Blender state to clear failed Cycles init.  Reloading the
    # .blend is heavier than necessary but the cleanest way to wipe
    # OPTIX context state that lingers in the running process.
    bpy.ops.wm.revert_mainfile(use_scripts=False)
    _activate_devices("CUDA")
    log.info("[RENDER_DRIVER] Rendering with: CUDA (GPU) [fallback]")
    bpy.ops.render.render(animation=True, ...)
    log.info("[RENDER_DRIVER] compiled_with=cuda_fallback")
```

**Why this works:** OPTIX kernel compile is the JIT step where NVIDIA's
runtime compiles Blender's PTX kernel for the specific GPU + driver
combination. Old/buggy drivers fail here. CUDA path-tracing uses a
completely different kernel pipeline (no PTX JIT, pre-compiled CUDA
binaries) and is robust against the same drivers.

**Edge cases handled in the plan:**

- **Infinite retry guard.** Fall back to CUDA exactly once. If the
  CUDA retry also fails, bubble up the original exception. Do not
  attempt CPU fallback (CPU rendering on a GPU-rented instance burns
  hours of expensive compute for low return).
- **Lingering OPTIX state.** Blender holds onto failed-Cycles-init
  state in the running process. `revert_mainfile()` is the cleanest
  reset. Alternative: spawn a fresh Blender subprocess for the retry,
  but that doubles container startup cost.
- **Output path collision.** The first OPTIX attempt may have written
  partial output frames before crashing. The subsequent CUDA retry
  overwrites them. Fine — Blender frame writes are atomic per-file
  via `os.replace`-style semantics. Worst case: a stale partial PNG
  briefly exists on local disk, gets overwritten, never gets uploaded.
- **Scene context loss.** After `revert_mainfile`, `bpy.context.scene`
  is a fresh object. Re-resolve scene name + view layer + camera mode
  from the original render-overrides JSON (already in scope from the
  initial setup) before invoking the render again.

**Telemetry:**

- Log `[RENDER_DRIVER] compiled_with=optix` on first-try success.
- Log `[RENDER_DRIVER] compiled_with=cuda_fallback` after fallback
  success.
- Log `[RENDER_DRIVER] OPTIX kernel compile failed: <msg>; falling
  back to CUDA` on the trigger.

These show up in Cloud Run logs (worker stdout is captured) so we
can grep/count occurrences after deploy:

```
gcloud logging read 'textPayload=~"compiled_with=cuda_fallback"' \
  --freshness=24h --format='value(timestamp,textPayload)'
```

Tells us how often Stage A is firing, i.e., how often we'd otherwise
have lost a chunk.

**Open questions:**

- **A1.** Reload .blend via `revert_mainfile` (clean, ~1-2s) or spawn
  a fresh Blender subprocess (cleanest, ~10-30s — full Blender boot)?
  Recommend revert_mainfile.
- **A2.** Capture Blender logs via `bpy.app.handlers` hooks or via
  redirecting `sys.stderr` during the render? Recommend the simpler
  stderr redirect — Blender's logging is already on stderr in
  headless mode.
- **A3.** Should we ALSO try OPTIX-only-on-Cycles-feature-set?
  Some scenes have `cycles.feature_set = 'EXPERIMENTAL'` which uses
  experimental OptiX paths more likely to fail. Could downgrade
  to `'SUPPORTED'` for the OPTIX retry before the CUDA fallback.
  Out of scope for now; flag for later.

**Cost / risk profile:**

- ~30-50 lines of Python, single file, no architectural change.
- No orchestrator change.
- Worst case: the fallback masks a real driver issue. The telemetry
  line surfaces it; we'd add anti-affinity contribution if telemetry
  shows specific GPU classes failing OPTIX repeatedly.
- Best case: ~5-20% of Vast renders that today die on OPTIX kernel
  compile would succeed instead, on the same instance, ~25% slower.

**Dependencies:** none. Ships standalone. Worker image rebuild + Vast
template update.

**Smoke test plan:**

1. Find a Vast offer with old NVIDIA drivers (~535.x range historically
   has the most issues). Rent it manually.
2. Force-render a known-OPTIX-failing scene on it.
3. Confirm the fallback kicks in, the chunk completes via CUDA, the
   `[RENDER_DRIVER] compiled_with=cuda_fallback` line appears.
4. Tail Cloud Run logs after deploy and count `compiled_with=` entries
   per day for the first week to validate the live rate.

---

## Stage B — `[reject-terminal-callbacks]`

**Where:** orchestrator only. Two files: `services/jobs/service.py`
and `api/routers/jobs.py`.

**What:** `PUT /jobs/{id}/heartbeat`, `PUT /jobs/{id}/progress`, and
`POST /jobs/{id}/request-upload-urls` check the job's `status` column
before doing any work. If status is in `{done, failed, cancelled}`,
return HTTP **410 Gone** with a structured body:

```json
{ "error": "job_terminal", "status": "failed", "reason": "..." }
```

**Why 410:** semantic match for "the resource is no longer available."
HTTP clients that respect 410 will not retry it (vs 5xx where most
clients retry). We want zombies to STOP, not retry.

**Endpoints in scope:**

| Endpoint | Reject on terminal? | Why |
|---|---|---|
| `PUT /jobs/{id}/heartbeat` | yes | the explicit kill switch for zombies |
| `PUT /jobs/{id}/progress` | yes | same reason — wasted Redis writes |
| `POST /jobs/{id}/request-upload-urls` | yes | no point signing R2 URLs for a dead job |
| `POST /jobs/{id}/register-outputs` | **no** | frames already on R2; let them dedupe via output_frames PK |
| `PUT /jobs/{id}/status` | **no** | the worker MUST be able to report terminal status; rejecting 410 here would leak state |

**Cost per heartbeat:** one extra DB read. Current heartbeat cadence
is ~5s/worker × ~10 active workers = ~2 reads/sec. Negligible.

**Optional optimization (out of scope for now):** cache terminal flag
in Redis (`job:{id}:terminal`, 5min TTL set on terminal transition).
Heartbeat reads Redis first, falls back to PG. Skip until heartbeat
volume justifies it.

**Open questions:**

- **B1.** Do we want a structured body, or just the 410 status code?
  Structured body lets the worker log a clean reason; raw 410 is
  simpler. Recommend structured body — cheap, useful in worker logs.

**Dependencies:** none for this stage in isolation. To actually break
the live-zombie loop, also needs Stage C.1 (worker handles 410).

---

## Stage C — Worker self-terminate trio

Three coordinated worker-side changes that together break every shape
of zombie / orphan-work loop on Vast.

### C.1 — Worker handles 410 from heartbeat/progress

**Where:** worker repo. Heartbeat sender thread + progress poster.

**What:** when the heartbeat or progress HTTP response is 410, set a
process-global `should_exit` event. The render driver checks this
event between frames (or via `render_pre`/`render_post` callbacks
in Blender) and exits cleanly with code 0 if set.

```python
# heartbeat_sender.py (sketch)
SHOULD_EXIT = threading.Event()

def _send_heartbeat(...):
    resp = requests.put(...)
    if resp.status_code == 410:
        body = resp.json()
        log.warning(
            "Orchestrator says job is %s (%s); exiting",
            body.get("status"), body.get("reason"),
        )
        SHOULD_EXIT.set()
        return
    resp.raise_for_status()
```

Render driver checks `SHOULD_EXIT.is_set()` between frames; on True,
break out of the render loop, run cleanup, exit 0.

**Pairs with Stage B.** Without B, the orchestrator never returns 410.
Without C.1, B is a no-op for workers.

### C.2 — Worker checks job status at startup

**Where:** worker repo. Top of the entrypoint script, before the
.blend download.

**What:** one HTTP call: `GET /jobs/{JOB_ID}` (or `/jobs/{JOB_ID}/status`
if a lighter endpoint exists). If response shows `status` in
`{done, failed, cancelled}`, log "job already terminal, no work to do"
and exit 0 immediately — before downloading anything.

```python
# entrypoint sketch
job_status = _get_job_status(JOB_ID)
if job_status in {"done", "failed", "cancelled"}:
    log.info("Job %s already %s; exiting before download", JOB_ID, job_status)
    sys.exit(0)
```

**Cost:** one HTTP roundtrip per container start (~50ms). Container
starts a handful of times per Vast instance lifetime. Negligible.

**Why this matters:** prevents the FIRST iteration of the zombie
restart loop. After a crash, Vast respawns the container with the
same env vars. Without this check, the respawn re-runs the whole
pipeline. With this check, the respawn exits in <100ms.

### C.3 — Worker self-destroys the Vast instance

**Where:** worker repo + tiny orchestrator addition.

**Two design options:**

- **(a)** Worker calls Vast API directly. Needs `VAST_API_KEY`
  injected into the container env. Reliable, but **leaks the key** to
  anyone who can read the container env (other tenants on the same
  peer? unclear).
- **(b)** Worker calls orchestrator endpoint. New
  `POST /jobs/{job_id}/self-destroy`. Orchestrator looks up
  `vast_job_id` from the row, calls `client.instances.destroy(...)`,
  returns 204. Idempotent — destroy() on already-destroyed is a no-op.
  Adds one HTTP hop but no credential leak.

**Recommend (b).** The orchestrator-side destroy() is the same call
the existing failure pipeline uses; we're just letting the worker
trigger it explicitly after success/failure terminal reporting,
rather than waiting for the orchestrator's monitor to notice.

**What this adds beyond C.1+C.2:** stops Vast billing immediately
after success. C.1/C.2 prevent zombies from doing useless work; C.3
stops PAYING for the instance even if Vast's restart policy would
keep it idle but billable until orchestrator-side cleanup.

**Worker needs to know its `instance_id`:** inject as
`VAST_INSTANCE_ID` env var at dispatch time (Vast strategy already
has it in scope when launching).

**Dependencies:** can ship independently of C.1/C.2 but most useful
in combination.

---

## Stage D — Peer-quality enrichment + ranking `[NEW]`

**Where:** orchestrator only. Touches Vast availability fetching +
the planner's composite scorer.

**What:** when fetching Vast offers, also capture per-offer:

- `price_per_hour` (already capture? confirm)
- `host_os` (`Ubuntu 22.04`, `Windows Server 2022`, etc.)
- `cuda_version` (driver-reported, e.g. `12.2`, `12.4`, `12.6`, `12.8`)

Surface these on `FleetCapability` (the value object the planner reads
when scoring targets). Currently `FleetCapability` carries `gpu_type`,
`vram_gb`, `cpu_cores`, `ram_gb`, `render_speed`, `price_per_hour`.

Add: `host_os: str | None`, `cuda_version: str | None`.

**Ranking change in `composite_scorer`:**

For two candidate offers of the same `gpu_type`, prefer the one with
the higher CUDA version. Concretely:

```
score = speed_weight * speed_term
      + cost_weight  * cost_term
      + cuda_bonus_weight * cuda_term      # NEW
```

Where `cuda_term` is a normalized 0..1 value derived from the offer's
CUDA version (e.g., 12.8 → 1.0, 12.6 → 0.8, 12.4 → 0.6, 12.2 → 0.4,
older → 0.0). `cuda_bonus_weight` is small (~0.1-0.15) so it's a
tiebreaker between otherwise-similar offers, not the dominant signal.

**Why:** OPTIX kernel compile failures correlate with old CUDA / driver
versions. Preferring newer CUDA reduces the rate of Stage A's
fallback (which is already a recovery, but each fallback is ~20%
slower than the first try). Direct prevention beats recovery.

**Universal across tiers:** Economy / Standard / Premium all get the
same CUDA preference. The cuda_bonus_weight is identical across
tier presets — newer drivers are always desirable, never a tradeoff.

**Implementation outline:**

1. **Vast offer search:** add `host_os` and `cuda_version` to the
   query response parser. Vast's `/asks/` API returns these fields
   per offer; we currently drop them.
2. **`FleetCapability`:** add the two new optional fields.
3. **`FleetAvailabilitySnapshot`:** unchanged shape; the new fields
   ride along on each `FleetCapability` already in `vast_available[]`.
4. **`composite_scorer`:** parse the cuda version string into a
   numeric score (e.g., `12.8` → `12.8` as float), normalize against
   a maximum (~13.0), apply the bonus weight.
5. **`AllocationWeights`:** add `cuda_bonus_weight` field, default
   `0.10` for all three tier presets. Small ratio relative to the
   primary `speed_weight + cost_weight = 1.0` budget.
6. **Modal availability:** Modal's CUDA version is fixed by the
   container image we deploy; can hardcode (e.g., `"12.4"`) or
   leave as None and have the scorer treat None as average.
   Recommend hardcoding to whatever our Modal image actually runs.
7. **Community machines:** the agent can self-report its CUDA version
   in the heartbeat; surface on `CommunityMachine`. For now, leave
   community CUDA as None and have the scorer treat None as average
   so it doesn't penalize community.

**Open questions for Stage D:**

- **D1.** What's the right `cuda_bonus_weight`? 0.10 means a CUDA
  12.8 offer scores ~10% better than a CUDA 12.2 offer of the same
  speed/price. Too high and we'd skip a much-cheaper-but-older
  offer; too low and the bonus is meaningless. Start at 0.10, tune
  via telemetry.
- **D2.** Hard floor? Should we exclude offers below CUDA 12.0
  entirely (driver too old to reliably run our worker image)?
  Recommend: don't hard-exclude in Stage D; revisit if Stage A
  telemetry shows old-CUDA offers are net-negative even after the
  fallback.
- **D3.** OS field — store and surface, but Stage D doesn't FILTER
  on it. That's Stage E.

**Dependencies:** Stage D ships after Stage B (because the worker
behavioral fixes need to land first; no point adding more complexity
to the planner while we're still bleeding zombies).

---

## Stage E — EEVEE Linux-only filter `[NEW]`

**Where:** orchestrator only. Add a target validator (similar to
existing `EngineCompatibilityValidator`).

**What:** when the render's engine is `BLENDER_EEVEE` or
`BLENDER_EEVEE_NEXT`, exclude any Vast offer whose `host_os` is not
Linux from eligibility.

**Why:** EEVEE requires a working OpenGL/EGL stack. On Linux Vast
peers this is reliable (Mesa or NVIDIA EGL); on Windows Vast peers
the GL surface init in headless mode is finicky and produces silent
black-frame renders or kernel-init failures. EEVEE also performs
better on Linux in our testing.

Per the project memory: "EEVEE in cloud = Vast (and future RunPod)
only" — i.e., Modal is already excluded for EEVEE. Stage E narrows
the Vast subset to Linux peers.

**Implementation outline:**

1. **Validator:** `EeveeLinuxOnlyValidator` (or extend the existing
   `EngineCompatibilityValidator` with this rule).
2. **Engine check:** `engine in {"BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"}`
3. **OS check:** for Vast offers (`fleet == "vast_serverless"`),
   require `host_os` to start with `"Linux"` or `"Ubuntu"` or contain
   case-insensitive `"linux"`. Be permissive — Vast OS strings vary.
4. **Community / Modal:** unchanged. Modal is already filtered for
   EEVEE by the existing engine-compat rule. Community is by
   definition the user's own machine, OS-agnostic from our side.

**Depends on Stage D** (need `host_os` in `FleetCapability` first).
Trivial code addition once D is in.

**Open questions for Stage E:**

- **E1.** Match strategy on `host_os` string? Vast may return
  variations like `"Ubuntu 22.04 LTS"`, `"Linux Mint"`, `"Debian 12"`.
  Safe match: `"linux" in host_os.lower()` OR `"ubuntu" in ...`.
  Reject anything starting with `"Windows"`. Recommend permissive
  (assume Linux unless explicitly Windows).

---

## Cross-stage notes

### Deploy ordering

Stages A and B can ship independently and in either order:

- **Stage A** is worker-only — needs a worker image rebuild + Vast
  template update.
- **Stage B** is orchestrator-only — needs a backend deploy.

The C trio needs to ship as a coordinated worker rebuild. C.1 needs
B already deployed (or C.1 silently does nothing on 410 since the
backend never sends one).

Stage D is orchestrator-only, independent of A/B/C. Ships any time.

Stage E depends on D. Ships after D.

**Recommended order:** A → B → C (all three) → D → E.

### Telemetry surface

After all stages land, we should have these grep-able log lines for
production observability:

```
[RENDER_DRIVER] compiled_with=optix             # A: normal path
[RENDER_DRIVER] compiled_with=cuda_fallback     # A: rescue fired
Heartbeat returned 410; worker exiting          # C.1: live zombie killed
Job ... already terminal at startup; exiting    # C.2: cold zombie killed
Worker self-destroyed Vast instance             # C.3: clean teardown
AntiAffinity: ignored N sibling(s) ...          # (already shipped today)
```

Daily greps quantify each stage's effectiveness.

### What this DOESN'T fix

- The "Dispatch failed: No Vast.ai offers" / "400 Bad Request"
  marketplace race — that's a TOCTOU between the snapshot cache
  and the dispatch call. Mitigation requires a snapshot TTL drop
  for high-demand GPU classes or pre-validation at dispatch time.
  Separate plan.
- Cold-start latency mis-modeling in the cost preview — the
  planner's `BASELINE_STARTUP_SEC` doesn't account for Vast
  provisioning (1-3 min) or Modal cold-start (1-3 min). Separate
  plan; flagged after today's calibration discussion.
- The "fragile per-fleet liveness" issue — backup_monitor cron
  reads only the Redis heartbeat key. A reconciler that compares
  `output_frames` count to expected frames would catch
  successfully-rendered-but-callback-lost cases that today drift
  to "failed" on the orchestrator side. Separate plan.
