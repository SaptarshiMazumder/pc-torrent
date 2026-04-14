from pydantic import BaseModel


class RegisterMachinePayload(BaseModel):
    machine_key: str | None = None
    gpu_model: str
    gpu_vram_gb: float
    cpu_cores: int = 0
    ram_gb: float = 0
    os_version: str | None = None
    nvidia_driver: str | None = None
    machine_type: str = "windows"
