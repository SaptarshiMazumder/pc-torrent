"""FastAPI application entry point for serverV2.

Startup sequence:
1. Load env vars
2. Build the composition root (Container)
3. Mount routers
4. Try to start the community monitor (singleton across instances)
5. Start the monitor lock sweeper (per-instance, no leader gating)
6. Start the allocation dispatch queue daemon on every replica;
   only the lock-holder's tick fires (Redis singleton).  Queued
   items left from a previous process are picked up on the next tick.
"""

from __future__ import annotations

import logging

from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from serverV2.bootstrap import Container, build
from serverV2.infrastructure.db import init_db
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

    # Every Cloud Run instance runs the same boot sequence.  Lock-based
    # ownership replaces the old leader election:
    #
    #  * ``community_monitor.try_start()`` attempts the singleton
    #    ``monitor:community`` Redis lock; the first booted instance
    #    wins and starts the scan thread, others no-op.  If that
    #    instance dies, its lock TTL expires (60s) and the
    #    MonitorLockSweeper running on every other instance will
    #    re-attempt try_start on the next tick.
    #
    #  * ``monitor_lock_facade.start()`` runs on every instance and
    #    iterates active Vast / Modal jobs every 30s, asking the
    #    managers to start_monitoring.  Each manager try_acquires a
    #    per-job lock; at most one wins per job.  No leader needed.
    #
    #  * ``allocation_dispatch_queue_daemon.start()`` runs on every
    #    instance.  Each replica's thread polls the
    #    ``allocation:dispatch:daemon`` Redis lock; only the holder
    #    actually dispatches.  When the holder dies, lock TTL expires
    #    and the next replica's poll picks up the work.  Stranded
    #    queue rows from a previous process are cleared the same way:
    #    next tick after boot.
    _container.monitor_lock_facade.start()
    _container.allocation_dispatch_queue_daemon.start()
    if _container.community_monitor.try_start():
        log.info("ServerV2 startup complete (community-monitor owner)")
    else:
        log.info(
            "ServerV2 startup complete "
            "(community-monitor owned by another instance)",
        )


def _wire_routers(c: Container) -> None:
    from serverV2.api.routers import (
        admin_config,
        assets,
        debug,
        docker,
        health,
        internal,
        jobs,
        logs,
        machines,
        pre_render,
        render_groups,
    )

    machines.init(c.machine_service, c.allocation_client)
    jobs.init(c.job_service, orchestrator=c.orchestrator, callback_router=c.callback_router)
    render_groups.init(c.render_group_service, c.upload_coordinator, c.allocation_facade, c.allocation_client)
    pre_render.init(c.pre_render_estimator)
    assets.init(c.asset_service)
    debug.init(aggregator=c.status_aggregator)
    internal.init(c.callback_router, c.config.orphan_secret)
    admin_config.init(c.allocation_client)

    app.include_router(health.router)
    app.include_router(machines.router)
    app.include_router(jobs.router)
    app.include_router(render_groups.router)
    app.include_router(pre_render.router)
    app.include_router(assets.router)
    app.include_router(docker.router)
    app.include_router(logs.router)
    app.include_router(debug.router)
    app.include_router(internal.router)
    app.include_router(admin_config.router)


