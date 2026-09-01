# Canonical data model

SQLAlchemy models are in `backend/app/db/models/__init__.py`. The initial Alembic revision freezes the same explicit schema. All tenant records have `site_id`; composite foreign keys prevent a referenced page, evidence/action relation or experiment from silently crossing sites. UTC timestamps reject naive datetimes. IDs are UUID strings.

| Group | Tables and purpose |
| --- | --- |
| Inventory | `sites`, `pages`, `page_versions`, `page_entities`, `queries`, `query_clusters` |
| Observations | `gsc_daily`, `ga4_daily`, `serp_snapshots`, `ai_search_snapshots`, `crawl_snapshots`, `crawl_issues` |
| Work | `opportunities`, `tasks`, `task_dependencies`, `task_ownership`, `agent_runs`, `agent_findings` |
| Knowledge | `claims`, `evidence`, `assumptions`, `contradictions`, `decision_logs` |
| Change control | `revisions`, `verifications`, `approvals`, `actions`, `action_events`, `action_batches`, `execution_leases`, `rollback_events` |
| Learning | `experiments`, `experiment_metrics`, `calibration_records`, `failure_cases` |
| Governance | `mission_states`, `policies`, `guardrails`, `strategy_versions` |
| Scheduling | `job_runs`, `job_leases` |

The model inventory is broader than initial automated population: entity relationships and some strategy/ownership registries are schema-supported and available for future bounded services. Do not assume an empty registry establishes absence of entities or dependencies.

## Evidence and claims

Evidence carries source/type, content/hash, observation time, confidence, owner, status and fixture provenance. A claim separately carries its epistemic type, source, evidence IDs, confidence, owner, status, contradictions, alternative explanations and supersession link. A source ID is provenance, not proof or independence.

GSC query rows and page-total rows have different aggregation meaning; an empty query denotes page totals. GA4 rows are organic landing-page/date observations; sessions are collected separately from qualified event outcomes. Unknown qualified conversion/value fields are nullable. Privacy, sampling, missing dates, unsettled reports and timezone flags travel with observations.

## Immutable change chain

A revision binds before/after JSON, their derived changes, the before fingerprint, revision hash, evidence, experiment, proposer and reason. Verification and approval bind the exact revision hash. An Action records immutable intent; appended ActionEvents describe requested, blocked, dispatching, succeeded and reconciliation states. PageVersions preserve exact snapshots. No mutation tool accepts arbitrary SQL or table names.

Execution freezes an `experiment_prediction` Evidence record before dispatch, including UNKNOWN when no legitimate forecast was supplied. The dispatch event binds the prediction ID/hash. Measurement checks the immutable prediction, observed data, definition hash and immutable measurement event before independent adjudication can produce calibration. Editable summaries are never sufficient calibration authority.

## Audit protection and retention

SQLite triggers cover updates/deletes and replacement semantics; PostgreSQL additionally blocks truncation. Deployment grants allow the application runtime only SELECT/INSERT on append-only tables. The migration owner remains separate. Backups include both mutable state and audit history; retain them according to the site's privacy/retention policy. Do not silently delete evidence because an outcome became inconvenient.
