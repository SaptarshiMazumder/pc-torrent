# Backup monitor for ONE env.  Five resources:
#   * Artifact Registry repo dedicated to backup_monitor images
#   * Dedicated scheduler service account (least-privilege)
#   * Cloud Run Job shell (placeholder image; deploy-backup-monitor.sh swaps it)
#   * IAM: scheduler SA -> run.invoker on the Job
#   * Cloud Scheduler cron triggering the Job once a minute
#
# Env vars on the Job are written by TF (frozen at deploy).  The deploy
# script re-writes them on every image swap so they always reflect the
# current .env -- prevents the test-env class of drift bug (point 4 in
# the description).

# --- Artifact Registry repo (env-scoped) -------------------------
resource "google_artifact_registry_repository" "backup_monitor" {
  provider      = google
  project       = var.gcp_project_id
  location      = var.region
  repository_id = var.artifact_registry_repo_name
  description   = "Forge backup_monitor container images for env=${var.env}"
  format        = "DOCKER"

  labels = {
    env     = var.env
    service = "backup-monitor"
    owner   = "forge"
  }
}

# --- Dedicated scheduler service account -------------------------
# Least-privilege.  Only grant: run.invoker on the Job (below).
# The Job's runtime SA is the project's default Compute SA -- it
# makes no GCP API calls (only outbound HTTP to Neon / Upstash /
# Vast / serverV2), so the default SA's identity is sufficient.

resource "google_service_account" "scheduler" {
  provider     = google
  project      = var.gcp_project_id
  account_id   = "bm-scheduler-${var.env}"
  display_name = "Backup-monitor scheduler (${var.env})"
  description  = "Scheduler-side identity that triggers the ${var.env} backup-monitor Job once a minute.  Only role: run.invoker on the Job."
}

# --- Cloud Run Job (shell) ---------------------------------------
resource "google_cloud_run_v2_job" "main" {
  provider = google
  project  = var.gcp_project_id
  location = var.region
  name     = var.job_name

  deletion_protection = var.env == "prod" ? true : false

  template {
    task_count  = 1
    parallelism = 1

    template {
      timeout     = var.task_timeout
      max_retries = var.max_retries

      containers {
        # Placeholder so the Job can be created before any monitor
        # image has been built.  deploy-backup-monitor.sh swaps this
        # to the real ghcr-image on first push.  Cloud Run requires
        # a runnable image reference at create time.
        image = "gcr.io/cloudrun/hello"

        resources {
          limits = {
            cpu    = var.cpu
            memory = var.memory
          }
        }

        env {
          name  = "ENV_NAME"
          value = var.env
        }
        env {
          name  = "DATABASE_URL"
          value = var.database_url
        }
        env {
          name  = "REDIS_URL"
          value = var.redis_url
        }
        env {
          name  = "ORCHESTRATOR_URL"
          value = var.orchestrator_url
        }
        env {
          name  = "ORPHAN_SECRET"
          value = var.orphan_secret
        }
        env {
          name  = "VAST_API_KEY"
          value = var.vast_api_key
        }
        env {
          name  = "HTTP_TIMEOUT_SEC"
          value = tostring(var.http_timeout_sec)
        }
        env {
          name  = "VAST_GHOST_MIN_AGE_SEC"
          value = tostring(var.vast_ghost_min_age_sec)
        }
        env {
          name  = "MODAL_TERMINAL_WINDOW_HOURS"
          value = tostring(var.modal_terminal_window_hours)
        }
        env {
          name  = "MODAL_TERMINAL_MAX_ROWS"
          value = tostring(var.modal_terminal_max_rows)
        }
      }
    }
  }

  labels = {
    env     = var.env
    service = "backup-monitor"
    owner   = "forge"
  }

  # deploy-backup-monitor.sh swaps image + refreshes env vars from the
  # current .env on every deploy.  Without ignore_changes, every TF
  # plan would diff against the live deployed values.
  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      template[0].template[0].containers[0].env,
      client,
      client_version,
    ]
  }
}

# --- IAM: scheduler SA can invoke the Job ------------------------
resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  provider = google
  project  = var.gcp_project_id
  location = var.region
  name     = google_cloud_run_v2_job.main.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

# --- Cloud Scheduler trigger -------------------------------------
resource "google_cloud_scheduler_job" "tick" {
  provider = google
  project  = var.gcp_project_id
  region   = var.region
  name     = var.scheduler_name
  schedule = var.schedule

  attempt_deadline = "320s" # default; well above the 60s tick

  retry_config {
    retry_count = 0 # next minute's tick recovers; no need to retry
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.gcp_project_id}/jobs/${google_cloud_run_v2_job.main.name}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  # The IAM binding must exist before the first tick fires, else the
  # very first invocation will 403.
  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_invoker]
}
