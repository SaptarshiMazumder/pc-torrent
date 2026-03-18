# PC Rent

Peer-to-peer distributed rendering marketplace. Providers share idle PCs; renters submit Blender jobs.

## Structure

```
pc-rent/
├── backend/     Node.js + Express + SQLite API
├── frontend/    React web app (Vite)
└── agent/       Python desktop agent (runs on provider's PC)
```

## Quick Start

### 1. Backend

```bash
cd backend
npm install
npm start
# Runs on http://localhost:3001
```

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
# Runs on http://localhost:5173
```

### 3. Desktop Agent (Provider's PC)

```bash
cd agent

# Create and activate virtual environment
python -m venv venv
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set your Blender path (default: C:\Program Files\Blender Foundation\Blender 4.3\blender.exe)
set BLENDER_PATH=C:\Path\To\blender.exe

# Point to your backend
set BACKEND_URL=http://localhost:3001

python agent.py
```

To deactivate the venv when done: `deactivate`

#### Agent env vars

| Variable       | Default                                                               | Description                     |
|----------------|-----------------------------------------------------------------------|---------------------------------|
| `BACKEND_URL`  | `http://localhost:3001`                                               | Backend server URL              |
| `BLENDER_PATH` | `C:\Program Files\Blender Foundation\Blender 4.3\blender.exe`        | Path to Blender executable      |
| `USE_SANDBOX`  | `false`                                                               | Set `true` to use Windows Sandbox (Windows 10/11 Pro only) |
| `MOCK_MODE`    | `false`                                                               | Set `true` to simulate renders without Blender (testing only) |

#### Testing without Blender

To test the entire flow without Blender installed:

```bash
cd agent
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
set MOCK_MODE=true
python agent.py
```

This will create dummy output files instead of actually rendering, letting you test the upload/download flow.

## Flow

```
Provider:
  1. Run agent → detects GPU/CPU/RAM → registers with backend → polls for jobs

Renter:
  1. Visit web app → browse available machines
  2. Upload .blend file → select machine → submit
  3. Watch job status → download rendered output when done
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/machines/register` | Agent registers machine |
| PUT | `/machines/:id/available` | Mark machine as ready |
| PUT | `/machines/:id/idle` | Mark machine as offline |
| GET | `/machines` | List available machines |
| POST | `/jobs` | Submit render job (multipart: machine_id + blender_file) |
| GET | `/jobs/:id` | Get job status |
| GET | `/jobs/:id/download` | Download rendered output |
| GET | `/jobs/next-for-machine/:id` | Agent polls for next job |
| PUT | `/jobs/:id/status` | Agent updates job status |
| POST | `/jobs/:id/output` | Agent uploads output files |

## Notes

- No auth, no payments — MVP only
- Blender must be installed on the provider's machine
- Output files are stored on the backend server disk under `backend/jobs/{job_id}/output/`
- Files are not auto-deleted yet — clean manually as needed
