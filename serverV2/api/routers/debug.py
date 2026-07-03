"""Instance status endpoints — live fleet monitoring for the frontend.

Reads from InstanceStatusAggregator (which collects all fleet providers).
Replaces the old debug-only registry endpoints.

Auth: requires a signed-in user (the desktop JobDetailView instances
drawer calls these with the user's Bearer token).  Previously these were
completely open, which exposed fleet topology to anyone with the URL.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from serverV2.api.dependencies import get_current_user
from serverV2.fleets.status_aggregator import InstanceStatusAggregator

router = APIRouter(tags=["instances"])

_aggregator: InstanceStatusAggregator | None = None


def init(aggregator: InstanceStatusAggregator) -> None:
    global _aggregator
    _aggregator = aggregator


@router.get("/instances")
def all_instances(_user: dict = Depends(get_current_user)):
    if _aggregator is None:
        return {}
    return _aggregator.get_all()


@router.get("/vast/instances")
def vast_instances(_user: dict = Depends(get_current_user)):
    if _aggregator is None:
        return []
    return _aggregator.get_by_fleet("vast_serverless")


@router.get("/modal/instances")
def modal_instances(_user: dict = Depends(get_current_user)):
    if _aggregator is None:
        return []
    return _aggregator.get_by_fleet("modal_serverless")
