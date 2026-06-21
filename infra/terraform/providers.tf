# Shared provider declarations.
#
# TF manages GCP resources only (Cloud Run service, Artifact Registry,
# Cloud Run Job + Scheduler for backup_monitor).  Neon, Upstash, and
# Cloudflare R2 are created manually in their dashboards; the
# connection strings + bucket name are pasted into each env's .env.
#
# Auth: ``gcloud auth application-default login`` once, then TF reads
# Application Default Credentials.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}
