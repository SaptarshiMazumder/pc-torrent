# Daemons & leader election

Every long-running thread in the serverV2 process, where it runs, when it
starts, when it dies, who can spawn it, and the leader-election story.

---

## TL;DR

```
Per-instance (leader or follower, no leader gating):
   - VastInstanceMonitor   -- one thread per active Vast job
   - ModalJobMonitor       -- one thread per active Modal job

Leader-only:
   - VastRecovery          -- one-shot scan at boot
   - ModalRecovery         -- one-shot scan at boot
   - CommunityMonitor      -- long-lived scanning thread

(backup_monitor is a separate process -- not in serverV2, scheduled cron)
```

The asymmetry is intentional: Vast/Modal have something per-job to watch
(their provider API for that specific instance), so the watcher lives
wherever the dispatch happened.  Community has nothing per-job to watch
(workers pull from the DB themselves) but does need someone scanning the
whole pool for stuck jobs -- one thread on the leader covers it.

---

## All daemons (and one-shots) at a glance

| Daemon | Type | Lives in | Started by | Started when | Stops when |
|---|---|---|---|---|---|
| `VastRecovery.recover()` | one-shot scan, no thread | serverV2 process | `main.py: _run_recovery()` | boot, **leader only** | finishes its loop and returns immediately |
| `ModalRecovery.recover()` | one-shot scan, no thread | serverV2 process | `main.py: _run_recovery()` | boot, **leader only** | same |
| `CommunityMonitor` | single long-lived scanning thread | serverV2 process | `main.py: c.community_monitor.start()` | boot, **leader only** | process death |
| `VastInstanceMonitor` | one thread **per active Vast job** | serverV2 process | `VastCallbackHandler.start_monitoring(job_id, ...)` | (a) dispatch time inside `VastFleetStrategy.dispatch`, OR (b) boot via `VastRecovery._recover_single` | thread observes terminal state and returns from `_tick()` with True; OR `stop_monitoring(job_id)` called |
| `ModalJobMonitor` | one thread **per active Modal job** | serverV2 process | `ModalCallbackHandler.start_monitoring(job_id, ...)` | same shape as Vast | same shape as Vast |
| `HeartbeatSender` (cloud_worker) | one thread per worker container | inside Vast/Modal cloud_worker container (separate process) | `handler.py: heartbeat.start()` | when the worker boots | container exit |
| `JobHeartbeatSender` (agent) | one thread per active community job | inside community agent (Tauri sidecar on user PC) | `agent.py: execute_job` | when the agent claims a job | finally block of `execute_job` |
| `backup_monitor` | external Cloud Run Job (cron) | **separate process**, NOT in serverV2 | Cloud Scheduler every 60s | scheduled trigger | each tick is its own process; exits after one scan |

---

## Q1 — How is the leader picked? Where in the code?

### Mechanism: Postgres advisory lock

