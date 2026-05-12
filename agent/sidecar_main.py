"""
PC Rent Agent - Sidecar Entry Point
This is the main entry point when running as a Tauri sidecar.
Communicates with the Tauri app via stdin/stdout JSON messages.
"""

import os
import signal
import sys
import threading
import time

from ipc import (
    init_sidecar_mode,
    emit_status,
    emit_log,
    emit_system_info,
    emit_runtime_info,
    emit_error,
    emit_uac_prompt,
    emit_preflight_steps,
    listen_commands,
)
from config import ensure_config_dir, load_config, save_config

DEFAULT_RUNTIME_STATE = {
    "preflight_complete": False,
    "preflight_passed": None,
    "preflight_message": "Preflight has not run yet.",
    "requirements_checked": False,
    "requirements_ready": None,
    "requirement_issues": [],
    "docker_installed": None,
    "docker_running": None,
    "gpu_verified": None,
    "image_present": None,
    "image_stage": "idle",
    "image_downloaded_bytes": None,
    "image_total_bytes": None,
    "image_progress_pct": None,
    "image_status": "Not checked yet.",
}

_UNSET = object()
_state_lock = threading.Lock()
_runtime_state = dict(DEFAULT_RUNTIME_STATE)
_preflight_running = False
_connect_running = False


def get_runtime_state():
    with _state_lock:
        return dict(_runtime_state)


def set_runtime_state(snapshot):
    """Replace runtime fields while preserving the current preflight result."""
    with _state_lock:
        preserved = {
            "preflight_complete": _runtime_state["preflight_complete"],
            "preflight_passed": _runtime_state["preflight_passed"],
            "preflight_message": _runtime_state["preflight_message"],
        }
        _runtime_state.clear()
        _runtime_state.update(DEFAULT_RUNTIME_STATE)
        _runtime_state.update(snapshot)
        _runtime_state.update(preserved)
        current = dict(_runtime_state)

    emit_runtime_info(current)
    return current


def patch_runtime_state(**patch):
    """Update runtime fields and emit the full snapshot."""
    with _state_lock:
        _runtime_state.update(patch)
        current = dict(_runtime_state)

    emit_runtime_info(current)
    return current


def set_preflight_state(*, complete=None, passed=_UNSET, message=None):
    patch = {}
    if complete is not None:
        patch["preflight_complete"] = complete
    if passed is not _UNSET:
        patch["preflight_passed"] = passed
    if message is not None:
        patch["preflight_message"] = message
    return patch_runtime_state(**patch)


def _get_flags():
    with _state_lock:
        return _preflight_running, _connect_running


def _set_preflight_running(value):
    global _preflight_running
    with _state_lock:
        _preflight_running = value


def _set_connect_running(value):
    global _connect_running
    with _state_lock:
        _connect_running = value


def emit_local_runtime_state():
    """Emit a one-shot snapshot of local requirements, specs, and image state."""
    import agent
    from system_check import check_requirements

    req = check_requirements()
    specs = agent.detect_specs()
    emit_system_info({
        "gpu_name": req.get("gpu_name", ""),
        "gpu_vram_gb": req.get("gpu_vram_gb", 0),
        "cpu_cores": specs.get("cpu_cores", 0),
        "ram_gb": specs.get("ram_gb", 0),
        "os_version": req.get("os_version", ""),
        "nvidia_driver": req.get("nvidia_driver", ""),
        "ready": req.get("ready", False),
        "issues": req.get("issues", []),
    })
    set_runtime_state(agent.get_runtime_status())


