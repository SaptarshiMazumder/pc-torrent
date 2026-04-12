# Render Job Lifecycle — Call Graphs

## 1. Dispatch Path (render request comes in)

```mermaid
flowchart TD
    render_groups_router["api/routers/render_groups.py"]
    service["services/render_groups/service.py"]
    orchestrator["scheduling/orchestrator.py"]
    fleet["scheduling/fleet.py"]
    frame_dist["scheduling/frame_distribution.py"]
    strategies_init["scheduling/strategies/__init__.py"]
    modal_strategy["scheduling/strategies/modal_strategy.py"]
    vast_strategy["scheduling/strategies/vast_strategy.py"]
    community_strategy["scheduling/strategies/community_strategy.py"]
    modal_init["services/modal/__init__.py"]
    vast_init["services/vast/__init__.py"]
    vast_dispatcher["services/vast/dispatcher.py"]
    vast_client["services/vast/client.py"]
    vast_poller["services/vast/poller.py"]
    modal_monitor["services/modal/monitor.py"]
    db["infrastructure/db.py"]

    render_groups_router -->|"confirm_upload"| service
    service -->|"get_available_machines"| fleet
    fleet -->|"filter_enabled"| frame_dist
    fleet -->|"query machines"| db
    service -->|"plan"| orchestrator
    orchestrator -->|"distribute_frames"| frame_dist
    service -->|"execute"| orchestrator
    orchestrator -->|"dispatch_all"| fleet
    fleet -->|"INSERT jobs"| db
    fleet -->|"get_strategy"| strategies_init
    strategies_init -->|"modal"| modal_strategy
    strategies_init -->|"vast"| vast_strategy
    strategies_init -->|"community"| community_strategy
    modal_strategy -->|"lazy import"| modal_init
    modal_init -->|"dispatch + start monitor"| modal_monitor
    vast_strategy -->|"lazy import"| vast_init
    vast_init -->|"dispatch_and_save"| vast_dispatcher
    vast_dispatcher -->|"API call"| vast_client
    vast_init -->|"start_polling_thread"| vast_poller
```

---

## 2. Failure Path (job fails, retry/failover)

```mermaid
flowchart TD
    vast_poller["services/vast/poller.py"]
    modal_monitor["services/modal/monitor.py"]
    failover_scanner["scheduling/failover_scanner.py"]
    orchestrator["scheduling/orchestrator.py"]
    fleet["scheduling/fleet.py"]
    strategies_init["scheduling/strategies/__init__.py"]
    db["infrastructure/db.py"]

    vast_poller -->|"on failure"| orchestrator
    modal_monitor -->|"on failure"| orchestrator
    failover_scanner -->|"stale desktop / failed serverless"| orchestrator
    orchestrator -->|"load_job"| fleet
    orchestrator -->|"remaining_frames?"| fleet
    orchestrator -->|"retry same endpoint"| fleet
    orchestrator -->|"pick_failover"| fleet
    orchestrator -->|"dispatch_failover"| fleet
    orchestrator -->|"mark_done / mark_failed"| fleet
    fleet -->|"query + update jobs"| db
    fleet -->|"query machines"| db
    fleet -->|"get_strategy"| strategies_init
```

---

## 3. Background + Status

```mermaid
flowchart TD
    failover_scanner["scheduling/failover_scanner.py"]
    orchestrator["scheduling/orchestrator.py"]
    fleet["scheduling/fleet.py"]
    service["services/render_groups/service.py"]
    vast_recovery["services/vast/recovery.py"]
    vast_poller["services/vast/poller.py"]
    db["infrastructure/db.py"]

    failover_scanner -->|"every 10s: scan groups"| db
    failover_scanner -->|"handle_failure"| orchestrator
    vast_recovery -->|"on startup: reattach polls"| vast_poller
    vast_recovery -->|"gone instance"| orchestrator
    service -->|"compute_group_status"| fleet
    service -->|"get_available_machines"| fleet
    fleet -->|"query"| db
```

---

## File Map

| File | Lines | Role |
|---|---|---|
| `scheduling/orchestrator.py` | ~145 | **WHAT** — plan, execute, handle_failure |
| `scheduling/fleet.py` | ~450 | **HOW** — DB writes, dispatch threads, failover selection, machine queries, status aggregation |
| `scheduling/frame_distribution.py` | ~265 | Pure math — scoring, budgeting, splitting frames |
| `scheduling/failover_scanner.py` | ~243 | Background daemon — detects stale/orphaned jobs |
| `scheduling/strategies/` | | Provider adapters — Modal, Vast, Community |
| `models/` | | Pure data — RenderJob, RenderGroup, value objects |
| `services/render_groups/service.py` | | HTTP-facing service — calls orchestrator |

---

## Key Design Decisions

| Decision | Why |
|---|---|
| Orchestrator is WHAT, Fleet is HOW | Anyone can read `orchestrator.py` and understand the full lifecycle in 145 lines |
| GET is read-only | All mutation happens via FailoverScanner or explicit POST calls |
| Orchestrator is stateless | Every call re-reads from DB; no in-memory job ownership |
| Provider threading is per-strategy | Modal = thread-per-job, Vast = sequential thread with 2s gap, Community = no-op |
| Retry cap is per-range, not per-job | A frame range stops at 5 failed attempts to prevent infinite GPU burn |
| blend_url is per-provider | Each strategy's service module has its own PUBLIC_BACKEND_URL |
