# Deployment

This repository runs the control API, a recurring backend worker, and PostgreSQL. A labelled `example.test` demo is available without provider credentials. Real sites start at Level 1 in shadow mode; registration does not crawl, call a model, or modify a website.

The current account/hosting freeze uses the prepared [durable verification package](DURABLE_DEPLOYMENT_PACKAGE.md): an immutable-image overlay, hard-zero action/spend settings, API-first startup, schema-head preflight, and non-overwriting backup/recovery procedures. It does not provision an external host. The general examples below are not authorization to leave the current verification envelope.

## Local offline demonstration

Use Python 3.12 from the repository directory:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --requirement requirements.lock.txt
.venv/bin/python scripts/init_env.py
.venv/bin/python scripts/bootstrap.py --demo
.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Open [the local dashboard](http://127.0.0.1:8000). Use the generated API token from the private `.env` file. The initializer creates six independent random credentials with mode `0600` and never displays them. If `.env` already exists, it preserves every byte; it does not fill missing keys or rotate credentials.

The demo applies Alembic migrations to the configured SQLite database, registers **Offline demo — example.test**, and runs one fixture cycle. Re-running `--demo` returns the same site and cycle; it preserves observations and audit history. Fixture sessions, clicks, and findings cannot establish real business outcomes. The demo always uses fixture agents even if the surrounding configuration selects a live model.

The independent worker can run in another terminal:

```sh
.venv/bin/python -m backend.app.scheduler --describe
.venv/bin/python -m backend.app.scheduler
```

SQLite is for local development and tests. Production configuration requires PostgreSQL.

## Container deployment

Use Docker Engine and the Compose v2 plugin. These commands run the stack on the current machine; they do not provision a remote host, DNS, TLS, or an OAuth issuer.

```sh
python3 scripts/init_env.py
docker compose config --quiet
docker compose build
docker compose up -d --wait db api worker
docker compose run --rm api bootstrap --demo
```

The API binds to `127.0.0.1:8000`; PostgreSQL has no published host port. The database persists in the `spiral-max-seo_seo-postgres` volume. API and worker processes run as UID/GID `10001`, with read-only filesystems, temporary `/tmp` storage, dropped Linux capabilities, and `no-new-privileges`. The image installs the exact Python versions in `requirements.lock.txt`.

Compose waits for PostgreSQL health and successful completion of the one-shot migration service before starting the API or worker. This uses the documented [Compose dependency conditions](https://docs.docker.com/compose/how-tos/startup-order/). The API readiness check requires the exact migration head shipped with the image. The worker health check verifies that its scheduler heartbeat is recent; review job records separately for successful observations.

Production `bootstrap --demo` and `bootstrap --domain` use the restricted API login and verify the current schema and API role before site setup. They do not run migrations. Apply schema changes separately through the guarded owner migration path; development/test bootstrap still applies local migrations automatically.

The verification overlay selects every application service with the same
immutable `SEO_RELEASE_IMAGE` digest and injects that selector into `migrate`.
Before constructing the owner database URL or invoking Alembic, the guarded
entrypoint rejects missing, mutable-tag, and malformed selectors. The later API
preflight rechecks the pin; neither check replaces obtaining the digest from a
trusted release process.

The `.env` `DATABASE_URL` is the local-development URL. Inside Compose, the entrypoint constructs an escaped PostgreSQL URL from each service's assigned `POSTGRES_*` values, including passwords containing reserved URL characters. A production PostgreSQL host outside the explicit local/Compose host set must use `sslmode=verify-full`; configuration, engine construction, bootstrap/migration, direct Alembic and entrypoint paths reject weaker remote transport before opening a connection. The transport gate adds `gssencmode=disable` when absent and rejects conflicting values, ensuring libpq cannot prefer GSSAPI transport over the stated TLS contract. For a discrete `POSTGRES_*` configuration, set `POSTGRES_SSLMODE=verify-full`; for a managed `DATABASE_URL`, include the same query parameter and use the provider's trusted CA/hostname.

## Database roles and capabilities

| Process | Database login | Capability |
| --- | --- | --- |
| PostgreSQL initialization and `migrate` | `POSTGRES_USER` / `POSTGRES_PASSWORD` | Own schema, apply migrations, create/grant both runtime roles |
| API | `POSTGRES_API_USER` / `POSTGRES_API_PASSWORD` | Authenticated API reads and documented configuration/review writes |
| Scheduler worker | `POSTGRES_WORKER_USER` / `POSTGRES_WORKER_PASSWORD` | Explicit operational table writes; authority fields and verdict tables are read-only |
| Optional MCP adapter | No database login | Fixed semantic requests to the API using its application token |

The owner credentials are not included in API, worker, or MCP environments. Neither runtime role has role memberships, superuser/role/database creation powers, replication or RLS-bypass powers, object ownership, schema creation, temporary tables, `TRIGGER`, `REFERENCES`, or `TRUNCATE` grants. The worker cannot insert `approvals` or `verifications` or update site authority. It receives only a semantically inert site coordination column plus three operational mission-status columns for updates. GSC and GA4 refreshes have column-scoped update grants for observation values only; their tenant/date/dimension keys are not updateable. The migration version table is strictly read-only. The application-dedicated database also removes conflicting `PUBLIC` and historical table/column grants. Every worker table must be explicitly classified, so an unreviewed future table makes provisioning/startup fail closed. See [the exact capability boundary](DATABASE_CAPABILITY_BOUNDARY.md).

The API and worker verify their distinct exact profile at startup and stop if those conditions fail. In production, API `/readyz`, worker health, and each scheduled tick repeat the applicable schema/profile checks, so post-start privilege drift fails closed. The verifier uses bounded set-based catalogue queries rather than one network round trip per table/column. API schema checks run on every probe; successful privilege checks are cached for at most 30 seconds behind a stampede lock, while worker tick checks remain uncached. The migration script rejects an existing role with inherited, administrative, membership, ownership, direct system-object ACL, or other-database access. A dedicated PostgreSQL cluster and three distinct roles are required. The fresh Compose volume runs `docker/initdb/010-dedicated-cluster.sql`; existing volumes must be isolated by an authorized owner because init hooks do not rerun. PostgreSQL distinguishes table grants from ownership and schema privileges; see [PostgreSQL 17 GRANT](https://www.postgresql.org/docs/17/sql-grant.html).

This split intentionally removes the legacy `POSTGRES_APP_*` configuration.
Because `scripts/init_env.py` never edits an existing `.env`, an upgrade must add
new, independently generated API and worker database credentials through the
operator's secret manager before running the owner migration. Missing or reused
credentials stop startup; they are never derived from the old runtime password.

## Register a real site

Replace the URL and name with the site you control:

```sh
docker compose run --rm api bootstrap --domain https://www.your-business.com --name "Your business"
```

`--domain` requires an explicit `--name` and a public HTTPS origin. It creates a Level 1 shadow registration with unverified conversion semantics and integration blockers. It performs no ingestion, model calls, or website writes. Repeating it preserves the existing registration and its authority. It cannot convert the reserved fixture into a live site.

Configure only the providers needed for that site, then recreate API and worker to load environment changes:

| Setting | Meaning |
| --- | --- |
| `GSC_PROPERTY` | Owner-authorized Search Console property |
| `GA4_PROPERTY_ID` | Owner-authorized GA4 property ID |
| `GOOGLE_APPLICATION_CREDENTIALS` | Container path to the scoped, read-only Google credential file |
| `WORDPRESS_URL`, `WORDPRESS_USERNAME`, `WORDPRESS_APPLICATION_PASSWORD` | Site-bound CMS connection; execution additionally requires its atomic revision contract |
| `AGENT_MODE=openai`, `OPENAI_MODEL`, `OPENAI_API_KEY` | Explicit selection of live model reasoning |
| `MAX_AGENT_CALLS_PER_RUN`, `MAX_DAILY_COST_USD` | Core per-task model admission and daily cost bounds; SDK calls additionally use one turn each |
| `MAX_CRAWL_PAGES`, `MAX_PAGES_PER_CRAWL`, `CRAWL_MAX_BYTES` | Collection bounds; midday crawl also caps itself at 50 pages |

Google credential files need explicit read-only bind mounts for both API and worker; setting a path alone does not mount a file. For example, save an operator-owned `compose.providers.yml`:

```yaml
services:
  api:
    volumes:
      - type: bind
        source: ./secrets/google-credentials.json
        target: /run/secrets/google-credentials.json
        read_only: true
        bind:
          create_host_path: false
  worker:
    volumes:
      - type: bind
        source: ./secrets/google-credentials.json
        target: /run/secrets/google-credentials.json
        read_only: true
        bind:
          create_host_path: false
```

Give the container UID read access to that file, set `GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/google-credentials.json`, and include the override when running Compose:

```sh
docker compose -f docker-compose.yml -f compose.providers.yml up -d api worker
```

Live model admission also requires trusted site configuration `model_price_bound` containing the exact configured `model`, a positive conservative `usd_per_million_tokens` covering both input and output, `verified: true`, and an official pricing `source` URL. Use the administrator-only `PUT /api/sites/{site_id}/model-price-bound` endpoint with those four fields. No default price is assumed. Missing, mismatched, unknown, or exhausted cost authority stops model calls; crashed calls retain their reservation. Provider dashboards remain the source for actual billing.

The `API_TOKEN`, `APPROVAL_TOKEN`, and `ADMIN_TOKEN` are distinct capabilities. Application/model tooling receives the application capability. Human review and exact-revision approval use the reviewer capability; site configuration uses the administrator capability. Keep `PRODUCTION_ENABLED=false` and `SHADOW_MODE=true` for observation-only operation. A scheduled cycle can execute only exact revisions accepted by the core execution, verification, approval, scope, rate-limit, and CMS concurrency guards. No scheduler job raises autonomy.

## Optional remote MCP profile

The MCP adapter supports stdio and an optional authenticated streamable-HTTP container. The remote profile needs an external OAuth authorization server; the adapter does not implement user login, client registration, or token issuance.

Set these values in the private configuration after the issuer and HTTPS ingress exist:

| Setting | Required value |
| --- | --- |
| `MCP_PUBLIC_URL` | Exact public HTTPS MCP resource URL, including `/mcp` |
| `MCP_OAUTH_ISSUER` | Trusted issuer's HTTPS URL |
| `MCP_ALLOWED_SUBJECTS` | Explicit comma-separated issuer subject allowlist |
| `MCP_OAUTH_PUBLIC_KEY_HOST_FILE` | Existing readable PEM public verification key; never a private signing key |

The issuer must mint an accepted signed JWT with `iss`, `aud`, `sub`, `iat`, `exp`, the configured audience, and appropriate scopes. Read access requires `seo:read`; proposal operations require `seo:propose`; execution forwarding additionally requires `seo:execute` and all backend guards. The internal API bearer token is not a public OAuth token.

```sh
docker compose --profile mcp up -d mcp
```

The adapter binds to host loopback on port `8001`; route public HTTPS through an operator-managed reverse proxy. Its public-key bind mount refuses a missing source file. The MCP container receives neither reviewer/admin credentials nor database credentials. Keep the API behind trusted ingress and configure `ALLOWED_HOSTS` to include its exact expected hosts and the internal health/MCP hosts.

## Verification gates

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

The GitHub Actions workflow adds PostgreSQL 17.11 as an actual service, exports
`TEST_POSTGRES_URL` and the exact service container ID as
`TEST_POSTGRES_CONTAINER`, applies/checks migrations, runs the complete suite,
verifies the restricted runtime login against real PostgreSQL, builds the
nonroot image, starts the fixture stack, and bootstraps the demo twice. Without
`TEST_POSTGRES_URL`, PostgreSQL-specific tests are explicitly skipped; without
`TEST_POSTGRES_CONTAINER`, the real dump/restore gate is also skipped. SQLite
passing does not claim PostgreSQL or container validation. A historical CI run
attests only its exact commit; later local changes require a fresh gate.

The workflow also installs pinned Playwright 1.62.1, checks the desktop/mobile dashboard and retains screenshots. This browser gate was not runnable locally because the browser binary download failed. It does not block using the control API, but must pass before claiming visual acceptance.

OpenTelemetry spans export only correlation metadata to structured logs; request URLs, headers, bodies, exception messages and model content are omitted. Remote OpenTelemetry export is not activated by this release. OpenAI SDK trace export is separately disabled by default and excludes sensitive data if explicitly enabled.