def handle_command(cmd):
    """Dispatch an incoming command from Tauri."""
    action = cmd.get("cmd", "")

    if action == "run_preflight":
        preflight_running, connect_running = _get_flags()
        if connect_running:
            emit_error("Disconnect before running preflight again.")
            return
        if preflight_running:
            emit_log("Preflight is already running.", source="setup")
            return

        # force=True when user manually clicks Refresh — bypasses GPU result cache
        force = cmd.get("force", False)
        _set_preflight_running(True)
        threading.Thread(target=run_preflight_flow, args=(force,), daemon=True).start()

    elif action == "connect":
        backend_url = cmd.get("backend_url", "")
        firebase_token = cmd.get("firebase_token", "")
        commitment_seconds = cmd.get("commitment_seconds")
        cfg = load_config()
        if backend_url:
            os.environ["BACKEND_URL"] = backend_url
            cfg["backend_url"] = backend_url
        if firebase_token:
            import agent as _agent_mod
            _agent_mod.FIREBASE_TOKEN = firebase_token
        save_config(cfg)

        # The server's /machines/register rejects a missing or <=0 value
        # with 400, so reject here too -- avoids a wasted preflight gate
        # check + clearer error surfaced to the UI.
        try:
            commitment_seconds = float(commitment_seconds)
        except (TypeError, ValueError):
            commitment_seconds = 0.0
        if commitment_seconds <= 0:
            emit_error("Pick how long you'll keep the PC available before connecting.")
            emit_status("disconnected", "Commitment window required")
            return

        preflight_running, connect_running = _get_flags()
        runtime = get_runtime_state()
        if preflight_running:
            emit_error("Preflight is still running.")
            emit_status("disconnected", "Waiting for preflight to finish")
            return
        if not runtime.get("preflight_complete"):
            emit_error("Preflight has not completed yet.")
            emit_status("disconnected", "Waiting for preflight to finish")
            return
        if connect_running:
            emit_log("Connect is already in progress.", source="agent", level="warn")
            return

        _set_connect_running(True)
        threading.Thread(
            target=run_connect_flow, args=(commitment_seconds,), daemon=True,
        ).start()

    elif action == "disconnect":
        import agent
        # Best-effort: tell the server we're retiring before the agent
        # tears itself down.  Snaps commitment_end_at = now on the row
        # so the planner stops considering us on its next snapshot.
        # HTTP failures here just log -- the row's window will expire
        # naturally and the planner will drop us then.
        try:
            agent.set_commitment(getattr(agent, "machine_id", None), 0)
        except Exception as exc:
            emit_log(f"set_commitment(0) failed: {exc}", source="agent", level="warn")
        agent.shutdown_agent("Disconnected from UI")

    elif action == "pause":
        import agent
        agent.pause_agent("Paused from UI")
        emit_status("paused", "Agent paused")

    elif action == "resume":
        import agent
        agent.resume_agent()
        emit_status("connected", "Polling for jobs...")

    elif action == "stop_job":
        import agent
        if not agent.request_stop_current_job("Stopped from UI"):
            emit_log("No active render job to stop", level="warn")

    elif action == "get_system_info":
        try:
            emit_local_runtime_state()
        except Exception as exc:
            emit_error(f"Failed to get system info: {exc}")

    elif action == "get_runtime_status":
        try:
            emit_local_runtime_state()
        except Exception as exc:
            emit_error(f"Failed to inspect runtime state: {exc}")

    elif action == "remove_image":
        try:
            import agent

            snapshot = agent.get_active_job_snapshot()
            if snapshot["id"]:
                emit_error("Cannot remove the render image while a job is running.")
                return

            emit_status("removing_image", "Removing render image...")
            patch_runtime_state(
                image_stage="removing",
                image_status="Removing render image...",
                image_progress_pct=None,
            )

            result = agent.remove_docker_image()
            set_runtime_state(agent.get_runtime_status())
            patch_runtime_state(image_status=result["message"])

            if result["ok"]:
                emit_log(result["message"], source="setup")
                emit_status("disconnected", result["message"])
            else:
                emit_error(result["message"])
                emit_status("error", result["message"])
        except Exception as exc:
            emit_error(f"Failed to remove render image: {exc}")

    elif action == "get_status":
        try:
            import agent

            snapshot = agent.get_active_job_snapshot()
            if snapshot["id"]:
                state = "rendering"
            elif agent.pause_event.is_set():
                state = "paused"
            elif agent.shutdown_event.is_set():
                state = "disconnected"
            elif agent.machine_id:
                state = "connected"
            else:
                state = "disconnected"
            emit_status(state, machine_id=agent.machine_id or "")
        except Exception:
            emit_status("disconnected")


