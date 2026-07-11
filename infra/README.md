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
- R2 bucket `pc-rent-<env>-blends` in Cloudflare → rendered frame outputs
- R2 bucket `pc-rent-logs-<env>` in Cloudflare → worker stdout logs (jobs_logger). Kept separate so retention lifecycle (recommended: delete after 30 days) can be tightened without touching user output. Reuses the same account-scoped R2 keys.
- ⚠️ **Separate Vast.ai account per env** — without this, this env's backup monitor will see other envs' Vast instances and destroy them. Shared Vast key = cross-env render kills.

**Fill `infra/envs/<env>/.env`** — copy `.env.example`. The `*_IMAGE` lines default to the existing test tags (`pcrent-*:5.0.4`), so the env works out of the box. Push env-specific tags later (see below) only when you want isolation.

**(Optional) web dashboard at `<backend-url>/home`** — serverV2 serves a browser dashboard (admin console for `role: "admin"` users, personal render stats for everyone else). Two manual steps per env:
1. Add `FIREBASE_WEB_CONFIG_JSON=<one-line JSON of the env's Firebase *web* SDK config>` to the env's `.env` (server serves it to the SPA via `GET /app/web-config`; web configs are public identifiers, not secrets).
2. In Firebase Console → Authentication → Settings → Authorized domains, add the env's Cloud Run domain so Google sign-in works from the browser.
Admins are minted by setting `role: "admin"` on the `users/{uid}` Firestore doc.

**(Optional) push env-specific worker images** — to get `:<env>-vX.Y.Z` tags:
```bash
bash infra/scripts/push-worker.sh <env> 0.0.22 vast-cycles
bash infra/scripts/push-worker.sh <env> 0.0.22 vast-eevee
bash infra/scripts/push-worker.sh <env> 0.0.22 modal-cycles
bash infra/scripts/push-worker.sh <env> 0.0.22 community-cycles
```
Then on GHCR, flip each new `pc-rent-*-worker-*` package from private → **public** (Modal/Vast can't pull private images). Finally bump the four `*_IMAGE` lines in `.env` to `:<env>-v0.0.22`.

**Deploy chain (run in this order):**
```bash
bash infra/scripts/tf-apply.sh <env>
bash infra/scripts/migrate-db.sh <env>
bash infra/scripts/seed-firestore.sh <env>
bash infra/scripts/deploy-modal.sh <env>
bash infra/scripts/deploy-server.sh <env>
bash infra/scripts/deploy-backup-monitor.sh <env>
```

**(Optional) per-env desktop** — copy `desktop.config.json.example` to `desktop.config.json`, fill in `backendUrl`, `firebaseConfig`, and `googleOAuth` (the env's "Desktop app" OAuth client id/secret), then run dev mode or build:
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

**Worker code change** — for EACH variant the fleet uses:

1. Push:
   ```bash
   bash infra/scripts/push-worker.sh <env> <X.Y.Z> vast-cycles
   bash infra/scripts/push-worker.sh <env> <X.Y.Z> vast-eevee
   bash infra/scripts/push-worker.sh <env> <X.Y.Z> modal-cycles
   bash infra/scripts/push-worker.sh <env> <X.Y.Z> community-cycles
   ```
2. **First push only**: flip each new `pc-rent-*-worker-*` package on GHCR from private → public (Modal/Vast/community can't pull private images).
3. Bump the matching `*_IMAGE` keys in `infra/envs/<env>/.env`:
   - `VAST_DOCKER_IMAGE` ← `vast-cycles` tag
   - `VAST_DOCKER_IMAGE_EEVEE` ← `vast-eevee` tag
   - `MODAL_WORKER_IMAGE_CYCLES` ← `modal-cycles` tag
   - `COMMUNITY_WORKER_IMAGE` ← `community-cycles` tag
4. Re-deploy:
   ```bash
   bash infra/scripts/deploy-server.sh <env>     # vast + community read VAST_DOCKER_IMAGE / COMMUNITY_WORKER_IMAGE from Cloud Run env
   bash infra/scripts/deploy-modal.sh <env>      # modal reads MODAL_WORKER_IMAGE_CYCLES at modal deploy time
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


Bump desktop/src-tauri/tauri.conf.json version (e.g. 1.0.1 → 1.0.2), commit it.
git tag desktop-v1.0.2 && git push origin desktop-v1.0.2