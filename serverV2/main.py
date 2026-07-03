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
from fastapi.staticfiles import StaticFiles

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
    # Attach the in-memory log ring buffer FIRST so startup logs are captured
    # and the admin dashboard's /logs panel has data from boot onward.
    from serverV2.api.routers.logs import install_log_capture
    install_log_capture()

    log.info("ServerV2 starting up...")

    init_db()

    redis_client = RedisClient()
    redis_client.connect()

    _container = build(redis_client=redis_client)

    _wire_routers(_container)

    # Every Cloud Run instance runs the same boot sequence.  Lock-based
    # ownership replaces the old leader election:
    #
    #  * Three fleet singletons (vast / modal / community) each try to
    #    acquire their ``monitor:<fleet>`` Redis lock at boot.  At most
    #    one Cloud Run instance wins each lock and runs that fleet's
    #    scan thread.  If a holder dies, its lock TTL expires (60s) and
    #    the MonitorLockSweeper running on every other instance re-
    #    attempts try_start on the next 30s tick.
    #
    #  * ``monitor_lock_facade.start()`` is the sweeper itself.  It
    #    loops every 30s and calls try_start() on each fleet singleton
    #    (idempotent if we already own it, no-op if another instance
    #    holds the lock).  Also runs the group-status drift audit
    #    strategy alongside the fleet sweeps.
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
    _container.vast_fleet_monitor.try_start()
    _container.modal_fleet_monitor.try_start()
    if _container.community_monitor.try_start():
        log.info("ServerV2 startup complete (community-monitor owner)")
    else:
        log.info(
            "ServerV2 startup complete "
            "(community-monitor owned by another instance)",
        )


def _wire_routers(c: Container) -> None:
    from serverV2.api import dependencies
    from serverV2.api.routers import (
        admin_config,
        admin_dashboard,
        app_meta,
        assets,
        community,
        debug,
        docker,
        health,
        internal,
        jobs,
        logs,
        machines,
        pre_render,
        render_groups,
        users,
    )

    # Authorization layer: require_admin resolves the caller's role
    # through the user facade.  Wired here alongside the router inits.
    dependencies.init_authz(c.user_facade)

    machines.init(c.machine_service, c.allocation_client)
    jobs.init(
        c.job_service,
        orchestrator=c.orchestrator,
        callback_router=c.callback_router,
        download_stats=c.download_stats,
    )
    render_groups.init(
        c.render_group_service,
        c.upload_coordinator,
        c.allocation_facade,
        c.allocation_client,
        download_stats=c.download_stats,
    )
    pre_render.init(c.pre_render_estimator)
    assets.init(c.asset_service)
    users.init(c.user_facade)
    debug.init(aggregator=c.status_aggregator)
    internal.init(
        c.callback_router,
        c.config.orphan_secret,
        vast_client=c.vast_client,
        modal_client=c.modal_client,
        job_repo=c.job_repo,
    )
    admin_config.init(c.allocation_client, c.cost_estimation_config_repo)
    admin_dashboard.init(c.admin_telemetry_service)
    app_meta.init(c.allocation_config_repo)
    community.init(c.config.community_worker_image)
    docker.init(download_stats=c.download_stats)

    app.include_router(health.router)
    app.include_router(machines.router)
    app.include_router(jobs.router)
    app.include_router(render_groups.router)
    app.include_router(pre_render.router)
    app.include_router(assets.router)
    app.include_router(users.router)
    app.include_router(docker.router)
    app.include_router(logs.router)
    app.include_router(debug.router)
    app.include_router(internal.router)
    app.include_router(admin_config.router)
    app.include_router(admin_dashboard.router)
    app.include_router(app_meta.router)
    app.include_router(community.router)

    _mount_home_spa()


def _mount_home_spa() -> None:
    """Serve the /home dashboard SPA (built by the Dockerfile's node stage
    into ``serverV2/home/dist``).  The SPA uses hash-based navigation, so
    ``StaticFiles(html=True)`` is all the routing it needs.  Skipped when
    the bundle isn't present (local dev without a build)."""
    dist = Path(__file__).parent / "home" / "dist"
    if not dist.is_dir():
        log.info("/home SPA bundle not found at %s — skipping mount", dist)
        return
    app.mount("/home", StaticFiles(directory=str(dist), html=True), name="home")
    log.info("/home SPA mounted from %s", dist)


