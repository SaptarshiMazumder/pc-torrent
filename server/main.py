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
from blend_parser import parse_upload, BlendParseError

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
    machine_type: str = "windows"  # "windows" or "linux_ssh"


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
                os_version = %s, nvidia_driver = %s, machine_type = %s,
                status = 'idle', registered_at = %s, last_seen_at = %s
            WHERE id = %s
            """,
            (
                machine_key, payload.gpu_model, payload.gpu_vram_gb,
                payload.cpu_cores, payload.ram_gb,
                payload.os_version, payload.nvidia_driver, payload.machine_type,
                current_time, current_time, machine_id,
            ),
        )
    else:
        machine_id = str(uuid4())
        execute(
            """
            INSERT INTO machines (
                id, machine_key, gpu_model, gpu_vram_gb, cpu_cores, ram_gb,
                os_version, nvidia_driver, machine_type, status, registered_at, last_seen_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'idle', %s, %s)
            """,
            (
                machine_id, machine_key, payload.gpu_model, payload.gpu_vram_gb,
                payload.cpu_cores, payload.ram_gb,
                payload.os_version, payload.nvidia_driver, payload.machine_type,
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


@app.put("/machines/{machine_id}/heartbeat")
def heartbeat_machine(machine_id: str) -> dict[str, bool]:
    machine = query_one("SELECT id FROM machines WHERE id = %s", (machine_id,))
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    execute(
        "UPDATE machines SET last_seen_at = %s WHERE id = %s",
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

    # For render group jobs, the input file lives under the group's R2 path
    if job.get("group_id"):
        job["input_url"] = f"{base}/render-groups/{job['group_id']}/input/{job['input_filename']}"
    else:
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


@app.get("/render-groups/{group_id}/input/{filename}")
def download_render_group_input_file(group_id: str, filename: str):
    """Serve input file for render group jobs (shared across all tasks)."""
    safe_name = sanitize_filename(filename)
    r2_key = f"jobs/{group_id}/input/{safe_name}"
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
# Render Groups (distributed multi-machine rendering)
# -----------------------------------------------

FAILOVER_STALE_SECONDS = 30  # longer than heartbeat to avoid false positives


def compute_power_score(machine: dict) -> float:
    """Compute a rendering power score from machine specs (GPU-weighted)."""
    vram = machine.get("gpu_vram_gb") or 0
    cores = machine.get("cpu_cores") or 0
    ram = machine.get("ram_gb") or 0
    return (vram * 4) + (cores * 1) + (ram * 0.3)


def distribute_frames(
    total_frames: int,
    frame_start: int,
    frame_end: int,
    frame_step: int,
    machines: list[dict],
) -> list[dict]:
    """Split frames across machines proportionally to their power scores."""
    scores = [(m, compute_power_score(m)) for m in machines]
    total_score = sum(s for _, s in scores)
    if total_score <= 0:
        total_score = len(machines)
        scores = [(m, 1.0) for m in machines]

    assignments = []
    current_frame = frame_start

    for i, (machine, score) in enumerate(scores):
        if i == len(scores) - 1:
            chunk_end = frame_end
        else:
            share = score / total_score
            chunk_frames = max(1, round(total_frames * share))
            chunk_end = current_frame + (chunk_frames - 1) * frame_step
            chunk_end = min(chunk_end, frame_end)

        chunk_total = ((chunk_end - current_frame) // frame_step) + 1 if chunk_end >= current_frame else 0
        assignments.append({
            "machine_id": machine["id"],
            "gpu_model": machine.get("gpu_model", "Unknown"),
            "gpu_vram_gb": machine.get("gpu_vram_gb", 0),
            "cpu_cores": machine.get("cpu_cores", 0),
            "ram_gb": machine.get("ram_gb", 0),
            "frame_start": current_frame,
            "frame_end": chunk_end,
            "frame_step": frame_step,
            "total_frames": chunk_total,
            "power_score": round(score, 1),
        })
        current_frame = chunk_end + frame_step

    return assignments


def serialize_render_group_task(job: dict, machine: dict | None = None) -> dict:
    """Serialize a job within a render group into a task dict for the API."""
    total_frames = job.get("total_frames")
    rendered_frames = max(0, job.get("rendered_frames") or 0)
    if total_frames and total_frames > 0:
        rendered_frames = min(rendered_frames, total_frames)

    return {
        "job_id": job["id"],
        "machine_id": job["machine_id"],
        "machine_gpu": machine["gpu_model"] if machine else "Unknown",
        "machine_vram": machine.get("gpu_vram_gb", 0) if machine else 0,
        "frame_start": job.get("frame_start"),
        "frame_end": job.get("frame_end"),
        "total_frames": total_frames,
        "rendered_frames": rendered_frames,
        "progress_pct": compute_progress_pct(
            job.get("status", ""), rendered_frames, total_frames
        ),
        "status": job.get("status", "pending"),
        "error": job.get("error"),
    }


def _check_failover(group_id: str, tasks_raw: list[dict]):
    """Check for stale machines in a render group and reassign their work.

    Called during polling. Mutates the database if reassignment happens.
    Returns list of any newly created job IDs.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=FAILOVER_STALE_SECONDS)).isoformat()
    new_job_ids = []

    # Collect machines in this group for power scoring
    machine_ids_in_group = list({t["machine_id"] for t in tasks_raw})
    machines_map = {}
    for mid in machine_ids_in_group:
        m = query_one("SELECT * FROM machines WHERE id = %s", (mid,))
        if m:
            machines_map[mid] = m

    for task in tasks_raw:
        if task["status"] != "running":
            continue

        machine = machines_map.get(task["machine_id"])
        if not machine:
            continue

        last_seen = machine.get("last_seen_at") or ""
        if last_seen >= cutoff:
            continue

        # Machine is stale — mark job as failed
        execute(
            "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
            ("Machine went offline", now_iso(), task["id"]),
        )

        # Calculate remaining frames
        rendered = max(0, task.get("rendered_frames") or 0)
        step = task.get("frame_step") or 1
        new_start = task["frame_start"] + rendered * step
        new_end = task["frame_end"]

        if new_start > new_end:
            continue  # all frames were already rendered

        # Find the most powerful non-failed machine in the group
        best_machine = None
        best_score = -1
        for mid, m in machines_map.items():
            if mid == task["machine_id"]:
                continue
            score = compute_power_score(m)
            if score > best_score:
                best_score = score
                best_machine = m

        if not best_machine:
            continue

        # Create reassignment job
        new_job_id = str(uuid4())
        new_total = ((new_end - new_start) // step) + 1
        execute(
            """
            INSERT INTO jobs (id, machine_id, group_id, input_filename, status,
                              total_frames, rendered_frames, output_files,
                              frame_start, frame_end, frame_step, submitted_at)
            VALUES (%s, %s, %s, %s, 'pending', %s, 0, '[]', %s, %s, %s, %s)
            """,
            (
                new_job_id, best_machine["id"], group_id,
                task["input_filename"], new_total,
                new_start, new_end, step, now_iso(),
            ),
        )
        new_job_ids.append(new_job_id)

    return new_job_ids


class CreateRenderGroupPayload(BaseModel):
    machine_ids: list[str]
    filename: str


@app.post("/render-groups/create")
def create_render_group(payload: CreateRenderGroupPayload) -> dict[str, Any]:
    """Create a render group for distributed rendering across multiple machines."""
    if not payload.machine_ids:
        raise HTTPException(status_code=400, detail="At least one machine required")

    input_filename = sanitize_filename(payload.filename)
    validate_job_input_filename(input_filename)

    # Validate all machines exist and are available
    for mid in payload.machine_ids:
        machine = query_one(
            "SELECT id FROM machines WHERE id = %s AND status = 'available'",
            (mid,),
        )
        if not machine:
            raise HTTPException(
                status_code=400,
                detail=f"Machine {mid[:8]}... is not available",
            )

    group_id = str(uuid4())
    r2_key = f"jobs/{group_id}/input/{input_filename}"
    upload_url = storage.generate_presigned_upload_url(r2_key)

    execute(
        """
        INSERT INTO render_groups (id, input_filename, r2_input_key, total_frames,
                                    frame_start, frame_end, frame_step, status, submitted_at)
        VALUES (%s, %s, %s, 0, 1, 1, 1, 'uploading', %s)
        """,
        (group_id, input_filename, r2_key, now_iso()),
    )

    return {
        "group_id": group_id,
        "upload_url": upload_url,
        "r2_key": r2_key,
        "machine_ids": payload.machine_ids,
    }


class ConfirmRenderGroupPayload(BaseModel):
    machine_ids: list[str]
    frame_start: int | None = None
    frame_end: int | None = None
    frame_step: int | None = None


@app.post("/render-groups/{group_id}/confirm-upload")
def confirm_render_group_upload(
    group_id: str,
    payload: ConfirmRenderGroupPayload,
    request: Request,
) -> dict[str, Any]:
    """Confirm upload, parse .blend, distribute frames, and create jobs."""
    group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")
    if group["status"] not in ("uploading", "pending"):
        raise HTTPException(status_code=400, detail="Render group not in uploading state")

    r2_key = group["r2_input_key"]
    if not storage.file_exists(r2_key):
        raise HTTPException(status_code=400, detail="File not found in storage")

    # If manual frame range provided, use it directly
    if payload.frame_start is not None and payload.frame_end is not None:
        frame_start = payload.frame_start
        frame_end = payload.frame_end
        frame_step = payload.frame_step or 1
    else:
        # Try to auto-parse .blend
        try:
            file_data = storage.download_file(r2_key)
            frame_info = parse_upload(file_data, group["input_filename"])
            frame_start = frame_info["frame_start"]
            frame_end = frame_info["frame_end"]
            frame_step = frame_info["frame_step"]
        except BlendParseError as e:
            # Can't parse — ask client for manual frame range
            execute(
                "UPDATE render_groups SET status = 'pending' WHERE id = %s",
                (group_id,),
            )
            return {
                "group_id": group_id,
                "needs_frame_input": True,
                "parse_error": str(e),
            }
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to analyze uploaded file")

    frame_step = max(1, frame_step)
    total_frames = ((frame_end - frame_start) // frame_step) + 1 if frame_end >= frame_start else 0

    if total_frames <= 0:
        raise HTTPException(status_code=400, detail="No renderable frames found in .blend file")

    # Update group
    execute(
        """
        UPDATE render_groups
        SET total_frames = %s, frame_start = %s, frame_end = %s, frame_step = %s,
            status = 'pending'
        WHERE id = %s
        """,
        (total_frames, frame_start, frame_end, frame_step, group_id),
    )

    # Fetch full machine data for power scoring
    machines = []
    for mid in payload.machine_ids:
        m = query_one("SELECT * FROM machines WHERE id = %s", (mid,))
        if not m:
            raise HTTPException(status_code=400, detail=f"Machine {mid[:8]}... not found")
        machines.append(m)

    # Distribute frames
    assignments = distribute_frames(total_frames, frame_start, frame_end, frame_step, machines)

    # Create individual jobs
    tasks = []
    for a in assignments:
        job_id = str(uuid4())
        execute(
            """
            INSERT INTO jobs (id, machine_id, group_id, input_filename, status,
                              total_frames, rendered_frames, output_files,
                              frame_start, frame_end, frame_step, submitted_at)
            VALUES (%s, %s, %s, %s, 'pending', %s, 0, '[]', %s, %s, %s, %s)
            """,
            (
                job_id, a["machine_id"], group_id, group["input_filename"],
                a["total_frames"], a["frame_start"], a["frame_end"], a["frame_step"],
                now_iso(),
            ),
        )
        # Mark machine as processing
        execute(
            "UPDATE machines SET status = 'processing' WHERE id = %s",
            (a["machine_id"],),
        )
        tasks.append({
            "job_id": job_id,
            "machine_id": a["machine_id"],
            "machine_gpu": a["gpu_model"],
            "machine_vram": a["gpu_vram_gb"],
            "frame_start": a["frame_start"],
            "frame_end": a["frame_end"],
            "total_frames": a["total_frames"],
            "rendered_frames": 0,
            "progress_pct": None,
            "status": "pending",
            "power_score": a["power_score"],
            "error": None,
        })

    return {
        "group_id": group_id,
        "status": "pending",
        "input_filename": group["input_filename"],
        "total_frames": total_frames,
        "frame_start": frame_start,
        "frame_end": frame_end,
        "frame_step": frame_step,
        "tasks": tasks,
    }


@app.get("/render-groups/{group_id}")
def get_render_group(group_id: str) -> dict[str, Any]:
    """Get render group status with per-task progress."""
    group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")

    jobs = query_all(
        "SELECT * FROM jobs WHERE group_id = %s ORDER BY frame_start ASC",
        (group_id,),
    )

    # Check for failover (stale machines with running tasks)
    if group["status"] in ("pending", "running"):
        _check_failover(group_id, jobs)
        # Re-fetch jobs after potential reassignment
        jobs = query_all(
            "SELECT * FROM jobs WHERE group_id = %s ORDER BY frame_start ASC",
            (group_id,),
        )

    # Build tasks with machine info
    tasks = []
    for job in jobs:
        machine = query_one("SELECT * FROM machines WHERE id = %s", (job["machine_id"],))
        tasks.append(serialize_render_group_task(job, machine))

    # Compute overall status
    statuses = [j["status"] for j in jobs]
    total_rendered = sum(t["rendered_frames"] or 0 for t in tasks)
    total_frames = group["total_frames"] or 0
    no_active = not any(s in ("pending", "running") for s in statuses)

    if (all(s == "done" for s in statuses) or
            (no_active and any(s == "done" for s in statuses) and total_frames > 0 and total_rendered >= total_frames)):
        overall_status = "done"
        if group["status"] != "done":
            execute(
                "UPDATE render_groups SET status = 'done', completed_at = %s WHERE id = %s",
                (now_iso(), group_id),
            )
    elif any(s == "running" for s in statuses):
        overall_status = "running"
        if group["status"] == "pending":
            execute(
                "UPDATE render_groups SET status = 'running' WHERE id = %s",
                (group_id,),
            )
    elif no_active and all(s == "failed" for s in statuses):
        overall_status = "failed"
        if group["status"] != "failed":
            execute(
                "UPDATE render_groups SET status = 'failed', completed_at = %s WHERE id = %s",
                (now_iso(), group_id),
            )
    else:
        overall_status = group["status"]

    # Aggregate progress
    overall_pct = None
    if total_frames > 0:
        overall_pct = round(min(100.0, total_rendered / total_frames * 100), 1)
    if overall_status == "done":
        overall_pct = 100.0

    return {
        "group_id": group_id,
        "status": overall_status,
        "input_filename": group["input_filename"],
        "total_frames": total_frames,
        "frame_start": group["frame_start"],
        "frame_end": group["frame_end"],
        "frame_step": group["frame_step"],
        "submitted_at": group["submitted_at"],
        "completed_at": group.get("completed_at"),
        "error": group.get("error"),
        "overall_rendered_frames": total_rendered,
        "overall_progress_pct": overall_pct,
        "tasks": tasks,
    }


@app.get("/render-groups/{group_id}/download")
def download_render_group_output(group_id: str):
    """Download all output files from a completed render group as a zip."""
    group = query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="Render group not found")

    jobs = query_all(
        "SELECT * FROM jobs WHERE group_id = %s AND status = 'done' ORDER BY frame_start ASC",
        (group_id,),
    )

    # Check if all non-failed tasks are done
    all_jobs = query_all("SELECT status FROM jobs WHERE group_id = %s", (group_id,))
    pending_or_running = [j for j in all_jobs if j["status"] in ("pending", "running")]
    if pending_or_running:
        raise HTTPException(status_code=400, detail="Render group has tasks still in progress")

    # Collect all output files from done jobs
    all_files: list[tuple[str, str]] = []  # (r2_key, filename)
    for job in jobs:
        files = parse_output_files(job["output_files"])
        for fname in files:
            r2_key = f"jobs/{job['id']}/output/{fname}"
            all_files.append((r2_key, fname))

    if not all_files:
        raise HTTPException(status_code=404, detail="No output files found")

    # Single file → redirect
    if len(all_files) == 1:
        r2_key, fname = all_files[0]
        url = storage.generate_presigned_url(r2_key, download_name=fname)
        return RedirectResponse(url=url)

    # Multiple files → zip
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for r2_key, fname in all_files:
            try:
                data = storage.download_file(r2_key)
                zf.writestr(fname, data)
            except Exception:
                continue
    zip_buffer.seek(0)

    headers = {
        "Content-Disposition": f"attachment; filename=render_{group_id[:8]}_output.zip"
    }
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
