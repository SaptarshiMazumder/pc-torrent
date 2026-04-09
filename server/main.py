try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from infrastructure.db import init_db
from services import runpod_dispatch, modal_dispatch
from api.routers import health, logs, machines, jobs, render_groups, assets, docker
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
    runpod_dispatch.register_virtual_machines()
    runpod_dispatch.start_heartbeat_thread()
    modal_dispatch.register_virtual_machines()
    modal_dispatch.start_heartbeat_thread()


app.include_router(health.router)
app.include_router(logs.router)
app.include_router(machines.router)
app.include_router(jobs.router)
app.include_router(render_groups.router)
app.include_router(assets.router)
app.include_router(docker.router)
