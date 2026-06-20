# Forge multi-environment infrastructure

Everything under `infra/` is additive.  The legacy `deploy.sh`,
`push-worker.sh`, `deploy-modal.sh`, `release-desktop.sh`,
`serverV2/.env`, and the original `desktop/` source tree are
untouched and continue to deploy the **test** environment exactly
as before.

## Environments

| env       | Where deployed                          | Desktop installer       |
|-----------|-----------------------------------------|-------------------------|
| `test`    | current Cloud Run, current Firebase, current Neon -- use root `deploy.sh` | existing Forge build |
| `dev`     | new Cloud Run `pcrent-server-v2-dev` in same GCP project | per-env build, override allowed |
| `staging` | new Cloud Run `pcrent-server-v2-staging`                  | per-env build, override allowed |
| `prod`    | new Cloud Run `pcrent-server-v2-prod`                     | per-env build, **locked** |

## Layout

```
infra/
  envs/{dev,staging,prod}/
    .env.example                ← template, COPY to .env (gitignored)
    desktop.config.json.example ← template, COPY to desktop.config.json (gitignored)
    README.md                   ← per-env notes
  scripts/                      ← deploy + push + build wrappers (added in Phase 2+)
  terraform/                    ← per-env composition + shared modules (added in Phase 1)
```

## Bootstrap order for a brand-new env

1. Create a Firebase project (manual one-time click-through in Firebase Console).
2. Copy `infra/envs/<env>/.env.example` -> `infra/envs/<env>/.env`.
3. Paste the values you can only get by hand:
   * `FIREBASE_SERVICE_ACCOUNT_JSON` -- from Firebase Console (Service accounts -> generate new key)
   * `OPENAI_API_KEY` -- from platform.openai.com
   * `ANTHROPIC_API_KEY` -- from console.anthropic.com (or leave PLACEHOLDER if using OpenAI only)
   * `VAST_API_KEY` -- from vast.ai console
   * `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` -- run `modal token new` (per env)
4. Copy `infra/envs/<env>/desktop.config.json.example` -> `desktop.config.json` and fill in the Firebase web SDK config (same Firebase project, Web app section in console).
5. (Phase 2 onward) `infra/scripts/deploy.sh <env>` creates the rest via Terraform + populates the auto-fillable fields in `.env` from `terraform output`.

## What lives in each env's Firestore

* `config/global` -- typed mirror of `serverV2/config.json`.  Seeded by `scripts/seed_config_to_firestore.py` per env.
* `config/cost_estimation` -- LLM cost estimator settings.  Per-env model + safety multiplier.
* `users/{uid}` -- per-user profile + credits.  Created lazily on first sign-in.
* `allocation_cost_file_formulas` -- LLM formula cache.  Per-env data.

Worker image tags do NOT live in Firestore -- they live in env vars
(`VAST_DOCKER_IMAGE`, `MODAL_WORKER_IMAGE_CYCLES`, `COMMUNITY_WORKER_IMAGE`)
alongside other deployment metadata.  See the worker tags section
below.

## Worker image tags

Per-env immutable + mutable pointer:

```
:dev-v1.2.0      ← immutable snapshot
:dev             ← mutable pointer (latest pushed dev)
:staging-v1.2.0
:staging
:prod-v1.2.0
:prod
```

Each env's `.env` references the **immutable** version-suffixed tag so a re-push doesn't silently change what's running.  Rollback = bump the tag in `.env` back to the previous version, redeploy.

Three env vars per env's `.env` -- one per fleet:

* `VAST_DOCKER_IMAGE` / `VAST_DOCKER_IMAGE_EEVEE` -- read at instance-create time, injected into each Vast container.
* `MODAL_WORKER_IMAGE_CYCLES` -- read at `modal deploy` time, baked into the Modal function image.
* `COMMUNITY_WORKER_IMAGE` -- served by `GET /community/worker-image` so the agent on each user's PC can ask the backend what to pull.  One agent binary works against any env this way.

`infra/scripts/push-worker.sh <env> <version> <variant>` (added in Phase 2) builds + pushes both tags in one call.

## Gitignore

`.env` and `desktop.config.json` under each env folder must be
gitignored.  `.env.example` and `desktop.config.json.example` are
checked in as templates.
