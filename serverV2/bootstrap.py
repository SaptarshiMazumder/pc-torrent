"""Bootstrap — wires all components together via composition.

Call ``build()`` once at startup to get a fully-configured Container.
Every dependency is explicit; nothing is a hidden global.

After Phase 1 of the allocator redesign, Modal and Vast no longer have
machine registrars — their endpoints come from ``config.json`` and the
allocator works against ``FleetCapability`` descriptors directly.  Only
the community fleet has DB-backed machine rows.
"""

from __future__ import annotations

import uuid

from serverV2.callbacks.failure_handler import FailureHandler
from serverV2.callbacks.router import CallbackRouter
from serverV2.callbacks.success_handler import SuccessHandler
from serverV2.config import AppConfig
from serverV2.core.enums import CallbackOutcome
from serverV2.core.models import AvailableResources
from serverV2.fleets.community.community_monitor import CommunityMonitor
from serverV2.fleets.community.strategy import CommunityStrategy
from serverV2.fleets.instance_registry import InstanceRegistry
from serverV2.fleets.modal.monitor import ModalMonitorManager
from serverV2.fleets.modal.client import ModalClient
from serverV2.fleets.modal.endpoint_validator import validate_modal_endpoints
from serverV2.fleets.fleet_availability import (
    CommunityAvailabilityBuilder,
    FleetAvailabilityBuilderFactory,
    InProgressServerlessFleetBuilder,
    ModalAvailabilityBuilder,
    VastAvailabilityBuilder,
)
from serverV2.fleets.modal.modal_active_jobs_hooks import ModalActiveJobsHooks
from serverV2.fleets.modal.modal_active_jobs_tracker import (
    ModalActiveJobsTracker,
)
from serverV2.fleets.modal.strategy import ModalFleetStrategy
from serverV2.fleets.registry import FleetRegistry
from serverV2.fleets.shared.pre_render_stall_detector import (
    IPreRenderStallDetector,
    PreRenderStallDetectorBuilder,
)
from serverV2.fleets.status_aggregator import InstanceStatusAggregator
from serverV2.fleets.status_provider import ModalStatusProvider, VastStatusProvider
from serverV2.fleets.vast.monitor import VastMonitorManager
from serverV2.fleets.vast.client import VastClient
from serverV2.fleets.vast.strategy import VastFleetStrategy
from serverV2.infrastructure import storage
from serverV2.infrastructure.redis_client import RedisClient
from serverV2.allocation.allocation_strategies.default_allocation_strategy import (
    DefaultAllocationStrategy,
)
from serverV2.allocation.allocation_strategies.economy_allocation_strategy import (
    EconomyAllocationStrategy,
)
from serverV2.allocation.allocation_strategies.fast_render_allocation_strategy import (
    FastRenderAllocationStrategy,
)
from serverV2.allocation.allocation_strategies.validators.allocation_engine_compatibility_validator import (
    AllocationEngineCompatibilityValidator,
)
from serverV2.monitor_lock import MonitorLockFacade, MonitorLockRepository
from serverV2.orchestrator.anti_affinity import (
    AntiAffinityFacade,
    AntiAffinityRepository,
    AntiAffinityService,
)
from serverV2.orchestrator.chunk_progress import ChunkProgressService
from serverV2.orchestrator.allocation_client import AllocationClient
from serverV2.orchestrator.lifecycle import RenderLifecycle
from serverV2.orchestrator.lifecycle_job_retry import RetryDeps, RetryExecutor
from serverV2.orchestrator.lifecycle_job_termination import (
    JobTerminator,
    LifecycleDeps,
    RenderCanceler,
)
from serverV2.orchestrator.orchestrator import RenderOrchestrator
from serverV2.allocation import AllocationDispatchQueueDaemon, AllocationFacade
from serverV2.allocation.allocation_blend_url_resolver import (
    AllocationBlendUrlResolver,
)
from serverV2.allocation.allocation_dispatch_queue_repository import (
    AllocationDispatchQueueRepository as DispatchQueueRepository,
)
from serverV2.allocation.allocation_dispatcher import AllocationDispatcher
from serverV2.allocation.services.allocation_dispatch_queue_service import (
    AllocationDispatchQueueService,
)
from serverV2.allocation.services.allocation_planning_service import (
    AllocationPlanningService,
)
from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_strategy_selector import (
    AllocationStrategySelector,
)
from serverV2.fleets.fleet_availability.fleet_availability_snapshot_cache import (
    FleetAvailabilitySnapshotCache,
)
from serverV2.repositories.heartbeat_repository import HeartbeatRepository
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.services.machines.machine_heartbeat_repository import (
    MachineHeartbeatRepository,
)
from serverV2.services.machines.machine_repository import MachineRepository
from serverV2.services.machines.machine_state_writer import MachineStateWriter
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
        fleet_registry: FleetRegistry,
        community_monitor: CommunityMonitor,
        monitor_lock_facade: MonitorLockFacade,
        job_repo: JobRepository,
        machine_repo: MachineRepository,
        group_repo: RenderGroupRepository,
        asset_repo: UserInputFileRepository,
        upload_coordinator: UploadCoordinator,
        render_group_service: RenderGroupService,
        job_service: JobService,
        machine_service: MachineService,
        asset_service: AssetService,
        status_aggregator: InstanceStatusAggregator,
        allocation_facade: AllocationFacade,
        allocation_dispatch_queue_daemon: AllocationDispatchQueueDaemon,
        fleet_availability_snapshot_cache: FleetAvailabilitySnapshotCache,
    ) -> None:
        self.config = config
        self.orchestrator = orchestrator
        self.callback_router = callback_router
        self.fleet_registry = fleet_registry
        self.community_monitor = community_monitor
        self.monitor_lock_facade = monitor_lock_facade
        self.job_repo = job_repo
        self.machine_repo = machine_repo
        self.group_repo = group_repo
        self.asset_repo = asset_repo
        self.upload_coordinator = upload_coordinator
        self.render_group_service = render_group_service
        self.job_service = job_service
        self.machine_service = machine_service
        self.asset_service = asset_service
        self.status_aggregator = status_aggregator
        # Phase A: new allocation module — constructed but inert.
        # Phase B starts the daemon and reroutes callers.
        self.allocation_facade = allocation_facade
        self.allocation_dispatch_queue_daemon = allocation_dispatch_queue_daemon
        self.fleet_availability_snapshot_cache = fleet_availability_snapshot_cache


