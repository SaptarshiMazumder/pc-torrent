from pydantic import BaseModel, Field


class RegisterMachinePayload(BaseModel):
    machine_key: str | None = None
    gpu_model: str
    gpu_vram_gb: float
    cpu_cores: int = 0
    ram_gb: float = 0
    os_version: str | None = None
    nvidia_driver: str | None = None
    machine_type: str = "windows"
    # Required at register-time per the time-aware allocation plan
    # (plans/time_aware_allocation_phases.md, Phase 4).  Server stamps
    # ``commitment_end_at = now + commitment_seconds`` on the row.  The
    # planner's time filter (Phase 5) consults the remaining window when
    # deciding which community machines can fit a chunk.  Must be > 0;
    # the desktop UI's datetime picker validates a minimum 15min window
    # before allowing connect, so anything that reaches here is a real
    # user commitment.
    commitment_seconds: float = Field(..., gt=0)


class MachineCommitmentPayload(BaseModel):
    # Sliding-window extension fired by the desktop agent while connected,
    # and a final ``0`` on graceful disconnect to retire the row from
    # planning.  Non-negative; ``0`` snaps ``commitment_end_at`` to now.
    commitment_seconds: float = Field(..., ge=0)
