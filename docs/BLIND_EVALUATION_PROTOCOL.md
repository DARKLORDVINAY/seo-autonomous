# Blind benchmark exchange v3

Status: protocol implemented and tested; **no new holdout has been authored or
scored**. The disclosed v2 holdout remains retired and its failed first result
remains the latest independent detector evidence. This protocol cannot qualify
Level 2 and does not change production authority.

## Purpose and trust boundary

The independent evaluator sees the frozen detector release and privately authors
a new holdout. The project owner receives only signed observation packets with
opaque case IDs. The runtime control plane receives only a signed aggregate
attestation. Truth, case results, family names, affected URLs and missed/matched
unit identities stay in evaluator custody.

| Custody | May contain | Must not contain |
| --- | --- | --- |
| Evaluator private store | truth, case/family labels, detailed score, signing key | production credentials |
| Signed challenge | observation-only crawl/GSC/GA4 fixtures, opaque IDs, keyed truth commitment, accepted source hash | truth, commitment secret, expected outcomes, policy or authority fields |
| Frozen owner response | signed challenge, deterministic predictions, byte commitments | truth, scorer, signing key, private results |
| Public attestation | aggregate counts/rates, commitments, fixed limitation codes, safety invariants | cases, URLs, families, recommendations, free-form evaluator prose |
| Runtime image/API/MCP | verified aggregate summary | corpus, Test Lab source labels, evaluator/operator scripts, raw benchmark failures |

Ed25519 authenticates evaluator statements. The public key is pinned out of band;
neither a challenge nor an attestation may provide or replace its trusted key.
Signatures are domain-separated by protocol message type and bind the routed key
ID, preventing a challenge signature from being reused as an attestation.
The signature is not proof that an evaluator is organizationally independent, so
the evaluator identity and process still require human due diligence.

## Exchange

1. Freeze and review a source commit. On that exact tree, obtain the owner-side
   fingerprint without reading any holdout:

   ```sh
   python scripts/blind_evaluation_v3.py fingerprint
   ```

2. A genuinely independent evaluator, before exposing any output, creates a new
   observation-only corpus and private truth. It signs a challenge committing to:
   the observation bytes, private-truth bytes, benchmark definition, accepted
   source fingerprint, CPython 3.12 runtime profile, first-exposure declaration
   and a maximum 30-day validity window enforced at prediction and scoring. The truth commitment uses an
   evaluator-retained 32–128 byte secret so enumerable labels cannot be tested
   against a bare digest. The benchmark-definition digest binds the public
   scoring definition to the scorer's module and callable source. That binding
   does not authenticate mutable in-memory globals or installed artifacts. The
   source fingerprint includes the dependency-lock text, but the local helper
   does not prove that installed wheels match it. Neither truth nor the
   commitment secret accompanies the challenge.
3. The owner verifies the pinned key and runs the isolated deterministic worker:

   ```sh
   python scripts/blind_evaluation_v3.py predict \
     --challenge /exchange/in/signed-challenge.json \
     --public-key /operator/pinned/evaluator-public.pem \
     --key-id independently-agreed-key-id \
     --output /exchange/out/frozen-response
   ```

   The output directory must not exist. It is created write-once and contains
   exactly the signed challenge, frozen predictions and their manifest. The child
   process stages bytes from one frozen source snapshot, uses a sanitized environment,
   denies ordinary network, subprocess, outside-read and write operations through
   a Python audit hook, and bounds CPU, memory, files and output. This is defense
   in depth for source-pinned trusted detector code, not a kernel sandbox for
   arbitrary hostile Python. A real blind run therefore requires an
   evaluator-owned OS/container boundary with network disabled and no truth,
   signing-key or evaluator-store mount in the predictor. The evaluator must pin
   an immutable runtime/image and verify dependency artifacts before making a
   reproducibility or truth-isolation claim.
4. The evaluator verifies every commitment, independently reruns the accepted
   source in the same truth-blind child boundary, rejects any non-reproducible
   prediction, scores against private truth, and retains its detailed report.
5. The evaluator signs only the strict aggregate attestation. The owner verifies
   it with the same pinned public key:

   ```sh
   python scripts/blind_evaluation_v3.py verify-attestation \
     --attestation /exchange/in/signed-aggregate-attestation.json \
     --public-key /operator/pinned/evaluator-public.pem \
     --key-id independently-agreed-key-id \
     --expected-definition-sha256 independently-recorded-definition-hash \
     --expected-source-fingerprint owner-frozen-source-hash \
     --expected-evaluation-id independently-recorded-evaluation-uuid \
     --expected-challenge-sha256 independently-recorded-challenge-hash \
     --expected-observations-sha256 independently-recorded-observation-hash \
     --expected-predictions-sha256 owner-frozen-prediction-hash \
     --expected-truth-commitment-sha256 independently-recorded-truth-commitment \
     --expected-execution-environment-sha256 independently-recorded-runtime-hash \
     --max-age-hours 168
   ```

