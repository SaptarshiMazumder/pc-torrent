variable "env" {
  description = "Environment name (dev / staging / prod).  Used in resource names + labels."
  type        = string
}

variable "gcp_project_id" {
  description = "GCP project ID hosting the Cloud Run Job + Scheduler."
  type        = string
}

variable "region" {
  description = "GCP region for the Job + Scheduler.  Match the env's serverV2 region."
  type        = string
}

variable "job_name" {
  description = "Cloud Run Job name (e.g. pc-rent-backup-monitor-dev)."
  type        = string
}

variable "scheduler_name" {
  description = "Cloud Scheduler job name (e.g. pc-rent-backup-monitor-tick-dev)."
  type        = string
}

variable "artifact_registry_repo_name" {
  description = "Artifact Registry Docker repo name dedicated to backup_monitor (e.g. pc-rent-backup-monitor-dev)."
  type        = string
}

# ----- Wiring from the env's other modules ----------------------
# These flow in from per-env main.tf via module outputs / variables,
# so the Job's env vars can never drift from what serverV2 sees.

variable "orchestrator_url" {
  description = "serverV2 Cloud Run URL for THIS env.  Reports go here.  Wired from module.cloud_run.service_url -- never hardcoded."
  type        = string
}

variable "database_url" {
  description = "Same Postgres URL serverV2 uses.  Marked sensitive so it stays out of plan output."
  type        = string
  sensitive   = true
}

variable "redis_url" {
  description = "Same Upstash URL serverV2 uses."
  type        = string
  sensitive   = true
}

variable "orphan_secret" {
  description = "Shared secret for /internal/* auth.  Must match serverV2's ORPHAN_SECRET."
  type        = string
  sensitive   = true
}

variable "vast_api_key" {
  description = "Per-env Vast API key (separate Vast accounts per env -- avoids the cross-env destroy hazard)."
  type        = string
  sensitive   = true
}

# ----- Tuning knobs (defaults match existing behavior) -----------

variable "schedule" {
  description = "Cron schedule for tick.  Default every 60s, matching the existing test-env deploy."
  type        = string
  default     = "* * * * *"
}

variable "task_timeout" {
  description = "Per-tick timeout.  Must comfortably exceed the slowest scan (Vast list_instances dominates)."
  type        = string
  default     = "60s"
}

variable "memory" {
  description = "Per-tick memory."
  type        = string
  default     = "512Mi"
}

variable "cpu" {
  description = "Per-tick vCPU."
  type        = string
  default     = "1"
}

variable "max_retries" {
  description = "Cloud Run Job retry count.  1 = no retry on tick failure (next minute's tick recovers anyway)."
  type        = number
  default     = 1
}

variable "http_timeout_sec" {
  description = "HTTP timeout for reports to the orchestrator + Vast list calls."
  type        = number
  default     = 10
}

variable "vast_ghost_min_age_sec" {
  description = "Grace window before an unmatched Vast instance is treated as a ghost.  Must exceed dispatch->vast_job_id-write latency."
  type        = number
  default     = 120
}

variable "modal_terminal_window_hours" {
  description = "Lookback for the modal terminal-cleanup scan."
  type        = number
  default     = 24
}

variable "modal_terminal_max_rows" {
  description = "Cap on rows per modal terminal-cleanup tick."
  type        = number
  default     = 200
}
