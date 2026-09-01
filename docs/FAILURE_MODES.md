# Failure modes and response

| Scenario | Expected response |
| --- | --- |
| Fake traffic spike / conversions fall as traffic rises | Treat traffic as a proxy; preserve conversion damage and alternative explanations; do not declare success |
| Seasonality / demand / competitor volatility | Competing explanation review; compatible reference pages where feasible; observational uncertainty retained |
| Partial GSC or privacy omission | Distinguish missing periods from unavailable query population; no invented zero rows |
| GA4 tracking outage / unsettled / timezone disagreement | Qualified completeness blocked; evaluate as inconclusive |
| Wrong qualification or value mapping | Administrator definition required; hash mismatch invalidates comparability |
| Duplicate pages / canonical mistake | Deterministic issue and bounded proposal; canonical/deletion execution blocked |
| Hallucinated claim / fabricated title | Strict extractive validation and evidence IDs; no supported proposal or independent block |
| Hostile competitor instructions | Data only; no model tool or policy capability; adversarial tests exercise the boundary |
| API disagreement / insufficient power | Explicit uncertainty; never transform unknown into zero or negative evidence |
| CMS timeout or post-write DB failure | Preserve dispatch record and execution lease; no resend; reconcile exact remote state |
| Stale page / concurrent editor | Before-fingerprint and atomic-precondition rejection |
| Lost scheduler lease | Stale commit fenced out; ownership must be reacquired |
| Replayed model reservation | Immutable call binding; completion preserved; rejected start remains rejected |
| Rate limit / network error | Bounded safe-read retry; paid live POST and mutation are not blindly retried |
| Poor prediction calibration | Remove affected earned categories; preserve human approval requirement; no automatic promotion |
| Failed code deployment | Stop rollout, retain previous release/DB backup, diagnose with CI and health checks; no model deployment tool |

Every materially wrong prediction or harmful action should record predicted/actual outcome, magnitude where known, cause, incorrect assumption, missing evidence, responsible agent, detection method and preventive guardrail change. The initial runtime loads prior canonical failures before proposing similar actions. Some failure records legitimately say the root cause is unknown pending investigation.

Do not repeatedly run a failed paid cycle using a new key merely to obtain a preferred answer. Inspect durable reservations and unresolved external actions first. A timeout is not evidence that nothing happened. See `RUNBOOK.md` for reconciliation, backup and rollback procedures.
