"""Composition root for the render_group concept.

``build_render_group`` constructs render_group's use-cases and injects the
ports they depend on.  render_group's OWN adapters (repository, gateways, and
its anti-corruption adapters to the external billing/identity contexts) are
built here.  The SIBLING adapters -- chunk repository + output frames (from
render_group_chunk), allocation, fleet -- are built by their own concepts and
handed in as parameters; this bootstrap only wires render_group.

DORMANT: not called by the running app.  Construction runs no I/O (adapters
just store their handles), so it wires and imports cleanly even though every
use-case body still raises ``NotImplementedError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# render_group use-cases
from serverV2.rendering.render_group.application.cancel_render_group import CancelRenderGroup
from serverV2.rendering.render_group.application.confirm_upload import ConfirmUpload
from serverV2.rendering.render_group.application.create_render_group import CreateRenderGroup
from serverV2.rendering.render_group.application.delete_render_group import DeleteRenderGroup
from serverV2.rendering.render_group.application.estimate_group_cost import EstimateGroupCost
from serverV2.rendering.render_group.application.get_group_status import GetGroupStatus
from serverV2.rendering.render_group.application.get_outputs import GetOutputs
from serverV2.rendering.render_group.application.list_groups import ListGroups
from serverV2.rendering.render_group.application.on_credits_exhausted import OnCreditsExhausted
from serverV2.rendering.render_group.application.reconcile_render_group_status import (
    ReconcileRenderGroupStatus,
)

# ports render_group needs from siblings + external contexts (injected)
from serverV2.rendering.render_group.application.ports.allocation_service import IAllocationService
from serverV2.rendering.render_group.application.ports.chunk_repository import IChunkRepository
from serverV2.rendering.render_group.application.ports.credits_service import ICreditsService
from serverV2.rendering.render_group.application.ports.fleet_service import IFleetService
from serverV2.rendering.render_group.application.ports.output_frame_repository import (
    IOutputFrameRepository,
)
from serverV2.rendering.render_group.application.ports.user_service import IUserService

# render_group's OWN infrastructure adapters
from serverV2.rendering.render_group.infrastructure.group_status_gateway import GroupStatusGateway
from serverV2.rendering.render_group.infrastructure.render_group_repository import RenderGroupRepository
from serverV2.rendering.render_group.infrastructure.storage_gateway import StorageGateway


# ===========================================================================
# Containers -- plain bags of wired objects, zero logic.
# ===========================================================================

@dataclass(frozen=True)
class Orchestration:
    create: CreateRenderGroup
    confirm_upload: ConfirmUpload
    cancel: CancelRenderGroup
    delete: DeleteRenderGroup
    reconcile: ReconcileRenderGroupStatus


@dataclass(frozen=True)
class Retrieval:
    status: GetGroupStatus
    listing: ListGroups
    outputs: GetOutputs
    cost: EstimateGroupCost


@dataclass(frozen=True)
class Events:
    on_credits_exhausted: OnCreditsExhausted


@dataclass(frozen=True)
class RenderGroupContainer:
    orchestration: Orchestration
    retrieval: Retrieval
    events: Events


# ===========================================================================
# The concept's composition root.
# ===========================================================================

def build_render_group(
    *,
    db: Any,
    storage_client: Any,
    cache: Any,
    chunk_repo: IChunkRepository,             # from render_group_chunk concept
    output_frames: IOutputFrameRepository,    # from render_group_chunk concept
    allocation: IAllocationService,           # from allocation concept
    fleet: IFleetService,                     # from fleet concept
    credits: ICreditsService,                 # from billing (external context)
    users: IUserService,                      # from identity (external context)
) -> RenderGroupContainer:
    # render_group's OWN adapters
    group_repo = RenderGroupRepository(db)
    group_status = GroupStatusGateway(cache)
    storage = StorageGateway(storage_client)

    # the chokepoint first -- events depend on cancel, callbacks (elsewhere) on reconcile
    reconcile = ReconcileRenderGroupStatus(
        group_repo=group_repo,
        chunk_repo=chunk_repo,
        output_frames=output_frames,
        allocation=allocation,
        notifier=group_status,
    )

    orchestration = Orchestration(
        create=CreateRenderGroup(group_repo=group_repo, storage=storage, users=users),
        confirm_upload=ConfirmUpload(
            group_repo=group_repo, storage=storage, credits=credits, allocation=allocation
        ),
        cancel=CancelRenderGroup(
            group_repo=group_repo, fleet=fleet, credits=credits, notifier=group_status
        ),
        delete=DeleteRenderGroup(group_repo=group_repo, storage=storage),
        reconcile=reconcile,
    )
    retrieval = Retrieval(
        status=GetGroupStatus(
            group_repo=group_repo, chunk_repo=chunk_repo, output_frames=output_frames
        ),
        listing=ListGroups(group_repo=group_repo),
        outputs=GetOutputs(group_repo=group_repo, storage=storage, output_frames=output_frames),
        cost=EstimateGroupCost(chunk_repo=chunk_repo),
    )
    events = Events(
        on_credits_exhausted=OnCreditsExhausted(
            group_repo=group_repo, cancel_group=orchestration.cancel
        ),
    )
    return RenderGroupContainer(orchestration=orchestration, retrieval=retrieval, events=events)
