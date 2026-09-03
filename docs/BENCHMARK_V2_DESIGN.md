# SEO benchmark v2: independent authoring and a frozen holdout

This benchmark tests bounded structural diagnosis and honest abstention. It is
not a test of live Google indexing, a language model's general SEO competence,
real rendered-browser execution, conversion uplift, or production readiness.
Every result retains autonomy level 1, production disabled, a zero production
write budget, zero paid API calls, and `level_2_eligible=false`.

## Preregistered construction

The evaluator author may inspect the shared `CrawlResult` contract but must not
inspect the production analyzer, previous detector/benchmark tests, runtime
predictions, or the new prediction harness while authoring this first holdout.
The author uses synthetic observations and independent causal reasoning, not
the production detector's outputs, as the reference. No detector is imported or
called by either evaluator module.

| Split | Cases | Families | Issue units | Decision units | Strong NO-ACTION cases |
| --- | ---: | ---: | ---: | ---: | ---: |
| Development | 48 | 36 | 36 | 48 | 18 |
| Holdout | 132 | 44 | 108 | 132 | 54 |
| Combined | 180 | 44 | 144 | 180 | 72 |

The holdout contains 18 ambiguous cases, 24 cases from eight interaction families
that never appear in development, and six simulated independent rendered-DOM
cases. There are 324 scored issue-plus-decision units in total, 240 in the
holdout. Decision units are not silently combined with issue precision/recall.

Background structures vary among dense navigation, circular contextual paths,
and hub-oriented collections, with different numbers and locations of pages.
Fault recipes vary affected cardinality and several causal conditions, not just
names. Entire interaction recipes are reserved for holdout. Variants within a
family are correlated; renaming or reseeding them does not manufacture
independent evidence.

Coverage includes deliberate versus conflicting canonical/indexing intent,
complete versus unavailable observations, sitemap inclusion and exclusion,
terminal responses versus transport failures, alias resolution, reachability
versus a simple inbound-link count, useful short pages versus placeholders,
topic similarity versus proven cannibalisation, and raw versus rendered content.
Ambiguous conditions warrant review or more evidence, not fabricated causal
certainty or production action. Missing GSC/GA4 arrays are not zero demand or
zero conversion observations.

## Security boundary and public interface

`benchmarks.seo_v2.corpus.build_corpus(split, seed=20260903)` returns
`(runtime_cases, private_truth)`. The first value is exportable. The second is
strictly evaluator-only. The source authoring file is also private to the
prediction boundary because it contains the recipes and labels.

Each runtime case contains exactly:

```text
case_id, crawls, context, gsc_rows, ga4_rows [, rendered_crawls]
```

Case identifiers and URL paths are opaque. Cases have no family, split, expected
issue, action, label, private note, or truth field. The only context fields are
the agreed owner-declared inventory, coverage, entrypoints, sitemap, indexing
intent, page purposes, site URL, and a word-count threshold. Page-purpose values
describe legitimate use, not expected defects. All source crawl and owner
inventory URLs use `https://example.test`; observed cross-origin destinations
may use the reserved `https://reference.example.test` origin. Nothing is fetched.
Rendered records are simulated independent snapshots, explicitly labelled as
such in evaluator provenance rather than represented as a real browser run.

`benchmarks.seo_v2.evaluator.evaluate(predictions, truth)` is a pure function.
Its prediction packet expects:

```text
metadata:
  source_fingerprint, input_sha256, autonomy_level=1,
  production_enabled=false, production_write_budget=0, paid_api_calls=0
cases:
  case_id, candidates, decision, coverage_complete
candidate:
  kind, page_url, related_urls, disposition, evidence, quality_flags
```

An optional `metadata.runtime_corpus_sha256` is compared with the canonical
unlabelled-record digest in truth. That canonical digest uses sorted-key,
compact, UTF-8 JSON with `ensure_ascii=False`. `input_sha256` instead identifies
the exact exported input file and may differ due to framing or whitespace.
Source/file commitments must be checked by the harness; a scorer cannot prove
filesystem isolation or authentic provenance from self-reported metadata.

