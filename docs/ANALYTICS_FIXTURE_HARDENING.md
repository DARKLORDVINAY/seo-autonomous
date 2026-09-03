# Analytics protocol and measurement hardening — 2026-09-03

This is an offline verification checkpoint, not a Google integration activation.
All external responses below come from `httpx.MockTransport`; all ingested
observations use `fixture:` provenance and temporary SQLite databases. No Google
sign-in, account modification, credential access, paid API, production database,
hosting change, or production mutation was performed by this workstream.

The public lab's Level 1 lock is unchanged. A passing scenario is evidence about
software behaviour, not a business outcome, causal effect, or permission to earn
Level 2.

## Verification scope and results

The new `tests/test_analytics_scenarios_v2.py` exercises actual GSC/GA4 parsing,
pagination and compatibility checks, then actual canonical ingestion,
`measurement._window`, and the experiment evaluator. Scenarios share one small
stateful protocol double; labels and expected results live in tests, not agent
inputs. They do not access the separate SEO holdout or its evaluator labels.

| Scenario family | Cases | Required safe result |
| --- | ---: | --- |
| Empty success versus explicit numeric zero, GSC and GA4 | 2 | Omission is unknown; explicitly returned zero remains zero |
| 403, 429 and 503 on each provider | 6 | No batch or invented row; 1 access-denied call or at most 3 retries |
| Repeated, contradictory and timed-out second pages, each provider | 6 | No partly accumulated batch escapes the failed fetch |
| Incomplete-date metadata contradicting final-only GSC requests | 3 | Reject before final-labelled rows reach ingestion |
| Incomplete-date metadata outside the requested GSC interval | 1 | Preserve metadata; no false partial-date classification |
| Invalid GSC counts/positions | 6 | Reject clicks above impressions, booleans and nonfinite values |
| GSC row cap, suppression and a seemingly large decline | 1 | Coverage stays incomplete; decline is not certified |
| London/Pacific calendar boundaries and both DST transitions | 6 | Use property calendar date, not UTC or fixed 24-hour guessing |
| Delayed qualification and a later attribution revision | 1 | Unknown → observed → revised; retain immutable earlier evidence |
| Page disappears from a newer otherwise successful report | 1 | Exclude its retained stale daily row from current measurement |
| Entire newer report disappears | 1 | Unknown, not zero or the previous positive outcome |
| Explicit zero refresh after a positive observation | 1 | Replace the materialized value with zero; preserve old evidence |
| Mutable daily values disagree with the selected snapshot | 3 | Fail closed with `api_disagreement` |
| One missing page in a prespecified group | 1 | Group primary outcome unknown, not a subtotal |
| Fixture/live provenance mismatch | 1 | Reject before any evidence/daily record is saved |
| Traffic doubles while qualified outcomes decline | 1 | Descriptive regression signal; no causal or rollback authority |
| Missing GA4 plus partial Search Console coverage | 1 | Missing/coverage flags remain explicit |
| **Total new cases** | **42** | **No production authority granted** |

Focused verification command:

```bash
.venv/bin/pytest -q \
  tests/test_analytics_scenarios_v2.py \
  tests/test_qualified_ga4.py \
  tests/test_integrations.py \
  tests/test_measurement.py \
  tests/test_prediction_provenance.py \
  tests/test_experiments.py
```

Result: **214 passed in 5.35 seconds**, including the 42 new cases. One existing
Pydantic warning remains: `CrawlResult.schema` shadows a parent attribute. This
was not a whole-repository test run. The run uses fake provider responses and
SQLite; it does not substitute for PostgreSQL concurrency/chaos verification.
Final standalone scenario rerun: **42 passed in 0.91 seconds**.

## Reproduced failures and minimal repairs

The first run produced **26 passes and 10 failures in 0.85 seconds**, across three
defect classes. A subsequent group-subtotal boundary test exposed one additional
manifestation of the measurement defect. These were failing regression tests,
not merely speculative code-review findings.

