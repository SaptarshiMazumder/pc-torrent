"""Debug/monitoring endpoints — instance state inspection."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["debug"])

_vast_registry = None
_modal_registry = None


def init(vast_registry=None, modal_registry=None) -> None:
    global _vast_registry, _modal_registry
    _vast_registry = vast_registry
    _modal_registry = modal_registry


@router.get("/vast/instances")
def vast_instances():
    if _vast_registry is None:
        return []
    return _vast_registry.get_all()


@router.get("/modal/instances")
def modal_instances():
    if _modal_registry is None:
        return []
    return _modal_registry.get_all()
