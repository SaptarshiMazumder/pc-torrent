"""Bootstrap — wires all components together via composition.

Call ``build()`` once at startup to get a fully-configured Container.
Every dependency is explicit; nothing is a hidden global.

After Phase 1 of the allocator redesign, Modal and Vast no longer have
machine registrars — their endpoints come from ``config.json`` and the
allocator works against ``FleetCapability`` descriptors directly.  Only
the community fleet has DB-backed machine rows.
"""

from __future__ import annotations

from serverV2.callbacks.failure_handler import FailureHandler
from serverV2.callbacks.router import CallbackRouter
from serverV2.callbacks.success_handler import SuccessHandler
from serverV2.config import AppConfig
from serverV2.core.enums import CallbackOutcome
from serverV2.core.models import AvailableResources, FleetCapability
from serverV2.fleets.community.community_monitor import CommunityMonitor
from serverV2.fleets.community.strategy import CommunityStrategy
from serverV2.fleets.instance_registry import InstanceRegistry
from serverV2.fleets.modal.callback import ModalCallbackHandler
from serverV2.fleets.modal.client import ModalClient
from serverV2.fleets.modal.endpoint_validator import validate_modal_endpoints
from serverV2.fleets.modal.recovery import ModalRecovery
from serverV2.fleets.modal.strategy import ModalFleetStrategy
from serverV2.fleets.registry import FleetRegistry
from serverV2.fleets.shared.pre_render_stall_detector import (
    IPreRenderStallDetector,
    PreRenderStallDetectorBuilder,
)
from serverV2.fleets.status_aggregator import InstanceStatusAggregator
from serverV2.fleets.status_provider import ModalStatusProvider, VastStatusProvider
from serverV2.fleets.vast.callback import VastCallbackHandler
from serverV2.fleets.vast.client import VastClient
from serverV2.fleets.vast.recovery import VastRecovery
from serverV2.fleets.vast.strategy import VastFleetStrategy
from serverV2.infrastructure import storage
from serverV2.infrastructure.redis_client import RedisClient
from serverV2.orchestrator.allocation.default_allocation_strategy import DefaultAllocationStrategy
from serverV2.orchestrator.allocation.economy_allocation_strategy import EconomyAllocationStrategy
from serverV2.orchestrator.allocation.fast_render_allocation_strategy import FastRenderAllocationStrategy
from serverV2.orchestrator.allocation.validators.engine_compatibility_validator import (
    EngineCompatibilityValidator,
)
from serverV2.orchestrator.blend_url_resolver import BlendUrlResolver
from serverV2.orchestrator.dispatch.coordinator import DispatchCoordinator
from serverV2.orchestrator.dispatch.dispatcher import Dispatcher
from serverV2.orchestrator.lifecycle import RenderLifecycle
from serverV2.orchestrator.orchestrator import RenderOrchestrator
from serverV2.repositories.dispatch_queue_repository import DispatchQueueRepository
from serverV2.repositories.heartbeat_repository import HeartbeatRepository
from serverV2.repositories.machine_heartbeat_repository import (
    MachineHeartbeatRepository,
)
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.machine_repository import MachineRepository
from serverV2.repositories.output_frame_repository import OutputFrameRepository
from serverV2.repositories.progress_repository import ProgressRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository
from serverV2.repositories.telemetry_repository import TelemetryRepository
from serverV2.repositories.user_input_file_repository import UserInputFileRepository
from serverV2.repositories.worker_start_repository import WorkerStartRepository
from serverV2.services.assets.service import AssetService
from serverV2.services.jobs.outputs_resolver import OutputsResolver
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
        callback_router: CallbackRouter,
        dispatch_coordinator: DispatchCoordinator,
        fleet_registry: FleetRegistry,
        community_monitor: CommunityMonitor,
        job_repo: JobRepository,
        machine_repo: MachineRepository,
        group_repo: RenderGroupRepository,
        asset_repo: UserInputFileRepository,
        upload_coordinator: UploadCoordinator,
        render_group_service: RenderGroupService,
        job_service: JobService,
        machine_service: MachineService,
        asset_service: AssetService,
        vast_recovery: VastRecovery,
        modal_recovery: ModalRecovery,
        status_aggregator: InstanceStatusAggregator,
    ) -> None:
        self.config = config
        self.orchestrator = orchestrator
        self.callback_router = callback_router
        self.dispatch_coordinator = dispatch_coordinator
        self.fleet_registry = fleet_registry
        self.community_monitor = community_monitor
        self.job_repo = job_repo
        self.machine_repo = machine_repo
        self.group_repo = group_repo
        self.asset_repo = asset_repo
        self.upload_coordinator = upload_coordinator
        self.render_group_service = render_group_service
        self.job_service = job_service
        self.machine_service = machine_service
        self.asset_service = asset_service
        self.vast_recovery = vast_recovery
        self.modal_recovery = modal_recovery
        self.status_aggregator = status_aggregator


