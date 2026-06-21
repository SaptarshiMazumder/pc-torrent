output "service_url" {
  description = "Public Cloud Run URL.  Consumed by deploy.sh as PUBLIC_BACKEND_URL + injected into the .env so workers know where to call back."
  value       = google_cloud_run_v2_service.main.uri
}

output "service_name" {
  description = "Echo of the Cloud Run service name (for deploy.sh's gcloud calls)."
  value       = google_cloud_run_v2_service.main.name
}

output "artifact_registry_repo" {
  description = "Fully-qualified Artifact Registry path for image pushes."
  value       = "${var.region}-docker.pkg.dev/${var.gcp_project_id}/${google_artifact_registry_repository.main.repository_id}"
}
