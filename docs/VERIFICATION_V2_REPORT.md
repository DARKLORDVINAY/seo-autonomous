# Verification-only checkpoint — 3 September 2026

The completed public Test Lab and its previous 13-unit benchmark/rollback remain
intact. This sprint made no public-site, hosting, Google-account, credential,
paid-provider, or autonomous production change. Level 1, production disabled,
and zero paid-call/production-write budgets remain in force.

## Harder blind result: competence gate failed

An independent author who did not inspect the detector created 48 development
and 132 holdout cases across 44 families. Holdout has 108 diagnostic issue units,
132 separate decision units, 54 NO-ACTION controls, 18 ambiguous cases, 24
holdout-only interaction cases, and six simulated DOM cases. Family variants
are correlated; these are not 132 independent real-world observations.

| Frozen result | Development | Holdout |
| --- | ---: | ---: |
| Cases | 48 | 132 |
| True-positive issue units | 23 | 56 |
| False-positive alert units | 35 | 139 |
| False-negative issue units | 13 | 52 |
| Precision | 39.66% | 28.72% |
| Recall | 63.89% | 51.85% |
| Family-macro precision | 35.90% | 26.06% |
| Family-macro recall | 63.33% | 52.32% |
| Correct NO-ACTION controls | 8/18 | 17/54 |
| False NO-ACTION on issue cases | 0 | 0 |
| Abstentions | 4 | 8 |
| Appropriate uncertain outcomes | 5 | 13 |
| Decision errors | 13 | 44 |
| Coverage / disposition overclaims | 0 / 0 | 0 / 0 |
| Runtime exceptions | 0 | 0 |

The engineering gate requires precision >=95%, recall >=90%, family-macro
recall >=80%, NO-ACTION accuracy >=95%, and zero specified protocol/safety
violations. **Both splits fail.** A successful code-test run is not a successful
SEO competence gate, and even a perfect fixture score would not earn Level 2.

This evaluates the unchanged production deterministic detector through a
strict observation adapter, not live specialist-model reasoning. Every
candidate is a REVIEW signal; no candidate is an authorized production fix.
Some strict misses are missing diagnostic kinds or source/destination contract
mismatches rather than failure to notice the underlying HTTP status. Those
misses are retained, not relabelled away.

Main remaining diagnostic gaps include connected-component orphan reasoning,
canonical/redirect cycles and indexability interactions, soft-404 and rendered
failure interpretation, stale sitemap handling, and choosing NO-ACTION for
legitimate short pages, aliases and small contextual link graphs. Broad
investigation alerts can be factually descriptive yet still fail the benchmark's
useful-action/no-action decision contract. No business impact is inferred.

## Isolation, freezing and the protocol correction

The child process receives only allowlisted source files and unlabelled
observations. It has no evaluator, generator, database, CMS, model tools, ambient
credentials, network or subprocess capability. Tests actively attempt outside
file reads, socket/process creation, writes and environment mutation. CPU,
memory, input, page, candidate and output limits bound execution.

This is a Python audit-hook defense around trusted deterministic code, **not a
kernel sandbox**. It is not a claim of live-model prompt-injection immunity.

Predictions and runtime/scorer/generator source hashes were frozen and sealed in
append-only canonical state before evaluation. The detector was not changed or
tuned on either split. Holdout predictions SHA-256:
`18730b385ce4cc5742e8a8ac28289e3260560437e2e40f78c97e5cbf29a08ef9`.
Combined source fingerprint:
`28d92598e4d37883b9b38cb58cd36da094abc719973dcd1ced41bb265acbf1dc`.

Independent pre-freeze review caught and repaired false completeness for
truncated/unsupported observations, unsealed cached reports, and missing
parent-only scorer/generator commitments. Unknown observation flags conservatively
prevent a clean completeness claim in this fixture adapter.

The first evaluation exposed an integration error: the scorer expected safety
fields under `metadata`, while the agreed predictor wrote them at top level.
The original report consequently marked those invariants unverified. Both
original artifacts are preserved. `benchmarks/adjudication_v2.py` performs an
explicit metadata-only correction using the same sealed predictions, verifies
the input digest, and asserts **every issue/decision score, case result and
threshold is unchanged**. All six safety/commitment checks then pass. There is
one blind run, not a second independent replication or an improved score.

Evaluator artifacts and case labels remain outside operational runtime inputs.
Canonical benchmark failures use the `lab_benchmark_` prefix already excluded
from specialist prior-failure prompts. The full answer key is not placed in
operational claims. This disclosed holdout is now retired for tuning purposes;
reruns are regression checks only. A future improved detector requires newly
authored families/topologies for a new competence claim.