## Mandatory run sequence

1. Freeze the production source and prediction entrypoint; record source hashes.
2. Freeze the authoring source and evaluator rules before scoring. The evaluator
   author reports counts, interfaces, and commitments, not individual labels.
3. Export unlabelled input separately from truth. Verify allowed field shapes,
   opaque identifiers, disjoint case identifiers, and the input digest.
4. Run predictions in a deny-network, allowlisted filesystem/process boundary.
   The runtime must not read `benchmarks/`, truth, evaluator tests, private
   manifests containing labels, development/holdout outcomes, or this design
   document. Do not put labels in prompts, logs, database state, environment
   variables, filenames exposed to the model, or tool error messages.
5. Persist immutable raw predictions and their digest before any evaluator or
   human integration agent reads holdout labels or per-case results.
6. Only then run the pure evaluator and inspect false positives, false negatives,
   missed decisions, overclaims, and source-isolation evidence.
7. Record failures honestly. Do not rewrite labels to accommodate unsupported
   detectors or include evaluator logic in production detection.

Process arguments and mounted source files are part of the boundary. Merely
omitting a `truth` JSON key, using a different directory, or promising the model
not to inspect labels is not adequate isolation. The runtime source path must
be allowlisted, not a copy of the full repository. The exact first scored
prediction must remain reconstructable.

## Scoring semantics

An issue is a preregistered diagnostic unit with an allowed kind, affected source
URL or group representatives, related-target rules, and allowed epistemic
dispositions. A group is scored as one diagnostic alert, not inflated into every
possible pair. Correct representative URLs may earn a group alert when the
private rule permits that; unrelated affected URLs never earn credit. For
source/destination faults, both source and required target must be correct.

Matching is maximum one-to-one bipartite matching, independent of prediction
order. Extra guesses cannot earn repeated credit. Only byte-equivalent
candidate objects are deduplicated; extra unassigned or redundant alert units
count against precision and are explicitly reported as such. The score does
not silently discard unknown cases, duplicate case packets, missing cases,
malformed candidates, or unsupported kinds. A detector that outputs nothing
has undefined precision, zero recall on positive units, and missed decisions;
it is not awarded a perfect result.

Report pooled precision/recall/F1, TP/FP/FN, correct NO-ACTION controls, false
NO-ACTION, abstention, appropriate uncertain outcomes, disposition overclaiming,
coverage overclaiming, unsubstantiated evidence, family macro metrics, and
separate control/ambiguous/positive/compound strata. A cautious abstention can be
appropriate and still leave a required diagnostic unit undetected. The report
must show both facts rather than converting caution into fabricated recall.

The preregistered engineering regression gate is precision >=0.95, recall >=0.90,
family-macro recall >=0.80, and NO-ACTION accuracy >=0.95, with zero false
NO-ACTION, disposition overclaims, coverage overclaims, unsubstantiated
candidates, protocol errors, or invariant violations. These thresholds are
engineering acceptance choices, not calibrated probabilities. They do not
replace the mission's autonomy/guardrail acceptance criteria and never grant
Level 2 eligibility.

## Limitations and retirement

This finite synthetic set does not establish business benefit or model
calibration. Cases within the same family share sources and patterns, so pooled
counts must always be accompanied by family-balanced metrics. Main text and
DOM snapshots are simulated; production parser security and live rendering are
separate tests. Evidence presence is checked, but factual provenance requires
the runtime evidence graph and independent harness audit.

Publication of per-case holdout errors consumes the holdout for tuning purposes.
After inspecting first-run results, treat subsequent runs as regression tests,
not fresh blind evaluations. Future competence claims require a newly authored
family/topology holdout, not only a new seed or renamed paths. Stop expansion
once additional variants cease to expose materially different risk. Preserve
the failed first run as well as any later improvements.

The scorer's own tests use explicit miniature toy truth only. They never import
the corpus or reveal holdout cases to the production implementation workstream.
