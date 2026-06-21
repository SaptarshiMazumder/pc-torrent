# Forge -- Terraform layout

State backend
:    GCS bucket ``gs://pc-rent-tf-state`` (same GCP project as Cloud Run).  Each env has its own state file under the env's prefix.

State backend setup is a one-time manual step (chicken-and-egg: TF needs a backend to store the state, but the backend is what TF creates).  Run ``bash infra/terraform/bootstrap.sh`` once.

## Layout

```
infra/terraform/
  backend.tf*          per-env override, points at gs://pc-rent-tf-state/<env>
  providers.tf         shared provider declarations (google, neon, upstash, cloudflare)
  bootstrap.sh         one-time GCS state-bucket creation
  README.md            this file

  modules/             shared module library -- resource definitions live here ONCE
    cloud_run/         Artifact Registry repo + Cloud Run service shell
    neon_db/           Neon project + branch + role + database
    upstash_redis/     Upstash Redis database
    r2_bucket/         Cloudflare R2 bucket + scoped API token

  envs/<env>/          thin per-env compositions (~25 lines each)
    main.tf            calls all 4 modules with this env's values
    variables.tf       inputs sourced from TF_VAR_* env vars
    outputs.tf         exposes backend_url, database_url, redis_url, r2_* for scripts to consume
    terraform.tfvars.example   template (real values come from infra/envs/<env>/.env)
```

\* ``backend.tf`` is per-env (each env points at a different state-file prefix), so each env directory has its own copy.  Phase 2 will provide a small wrapper that copies a templated ``backend.tf`` into the active env's directory before ``terraform init``.

## What lives in TF vs not

In TF
:    GCP Cloud Run, GCP Artifact Registry, GCP IAM bindings, Neon project + DB, Upstash Redis DB, Cloudflare R2 bucket + scoped token.

Not in TF
:    Firebase project (manual one-time click-through), Modal deploys (CLI flow), GHCR worker images (push-worker.sh), Vast.ai account (no provider), GitHub releases, frontend (retired).

## Auth -- what each provider needs

| Provider | Auth env var(s) | Where to get it |
|---|---|---|
| ``google`` | ``GOOGLE_APPLICATION_CREDENTIALS`` or ``gcloud auth application-default login`` | GCP service account or your gcloud login |
| ``neon`` | ``NEON_API_KEY`` | Neon console -> Account settings -> API keys |
| ``upstash`` | ``UPSTASH_EMAIL`` + ``UPSTASH_API_KEY`` | Upstash console -> Account -> Management API |
| ``cloudflare`` | ``CLOUDFLARE_API_TOKEN`` | Cloudflare dashboard -> My Profile -> API Tokens (scope: ``R2:Edit``) |

These four (six counting GCP) provisioner secrets live in each env's ``infra/envs/<env>/.env`` alongside the runtime secrets serverV2 reads.  Phase 2's ``deploy.sh`` exports them as ``TF_VAR_*`` before calling Terraform.

## Workflow (manual, until Phase 2 wraps it in deploy.sh)

```bash
# One-time, for the whole org
bash infra/terraform/bootstrap.sh

# Per env -- source the env's .env, export provisioner secrets, then plan/apply
set -a
. infra/envs/dev/.env
set +a
export TF_VAR_env=dev
export TF_VAR_gcp_project_id="$GCP_PROJECT"
# ... (Phase 2 wrapper handles the boilerplate)

cd infra/terraform/envs/dev
terraform init
terraform plan
# terraform apply       <- gated until Phase 2 review
```

## Open notes (for later phases)

* **Bootstrap order on a fresh env**: Firebase project creation must happen MANUALLY before ``terraform apply`` (TF doesn't manage Firebase project creation; only Firestore rules later, optional).  After Firebase exists, paste the service-account JSON into ``infra/envs/<env>/.env`` and the Cloud Run TF module picks it up via env var injection at deploy time.

* **State bucket location** = ``asia-northeast1`` (same region as Cloud Run).  Change at your peril -- moving a TF state bucket later requires copying + reconfiguring every env's ``backend.tf``.

* **Provider versions** are pinned to majors only.  ``terraform init`` pulls latest patch within the range.
