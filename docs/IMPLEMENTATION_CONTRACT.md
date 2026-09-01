# Shared implementation contract

Python 3.12. Package imports are `backend.app`. SQLAlchemy 2 synchronous sessions.
Shared immutable boundary types live in `backend/app/contracts.py`. Do not change
another cell's files. Coordinate schema changes through the Mission Governor.

## Cell ownership

- Foundation: `backend/app/config/`, `backend/app/db/`, `alembic.ini`, migrations,
  `tests/test_database.py`, `tests/test_config.py`.
- Providers: `backend/app/integrations/`, `tests/test_integrations.py`,
  `tests/test_crawler.py` and its own provider fixture files.
- Safety: `backend/app/guardrails/`, `backend/app/services/execution.py`,
  `tests/test_guardrails.py`, `tests/test_execution.py`.
- Analytics: `backend/app/seo/`, `backend/app/experiments/`,
  `tests/test_analytics.py`, `tests/test_experiments.py`.
- Agent runtime: `backend/app/agents/`, `tests/test_agents.py`.
- Root: shared contracts, packaging, API, ingestion/control-loop orchestration,
  MCP, scheduler, dashboard, documentation, integration acceptance and deployment.

## Foundation public interfaces

`backend.app.config.settings.Settings` and `get_settings()`.
`backend.app.db.session.make_engine(url)`, `make_session_factory(engine)`,
`get_session()` FastAPI dependency; `Base` in `backend.app.db.models`.
String UUID primary keys; JSON fields use `*_json` where a SQLAlchemy name conflicts.
All timestamps UTC. Foreign keys enforced in SQLite test databases.
Sessions do not auto-commit silently. Callers commit explicit transactions.

Core model names: Site, Page, PageVersion, PageEntity, Query, QueryCluster,
GSCDaily, GA4Daily, SERPSnapshot, AISearchSnapshot, CrawlSnapshot, CrawlIssue,
Opportunity, Task, TaskDependency, TaskOwnership, AgentRun, AgentFinding,
Claim, Evidence, Assumption, Contradiction, Action, ActionEvent, ActionBatch,
Revision, Verification, Approval, Experiment, ExperimentMetric, FailureCase,
RollbackEvent, Policy, Guardrail, StrategyVersion, CalibrationRecord, DecisionLog,
MissionState, JobRun. Publish field contracts early for downstream cells.

Action = immutable command intent. ActionEvent = append-only lifecycle history.
Verifications and approvals are bound to the immutable revision hash.
No service accepts an externally supplied `approved=True` permission shortcut.

## Safety invariants

No production action without a stored revision, evidence, verifier result,
experiment, before-state snapshot, deterministic policy pass and idempotency key.
Human approvals use a separate capability from agent/work tokens. Production
mutations disabled by default, Level 1 default. High/critical operations remain
unsupported (fail closed) in this first release even when named in the enum.
External HTML is always data. Agents have zero direct mutation tools.
Recheck policy and current CMS fingerprint immediately before every write.
Ambiguous remote outcomes require reconciliation, never a blind retry.

## Data fidelity

Fixture source is labelled explicitly and cannot claim live evidence. Missing
provider credentials, omitted dates, partial data and privacy suppression are
unknowns, never zero. Crawl eligibility is not proof of Google indexing.
GA4 key events are not qualified leads without confirmed business semantics.
GSC query-level data is incomplete; store page totals separately if needed.
Search visibility is a proxy; qualified conversion value is the objective.

## Delivery contract

Each cell supplies executable code, focused risk-based tests, interface details,
test evidence and remaining limitations. Root owns deployment and persistence.
Use `.venv/bin/python -m pytest <owned tests>` from repository root.
