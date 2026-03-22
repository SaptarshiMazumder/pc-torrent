# PC Rent Python Server (FastAPI)

This folder contains a FastAPI implementation of the backend API.

## Run

```powershell
cd server
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

## Notes

- Keeps its own SQLite DB at `server/pcrent.db`
- Stores files under `server/jobs/{job_id}/...`
- Deduplicates machine registration using `machine_key` sent by agent
