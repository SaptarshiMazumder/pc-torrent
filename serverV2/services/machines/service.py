"""MachineService — desktop agent registration, heartbeat, status."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from serverV2.core.value_objects import now_iso
from serverV2.infrastructure.db import execute, query_one, query_all
from serverV2.services.machines.machine_redis_mirror import MachineRedisMirror
from serverV2.services.machines.machine_repository import MachineRepository

if TYPE_CHECKING:
    from serverV2.orchestrator.orchestrator import RenderOrchestrator

log = logging.getLogger(__name__)


class MachineServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class MachineService:

    def __init__(
        self,
        *,
        orchestrator: "RenderOrchestrator",
        machine_repo: MachineRepository,
        mirror: MachineRedisMirror,
        vast_config=None,
        modal_config=None,
    ) -> None:
        self._orchestrator = orchestrator
        self._machine_repo = machine_repo
        self._mirror = mirror
        self._vast_cfg = vast_config
        self._modal_cfg = modal_config

    def _excluded_types(self) -> set[str]:
        excluded: set[str] = set()
        if self._vast_cfg and not self._vast_cfg.is_enabled():
            excluded.add("vast_serverless")
        if self._modal_cfg and not self._modal_cfg.is_enabled():
            excluded.add("modal_serverless")
        return excluded

    def register(self, payload: Any, user_id: str) -> dict[str, str]:
        gpu_model = getattr(payload, "gpu_model", None)
        gpu_vram_gb = getattr(payload, "gpu_vram_gb", None)
        if not gpu_model or gpu_vram_gb is None:
            raise MachineServiceError(400, "Missing required fields (gpu_model, gpu_vram_gb)")

        machine_key = (getattr(payload, "machine_key", None) or "").strip() or None
        current_time = now_iso()

        existing = None
        if machine_key:
            existing = query_one("SELECT id FROM machines WHERE machine_key = %s", (machine_key,))

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
                    machine_key, gpu_model, gpu_vram_gb,
                    getattr(payload, "cpu_cores", None),
                    getattr(payload, "ram_gb", None),
                    getattr(payload, "os_version", None),
                    getattr(payload, "nvidia_driver", None),
                    getattr(payload, "machine_type", "windows"),
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
                    machine_id, machine_key, gpu_model, gpu_vram_gb,
                    getattr(payload, "cpu_cores", None),
                    getattr(payload, "ram_gb", None),
                    getattr(payload, "os_version", None),
                    getattr(payload, "nvidia_driver", None),
                    getattr(payload, "machine_type", "windows"),
                    current_time, current_time, user_id,
                ),
            )

        # Mirror the freshly-written 'idle' status into Redis so the
        # allocator's read path and the heartbeat self-heal don't see
        # a missing entry on first sight.
        self._mirror.set_status(machine_id, "idle")
        return {"machine_id": machine_id}

    def set_available(self, machine_id: str) -> dict[str, bool]:
        machine = query_one("SELECT id FROM machines WHERE id = %s", (machine_id,))
        if not machine:
            raise MachineServiceError(404, "Machine not found")
        # Going-idle implies the agent has nothing in flight.  Any
        # ``status='running'`` jobs still tied to this machine are
        # leftovers from a prior session (sidecar rebuild, crash,
        # network blip).  Route them through the orchestrator's
        # standard failure path so retries fire — same flow Vast/Modal
        # use when their per-job monitor sees a container disappear.
        self._orchestrator.handle_community_machine_idle(machine_id)
        # PG sync + Redis async (mirror).
        self._machine_repo.update_status(machine_id, "available")
        # Also seed the Redis machines:alive sorted set so the agent
        # is considered live from this instant -- the next poll's
        # ZADD just refreshes the same entry.
        self._mirror.record(machine_id)
        return {"success": True}

    def set_idle(self, machine_id: str) -> dict[str, bool]:
        machine = query_one("SELECT id FROM machines WHERE id = %s", (machine_id,))
        if not machine:
            raise MachineServiceError(404, "Machine not found")
        # Drop the agent from the alive set in Redis -- the allocator's
        # liveness intersect should immediately stop returning this PC.
        self._mirror.clear(machine_id)
        self._machine_repo.update_status(machine_id, "idle")
        return {"success": True}

    def ensure_available(self, machine_id: str) -> None:
        """Self-heal entry called from ``JobService.next_for_machine`` on
        every work-claim poll.  An agent reaches that endpoint only when
        idle, so its status should be 'available' -- if Redis says
        otherwise, we flip it (PG + Redis) and let the next allocator
        tick pick the agent up.

        Cheap on the happy path: one HGET, no writes when the cached
        status is already 'available'.  Fail-safe on Redis miss/down:
        ``mirror.get_status`` returns None, so we treat that as "drift"
        and reassert via PG -- the resulting Redis write reseeds the
        cache.
        """
        if self._mirror.get_status(machine_id) == "available":
            return
        self._machine_repo.update_status(machine_id, "available")

    def list_available(self) -> list[dict[str, Any]]:
        # Community-only after Phase 1 of the allocator redesign — Modal
        # and Vast capacities live in config.json, not the machines table.
        machines = self._machine_repo.get_available_community()
        return [
            {
                "id": m.id,
                "machine_type": "windows",
                "gpu_model": m.gpu_model,
                "gpu_vram_gb": m.vram_gb,
                "cpu_cores": m.cpu_cores,
                "ram_gb": m.ram_gb,
                "status": m.status,
                "render_speed": m.render_speed,
                "last_seen_at": m.last_seen_at,
            }
            for m in machines
        ]
