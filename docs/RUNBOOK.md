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

Production readiness is fail-closed for remote PostgreSQL transport, the exact
Alembic head, and the process-specific API/worker database profile. Remote managed database
URLs require `sslmode=verify-full` before any application or migration
connection; a discrete remote `POSTGRES_*` configuration uses
`POSTGRES_SSLMODE=verify-full`. The gate supplies `gssencmode=disable` when
absent and rejects conflicting values to keep this an actual TLS boundary. Do
not downgrade `sslmode` to `require` or
`verify-ca`, which do not satisfy the hostname-verification policy.

Use the dashboard's jobs, failures, action events, source freshness, mission blockers, and experiment views. A healthy process is not evidence of successful collection or business impact. Missing GA4/GSC data remains unknown; fixture data remains labelled; a completed crawl can carry incomplete coverage or robots/network quality flags.

The worker heartbeat reports scheduler liveness only. Failed jobs record the exception class and canonical IDs rather than external response bodies or credentials. The API and worker stop at startup if configured with a schema-owning, cross-profile, or otherwise privileged database login; each production scheduler tick rechecks the exact worker profile before doing work.

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

For the locked verification package, set `SEO_RELEASE_IMAGE` to the reviewed
`repository@sha256:...` (or exact local `sha256:...` ID) and use both Compose
files. The overlay injects the same selector into `migrate`; its entrypoint
rejects a tag, missing value, or malformed digest before owner database URL
construction and before `migrate()` is called. A failure leaves API and worker
blocked on unsuccessful migration completion.

Also set `SEO_MIGRATION_EXPECTED_DATABASE`,
`SEO_MIGRATION_EXPECTED_SYSTEM_IDENTIFIER`, `SEO_MIGRATION_MODE`, and
`SEO_MIGRATION_EXPECTED_SCHEMA_HEADS` from the independent release/target
record. `bootstrap` requires an empty `public` schema and the literal head value
`uninitialized`; `upgrade` requires the exact sorted predecessor head list.
The migration first confirms those pins in a read-only transaction, then pins
`search_path=public`, reacquires the checks and a nonblocking advisory lock on
the exact DDL connection, and commits Alembic plus both runtime-role grants in
one transaction.

Migrations run with the owner role and then atomically reapply the distinct API and worker grants. New tables receive no implicit worker privilege: an unclassified table stops provisioning/startup. Startup, health, and per-tick role verification provide further gates. Alembic drift checks and real PostgreSQL tests run in CI. Schema rollback is an operator-reviewed migration/restore decision; the application never rewrites immutable history to undo a release.

The runtime profiles require an application-dedicated PostgreSQL cluster: each
runtime login must lack effective `CONNECT` and `TEMPORARY` on every database
except the selected application database. Fresh Compose volumes apply this
boundary during `initdb`. Because PostgreSQL init hooks do not rerun on existing
volumes, an authorized owner must revoke ambient `PUBLIC CONNECT,TEMPORARY` on
all databases in an upgraded dedicated cluster before running `migrate`.
Provisioning deliberately fails rather than changing database ACLs across an
unknown or shared cluster.

For the runtime-role split upgrade, provision independent
`POSTGRES_API_PASSWORD` and `POSTGRES_WORKER_PASSWORD` values in the secret
manager before starting `migrate`; any legacy `POSTGRES_APP_*` value is not
accepted as a fallback. Keep all services stopped if either capability
is absent or reused.

## Backup and recovery

The named PostgreSQL volume persists across container recreation and `docker compose down`. Volume persistence is not a backup. Store backups encrypted outside the Docker host and test restoring them to an isolated database.

For a release checkpoint, stop API/worker writes and use the atomic,
non-overwriting backup utility with an already-configured private libpq service.
Pin all values from the reviewed release/checkpoint record; the concrete values
below are illustrative:

```sh
docker compose stop worker api
python -m scripts.backup_database \
  --service seo_checkpoint_source \
  --output-directory /operator/private/seo-backups \
  --writers-stopped \
  --expected-database seo_production \
  --expected-server-identity 7439284610293847561 \
  --expected-server-address 192.0.2.10 \
  --expected-server-port 5432 \
  --tls-server-name db.example.net \
  --schema-head 0002_runtime_role_split \
  --release-image registry.example/seo@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --release-commit 0123456789abcdef0123456789abcdef01234567 \
  --runtime-identity deployment:seo-prod-20260904T120000Z \
  --checkpoint-identity action:00000000-0000-4000-8000-000000000000
```

The utility requires `psql`, `pg_dump` and `pg_restore`; the service supplies the
login/passfile while a bounded non-secret conninfo argument pins the database,
TLS hostname, numeric address and port. It forcibly overrides service transport
with `sslmode=verify-full` and `gssencmode=disable`; no password appears in the
argument or environment. It independently observes the database, endpoint,
PostgreSQL cluster system identifier, server version and schema heads before and
after `pg_dump` and refuses a pin or stability mismatch. Successful output is one private `.backup`
directory containing a custom archive and receipt. A hidden staging directory is
atomically renamed only after both members are synced, checked and bound to the
source/release/runtime/checkpoint identities and dump timestamps. Any `pg_dump`
or archive-list stderr blocks promotion even when the process exits zero. Failed
staging directories are retained privately; no service is automatically
restarted.

