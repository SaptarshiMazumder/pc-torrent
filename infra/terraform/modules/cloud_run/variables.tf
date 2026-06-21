variable "env" {
  description = "Environment name (dev / staging / prod).  Used in resource names + labels."
  type        = string
}

variable "gcp_project_id" {
  description = "GCP project ID hosting Cloud Run + Artifact Registry."
  type        = string
}

variable "region" {
  description = "GCP region for both Cloud Run and Artifact Registry."
  type        = string
}

variable "service_name" {
  description = "Cloud Run service name (e.g. pc-rent-server-v2-dev)."
  type        = string
}

variable "artifact_registry_repo_name" {
  description = "Artifact Registry Docker repo name (e.g. pc-rent-dev)."
  type        = string
}

variable "min_instances" {
  description = "Minimum container instances kept warm.  Forge uses 1 to avoid cold-start latency on heartbeat polling."
  type        = number
  default     = 1
}

variable "max_instances" {
  description = "Max concurrent instances.  Forge's traffic shape barely uses 1; leave headroom."
  type        = number
  default     = 5
}

variable "cpu" {
  description = "Per-instance vCPUs."
  type        = string
  default     = "2"
}

variable "memory" {
  description = "Per-instance memory (Cloud Run gibibyte units)."
  type        = string
  default     = "2Gi"
}

variable "concurrency" {
  description = "Concurrent requests per instance."
  type        = number
  default     = 80
}
