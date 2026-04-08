# PC Rent

Peer-to-peer distributed rendering network. Providers share idle PCs; renters submit Blender jobs.

## Structure

```text
pc-rent/
|-- server/      Python + FastAPI + SQLite API
|-- frontend/    React web app (Vite)
`-- agent/       Python desktop agent (runs on provider's PC)
```

## Quick Start (Python Server)

### 1. Start Python server

```bash
cd server
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
# Runs on http://localhost:8000
```

### 2. Start frontend

```bash
cd frontend
npm install
npm run dev
# Runs on http://localhost:5173
```

Frontend defaults to `http://localhost:8000`.

### 3. Start desktop agent (provider PC)

```bash
cd agent
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

# point agent to Python server
set BACKEND_URL=http://localhost:8000

# optional: test mode without Blender
set MOCK_MODE=true

python agent.py
```

## Agent env vars

| Variable       | Default                                                       | Description                                    |
| -------------- | ------------------------------------------------------------- | ---------------------------------------------- |
| `BACKEND_URL`  | `http://localhost:8000`                                       | API server URL                                 |
| `BLENDER_PATH` | `C:\Program Files\Blender Foundation\Blender 4.5\blender.exe` | Blender executable path                        |
| `USE_SANDBOX`  | `false`                                                       | Set `true` to use Windows Sandbox              |
| `MOCK_MODE`    | `false`                                                       | Set `true` to simulate renders without Blender |

## RunPod Worker (GHCR)

The serverless worker image lives at `ghcr.io/saptarshimazumder/pcrent-worker`.

### First-time setup

```bash
# Authenticate with GHCR using a GitHub PAT (write:packages scope)
echo YOUR_GITHUB_PAT | docker login ghcr.io -u SaptarshiMazumder --password-stdin
```

After pushing, go to `github.com/SaptarshiMazumder` → **Packages** → `pcrent-worker` → **Package settings** → **Change visibility** → **Public** (one-time only).

### Build and push a new version

```bash
docker build -t ghcr.io/saptarshimazumder/pcrent-worker:2.0x -f runpod_worker/Dockerfile .
docker push ghcr.io/saptarshimazumder/pcrent-worker:2.0x
```

Then update the container image tag in your RunPod endpoint settings to match.

## API Endpoints

| Method | Path                          | Description                                                  |
| ------ | ----------------------------- | ------------------------------------------------------------ |
| POST   | `/machines/register`          | Agent registers machine                                      |
| PUT    | `/machines/{id}/available`    | Mark machine as ready                                        |
| PUT    | `/machines/{id}/idle`         | Mark machine as offline                                      |
| GET    | `/machines`                   | List available machines                                      |
| POST   | `/jobs`                       | Submit render job (multipart: `machine_id` + `blender_file`) |
| GET    | `/jobs`                       | List **legacy single-machine jobs only** (`group_id` is null) |
| GET    | `/render-groups`              | List distributed render groups and aggregated task progress  |
| GET    | `/jobs/{id}`                  | Get job status                                               |
| GET    | `/jobs/{id}/download`         | Download rendered output                                     |
| GET    | `/jobs/next-for-machine/{id}` | Agent polls for next job                                     |
| PUT    | `/jobs/{id}/status`           | Agent updates job status                                     |
| POST   | `/jobs/{id}/output`           | Agent uploads output files                                   |
