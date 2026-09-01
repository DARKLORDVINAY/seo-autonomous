# Spiral Max SEO

A persistent, evidence-led SEO control loop optimising **incremental qualified organic conversion value**. Python 3.12, FastAPI, SQLAlchemy/Alembic, PostgreSQL, OpenAI Agents SDK, semantic MCP tools and a lightweight Mission Control dashboard.

**Current release: Level 1, shadow by default.** The local fixture loop works without credentials. Live provider adapters and guarded execution are implemented; real-site effectiveness, production PostgreSQL and deployed infrastructure require their activation gates. A passing synthetic test is not evidence of SEO uplift.

## Run the offline workspace

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python scripts/init_env.py
.venv/bin/python scripts/bootstrap.py --demo
.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Open [Mission Control](http://127.0.0.1:8000). Enter the operator `API_TOKEN` from the private generated `.env`. Tokens are not printed, committed, or stored by the dashboard. The demo registers `https://example.test`, migrates SQLite, and runs one idempotent fixture cycle. Qualified outcomes remain unknown because synthetic traffic does not establish business qualification.

Run `.venv/bin/python -m backend.app.scheduler --describe` to inspect the authoritative backend schedule. The default cadence is 05:00 ingestion/control loop, 12:00 integrity, 19:00 measurement, Monday 06:00 strategy review, in the configured timezone. Run the same module without `--describe` to start it. This shell/session is not a durable hosting service; use the delivered Compose deployment for continuous operation.

## What runs

| Component | Behaviour |
| --- | --- |
| Canonical state | 42 relational tables; site-scoped foreign keys; immutable evidence, revision and audit records; SQL migrations |
| Observations | Robots/sitemap crawl, CMS snapshots, GSC query and page totals, organic GA4 traffic and explicitly qualified outcomes |
| Diagnosis | Deterministic decay, CTR, cannibalisation, links, metadata and technical detectors; scored evidence-backed opportunities |
| Interpretation | Up to three specialists, an evidence-first blind review, then a sceptical verifier; strict output packets and durable cost reservations |
| Proposal | Human-authored scoped revisions and one bounded, grounded model title proposal when supported; every substantive revision has an experiment |
| Execution | Exact immutable revisions; independent verification, approval, source quality, policy, daily budget, current-state and atomic CMS checks |
| Recovery | Before/after page versions, exact inverse proposals, fresh rollback approval, idempotent replay, no blind retry after ambiguous writes |
| Learning | 7/14/28/56-day evaluations, explicit confounders, immutable predeployment forecasts, independently adjudicated calibration and downward-only permission changes |
| MCP | Fixed semantic tools; no SQL, shell, arbitrary HTML, approval or authority-escalation tool |
| Mission Control | Overview, opportunities, pages, queries, technical, SERPs, AI search, experiments, actions, agents, failures, strategy and approval history |

SERP and AI Mode clients are implemented and tested with mocked responses. Their paid collection is opt-in and is **not scheduled automatically** in this release. AI citation absence is unknown without a compatible observation. GitHub integration is read-only; no repository is silently repurposed or published.

The built-in WordPress adapter deliberately blocks existing-page production updates because core WordPress REST does not supply the atomic compare-and-swap contract this executor requires. CMS drafts have a separate guarded path. Do not disable the concurrency guard to make a demonstration pass; use a verified site-side adapter/extension before production updates.

## Verify

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/python -m alembic check
```

See `docs/VERIFICATION.md` for the recorded checkpoint, exact gates and known limitations. GitHub Actions adds PostgreSQL, migration drift checking, restricted runtime-role checks and Compose startup. Two actual-PostgreSQL tests skip explicitly when `TEST_POSTGRES_URL` is missing. A workflow file existing is not evidence it has run.

## Connect a real site

1. Choose a site you control and a deployment/GitHub destination. Follow `docs/DEPLOYMENT.md` to run PostgreSQL and the restricted application role.
2. Register the real origin with `scripts/bootstrap.py --domain https://your-site.example --name "Your business"`.
3. Supply scoped Google read-only credentials and a WordPress application password through the host's secret mechanism. No password belongs in chat or Git.
4. Define qualified conversions, value semantics and brand facts through the administrator-only routes in `docs/SITE_CONFIGURATION.md`. Traffic and key events alone are insufficient.
5. Select a live model and record its conservative price bound. Keep shadow mode on while comparing recommendations with human review.
6. Prove live backups, restoration, CMS concurrency, scoped credentials, source quality and independent outcomes before considering production authority. No automatic graduation is implemented.

## Repository map

`backend/app` holds the API, canonical models, integrations, deterministic analysis, agents, guardrails, executor, measurement and scheduler. `seo_mcp` is the importable MCP package; `mcp/server/main.py` is a compatibility entrypoint. `dashboard/app` contains dependency-free static assets served by FastAPI. `scripts` contains explicit bootstrap and privilege tools; `docker` and `docker-compose.yml` package the runtime.

Read [architecture](docs/ARCHITECTURE.md), [mission](docs/MISSION.md), [autonomy policy](docs/AUTONOMY_POLICY.md), [SEO policy](docs/SEO_POLICY.md), [threat model](docs/THREAT_MODEL.md), [agent contracts](docs/AGENT_CONTRACTS.md), [data model](docs/DATA_MODEL.md), [runbook](docs/RUNBOOK.md), [failure modes](docs/FAILURE_MODES.md), and [current mission state](docs/MISSION_STATE.json).
