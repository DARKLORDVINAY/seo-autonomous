# Operations runbook

The backend worker owns recurrence. Closing the dashboard or this chat does not stop a running deployment. Scheduled work uses the canonical database for job history, idempotency, evidence, failures, and decisions.

## Cadence and bounded work

All times use `SCHEDULER_TIMEZONE` (default `UTC`).

| Job | Schedule | Work and authority |
| --- | --- | --- |
| Daily cycle | Every day, 05:00 | Ingest, diagnose, bounded specialists, eligible guarded revisions, measurement |
| Integrity crawl | Every day, 12:00 | Robots-aware same-origin crawl, at most 50 pages and within configured limits; no model call or CMS write |
| Evening measurement | Every day, 19:00 | Evaluate due recorded experiment windows and calibration; no model call or production mutation |
| Weekly review | Monday, 06:00 | Review stored strategy, calibration, failures and blockers; append a decision; no promotion or model call |

APScheduler 3 uses one normal job thread, `max_instances=1`, coalescing, and a one-hour misfire grace. A separate heartbeat thread remains responsive during observations. These concurrency/misfire controls follow the [APScheduler 3 user guide](https://apscheduler.readthedocs.io/en/3.x/userguide.html). Database leases protect against additional worker processes and concurrent API cycles.

At startup, the worker reconciles already-due slots for the current local day and the current week's review. It does not replay an unbounded backlog. Completed period keys return the existing result. The daily cycle's core owns its site lease; scheduler observation jobs acquire the same `site-cycle:<site_id>` lease. Each protected commit conditionally renews and locks the fencing token, while a separate session renews during network waits. A worker that loses its lease rolls back instead of committing stale state.

Observation/review failures or interrupted runs can retry the same durable `JobRun`; each attempt has immutable start/final audit events. A crashed core cycle keeps its existing job and model reservations for diagnostic review rather than silently repeating possibly billed calls. Expiring observation leases are separate from non-expiring execution leases, which remain until external CMS state is reconciled.

## Daily checks

```sh
docker compose ps
docker compose logs --since 24h --tail 200 worker api migrate
curl --fail --silent http://127.0.0.1:8000/readyz
docker compose run --rm worker worker --describe
```

Use the dashboard's jobs, failures, action events, source freshness, mission blockers, and experiment views. A healthy process is not evidence of successful collection or business impact. Missing GA4/GSC data remains unknown; fixture data remains labelled; a completed crawl can carry incomplete coverage or robots/network quality flags.

The worker heartbeat reports scheduler liveness only. Failed jobs record the exception class and canonical IDs rather than external response bodies or credentials. The API and worker will stop at startup if configured with a schema-owning or otherwise privileged database login.

## Trigger one scheduled operation

Use the site ID shown in the dashboard:

```sh
docker compose run --rm worker worker --once integrity-crawl --site-id SITE_UUID
docker compose run --rm worker worker --once evening-measurement --site-id SITE_UUID
docker compose run --rm worker worker --once weekly-review --site-id SITE_UUID
```

These commands preserve the normal period key, site lease, and canonical audit. A completed slot replays its result. `--once daily-cycle` invokes the bounded core and can use configured live model allowance; check outstanding calls and reservations before deliberately requesting a separate manual cycle. The one-off CLI is available even when recurrence is disabled; it does not bypass core authority or budget checks.

## Pause and resume

To stop future scheduled work:

```sh
docker compose stop worker
```

This requests graceful completion of active work. To keep it disabled through future recreations, set `SCHEDULER_ENABLED=false` in private configuration and leave the worker stopped; an explicitly disabled worker exits without opening the database. API users can still request operations subject to their configured capabilities.

To disable website writes across the instance, set `PRODUCTION_ENABLED=false` and `SHADOW_MODE=true`, then recreate API and worker. Preserve execution leases and audit records while inspecting in-flight CMS actions; a stopped process alone does not prove an external request did not succeed. Administrator endpoints only provide their explicitly documented authority; there is no generic endpoint for changing arbitrary site configuration or raising autonomy.

After resolving blockers and reviewing active execution leases:

```sh
docker compose up -d api worker
```

If recurrence was disabled in configuration, enable it before restarting the worker. Startup reconciliation uses the existing canonical period keys.

## A job failed or a lease was lost

1. Inspect the job's status, action events, failure record, and provider quality flags.
2. Check source credentials, property/site scope, network reachability and robots rules. An unavailable optional source is not zero performance.
3. For `lease_busy`, inspect the current owner and its active job. Do not delete leases to create parallel work.
4. For `lease_lost`, let the previous owner finish stopping. The stale final transaction was rejected; earlier committed observations can exist. Observation retries reuse the job and record a new attempt.
5. For a core cycle interrupted during a live call, inspect recorded start/finish events and retained model-cost reservations. Do not clear unknown costs to create more allowance.
6. For any CMS execution with an uncertain outcome, use the administrator reconciliation endpoint after independently checking the exact external revision. The non-expiring execution lease is intentional.

Do not change production authority, verification outcomes, approval expiry, or audit rows to make a retry pass. Resolve the reported guard or choose a smaller supported action.

## Upgrades

Review the change and take a restorable backup first. Stop writers before schema changes:

```sh
docker compose stop worker api
docker compose build
docker compose run --rm migrate
docker compose up -d --wait api worker
```

Migrations run with the owner role and then reapply the restricted runtime grants. New tables receive no automatic blanket update privilege. Startup role verification provides a further gate. Alembic drift checks and real PostgreSQL tests run in CI. Schema rollback is an operator-reviewed migration/restore decision; the application never rewrites immutable history to undo a release.

## Backup and recovery

The named PostgreSQL volume persists across container recreation and `docker compose down`. Volume persistence is not a backup. Store backups encrypted outside the Docker host and test restoring them to an isolated database.

For a release checkpoint, stop API/worker writes and use the non-overwriting backup utility with an already-configured private libpq service:

```sh
docker compose stop worker api
python -m scripts.backup_database --service seo_checkpoint_source --output-directory /operator/private/seo-backups --writers-stopped
```

The utility requires PostgreSQL client tools; the service identifies the exact database without a password/DSN argument. It creates a unique private archive, verifies process success and archive listing, and records a checksum. Failed partial files are retained; no service is automatically restarted. Preserve configuration and credentials separately in an operator-controlled secret store. Dumps contain business records and immutable audit history. See the [durable package](DURABLE_DEPLOYMENT_PACKAGE.md) for managed-provider requirements, API-first restart, isolated restore and image/schema rollback gates.

Restore first into a fresh, isolated PostgreSQL 17 database with compatible owner role names. Keep the application stopped, restore the archive with `pg_restore --exit-on-error`, run owner migrations and runtime grants, then verify migration version, row counts, audit immutability, source provenance, and active execution leases. Start the API for review before the worker. Reconcile uncertain CMS effects before resuming recurrence. Do not restore over the current production database without an explicit recovery decision.

## Rotate credentials

`scripts/init_env.py` never rotates existing credentials. Use the operator's secret manager to generate replacements; keep application, reviewer, administrator, migration-owner and runtime passwords distinct.

- API/reviewer/admin tokens: update private configuration, recreate affected services, and replace the corresponding human/tool connection credentials. Old bearer tokens cease to authenticate when the new configuration is loaded.
- Runtime PostgreSQL password: stop API/worker, update `POSTGRES_APP_PASSWORD`, run the `migrate` service to apply it, then recreate API/worker with the new login.
- Migration owner password: updating `POSTGRES_PASSWORD` in an existing volume does not alter the existing PostgreSQL role. Rotate the role through an authorized database-administration channel and update private configuration together.
- Provider and OAuth credentials: rotate at their issuer, replace the scoped local secret or pinned public verification key, and recreate the consuming service.

Use `docker compose config --quiet` to validate configuration without printing interpolated credentials. Do not publish `.env`, credential files, database dumps, or expanded container environment output. Local secrets and runtime databases are excluded by `.gitignore` and `.dockerignore`.

## Validation limits

Run the complete fixture/test suite and lint locally. Set `TEST_POSTGRES_URL` to an isolated owner-capable PostgreSQL test instance to exercise actual migrations, append-only triggers, leases, and runtime-role rejection. The role test creates and removes its own randomly named database and runtime role. It must not point at an operational database account without authority to create isolated test databases.

CI additionally builds/starts the Compose stack and verifies two demo bootstrap invocations using the restricted runtime role. Until that gate passes, a machine without Docker/PostgreSQL has only local Python and configuration validation; it has not validated a container deployment.
