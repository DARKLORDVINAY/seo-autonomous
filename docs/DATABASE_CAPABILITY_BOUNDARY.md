# API/worker database capability boundary

PostgreSQL uses three non-interchangeable identities. The migration owner owns
schema changes and grants. The API role serves authenticated configuration,
review, approval and execution requests. The scheduler worker role performs
bounded observation, diagnosis, measurement and already-authorized execution.

The worker has no write privilege on any authority-bearing field in `sites` or
`mission_states`, and has `SELECT` only on `policies`, `strategy_versions`,
`approvals`, and `verifications`. It therefore cannot raise or lower site
authority, change production enablement, create an approval, or turn a verifier
preview into an authoritative verification record. It can read exact approvals
and verifications so the executor can evaluate already-authorized revisions.

Worker write access is an explicit per-table allowlist:

- append-only observation/audit tables receive `SELECT, INSERT`;
- `gsc_daily` and `ga4_daily` retain table-level `SELECT, INSERT`; only their
  non-key observation-value columns receive `UPDATE`, permitting overlapping
  provider lookback refreshes without permitting tenant/date/dimension rewrites;
- `pages`, `opportunities`, `tasks`, `agent_runs`, `experiments`, `job_runs`,
  and `job_leases` receive only the insert/update operations used by current
  scheduler paths;
- `execution_leases` receives `SELECT, INSERT, DELETE` for its non-expiring
  external-write reconciliation protocol;
- `sites.coordination_token` alone is updateable so PostgreSQL can take a site
  row lock without granting any site policy/configuration update;
- `mission_states.available_resources_json`, `blockers_json`, and `updated_at`
  alone are updateable for scheduled source-status refreshes;
- currently unused planning/relationship tables remain read-only.

Every canonical table must appear in exactly one worker capability group. A
future migration that adds or removes a table without updating that inventory
stops owner grant provisioning and worker startup. Neither role receives object
ownership, role membership, schema/database creation, temporary-table,
sequence, function, trigger, reference, truncate, or trigger-disable authority.
Historical table and column grants are revoked before exact grants are applied.
Role and current-database role defaults are cleared before the sole pinned
`search_path` default is installed. Runtime readiness also requires
`session_replication_role=origin`, so an owner-authored stale default cannot
silently disable ordinary foreign-key and immutable-audit triggers. Explicit
access to relations, sequences, or routines in every other non-system schema is
revoked and rejected; qualified object names cannot bypass this boundary.
Role-specific ACL entries on system schemas, relations, columns, or routines are
also rejected, while PostgreSQL's ordinary `PUBLIC` catalogue access is left
intact.

Database privileges are cluster-scoped. Readiness rejects a runtime role that
can `CONNECT` to or create temporary objects in any other connectable database,
including through `PUBLIC`. The packaged Compose initialization revokes ambient
`PUBLIC CONNECT,TEMPORARY` from every database in its dedicated cluster, after
which each runtime role receives explicit `CONNECT` only to the application
database. A shared PostgreSQL cluster is therefore not supported.

## Startup and migration contract

Only the owner-run migration process may run Alembic or grant roles. After each
migration it atomically provisions distinct `POSTGRES_API_*` and
`POSTGRES_WORKER_*` roles. API startup verifies the exact API profile; worker
startup and health verify the exact worker profile. Supplying API credentials to
the worker fails because that login has forbidden extra privileges. Supplying
worker credentials to the API fails because required API privileges are absent.

The worker also receives no API, reviewer or administrator bearer token. A
model/provider payload cannot change the selected executable role or database
profile.

Production connection construction accepts only the supported psycopg dialect,
an explicit username/database, an explicit host, and the reviewed `sslmode`/
`gssencmode` query keys. Remote hosts require `sslmode=verify-full`; GSS
substitution, ambient libpq overrides, nested `conninfo`, psycopg wrapper
parameters, SQLAlchemy plugins, custom pools/factories and all caller-supplied
engine kwargs fail before driver connection.

Every production owner migration also requires an immutable release-image
selector and independently recorded database name, PostgreSQL system identifier
and predecessor Alembic head. The exact DDL connection pins `search_path` to
`public`, rechecks those identities, acquires a nonblocking advisory transaction
lock, and applies migrations plus runtime grants in one transaction. Production
offline Alembic is unsupported because it cannot observe the live target.
Direct ASGI launch repeats the exact API-role check with no cache at startup;
an admission latch blocks requests when a server disables lifespan handling.

## Residual limitations

This is process-capability separation, not tenant row isolation. Each runtime
role can read all sites in the dedicated database, and a compromised API process
still has its documented authority-writing capability. The worker retains broad
ability to append evidence and audit records and to modify the operational
tables listed above; deterministic validation, leases, append-only triggers and
action guardrails remain necessary. The database owner can bypass runtime ACLs
and must never be present in API or worker environments. Provisioning also
removes `PUBLIC` object access across non-system schemas, so both the database
and PostgreSQL cluster must remain dedicated to the application rather than
sharing unrelated workloads. The Compose init hook runs only for a fresh data
volume; an existing cluster must have its database ACLs isolated by an
authorized owner before the next migration/startup check.

PostgreSQL/container execution is required to prove actual ACL behavior. On a
machine without the disposable PostgreSQL gate, static/unit checks do not
substitute for that verification.