def build(
    config: AppConfig | None = None,
    redis_client: RedisClient | None = None,
) -> Container:
    """Compose the full object graph.  Pure wiring, no side effects."""

    cfg = config or AppConfig.from_env()
    redis = redis_client or RedisClient()

    # -- Modal endpoint drift check --
    # Fail loud at boot if config.json declares a Modal GPU that is not
    # deployed on Modal.  Prevents silent 404s at dispatch time.
    validate_modal_endpoints(cfg.modal)

    # -- repositories --
    job_repo = JobRepository()
    machine_repo = MachineRepository(
        stale_seconds=cfg.machine_stale_seconds,
        community_price_per_hour=cfg.community_price_per_hour,
    )
    group_repo = RenderGroupRepository()
    asset_repo = UserInputFileRepository()
    heartbeat_repo = HeartbeatRepository(redis)
    machine_heartbeat_repo = MachineHeartbeatRepository(redis)
    progress_repo = ProgressRepository(redis)
    worker_start_repo = WorkerStartRepository(redis)
    in_progress_repo = InProgressChunkRepository()
    telemetry_repo = TelemetryRepository()
    output_frame_repo = OutputFrameRepository()

    # -- fleet registry --
    registry = FleetRegistry()

    # -- blend URL resolver --
    blend_resolver = BlendUrlResolver(cfg)

    # -- instance registries (one per fleet, shared InstanceRegistry class) --
    modal_instance_registry = InstanceRegistry()
    vast_instance_registry = InstanceRegistry()

    # -- fleet monitor callbacks -> CallbackRouter (late-bound) --
    router_ref: list[CallbackRouter | None] = [None]

    def _on_failure(job_id: str, error: str) -> None:
        if router_ref[0]:
            router_ref[0].route(
                job_id=job_id,
                outcome=CallbackOutcome.FAILURE,
                error=error,
            )

    def _on_success(job_id: str) -> None:
        if router_ref[0]:
            router_ref[0].route(
                job_id=job_id,
                outcome=CallbackOutcome.SUCCESS,
            )

    # -- pre-render stall detector factory --
    # One builder shape today; per-fleet builders can diverge later if
    # community needs different thresholds (slower download tolerance,
    # longer hard ceiling, etc.).  Each call returns a fresh detector.
    #
    # CpuStallRule intentionally OFF: the worker's ProcessSampler reads
    # CPU% off the handler process, not the Blender subprocess.  Handler
    # is idle during render so the metric reads ~0% and the rule
    # false-positives.  Re-enable once ProcessSampler walks children.
    def _make_pre_render_stall_detector() -> IPreRenderStallDetector:
        s = cfg.stall
        return (PreRenderStallDetectorBuilder()
            .with_bytes_stall(stall_sec=s.download_bytes_stall_sec)
            .with_download_ceiling(
                secs_per_gb=s.download_secs_per_gb,
                min_sec=s.download_phase_min_sec,
                max_sec=s.download_phase_max_sec,
            )
            .with_hard_ceiling(max_sec=s.hard_max_chunk_sec)
            .build())

    # -- vast fleet --
    # No machine registrar — Vast capabilities live in config.json.
    # Strategy reads task.gpu_type at dispatch time.
    vast_client = VastClient(cfg.vast)
    vast_callback = VastCallbackHandler(
        config=cfg.vast, client=vast_client,
        heartbeat_repo=heartbeat_repo,
        progress_repo=progress_repo,
        output_frame_repo=output_frame_repo,
        on_failure=_on_failure,
        on_success=_on_success,
        stall_detector_factory=_make_pre_render_stall_detector,
        registry=vast_instance_registry,
    )
    vast_strategy = VastFleetStrategy(
        config=cfg.vast, client=vast_client,
        callback_handler=vast_callback, job_repo=job_repo,
        on_failure=_on_failure,
    )
    vast_recovery = VastRecovery(
        config=cfg.vast, client=vast_client,
        callback_handler=vast_callback,
    )
    registry.register(vast_strategy)

    # -- modal fleet --
    # No machine registrar — Modal capabilities live in config.json.
    modal_client = ModalClient(cfg.modal)
    modal_callback = ModalCallbackHandler(
        config=cfg.modal, client=modal_client,
        heartbeat_repo=heartbeat_repo,
        progress_repo=progress_repo,
        output_frame_repo=output_frame_repo,
        on_failure=_on_failure,
        on_success=_on_success,
        stall_detector_factory=_make_pre_render_stall_detector,
        registry=modal_instance_registry,
    )
    modal_strategy = ModalFleetStrategy(
        config=cfg.modal, client=modal_client,
        callback_handler=modal_callback, job_repo=job_repo,
        on_failure=_on_failure,
    )
    modal_recovery = ModalRecovery(
        config=cfg.modal, callback_handler=modal_callback,
    )
    registry.register(modal_strategy)

    # -- community fleet --
    community_strategy = CommunityStrategy(job_repo=job_repo)
    registry.register(community_strategy)

    # -- allocation + dispatch --
    # Two strategies are constructed; the lifecycle picks one per render
    # based on file size + frame count (heuristic in RenderLifecycle).
    # Both share the same validator list — per-target eligibility rules
    # (engine compatibility today; tier / price caps in the future).
    target_validators = [EngineCompatibilityValidator()]
    default_strategy = DefaultAllocationStrategy(registry, validators=target_validators)
    fast_render_strategy = FastRenderAllocationStrategy(registry, validators=target_validators)
    economy_strategy = EconomyAllocationStrategy(registry, validators=target_validators)
    dispatcher = Dispatcher(registry)

    # -- dispatch queue (DB-backed) --
    queue_repo = DispatchQueueRepository()

    # -- resource picker: builds AvailableResources per allocation --
    def _resource_picker() -> AvailableResources:
        # Liveness signal lives in Redis (sorted set written by the
        # /machines/{id}/heartbeat route).  Postgres returns the static
        # row data; we intersect with the alive cohort from Redis to
        # filter out PCs whose agents have stopped pinging.  Falls back
        # to the Postgres last_seen_at column if Redis is unavailable.
        community = machine_repo.get_available_community()
        alive_ids = machine_heartbeat_repo.alive_ids(cfg.machine_stale_seconds)
        if alive_ids is not None:
            community = [m for m in community if m.id in alive_ids]
        capabilities: list[FleetCapability] = []
        for ep in cfg.modal.endpoints:
            capabilities.append(FleetCapability(
                fleet="modal_serverless",
                gpu_type=ep.gpu_type,
                label=ep.label,
                vram_gb=ep.vram_gb,
                cpu_cores=ep.cpu_cores,
                ram_gb=ep.ram_gb,
                render_speed=ep.render_speed,
                fleet_max_parallel=cfg.modal.max_parallel,
                price_per_hour=ep.price_per_hour,
            ))
        for ep in cfg.vast.endpoints:
            capabilities.append(FleetCapability(
                fleet="vast_serverless",
                gpu_type=ep.gpu_name,
                label=ep.label,
                vram_gb=ep.vram_gb,
                cpu_cores=ep.cpu_cores,
                ram_gb=ep.ram_gb,
                render_speed=ep.render_speed,
                fleet_max_parallel=cfg.vast.max_parallel,
                price_per_hour=ep.price_per_hour,
            ))
        in_flight = job_repo.count_active_by_fleet()
        return AvailableResources(
            community_machines=community,
            serverless_capabilities=capabilities,
            serverless_in_flight=in_flight,
        )

    # -- per-fleet cap lookup used by the dispatch coordinator --
    def _fleet_cap_lookup(fleet: str) -> int:
        if fleet == "modal_serverless":
            return cfg.modal.max_parallel
        if fleet == "vast_serverless":
            return cfg.vast.max_parallel
        # Community: each machine handles its own queue, no fleet-wide cap.
        # Use a high number so the coordinator never gates community.
        return 10_000

    # -- orchestration: DispatchCoordinator (plumbing) + RenderLifecycle
    # (narrative) + RenderOrchestrator (facade).  External callers hold only
    # the facade; lifecycle holds the flow logic; coordinator is thin plumbing.
    dispatch_coordinator = DispatchCoordinator(
        queue_repo=queue_repo,
        in_progress_repo=in_progress_repo,
        dispatcher=dispatcher,
        blend_url_resolver=blend_resolver,
        job_repo=job_repo,
        fleet_cap_lookup=_fleet_cap_lookup,
    )

    lifecycle = RenderLifecycle(
        default_strategy=default_strategy,
        fast_render_strategy=fast_render_strategy,
        economy_strategy=economy_strategy,
        coordinator=dispatch_coordinator,
        job_repo=job_repo,
        group_repo=group_repo,
        machine_repo=machine_repo,
        queue_repo=queue_repo,
        in_progress_repo=in_progress_repo,
        telemetry_repo=telemetry_repo,
        output_frame_repo=output_frame_repo,
        fleet_registry=registry,
        resource_picker=_resource_picker,
    )
    orchestrator = RenderOrchestrator(lifecycle)

    # -- callbacks --
    # Adapter layer between fleet-monitor outcomes and the orchestrator.
    # Handlers are thin orchestrator-callers; they hold zero state and
    # touch zero repositories.  All DB mutations originate inside the
    # orchestrator → lifecycle path.
    success_handler = SuccessHandler(orchestrator)
    failure_handler = FailureHandler(orchestrator)
    callback_router = CallbackRouter(orchestrator, success_handler, failure_handler)
    router_ref[0] = callback_router

    # Late-bind orchestrator into fleet callback handlers so the per-job
    # monitors they spawn can use it for read facade calls.  Two-phase
    # wiring breaks the otherwise-circular construction order
    # (orchestrator → registry → strategies → handlers → orchestrator).
    vast_callback.set_orchestrator(orchestrator)
    modal_callback.set_orchestrator(orchestrator)

    # -- community fleet monitor (machine-offline + group-terminal reconciliation)
    # Modal and Vast own their own health via per-job monitors inside their
    # callback handlers; community is pull-based and needs this daemon.
    community_monitor = CommunityMonitor(
        job_repo=job_repo,
        group_repo=group_repo,
        machine_repo=machine_repo,
        heartbeat_repo=heartbeat_repo,
        machine_heartbeat_repo=machine_heartbeat_repo,
        output_frame_repo=output_frame_repo,
        on_failure=_on_failure,
        stall_detector=_make_pre_render_stall_detector(),
        demote_seconds=cfg.community_machine_demote_seconds,
    )

    # -- status providers + aggregator --
    modal_status_provider = ModalStatusProvider(modal_instance_registry)
    vast_status_provider = VastStatusProvider(vast_instance_registry)
    status_aggregator = InstanceStatusAggregator()
    status_aggregator.register(modal_status_provider)
    status_aggregator.register(vast_status_provider)

    # -- application services --
    upload_coordinator = UploadCoordinator()

    outputs_resolver = OutputsResolver(
        output_frame_repo=output_frame_repo,
        presigner=lambda key, name: storage.generate_presigned_url(key, download_name=name),
    )

    render_group_service = RenderGroupService(
        group_repo=group_repo,
        job_repo=job_repo,
        machine_repo=machine_repo,
        asset_repo=asset_repo,
        orchestrator=orchestrator,
        fleet_registry=registry,
        outputs_resolver=outputs_resolver,
        output_frame_repo=output_frame_repo,
    )

    job_service = JobService(
        job_repo=job_repo,
        machine_repo=machine_repo,
        heartbeat_repo=heartbeat_repo,
        machine_heartbeat_repo=machine_heartbeat_repo,
        progress_repo=progress_repo,
        worker_start_repo=worker_start_repo,
        outputs_resolver=outputs_resolver,
        output_frame_repo=output_frame_repo,
        success_notifier=lambda jid: callback_router.route(
            job_id=jid, outcome=CallbackOutcome.SUCCESS,
        ),
    )

    machine_service = MachineService(
        orchestrator=orchestrator,
        machine_heartbeat_repo=machine_heartbeat_repo,
        vast_config=cfg.vast,
        modal_config=cfg.modal,
    )

    asset_service = AssetService(asset_repo=asset_repo)

    return Container(
        config=cfg,
        orchestrator=orchestrator,
        callback_router=callback_router,
        dispatch_coordinator=dispatch_coordinator,
        fleet_registry=registry,
        community_monitor=community_monitor,
        job_repo=job_repo,
        machine_repo=machine_repo,
        group_repo=group_repo,
        asset_repo=asset_repo,
        upload_coordinator=upload_coordinator,
        render_group_service=render_group_service,
        job_service=job_service,
        machine_service=machine_service,
        asset_service=asset_service,
        vast_recovery=vast_recovery,
        modal_recovery=modal_recovery,
        status_aggregator=status_aggregator,
    )
