# Durable deployment package: prepared, not provisioned

Status: the current user instruction freezes all account, credential, hosting,
and production activity. This document is an operator runbook for a later,
explicitly authorized deployment. No durable host was created by this audit.

The existing PostgreSQL + FastAPI + worker Compose architecture is retained.
Cloudflare Pages hosts the public static Test Lab; it does not replace the
persistent control-plane database or authoritative scheduler.

## Hard verification envelope

Use the base file with `docker-compose.verification.yml`. The overlay fixes
Level 1, production disabled, shadow enabled, fixture providers, and zero action
and model-spend budgets. It removes provider credentials from these services.
`VERIFICATION_ONLY=true` independently rejects conflicting settings at startup.
The worker is profile-gated and has no restart policy; the API contains no
embedded authoritative scheduler.

The profile is a deployment convenience, not an authorization boundary:
explicitly naming a service can start it. The owner must approve recurrence
separately. See [Docker's profile semantics](https://docs.docker.com/compose/how-tos/profiles/).

The service image is also an evidence boundary. It contains the backend, MCP,
dashboard, container entry point and only `bootstrap.py`, `grant_runtime.py` and
`deployment_preflight.py`. The Docker build context and resulting image exclude
benchmark corpora, evaluator truth, Test Lab source manifests, detailed reports,
rollback/operator tools and the blind-exchange CLI. CI inspects the actual image,
not only the Dockerfile. Do not broaden these copies in a provider-specific
deployment. See `BLIND_EVALUATION_PROTOCOL.md`.

An optional benchmark attestation import needs a separately agreed evaluator
key ID, the matching **public** Ed25519 key mounted read-only, and the expected
benchmark-definition/source-release SHA-256 values pinned in environment
configuration. Do not place an evaluator private key, truth corpus or detailed
report on the host. With incomplete pins, the administrator-only import route
fails closed; the scheduler, agents and MCP cannot invoke it.

The base stack does not mount evaluator material. To enable aggregate import,
set `BENCHMARK_EVALUATOR_PUBLIC_KEY_HOST_FILE` to the reviewed public key and
start the API with the optional `docker-compose.benchmark-attestation.yml`
overlay. It bind-mounts that public key read-only into the API only; the worker
does not receive the key, benchmark-import settings, operator token, reviewer
token or administrator token.

`SEO_RELEASE_IMAGE` must identify an already-reviewed image by immutable
`repository@sha256:...` or local `sha256:...` image ID. Pulling is disabled in the
overlay. Build/retrieve that image through a reviewed supply-chain process;
record its digest, source commit, dependency lock hash, current schema heads,
previous compatible image digest, and backup receipt before release. A floating
base-image tag is not a reproducible release pin. The overlay passes the exact
Compose image selector into the one-shot migration container. Its guarded
entrypoint validates the digest syntax before constructing an owner database URL
or creating an engine/running migration code; a missing, mutable or malformed selector
therefore fails closed before any database operation. API preflight validates
the same pin again. This in-image check does not independently attest a hostile
image, so the operator must still obtain and review the digest through the
trusted release process before invoking Compose.

## Provider capability checklist

| Requirement | Acceptance condition |
| --- | --- |
| PostgreSQL | Application-dedicated cluster with other-database CONNECT/TEMP denied; tested baseline PostgreSQL 17; durable disk and owner-controlled recovery |
| Migration identity | Can own/create the public application schema, install triggers, and grant/revoke both restricted runtime roles |
| API identity | Distinct login; no administrative/inherited/ownership authority; passes the exact `api` profile |
| Worker identity | Distinct login; cannot update site authority or insert approval/verification; passes the exact `worker` profile |
| Connectivity | Private network; verified TLS and CA/hostname checking for a managed remote database |
| Container host | Nonroot image, read-only root filesystem, `/tmp`, graceful shutdown, health checks, restart controls |
| Secrets | Existing operator-managed store; app/reviewer/admin and owner/API/worker credentials distinct; migration owner absent from runtime |
| Ingress | Trusted HTTPS reverse proxy, exact allowed hosts, API private by default; OAuth issuer needed only for remote MCP |
| Recovery | Encrypted off-host backup, isolated restore drill, explicit RPO/RTO and retention choices |

A provider unable to support the required database roles/grants is a deployment
blocker. Do not weaken privileges to accommodate it. Managed providers with
injected roles, extensions, or a non-public schema need independent validation.

Two database configurations are distinct:

- **Bundled Compose:** `POSTGRES_HOST=db`; entrypoint constructs an escaped URL
  from `POSTGRES_DB`, `POSTGRES_PORT`, and the service's assigned login.
- **Managed PostgreSQL:** omit `POSTGRES_HOST` and supply a verified-TLS
  `DATABASE_URL` ending in `sslmode=verify-full` through the secret store. The
  shared transport gate runs before engine/driver connection in production,
  including application, bootstrap/migration, direct Alembic and container
  entrypoint paths. It adds `gssencmode=disable` when absent and rejects a
  conflicting value so libpq cannot substitute GSSAPI transport for TLS. A
  discrete remote `POSTGRES_*` configuration instead needs
  `POSTGRES_SSLMODE=verify-full`. Use a reviewed provider-specific
  service definition; the bundled Compose file is not a drop-in managed-DB
  override. Migration still requires separate `POSTGRES_USER`/
  `POSTGRES_PASSWORD`, `POSTGRES_API_USER`/`POSTGRES_API_PASSWORD`, and
  `POSTGRES_WORKER_USER`/`POSTGRES_WORKER_PASSWORD` settings for the owner/API/
  worker separation and exact grant checks. Never send these values in chat.

No provider resources, account choice, credentials, TLS issuer, backup storage,
or production ingress are assumed configured by this package.

## Startup and upgrade gates

Before exposing owner credentials to the migration container, set the following
from the independently reviewed release/target record: `SEO_RELEASE_IMAGE`,
`SEO_MIGRATION_EXPECTED_DATABASE`,
`SEO_MIGRATION_EXPECTED_SYSTEM_IDENTIFIER`, `SEO_MIGRATION_MODE`, and
`SEO_MIGRATION_EXPECTED_SCHEMA_HEADS`. Use `bootstrap` plus `uninitialized` only
for a newly created empty `public` schema; use `upgrade` plus the exact sorted
predecessor head list otherwise. Missing, inferred-at-runtime, mismatched, or
mutable pins stop before DDL. The disposable CI derives pins mechanically only
to test this gate; that is not production provenance.

After authorization, the operator uses the guarded container entrypoint, not
direct production `uvicorn`, so runtime role checks cannot be skipped:

```sh
docker compose -f docker-compose.yml -f docker-compose.verification.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.verification.yml up -d --wait db
docker compose -f docker-compose.yml -f docker-compose.verification.yml run --rm migrate
docker compose -f docker-compose.yml -f docker-compose.verification.yml run --rm api preflight
docker compose -f docker-compose.yml -f docker-compose.verification.yml up -d --wait api
```

Configuration validation must not print interpolated secrets. Migration is a
one-shot owner process; API/worker depend on database health and migration
completion. In the verification overlay, migration first checks the immutable
`SEO_RELEASE_IMAGE` selector injected by Compose; rejection occurs before owner
database URL construction or migration invocation. It then reads the target in
a read-only transaction and repeats database/system/head checks on the exact
DDL connection after pinning `search_path=public` and taking a nonblocking
advisory lock. Migration and both runtime-role grants commit atomically.
Preflight checks restricted role, exact Alembic head, canonical
Level-1 authority, and the immutable image pin. `/healthz` is process liveness;
`/readyz` now rejects missing, stale, or future migration revisions and, in
production, a runtime-role privilege-policy regression. Schema is checked on
every probe; a stampede lock bounds the set-based privilege check to one per
30-second success window (and briefly caches failures). Worker ticks remain
uncached. Neither is
proof that Google evidence is fresh or a scheduled observation succeeded.

Start the API for review first. Leave the worker stopped until host-specific
checks, source scopes, source mode, job budgets, backups, and human approval are
complete. For a later approved fixture recurrence only, the exact command is:

```sh
docker compose -f docker-compose.yml -f docker-compose.verification.yml --profile verified-scheduler up -d --wait worker
```

This does not enable live providers or production writes. A different live
read-only deployment needs its own reviewed configuration, not a flag silently
removed from this verification package.

## Atomic, non-overwriting backup bundles

The volume is persistence, not disaster recovery. The prepared utility requires
PostgreSQL client tools and an existing private libpq service. It receives no
password or credential-bearing DSN argument. Before using it, independently
record the expected database name, TLS hostname, numeric endpoint,
`pg_control_system().system_identifier`, Alembic head(s), immutable release
image, full Git commit, deployed runtime identity and canonical checkpoint
identity. The following values are illustrative and must be replaced with pins
from the release record:

```sh
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

Child PostgreSQL processes receive a minimal environment: executable/home paths,
explicit service/passfile/system-service-file paths, optional system certificate
paths, fixed C locale/UTC, and fixed TLS defaults. Ambient DSNs, `PGOPTIONS`,
bearer tokens and unrelated process secrets are not inherited. Every `psql` and
`pg_dump` connection also receives a bounded, non-secret conninfo override after
the service name: the exact database, certificate hostname, numeric address and
port, `sslmode=verify-full`, and `gssencmode=disable`. Direct conninfo values
override weaker or multi-host service-file values. The passfile/service file
still supplies the login and credential without placing either password in a
command argument or environment variable.

The operator stops writers first and confirms that fact; the script does not
stop or restart anything. Before and after dumping, it checks the connected
database, numeric endpoint, cluster system identifier and Alembic heads against
the independent pins and requires the complete source observations to match. The
dump uses the same exact target override. It records both source-observation
hashes and the PostgreSQL server/client versions. It writes the
archive and receipt inside one hidden private staging directory, fsyncs both,
and atomically renames the directory to a unique `.backup` bundle. An existing
bundle is never replaced. A failure before rename leaves only the hidden staging
directory for private diagnosis; a failure after rename can leave only a complete
two-member bundle, never one published member. Keep writers stopped and run the
verifier before deciding whether such a bundle is usable.

Any `pg_dump` stderr output, including a warning with exit status zero, blocks
promotion. Archive-list stderr does too. The receipt binds the libpq service,
database, cluster system identifier, address/port, server and client versions,
schema heads, immutable image, source commit, runtime/checkpoint identities,
dump start/end and archive-validation/receipt timestamps. Receipt schema v3 also
records the exact TLS/GSS policy and equal pre/post source hashes. It records
`warnings_present=false` and explicitly says `restore_verified=false`: listing
an archive is not a restore. Before running the verifier, preserve the emitted
archive SHA-256 and all expected source/release/checkpoint pins in an independently
controlled release record. The verifier requires those pins on its command line;
validly shaped but wrong identities fail. It also requires exactly the two
declared members, bounds and orders UTC timestamps, rechecks private modes, size,
SHA-256 and archive listing:

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

The pre/post checks and exact address remove service-file failover between
unrelated endpoints. They are not a cryptographic proof of which backend a
same-address database proxy selected during the middle connection. The bundle
remains an operator-authored assertion plus archive-integrity record, not an
authenticated provenance signature; retain the independent release record and
complete an isolated restore before recovery use.

The custom archive preserves schema and data; global roles need separate
operator-managed recovery. Restore only archives from a trusted source because
restoration executes database code. These are documented properties of
[PostgreSQL pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html).

Copy the complete successful bundle to encrypted off-host storage with
operator-selected retention. Where the storage system cannot atomically publish
a directory, upload the archive first and the receipt last as the commit marker;
never inventory an archive without its verified receipt as a usable backup. Keep
secrets separate. Local fsync is not proof of off-host durability or that an
object-store copy is recoverable.

## Isolated restore and rollback

The destination must be a **new empty, isolated database**, never the active
database. Pin a compatible PostgreSQL client/server and trusted archive. After
verifying the recorded checksum, use `pg_restore --exit-on-error
--single-transaction --no-owner --no-acl` with the selected destination identity.
Reapply owner migrations and explicit runtime grants, then verify schema head,
all canonical table counts/logical row hashes, immutable triggers, restricted
role, provenance, and retained execution/cost leases. Review any uncertain
external action before resuming recurrence. The chosen flags and transaction
behavior are documented in [PostgreSQL pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html).

The real restore drill creates two uniquely named databases inside the existing
disposable PostgreSQL service, takes an actual custom archive, restores it,
compares every canonical table, and exercises owner-trigger/runtime-ACL
protection. It never restores over `seo_ci` or any user database. When that
service is unavailable, the test skips explicitly; local mocked commands do not
substitute for it. Running this gate requires both `TEST_POSTGRES_URL` and the
exact disposable container ID in `TEST_POSTGRES_CONTAINER`. Historical linked
CI evidence does not attest the current unpushed or otherwise changed local tree.
That disposable drill uses an explicitly test-only local-socket adapter; it
tests dump/restore fidelity, not the production verified-TLS transport boundary.

For application rollback, stop recurrence and select the recorded previous
immutable image **only if schema-compatible**. Readiness refuses a different
schema head. There is no automatic Alembic downgrade and no forced audit-history
rewrite. A schema reversal or data restore requires a separate reviewed
recovery decision. No rollback command in this package deletes a live volume.

## Exact remaining human gate

When the owner is available: explicitly end the verification-only freeze and
select/authorize an existing durable container/PostgreSQL target that meets this
checklist. Then securely connect the existing read-only Google properties;
do not recreate their accounts. The previously observed GA4 default-value
discrepancy and unverified event receipt also remain to be resolved deliberately.
Nothing in this package or benchmark qualifies Level 2.