def build(
    config: AppConfig | None = None,
    redis_client: RedisClient | None = None,
) -> Container:
    """Compose the full object graph.  Pure wiring, no side effects."""

    cfg = config or AppConfig.from_env()
    redis = redis_client or RedisClient()

    # Per-instance ID used as the value of every monitor lock this
    # process holds.  Generated once per process: surviving across
    # restarts is wrong (we want a fresh ID so old locks held by the
    # dead process expire on TTL rather than being refreshed).  Used by
    # MonitorLockRepository's compare-and-swap refresh / release.
    instance_id = str(uuid.uuid4())

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
    # Single writer for the machines.status field -- every status
    # mutation goes through here so PG and Redis stay in lockstep.
    machine_state_writer = MachineStateWriter(
        machine_repo=machine_repo,
        machine_heartbeat_repo=machine_heartbeat_repo,
    )
    progress_repo = ProgressRepository(redis)
    worker_start_repo = WorkerStartRepository(redis)
    in_progress_repo = InProgressChunkRepository()
    telemetry_repo = TelemetryRepository()
    output_frame_repo = OutputFrameRepository()
    monitor_lock_repo = MonitorLockRepository(redis)

    # -- fleet registry --
    registry = FleetRegistry()

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
    vast_callback = VastMonitorManager(
        config=cfg.vast, client=vast_client,
        heartbeat_repo=heartbeat_repo,
        progress_repo=progress_repo,
        output_frame_repo=output_frame_repo,
        on_failure=_on_failure,
        on_success=_on_success,
        stall_detector_factory=_make_pre_render_stall_detector,
        lock_repo=monitor_lock_repo,
        instance_id=instance_id,
        registry=vast_instance_registry,
    )
    vast_strategy = VastFleetStrategy(
        config=cfg.vast, client=vast_client,
        callback_handler=vast_callback, job_repo=job_repo,
        on_failure=_on_failure,
    )
    registry.register(vast_strategy)

    # -- modal fleet --
    # No machine registrar — Modal capabilities live in config.json.
    modal_client = ModalClient(cfg.modal)
    modal_callback = ModalMonitorManager(
        config=cfg.modal, client=modal_client,
        heartbeat_repo=heartbeat_repo,
        progress_repo=progress_repo,
        output_frame_repo=output_frame_repo,
        on_failure=_on_failure,
        on_success=_on_success,
        stall_detector_factory=_make_pre_render_stall_detector,
        lock_repo=monitor_lock_repo,
        instance_id=instance_id,
        registry=modal_instance_registry,
    )
    modal_active_jobs_tracker = ModalActiveJobsTracker(redis_client=redis)
    modal_active_jobs_hooks = ModalActiveJobsHooks(
        tracker=modal_active_jobs_tracker,
    )
    modal_strategy = ModalFleetStrategy(
        config=cfg.modal, client=modal_client,
        callback_handler=modal_callback, job_repo=job_repo,
        on_failure=_on_failure,
        active_jobs_hooks=modal_active_jobs_hooks,
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
    target_validators = [AllocationEngineCompatibilityValidator()]
    default_strategy = DefaultAllocationStrategy(registry, validators=target_validators)
    fast_render_strategy = FastRenderAllocationStrategy(registry, validators=target_validators)
    economy_strategy = EconomyAllocationStrategy(registry, validators=target_validators)

    # -- dispatch queue (DB-backed) --
    queue_repo = DispatchQueueRepository()

    # -- fleet availability: per-step builders + factory.  The cache
    # holds the snapshot; the daemon and orchestrator both go through
    # AllocationClient + FleetAvailabilitySnapshotCache.  No
    # _resource_picker closure here -- that lived in the old
    # DispatchCoordinator world; AllocationClient does the snapshot
    # fetch + AvailableResources adapter internally.
    fleet_availability_factory = FleetAvailabilityBuilderFactory(
        vast=VastAvailabilityBuilder(
            client=vast_client, config=cfg.vast, job_repo=job_repo,
        ),
        modal=ModalAvailabilityBuilder(
            config=cfg.modal,
            job_repo=job_repo,
            tracker=modal_active_jobs_tracker,
        ),
        community=CommunityAvailabilityBuilder(
            machine_repo=machine_repo,
            machine_heartbeat_repo=machine_heartbeat_repo,
            stale_seconds=cfg.machine_stale_seconds,
        ),
        in_progress_serverless_fleet=InProgressServerlessFleetBuilder(
            job_repo=job_repo,
        ),
    )

    # ------------------------------------------------------------------
    # Allocation module wiring.  Lifecycle plans + enqueues through
    # AllocationClient; the daemon picks up queued items every ~2s
    # under a Redis singleton lock.
    # ------------------------------------------------------------------
    allocation_dispatcher = AllocationDispatcher(registry)
    allocation_blend_resolver = AllocationBlendUrlResolver(cfg)
    allocation_strategy_selector = AllocationStrategySelector()
    allocation_planning_service = AllocationPlanningService(
        strategies={
            "default": default_strategy,
            "fast_render": fast_render_strategy,
            "economy": economy_strategy,
        },
        selector=allocation_strategy_selector,
    )
    _allocation_fleet_caps: dict[str, int] = {
        "modal_serverless": cfg.modal.max_parallel,
        "vast_serverless": cfg.vast.max_parallel,
        "community": 10_000,  # per-machine queue, no fleet-wide cap
    }
    _TERMINAL_GROUP_STATUSES = frozenset({"done", "failed", "cancelled"})

    def _is_group_terminal(group_id: str) -> bool:
        grp = group_repo.get_by_id(group_id)
        if grp is None:
            # Group was deleted out from under us — treat as terminal so
            # we drop the orphaned queue row rather than dispatching
            # against a non-existent group.
            return True
        return str(grp.get("status") or "") in _TERMINAL_GROUP_STATUSES

    allocation_dispatch_queue_service = AllocationDispatchQueueService(
        queue_repo=queue_repo,
        in_progress_repo=in_progress_repo,
        active_count_by_fleet=job_repo.count_active_by_fleet,
        dispatcher=allocation_dispatcher,
        blend_url_resolver=allocation_blend_resolver,
        fleet_caps=_allocation_fleet_caps,
        is_group_terminal=_is_group_terminal,
    )
    allocation_facade = AllocationFacade(
        planning=allocation_planning_service,
        dispatch_queue=allocation_dispatch_queue_service,
    )
    fleet_availability_snapshot_cache = FleetAvailabilitySnapshotCache(
        builder_factory=fleet_availability_factory,
        redis_client=redis,
    )
    allocation_dispatch_queue_daemon = AllocationDispatchQueueDaemon(
        dispatch_queue=allocation_dispatch_queue_service,
        snapshot_cache=fleet_availability_snapshot_cache,
        lock_repo=monitor_lock_repo,
        instance_id=instance_id,
        enabled_fleets=list(_allocation_fleet_caps.keys()),
    )
    allocation_client = AllocationClient(
        facade=allocation_facade,
        snapshot_cache=fleet_availability_snapshot_cache,
    )

    # -- anti-affinity: facade/service/repository for retry exclusion
    # resolution.  Resolves the union of (fleet, gpu_type) and
    # machine_id exclusions across all prior failed/cancelled attempts
    # of a chunk.  RenderLifecycle calls it before invoking any
    # termination or retry pipeline.
    anti_affinity_repository = AntiAffinityRepository()
    anti_affinity_service = AntiAffinityService()
    anti_affinity_facade = AntiAffinityFacade(
        repository=anti_affinity_repository,
        service=anti_affinity_service,
    )

    # -- retry: pipelines + executor + termination steps + group cancel --
    # Constructed bottom-up because ``_reconcile_group`` needs a forward
    # reference to lifecycle.  The closure captures the local
    # ``lifecycle`` name, which we assign at the end of this block.

    def _reconcile_group(group_id: str) -> None:
        lifecycle.reconcile_group_status(group_id)

    chunk_progress_service = ChunkProgressService(
        output_frame_repo=output_frame_repo,
    )

    retry_deps = RetryDeps(
        job_repo=job_repo,
        group_repo=group_repo,
        chunk_progress=chunk_progress_service,
        in_progress_repo=in_progress_repo,
        allocation_client=allocation_client,
        reconcile_group=_reconcile_group,
    )
    retry_executor = RetryExecutor(deps=retry_deps)

    lifecycle_deps = LifecycleDeps(
        job_repo=job_repo,
        group_repo=group_repo,
        in_progress_repo=in_progress_repo,
        state_writer=machine_state_writer,
        fleet_registry=registry,
        retry_executor=retry_executor,
        reconcile_group=_reconcile_group,
    )

    job_terminator = JobTerminator(deps=lifecycle_deps)

    render_canceler = RenderCanceler(
        group_repo=group_repo,
        job_repo=job_repo,
        queue_repo=queue_repo,
        in_progress_repo=in_progress_repo,
        deps=lifecycle_deps,
        modal_active_jobs_hooks=modal_active_jobs_hooks,
    )

    lifecycle = RenderLifecycle(
        allocation_client=allocation_client,
        job_repo=job_repo,
        group_repo=group_repo,
        machine_repo=machine_repo,
        machine_state_writer=machine_state_writer,
        in_progress_repo=in_progress_repo,
        telemetry_repo=telemetry_repo,
        output_frame_repo=output_frame_repo,
        fleet_registry=registry,
        retry_executor=retry_executor,
        anti_affinity=anti_affinity_facade,
        modal_active_jobs_hooks=modal_active_jobs_hooks,
        job_terminator=job_terminator,
        render_canceler=render_canceler,
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
    # Singleton across instances via the ``monitor:community`` Redis lock.
    community_monitor = CommunityMonitor(
        job_repo=job_repo,
        group_repo=group_repo,
        machine_repo=machine_repo,
        heartbeat_repo=heartbeat_repo,
        machine_heartbeat_repo=machine_heartbeat_repo,
        output_frame_repo=output_frame_repo,
        progress_repo=progress_repo,
        on_failure=_on_failure,
        stall_detector=_make_pre_render_stall_detector(),
        lock_repo=monitor_lock_repo,
        instance_id=instance_id,
        demote_seconds=cfg.community_machine_demote_seconds,
    )

    # -- monitor lock facade --
    # Per-instance daemon that re-claims orphaned per-job (Vast/Modal)
    # monitors and the community singleton.  Replaces leader-only boot
    # recovery: every instance runs this and ownership is decided by
    # the per-key Redis lock the managers acquire on ``start_monitoring``.
    # The facade hides the per-fleet sweep strategies + thread driver --
    # bootstrap only sees the public surface.
    monitor_lock_facade = MonitorLockFacade.build(
        vast_cfg=cfg.vast,
        modal_cfg=cfg.modal,
        vast_manager=vast_callback,
        modal_manager=modal_callback,
        community_monitor=community_monitor,
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
        chunk_progress=chunk_progress_service,
    )

    job_service = JobService(
        job_repo=job_repo,
        machine_repo=machine_repo,
        heartbeat_repo=heartbeat_repo,
        machine_heartbeat_repo=machine_heartbeat_repo,
        machine_state_writer=machine_state_writer,
        progress_repo=progress_repo,
        worker_start_repo=worker_start_repo,
        outputs_resolver=outputs_resolver,
        output_frame_repo=output_frame_repo,
        success_notifier=lambda jid: callback_router.route(
            job_id=jid, outcome=CallbackOutcome.SUCCESS,
        ),
        community_idle_notifier=orchestrator.handle_community_machine_idle,
    )

    machine_service = MachineService(
        orchestrator=orchestrator,
        machine_heartbeat_repo=machine_heartbeat_repo,
        state_writer=machine_state_writer,
        vast_config=cfg.vast,
        modal_config=cfg.modal,
    )

    asset_service = AssetService(asset_repo=asset_repo)

    return Container(
        config=cfg,
        orchestrator=orchestrator,
        callback_router=callback_router,
        fleet_registry=registry,
        community_monitor=community_monitor,
        monitor_lock_facade=monitor_lock_facade,
        job_repo=job_repo,
        machine_repo=machine_repo,
        group_repo=group_repo,
        asset_repo=asset_repo,
        upload_coordinator=upload_coordinator,
        render_group_service=render_group_service,
        job_service=job_service,
        machine_service=machine_service,
        asset_service=asset_service,
        status_aggregator=status_aggregator,
        allocation_facade=allocation_facade,
        allocation_dispatch_queue_daemon=allocation_dispatch_queue_daemon,
        fleet_availability_snapshot_cache=fleet_availability_snapshot_cache,
    )