1. **Requested finality was being treated as proved finality.** The GSC client
   requested `dataState=final`, but ignored response metadata declaring requested
   dates incomplete. The adapter now validates `first_incomplete_date`, rejects
   malformed or contradictory metadata, and preserves per-page response
   metadata. An incomplete date strictly after the requested interval is not a
   contradiction. Final rows still do not establish exhaustive query coverage.

2. **Some invalid GSC metrics reached the row contract.** Boolean counts/position,
   clicks above impressions, and positive infinity could be accepted through
   coercion. The adapter now requires finite numeric metric values (not boolean),
   enforces clicks ≤ impressions, and requires string dimensions. Existing
   nonnegative/integral row validation remains in force. Errors produce a failed
   provider observation, never a zero row.

3. **Date-only coverage could make stale page observations appear current.** A
   newer report could omit `/other` while the old `GA4Daily` row remained. The
   measurement service previously accepted the old row under the new date's
   complete metadata. It now binds each selected daily grain and its metrics to
   the selected immutable collector snapshot. Missing or mismatched rows are
   excluded; missing page/date observations invalidate complete coverage. If any
   prespecified page/date is missing, qualified group totals are unknown, not a
   sum that silently treats missing contributions as zero. Old daily rows and
   immutable evidence are preserved; no historical evidence is deleted.

   Compatibility note: older explicitly scoped operator imports without a
   `rows` payload retain their existing trusted-import coverage contract. All
   normal collector batches include `rows`, including an empty list. This
   compatibility path is not evidence of fresh Google observations.

## Evidence semantics and assumptions

- **Missing provider:** an access/transport/rate-limit failure raises a typed
  provider error. No successful empty observation or numeric zero is returned.
- **Missing row/date:** not a zero. A successful exhausted report may still omit
  suppressed GSC queries, missing GA4 landing pages, or delayed qualification.
- **Observed zero:** retained only when a valid response explicitly supplies that
  grain and value. Zero does not by itself prove healthy tracking.
- **Partial response:** no batch escapes a failed multi-page fetch. A row budget
  cutoff returns explicit incomplete coverage, not a complete total.
- **Tracking outage:** disappearance can be an outage, changed attribution,
  suppression, delay, or actual absence of traffic. These simulations do not
  label one explanation certain; they require measurement to abstain.
- **GSC dates:** Pacific reporting calendar. The final-only request is validated
  against response metadata, but GSC top-row/query suppression means pagination
  exhaustion does not prove full coverage.
- **GA4 dates:** use the returned property's verified IANA time zone. The
  configured calendar-day holdback is an operator policy, not a Google finality
  guarantee. Even a zero-day holdback never qualifies the current property day.
  DST tests cover both repeated and skipped local-hour boundaries.
- **Attribution/qualification lag:** synthetic later imports may add or revise a
  value. Earlier immutable evidence remains reconstructable. No value of the
  default 12-day holdback proves reporting or attribution can never change.
- **Cross-provider alignment:** a GSC Pacific date and GA4 property-local date are
  not automatically the same instant interval. These daily aggregate fixtures do
  not validate an hourly cross-provider causal join.
- **Business objective:** the synthetic traffic-gain scenario does not override
  the declining primary outcome. The returned regression is descriptive; causal
  effect, statistical power, real conversion value, calibration eligibility and
  automatic rollback remain unproved or false.

## Provenance and stopping rule

Tests explicitly relabel batches produced by the real parser as
`fixture:analytics-v2:<provider>`, mark `business=fictional`,
`calibration_eligible=false`, `qualifies_for_autonomy=false`, and zero external/
paid calls before ingestion. A separate regression verifies a fixture batch is
rejected by a live site. The measurement window preserves
`fixture_outcomes_cannot_earn_production_autonomy`; tests assert no calibration
records or production action is created, no budget increase, and Level 1 remains
in force. These labels are not a substitute for the actual production promotion
gate; they are additional test provenance.

Additional permutations of already-covered absent/zero rows, error statuses and
DST inputs were stopped once they no longer changed the decision or exposed a
new failure mode. Remaining human-required work is unchanged: verify the saved
test-event configuration/receipt when the owner is available, then provide a
read-only Google API identity and select/configure durable backend hosting.
None of those account actions is attempted during this blocked hardening work.
