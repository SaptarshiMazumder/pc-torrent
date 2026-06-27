"""Machine management routes — thin controllers."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from serverV2.api.dependencies import get_current_user
from serverV2.api.schemas.machine import (
    MachineCommitmentPayload,
    RegisterMachinePayload,
)
from serverV2.clients.allocation_client import AllocationClient
from serverV2.services.machines.service import MachineService, MachineServiceError

router = APIRouter(tags=["machines"])

_svc: MachineService | None = None
_allocation_client: AllocationClient | None = None


def init(service: MachineService, allocation_client: AllocationClient) -> None:
    global _svc, _allocation_client
    _svc = service
    _allocation_client = allocation_client


def _get() -> MachineService:
    if _svc is None:
        raise HTTPException(500, "MachineService not initialized")
    return _svc


def _get_allocation_client() -> AllocationClient:
    if _allocation_client is None:
        raise HTTPException(500, "AllocationClient not initialized")
    return _allocation_client


@router.post("/machines/register")
def register(payload: RegisterMachinePayload, user: dict = Depends(get_current_user)):
    try:
        return _get().register(payload, user["uid"])
    except MachineServiceError as e:
        raise HTTPException(e.status, e.message)


@router.put("/machines/{machine_id}/available")
def set_available(machine_id: str):
    try:
        return _get().set_available(machine_id)
    except MachineServiceError as e:
        raise HTTPException(e.status, e.message)


@router.put("/machines/{machine_id}/idle")
def set_idle(machine_id: str):
    try:
        return _get().set_idle(machine_id)
    except MachineServiceError as e:
        raise HTTPException(e.status, e.message)


@router.put("/machines/{machine_id}/commitment")
def set_commitment(
    machine_id: str,
    payload: MachineCommitmentPayload,
    user: dict = Depends(get_current_user),
):
    """Update a community machine's commitment window.

    - ``commitment_seconds > 0``: stamps ``commitment_end_at = now + value``
      so the planner sees the refreshed window on its next read.
    - ``commitment_seconds == 0``: snaps ``commitment_end_at`` to now so
      the planner immediately stops considering this machine.  Fired by
      the desktop agent on graceful disconnect.

    403 when the caller doesn't own the machine.
    """
    try:
        return _get().set_commitment(machine_id, payload.commitment_seconds, user["uid"])
    except MachineServiceError as e:
        raise HTTPException(e.status, e.message)


@router.get("/machines")
def list_machines():
    return _get().list_available()


@router.get("/machines/available")
def list_available_machines():
    """Fleet-availability snapshot the dispatcher will use on its next
    tick: community PCs + Vast offers + Modal capacities.  Reads through
    the same Redis cache the planner reads (60s TTL); on cache miss the
    rebuild path is real-time Vast marketplace probe + PG fallback for
    Modal/community.  UI and planner cannot disagree about what's
    eligible right now."""
    snapshot = _get_allocation_client().list_available_machines()
    return {
        "community": [
            {
                "id": m.id,
                "gpu_model": m.gpu_model,
                "vram_gb": m.vram_gb,
                "cpu_cores": m.cpu_cores,
                "ram_gb": m.ram_gb,
                "render_speed": m.render_speed,
                "status": m.status,
                "last_seen_at": m.last_seen_at,
                "price_per_hour": m.price_per_hour,
                "available_seconds": m.available_seconds,
            }
            for m in snapshot.community_available
        ],
        "vast": [
            {
                "fleet": c.fleet,
                "gpu_type": c.gpu_type,
                "label": c.label,
                "vram_gb": c.vram_gb,
                "cpu_cores": c.cpu_cores,
                "ram_gb": c.ram_gb,
                "render_speed": c.render_speed,
                "fleet_max_parallel": c.fleet_max_parallel,
                "price_per_hour": c.price_per_hour,
                "offer_id": c.offer_id,
                "cuda_version": c.cuda_version,
                "host_os": c.host_os,
                "available_seconds": c.available_seconds,
            }
            for c in snapshot.vast_available
        ],
        "modal": [
            {
                "fleet": c.fleet,
                "gpu_type": c.gpu_type,
                "label": c.label,
                "vram_gb": c.vram_gb,
                "cpu_cores": c.cpu_cores,
                "ram_gb": c.ram_gb,
                "render_speed": c.render_speed,
                "fleet_max_parallel": c.fleet_max_parallel,
                "price_per_hour": c.price_per_hour,
                "available_seconds": c.available_seconds,
            }
            for c in snapshot.modal_available
        ],
        "serverless_in_flight": dict(snapshot.serverless_in_flight),
    }
