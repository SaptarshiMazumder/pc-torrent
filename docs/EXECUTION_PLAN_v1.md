# PC Rent v2 - Execution Plan: Dockerized GPU Rendering

## Problem Statement

Current system requires Blender to be pre-installed on the provider's PC, and .blend files sit exposed on the host filesystem. This creates two issues:
1. **Provider friction** - must install Blender manually
2. **Security risk** - provider can view/steal renter's proprietary .blend files

## Target Market

**PC cafes in Japan** (and similar venues). These run a mix of Windows 10 and Windows 11 machines with NVIDIA GPUs. The agent must work on both OS versions with full GPU rendering support. Studios submit heavy .blend files that require GPU (CUDA/OptiX) rendering.

## Solution

Containerize the entire render pipeline using Docker. The server provides a Docker image with Blender pre-installed. The agent (a Windows desktop app) auto-installs Docker on first run, then for each job, runs the render inside a container with **full GPU access**. The renter only uploads a .blend file - nothing else.

---

## Architecture Overview

```
RENTER (Browser)                 SERVER (FastAPI)                 PROVIDER (Windows PC)
      |                               |                               |
      |  1. Browse machines            |                               |
      |----GET /machines-------------->|                               |
      |<---machine list----------------|                               |
      |                               |                               |
      |  2. Upload .blend + pick PC    |                               |
      |----POST /jobs----------------->|                               |
      |<---job_id----------------------|                               |
      |                               |                               |
      |                               |  3. Agent polls                |
      |                               |<---GET /jobs/next-for-machine--|
      |                               |----job + input_url------------>|
      |                               |                               |
      |                               |           4. Agent pulls Docker image (if not cached)
      |                               |<---GET /docker/image-----------|
      |                               |----image tar/stream----------->|
      |                               |                               |
      |                               |           5. Agent downloads .blend INTO container
      |                               |           6. Container runs Blender with --gpus all
      |                               |           7. Container outputs rendered frames
      |                               |                               |
      |                               |           8. Agent uploads output
      |                               |<---POST /jobs/{id}/output------|
      |                               |----ok------------------------->|
      |                               |                               |
      |  9. Poll job status            |                               |
      |----GET /jobs/{id}------------->|                               |
      |<---status: done + files--------|                               |
      |                               |                               |
      | 10. Download output            |                               |
      |----GET /jobs/{id}/download---->|                               |
      |<---zip/file--------------------|                               |
```

---

## Windows 10 vs Windows 11 - GPU Support Strategy

Both Windows 10 and Windows 11 support GPU passthrough to Docker containers via WSL2, but with different levels of ease.

### Windows 11 (priority path)
- GPU passthrough in WSL2 works **out of the box**
- NVIDIA drivers on the host automatically expose GPU to WSL2
- `docker run --gpus all` just works
- No extra configuration needed

### Windows 10 (supported path)
- GPU passthrough in WSL2 is supported but requires:
  - Windows 10 version **21H2 or later** (build 19044+)
  - NVIDIA driver **510.06 or later** (R510+ branch)
  - WSL2 kernel version **5.10.43.3 or later** (via `wsl --update`)
- Once these requirements are met, `docker run --gpus all` works identically

### Agent Detection Flow
```
Agent starts
  |
  v
Detect Windows version (10 vs 11)
  |
  v
Detect NVIDIA driver version (nvidia-smi --query-gpu=driver_version)
  |
  v
┌─────────────────────────────────────────────────────────────┐
│ Windows 11 + any recent NVIDIA driver                       │
│   → GPU READY. Proceed normally.                            │
├─────────────────────────────────────────────────────────────┤
│ Windows 10 (21H2+) + NVIDIA driver 510+                    │
│   → GPU READY. Proceed normally.                            │
├─────────────────────────────────────────────────────────────┤
│ Windows 10 (21H2+) + NVIDIA driver < 510                   │
│   → Show message:                                           │
│     "Your NVIDIA driver needs updating for GPU rendering.   │
│      Please update to driver 510+ from nvidia.com/drivers   │
│      Current: {version}. Required: 510+"                    │
│   → Provide direct download link                            │
│   → Agent waits / retries after user updates                │
├─────────────────────────────────────────────────────────────┤
│ Windows 10 (pre-21H2)                                       │
│   → Show message:                                           │
│     "Windows 10 version 21H2 or later required.             │
│      Please run Windows Update."                            │
│   → Agent exits                                             │
├─────────────────────────────────────────────────────────────┤
│ No NVIDIA GPU detected                                      │
│   → Show message:                                           │
│     "NVIDIA GPU required for rendering.                     │
│      AMD GPU support coming soon."                          │
│   → Agent exits                                             │
└─────────────────────────────────────────────────────────────┘
```