def _sync_runtime_from_steps(steps_snapshot):
    """Translate step statuses into existing runtime_info fields for backward compat."""
    resolved = ("passed", "failed", "warning")
    patch = {}
    for step in steps_snapshot:
        sid = step["id"]
        passed = step["status"] == "passed"
        if sid == "docker_install":
            patch["docker_installed"] = passed if step["status"] in resolved else None
        elif sid == "docker_running":
            patch["docker_running"] = passed if step["status"] in resolved else None
        elif sid == "gpu_verify":
            patch["gpu_verified"] = passed if step["status"] in resolved else None
    patch_runtime_state(**patch)


def run_preflight_flow(force_gpu_recheck=False):
    """Run launch-time preflight before the user can connect.

    Uses the modular PreflightRunner to execute ordered steps, emitting
    both the new ``preflight_steps`` event and the legacy ``runtime_info``
    fields so the existing UI stays in sync during the transition.
    """
    import agent
    from system_check import check_requirements
    from docker_setup import clear_gpu_check_cache
    from preflight_steps import build_preflight_steps, PreflightRunner

    ensure_config_dir()
    agent.BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")

    try:
        set_runtime_state(agent.get_runtime_status())
        set_preflight_state(complete=False, passed=None, message="Running preflight...")

        # ---- System requirements pre-check (populates GpuInfoCard) ----
        emit_status("checking_requirements", "Checking system requirements...")
        req = check_requirements()
        emit_system_info({
            "gpu_name": req.get("gpu_name", ""),
            "gpu_vram_gb": req.get("gpu_vram_gb", 0),
            "cpu_cores": agent.get_cpu_cores(),
            "ram_gb": agent.get_ram_gb(),
            "os_version": req.get("os_version", ""),
            "nvidia_driver": req.get("nvidia_driver", ""),
            "ready": req.get("ready", False),
            "issues": req.get("issues", []),
        })
        patch_runtime_state(
            requirements_checked=True,
            requirements_ready=req.get("ready", False),
            requirement_issues=req.get("issues", []),
        )

        if not req["ready"]:
            set_runtime_state(agent.get_runtime_status())
            set_preflight_state(
                complete=True,
                passed=False,
                message="System requirements not met.",
            )
            for issue in req["issues"]:
                emit_error(issue)
            emit_status("error", "System requirements not met")
            return

        # ---- Modular step pipeline ----
        emit_status("setting_up_docker", "Setting up Docker...")

        if force_gpu_recheck:
            clear_gpu_check_cache()

        steps = build_preflight_steps()

        def on_steps_update(steps_snapshot):
            emit_preflight_steps(steps_snapshot)
            _sync_runtime_from_steps(steps_snapshot)
            # Forward step log lines
            for step in steps_snapshot:
                if step["status"] == "running" and step["detail"]:
                    emit_log(step["detail"], source="setup")

        runner = PreflightRunner(steps, on_update=on_steps_update)
        all_passed = runner.run(force_recheck=force_gpu_recheck)

        # Sync final runtime state
        runtime_snapshot = agent.get_runtime_status()
        for step in steps:
            if step.id == "docker_install" and step.status == "passed":
                runtime_snapshot["docker_installed"] = True
            if step.id == "docker_running" and step.status == "passed":
                runtime_snapshot["docker_running"] = True
            if step.id == "gpu_verify":
                runtime_snapshot["gpu_verified"] = step.status == "passed"
        set_runtime_state(runtime_snapshot)

        if all_passed:
            # Log GPU result
            gpu_step = next((s for s in steps if s.id == "gpu_verify"), None)
            if gpu_step and gpu_step.status == "passed":
                emit_log(f"{gpu_step.detail}", source="setup")
            elif gpu_step and gpu_step.status == "warning":
                emit_log(
                    f"WARNING: {gpu_step.detail}",
                    source="setup", level="warn",
                )

            set_preflight_state(complete=True, passed=True, message="Ready to connect.")
            emit_status("disconnected", "Ready to connect.")
        else:
            failed = [s for s in steps if s.status == "failed"]
            msg = failed[0].detail if failed else "Preflight failed."
            # Only flag reboot for Docker daemon failures, not GPU warnings
            needs_reboot = any(
                s.id == "docker_running" and "restart" in s.detail.lower()
                for s in failed
            )

            set_preflight_state(complete=True, passed=False, message=msg)
            if needs_reboot:
                emit_status("needs_reboot", msg)
            else:
                for s in failed:
                    emit_error(s.detail)
                emit_status("error", "Docker setup failed")

    except Exception as exc:
        failure_message = f"Preflight failed: {exc}"
        emit_error(failure_message)
        set_preflight_state(
            complete=True,
            passed=False,
            message=failure_message,
        )
        emit_status("error", "Preflight failed")
    finally:
        _set_preflight_running(False)


