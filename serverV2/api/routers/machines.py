"""Machine management routes — thin controllers."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from serverV2.api.dependencies import get_current_user
from serverV2.api.schemas.machine import RegisterMachinePayload
from serverV2.services.machines.service import MachineService, MachineServiceError

router = APIRouter(tags=["machines"])

_svc: MachineService | None = None
_machine_repo = None


def init(service: MachineService, machine_repo=None) -> None:
    global _svc, _machine_repo
    _svc = service
    _machine_repo = machine_repo


def _get() -> MachineService:
    if _svc is None:
        raise HTTPException(500, "MachineService not initialized")
    return _svc


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


@router.put("/machines/{machine_id}/heartbeat")
def heartbeat(machine_id: str):
    try:
        return _get().heartbeat(machine_id)
    except MachineServiceError as e:
        raise HTTPException(e.status, e.message)


@router.get("/machines")
def list_machines():
    if _machine_repo is not None:
        return _get().list_available(_machine_repo)
    return _get().list_all()
