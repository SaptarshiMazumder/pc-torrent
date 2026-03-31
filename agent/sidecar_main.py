"""
PC Rent Agent - Sidecar Entry Point
This is the main entry point when running as a Tauri sidecar.
Communicates with the Tauri app via stdin/stdout JSON messages.
"""

import argparse
import json
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
    "wsl_ready": None,
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


def _runtime_needs_setup(runtime):
    return not (
        runtime.get("preflight_passed") is True
        and runtime.get("requirements_ready") is True
        and runtime.get("wsl_ready") is True
        and runtime.get("docker_installed") is True
        and runtime.get("docker_running") is True
    )


def _format_setup_message(message):
    text = (message or "").strip()
    if text.startswith("[SETUP]"):
        text = text[len("[SETUP]"):].strip()
    return text or "Setting up Docker..."


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

        _set_preflight_running(True)
        threading.Thread(target=run_preflight_flow, daemon=True).start()

    elif action == "run_wsl_setup":
        preflight_running, connect_running = _get_flags()
        if connect_running:
            emit_error("Disconnect before installing WSL.")
            return
        if preflight_running:
            emit_log("Setup is already running.", source="setup")
            return

        _set_preflight_running(True)
        threading.Thread(target=run_wsl_setup_flow, daemon=True).start()

    elif action == "run_docker_setup":
        preflight_running, connect_running = _get_flags()
        if connect_running:
            emit_error("Disconnect before installing Docker.")
            return
        if preflight_running:
            emit_log("Setup is already running.", source="setup")
            return

        _set_preflight_running(True)
        threading.Thread(target=run_docker_setup_flow, daemon=True).start()

    elif action == "connect":
        backend_url = cmd.get("backend_url", "")
        if backend_url:
            os.environ["BACKEND_URL"] = backend_url
            cfg = load_config()
            cfg["backend_url"] = backend_url
            save_config(cfg)

        preflight_running, connect_running = _get_flags()
        runtime = get_runtime_state()
        if preflight_running:
            emit_error("Preflight is still running.")
            emit_status("disconnected", "Waiting for preflight to finish")
            return
        if connect_running:
            emit_log("Connect is already in progress.", source="agent", level="warn")
            return

        if _runtime_needs_setup(runtime):
            emit_log("Running setup before connect...", source="setup")
            _set_preflight_running(True)
            threading.Thread(target=run_preflight_then_connect_flow, daemon=True).start()
            return

        _set_connect_running(True)
        threading.Thread(target=run_connect_flow, daemon=True).start()

    elif action == "disconnect":
        import agent
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


def run_preflight_flow():
    """Run launch-time preflight before the user can connect."""
    import agent
    from system_check import check_requirements
    from docker_setup import full_bootstrap

    ensure_config_dir()
    agent.BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")

    try:
        set_runtime_state(agent.get_runtime_status())
        set_preflight_state(complete=False, passed=None, message="Running preflight...")

        emit_status("checking_requirements", "Checking system requirements...")
        set_preflight_state(message="Checking system requirements...")
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

        emit_status("setting_up_docker", "Preparing runtime setup...")
        set_preflight_state(message="Preparing runtime setup...")

        def on_docker_status(msg):
            display = _format_setup_message(msg)
            emit_log(msg, source="setup")
            emit_status("setting_up_docker", display)
            set_preflight_state(message=display)
            patch_runtime_state(image_status=display)

        docker_result = full_bootstrap(on_status=on_docker_status)
        runtime_snapshot = agent.get_runtime_status()

        if docker_result.get("ready"):
            runtime_snapshot.update({
                "docker_installed": True,
                "docker_running": True,
                "gpu_verified": docker_result.get("gpu_verified", False),
            })

        set_runtime_state(runtime_snapshot)

        if docker_result.get("needs_reboot"):
            set_preflight_state(
                complete=True,
                passed=False,
                message=docker_result["message"],
            )
            emit_status("needs_reboot", docker_result["message"])
            return

        if not docker_result.get("ready"):
            emit_error(docker_result["message"])
            set_preflight_state(
                complete=True,
                passed=False,
                message=docker_result["message"],
            )
            emit_status("error", "Docker setup failed")
            return

        gpu_docker = docker_result.get("gpu_docker_name", "")
        if gpu_docker:
            emit_log(
                f"GPU in Docker: {gpu_docker} - ready for rendering.",
                source="setup",
            )
        else:
            emit_log(
                "WARNING: GPU not accessible in Docker. Renders will use CPU only.",
                source="setup",
                level="warn",
            )

        ready_message = "Ready to connect."
        set_preflight_state(
            complete=True,
            passed=True,
            message=ready_message,
        )
        emit_status("disconnected", ready_message)
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