**[serverV2/infrastructure/db.py:80-104](serverV2/infrastructure/db.py#L80-L104)** — `try_acquire_leader_lock()`:

```python
_LEADER_LOCK_KEY = 7_391_823
_leader_conn = None

def try_acquire_leader_lock() -> bool:
    pool = _get_pool()
    conn = pool.getconn()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_LEADER_LOCK_KEY,))
        acquired = bool(cur.fetchone()[0])
    conn.commit()
    if acquired:
        _leader_conn = conn      # hold the conn open for the lifetime of the process
        return True
    pool.putconn(conn)
    return False
```

`pg_try_advisory_lock()` is a non-blocking Postgres primitive — at most one
session in the entire database can hold a given lock key at a time.

The leader is whichever Cloud Run instance manages to call this first
during boot. The lock is held for the lifetime of `_leader_conn`. When
that connection drops (process dies, crash, redeploy), Postgres
auto-releases the lock and the next caller succeeds.

### Where it's called

**[serverV2/main.py:71-77](serverV2/main.py#L71-L77)** in the FastAPI startup hook:

```python
if try_acquire_leader_lock():
    _run_recovery(_container)              # VastRecovery + ModalRecovery
    _drain_queues_after_recovery(_container)
    _container.community_monitor.start()   # the one long-lived scanner
    log.info("ServerV2 startup complete (leader)")
else:
    log.info("ServerV2 startup complete (follower — daemons skipped)")
```

### Practical behavior on Cloud Run

```
Cold start:
  Instance A spins up -> startup hook runs -> wins lock -> becomes leader
  Instance B spins up later -> startup hook runs -> loses lock -> follower

If instance A is killed (autoscale down, OOM, redeploy):
  Connection holding _LEADER_LOCK_KEY closes -> Postgres releases lock
  Instance B's NEXT startup hook would acquire it... but startup only runs
    once per instance lifetime, not on lock-loss
  Practical effect: when A dies, the system has NO leader until either:
    - a new instance boots (Cloud Run autoscale up) and acquires the lock, or
    - an existing follower restarts (Cloud Run rotates instances)
```

That's a real gap — there's no live "leader handoff" while followers
continue to serve traffic. In practice Cloud Run cycles instances often
enough that a fresh leader takes over within a few minutes, and the
backup_monitor (separate cron) catches anything stuck during the gap.

---

## Q2 — A follower receives a dispatch request. Can it start daemons?

### Short answer

**Yes, partially.** A follower CAN spawn per-job monitor threads (Vast /
Modal) on itself when it handles a dispatch. It CANNOT start the
leader-only daemons (recovery, CommunityMonitor). Per-job monitors and
leader-only daemons are different things gated by different mechanisms.

### Trace through the code path

```
HTTP request lands at any instance (load balancer is round-robin)
    POST /render-groups/{id}/confirm-upload
        -> RenderGroupService.confirm_upload
            -> orchestrator.plan(engine=...)        # pure CPU, no daemons
            -> orchestrator.execute(...)
                -> RenderLifecycle.start_render
                    -> DispatchCoordinator.enqueue_and_flush
                        -> Dispatcher.dispatch_one(task, ctx, job_id)
                            -> FleetRegistry.get(task.fleet)
                            -> strategy.dispatch(task, ctx, job_id)

For Vast (fleets/vast/strategy.py):
    strategy.dispatch:
        1. job_repo.create(...)                                    # DB write
        2. vast_client.dispatch_job(...)                           # HTTP -> Vast.ai
        3. self._callback.start_monitoring(job_id=..., ...)        # SPAWNS THREAD HERE

VastCallbackHandler.start_monitoring -> threading.Thread(target=monitor.run).start()
```

That last line spawns a thread on **the current instance**, regardless of
whether it's the leader or a follower. There is no leader check around
`start_monitoring`. The thread polls Vast.ai and reports
success/failure via the `_on_failure` / `_on_success` closures.

Same shape for Modal. Same mechanism.

### Why this is the right design

The leader-only daemons (`VastRecovery`, `ModalRecovery`,
`CommunityMonitor`) all do **discovery**: they enumerate ALL jobs in the
DB to find ones that need attention. Running discovery on every instance
would multiply the work pointlessly and risk double-fires (multiple
instances reattaching the same monitor).

Per-job monitors are different — they watch ONE specific job that THIS
instance just dispatched. Whoever dispatched it has the in-memory context
(callback handler, job_id) so it's the natural place to spawn the watcher.
No discovery needed, no leader coordination needed.

### What if the follower instance dies mid-render?

Per-job monitor thread dies with it. Three independent safety nets pick
up the orphan:

1. **Leader's `VastRecovery.recover()` / `ModalRecovery.recover()` at
   the next leader-boot** (whenever a new leader takes over, e.g.
   instance churn). They query for `status='running'` Vast/Modal jobs
   and re-spawn monitor threads via the same `start_monitoring` call.
2. **`backup_monitor` Cloud Run Job (cron, every 60s)** — scans for
   `status='running'` jobs whose Redis heartbeat has expired, POSTs to
   `/internal/orphan/{job_id}`, server marks failed via CallbackRouter.
3. **Eventually-consistent heartbeat death** — the worker container
   keeps running on the provider's infra. If it finishes, it pushes
   final frames to R2, server's `register_outputs` fires success
   (post-B3-reshape) regardless of whether any monitor thread is alive.
   The data IS the success signal.

So follower-death has multiple recovery paths. None of them are instant,
but the system converges.

### Concrete walk-through: follower-only dispatch

```
T=0   Follower B receives POST /render-groups/.../confirm-upload
T=0   Follower B's Dispatcher.dispatch_one calls
      VastFleetStrategy.dispatch
T=1   Follower B issues create-instance HTTP to Vast.ai, gets instance_id
T=1   Follower B writes job row with vast_job_id=<instance_id>
T=1   Follower B's VastCallbackHandler.start_monitoring spawns
      VastInstanceMonitor thread on instance B
T=2..N  Thread on B polls Vast.ai every N sec; worker uploads frames;
        register_outputs fires success on the server side; thread sees
        local_status='done' on its next tick, destroys the Vast instance,
        exits cleanly

If at T=K instance B dies:
  - Per-job monitor thread on B dies
  - Vast instance keeps running idle on Vast.ai
  - Worker may still be pushing frames (which will land via register_outputs)
  - Heartbeat key expires after 60s in Redis
  - At T=K+~60s: backup_monitor scans, sees no heartbeat, reports orphan
    - If worker actually finished (output_files complete), CallbackRouter's
      handle_chunk_failed sees the row already at status='done' (because
      register_outputs fired success first) and the orphan report is a
      no-op via is_job_terminal guard
    - If worker is genuinely stuck, handle_chunk_failed marks the job
      failed and the retry chain fires
  - When the next leader-boot happens (could be instance B if it restarts,
    could be a new instance C), VastRecovery sweeps and re-spawns
    monitors for any still-running Vast jobs
```

---

## Summary: what runs where

```
                 +-----------------------------------+
                 |  serverV2 process (any instance)  |
                 +-----------------------------------+
                 | API routes (HTTP)                 |
                 | RenderGroupService                |
                 | RenderOrchestrator + Lifecycle    |
                 | DispatchCoordinator               |
                 | FleetRegistry (per-fleet strategy)|
                 | CallbackRouter + handlers         |
                 | Repositories (DB I/O)             |
                 |                                   |
                 | Per-job monitor threads           |
                 |   (spawned by THIS instance       |
                 |    when it dispatches a job;      |
                 |    not leader-gated)              |
                 +-----------------------------------+
                       |
                  if leader:
                       v
                 +-----------------------------------+
                 | Leader-only daemons               |
                 +-----------------------------------+
                 | VastRecovery (one-shot at boot)   |
                 | ModalRecovery (one-shot at boot)  |
                 | CommunityMonitor (long-lived)     |
                 +-----------------------------------+


+-------------------------------------+
|  backup_monitor Cloud Run Job       |  <- separate process, scheduled every 60s
+-------------------------------------+
| 1. SELECT running serverless jobs   |
| 2. Check Redis heartbeat            |
| 3. POST /internal/orphan/{id} for   |
|    those without heartbeat          |
+-------------------------------------+


+-------------------------------------+      +-------------------------------------+
| cloud_worker container (Vast/Modal) |      | community agent (user's PC)         |
+-------------------------------------+      +-------------------------------------+
| HeartbeatSender thread              |      | machine heartbeat thread            |
|   + ProcessSampler (CPU/RSS)        |      | JobHeartbeatSender (per active job) |
|   + PhaseTracker                    |      |   + PhaseTracker                    |
|   + BytesProgress                   |      |   + BytesProgress (download phase)  |
| Blender subprocess                  |      | Docker container running render     |
+-------------------------------------+      +-------------------------------------+
```
