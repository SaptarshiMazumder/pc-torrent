import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel

from db import execute, init_db, query_all, query_one
import storage

MACHINE_STALE_SECONDS = 15

app = FastAPI(title="PC Rent Server")

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
    os_version: str | None = None
    nvidia_driver: str | None = None


class UpdateJobStatusPayload(BaseModel):
    status: str
    error: str | None = None
    output_files: list[str] | None = None


class UpdateJobProgressPayload(BaseModel):
    total_frames: int | None = None
    rendered_frames: int


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_output_files(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def compute_progress_pct(
    status: str,
    rendered_frames: int | None,
    total_frames: int | None,
) -> float | None:
    rendered = max(0, rendered_frames or 0)
    if total_frames and total_frames > 0:
        pct = rendered / total_frames * 100
        if status == "done":
            pct = 100.0
        return round(max(0.0, min(100.0, pct)), 1)
    if status == "done":
        return 100.0
    return None


def serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    output_files = parse_output_files(job.get("output_files"))
    total_frames = job.get("total_frames")
    rendered_frames = max(0, job.get("rendered_frames") or 0)
    if total_frames is not None and total_frames > 0:
        rendered_frames = min(rendered_frames, total_frames)

    return {
        **job,
        "output_files": output_files,
        "total_frames": total_frames,
        "rendered_frames": rendered_frames,
        "progress_pct": compute_progress_pct(
            job.get("status", ""),
            rendered_frames,
            total_frames,
        ),
    }


def sanitize_filename(name: str) -> str:
    from pathlib import Path
    safe = Path(name).name
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return safe


def validate_job_input_filename(name: str) -> None:
    from pathlib import Path
    ext = Path(name).suffix.lower()
    if ext not in {".blend", ".zip"}:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Upload a .blend file or a .zip project bundle.",
        )


@app.on_event("startup")
def on_startup() -> None:
    init_db()


# -----------------------------------------------
# Health
# -----------------------------------------------
@app.get("/")
def root() -> dict[str, str]:
    return {"status": "PC Rent server running"}


# -----------------------------------------------
# Machines
# -----------------------------------------------
@app.post("/machines/register")
def register_machine(payload: RegisterMachinePayload) -> dict[str, str]:
    if not payload.gpu_model or payload.gpu_vram_gb is None:
        raise HTTPException(status_code=400, detail="Missing required fields")

    machine_key = payload.machine_key.strip() if payload.machine_key else None
    current_time = now_iso()

    existing = None
    if machine_key:
        existing = query_one(
            "SELECT id FROM machines WHERE machine_key = %s", (machine_key,)
        )

    if existing:
        machine_id = existing["id"]
        execute(
            """
            UPDATE machines
            SET machine_key = %s, gpu_model = %s, gpu_vram_gb = %s, cpu_cores = %s, ram_gb = %s,
                os_version = %s, nvidia_driver = %s,
                status = 'idle', registered_at = %s, last_seen_at = %s
            WHERE id = %s
            """,
            (
                machine_key, payload.gpu_model, payload.gpu_vram_gb,
                payload.cpu_cores, payload.ram_gb,
                payload.os_version, payload.nvidia_driver,
                current_time, current_time, machine_id,
            ),
        )
    else:
        machine_id = str(uuid4())
        execute(
            """
            INSERT INTO machines (
                id, machine_key, gpu_model, gpu_vram_gb, cpu_cores, ram_gb,
                os_version, nvidia_driver, status, registered_at, last_seen_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'idle', %s, %s)
            """,
            (
                machine_id, machine_key, payload.gpu_model, payload.gpu_vram_gb,
                payload.cpu_cores, payload.ram_gb,
                payload.os_version, payload.nvidia_driver,
                current_time, current_time,
            ),
        )

    return {"machine_id": machine_id}


@app.put("/machines/{machine_id}/available")
def mark_machine_available(machine_id: str) -> dict[str, bool]:
    machine = query_one("SELECT id FROM machines WHERE id = %s", (machine_id,))
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    execute(
        "UPDATE machines SET status = 'available', last_seen_at = %s WHERE id = %s",
        (now_iso(), machine_id),
    )
    return {"success": True}


@app.put("/machines/{machine_id}/idle")
def mark_machine_idle(machine_id: str) -> dict[str, bool]:
    execute(
        "UPDATE machines SET status = 'idle', last_seen_at = %s WHERE id = %s",
        (now_iso(), machine_id),
    )
    return {"success": True}


