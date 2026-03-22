"""
PC Rent Agent - System Tray App
Provides a tray icon with status updates while the agent runs in the background.
"""

import threading
import sys
import os

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False


# Status constants
STATUS_SETUP = "setup"
STATUS_AVAILABLE = "available"
STATUS_RENDERING = "rendering"
STATUS_ERROR = "error"
STATUS_OFFLINE = "offline"

# Colors for each status
STATUS_COLORS = {
    STATUS_SETUP: (128, 128, 128),    # Gray
    STATUS_AVAILABLE: (0, 180, 0),     # Green
    STATUS_RENDERING: (0, 120, 255),   # Blue
    STATUS_ERROR: (220, 40, 40),       # Red
    STATUS_OFFLINE: (80, 80, 80),      # Dark gray
}

STATUS_LABELS = {
    STATUS_SETUP: "Setting up...",
    STATUS_AVAILABLE: "Available",
    STATUS_RENDERING: "Rendering...",
    STATUS_ERROR: "Error",
    STATUS_OFFLINE: "Offline",
}


def _create_icon_image(color, size=64):
    """Create a simple colored circle icon."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = 4
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=color,
        outline=(255, 255, 255, 200),
        width=2,
    )
    return image


class TrayApp:
    """System tray icon for the PC Rent agent."""

    def __init__(self, on_pause=None, on_resume=None, on_stop_job=None, on_exit=None):
        self._status = STATUS_SETUP
        self._detail = ""
        self._gpu_info = ""
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_stop_job = on_stop_job
        self._on_exit = on_exit
        self._paused = False
        self._icon = None

    def _build_menu(self):
        status_text = STATUS_LABELS.get(self._status, self._status)
        if self._detail:
            status_text += f" - {self._detail}"

        items = [
            pystray.MenuItem(f"Status: {status_text}", None, enabled=False),
        ]

        if self._gpu_info:
            items.append(pystray.MenuItem(f"GPU: {self._gpu_info}", None, enabled=False))

        items.append(pystray.Menu.SEPARATOR)

        if self._paused:
            items.append(pystray.MenuItem("Resume", self._handle_resume))
        else:
            items.append(pystray.MenuItem("Pause", self._handle_pause))

        if self._status == STATUS_RENDERING:
            items.append(pystray.MenuItem("Stop Current Job", self._handle_stop_job))

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Exit", self._handle_exit))

        return pystray.Menu(*items)

    def _handle_pause(self, icon, item):
        self._paused = True
        if self._on_pause:
            self._on_pause()
        self.update_status(STATUS_OFFLINE, "Paused")

    def _handle_resume(self, icon, item):
        self._paused = False
        if self._on_resume:
            self._on_resume()
        self.update_status(STATUS_AVAILABLE)

    def _handle_stop_job(self, icon, item):
        if self._on_stop_job:
            self._on_stop_job()

    def _handle_exit(self, icon, item):
        if self._on_exit:
            self._on_exit()
        if self._icon:
            self._icon.stop()

    def set_gpu_info(self, info):
        self._gpu_info = info
        self._refresh()

    def update_status(self, status, detail=""):
        self._status = status
        self._detail = detail
        self._refresh()

    def _refresh(self):
        if self._icon:
            color = STATUS_COLORS.get(self._status, (128, 128, 128))
            self._icon.icon = _create_icon_image(color)
            self._icon.menu = self._build_menu()

            title = f"PC Rent - {STATUS_LABELS.get(self._status, self._status)}"
            if self._detail:
                title += f": {self._detail}"
            self._icon.title = title

    def run(self):
        """Run the tray icon (blocks - call from main thread or dedicated thread)."""
        color = STATUS_COLORS[self._status]
        self._icon = pystray.Icon(
            name="PCRentAgent",
            icon=_create_icon_image(color),
            title="PC Rent Agent",
            menu=self._build_menu(),
        )
        self._icon.run()

    def stop(self):
        if self._icon:
            self._icon.stop()


def run_agent_with_tray():
    """
    Start the agent with a system tray icon.
    Tray runs on the main thread, agent runs in a background thread.
    """
    if not HAS_TRAY:
        print("[TRAY] pystray/Pillow not installed, running without tray icon.")
        from agent import main
        main()
        return

    # Import agent module
    import agent

    def on_pause():
        agent.pause_agent("Paused from tray")

    def on_resume():
        agent.resume_agent()

    def on_stop_job():
        agent.request_stop_current_job("Render stopped from the tray")

    def on_exit():
        agent.shutdown_agent("Exit requested from tray")

    tray = TrayApp(on_pause=on_pause, on_resume=on_resume, on_stop_job=on_stop_job, on_exit=on_exit)

    def agent_thread():
        """Modified agent main loop that updates tray status."""
        import signal
        import time
        import requests as req_lib

        agent.ensure_config_dir()

        tray.update_status(STATUS_SETUP, "Checking requirements")

        # System requirements
        from system_check import check_requirements
        reqs = check_requirements()
        if not reqs["ready"]:
            tray.update_status(STATUS_ERROR, "Requirements not met")
            return

        if reqs["gpu_name"]:
            tray.set_gpu_info(f"{reqs['gpu_name']} ({reqs['gpu_vram_gb']}GB)")

        # Docker bootstrap
        tray.update_status(STATUS_SETUP, "Setting up Docker")
        from docker_setup import full_bootstrap, check_docker_running
        docker_result = full_bootstrap()

        if docker_result.get("needs_reboot"):
            tray.update_status(STATUS_ERROR, "Reboot required")
            return

        if not docker_result.get("ready"):
            tray.update_status(STATUS_ERROR, "Docker setup failed")
            return

        # Render image
        tray.update_status(STATUS_SETUP, "Checking render image")
        agent.ensure_docker_image()

        # Detect specs and register
        tray.update_status(STATUS_SETUP, "Registering")
        specs = agent.detect_specs()

        try:
            agent.machine_id = agent.register_machine(specs)
            agent.save_machine_id(agent.machine_id)
        except Exception as e:
            saved = agent.load_machine_id()
            if saved:
                agent.machine_id = saved
            else:
                tray.update_status(STATUS_ERROR, f"Registration failed: {e}")
                return

        agent.set_available(agent.machine_id)
        tray.update_status(STATUS_AVAILABLE)

        # Poll loop
        while agent.running:
            if agent.pause_event.is_set():
                time.sleep(agent.POLL_INTERVAL)
                continue

            try:
                if not check_docker_running():
                    tray.update_status(STATUS_ERROR, "Docker not running")
                    time.sleep(agent.POLL_INTERVAL * 2)
                    continue

                job = agent.poll_for_job(agent.machine_id)
                if job:
                    tray.update_status(STATUS_RENDERING, job["input_filename"])
                    agent.execute_job(job)
                    if agent.should_offer_capacity():
                        agent.set_available(agent.machine_id)
                        tray.update_status(STATUS_AVAILABLE)
                    elif agent.pause_event.is_set() and not agent.shutdown_event.is_set():
                        tray.update_status(STATUS_OFFLINE, "Paused")
                else:
                    time.sleep(agent.POLL_INTERVAL)

            except req_lib.exceptions.ConnectionError:
                tray.update_status(STATUS_ERROR, "Backend unreachable")
                time.sleep(agent.POLL_INTERVAL)
                tray.update_status(STATUS_AVAILABLE)
            except Exception:
                time.sleep(agent.POLL_INTERVAL)

        tray.update_status(STATUS_OFFLINE, "Stopped")
        tray.stop()

    # Start agent in background thread
    t = threading.Thread(target=agent_thread, daemon=True)
    t.start()

    # Run tray on main thread (required by pystray on Windows)
    tray.run()


if __name__ == "__main__":
    run_agent_with_tray()
