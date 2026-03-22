import io
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

try:
    from .db import conn, init_db, lock
except ImportError:
    from db import conn, init_db, lock

BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "jobs"

app = FastAPI(title="PC Rent Python Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RegisterMachinePayload(BaseModel):
    machine_key: str | None = None
    gpu_model: str
    gpu_vram_gb: float
    cpu_cores: int
    ram_gb: float


class UpdateJobStatusPayload(BaseModel):
    status: str
    error: str | None = None
    output_files: list[str] | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def parse_output_files(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def sanitize_filename(name: str) -> str:
    safe = Path(name).name
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return safe


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    JOBS_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/")
def root() -> dict[str, str]:
    return {"status": "PC Rent python server running"}


@app.post("/machines/register")
def register_machine(payload: RegisterMachinePayload) -> dict[str, str]:
    if (
        not payload.gpu_model
        or payload.gpu_vram_gb is None
        or payload.cpu_cores is None
        or payload.ram_gb is None
    ):
        raise HTTPException(
            status_code=400,
            detail="Missing required fields: gpu_model, gpu_vram_gb, cpu_cores, ram_gb",
        )

    machine_key = payload.machine_key.strip() if payload.machine_key else None
    current_time = now_iso()

    with lock:
        existing = None
        if machine_key:
            existing = conn.execute(
                "SELECT id FROM machines WHERE machine_key = ?",
                (machine_key,),
            ).fetchone()

        if existing:
            machine_id = existing["id"]
            conn.execute(
                """
                UPDATE machines
                SET machine_key = ?, gpu_model = ?, gpu_vram_gb = ?, cpu_cores = ?, ram_gb = ?,
                    status = 'idle', registered_at = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (
                    machine_key,
                    payload.gpu_model,
                    payload.gpu_vram_gb,
                    payload.cpu_cores,
                    payload.ram_gb,
                    current_time,
                    current_time,
                    machine_id,
                ),
            )
        else:
            machine_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO machines (
                    id, machine_key, gpu_model, gpu_vram_gb, cpu_cores, ram_gb, status, registered_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'idle', ?, ?)
                """,
                (
                    machine_id,
                    machine_key,
                    payload.gpu_model,
                    payload.gpu_vram_gb,
                    payload.cpu_cores,
                    payload.ram_gb,
                    current_time,
                    current_time,
                ),
            )
        conn.commit()

    return {"machine_id": machine_id}


@app.put("/machines/{machine_id}/available")
def mark_machine_available(machine_id: str) -> dict[str, bool]:
    with lock:
        machine = conn.execute("SELECT * FROM machines WHERE id = ?", (machine_id,)).fetchone()
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")
        conn.execute(
            "UPDATE machines SET status = 'available', last_seen_at = ? WHERE id = ?",
            (now_iso(), machine_id),
        )
        conn.commit()
    return {"success": True}


@app.put("/machines/{machine_id}/idle")
def mark_machine_idle(machine_id: str) -> dict[str, bool]:
    with lock:
        conn.execute(
            "UPDATE machines SET status = 'idle', last_seen_at = ? WHERE id = ?",
            (now_iso(), machine_id),
        )
        conn.commit()
    return {"success": True}


@app.get("/machines")
def list_available_machines() -> list[dict[str, Any]]:
    with lock:
        rows = conn.execute(
            "SELECT * FROM machines WHERE status = 'available' ORDER BY gpu_vram_gb DESC"
        ).fetchall()
    return [row_to_dict(row) for row in rows]


@app.post("/jobs")
async def submit_job(
    machine_id: str = Form(...), blender_file: UploadFile = File(...)
) -> dict[str, str]:
    if not machine_id or not blender_file:
        raise HTTPException(status_code=400, detail="machine_id and blender_file are required")

    with lock:
        machine = conn.execute(
            "SELECT * FROM machines WHERE id = ? AND status = 'available'",
            (machine_id,),
        ).fetchone()
    if not machine:
        raise HTTPException(status_code=400, detail="Machine not available")

    job_id = str(uuid4())
    input_dir = JOBS_DIR / job_id / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    input_filename = sanitize_filename(blender_file.filename or "scene.blend")
    input_path = input_dir / input_filename
    with input_path.open("wb") as out_file:
        shutil.copyfileobj(blender_file.file, out_file)

    with lock:
        conn.execute(
            """
            INSERT INTO jobs (id, machine_id, input_filename, status, output_files, submitted_at)
            VALUES (?, ?, ?, 'pending', '[]', ?)
            """,
            (job_id, machine_id, input_filename, now_iso()),
        )
        conn.execute("UPDATE machines SET status = 'processing' WHERE id = ?", (machine_id,))
        conn.commit()

    return {"job_id": job_id, "status": "pending"}


@app.get("/jobs/next-for-machine/{machine_id}")
def get_next_job_for_machine(machine_id: str, request: Request) -> dict[str, Any] | None:
    with lock:
        conn.execute(
            "UPDATE machines SET last_seen_at = ? WHERE id = ?",
            (now_iso(), machine_id),
        )
        conn.commit()
        job = conn.execute(
            """
            SELECT * FROM jobs
            WHERE machine_id = ? AND status = 'pending'
            ORDER BY submitted_at ASC
            LIMIT 1
            """,
            (machine_id,),
        ).fetchone()
    if not job:
        return None

    job_data = row_to_dict(job)
    encoded_name = quote(job_data["input_filename"])
    base = str(request.base_url).rstrip("/")
    job_data["input_url"] = f"{base}/jobs/{job_data['id']}/input/{encoded_name}"
    return job_data


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    with lock:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    job_data = row_to_dict(row)
    job_data["output_files"] = parse_output_files(job_data.get("output_files"))
    return job_data


@app.put("/jobs/{job_id}/status")
def update_job_status(job_id: str, payload: UpdateJobStatusPayload) -> dict[str, bool]:
    with lock:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        job_data = row_to_dict(job)
        completed_at = job_data.get("completed_at")
        if payload.status in ("done", "failed"):
            completed_at = now_iso()
            conn.execute(
                "UPDATE machines SET status = 'available', last_seen_at = ? WHERE id = ?",
                (completed_at, job_data["machine_id"]),
            )

        output_files_json = job_data["output_files"]
        if payload.output_files is not None:
            output_files_json = json.dumps(payload.output_files)

        conn.execute(
            """
            UPDATE jobs
            SET status = ?, completed_at = ?, error = ?, output_files = ?
            WHERE id = ?
            """,
            (
                payload.status,
                completed_at,
                payload.error,
                output_files_json,
                job_id,
            ),
        )
        conn.commit()

    return {"success": True}


@app.post("/jobs/{job_id}/output")
async def upload_job_output(job_id: str, files: list[UploadFile] = File(...)) -> dict[str, Any]:
    with lock:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    output_dir = JOBS_DIR / job_id / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    uploaded_names: list[str] = []
    for file in files:
        filename = sanitize_filename(file.filename or "output.bin")
        destination = output_dir / filename
        with destination.open("wb") as out_file:
            shutil.copyfileobj(file.file, out_file)
        uploaded_names.append(filename)

    existing = parse_output_files(job["output_files"])
    merged = list(dict.fromkeys(existing + uploaded_names))

    with lock:
        conn.execute(
            "UPDATE jobs SET output_files = ? WHERE id = ?",
            (json.dumps(merged), job_id),
        )
        conn.commit()

    return {"success": True, "files": merged}


@app.get("/jobs/{job_id}/input/{filename}")
def download_job_input_file(job_id: str, filename: str) -> FileResponse:
    safe_name = sanitize_filename(filename)
    input_file_path = JOBS_DIR / job_id / "input" / safe_name
    if not input_file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=input_file_path, filename=safe_name)


@app.get("/jobs/{job_id}/output/{filename}")
def download_job_output_file(job_id: str, filename: str) -> FileResponse:
    safe_name = sanitize_filename(filename)
    output_file_path = JOBS_DIR / job_id / "output" / safe_name
    if not output_file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=output_file_path, filename=safe_name)


@app.get("/jobs/{job_id}/download", response_model=None)
def download_job_output_archive(job_id: str) -> Response:
    with lock:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Job not complete yet")

    files = parse_output_files(job["output_files"])
    if not files:
        raise HTTPException(status_code=404, detail="No output files found")

    output_dir = JOBS_DIR / job_id / "output"
    existing_files = [output_dir / sanitize_filename(name) for name in files]
    existing_files = [path for path in existing_files if path.exists()]
    if not existing_files:
        raise HTTPException(status_code=404, detail="No output files found")

    if len(existing_files) == 1:
        single = existing_files[0]
        return FileResponse(path=single, filename=single.name)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for file_path in existing_files:
            zip_file.write(file_path, arcname=file_path.name)
    zip_buffer.seek(0)

    headers = {"Content-Disposition": f"attachment; filename=job_{job_id}_output.zip"}
    return StreamingResponse(zip_buffer, media_type="application/zip", headers=headers)
