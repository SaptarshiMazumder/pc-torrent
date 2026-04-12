try:
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).with_name(".env"))
except ImportError:
    pass

import logging
logging.basicConfig(level=logging.INFO)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from infrastructure.db import init_db, query_all
from scheduling.failover_scanner import scanner as failover_scanner
from services import modal as modal_dispatch
from services import vast as vast_dispatch
from api.routers import (
    health,
    logs,
    machines,
    jobs,
    render_groups,
    assets,
    docker,
    vast,
    modal_instances,
)
from api.routers.logs import setup_log_broadcast

app = FastAPI(title="PC Rent Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    setup_log_broadcast()
    init_db()
    modal_dispatch.register_virtual_machines()
    modal_dispatch.start_heartbeat_thread()
    modal_dispatch.recover_polling_threads()
    vast_dispatch.register_virtual_machines()
    vast_dispatch.start_heartbeat_thread()
    vast_dispatch.recover_polling_threads()

    failover_scanner.start()


app.include_router(health.router)
app.include_router(logs.router)
app.include_router(machines.router)
app.include_router(jobs.router)
app.include_router(render_groups.router)
app.include_router(assets.router)
app.include_router(docker.router)
app.include_router(vast.router)
app.include_router(modal_instances.router)
