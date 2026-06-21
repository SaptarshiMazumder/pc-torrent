# Cloud Run shell for serverV2 + the Artifact Registry repo that
# hosts its container images.  TF owns the SHELL only -- the image
# itself is built and pushed by ``deploy.sh ENV`` (Phase 2), which
# then runs ``gcloud run deploy --image=<new>`` to swap the running
# container.  Image env vars (DATABASE_URL etc.) are also set at
# ``gcloud run deploy`` time from the env's .env file, NOT in TF --
# keeps secrets out of state.

resource "google_artifact_registry_repository" "main" {
  provider      = google
  project       = var.gcp_project_id
  location      = var.region
  repository_id = var.artifact_registry_repo_name
  description   = "Forge serverV2 container images for env=${var.env}"
  format        = "DOCKER"

  labels = {
    env     = var.env
    service = "pc-rent-server-v2"
    owner   = "forge"
  }
}

resource "google_cloud_run_v2_service" "main" {
  provider = google
  project  = var.gcp_project_id
  location = var.region
  name     = var.service_name

  # ``deletion_protection`` defaults true on new resources, which would
  # block ``terraform destroy`` during dev iteration.  Off for dev/
  # staging.  prod-level lock comes from a separate IAM policy.
  deletion_protection = var.env == "prod" ? true : false

  template {
    # Placeholder image so the service exists; deploy.sh swaps it on
    # first push.  Cloud Run requires a runnable image at create time.
    containers {
      image = "gcr.io/cloudrun/hello"

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        cpu_idle          = false # equivalent to --no-cpu-throttling
        startup_cpu_boost = true
      }
    }

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    max_instance_request_concurrency = var.concurrency
  }

  # Cloud Run reads env vars from the running revision, not from the
  # service spec.  ``deploy.sh`` updates env vars via
  # ``gcloud run deploy --env-vars-file`` on every deploy, so TF
  # ignores changes to the template after creation -- otherwise every
  # plan would show a diff against the live deployed revision.
  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      template[0].containers[0].env,
      client,
      client_version,
    ]
  }

  labels = {
    env   = var.env
    owner = "forge"
  }
}

# Public invoker -- desktop clients hit Cloud Run unauthenticated and
# the FastAPI app does its own Firebase-Bearer auth on protected routes.
resource "google_cloud_run_v2_service_iam_member" "public" {
  provider = google
  project  = var.gcp_project_id
  location = var.region
  name     = google_cloud_run_v2_service.main.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
