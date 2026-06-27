"""MachineService — desktop agent registration, heartbeat, status."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
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
        vast_provider=None,
        modal_provider=None,
    ) -> None:
        self._orchestrator = orchestrator
        self._machine_repo = machine_repo
        self._mirror = mirror
        self._vast_provider = vast_provider
        self._modal_provider = modal_provider

    def _excluded_types(self) -> set[str]:
        excluded: set[str] = set()
        if self._vast_provider and not self._vast_provider.get().is_enabled():
            excluded.add("vast_serverless")
        if self._modal_provider and not self._modal_provider.get().is_enabled():
            excluded.add("modal_serverless")
        return excluded

    def register(self, payload: Any, user_id: str) -> dict[str, str]:
        gpu_model = getattr(payload, "gpu_model", None)
        gpu_vram_gb = getattr(payload, "gpu_vram_gb", None)
        if not gpu_model or gpu_vram_gb is None:
            raise MachineServiceError(400, "Missing required fields (gpu_model, gpu_vram_gb)")

        commitment_seconds = getattr(payload, "commitment_seconds", None)
        if commitment_seconds is None or commitment_seconds <= 0:
            raise MachineServiceError(
                400, "commitment_seconds is required and must be > 0",
            )
        commitment_end_at = datetime.now(timezone.utc) + timedelta(seconds=float(commitment_seconds))

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
                    status = 'idle', registered_at = %s, last_seen_at = %s, user_id = %s,
                    commitment_end_at = %s
                WHERE id = %s
                """,
                (
                    machine_key, gpu_model, gpu_vram_gb,
                    getattr(payload, "cpu_cores", None),
                    getattr(payload, "ram_gb", None),
                    getattr(payload, "os_version", None),
                    getattr(payload, "nvidia_driver", None),
                    getattr(payload, "machine_type", "windows"),
                    current_time, current_time, user_id, commitment_end_at, machine_id,
                ),
            )
        else:
            machine_id = str(uuid4())
            execute(
                """
                INSERT INTO machines (
                    id, machine_key, gpu_model, gpu_vram_gb, cpu_cores, ram_gb,
                    os_version, nvidia_driver, machine_type, status,
                    registered_at, last_seen_at, user_id, commitment_end_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'idle', %s, %s, %s, %s)
                """,
                (
                    machine_id, machine_key, gpu_model, gpu_vram_gb,
                    getattr(payload, "cpu_cores", None),
                    getattr(payload, "ram_gb", None),
                    getattr(payload, "os_version", None),
                    getattr(payload, "nvidia_driver", None),
                    getattr(payload, "machine_type", "windows"),
                    current_time, current_time, user_id, commitment_end_at,
                ),
            )

        # Mirror the freshly-written 'idle' status into Redis so the
        # allocator's read path and the heartbeat self-heal don't see
        # a missing entry on first sight.
        self._mirror.set_status(machine_id, "idle")
        return {"machine_id": machine_id}

    def set_commitment(
        self, machine_id: str, commitment_seconds: float, user_id: str,
    ) -> dict[str, Any]:
        """Update a community machine's commitment window.

        - ``commitment_seconds > 0``: slides the window to ``now + value``.
        - ``commitment_seconds == 0``: snaps ``commitment_end_at`` to now,
          retiring the row from planning on the next snapshot read.
        """
        row = query_one(
            "SELECT id, user_id FROM machines WHERE id = %s",
            (machine_id,),
        )
        if not row:
            raise MachineServiceError(404, "Machine not found")
        if not row.get("user_id") or row["user_id"] != user_id:
            raise MachineServiceError(403, "Access denied")
        if commitment_seconds < 0:
            raise MachineServiceError(400, "commitment_seconds must be >= 0")

        if commitment_seconds == 0:
            new_end = datetime.now(timezone.utc)
        else:
            new_end = datetime.now(timezone.utc) + timedelta(seconds=float(commitment_seconds))
        execute(
            "UPDATE machines SET commitment_end_at = %s WHERE id = %s",
            (new_end, machine_id),
        )
        return {"success": True, "commitment_end_at": new_end.isoformat()}

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
