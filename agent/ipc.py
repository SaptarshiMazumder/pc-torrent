"""
PC Rent Agent - IPC Layer
Structured JSON communication over stdin/stdout for Tauri sidecar mode.
"""

import json
import sys
import threading
import os


# When in sidecar mode, we hijack stdout for JSON events only.
# The original stdout is saved so we can write JSON to it.
# All other output (print from libraries, warnings, etc.) goes to stderr or a log file.
_original_stdout = None
_emit_lock = threading.Lock()
_sidecar_mode = False


def init_sidecar_mode():
    """
    Switch to sidecar mode: redirect stdout so only emit() writes to it.
    All regular print() calls go to stderr instead.
    """
    global _original_stdout, _sidecar_mode
    _original_stdout = sys.stdout
    # Redirect print() to stderr so library output doesn't corrupt JSON
    sys.stdout = sys.stderr
    _sidecar_mode = True


def emit(event, **data):
    """
    Emit a JSON event to the Tauri host (via stdout).
    Thread-safe. Each call writes exactly one line of JSON.
    """
    data["event"] = event
    line = json.dumps(data, default=str)

    with _emit_lock:
        out = _original_stdout if _sidecar_mode else sys.__stdout__
        try:
            out.write(line + "\n")
            out.flush()
        except (BrokenPipeError, OSError):
            pass


def emit_status(state, message="", **extra):
    """Emit a status change event."""
    emit("status", state=state, message=message, **extra)


def emit_log(message, source="agent", level="info"):
    """Emit a log line event."""
    emit("log", source=source, level=level, message=message)


def emit_system_info(info):
    """Emit system info (GPU, CPU, RAM, OS, driver)."""
    emit("system_info", **info)


def emit_runtime_info(info):
    """Emit runtime state (requirements, Docker, image cache, progress)."""
    emit("runtime_info", **info)


def emit_job_progress(**progress):
    """Emit per-job render progress updates."""
    emit("job_progress", **progress)


def emit_error(message):
    """Emit an error event."""
    emit("error", message=message)


def emit_job_complete(job_id, status, files=None, error=None):
    """Emit job completion event."""
    emit("job_complete", job_id=job_id, status=status,
         files=files or [], error=error)


def listen_commands(callback):
    """
    Listen for JSON commands on stdin. Blocks until stdin is closed.
    Calls callback(cmd_dict) for each received command.
    """
    # In sidecar mode, stdin is the real stdin (Tauri writes to it)
    input_stream = sys.__stdin__
    try:
        for line in input_stream:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
                if isinstance(cmd, dict) and "cmd" in cmd:
                    callback(cmd)
            except json.JSONDecodeError:
                pass
    except (EOFError, OSError):
        pass
