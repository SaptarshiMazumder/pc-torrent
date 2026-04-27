# backup_monitor

Cloud Run **Job** (not service) that catches orphaned jobs the in-process
monitors miss.  Triggered every minute by Cloud Scheduler — runs one scan
tick and exits.  Cost: ~$3-5/month vs ~$65/month for an always-on service.

## When this fires

A job is "orphaned" when:
- DB says `status = 'running'` (serverless fleet)
- Redis heartbeat key for that job has expired (worker is dead)
- AND nothing inside the main serverV2 service is doing anything about it
  (the per-job monitor thread is gone — Cloud Run restart, leader-recovery
  hasn't kicked in yet, etc.)

Each tick finds these and POSTs to the main orchestrator's
`/internal/orphan/{job_id}` endpoint, which fires the existing retry path.

## What it is NOT

- Not a per-job monitor — it does no Vast/Modal API polling.
- Not a state authority — never writes to Postgres.
- Not a replacement for in-process monitors — they handle the fast 15s
  detection path during normal operation.  This is the safety net.

## Deploy

From inside this directory:

```bash
./deploy.sh
```

The script:
1. Reads `serverV2/.env` for `DATABASE_URL`, `REDIS_URL`, `ORPHAN_SECRET`
   (the last must match the value set in serverV2's environment, otherwise
   the main service rejects orphan reports).
2. Deploys the Cloud Run Job (`pcrent-backup-monitor`).
3. Grants `roles/run.invoker` to the Compute service account.
4. Creates/updates the Cloud Scheduler trigger
   (`pcrent-backup-monitor-tick`) with cron `* * * * *`.

Cloud Run Job config:
- `memory = 512Mi`, `cpu = 1`
- `task-timeout = 60s` (one tick should complete in seconds)
- `max-retries = 1` (Scheduler will fire again next minute regardless)

Override defaults via env vars before invoking the script:
- `REGION`, `JOB_NAME`, `SCHEDULER_NAME`, `SCHEDULE`, `ORCHESTRATOR_URL`,
  `SCHEDULER_SA`.

## Verify

Trigger a one-off run to confirm wiring:

```bash
gcloud run jobs execute pcrent-backup-monitor --region asia-northeast1
```

List recent executions and tail logs:

```bash
gcloud run jobs executions list --job pcrent-backup-monitor --region asia-northeast1
gcloud run jobs executions logs <EXECUTION_NAME> --region asia-northeast1
```

You should see `Scan: N running serverless job(s)` per execution.  When an
orphan is detected:

```
Orphan detected: job 12ab34... (fleet=vast_serverless, group=...)
Reported orphan 12ab34... — orchestrator accepted
```
