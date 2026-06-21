# prod env composition.  See ../dev/main.tf for the design rationale.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }

  backend "gcs" {
    bucket = "pc-rent-tf-state"
    prefix = "prod"
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
  service_name                = "pc-rent-server-v2-prod"
  artifact_registry_repo_name = "pc-rent-prod"
}

module "backup_monitor" {
  source                      = "../../modules/backup_monitor"
  env                         = var.env
  gcp_project_id              = var.gcp_project_id
  region                      = var.gcp_region
  job_name                    = "pc-rent-backup-monitor-prod"
  scheduler_name              = "pc-rent-backup-monitor-tick-prod"
  artifact_registry_repo_name = "pc-rent-backup-monitor-prod"
  orchestrator_url            = module.cloud_run.service_url
  database_url                = var.database_url
  redis_url                   = var.redis_url
  orphan_secret               = var.orphan_secret
  vast_api_key                = var.vast_api_key
}