def run_wsl_setup_flow():
    """Install/repair WSL2 only."""
    import agent
    from docker_setup import ensure_wsl2_ready

    ensure_config_dir()
    agent.BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")

    try:
        set_runtime_state(agent.get_runtime_status())
        set_preflight_state(complete=False, passed=None, message="Installing WSL2...")
        emit_status("setting_up_docker", "Installing WSL2...")
        patch_runtime_state(image_status="Installing WSL2...")

        def on_setup_status(msg):
            display = _format_setup_message(msg)
            emit_log(msg, source="setup")
            emit_status("setting_up_docker", display)
            set_preflight_state(message=display)
            patch_runtime_state(image_status=display)

        result = ensure_wsl2_ready(on_status=on_setup_status)
        runtime_snapshot = agent.get_runtime_status()
        set_runtime_state(runtime_snapshot)

        if result.get("needs_reboot"):
            message = result.get("message") or "Reboot required to complete WSL setup."
            set_preflight_state(complete=True, passed=False, message=message)
            emit_status("needs_reboot", message)
            return

        if not result.get("ready"):
            message = result.get("message") or "WSL setup failed."
            emit_error(message)
            set_preflight_state(complete=True, passed=False, message=message)
            emit_status("error", "WSL setup failed")
            return

        ready_message = result.get("message") or "WSL2 is ready."
        ready_for_connect = (
            runtime_snapshot.get("requirements_ready") is True
            and runtime_snapshot.get("wsl_ready") is True
            and runtime_snapshot.get("docker_installed") is True
            and runtime_snapshot.get("docker_running") is True
        )
        set_preflight_state(complete=True, passed=ready_for_connect, message=ready_message)
        emit_status("disconnected", ready_message)
    except Exception as exc:
        failure_message = f"WSL setup failed: {exc}"
        emit_error(failure_message)
        set_preflight_state(complete=True, passed=False, message=failure_message)
        emit_status("error", "WSL setup failed")
    finally:
        _set_preflight_running(False)


def run_docker_setup_flow():
    """Install/repair Docker runtime (includes WSL checks)."""
    import agent
    from docker_setup import full_bootstrap

    ensure_config_dir()
    agent.BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")

    try:
        set_runtime_state(agent.get_runtime_status())
        set_preflight_state(complete=False, passed=None, message="Preparing runtime setup...")
        emit_status("setting_up_docker", "Preparing runtime setup...")
        patch_runtime_state(image_status="Preparing runtime setup...")

        def on_setup_status(msg):
            display = _format_setup_message(msg)
            emit_log(msg, source="setup")
            emit_status("setting_up_docker", display)
            set_preflight_state(message=display)
            patch_runtime_state(image_status=display)

        result = full_bootstrap(on_status=on_setup_status)
        runtime_snapshot = agent.get_runtime_status()

        if result.get("ready"):
            runtime_snapshot.update({
                "docker_installed": True,
                "docker_running": True,
                "gpu_verified": result.get("gpu_verified", False),
            })

        set_runtime_state(runtime_snapshot)

        if result.get("needs_reboot"):
            message = result.get("message") or "Reboot required to complete setup."
            set_preflight_state(complete=True, passed=False, message=message)
            emit_status("needs_reboot", message)
            return

        if not result.get("ready"):
            message = result.get("message") or "Docker setup failed."
            emit_error(message)
            set_preflight_state(complete=True, passed=False, message=message)
            emit_status("error", "Docker setup failed")
            return

        ready_for_connect = (
            runtime_snapshot.get("requirements_ready") is True
            and runtime_snapshot.get("wsl_ready") is True
            and runtime_snapshot.get("docker_installed") is True
            and runtime_snapshot.get("docker_running") is True
        )
        ready_message = "Docker runtime is ready."
        set_preflight_state(complete=True, passed=ready_for_connect, message=ready_message)
        emit_status("disconnected", ready_message)
    except Exception as exc:
        failure_message = f"Docker setup failed: {exc}"
        emit_error(failure_message)
        set_preflight_state(complete=True, passed=False, message=failure_message)
        emit_status("error", "Docker setup failed")
    finally:
        _set_preflight_running(False)


