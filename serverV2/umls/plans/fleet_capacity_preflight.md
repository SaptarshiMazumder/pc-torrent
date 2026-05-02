# Fleet capacity pre-flight — Vast vs Modal

> Open thread. Pauses while other work ships. Pick up here when the chunk-progress / retry / anti-affinity deploys are stable.

## Context: why this came up

Today's dispatch flow is "create row → call provider → handle failure if any." That works (failures route through `FAILURE_PIPELINE` → auto-retry), but creates spurious `'failed'` rows when a provider has no capacity *right now*. The user asked whether we can pre-check availability before committing to a dispatch.

Answer differs sharply by fleet because Vast and Modal are architecturally different.

---

## Vast — pre-flight is real and already half-implemented

### How dispatch works today

1. Allocator picks a Vast `(fleet, gpu_type)` target.
2. `VastFleetStrategy.dispatch` writes a `'pending'` row.
3. `VastClient.dispatch_job` calls `VastOfferSearcher.search(gpu_name)` which does `GET /bundles/` with filters (rentable, reliability, CUDA version, price cap, disk, secure-cloud).
4. Picks `offers[0]` (cheapest).
5. `VastInstanceManager.create` rents via `PUT /asks/{offer_id}/`.
6. Returns `instance_id`, monitor starts.

### The pre-flight question

`GET /bundles/` IS already a pure availability query — read-only, no state change. The "search" call inside `dispatch_job` IS the availability check. The only issue is **timing**:

- Today: search happens AFTER `JobRepository.create` writes the `'pending'` row.
- Pre-flight: search would happen INSIDE the allocator, BEFORE deciding to assign Vast.

### Failure modes and what they cost today

| Cause | Today's cost |
|---|---|
| `bundles/` returns empty (no matching offers) | 1 row created → 1 mark-failed → 1 retry round-trip |
| `PUT /asks/{id}/` race lost (offer rented by someone else) | Same |
| Vast 4xx/5xx | Same |
| Network timeout | Same |

All path through `FAILURE_PIPELINE` → `AUTO_RETRY_PIPELINE`. Chunk doesn't disappear; it just costs an extra DB row + log line + one retry slot.

### Three pre-flight options

1. **Status quo.** Retry pipeline absorbs the cost. Zero work. Spurious DB/log noise, but functionally correct.
2. **Pre-flight in the allocator.** Allocator calls `VastClient.has_available_offers(gpu_name)` (a thin wrapper over `search`) during `_eligible_targets`. If empty, that GPU type drops out. Real architectural change. Costs an extra Vast API call per allocation cycle per GPU type.
3. **Cache-backed pre-flight.** Cache `has_available_offers(gpu_name)` for ~30s. Allocator reads cache. Best of both worlds, more complexity.

Worth doing only if you're seeing real spurious-failure problems. As of writing the retry pipeline handles it correctly (after the recent typo fix + including_row + cancelled-retryable + chunk_progress fixes ship).

---

## Modal — pre-flight is fundamentally not viable

### How Modal differs from Vast

Vast is a marketplace (catalog of itemized supply, search-then-rent). Modal is serverless compute (call a function, scheduler decides). There's no catalog to query.

Modal dispatch:

```
POST https://{workspace}--{app}-render-{gpu_type}.modal.run
Headers: Modal-Key, Modal-Secret
Body:    { "input": { job_id, blend_url, frames..., render_overrides_b64, backend_url } }
Response: 2xx/redirect with x-modal-function-call-id header → "fc-XXX"
```

The `fc-XXX` call id comes back as soon as Modal accepts the job into its queue, NOT when the worker starts.

### Modal's hard cap layers and how they manifest

