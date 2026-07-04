# Cost accounting

Read-only aggregation of every paid dependency for the admin cost dashboard.
One `CostSourceProvider` per source (see `providers/`), aggregated by
`CostAccountingService`, surfaced at:

- `GET /admin/costs/overview` — month-to-date + live burn, all sources
- `GET /admin/costs/source/{name}` — one source's usage / series / breakdown

All providers are **read-only and fail-open**. The only writes in the wider
feature are two additive, fail-open observability records that feed two of the
providers: the `llm_usage` table (LLM tokens) and the `daemon:history:*` Redis
rings (daemon graphs). Neither is read by any operational path.

## Sources, confidence, and what upgrades each to exact billing

| Source | Confidence today | Data used now | Env var that makes it exact |
|---|---|---|---|
| `render` (GPU: Vast/Modal/community) | **exact** | our own `render_telemetry` + live in-flight | — already exact |
| `vast_account` (balance) | **exact** | Vast `GET /users/current/` (existing `VAST_API_KEY`) | — |
| `llm` (Anthropic/OpenAI) | estimated | recorded tokens × configured price | set per-model prices in `config/cost_pricing` |
| `redis` (Upstash) | estimated | Redis `INFO` × price | `UPSTASH_MGMT_TOKEN` |
| `postgres` (Neon) | estimated | `pg_database_size` × price | `NEON_API_KEY` |
| `r2` (Cloudflare) | estimated | bucket object bytes × price | `CLOUDFLARE_API_TOKEN` |
| `cloudrun` | estimated | assumed shape × price | `GCP_BILLING_ACCOUNT` |
| `firestore` | unavailable | — | `GCP_BILLING_ACCOUNT` |

A provider flips itself toward exact automatically when its key is present in
the environment; no key ever lives in code. Unit prices live in the Firestore
`config/cost_pricing` doc (`CostPricingConfig` defaults until overridden) and
are read per request, so edits take effect with no redeploy.

## GCP Cloud Run metrics (the "Cloud (GCP)" tab)

`GET /admin/gcp/metrics` → per-service request count, billable instance time,
and instance count as daily series + month totals, plus a usage-based Cloud Run
cost (billable seconds × price). Backed by `GcpMetricsService` +
`CloudMonitoringClient`, which reads Cloud Monitoring via **Application Default
Credentials** — no key.

All Cloud Run services (`pc-rent-server-v2-{dev,staging,prod}`) live in one
compute project (`gen-lang-client-0545494042`), and Monitoring is
project-scoped, so **one query returns every environment's service** — the tab
shows dev/staging/prod side by side from any environment's dashboard. (Business
data — jobs, render cost, Redis, DB — stays per-env, since those stores are
per-env.)

To enable, grant the Cloud Run runtime service account read access once:

```
gcloud projects add-iam-policy-binding gen-lang-client-0545494042 \
  --member="serviceAccount:<runtime-SA>" --role="roles/monitoring.viewer"
```

Until then the tab shows a "grant pending" note. This panel and all cost panels
**slow-poll (30 min) with a manual Refresh button** — Monitoring calls and other
non-live reads don't need per-second polling.
