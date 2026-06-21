# Per-env inputs.  Values come from infra/envs/dev/.env via
# TF_VAR_* env-var injection by tf-apply.sh.

variable "env" {
  type    = string
  default = "dev"
}

variable "gcp_project_id" {
  description = "GCP project ID hosting Cloud Run + Artifact Registry."
  type        = string
}

variable "gcp_region" {
  description = "GCP region for Cloud Run + Artifact Registry."
  type        = string
  default     = "asia-northeast1"
}

# ----- backup_monitor wiring (sourced from infra/envs/dev/.env) -----

variable "orphan_secret" {
  description = "Shared secret for /internal/* auth.  Must match serverV2's ORPHAN_SECRET."
  type        = string
  sensitive   = true
}

variable "vast_api_key" {
  description = "Per-env Vast API key (separate Vast account per env)."
  type        = string
  sensitive   = true
}

variable "database_url" {
  description = "Postgres connection string the backup_monitor uses.  Pasted from Neon dashboard into .env."
  type        = string
  sensitive   = true
}

variable "redis_url" {
  description = "Redis connection string the backup_monitor uses.  Pasted from Upstash dashboard into .env."
  type        = string
  sensitive   = true
}
