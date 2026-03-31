import json
import os
import shutil
import subprocess
import tempfile
import zipfile
import glob
from pathlib import Path

from blend_parser import BlendParseError, parse_upload_from_path

ANALYZE_PREFIX = "PCR_ANALYZE_JSON "


def _success(frame_start, frame_end, frame_step, method, total_frames=None, blender_path=None):
    if total_frames is None:
        total_frames = ((frame_end - frame_start) // frame_step) + 1 if frame_end >= frame_start else 0
    result = {
        "ok": True,
        "frame_start": int(frame_start),
        "frame_end": int(frame_end),
        "frame_step": max(1, int(frame_step)),
        "total_frames": int(total_frames),
        "method": method,
        "error": None,
    }
    if blender_path:
        result["blender_path"] = blender_path
    return result


def _failure(message, parser_error=None, blender_error=None):
    result = {
        "ok": False,
        "frame_start": None,
        "frame_end": None,
        "frame_step": None,
        "total_frames": None,
        "method": "manual_required",
        "error": message,
    }
    if parser_error:
        result["parser_error"] = parser_error
    if blender_error:
        result["blender_error"] = blender_error
    return result


def _find_blender_executable():
    checked = []

    for cmd_name in ("blender", "blender.exe"):
        candidate = shutil.which(cmd_name)
        if candidate:
            checked.append(candidate)

    roots = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LOCALAPPDATA"),
    ]
    patterns = [
        os.path.join("Blender Foundation", "Blender*", "blender.exe"),
        os.path.join("Programs", "Blender Foundation", "Blender*", "blender.exe"),
    ]

    for root in roots:
        if not root:
            continue
        for pattern in patterns:
            glob_pattern = os.path.join(root, pattern)
            for path in sorted(glob.glob(glob_pattern), reverse=True):
                checked.append(path)

    seen = set()
    for candidate in checked:
        if not candidate:
            continue
        normalized = os.path.normcase(os.path.normpath(candidate))
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.isfile(candidate):
            return candidate

    return None


def _pick_blend_from_zip(zf):
    blend_names = [
        name for name in zf.namelist()
        if name.lower().endswith(".blend")
        and not name.startswith("__MACOSX/")
        and "/._" not in name
        and not name.startswith("._")
    ]
    if not blend_names:
        raise BlendParseError("No .blend file found inside the zip archive")
    blend_names.sort(key=lambda n: n.count("/"))
    return blend_names[0]


def _resolve_blend_for_blender(file_path):
    ext = file_path.suffix.lower()
    if ext == ".blend":
        return str(file_path), None

    if ext != ".zip":
        raise BlendParseError(f"Unsupported file extension: {ext}")

    tmp_dir = tempfile.mkdtemp(prefix="pcrent-analyze-")
    try:
        with zipfile.ZipFile(file_path) as zf:
            blend_member = _pick_blend_from_zip(zf)
            blend_data = zf.read(blend_member)
            if len(blend_data) < 12:
                raise BlendParseError(f"Extracted .blend '{blend_member}' is too small")
            blend_name = Path(blend_member).name or "project.blend"
            blend_path = Path(tmp_dir) / blend_name
            blend_path.write_bytes(blend_data)
        return str(blend_path), tmp_dir
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _blender_probe_script_contents():
    return """import json\nimport bpy\nimport sys\nscene = bpy.context.scene\nstart = int(scene.frame_start)\nend = int(scene.frame_end)\nstep = max(1, int(getattr(scene, 'frame_step', 1)))\ntotal = ((end - start) // step) + 1 if end >= start else 0\npayload = {'frame_start': start, 'frame_end': end, 'frame_step': step, 'total_frames': total}\nsys.stdout.write('PCR_ANALYZE_JSON ' + json.dumps(payload, sort_keys=True) + '\\n')\nsys.stdout.flush()\n"""


def _analyze_with_blender(file_path):
    blender_path = _find_blender_executable()
    if not blender_path:
        return _failure("Blender executable not found on this machine")

    blend_path = None
    cleanup_dir = None
    script_path = None

    try:
        blend_path, cleanup_dir = _resolve_blend_for_blender(file_path)

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as script_file:
            script_file.write(_blender_probe_script_contents())
            script_path = script_file.name

        cmd = [blender_path, "-b", blend_path, "-P", script_path]
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=180,
        )

        payload = None
        for line in completed.stdout.splitlines():
            if line.startswith(ANALYZE_PREFIX):
                payload = line[len(ANALYZE_PREFIX):].strip()

        if payload:
            try:
                parsed = json.loads(payload)
                frame_start = int(parsed.get("frame_start"))
                frame_end = int(parsed.get("frame_end"))
                frame_step = max(1, int(parsed.get("frame_step") or 1))
                total_frames = int(parsed.get("total_frames") or 0)
                return _success(
                    frame_start=frame_start,
                    frame_end=frame_end,
                    frame_step=frame_step,
                    total_frames=total_frames,
                    method="blender_cli",
                    blender_path=blender_path,
                )
            except Exception as exc:
                return _failure(f"Blender output parse failed: {exc}")

        stderr = (completed.stderr or "").strip()
        stdout_tail = "\n".join((completed.stdout or "").splitlines()[-8:]).strip()
        if completed.returncode != 0:
            detail = stderr or stdout_tail or f"exit code {completed.returncode}"
            return _failure(f"Blender probe failed: {detail}")

        detail = stderr or stdout_tail or "Blender did not return frame metadata"
        return _failure(f"Blender probe did not produce frame data: {detail}")

    except subprocess.TimeoutExpired:
        return _failure("Blender probe timed out")
    except Exception as exc:
        return _failure(f"Blender probe failed: {type(exc).__name__}: {exc}")
    finally:
        if script_path:
            try:
                os.unlink(script_path)
            except OSError:
                pass
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


def analyze_project_file(file_path):
    input_path = Path(file_path)
    if not input_path.exists() or not input_path.is_file():
        return _failure(f"File not found: {file_path}")

    parser_error = None
    try:
        info = parse_upload_from_path(str(input_path), input_path.name)
        return _success(
            frame_start=info["frame_start"],
            frame_end=info["frame_end"],
            frame_step=info.get("frame_step") or 1,
            total_frames=info.get("total_frames"),
            method="embedded_parser",
        )
    except Exception as exc:
        parser_error = f"{type(exc).__name__}: {exc}"

    blender_result = _analyze_with_blender(input_path)
    if blender_result.get("ok"):
        blender_result["parser_error"] = parser_error
        return blender_result

    return _failure(
        message="Local analysis failed. Enter frame range manually.",
        parser_error=parser_error,
        blender_error=blender_result.get("error"),
    )