def run_connect_flow(commitment_seconds):
    """Connect to the backend after preflight has already completed.

    ``commitment_seconds`` is the user-chosen availability window from
    the dashboard datetime picker.  Threaded through to register_machine
    so the server can stamp commitment_end_at on the machine row.
    """
    import agent
    from docker_setup import check_docker_running

    ensure_config_dir()
    agent.BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")

    runtime_state = get_runtime_state()

    def update_runtime(**patch):
        runtime_state.update(patch)
        patch_runtime_state(**patch)

    def on_image_stage(stage_name, message, progress=None, **_extra):
        patch = {
            "image_stage": stage_name,
            "image_status": message,
        }
        if progress is not None:
            patch["image_progress_pct"] = progress
        if stage_name == "ready":
            patch["image_present"] = True
            patch["image_downloaded_bytes"] = None
            patch["image_total_bytes"] = None
            patch["image_progress_pct"] = 100 if progress is None else progress
        elif stage_name == "missing":
            patch["image_present"] = False
            patch["image_downloaded_bytes"] = None
            patch["image_total_bytes"] = None
            patch["image_progress_pct"] = None
        elif stage_name == "installing":
            patch["image_progress_pct"] = 100
        elif stage_name == "error":
            patch["image_present"] = False
        update_runtime(**patch)

        if stage_name in {"checking", "downloading"}:
            emit_status("downloading_image", message)
        elif stage_name == "installing":
            emit_status("installing_image", message)

    def on_image_progress(downloaded, total, pct):
        message = f"Downloading render image... {pct}%"
        update_runtime(
            image_stage="downloading",
            image_present=False,
            image_downloaded_bytes=downloaded,
            image_total_bytes=total,
            image_progress_pct=pct,
            image_status=message,
        )
        emit_status("downloading_image", message)

    try:
        agent.running = True
        agent.pause_event.clear()
        agent.shutdown_event.clear()

        # Always run docker pull at startup so peers with a cached image
        # pick up new digests pushed to GHCR without manual refresh.
        # docker pull is a no-op on digest match (a single registry HEAD).
        # On failure (offline etc) any cached copy stays usable; the per-job
        # fallback below handles the genuinely-missing case.
        emit_status("downloading_image", "Refreshing render image...")
        agent.ensure_docker_image(on_stage=on_image_stage, on_progress=on_image_progress)
        if agent.check_image_loaded():
            update_runtime(
                image_present=True,
                image_stage="ready",
                image_status="Render image ready.",
                image_downloaded_bytes=None,
                image_total_bytes=None,
                image_progress_pct=100,
            )
        else:
            update_runtime(
                image_present=False,
                image_stage="missing",
                image_status="No render image available — will retry on first job.",
                image_downloaded_bytes=None,
                image_total_bytes=None,
                image_progress_pct=None,
            )

        if agent.shutdown_event.is_set():
            emit_status("disconnected", "Agent stopped")
            return

        emit_status("registering", "Registering with backend...")
        specs = agent.detect_specs()

        saved_id = agent.load_machine_id()
        try:
            agent.machine_id = agent.register_machine(specs, commitment_seconds)
            agent.save_machine_id(agent.machine_id)
            emit_log(f"Registered. Machine ID: {agent.machine_id}", source="agent")
        except Exception as exc:
            if saved_id:
                agent.machine_id = saved_id
                emit_log(f"Using saved machine ID: {saved_id}", source="agent", level="warn")
            else:
                emit_error(f"Registration failed: {exc}")
                emit_status("error", "Failed to register")
                return

        if agent.shutdown_event.is_set():
            emit_status("disconnected", "Agent stopped")
            return

        agent.set_available(agent.machine_id)
        emit_status("connected", "Polling for jobs...", machine_id=agent.machine_id)

        try:
            while agent.running:
                if agent.pause_event.is_set():
                    time.sleep(agent.POLL_INTERVAL)
                    continue

                try:
                    if not check_docker_running():
                        emit_status("error", "Docker not running")
                        update_runtime(docker_running=False)
                        time.sleep(agent.POLL_INTERVAL * 2)
                        update_runtime(docker_running=True)
                        emit_status("connected", "Polling for jobs...", machine_id=agent.machine_id)
                        continue

                    job = agent.poll_for_job(agent.machine_id)
                    if job:
                        job_id = job["id"]
                        filename = job["input_filename"]
                        emit_log(f"Got job: {job_id} ({filename})", source="agent")
                        agent.update_job_status(job_id, "running")

                        if not agent.check_image_loaded():
                            emit_log(f"Render image {agent.COMMUNITY_IMAGE} not loaded, downloading...", source="agent")
                            if not agent.ensure_docker_image(on_stage=on_image_stage, on_progress=on_image_progress):
                                emit_error("Cannot load render image")
                                agent.notify_orchestrator_failure(job_id, "Render image not available")
                                image_stage = runtime_state["image_stage"] if runtime_state["image_stage"] == "error" else "missing"
                                image_status = runtime_state["image_status"] if image_stage == "error" else "Render image not available."
                                update_runtime(
                                    image_present=False,
                                    image_stage=image_stage,
                                    image_status=image_status,
                                    image_downloaded_bytes=None,
                                    image_total_bytes=None,
                                    image_progress_pct=None,
                                )
                                emit_status("connected", "Polling for jobs...", machine_id=agent.machine_id)
                                continue
                            update_runtime(
                                image_present=True,
                                image_stage="ready",
                                image_status="Render image ready.",
                                image_downloaded_bytes=None,
                                image_total_bytes=None,
                                image_progress_pct=100,
                            )

                        emit_status(
                            "rendering",
                            f"Rendering {filename}",
                            job_id=job_id,
                            filename=filename,
                        )
                        agent.execute_job(job)

                        if agent.should_offer_capacity():
                            agent.set_available(agent.machine_id)
                            emit_status("connected", "Polling for jobs...", machine_id=agent.machine_id)
                        elif agent.pause_event.is_set() and not agent.shutdown_event.is_set():
                            emit_status("paused", "Agent paused")
                    else:
                        time.sleep(agent.POLL_INTERVAL)

                except Exception as exc:
                    if "ConnectionError" in type(exc).__name__:
                        emit_log("Cannot reach backend, retrying...", source="agent", level="warn")
                    else:
                        emit_log(f"Error: {exc}", source="agent", level="error")
                    time.sleep(agent.POLL_INTERVAL)
        finally:
            if agent.machine_id:
                agent.set_idle(agent.machine_id)
            emit_status("disconnected", "Agent stopped")
    finally:
        _set_connect_running(False)


def main():
    init_sidecar_mode()
    import agent as _agent

    _agent.set_sidecar_mode(True)
    ensure_config_dir()

    cfg = load_config()
    if cfg.get("backend_url"):
        os.environ.setdefault("BACKEND_URL", cfg["backend_url"])

    emit_status("disconnected", "Ready")

    def sig_handler(sig, frame):
        try:
            import agent

            agent.shutdown_agent(f"Signal {sig}")
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    listen_commands(handle_command)


if __name__ == "__main__":
    main()
