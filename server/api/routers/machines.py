from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from api.schemas.machine import RegisterMachinePayload
from scheduling.fleet import fleet
from models.value_objects import now_iso
from firebase_auth import get_current_user
from infrastructure.db import execute, query_one

router = APIRouter(tags=["machines"])


@router.post("/machines/register")
def register_machine(
    payload: RegisterMachinePayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, str]:
    from uuid import uuid4

    if not payload.gpu_model or payload.gpu_vram_gb is None:
        raise HTTPException(status_code=400, detail="Missing required fields")

    machine_key = payload.machine_key.strip() if payload.machine_key else None
    user_id = current_user["uid"]
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
            SET machine_key = %s, gpu_model = %s, gpu_vram_gb = %s, cpu_cores = %s,
                ram_gb = %s, os_version = %s, nvidia_driver = %s, machine_type = %s,
                status = 'idle', registered_at = %s, last_seen_at = %s, user_id = %s
            WHERE id = %s
            """,
            (
                machine_key, payload.gpu_model, payload.gpu_vram_gb,
                payload.cpu_cores, payload.ram_gb,
                payload.os_version, payload.nvidia_driver, payload.machine_type,
                current_time, current_time, user_id, machine_id,
            ),
        )
    else:
        machine_id = str(uuid4())
        execute(
            """
            INSERT INTO machines (
                id, machine_key, gpu_model, gpu_vram_gb, cpu_cores, ram_gb,
                os_version, nvidia_driver, machine_type, status,
                registered_at, last_seen_at, user_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'idle', %s, %s, %s)
            """,
            (
                machine_id, machine_key, payload.gpu_model, payload.gpu_vram_gb,
                payload.cpu_cores, payload.ram_gb,
                payload.os_version, payload.nvidia_driver, payload.machine_type,
                current_time, current_time, user_id,
            ),
        )

    return {"machine_id": machine_id}


@router.put("/machines/{machine_id}/available")
def mark_machine_available(machine_id: str) -> dict[str, bool]:
    machine = query_one("SELECT id FROM machines WHERE id = %s", (machine_id,))
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    execute(
        "UPDATE machines SET status = 'available', last_seen_at = %s WHERE id = %s",
        (now_iso(), machine_id),
    )
    return {"success": True}


@router.put("/machines/{machine_id}/idle")
def mark_machine_idle(machine_id: str) -> dict[str, bool]:
    execute(
        "UPDATE machines SET status = 'idle', last_seen_at = %s WHERE id = %s",
        (now_iso(), machine_id),
    )
    return {"success": True}


@router.put("/machines/{machine_id}/heartbeat")
def heartbeat_machine(machine_id: str) -> dict[str, bool]:
    machine = query_one("SELECT id FROM machines WHERE id = %s", (machine_id,))
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    execute(
        "UPDATE machines SET last_seen_at = %s WHERE id = %s",
        (now_iso(), machine_id),
    )
    return {"success": True}


@router.get("/machines")
def list_available_machines() -> list[dict[str, Any]]:
    return fleet.get_available_machines()