@app.get("/machines")
def list_available_machines() -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=MACHINE_STALE_SECONDS)).isoformat()
    execute(
        """
        UPDATE machines
        SET status = 'idle'
        WHERE status = 'available' AND (last_seen_at IS NULL OR last_seen_at < %s)
        """,
        (cutoff,),
    )
    rows = query_all(
        """
        SELECT *
        FROM machines
        WHERE status = 'available' AND last_seen_at >= %s
        ORDER BY gpu_vram_gb DESC
        """,
        (cutoff,),
    )
    return rows


# -----------------------------------------------
# Jobs
# -----------------------------------------------
class RequestUploadPayload(BaseModel):
    machine_id: str
    filename: str


@app.post("/jobs/request-upload")
def request_upload(payload: RequestUploadPayload) -> dict[str, str]:
    """Get a presigned URL to upload directly to R2."""
    machine = query_one(
        "SELECT id FROM machines WHERE id = %s AND status = 'available'",
        (payload.machine_id,),
    )
    if not machine:
        raise HTTPException(status_code=400, detail="Machine not available")

    input_filename = sanitize_filename(payload.filename)
    validate_job_input_filename(input_filename)

    job_id = str(uuid4())
    r2_key = f"jobs/{job_id}/input/{input_filename}"
    upload_url = storage.generate_presigned_upload_url(r2_key)

    # Create job in 'uploading' state
    execute(
        """
        INSERT INTO jobs (id, machine_id, input_filename, status, output_files, submitted_at)
        VALUES (%s, %s, %s, 'uploading', '[]', %s)
        """,
        (job_id, payload.machine_id, input_filename, now_iso()),
    )

    return {"job_id": job_id, "upload_url": upload_url, "r2_key": r2_key}


@app.post("/jobs/{job_id}/confirm-upload")
def confirm_upload(job_id: str) -> dict[str, str]:
    """Confirm the file was uploaded to R2 and start the job."""
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "uploading":
        raise HTTPException(status_code=400, detail="Job not in uploading state")

    # Verify the file exists in R2
    r2_key = f"jobs/{job_id}/input/{job['input_filename']}"
    if not storage.file_exists(r2_key):
        raise HTTPException(status_code=400, detail="File not found in storage. Upload may have failed.")

    execute("UPDATE jobs SET status = 'pending' WHERE id = %s", (job_id,))
    execute("UPDATE machines SET status = 'processing' WHERE id = %s", (job["machine_id"],))

    return {"job_id": job_id, "status": "pending"}


@app.get("/jobs/next-for-machine/{machine_id}")
def get_next_job_for_machine(machine_id: str, request: Request) -> dict[str, Any] | None:
    execute(
        "UPDATE machines SET last_seen_at = %s WHERE id = %s",
        (now_iso(), machine_id),
    )
    job = query_one(
        """
        SELECT * FROM jobs
        WHERE machine_id = %s AND status = 'pending'
        ORDER BY submitted_at ASC
        LIMIT 1
        """,
        (machine_id,),
    )
    if not job:
        return None

    base = str(request.base_url).rstrip("/")
    job["input_url"] = f"{base}/jobs/{job['id']}/input/{job['input_filename']}"
    return job


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return serialize_job(job)