### Minimum Requirements for Provider PC
- **OS:** Windows 10 21H2+ or Windows 11
- **GPU:** NVIDIA GPU (any CUDA-capable card)
- **Driver:** NVIDIA 510+ (Windows 10) or any recent driver (Windows 11)
- **RAM:** 8GB+ recommended
- **Disk:** ~2GB free (for Docker + render image)

---

## Component Breakdown

---

### PHASE 1: Docker Image (the "render container")

**What**: A Docker image hosted on the server that contains Blender + CUDA support for GPU rendering.

**Image contents:**
```dockerfile
# Dockerfile.render
FROM nvidia/cuda:12.2.0-runtime-ubuntu22.04

# Install Blender dependencies
RUN apt-get update && apt-get install -y \
    wget xz-utils libxi6 libxxf86vm1 libxfixes3 \
    libxrender1 libgl1-mesa-glx libxkbcommon0 \
    libsm6 libice6 \
    && rm -rf /var/lib/apt/lists/*

# Download Blender portable (headless)
RUN wget -q https://download.blender.org/release/Blender4.3/blender-4.3.0-linux-x64.tar.xz \
    && tar xf blender-4.3.0-linux-x64.tar.xz \
    && mv blender-4.3.0-linux-x64 /opt/blender \
    && rm blender-4.3.0-linux-x64.tar.xz

# Create working directories
RUN mkdir -p /input /output

# Entry script: renders the .blend file found in /input
COPY render.sh /render.sh
RUN chmod +x /render.sh

ENTRYPOINT ["/render.sh"]
```

**Key change from v1:** Base image is `nvidia/cuda:12.2.0-runtime-ubuntu22.04` instead of plain `ubuntu:22.04`. This includes CUDA runtime libraries so Blender can use GPU.

**render.sh:**
```bash
#!/bin/bash
set -e

BLEND_FILE=$(find /input -name "*.blend" -print -quit)

if [ -z "$BLEND_FILE" ]; then
    echo "ERROR: No .blend file found in /input"
    exit 1
fi

echo "=== PC Rent Render Container ==="
echo "Blend file: $BLEND_FILE"

# Detect available GPUs
if nvidia-smi > /dev/null 2>&1; then
    echo "GPU detected:"
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
    DEVICE_FLAG="-- --cycles-device OPTIX"
    echo "Rendering with: OPTIX (GPU)"
else
    DEVICE_FLAG=""
    echo "WARNING: No GPU detected, falling back to CPU rendering"
fi

echo "Starting render..."
/opt/blender/blender \
    -b "$BLEND_FILE" \
    -o /output/frame#### \
    -E CYCLES \
    -a \
    $DEVICE_FLAG

echo "Render complete. Output files:"
ls -la /output/
```

