"""FastAPI application entry point for serverV2.

Startup sequence:
1. Load env vars
2. Build the composition root (Container)
3. Register virtual machines + start heartbeat threads
4. Run startup recovery for in-flight jobs
5. Start failover scanner background thread
6. Mount all routers
"""

from __future__ import annotations

import logging
import os
import threading

from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from serverV2.bootstrap import Container, build
from serverV2.infrastructure.db import init_db, try_acquire_leader_lock
from serverV2.infrastructure.redis_client import RedisClient

log = logging.getLogger(__name__)

app = FastAPI(title="PC Rent Server V2", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_container: Container | None = None


def _get_container() -> Container:
    global _container
    if _container is None:
        raise RuntimeError("Container not built — startup incomplete")
    return _container


@app.on_event("startup")
def on_startup() -> None:
    global _container
    log.info("ServerV2 starting up...")

    init_db()

    redis_client = RedisClient()
    redis_client.connect()

    _container = build(redis_client=redis_client)

    _wire_routers(_container)

    # Background daemons must only run on ONE Cloud Run instance.  After
    # the allocator redesign, Modal and Vast no longer have machine
    # registrars / heartbeat threads — their capacity is described in
    # config.json and they're elastic at dispatch time.  Recovery and the
    # community monitor remain leader-only.
    if try_acquire_leader_lock():
        _run_recovery(_container)
        _drain_queues_after_recovery(_container)
        _container.community_monitor.start()
        log.info("ServerV2 startup complete (leader)")
    else:
        log.info("ServerV2 startup complete (follower — daemons skipped)")


def _wire_routers(c: Container) -> None:
    from serverV2.api.routers import (
        assets,
        debug,
        docker,
        health,
        internal,
        jobs,
        logs,
        machines,
        render_groups,
    )

    machines.init(c.machine_service, machine_repo=c.machine_repo)
    jobs.init(c.job_service, orchestrator=c.orchestrator, callback_router=c.callback_router)
    render_groups.init(c.render_group_service, c.upload_coordinator)
    assets.init(c.asset_service)
    debug.init(aggregator=c.status_aggregator)
    internal.init(c.callback_router, c.config.orphan_secret)

    app.include_router(health.router)
    app.include_router(machines.router)
    app.include_router(jobs.router)
    app.include_router(render_groups.router)
    app.include_router(assets.router)
    app.include_router(docker.router)
    app.include_router(logs.router)
    app.include_router(debug.router)
    app.include_router(internal.router)


def _run_recovery(c: Container) -> None:
    try:
        c.vast_recovery.recover()
    except Exception as exc:
        log.warning("Vast recovery failed: %s", exc)

    try:
        c.modal_recovery.recover()
    except Exception as exc:
        log.warning("Modal recovery failed: %s", exc)


def _drain_queues_after_recovery(c: Container) -> None:
    """Catch the post-restart edge case where queued items were left
    behind by the previous process.  If no active job exists to fire a
    drain trigger, those rows would sit forever — kick the drain once
    per enabled fleet at boot.
    """
    for fleet in c.fleet_registry.enabled_fleets():
        try:
            c.dispatch_coordinator.drain_for_fleet(fleet)
        except Exception as exc:
            log.warning("Boot-time drain for %s failed: %s", fleet, exc)
