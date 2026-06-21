output "backend_url"                          { value = module.cloud_run.service_url }
output "service_name"                         { value = module.cloud_run.service_name }
output "artifact_registry_repo"               { value = module.cloud_run.artifact_registry_repo }
output "backup_monitor_job_name"              { value = module.backup_monitor.job_name }
output "backup_monitor_artifact_registry_repo" { value = module.backup_monitor.artifact_registry_repo }