**Render behavior:**
- Blender uses CYCLES render engine (the standard for GPU rendering)
- `--cycles-device OPTIX` uses NVIDIA OptiX (fastest GPU path)
- Falls back to CPU if no GPU detected (shouldn't happen, but safety net)
- Uses **all available GPU memory and all CPU cores** automatically
- No resource limits imposed by Docker (full machine utilization)

**Image size:** ~1.5-2GB compressed (CUDA runtime adds ~800MB over plain Ubuntu)

**Where it lives:**
- Built once, exported as `pcrent-render.tar.gz`
- Stored on the server at `server/docker/pcrent-render.tar.gz`
- Served via `GET /docker/image` endpoint
- Agent downloads once, caches locally (never re-downloads unless version changes)

**Versioning:**
- Server stores a version hash/tag alongside the image
- Agent checks version before each job; re-downloads only if changed
- Endpoint: `GET /docker/image/version` returns `{ "version": "v1.0.0", "sha256": "abc123..." }`

---

### PHASE 2: Server Changes

**New endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/docker/image/version` | Returns current image version + hash |
| GET | `/docker/image` | Streams the Docker image tar file |

**New files on server:**
```
server/
  docker/
    Dockerfile.render       # Dockerfile to build the image
    render.sh               # Entry script copied into image
    pcrent-render.tar.gz    # Pre-built image (binary, gitignored)
    build.sh                # Script to build & export the image
```

**build.sh** (run once by us to create the image):
```bash
#!/bin/bash
docker build -t pcrent-render:latest -f Dockerfile.render .
docker save pcrent-render:latest | gzip > pcrent-render.tar.gz
sha256sum pcrent-render.tar.gz > pcrent-render.sha256
echo "Image built and exported."
echo "Size: $(du -h pcrent-render.tar.gz | cut -f1)"
```

**New endpoint implementation (main.py additions):**

```python
DOCKER_DIR = BASE_DIR / "docker"

@app.get("/docker/image/version")
def get_docker_image_version():
    sha_file = DOCKER_DIR / "pcrent-render.sha256"
    if not sha_file.exists():
        raise HTTPException(404, "No image available")
    sha = sha_file.read_text().strip().split()[0]
    return {"version": "v1.0.0", "sha256": sha}

@app.get("/docker/image")
def download_docker_image():
    image_path = DOCKER_DIR / "pcrent-render.tar.gz"
    if not image_path.exists():
        raise HTTPException(404, "Image not found")
    return FileResponse(path=image_path, filename="pcrent-render.tar.gz")
```

**Database changes:**

Add `os_version` and `nvidia_driver` to the machines table so the frontend can display compatibility info:

```sql
ALTER TABLE machines ADD COLUMN os_version TEXT;
ALTER TABLE machines ADD COLUMN nvidia_driver TEXT;
```

Agent sends these during registration. No other DB changes needed.

**No changes needed to existing job endpoints** - job submission, status, output upload all stay the same.

---

### PHASE 3: Agent Rewrite

The agent becomes a full Windows desktop application.

#### 3A. System Requirements Check (first-time)

Before anything else, agent checks if this PC can run GPU-accelerated Docker containers.

```
Agent starts
  |
  v
Check: Windows version
  → Get build number via platform.version() or winreg
  → Need: Win10 21H2 (19044+) or Win11
  |
  v
Check: NVIDIA GPU present?
  → Run: nvidia-smi
  → If not found: "NVIDIA GPU required" → exit
  |
  v
Check: NVIDIA driver version
  → Run: nvidia-smi --query-gpu=driver_version --format=csv,noheader
  → Win10: need 510+
  → Win11: any recent driver OK
  → If too old: show update instructions + link
  |
  v
All checks passed → proceed to Docker bootstrap
```

#### 3B. Docker Bootstrap (first-time setup)

```
Check: Is Docker installed?  (run: docker --version)
  |
  NO ──────────────────────────────────────────────┐
  |                                                |
  v                                                v
Check: Is WSL2 installed?                   Show: "Setting up Docker..."
  → Run: wsl --status                             |
  |                                                |
  NO ──> Install WSL2:                             |
  |      wsl --install --no-distribution           |
  |      (triggers UAC prompt)                     |
  |                                                |
  v                                                |
Reboot needed?                                     |
  → Check: wsl --status after install              |
  |                                                |
  YES ──> Save state to %APPDATA%\PCRent\          |
  |       Show: "Restart your PC to continue"      |
  |       Add agent to RunOnce registry key         |
  |       (auto-resumes after reboot)              |
  |       Exit.                                    |
  |                                                |
  NO ──> Continue                                  |
  |                                                |
  v                                                |
Download Docker Desktop installer                  |
  → URL: desktop.docker.com/win/main/amd64/...    |
  → Save to %TEMP%\DockerDesktopInstaller.exe      |
  |                                                |
  v                                                |
Run installer silently:                            |
  "Docker Desktop Installer.exe" install           |
     --quiet --accept-license                      |
  |                                                |
  v                                                |
Wait for Docker daemon to be ready:                |
  Loop: docker info (retry every 5s, up to 120s)   |
  |                                                |
  v                                                |
Configure Docker for GPU:                          |
  → Verify: docker run --rm --gpus all             |
       nvidia/cuda:12.2.0-base-ubuntu22.04         |
       nvidia-smi                                  |
  → This confirms GPU passthrough works            |
  |                                                |
  YES (Docker + GPU ready) <───────────────────────┘
  |
  v
Continue to registration
```

**Resume after reboot:**
- Agent writes to `HKCU\Software\Microsoft\Windows\CurrentVersion\RunOnce`
- On next login, agent resumes setup automatically
- Checks WSL2 is now active, continues with Docker install

#### 3C. Image Management

```
Check: Is pcrent-render image loaded locally?
  docker images pcrent-render --format "{{.ID}}"
  |
  v
Check version with server:
  GET /docker/image/version → { sha256: "..." }
  |
  v
Matches local cache? (stored in %APPDATA%\PCRent\image.sha256)
  |
  YES ──> Use cached image, skip download
  |
  NO ──> Download image:
         GET /docker/image → save to %TEMP%\pcrent-render.tar.gz
         docker load -i %TEMP%\pcrent-render.tar.gz
         Save sha256 to %APPDATA%\PCRent\image.sha256
         Delete %TEMP%\pcrent-render.tar.gz
```

**Cache location:** `%APPDATA%\PCRent\`
```
%APPDATA%\PCRent\
  image.sha256      # Hash of currently loaded Docker image
  machine_id        # Persisted machine UUID (survives restarts)
  config.json       # Agent configuration (backend URL, etc.)
  setup_state.json  # Tracks where we are in first-time setup (for reboot resume)
```

#### 3D. Job Execution (Docker + GPU)

**Flow when job arrives:**
```
1. Create temp directory for this job
   %TEMP%\pcrent_{job_id}\input\
   %TEMP%\pcrent_{job_id}\output\

2. Download .blend file into temp dir
   GET {input_url} → %TEMP%\pcrent_{job_id}\input\scene.blend

3. Update job status: "running"

4. Run Docker container with GPU:
   docker run --rm \
     --gpus all \
     --network none \
     --name pcrent-job-{job_id_short} \
     -v "%TEMP%\pcrent_{job_id}\input:/input:ro" \
     -v "%TEMP%\pcrent_{job_id}\output:/output" \
     pcrent-render:latest

5. Container executes:
   - Detects GPU via nvidia-smi (inside container)
   - Runs: blender -b /input/scene.blend -o /output/frame#### -E CYCLES -a -- --cycles-device OPTIX
   - Uses ALL GPU memory, ALL CPU cores, ALL RAM
   - Outputs frames to /output/

6. Container exits (exit code 0 = success)

7. Collect output files from %TEMP%\pcrent_{job_id}\output\

8. Upload output:
   POST /jobs/{job_id}/output (multipart file upload)

9. Update status:
   PUT /jobs/{job_id}/status → "done"

10. Cleanup:
    Delete %TEMP%\pcrent_{job_id}\
```

**Docker run command (full):**
```bash
docker run --rm \
  --gpus all \
  --network none \
  --name pcrent-job-{job_id_short} \
  -v "C:/Users/.../pcrent_{job_id}/input:/input:ro" \
  -v "C:/Users/.../pcrent_{job_id}/output:/output" \
  pcrent-render:latest
```

**Flags explained:**
- `--gpus all` → pass ALL host GPUs into the container
- `--network none` → container has NO internet access (security)
- `--rm` → auto-delete container after exit (cleanup)
- `-v .../input:/input:ro` → mount input as READ-ONLY (security)
- `-v .../output:/output` → mount output as writable
- No `--memory` or `--cpus` limits → uses everything the PC has

**Error handling:**
- Container exit code != 0 → job status = "failed", capture stderr
- Container timeout (configurable, default 4 hours) → `docker kill`, status = "failed"
- Docker daemon not running → retry, show tray notification

**Security notes:**
- `.blend` file is in a temp dir mounted read-only
- Container has NO network access (can't exfiltrate data)
- Container is auto-deleted after job
- Temp dir is deleted after upload
- Provider would need to actively `docker exec` into a running container to snoop - casual access prevented

#### 3E. Windows App Packaging

**Structure:**
```
agent/
  agent.py              # Core logic (rewritten for Docker + GPU)
  docker_setup.py       # Docker/WSL2/GPU detection and installation
  system_check.py       # Windows version, NVIDIA driver, requirements check
  tray.py               # System tray icon (using pystray)
  config.py             # Configuration management (%APPDATA%)
  requirements.txt      # Python dependencies
  icon.ico              # App icon
  build.spec            # PyInstaller spec file
  installer/
    setup.iss            # Inno Setup script for PCRentAgent-Setup.exe
```

**System tray app (using pystray):**
- Tray icon with status colors:
  - Gray: setting up / offline
  - Green: available, waiting for jobs
  - Yellow: downloading image / updating
  - Blue: rendering a job
  - Red: error (with tooltip showing what went wrong)
- Right-click menu:
  - "Status: Available" (or "Rendering Job #abc...")
  - "GPU: NVIDIA RTX 4060 (8GB)"
  - ---separator---
  - "Pause" / "Resume"
  - "View Logs"
  - "Exit"
- Notifications:
  - "Setup complete! Your PC is now available for rendering."
  - "Rendering job started..."
  - "Job complete! Rendered 120 frames."
  - "Error: ..." (if job fails)

**PyInstaller packaging:**
```bash
pyinstaller --onefile --windowed --icon=icon.ico --name=PCRentAgent agent.py
# Produces: dist/PCRentAgent.exe (~15-20MB)
```

**Inno Setup installer (setup.iss):**
- Requests admin privileges (for Docker/WSL2 install)
- Installs PCRentAgent.exe to `C:\Program Files\PCRent\`
- Creates Start Menu shortcut
- Adds to Windows Startup (`HKCU\...\Run` registry key)
- Shows setup progress
- Uninstaller included (removes agent, does NOT uninstall Docker)

---

### PHASE 4: Frontend Changes

**Minimal changes needed.** The frontend flow stays almost identical:

1. Browse machines → same
2. Click "Rent" → same
3. Upload .blend file → same (renter ONLY provides .blend)
4. Watch status → same
5. Download output → same

**Small additions:**
- Show GPU info more prominently (studios care about GPU model)
- Show OS version badge (Win10/Win11)
- Show "GPU Rendering" badge on each machine

**No structural frontend changes required.**

---

### PHASE 5: Registration Updates

Agent now sends additional info during registration:

```json
{
  "machine_key": "sha256...",
  "gpu_model": "NVIDIA GeForce RTX 4060",
  "gpu_vram_gb": 8.0,
  "cpu_cores": 12,
  "ram_gb": 32.0,
  "os_version": "Windows 11 23H2",
  "nvidia_driver": "560.94"
}
```

Server stores `os_version` and `nvidia_driver` in the machines table. Frontend can display this info to renters so they can pick the best machine.

---

## Implementation Order

### Step 1: Build the Docker Image
- [ ] Create `server/docker/Dockerfile.render` (based on nvidia/cuda image)
- [ ] Create `server/docker/render.sh` (GPU detection + Blender render)
- [ ] Create `server/docker/build.sh`
- [ ] Build image locally: `docker build -t pcrent-render:latest .`
- [ ] Test GPU rendering: `docker run --gpus all -v ./test:/input -v ./out:/output pcrent-render:latest`
- [ ] Verify: renders a sample .blend file using GPU (check Blender log for "OptiX" or "CUDA")
- [ ] Export: `docker save pcrent-render:latest | gzip > pcrent-render.tar.gz`

### Step 2: Server - Image Distribution Endpoints
- [ ] Add `GET /docker/image/version` endpoint to `main.py`
- [ ] Add `GET /docker/image` endpoint to `main.py` (streams tar.gz)
- [ ] Add `os_version` and `nvidia_driver` columns to machines table in `db.py`
- [ ] Update `/machines/register` to accept new fields
- [ ] Add `.gitignore` in `server/docker/` (ignore the tar.gz binary)
- [ ] Test: `curl http://localhost:8000/docker/image/version`
- [ ] Test: `curl -O http://localhost:8000/docker/image`

### Step 3: Agent - System Requirements Check
- [ ] Create `system_check.py` module
  - [ ] `get_windows_version()` → version string + build number
  - [ ] `get_nvidia_driver_version()` → driver version string
  - [ ] `check_gpu_present()` → bool (runs nvidia-smi)
  - [ ] `check_requirements()` → returns status object:
    - `gpu_ready: bool`
    - `os_ready: bool`
    - `driver_ready: bool`
    - `message: str` (human-readable explanation if not ready)
- [ ] Test on Windows 10 machine
- [ ] Test on Windows 11 machine

### Step 4: Agent - Docker Bootstrap
- [ ] Create `docker_setup.py` module
  - [ ] `check_docker_installed()` → bool
  - [ ] `check_wsl2_installed()` → bool
  - [ ] `install_wsl2()` → runs `wsl --install --no-distribution` (UAC)
  - [ ] `needs_reboot()` → bool (checks if WSL2 install needs restart)
  - [ ] `schedule_resume_after_reboot()` → writes RunOnce registry key
  - [ ] `install_docker_desktop()` → downloads + silent install
  - [ ] `wait_for_docker_ready(timeout=120)` → polls `docker info`
  - [ ] `verify_gpu_in_docker()` → runs nvidia-smi inside test container
  - [ ] `full_bootstrap()` → orchestrates the entire setup flow
- [ ] Test on clean Windows 10 machine (no Docker, no WSL2)
- [ ] Test on clean Windows 11 machine
- [ ] Test reboot-resume flow

### Step 5: Agent - Image Management
- [ ] `check_image_version()` → GET `/docker/image/version`, compare with local
- [ ] `download_and_load_image()` → GET `/docker/image`, `docker load`
- [ ] `ensure_image_ready()` → check + download if needed
- [ ] Cache at `%APPDATA%\PCRent\image.sha256`
- [ ] Test: image downloads, loads, is usable

### Step 6: Agent - Docker Job Execution
- [ ] Rewrite `execute_job()` in `agent.py` to use Docker
- [ ] Build `docker run --gpus all --network none ...` command
- [ ] Stream container stdout/stderr to agent logs
- [ ] Handle exit codes (0 = success, non-zero = failed)
- [ ] Handle timeout (configurable max render time, default 4 hours)
  - [ ] `docker kill` on timeout
- [ ] Collect output files from output volume mount
- [ ] Upload output + update status (existing logic, minimal changes)
- [ ] Cleanup temp dirs after each job
- [ ] Test: submit .blend via frontend → renders on GPU in container → download output

### Step 7: Agent - System Tray App
- [ ] Add `pystray` + `Pillow` to requirements
- [ ] Create `tray.py` with icon states:
  - Gray (setting up), Green (available), Yellow (downloading), Blue (rendering), Red (error)
- [ ] Right-click menu: status, GPU info, pause/resume, view logs, exit
- [ ] Windows toast notifications for key events
- [ ] Run agent polling loop in background thread, tray in main thread
- [ ] Test on Windows

### Step 8: Agent - Windows Packaging
- [ ] Create PyInstaller spec file (`build.spec`)
- [ ] Build `PCRentAgent.exe` (single file, windowed mode)
- [ ] Create Inno Setup script (`installer/setup.iss`)
  - [ ] UAC/admin elevation
  - [ ] Install to Program Files
  - [ ] Add to Windows Startup
  - [ ] Start Menu shortcuts
  - [ ] Uninstaller
- [ ] Build `PCRentAgent-Setup.exe`
- [ ] Test: run installer on fresh Windows 10
- [ ] Test: run installer on fresh Windows 11

### Step 9: Integration Testing
- [ ] Start server on a machine
- [ ] Install agent on a separate Windows PC (PC cafe scenario)
- [ ] Agent auto-installs WSL2 + Docker (with reboot if needed)
- [ ] Agent verifies GPU passthrough works
- [ ] Agent registers, appears as available in frontend
- [ ] Open frontend, browse machines, see GPU specs
- [ ] Click "Rent", upload .blend file
- [ ] Job runs inside Docker container with GPU
- [ ] Blender log shows "OptiX" or "CUDA" (confirm GPU rendering)
- [ ] Download rendered output, verify quality
- [ ] Submit second job to confirm re-use works
- [ ] Kill agent, verify machine goes idle
- [ ] Restart agent, verify it reconnects (same machine_id)

---

## File Changes Summary

### New files:
```
server/docker/
  Dockerfile.render          # Docker image (nvidia/cuda base + Blender)
  render.sh                  # Entrypoint script (GPU detection + render)
  build.sh                   # Build & export script
  .gitignore                 # Ignore pcrent-render.tar.gz

agent/
  docker_setup.py            # Docker/WSL2 installation + GPU verification
  system_check.py            # Windows version, NVIDIA driver checks
  tray.py                    # System tray app (pystray)
  config.py                  # Config management (%APPDATA%\PCRent\)
  icon.ico                   # Tray icon
  build.spec                 # PyInstaller config
  installer/
    setup.iss                # Inno Setup installer script
```

### Modified files:
```
server/main.py               # Add /docker/image endpoints
server/db.py                 # Add os_version, nvidia_driver columns
agent/agent.py               # Rewrite: Docker GPU execution, system checks
agent/requirements.txt       # Add: pystray, Pillow, docker
```

### Unchanged files:
```
frontend/src/api.js          # No API changes needed
frontend/src/App.jsx         # Minor: display GPU info more prominently (optional)
frontend/src/main.jsx        # No changes
```

---

## Risk & Considerations

### Docker Desktop Licensing
- Docker Desktop is **free for personal use** and companies with <250 employees and <$10M revenue
- PC cafes likely qualify as small businesses (free tier)
- If licensing is a concern later:
  - **Alternative:** Install Docker Engine directly inside WSL2 (fully free, no Docker Desktop)
  - Agent would: install WSL2 → install Ubuntu distro → install Docker Engine inside WSL2
  - Same `docker run --gpus all` works, no licensing issues
  - Slightly more complex setup, but avoids Docker Desktop entirely

### Image Size
- Docker image is ~1.5-2GB compressed (CUDA base + Blender)
- First download takes 5-15 minutes on typical connection
- Cached after first download - only re-downloaded when Blender version changes
- PC cafes likely have fast internet (Japan has excellent broadband)

### WSL2 Reboot (Windows 10)
- First-time WSL2 install on Windows 10 almost always needs a reboot
- Windows 11 usually does NOT need a reboot
- Agent handles this: saves state, registers RunOnce, resumes after reboot
- PC cafe scenario: can be done during off-hours / initial setup batch

### GPU Passthrough Compatibility
- **NVIDIA only** for now (CUDA/OptiX). AMD ROCm in Docker on Windows is not mature
- Most PC cafes and studios use NVIDIA GPUs, so this is acceptable
- Future: add AMD support when ROCm-on-WSL2 stabilizes

### Provider Trust / Security
- Docker with `--network none` prevents data exfiltration
- `.blend` file mounted read-only, deleted after job
- A determined provider could still:
  - `docker exec` into running container
  - Inspect Docker volumes
- For MVP: "good enough" (prevents casual snooping)
- Future improvements:
  - Encrypted volume mounts
  - Custom Docker runtime with restricted exec
  - Remote attestation

### PC Cafe Specific Considerations
- PCs may be rebooted frequently (customer turnover)
  - Agent auto-starts on boot, reconnects with same machine_id
- Multiple PCs in same cafe share same network
  - Each PC registers independently, all point to same backend URL
- Cafe owner needs to install agent on each PC once
  - Could create a batch setup script for deploying to all PCs
- PCs may have different GPU models within same cafe
  - Agent detects per-machine specs, renters can pick the best GPU

---

## Provider Experience (Final)

### First-time Setup (PC Cafe Owner)
```
1. Download PCRentAgent-Setup.exe from website
2. Run on each PC → approve UAC ("Yes")
3. Installer:
   a. Checks Windows version + NVIDIA driver  ✓
   b. Installs WSL2 (if needed)
   c. Prompts: "Restart PC to continue" (if needed, mainly Win10)
4. After restart, agent auto-resumes:
   a. Installs Docker Desktop (silent)
   b. Verifies GPU works in Docker
   c. Downloads render image (~2GB, one-time)
   d. Registers PC with backend
5. System tray icon turns green ✓
6. Done. PC now appears in marketplace.
```

### Ongoing (Zero Maintenance)
```
- Agent starts automatically on boot
- Polls for jobs silently
- Renders in background using Docker + GPU
- Tray icon shows status
- Auto-updates render image when server has new version
- Cafe owner never touches it again
```

## Renter Experience (Final)

```
1. Open website
2. Browse available machines:
   - "NVIDIA RTX 4060, 8GB VRAM, 12 cores, 32GB RAM, Win11"
   - "NVIDIA GTX 1660 Ti, 6GB VRAM, 8 cores, 16GB RAM, Win10"
3. Click "Rent" on best machine
4. Upload .blend file (that's ALL they provide)
5. Click "Start Render"
6. Watch status: pending → running → done
7. Click "Download Output" (rendered frames/video)
8. Done. Never installed anything. .blend file never exposed.
```
