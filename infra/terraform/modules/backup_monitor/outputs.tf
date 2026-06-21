output "job_name" {
  description = "Cloud Run Job name -- deploy-backup-monitor.sh uses this for `gcloud run jobs update`."
  value       = google_cloud_run_v2_job.main.name
}

output "scheduler_name" {
  description = "Cloud Scheduler job name -- useful for `gcloud scheduler jobs pause/resume` during incidents."
  value       = google_cloud_scheduler_job.tick.name
}

output "artifact_registry_repo" {
  description = "Fully-qualified Artifact Registry path for backup_monitor image pushes."
  value       = "${var.region}-docker.pkg.dev/${var.gcp_project_id}/${google_artifact_registry_repository.backup_monitor.repository_id}"
}

output "scheduler_service_account" {
  description = "Email of the dedicated scheduler SA (visible in IAM)."
  value       = google_service_account.scheduler.email
}
