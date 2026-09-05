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

`SEO_RELEASE_IMAGE` must identify an already-reviewed image by immutable
`repository@sha256:...` or local `sha256:...` image ID. Pulling is disabled in the
overlay. Build/retrieve that image through a reviewed supply-chain process;
record its digest, source commit, dependency lock hash, current schema heads,
previous compatible image digest, and backup receipt before release. A floating
base-image tag is not a reproducible release pin.

## Provider capability checklist

| Requirement | Acceptance condition |
| --- | --- |
| PostgreSQL | Dedicated database; tested baseline PostgreSQL 17; durable disk and owner-controlled recovery |
| Migration identity | Can own/create the public application schema, install triggers, and grant/revoke the restricted runtime role |
| Runtime identity | No administrative/inherited/ownership authority; passes `verify_runtime_role` |
| Connectivity | Private network; verified TLS and CA/hostname checking for a managed remote database |
| Container host | Nonroot image, read-only root filesystem, `/tmp`, graceful shutdown, health checks, restart controls |
| Secrets | Existing operator-managed store; app/reviewer/admin capabilities distinct; migration owner absent from runtime |
| Ingress | Trusted HTTPS reverse proxy, exact allowed hosts, API private by default; OAuth issuer needed only for remote MCP |
| Recovery | Encrypted off-host backup, isolated restore drill, explicit RPO/RTO and retention choices |

A provider unable to support the required database roles/grants is a deployment
blocker. Do not weaken privileges to accommodate it. Managed providers with
injected roles, extensions, or a non-public schema need independent validation.

Two database configurations are distinct:

- **Bundled Compose:** `POSTGRES_HOST=db`; entrypoint constructs an escaped URL
  from `POSTGRES_DB`, `POSTGRES_PORT`, and the service's assigned login.
- **Managed PostgreSQL:** omit `POSTGRES_HOST` and supply a verified-TLS
  `DATABASE_URL` through the secret store. Use a reviewed provider-specific
  service definition; the bundled Compose file is not a drop-in managed-DB
  override. Migration still requires separate `POSTGRES_USER`,
  `POSTGRES_PASSWORD`, `POSTGRES_APP_USER`, and `POSTGRES_APP_PASSWORD` settings
  for the existing separation/grant checks. Never send these values in chat.

No provider resources, account choice, credentials, TLS issuer, backup storage,
or production ingress are assumed configured by this package.

## Startup and upgrade gates

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
completion. Preflight checks restricted role, exact Alembic head, canonical
Level-1 authority, and the immutable image pin. `/healthz` is process liveness;
`/readyz` now rejects missing, stale, or future migration revisions. Neither is
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

## Non-overwriting backups

The volume is persistence, not disaster recovery. The prepared utility requires
PostgreSQL client tools and an existing private libpq service with an explicit
database identity. It receives no password or DSN argument:

```sh
python -m scripts.backup_database --service seo_checkpoint_source --output-directory /operator/private/seo-backups --writers-stopped
```

The operator stops writers first and confirms that fact; the script does not
stop or restart anything. It creates an exclusive private `.partial` file,
checks successful `pg_dump`, verifies archive listing, computes SHA-256, then
promotes to a unique `.dump` and receipt. Failure retains the partial artifact
and leaves writers stopped. It never overwrites an existing backup. A receipt
explicitly says `restore_verified=false`: listing an archive is not a restore.

The custom archive preserves schema and data; global roles need separate
operator-managed recovery. Restore only archives from a trusted source because
restoration executes database code. These are documented properties of
[PostgreSQL pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html).

Copy successful archives and receipts to encrypted off-host storage with
operator-selected retention. Keep secrets separate. Fsync of the file is not
proof of off-host durability or that an object-store copy is recoverable.

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

CI's new restore drill creates two uniquely named databases inside the existing
disposable PostgreSQL service, takes an actual custom archive, restores it,
compares every canonical table, and exercises owner-trigger/runtime-ACL
protection. It never restores over `seo_ci` or any user database. When that
service is unavailable, the test skips explicitly; local mocked commands do not
substitute for it.

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