| Cap | What happens at the limit |
|---|---|
| **Per-function `max_containers`** (set in the Modal App's `@app.function(max_containers=N)`) | Calls **silently queue** — POST returns 2xx with `fc-XXX`, function call sits idle until a slot opens |
| **Workspace concurrent-GPU limits** (Modal account quota; A100/H100 commonly capped, support contact to raise) | Same queuing behavior; sometimes 429 if Modal's own queue is full |
| **Billing/account caps** (out of credits, payment failure) | HTTP 4xx (401/402/403) at dispatch time |
| **Modal-side outage / 5xx** | HTTP 5xx |
| **Network timeout to Modal** | `httpx.TimeoutException` → wrapped `RuntimeError` |

### Two distinct failure surfaces from cap-hits

1. **Dispatch-time failures** (4xx/5xx/timeout) — caught in `ModalFleetStrategy` exception block, routes through `_on_failure` → `FAILURE_PIPELINE` → auto-retry. Same path as Vast.
2. **Silent queue-stall** — dispatch returned 2xx with `fc-XXX`, row is `pending`, worker never starts because Modal is throttling internally. Caught by the per-job monitor's `in_queue_timeout_sec` (120s default): when a Modal job sits in `pending` past that window without a worker check-in, the monitor fires `_on_failure(job_id, "queue timeout")` and the retry pipeline runs.

Caps don't make jobs disappear. They show up as either fast failures (dispatch errors) or slow failures (queue timeouts).

### What the codebase already does for Modal capacity

`config.py:216` — `max_parallel: int = 15` per fleet, configurable via `config.json` `modal.max_parallel`. The allocator's resource picker uses `JobRepository.count_active_by_fleet` (counts live `pending`/`running` rows per `machine_type`) and refuses to dispatch beyond `max_parallel` even if Modal would accept.

This is a **client-side throttle that sits BELOW Modal's account-effective cap**. Acts as the pre-flight equivalent.

### Why Modal can't have a true HTTP pre-flight

| Possibility | Verdict |
|---|---|
| Hit Modal's stats API via Python SDK (`Function.get_current_stats()`) | Works, but heavyweight — SDK init is non-trivial cold start, not HTTP-friendly, runs in-process awkwardly inside Cloud Run |
| Track `max_containers` minus in-flight ourselves | Already done (`max_parallel` + `count_active_by_fleet`) |
| HEAD / 0-payload POST | Modal charges for the cold start; counterproductive |
| Watch 429 rate and back off (circuit breaker) | Reactive, not pre-flight; could be added later |

### Honest framing

> **Pre-flight isn't viable for Modal. The capacity story is: set `max_parallel` low enough to stay under your account's cap, trust the retry pipeline + `in_queue_timeout_sec` for the rare overage.**

---

## Open work to do when this thread resumes

| Item | Detail |
|---|---|
| **Verify `modal.max_parallel` in config.json** | Should be ≤ Modal account's effective concurrent-GPU cap. Above that, silent queueing dominates and `in_queue_timeout_sec` failures pile up |
| **Verify `in_queue_timeout_sec` is right for Modal cold starts** | Default 120s. On rare GPUs (H100, A100-80G) under load, Modal can queue for 90s+ before assigning, then 40s+ for cold start = 130s+ before first heartbeat. May need bumping |
| **Decide on Vast pre-flight (if needed)** | Three options above; default is status quo unless real spurious-failure problems show up |
| **Decide on Modal circuit breaker (if needed)** | If 429 rate climbs, add reactive back-off in `ModalFleetStrategy.dispatch`. Not a pre-flight, but reduces wasted retries |
| **Soft-fallback in Vast strategy** | `if self._on_failure: ... else: mark_failed` at `vast/strategy.py:104-107` is a defensive branch that masks a wiring bug if `_on_failure` ever drops out. Per the "no defensive fallbacks" rule, replace with assertion or remove the fallback |

## Resolved (don't re-litigate)

- Modal pre-flight via HTTP — not possible, scoped out.
- Vast pre-flight via cache — viable but premature; revisit if status quo proves too noisy.
- Whether dispatch failures cause chunks to disappear — they don't; retry pipeline handles them. Verified end-to-end.
