"""
PC Rent Agent - Sidecar Entry Point
This is the main entry point when running as a Tauri sidecar.
Communicates with the Tauri app via stdin/stdout JSON messages.
"""

import os
import sys
import signal
import threading
import time

from ipc import (
    init_sidecar_mode,
    emit_status,
    emit_log,
    emit_system_info,
    emit_error,
    emit_job_complete,
    listen_commands,
)
from config import ensure_config_dir, load_config, save_config


def handle_command(cmd):
    """Dispatch an incoming command from Tauri."""
    action = cmd.get("cmd", "")

    if action == "connect":
        backend_url = cmd.get("backend_url", "")
        if backend_url:
            os.environ["BACKEND_URL"] = backend_url
            # Persist the URL
            cfg = load_config()
            cfg["backend_url"] = backend_url
            save_config(cfg)
        threading.Thread(target=run_agent_loop, daemon=True).start()

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
            from system_check import check_requirements
            info = check_requirements()
            from agent import detect_specs, get_cpu_cores, get_ram_gb
            specs = detect_specs()
            emit_system_info({
                "gpu_name": info.get("gpu_name", ""),
                "gpu_vram_gb": info.get("gpu_vram_gb", 0),
                "cpu_cores": specs.get("cpu_cores", 0),
                "ram_gb": specs.get("ram_gb", 0),
                "os_version": info.get("os_version", ""),
                "nvidia_driver": info.get("nvidia_driver", ""),
                "ready": info.get("ready", False),
                "issues": info.get("issues", []),
            })
        except Exception as e:
            emit_error(f"Failed to get system info: {e}")

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


def run_agent_loop():
    """Run the full agent lifecycle, emitting events at each stage."""
    import agent
    from system_check import check_requirements
    from docker_setup import full_bootstrap, check_docker_running

    ensure_config_dir()

    # Reload BACKEND_URL in case it was updated
    agent.BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")

    # Step 1: System requirements
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

    if not req["ready"]:
        for issue in req["issues"]:
            emit_error(issue)
        emit_status("error", "System requirements not met")
        return

    # Step 2: Docker bootstrap
    emit_status("setting_up_docker", "Setting up Docker...")

    def on_docker_status(msg):
        emit_log(msg, source="setup")

    docker_result = full_bootstrap(on_status=on_docker_status)

    if docker_result.get("needs_reboot"):
        emit_status("needs_reboot", docker_result["message"])
        return

    if not docker_result.get("ready"):
        emit_error(docker_result["message"])
        emit_status("error", "Docker setup failed")
        return

    if docker_result.get("gpu_verified"):
        emit_log("GPU rendering verified in Docker", source="setup")
    else:
        emit_log("WARNING: GPU not verified. Renders may use CPU only.", source="setup", level="warn")

    # Step 3: Render image
    emit_status("downloading_image", "Checking render image...")
    if not agent.ensure_docker_image():
        emit_log("No render image available. Will retry when jobs arrive.", source="setup", level="warn")

    # Step 4: Detect specs & register
    emit_status("registering", "Registering with backend...")
    specs = agent.detect_specs()

    saved_id = agent.load_machine_id()
    try:
        agent.machine_id = agent.register_machine(specs)
        agent.save_machine_id(agent.machine_id)
        emit_log(f"Registered. Machine ID: {agent.machine_id}", source="agent")
    except Exception as e:
        if saved_id:
            agent.machine_id = saved_id
            emit_log(f"Using saved machine ID: {saved_id}", source="agent", level="warn")
        else:
            emit_error(f"Registration failed: {e}")
            emit_status("error", "Failed to register")
            return

    agent.set_available(agent.machine_id)
    emit_status("connected", "Polling for jobs...", machine_id=agent.machine_id)

    # Step 5: Poll loop
    agent.running = True
    agent.pause_event.clear()
    agent.shutdown_event.clear()

    try:
        while agent.running:
            if agent.pause_event.is_set():
                time.sleep(agent.POLL_INTERVAL)
                continue

            try:
                if not check_docker_running():
                    emit_status("error", "Docker not running")
                    time.sleep(agent.POLL_INTERVAL * 2)
                    emit_status("connected", "Polling for jobs...", machine_id=agent.machine_id)
                    continue

                job = agent.poll_for_job(agent.machine_id)
                if job:
                    job_id = job["id"]
                    filename = job["input_filename"]
                    emit_status("rendering", f"Rendering {filename}",
                                job_id=job_id, filename=filename)
                    emit_log(f"Got job: {job_id} ({filename})", source="agent")

                    if not agent.check_image_loaded():
                        emit_log("Render image not loaded, downloading...", source="agent")
                        if not agent.ensure_docker_image():
                            emit_error("Cannot load render image")
                            agent.update_job_status(job_id, "failed", error="Render image not available")
                            emit_status("connected", "Polling for jobs...", machine_id=agent.machine_id)
                            continue

                    agent.execute_job(job)

                    # Determine what happened
                    if agent.should_offer_capacity():
                        agent.set_available(agent.machine_id)
                        emit_status("connected", "Polling for jobs...", machine_id=agent.machine_id)
                    elif agent.pause_event.is_set() and not agent.shutdown_event.is_set():
                        emit_status("paused", "Agent paused")
                else:
                    time.sleep(agent.POLL_INTERVAL)

            except Exception as e:
                if "ConnectionError" in type(e).__name__:
                    emit_log(f"Cannot reach backend, retrying...", source="agent", level="warn")
                else:
                    emit_log(f"Error: {e}", source="agent", level="error")
                time.sleep(agent.POLL_INTERVAL)

    finally:
        if agent.machine_id:
            agent.set_idle(agent.machine_id)
        emit_status("disconnected", "Agent stopped")


def main():
    init_sidecar_mode()
    ensure_config_dir()

    # Load saved backend URL
    cfg = load_config()
    if cfg.get("backend_url"):
        os.environ.setdefault("BACKEND_URL", cfg["backend_url"])

    # Emit initial status
    emit_status("disconnected", "Ready")

    # Handle signals gracefully
    def sig_handler(sig, frame):
        try:
            import agent
            agent.shutdown_agent(f"Signal {sig}")
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    # Listen for commands from Tauri (blocks until stdin closes)
    listen_commands(handle_command)


if __name__ == "__main__":
    main()
