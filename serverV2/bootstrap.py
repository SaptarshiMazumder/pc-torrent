"""Bootstrap — wires all components together via composition.

Call ``build()`` once at startup to get a fully-configured Container.
Every dependency is explicit; nothing is a hidden global.
"""

from __future__ import annotations

from serverV2.callbacks.failure_handler import FailureHandler
from serverV2.callbacks.router import CallbackRouter
from serverV2.callbacks.success_handler import SuccessHandler
from serverV2.config import AppConfig
from serverV2.core.enums import CallbackOutcome
from serverV2.fleets.community.strategy import CommunityStrategy
from serverV2.fleets.instance_registry import InstanceRegistry
from serverV2.fleets.modal.callback_handler import ModalCallbackHandler
from serverV2.fleets.modal.client import ModalClient
from serverV2.fleets.modal.machine_registrar import ModalMachineRegistrar
from serverV2.fleets.modal.recovery import ModalRecovery
from serverV2.fleets.modal.strategy import ModalFleetStrategy
from serverV2.fleets.registry import FleetRegistry
from serverV2.fleets.status_aggregator import InstanceStatusAggregator
from serverV2.fleets.status_provider import ModalStatusProvider, VastStatusProvider
from serverV2.fleets.vast.callback_handler import VastCallbackHandler
from serverV2.fleets.vast.client import VastClient
from serverV2.fleets.vast.machine_registrar import VastMachineRegistrar
from serverV2.fleets.vast.recovery import VastRecovery
from serverV2.fleets.vast.strategy import VastFleetStrategy
from serverV2.orchestrator.blend_url_resolver import BlendUrlResolver
from serverV2.orchestrator.dispatch_queue import DispatchQueueManager
from serverV2.orchestrator.dispatcher import Dispatcher
from serverV2.orchestrator.frame_allocator import FrameAllocator
from serverV2.orchestrator.orchestrator import RenderOrchestrator
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.machine_repository import MachineRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository
from serverV2.repositories.user_input_file_repository import UserInputFileRepository
from serverV2.scanner.failover_scanner import FailoverScanner
from serverV2.services.assets.service import AssetService
from serverV2.services.jobs.service import JobService
from serverV2.services.machines.service import MachineService
from serverV2.services.render_groups.service import RenderGroupService
from serverV2.services.upload.coordinator import UploadCoordinator


class Container:
    """Holds all wired-up components.  Created once at boot."""

    def __init__(
        self,
        config: AppConfig,
        orchestrator: RenderOrchestrator,
        fleet_registry: FleetRegistry,
        failover_scanner: FailoverScanner,
        job_repo: JobRepository,
        machine_repo: MachineRepository,
        group_repo: RenderGroupRepository,
        asset_repo: UserInputFileRepository,
        upload_coordinator: UploadCoordinator,
        render_group_service: RenderGroupService,
        job_service: JobService,
        machine_service: MachineService,
        asset_service: AssetService,
        vast_registrar: VastMachineRegistrar,
        modal_registrar: ModalMachineRegistrar,
        vast_recovery: VastRecovery,
        modal_recovery: ModalRecovery,
        status_aggregator: InstanceStatusAggregator,
    ) -> None:
        self.config = config
        self.orchestrator = orchestrator
        self.fleet_registry = fleet_registry
        self.failover_scanner = failover_scanner
        self.job_repo = job_repo
        self.machine_repo = machine_repo
        self.group_repo = group_repo
        self.asset_repo = asset_repo
        self.upload_coordinator = upload_coordinator
        self.render_group_service = render_group_service
        self.job_service = job_service
        self.machine_service = machine_service
        self.asset_service = asset_service
        self.vast_registrar = vast_registrar
        self.modal_registrar = modal_registrar
        self.vast_recovery = vast_recovery
        self.modal_recovery = modal_recovery
        self.status_aggregator = status_aggregator