The child tools receive only a minimal environment (path/home, explicit
service/passfile/system-service-file paths, optional system certificate paths,
fixed C locale/UTC and fixed TLS/GSS defaults). They do not inherit ambient libpq
target/query options or unrelated bearer tokens. Keep CA/client-certificate
paths and credentials in the reviewed private service/passfile configuration.

Copy the emitted archive SHA-256 and the expected identities into the independent
release/checkpoint record before using the verification result for a copy or
restart decision. A receipt alone is not an independent expected-value source.

Verify the nested receipt before any restart or copy decision:

```sh
python -m scripts.verify_backup \
  --receipt /operator/private/seo-backups/seo-20260904T120000Z-0123456789ab.backup/seo-20260904T120000Z-0123456789ab.json \
  --expected-service seo_checkpoint_source \
  --expected-database seo_production \
  --expected-server-identity 7439284610293847561 \
  --expected-server-address 192.0.2.10 \
  --expected-server-port 5432 \
  --expected-tls-server-name db.example.net \
  --schema-head 0002_runtime_role_split \
  --expected-release-image registry.example/seo@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --expected-release-commit 0123456789abcdef0123456789abcdef01234567 \
  --expected-runtime-identity deployment:seo-prod-20260904T120000Z \
  --expected-checkpoint-identity action:00000000-0000-4000-8000-000000000000 \
  --expected-archive-sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
```

The verifier checks every identity pin, the exact two-member bundle,
version/time/warning fields, private modes, independently expected checksum and
archive list; it still reports `restore_verified=false`. Pre/post checks cannot
cryptographically prove a same-address proxy's middle backend, and the unsigned
receipt is not hostile-source provenance. Preserve
configuration and credentials separately in an operator-controlled secret store.
Dumps contain business records and immutable audit history. See the
[durable package](DURABLE_DEPLOYMENT_PACKAGE.md) for managed-provider requirements,
API-first restart, isolated restore and image/schema rollback gates.

Restore first into a fresh, isolated PostgreSQL 17 database with compatible owner role names. Keep the application stopped, restore the archive with `pg_restore --exit-on-error`, run owner migrations and runtime grants, then verify migration version, row counts, audit immutability, source provenance, and active execution leases. Start the API for review before the worker. Reconcile uncertain CMS effects before resuming recurrence. Do not restore over the current production database without an explicit recovery decision.

## Rotate credentials

`scripts/init_env.py` never rotates existing credentials. Use the operator's secret manager to generate replacements; keep application, reviewer, administrator, migration-owner and runtime passwords distinct.

- API/reviewer/admin tokens: update private configuration, recreate affected services, and replace the corresponding human/tool connection credentials. Old bearer tokens cease to authenticate when the new configuration is loaded.
- API PostgreSQL password: stop the API, update `POSTGRES_API_PASSWORD`, run the owner-only `migrate` service to reapply exact grants, then recreate the API.
- Worker PostgreSQL password: stop the worker, update `POSTGRES_WORKER_PASSWORD`, run the owner-only `migrate` service to reapply exact grants, then recreate the worker.
- Migration owner password: updating `POSTGRES_PASSWORD` in an existing volume does not alter the existing PostgreSQL role. Rotate the role through an authorized database-administration channel and update private configuration together.
- Provider and OAuth credentials: rotate at their issuer, replace the scoped local secret or pinned public verification key, and recreate the consuming service.

Use `docker compose config --quiet` to validate configuration without printing interpolated credentials. Do not publish `.env`, credential files, database dumps, or expanded container environment output. Local secrets and runtime databases are excluded by `.gitignore` and `.dockerignore`.

## Validation limits

Run the complete fixture/test suite and lint locally. Set `TEST_POSTGRES_URL` to
an isolated owner-capable PostgreSQL test instance to exercise actual migrations,
append-only triggers, leases, and runtime-role rejection. The dump/restore gate
also requires `TEST_POSTGRES_CONTAINER` to be the exact 12–64 lowercase-hex ID of
that disposable PostgreSQL 17 container; the test validates its image before
sending archive bytes. Supplying only one variable is not a partial pass. The
tests create and remove only their own randomly named databases and runtime role.
They must not point at an operational database account without authority to
create isolated test databases.

CI can additionally build/start the Compose stack and verify demo bootstrap using
the restricted runtime role. A historical successful CI run applies only to its
recorded commit. Until these gates pass for the current tree, a machine without
Docker/PostgreSQL has only local Python and configuration validation; it has not
validated the current container or real backup/restore behavior.
