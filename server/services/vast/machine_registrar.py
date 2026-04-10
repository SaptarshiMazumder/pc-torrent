"""
MachineRegistrar — virtual machine lifecycle in the database.

Handles:
- Upsert one DB row per GPU endpoint
- Heartbeat daemon thread to keep machines "available"
- Deactivation of stale endpoints no longer in config
"""

from __future__ import annotations

import logging
import threading
import time
from uuid import uuid4

from domain.value_objects import now_iso
from infrastructure.db import execute, query_one
from services.vast.config import VastConfig

log = logging.getLogger(__name__)


class MachineRegistrar:
    def __init__(self, config: VastConfig) -> None:
        self._cfg = config
        self._machine_endpoint_map: dict[str, str] = {}

    @property
    def machine_endpoint_map(self) -> dict[str, str]:
        return self._machine_endpoint_map

    def register_all(self) -> list[str]:
        """Upsert one virtual machine row per Vast.ai GPU type.
        Returns list of machine IDs.
        """
        if not self._cfg.is_enabled():
            self._machine_endpoint_map.clear()
            self._deactivate_stale([])
            log.info("Vast.ai not configured — skipping registration")
            return []

        self._machine_endpoint_map.clear()
        machine_ids: list[str] = []
        active_keys: list[str] = []
        now = now_iso()

        for ep in self._cfg.endpoints:
            machine_key = f"vast-serverless-{ep.gpu_name.replace(' ', '_').lower()}"
            active_keys.append(machine_key)

            existing = query_one("SELECT id FROM machines WHERE machine_key = %s", (machine_key,))
            if existing:
                machine_id = existing["id"]
                execute(
                    """
                    UPDATE machines
                    SET gpu_model = %s, gpu_vram_gb = %s, cpu_cores = %s, ram_gb = %s,
                        os_version = %s, machine_type = %s, render_speed = %s,
                        status = 'available', last_seen_at = %s
                    WHERE id = %s
                    """,
                    (ep.label, ep.vram_gb, self._cfg.cpu_cores, self._cfg.ram_gb,
                     "Linux", "vast_serverless", ep.render_speed, now, machine_id),
                )
                log.info(
                    f"Vast.ai endpoint '{ep.gpu_name}' updated: {machine_id} "
                    f"({ep.label}, {ep.vram_gb}GB, speed={ep.render_speed})"
                )
            else:
                machine_id = str(uuid4())
                execute(
                    """
                    INSERT INTO machines (
                        id, machine_key, gpu_model, gpu_vram_gb, cpu_cores, ram_gb,
                        os_version, machine_type, render_speed,
                        status, registered_at, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'available', %s, %s)
                    """,
                    (machine_id, machine_key,
                     ep.label, ep.vram_gb, self._cfg.cpu_cores, self._cfg.ram_gb,
                     "Linux", "vast_serverless", ep.render_speed, now, now),
                )
                log.info(
                    f"Vast.ai endpoint '{ep.gpu_name}' registered: {machine_id} "
                    f"({ep.label}, {ep.vram_gb}GB, speed={ep.render_speed})"
                )

            self._machine_endpoint_map[machine_id] = ep.gpu_name
            machine_ids.append(machine_id)

        self._deactivate_stale(active_keys)
        return machine_ids

    def start_heartbeat(self) -> None:
        if not self._cfg.is_enabled():
            return

        machine_keys = [
            f"vast-serverless-{ep.gpu_name.replace(' ', '_').lower()}"
            for ep in self._cfg.endpoints
        ]

        def _loop() -> None:
            while True:
                try:
                    for mk in machine_keys:
                        execute(
                            """
                            UPDATE machines
                            SET status = 'available', last_seen_at = %s
                            WHERE machine_key = %s AND machine_type = 'vast_serverless'
                            """,
                            (now_iso(), mk),
                        )
                except Exception as e:
                    log.warning(f"Vast.ai heartbeat error: {e}")
                time.sleep(self._cfg.heartbeat_interval_sec)

        t = threading.Thread(target=_loop, daemon=True, name="vast-heartbeat")
        t.start()
        log.info(f"Vast.ai heartbeat thread started ({len(self._cfg.endpoints)} endpoint(s))")

    def gpu_name_for_machine(self, machine_id: str) -> str:
        gpu_name = self._machine_endpoint_map.get(machine_id)
        if gpu_name:
            return gpu_name
        if len(self._cfg.endpoints) == 1:
            return self._cfg.endpoints[0].gpu_name
        raise ValueError(f"No Vast.ai endpoint mapped for machine_id={machine_id}")

    def _deactivate_stale(self, active_keys: list[str]) -> None:
        if not active_keys:
            execute("UPDATE machines SET status = 'idle' WHERE machine_type = 'vast_serverless'")
            return
        placeholders = ", ".join(["%s"] * len(active_keys))
        execute(
            f"""
            UPDATE machines
            SET status = 'idle'
            WHERE machine_type = 'vast_serverless'
              AND machine_key NOT IN ({placeholders})
            """,
            tuple(active_keys),
        )
