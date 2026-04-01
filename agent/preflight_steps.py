"""
PC Rent Agent - Modular Preflight Steps
Defines a step-based preflight pipeline where each check is a discrete,
ordered unit with its own status, detail message, and optional fix action.

Steps are defined in a plain list — reorder by moving items, add new steps
by appending to the list.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class PreflightStep:
    """A single preflight check/action unit."""
    id: str
    name: str
    check: Callable  # () -> StepResult  (or (force) -> StepResult)
    action: Optional[Callable] = None  # (progress_cb) -> StepResult
    depends_on: list = field(default_factory=list)
    required: bool = True  # False = failure is a warning, not a blocker

    # Mutable runtime state (managed by PreflightRunner)
    status: str = "pending"        # pending | running | passed | failed | skipped | warning
    detail: str = ""
    progress: Optional[float] = None
    awaiting_uac: bool = False

    def snapshot(self):
        """Return a JSON-serialisable dict of current state."""
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "progress": self.progress,
            "awaiting_uac": self.awaiting_uac,
        }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class PreflightRunner:
    """Execute an ordered list of PreflightSteps, emitting updates after
    every state change via *on_update(steps_snapshot)*."""

    def __init__(self, steps: list, on_update: Callable):
        self.steps = steps
        self.on_update = on_update
        self._step_map = {s.id: s for s in steps}

    # -- public API --

    def run(self, force_recheck=False):
        """Run all steps in order.

        Returns True if every *required* step passed.  Non-required steps
        that fail are marked ``"warning"`` instead of ``"failed"`` and do
        not block the overall result.
        """
        # Reset all steps
        for step in self.steps:
            step.status = "pending"
            step.detail = ""
            step.progress = None
            step.awaiting_uac = False
        self._emit()

        for step in self.steps:
            # Check dependencies
            if not self._deps_passed(step):
                step.status = "skipped"
                step.detail = "Skipped: a prerequisite step failed."
                self._emit()
                continue

            step.status = "running"
            step.detail = f"Checking {step.name}..."
            self._emit()

            # Fast check — already satisfied?
            # force_recheck should refresh checks, not trigger setup/install
            # actions for steps that already pass.
            result = step.check()
            if result.get("passed"):
                step.status = "passed"
                step.detail = result.get("detail", "")
                step.progress = None
                step.awaiting_uac = False
                self._emit()
                continue

            # No action available — resolve from check result
            if step.action is None:
                if result.get("passed"):
                    step.status = "passed"
                else:
                    step.status = "failed" if step.required else "warning"
                step.detail = result.get("detail", "")
                step.progress = None
                step.awaiting_uac = False
                self._emit()
                continue

            # Run the fix action with a progress callback
            step.detail = f"Setting up {step.name}..."
            self._emit()

            def make_progress_cb(s):
                def progress_cb(detail, pct=None, awaiting_uac=False):
                    s.detail = detail
                    s.progress = pct
                    s.awaiting_uac = awaiting_uac
                    self._emit()
                return progress_cb

            result = step.action(make_progress_cb(step))
            if result.get("passed"):
                step.status = "passed"
            else:
                step.status = "failed" if step.required else "warning"
            step.detail = result.get("detail", "")
            step.progress = None
            step.awaiting_uac = False
            self._emit()

        return all(
            s.status in ("passed", "warning")
            for s in self.steps
        )

    # -- internals --

    def _deps_passed(self, step):
        for dep_id in step.depends_on:
            dep = self._step_map.get(dep_id)
            if dep is None or dep.status not in ("passed", "warning"):
                return False
        return True

    def _emit(self):
        self.on_update([s.snapshot() for s in self.steps])


# ---------------------------------------------------------------------------
# Step check / action wrappers
# ---------------------------------------------------------------------------

def _check_docker_installed():
    from docker_setup import check_docker_installed
    installed = check_docker_installed()
    return {
        "passed": installed,
        "detail": "Docker Desktop is installed." if installed
                  else "Docker Desktop is not installed.",
    }


def _install_docker(progress_cb):
    """Download and install Docker Desktop with progress + UAC handling."""
    from docker_setup import download_docker_desktop, install_docker_desktop

    # Download phase
    progress_cb("Downloading Docker Desktop (~500 MB)...", 0)
    path = download_docker_desktop(
        on_status=lambda msg: progress_cb(msg),
        on_progress=lambda downloaded, total, pct: progress_cb(
            f"Downloading Docker Desktop... {pct:.0f}%", pct * 0.5  # 0-50%
        ),
    )
    if not path:
        return {
            "passed": False,
            "detail": "Failed to download Docker Desktop. Check your internet connection.",
        }

    # Install phase — UAC prompt incoming
    progress_cb(
        "Installing Docker Desktop — Windows will ask for permission...",
        50,
        awaiting_uac=True,
    )
    result = install_docker_desktop(
        on_status=lambda msg: progress_cb(msg, None),
    )

    if not result["success"]:
        return {"passed": False, "detail": result["message"]}

    return {
        "passed": True,
        "detail": "Docker Desktop installed successfully.",
    }


def _check_docker_running():
    from docker_setup import check_docker_running
    running = check_docker_running()
    return {
        "passed": running,
        "detail": "Docker daemon is running." if running
                  else "Docker daemon is not running.",
    }


def _start_docker(progress_cb):
    """Start Docker Desktop and wait for the daemon to become responsive."""
    from docker_setup import _try_start_docker_desktop, wait_for_docker_ready

    progress_cb("Starting Docker Desktop...")
    _try_start_docker_desktop()

    ready = wait_for_docker_ready(
        timeout=120,
        on_status=lambda msg: progress_cb(msg),
    )
    if ready:
        return {"passed": True, "detail": "Docker daemon is running."}

    return {
        "passed": False,
        "detail": (
            "Docker is installed but the daemon did not start. "
            "A PC restart may be needed to finish Docker setup."
        ),
    }


def _check_gpu_in_docker():
    from docker_setup import resolve_gpu_verification
    result = resolve_gpu_verification(use_cache=True)
    gpu_name = result.get("gpu_docker_name", "")
    verified = result.get("gpu_verified", False)
    gpu_error = (result.get("gpu_error", "") or "").strip()
    if verified:
        return {"passed": True, "detail": f"GPU verified: {gpu_name}"}

    detail = "GPU not verified in Docker. GPU rendering may be unavailable."
    if gpu_error:
        compact_error = " ".join(gpu_error.split())
        if len(compact_error) > 220:
            compact_error = f"{compact_error[:217]}..."
        detail = f"{detail} Docker error: {compact_error}"

    return {
        "passed": False,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Step list factory
# ---------------------------------------------------------------------------

def build_preflight_steps():
    """Return the ordered list of preflight steps.

    To reorder: move items in this list.
    To add a step: insert a new PreflightStep at the desired position.
    """
    return [
        PreflightStep(
            id="docker_install",
            name="Docker Installation",
            check=_check_docker_installed,
            action=_install_docker,
            depends_on=[],
        ),
        PreflightStep(
            id="docker_running",
            name="Docker Running",
            check=_check_docker_running,
            action=_start_docker,
            depends_on=["docker_install"],
        ),
        PreflightStep(
            id="gpu_verify",
            name="GPU Verification",
            check=_check_gpu_in_docker,
            action=None,
            depends_on=["docker_running"],
            required=False,  # GPU failure is a warning, not a blocker
        ),
    ]
