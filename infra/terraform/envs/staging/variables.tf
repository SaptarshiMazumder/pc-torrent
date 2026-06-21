# Per-env inputs.  See ../dev/variables.tf for the full annotations.

variable "env" {
  type    = string
  default = "staging"
}

variable "gcp_project_id" { type = string }
variable "gcp_region"     { type = string; default = "asia-northeast1" }

variable "orphan_secret"  { type = string; sensitive = true }
variable "vast_api_key"   { type = string; sensitive = true }
variable "database_url"   { type = string; sensitive = true }
variable "redis_url"      { type = string; sensitive = true }