def build(config: AppConfig | None = None) -> Container:
    """Compose the full object graph.  Pure wiring, no side effects."""

    cfg = config or AppConfig.from_env()

    # -- repositories --
    job_repo = JobRepository()
    machine_repo = MachineRepository(stale_seconds=cfg.machine_stale_seconds)
    group_repo = RenderGroupRepository()
    asset_repo = UserInputFileRepository()

    # -- fleet registry --
    registry = FleetRegistry()

    # -- blend URL resolver --
    blend_resolver = BlendUrlResolver(cfg)

    # -- instance registries (one per fleet, shared InstanceRegistry class) --
    modal_instance_registry = InstanceRegistry()
    vast_instance_registry = InstanceRegistry()

    # -- _on_failure: fleet monitors -> CallbackRouter (late-bound) --
    router_ref: list[CallbackRouter | None] = [None]

    def _on_failure(job_id: str, error: str) -> None:
        if router_ref[0]:
            router_ref[0].route(
                job_id=job_id,
                outcome=CallbackOutcome.FAILURE,
                error=error,
            )

    # -- vast fleet --
    vast_client = VastClient(cfg.vast)
    vast_callback = VastCallbackHandler(
        config=cfg.vast, client=vast_client,
        job_repo=job_repo, on_failure=_on_failure,
        registry=vast_instance_registry,
    )
    vast_strategy = VastFleetStrategy(
        config=cfg.vast, client=vast_client,
        callback_handler=vast_callback, job_repo=job_repo,
    )
    vast_registrar = VastMachineRegistrar(cfg.vast)
    vast_recovery = VastRecovery(
        config=cfg.vast, client=vast_client,
        callback_handler=vast_callback,
    )
    registry.register(vast_strategy)

    # -- modal fleet --
    modal_client = ModalClient(cfg.modal)
    modal_callback = ModalCallbackHandler(
        config=cfg.modal, client=modal_client,
        job_repo=job_repo, on_failure=_on_failure,
        registry=modal_instance_registry,
    )
    modal_strategy = ModalFleetStrategy(
        config=cfg.modal, client=modal_client,
        callback_handler=modal_callback, job_repo=job_repo,
    )
    modal_registrar = ModalMachineRegistrar(cfg.modal)
    modal_recovery = ModalRecovery(
        config=cfg.modal, callback_handler=modal_callback,
    )
    registry.register(modal_strategy)

    # -- community fleet --
    community_strategy = CommunityStrategy()
    registry.register(community_strategy)

    # -- allocation + dispatch --
    allocator = FrameAllocator(registry)
    dispatcher = Dispatcher(registry)

    # -- dispatch queue manager --
    queue_manager = DispatchQueueManager()

    # -- machine picker: returns available machines filtered to enabled fleets --
    enabled_types = registry.enabled_types

    def _machine_picker():
        machines = machine_repo.get_available()
        active = enabled_types()
        if active:
            machines = [m for m in machines if m.machine_type in active]
        return machines

    # -- callbacks --
    failure_handler = FailureHandler(job_repo)
    success_handler = SuccessHandler(job_repo, group_repo)
    callback_router = CallbackRouter(job_repo, success_handler, failure_handler)
    router_ref[0] = callback_router

    # -- orchestrator (facade) --
    orchestrator = RenderOrchestrator(
        frame_allocator=allocator,
        dispatcher=dispatcher,
        callback_router=callback_router,
        blend_url_resolver=blend_resolver,
        job_repo=job_repo,
        queue_manager=queue_manager,
        machine_picker=_machine_picker,
    )
    failure_handler.set_orchestrator(orchestrator)

    # -- failover scanner --
    scanner = FailoverScanner(
        job_repo=job_repo,
        group_repo=group_repo,
        callback_router=callback_router,
        failover_stale_seconds=cfg.failover_stale_seconds,
    )

    # -- status providers + aggregator --
    modal_status_provider = ModalStatusProvider(modal_instance_registry)
    vast_status_provider = VastStatusProvider(vast_instance_registry)
    status_aggregator = InstanceStatusAggregator()
    status_aggregator.register(modal_status_provider)
    status_aggregator.register(vast_status_provider)

    # -- application services --
    upload_coordinator = UploadCoordinator()

    render_group_service = RenderGroupService(
        group_repo=group_repo,
        job_repo=job_repo,
        machine_repo=machine_repo,
        asset_repo=asset_repo,
        orchestrator=orchestrator,
        fleet_registry=registry,
    )

    job_service = JobService(
        job_repo=job_repo,
        machine_repo=machine_repo,
    )

    machine_service = MachineService(vast_config=cfg.vast, modal_config=cfg.modal)

    asset_service = AssetService(asset_repo=asset_repo)

    return Container(
        config=cfg,
        orchestrator=orchestrator,
        fleet_registry=registry,
        failover_scanner=scanner,
        job_repo=job_repo,
        machine_repo=machine_repo,
        group_repo=group_repo,
        asset_repo=asset_repo,
        upload_coordinator=upload_coordinator,
        render_group_service=render_group_service,
        job_service=job_service,
        machine_service=machine_service,
        asset_service=asset_service,
        vast_registrar=vast_registrar,
        modal_registrar=modal_registrar,
        vast_recovery=vast_recovery,
        modal_recovery=modal_recovery,
        status_aggregator=status_aggregator,
    )
