# dev env composition.  TF manages GCP only: the Cloud Run service
# shell, the Artifact Registry repo, and the backup_monitor Cloud
# Run Job + Scheduler.  Neon DB, Upstash Redis, and Cloudflare R2
# are created manually in their respective dashboards; the URLs +
# keys are pasted into infra/envs/dev/.env.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }

  backend "gcs" {
    bucket = "pc-rent-tf-state"
    prefix = "dev"
  }
}

provider "google" {
  project = var.gcp_project_id
  region  = var.gcp_region
}

module "cloud_run" {
  source                      = "../../modules/cloud_run"
  env                         = var.env
  gcp_project_id              = var.gcp_project_id
  region                      = var.gcp_region
  service_name                = "pc-rent-server-v2-dev"
  artifact_registry_repo_name = "pc-rent-dev"
}

module "backup_monitor" {
  source                      = "../../modules/backup_monitor"
  env                         = var.env
  gcp_project_id              = var.gcp_project_id
  region                      = var.gcp_region
  job_name                    = "pc-rent-backup-monitor-dev"
  scheduler_name              = "pc-rent-backup-monitor-tick-dev"
  artifact_registry_repo_name = "pc-rent-backup-monitor-dev"
  orchestrator_url            = module.cloud_run.service_url
  database_url                = var.database_url
  redis_url                   = var.redis_url
  orphan_secret               = var.orphan_secret
  vast_api_key                = var.vast_api_key
}
