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
from serverV2.infrastructure.db import init_db

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

    _container = build()

    _wire_routers(_container)

    _register_machines(_container)

    _run_recovery(_container)

    _container.failover_scanner.start()
    log.info("ServerV2 startup complete")


def _wire_routers(c: Container) -> None:
    from serverV2.api.routers import (
        assets,
        debug,
        docker,
        health,
        jobs,
        logs,
        machines,
        render_groups,
    )

    machines.init(c.machine_service, machine_repo=c.machine_repo)
    jobs.init(c.job_service, c.upload_coordinator)
    render_groups.init(c.render_group_service, c.upload_coordinator)
    assets.init(c.asset_service)
    debug.init(
        vast_registry=c.vast_instance_registry,
        modal_registry=c.modal_instance_registry,
    )

    app.include_router(health.router)
    app.include_router(machines.router)
    app.include_router(jobs.router)
    app.include_router(render_groups.router)
    app.include_router(assets.router)
    app.include_router(docker.router)
    app.include_router(logs.router)
    app.include_router(debug.router)


def _register_machines(c: Container) -> None:
    try:
        c.vast_registrar.register_all()
        c.vast_registrar.start_heartbeat()
    except Exception as exc:
        log.warning("Vast machine registration failed: %s", exc)

    try:
        c.modal_registrar.register_all()
        c.modal_registrar.start_heartbeat()
    except Exception as exc:
        log.warning("Modal machine registration failed: %s", exc)


def _run_recovery(c: Container) -> None:
    try:
        c.vast_recovery.recover()
    except Exception as exc:
        log.warning("Vast recovery failed: %s", exc)

    try:
        c.modal_recovery.recover()
    except Exception as exc:
        log.warning("Modal recovery failed: %s", exc)
