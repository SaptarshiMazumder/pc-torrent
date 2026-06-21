# Forge multi-env infra

Test env still runs off root `deploy.sh` / `push-worker.sh` / `serverV2/.env` — untouched. Everything below is for `dev` / `staging` / `prod`.

---

## Cold start a new env

**One-time per machine:**
```bash
gcloud auth application-default login
bash infra/terraform/bootstrap.sh
```

**External accounts to create manually (per env):**
- Firebase project `pc-rent-<env>` → service account JSON + web SDK config
- Neon project + DB → pooled connection URL
- Upstash global Redis DB → `rediss://` URL
- R2 bucket `pc-rent-<env>-blends` in Cloudflare → reuse account-scoped R2 keys
- Separate Vast.ai account → API key (recommended; see Gotchas)

**Fill `infra/envs/<env>/.env`** — copy `.env.example`, paste everything above.

**(Optional) push env-specific worker images** — skip to reuse test's `:5.0.4`:
```bash
bash infra/scripts/push-worker.sh <env> 0.0.1 vast-cycles
bash infra/scripts/push-worker.sh <env> 0.0.1 modal-cycles
bash infra/scripts/push-worker.sh <env> 0.0.1 community-cycles
```
Then flip each new `pc-rent-*-worker-*` GHCR package to **public**, and bump `VAST_DOCKER_IMAGE` / `MODAL_WORKER_IMAGE_CYCLES` / `COMMUNITY_WORKER_IMAGE` in `.env` to `:<env>-v0.0.1`.

**Deploy chain (run in this order):**
```bash
bash infra/scripts/tf-apply.sh <env>
bash infra/scripts/migrate-db.sh <env>
bash infra/scripts/seed-firestore.sh <env>
bash infra/scripts/deploy-modal.sh <env>
bash infra/scripts/deploy-server.sh <env>
bash infra/scripts/deploy-backup-monitor.sh <env>
```

**(Optional) per-env desktop** — copy `desktop.config.json.example` to `desktop.config.json`, fill in `backendUrl` + `firebaseConfig`, then run dev mode or build:
```bash
bash infra/scripts/desktop-dev.sh <env>     # hot reload against env
bash infra/scripts/desktop-build.sh <env>   # produce installer
```

---

## Common workflows

**Server code change**
```bash
bash infra/scripts/deploy-server.sh <env>
```

**Worker code change** — push, bump tag in `.env`, redeploy the fleet that uses it:
```bash
bash infra/scripts/push-worker.sh <env> <X.Y.Z> <variant>
bash infra/scripts/deploy-server.sh <env>     # vast / community
bash infra/scripts/deploy-modal.sh <env>      # modal
```

**Modal code change**
```bash
bash infra/scripts/deploy-modal.sh <env>
```

**Backup monitor code change**
```bash
bash infra/scripts/deploy-backup-monitor.sh <env>
```

**Desktop dev (hot reload)**
```bash
bash infra/scripts/desktop-dev.sh <env>
```

**Desktop release (installer)**
```bash
bash infra/scripts/desktop-build.sh <env>
```

**Rollback serverV2**
```bash
bash infra/scripts/rollback.sh <env>                # list revisions
bash infra/scripts/rollback.sh <env> <revision>     # shift 100% traffic
```

**Nuke env**
```bash
bash infra/scripts/destroy.sh <env>                 # prod needs --i-know
```

---

## Script reference

| Script | Does |
|---|---|
| `tf-apply.sh <env>` | Creates Cloud Run, Artifact Registry, backup_monitor Job + Scheduler. Patches `.env` with output URLs. |
| `deploy-server.sh <env>` | Builds serverV2 image, deploys to Cloud Run with `.env` as env-vars. |
| `deploy-modal.sh <env>` | Deploys Modal app `pc-rent-render-<env>`. |
| `deploy-backup-monitor.sh <env>` | Builds + swaps backup_monitor image on the Job. |
| `push-worker.sh <env> <ver> <variant>` | Builds + pushes GHCR worker image (`:<env>-v<ver>` + `:<env>`). |
| `desktop-dev.sh <env>` | Patches desktop/ source for env, runs `tauri dev`, restores on exit. |
| `desktop-build.sh <env>` | Builds env-specific installer to `infra/dist/forge-<env>-setup.exe`. |
| `seed-firestore.sh <env>` | Copies `config/*` from test Firestore to `<env>` Firestore. |
| `migrate-db.sh <env>` | Schema-only `pg_dump` from test → env's Neon. Required on first cold-start; idempotent. |
| `rollback.sh <env> [revision]` | Shifts Cloud Run traffic to a prior revision. |
| `destroy.sh <env>` | `terraform destroy` for the env. |

Variants for `push-worker.sh`: `base-cycles`, `base-eevee`, `vast-cycles`, `vast-eevee`, `modal-cycles`, `community-cycles`.

---

## Gotchas

- **Vast accounts must be per-env.** Backup monitor's ghost scanner lists every Vast instance on the account; shared account = one env's monitor can kill another env's renders.
- **First GHCR push creates a private package.** Modal/Vast/community workers can't pull private images — flip each new `pc-rent-*-worker-*` package to **public** in GitHub Packages settings.
- **Modal deploys before server.** serverV2 boot validates every `modal_instances` endpoint URL exists.
- **`deploy-server.sh` re-injects the entire `.env` as Cloud Run env vars on every run.** Edit `.env` then re-deploy to push config changes.
- **`tf-apply.sh` only manages GCP.** Neon, Upstash, R2, Firebase are all manual-create-then-paste-into-`.env`.

---

## Firestore per env

- `config/global` — config mirror; seeded from test by `seed-firestore.sh`. Editable via desktop ConfigurationPage.
- `config/cost_estimation` — LLM cost estimator settings.
- `users/{uid}` — created lazily on first sign-in.
- `allocation_cost_file_formulas` — LLM formula cache, built on demand.
