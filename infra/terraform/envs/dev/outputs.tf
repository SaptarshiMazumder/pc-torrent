# Outputs consumed by tf-apply.sh to patch the env's .env, and by
# deploy-server.sh / deploy-backup-monitor.sh to know where to push
# images.

output "backend_url" {
  description = "Cloud Run public URL.  Goes into PUBLIC_BACKEND_URL."
  value       = module.cloud_run.service_url
}

output "service_name" {
  description = "Cloud Run service name for gcloud deploy."
  value       = module.cloud_run.service_name
}

output "artifact_registry_repo" {
  description = "Fully-qualified AR repo for serverV2 image pushes."
  value       = module.cloud_run.artifact_registry_repo
}

output "backup_monitor_job_name" {
  description = "Cloud Run Job name.  Goes into BACKUP_MONITOR_JOB_NAME."
  value       = module.backup_monitor.job_name
}

output "backup_monitor_artifact_registry_repo" {
  description = "AR repo for backup_monitor image pushes."
  value       = module.backup_monitor.artifact_registry_repo
}