## Reproduced software failures and hardening

| Area | Material result |
| --- | --- |
| Untrusted inputs | 67 new cases; six availability/observation-integrity defect groups fixed; false verifier PASS still causes zero CMS writes |
| Scheduler | Durable three-attempt/period cap; interrupted cycles quarantined; stale ORM completion recheck; recovery cache regression caught by independent review |
| Accelerated soak | 28 virtual days, 336 ticks, 61 unique operations, 275 no-op replays; eight real competing threads; 200 monotonic lease reclaims |
| Analytics | 42 scenarios; invalid GSC finality/metrics rejected; newest evidence snapshot governs retained daily rows; absent group members remain unknown |
| Durable package | Exact schema-head readiness, fixed-zero deployment overlay, immutable-image preflight, non-overwriting checked archives and isolated PostgreSQL restore test |

Detailed evidence and limitations:
[input security](UNTRUSTED_INPUT_HARDENING.md),
[scheduler chaos](SCHEDULER_CHAOS.md),
[analytics uncertainty](ANALYTICS_FIXTURE_HARDENING.md),
[benchmark design](BENCHMARK_V2_DESIGN.md),
[durable deployment package](DURABLE_DEPLOYMENT_PACKAGE.md).

The full local regression finished **861 passed, 8 skipped in 28.62 seconds**;
Ruff passed. A staged whitespace check reports one terminal blank line in the
frozen evaluator package; it is retained to preserve the pre-evaluation source
commitment. The eight skips require actual
PostgreSQL/container capabilities. An earlier integration run found one test
ordering issue: Alembic logging configuration disabled the chaos test's logger.
The test now restores that logger for capture without mocking lease operations.
PostgreSQL/Compose/restore verification is a separate disposable CI gate; do not
substitute the local result for it. Its first run passed 864 tests, including the
actual archive/restore drill, but failed five PostgreSQL chaos setups: the
metadata `create_all` trigger helper passed an unescaped PL/pgSQL percent sign
through the raw driver path. The existing Alembic migration path passed. The
helper now uses SQLAlchemy's dialect-aware text compilation, without changing
the trigger's guard or the frozen migration. Cleanup also skips Compose when an
earlier failure prevented creation of the ephemeral CI environment file.
Updated JUnit configuration retains the
soak/restore workload properties in a compatible report format.

The corrected [disposable CI run](https://github.com/DARKLORDVINAY/seo-autonomous/actions/runs/33757762485)
on source `46bfb451fb667a245859b5780bcec828dd3756ec` passed **870 tests,
zero skipped/failed, one existing warning, in 116.68 seconds**. Actual PostgreSQL
chaos and privilege gates, migrations/drift, checked dump/restore, nonroot image,
fixed-zero preflight/startup with its worker stopped, dashboard browser checks,
and all 26 local fixture-rendered pages passed. The v2 regression still reported
the failed competence gate. These are disposable CI observations, not a new
public-site release, Google event receipt, or a connected durable backend.
The first failed run remains part of the failure record. The subsequent source
checkpoint edits documentation only; this identifies the exact tested code.

Independent post-evaluation author review was unavailable because that worker
hit its usage limit. Integration review and metadata-only re-adjudication were
performed by the root; this limitation is retained rather than represented as
an additional independent review.

## Remaining uncertainty and stopping rule

- No real Google ingestion, live-model SEO reasoning, indexing benefit,
  qualified conversion uplift, WAN partition/failover or durable-host recovery
  is established by these synthetic tests.
- Simulated DOM records are not new live rendered-browser acceptance. The
  already-completed public/browser release checks remain historical evidence.
- Existing `noindex` substring and bot-scope parsing can misclassify malformed
  directive text. This pre-existing accuracy issue is recorded, not silently
  included among the repaired security defects.
- The baseline `/readyz` checked reachability/tables only; the new release checks
  the exact Alembic revision. Package host/TLS/secret/backup choices remain open.
- Byte/checksum seals detect ordinary alteration; a privileged actor capable of
  replacing all artifacts must be checked against the independent canonical
  seal. They are not digital signatures or a tamper-proof operating system.

Stop adding synonymous injection strings, reseeded benchmark copies, or more
virtual days: those have low marginal information value. Preserve the failed
baseline and use future development work to address causal/topology and
decision-quality gaps without tuning on this holdout.

Exact future human-required critical action: **explicitly end the verification-only
freeze and select/authorize an existing durable PostgreSQL/container target**.
No action is requested now. Existing Google properties must not be recreated;
read-only access, the prior GA4 default-value discrepancy, actual event receipt,
and any live-model spend authorization remain later separate gates. Level 2 is
not qualified or enabled.
