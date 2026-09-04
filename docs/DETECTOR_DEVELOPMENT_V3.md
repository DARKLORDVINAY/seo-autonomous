# Detector development v3 — 4 September 2026

## Scope and safety boundary

This checkpoint improves deterministic diagnostic quality while all production
authority remains disabled. It changes no public Test Lab files, hosting,
Google account, credential, paid provider, CMS content, or autonomy setting.

- `AUTONOMY_LEVEL=1`
- `PRODUCTION_ENABLED=false`
- autonomous production-write budget: `0`
- paid API-call budget: `0`
- Level 2 eligibility: `false`

The retired v2 holdout observations, labels, predictions, and evaluation were
not opened or rerun during this development. Only its declared development
split and general deterministic SEO invariants were used. A broad source-symbol
search did surface benchmark-generator function names; the v2 holdout was
already disclosed and retired, and this checkpoint therefore makes no blind or
independent competence claim. A future claim requires a newly authored holdout
whose author does not inspect this implementation.

## Implemented diagnostic changes

1. Broken internal links retain the actionable source-to-destination edge. One
   failed destination linked from several pages produces one review unit per
   affected source, while an unlinked sitemap-only 404 remains a sitemap issue.
2. Canonical declarations are analysed as a directed graph. A cycle produces
   one grouped `canonical_cycle` finding, rather than several misleading
   canonical-mismatch alerts. Unexplained same-host and cross-host canonicals
   remain review signals, never automatic rewrite authority.
3. Indexability findings now compare observed noindex/robots controls with
   trusted owner intent. Intentionally private, retired, utility, and redirect
   inventory entries do not become defects merely because they are excluded.
4. Orphan and weak-link analysis uses requested URL identity, direct response
   state, the public intended-indexable graph, and trusted page-purpose context.
   Redirect aliases and terminal unavailable resources are not labelled orphan
   pages. Incomplete public graph evidence still yields only a potential orphan.
5. Thin-content review requires an observed main-content region. It abstains on
   unrendered application shells and does not use word count as a quality proxy
   for trusted concise definitions or functional tools.
6. A conservative English-language soft-404 hypothesis requires both a
   missing-resource heading and corroborating missing-resource body text behind
   HTTP 200. Articles that merely discuss HTTP 404 are controls.
7. Sitemap analysis separately identifies an inventoried URL that is still
   listed but returns 404/410.
8. The isolated benchmark adapter can consider an explicitly non-indexable
   private URL sufficiently observed when robots blocks its content. That does
   not relax completeness for intended public URLs or unknown collection faults.

All outputs remain investigation candidates. None of these changes grants an
executor capability or turns uncertain search-engine state into a fact.

## Development-only result

The previously declared 48-case development split was rerun after the changes.
This is training feedback and is expected to be optimistic.

| Metric | Before | After |
| --- | ---: | ---: |
| Issue units | 36 | 36 |
| True positives | 23 | 36 |
| False positives | 35 | 0 |
| False negatives | 13 | 0 |
| Precision | 39.66% | 100% |
| Recall | 63.89% | 100% |
| Correct NO-ACTION controls | 8/18 | 18/18 |
| False NO-ACTION | 0 | 0 |
| Coverage/disposition overclaims | 0/0 | 0/0 |

The development scorer still reports `engineering_benchmark_gate_passed=false`
and `level_2_eligible=false` by design. The old v2 holdout failure remains the
latest independent competence evidence and must not be replaced by this result.

## Verification

- Focused detector/runtime tests: 77 passed.
- Full local suite: 872 passed, 8 environment-gated skips, 0 failures.
- Ruff on changed Python files: passed.
- Legacy v1 Test Lab source/destination evaluator compatibility: passed.
- Production writes, paid calls, public deployments, and account actions: zero.

The eight local skips are the existing container/PostgreSQL gates. They passed
on the unchanged baseline in disposable CI; because detector code has now
changed, the source PR must run the complete disposable CI gate again before
merge. That CI result is software verification, not durable-host evidence.

## Remaining uncertainty and stop rule

- Purpose labels and intended-indexable inventory must come from trusted
  canonical configuration; crawled text and metadata cannot supply them.
- Soft-404 phrase detection is deliberately narrow and English-only.
- Link count does not establish relevance or user value.
- Synthetic development families are correlated and do not forecast business
  impact, Google indexing, conversions, or live specialist-model quality.
- No independent review or fresh blind holdout has evaluated this code version.

Further tuning on the disclosed v2 corpus has low evidential value and is
stopped. The next competence action is to freeze this detector version and have
an independent author create a genuinely new holdout before seeing its output.
Level 2 remains blocked regardless of development performance.