@app.put("/jobs/{job_id}/progress")
def update_job_progress(job_id: str, payload: UpdateJobProgressPayload) -> dict[str, bool]:
    job = query_one(
        "SELECT id, status, total_frames, rendered_frames FROM jobs WHERE id = %s",
        (job_id,),
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "running":
        raise HTTPException(status_code=409, detail="Job is not running")

    current_total = job.get("total_frames")
    incoming_total = payload.total_frames
    if incoming_total is not None and incoming_total < 0:
        raise HTTPException(status_code=400, detail="total_frames must be non-negative")
    if payload.rendered_frames < 0:
        raise HTTPException(status_code=400, detail="rendered_frames must be non-negative")

    next_total = current_total
    if incoming_total is not None:
        next_total = max(current_total or 0, incoming_total)

    next_rendered = max(job.get("rendered_frames") or 0, payload.rendered_frames)
    if next_total and next_total > 0:
        next_rendered = min(next_rendered, next_total)

    execute(
        """
        UPDATE jobs
        SET total_frames = %s, rendered_frames = %s
        WHERE id = %s
        """,
        (next_total, next_rendered, job_id),
    )

    return {"success": True}


@app.put("/jobs/{job_id}/status")
def update_job_status(job_id: str, payload: UpdateJobStatusPayload) -> dict[str, bool]:
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    completed_at = job.get("completed_at")
    total_frames = job.get("total_frames")
    rendered_frames = max(0, job.get("rendered_frames") or 0)
    if payload.status in ("done", "failed"):
        completed_at = now_iso()
        execute(
            "UPDATE machines SET status = 'available', last_seen_at = %s WHERE id = %s",
            (completed_at, job["machine_id"]),
        )
    if payload.status == "done":
        if total_frames and total_frames > 0:
            rendered_frames = total_frames

    output_files_json = job["output_files"]
    if payload.output_files is not None:
        output_files_json = json.dumps(payload.output_files)

    execute(
        """
        UPDATE jobs
        SET status = %s, completed_at = %s, error = %s, output_files = %s,
            rendered_frames = %s
        WHERE id = %s
        """,
        (
            payload.status,
            completed_at,
            payload.error,
            output_files_json,
            rendered_frames,
            job_id,
        ),
    )

    return {"success": True}


@app.post("/jobs/{job_id}/output")
async def upload_job_output(job_id: str, files: list[UploadFile] = File(...)) -> dict[str, Any]:
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    uploaded_names: list[str] = []
    for file in files:
        filename = sanitize_filename(file.filename or "output.bin")
        file_data = await file.read()
        r2_key = f"jobs/{job_id}/output/{filename}"
        storage.upload_file(r2_key, file_data)
        uploaded_names.append(filename)

    existing = parse_output_files(job["output_files"])
    merged = list(dict.fromkeys(existing + uploaded_names))

    execute(
        "UPDATE jobs SET output_files = %s WHERE id = %s",
        (json.dumps(merged), job_id),
    )

    return {"success": True, "files": merged}


@app.get("/jobs/{job_id}/input/{filename}")
def download_job_input_file(job_id: str, filename: str):
    safe_name = sanitize_filename(filename)
    r2_key = f"jobs/{job_id}/input/{safe_name}"
    if not storage.file_exists(r2_key):
        raise HTTPException(status_code=404, detail="File not found")
    url = storage.generate_presigned_url(r2_key, download_name=safe_name)
    return RedirectResponse(url=url)


@app.get("/jobs/{job_id}/output/{filename}")
def download_job_output_file(job_id: str, filename: str):
    safe_name = sanitize_filename(filename)
    r2_key = f"jobs/{job_id}/output/{safe_name}"
    if not storage.file_exists(r2_key):
        raise HTTPException(status_code=404, detail="File not found")
    url = storage.generate_presigned_url(r2_key, download_name=safe_name)
    return RedirectResponse(url=url)


@app.get("/jobs/{job_id}/download")
def download_job_output_archive(job_id: str):
    job = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Job not complete yet")

    files = parse_output_files(job["output_files"])
    if not files:
        raise HTTPException(status_code=404, detail="No output files found")

    # Single file → redirect to presigned URL
    if len(files) == 1:
        r2_key = f"jobs/{job_id}/output/{files[0]}"
        url = storage.generate_presigned_url(r2_key, download_name=files[0])
        return RedirectResponse(url=url)

    # Multiple files → zip them in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for filename in files:
            r2_key = f"jobs/{job_id}/output/{filename}"
            try:
                data = storage.download_file(r2_key)
                zf.writestr(filename, data)
            except Exception:
                continue
    zip_buffer.seek(0)

    headers = {"Content-Disposition": f"attachment; filename=job_{job_id}_output.zip"}
    return StreamingResponse(zip_buffer, media_type="application/zip", headers=headers)


# -----------------------------------------------
# Docker image distribution
# -----------------------------------------------
@app.get("/docker/image/version")
def get_docker_image_version() -> dict[str, str]:
    try:
        sha_data = storage.download_file("docker/pcrent-render.sha256")
        sha = sha_data.decode().strip().split()[0]
        return {"version": "v1.0.0", "sha256": sha}
    except Exception:
        raise HTTPException(status_code=404, detail="No image available")


@app.get("/docker/image")
def download_docker_image():
    if not storage.file_exists("docker/pcrent-render.tar.gz"):
        raise HTTPException(status_code=404, detail="Image not found")
    url = storage.generate_presigned_url("docker/pcrent-render.tar.gz", expires_in=7200)
    return RedirectResponse(url=url)


# -----------------------------------------------
# Desktop installer distribution
# -----------------------------------------------
@app.get("/releases/latest")
def download_latest_release():
    if not storage.file_exists("releases/PCRentAgent-Setup.exe"):
        raise HTTPException(status_code=404, detail="No release available")
    url = storage.generate_presigned_url("releases/PCRentAgent-Setup.exe", expires_in=7200)
    return RedirectResponse(url=url)