def run_preflight_then_connect_flow():
    """Run setup and auto-connect only when setup passes."""
    try:
        run_preflight_flow()

        runtime = get_runtime_state()
        if runtime.get("preflight_passed") is not True:
            emit_log("Setup did not pass. Connect aborted.", source="setup", level="warn")
            return

        _set_connect_running(True)
        run_connect_flow()
    except Exception as exc:
        emit_error(f"Connect setup failed: {exc}")
        emit_status("error", "Connect setup failed")


def run_connect_flow():
    """Connect to the backend after preflight has already completed."""
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

        emit_status("downloading_image", "Checking render image...")
        if not agent.ensure_docker_image(on_stage=on_image_stage, on_progress=on_image_progress):
            if runtime_state.get("image_stage") == "error":
                emit_error(runtime_state.get("image_status") or "Render runtime setup failed.")
                emit_status("error", "Render runtime setup failed")
                return

            emit_log(
                "No render image available. Will retry when jobs arrive.",
                source="setup",
                level="warn",
            )
            image_present = agent.check_image_loaded()
            image_stage = runtime_state["image_stage"] if runtime_state["image_stage"] == "error" else "missing"
            image_status = runtime_state["image_status"] if image_stage == "error" else "Render image not available yet."
            update_runtime(
                image_present=image_present,
                image_stage=image_stage,
                image_status=image_status,
                image_downloaded_bytes=None,
                image_total_bytes=None,
                image_progress_pct=None,
            )
        else:
            update_runtime(
                image_present=agent.check_image_loaded(),
                image_stage="ready",
                image_status="Render image ready.",
                image_downloaded_bytes=None,
                image_total_bytes=None,
                image_progress_pct=100,
            )

        if agent.shutdown_event.is_set():
            emit_status("disconnected", "Agent stopped")
            return

        emit_status("registering", "Registering with backend...")
        specs = agent.detect_specs()

        saved_id = agent.load_machine_id()
        try:
            agent.machine_id = agent.register_machine(specs)
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
                            emit_log("Render image not loaded, downloading...", source="agent")
                            if not agent.ensure_docker_image(on_stage=on_image_stage, on_progress=on_image_progress):
                                emit_error("Cannot load render image")
                                agent.update_job_status(job_id, "failed", error="Render image not available")
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
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--analyze-file", dest="analyze_file")
    args, _ = parser.parse_known_args()

    if args.analyze_file:
        try:
            from project_analyzer import analyze_project_file

            result = analyze_project_file(args.analyze_file)
        except Exception as exc:
            result = {
                "ok": False,
                "frame_start": None,
                "frame_end": None,
                "frame_step": None,
                "total_frames": None,
                "method": "manual_required",
                "error": f"Analyzer crashed: {type(exc).__name__}: {exc}",
            }

        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        sys.stdout.flush()
        return 0 if result.get("ok") else 2

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