6. If later import is explicitly authorized, mount the evaluator public key
   read-only, configure `BENCHMARK_EVALUATOR_PUBLIC_KEY_FILE` and
   `BENCHMARK_EVALUATOR_KEY_ID`, and pin the preregistered
   `BENCHMARK_EXPECTED_DEFINITION_SHA256` and
   `BENCHMARK_EXPECTED_SOURCE_FINGERPRINT`. Also pin the one intended
   `BENCHMARK_EXPECTED_EVALUATION_ID` and
   `BENCHMARK_EXPECTED_CHALLENGE_SHA256`, the observation, frozen-prediction and
   private-truth commitments, plus the immutable runtime/image as
   `BENCHMARK_EXPECTED_EXECUTION_ENVIRONMENT_SHA256`; the default import freshness window is
   seven days and is independently bounded by
   `BENCHMARK_ATTESTATION_MAX_AGE_HOURS`. Then submit the signed envelope to the
   administrator-only
   `POST /api/sites/{site_id}/benchmark-attestations` route. The idempotent route
   stores only a verified aggregate evidence row, its signature/key fingerprint
   receipt, and a non-production audit action. The envelope cannot change site authority and no MCP tool exposes the
   import. It must never receive the evaluator's truth or detailed case report.

## Fail-closed checks

- Strict schemas recursively reject extra truth/authority keys such as
  `ground_truth`, `groundTruth`, `expected_issues`, policy, budgets or autonomy,
  including separator and case variants inside metadata, JSON-LD and issues.
- Observation, challenge, prediction and source commitments are SHA-256 hashes
  of canonical JSON/source bytes. Private truth uses keyed HMAC-SHA-256. The
  benchmark-definition hash also commits the exact scorer module and callable.
- The signed challenge binds source release, evaluator, validity window and
  first exposure before prediction.
- Runtime import independently pins the evaluator key, benchmark-definition hash,
  source fingerprint, exact evaluation and signed challenge, and rejects stale
  attestations; a valid signature for another test or release fails.
- The evaluator reruns the predictor; an owner-edited prediction is rejected even
  if the owner recomputes its local hash.
- Attestation arithmetic is recomputed. A passing engineering result cannot carry
  protocol, false-NO-ACTION, disposition-overclaim, coverage-overclaim or
  unsubstantiated-candidate errors, and every preregistered ambiguous case must
  receive the appropriate uncertain outcome. Every attestation must retain the
  fixture, non-browser, no-live-measurement, evaluator-independence and
  no-autonomy scope limitations even when its engineering gate passes.
- The attestation schema hard-fixes Level 1, production disabled, zero writes,
  zero paid calls, no live model execution and `level_2_eligible=false`.
- The public schema has no arbitrary prose, case, URL, family or recommendation
  fields. Its signature is verified before an aggregate is returned.
- Runtime Docker build context excludes `benchmarks/`, `test_lab/` and all but
  three required operational scripts. Legacy label reading/scoring is isolated
  under `benchmarks/`; the runtime Test Lab service contains observation and
  freezing only. CI inspects the built image and imported runtime symbols.
- Historical v1 benchmark rows remain append-only for audit integrity, but API,
  MCP and agent evidence paths expose only aggregate/generalized records and a
  hash of that public projection, never the private record hash.

## Leakage and overfitting policy

Publishing any case-level result consumes the holdout. Replaying it can provide
regression evidence only; it cannot become another blind replication. Detector
tuning must stop after disclosure, and a future independent author must create a
new holdout. The evaluator should use many diverse deterministic, ambiguous and
NO-ACTION controls, cross-page fault combinations and metamorphic variants, but
must not reveal family templates before the prediction is frozen.

Benchmark success is synthetic structural evidence. It does not establish
Google indexing, rankings, qualified conversion value, live-model quality,
rollback safety on a durable host or causal benefit. Autonomy graduation remains
a separate human decision supported by live shadow evidence and calibration.

## Residual external verification boundary

The repository supplies a protocol scaffold and defense-in-depth worker, not an
independent evaluator or a certified sandbox. Importing the evaluator helper is
side-effect tested not to load the detector, but it remains project-supplied
Python. A genuine holdout result is acceptable only when an independent party
reviews or reimplements the truth-holding harness, runs the predictor in a
kernel-enforced boundary, keeps truth and signing material outside that boundary,
and binds the attestation to an immutable interpreter/image plus verified
dependency artifacts. Until that occurs, local protocol tests are engineering
evidence only and `level_2_eligible` remains false.
