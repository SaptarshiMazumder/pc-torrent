# PC Rent - MVP Requirements

## Overview
Peer-to-peer PC rental marketplace for distributed compute jobs. Providers list idle PCs; renters submit Blender render jobs to available machines.

---

## Core Actors

**Provider**: Owner of a PC who rents it out
**Renter**: User who submits jobs to render
**Backend**: Central service managing listings, jobs, file storage
**Desktop Agent**: Lightweight app running on provider's PC

---

## Functional Requirements

### Provider Flow
- [ ] Download and run desktop agent (Windows)
- [ ] Agent auto-detects: GPU model, GPU VRAM, CPU cores, RAM
- [ ] Agent registers machine with backend (POST /machines/register)
- [ ] Agent marks machine as "available" (PUT /machines/{id}/available)
- [ ] Agent polls backend for incoming jobs (GET /jobs/next-for-machine/{id})
- [ ] Agent receives job details (blender file URL, output specs)
- [ ] Agent downloads blender file from backend
- [ ] Agent spawns Windows Sandbox with mounted input/output folders
- [ ] Agent executes blender command inside sandbox
- [ ] Agent uploads rendered output back to backend
- [ ] Agent marks job as complete
- [ ] Agent returns to idle/polling state

### Renter Flow
- [ ] Browse available machines on web UI (GET /machines)
  - Display: GPU model, GPU VRAM, CPU cores, RAM
- [ ] Upload .blend file via web form
- [ ] Select a machine from dropdown
- [ ] Submit job (POST /jobs)
  - Job enters "pending" state
- [ ] View job status on web (GET /jobs/{id})
  - Display: status (pending/running/done/failed)
- [ ] Download rendered output when complete (GET /jobs/{id}/download)

### Backend API
- `POST /machines/register` - Provider agent registers PC
- `PUT /machines/{id}/available` - Mark as ready for jobs
- `GET /machines` - List all available machines
- `POST /jobs` - Renter submits job (file upload + machine selection)
- `GET /jobs/{id}` - Check job status
- `GET /jobs/next-for-machine/{id}` - Poll for next job (agent endpoint)
- `GET /jobs/{id}/download` - Download job output
- `PUT /jobs/{id}/status` - Agent updates job status

### File Storage
- [ ] Store uploaded .blend files on backend disk at `/jobs/{job_id}/input/`
- [ ] Store rendered output on backend disk at `/jobs/{job_id}/output/`
- [ ] Cleanup old jobs (>48 hours) to free space

### Windows Sandbox Integration
- [ ] Generate sandbox config XML with mounted folders
- [ ] Mount `/jobs/{job_id}/input` as `C:\input` (read-only)
- [ ] Mount `/jobs/{job_id}/output` as `C:\output` (read-write)
- [ ] Execute blender command: `blender -b C:\input\scene.blend -o C:\output\frame###.png`
- [ ] Wait for process completion
- [ ] Capture return code and errors

---

## Non-Functional Requirements (MVP)

- No authentication/login required (skip security)
- No payment processing (skip billing)
- No ratings/reviews (skip reputation)
- No user accounts (skip persistence)
- Single-threaded job execution per machine (agents process one job at a time)
- No SLAs or reliability guarantees

---

## Scope - NOT Included in MVP

- Security (sandboxing, user isolation, file permissions)
- Payment/billing
- User accounts or authentication
- Rating/review system
- GPU passthrough (Windows Sandbox only)
- Multi-machine job batching/splitting
- Real-time progress updates
- Job cancellation
- Mac/Linux support (Windows only)
- GPU/CPU pricing variation (flat rate assumed)

---

## Data Models

### Machine
```json
{
  "id": "uuid",
  "gpu_model": "RTX 4090",
  "gpu_vram_gb": 24,
  "cpu_cores": 16,
  "ram_gb": 64,
  "status": "available|idle|processing",
  "registered_at": "2026-03-04T12:00:00Z"
}
```

### Job
```json
{
  "id": "uuid",
  "machine_id": "uuid",
  "input_file": "scene.blend",
  "status": "pending|running|done|failed",
  "output_files": ["frame0001.png", "frame0002.png"],
  "submitted_at": "2026-03-04T12:00:00Z",
  "completed_at": "2026-03-04T13:00:00Z"
}
```

---

## Tech Stack

- **Backend**: Node.js + Express
- **Database**: SQLite
- **Frontend**: React (or basic HTML forms)
- **Desktop Agent**: Python
- **File Storage**: Local disk (`/jobs/{id}/`)
- **Sandboxing**: Windows Sandbox (built-in)

---

## Success Criteria

1. Provider can register PC and see it in available machines list
2. Renter can upload .blend, select a machine, submit job
3. Job runs in Windows Sandbox and produces output
4. Renter can download rendered files
5. Agent returns to idle state after job completes
