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
| Signed challenge | observation-only crawl/GSC/GA4 fixtures, opaque IDs, truth hash, accepted source hash | truth, expected outcomes, policy or authority fields |
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
   and a maximum 30-day validity window. The fingerprint includes the exact
   dependency lock. The private truth never accompanies the challenge.
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
   process has a minimal filesystem, sanitized environment, no network, no shell,
   no database/executor and bounded CPU, memory, files and output.
4. The evaluator verifies every commitment, independently reruns the accepted
   source in the same truth-blind child boundary, rejects any non-reproducible
   prediction, scores against private truth, and retains its detailed report.
5. The evaluator signs only the strict aggregate attestation. The owner verifies
   it with the same pinned public key:

   ```sh
   python scripts/blind_evaluation_v3.py verify-attestation \
     --attestation /exchange/in/signed-aggregate-attestation.json \
     --public-key /operator/pinned/evaluator-public.pem \
     --key-id independently-agreed-key-id
   ```

6. If later import is explicitly authorized, mount the evaluator public key
   read-only, configure `BENCHMARK_EVALUATOR_PUBLIC_KEY_FILE` and
   `BENCHMARK_EVALUATOR_KEY_ID`, and pin the preregistered
   `BENCHMARK_EXPECTED_DEFINITION_SHA256` and
   `BENCHMARK_EXPECTED_SOURCE_FINGERPRINT`. Then submit the signed envelope to the
   administrator-only
   `POST /api/sites/{site_id}/benchmark-attestations` route. The idempotent route
   stores only a verified aggregate evidence row, its signature/key fingerprint
   receipt, and a non-production audit action. The envelope cannot change site authority and no MCP tool exposes the
   import. It must never receive the evaluator's truth or detailed case report.

## Fail-closed checks

- Strict schemas reject extra case fields such as `ground_truth`,
  `expected_issues`, policy, budgets or autonomy.
- Observation, challenge, prediction, truth, source and benchmark-definition
  commitments are SHA-256 hashes of canonical JSON/source bytes.
- The signed challenge binds source release, evaluator, validity window and
  first exposure before prediction.
- Runtime import independently pins the evaluator key, benchmark-definition hash
  and source fingerprint; a valid signature for another test or release fails.
- The evaluator reruns the predictor; an owner-edited prediction is rejected even
  if the owner recomputes its local hash.
- Attestation arithmetic is recomputed. A passing engineering result cannot carry
  protocol, false-NO-ACTION, disposition-overclaim or coverage-overclaim errors.
- The attestation schema hard-fixes Level 1, production disabled, zero writes,
  zero paid calls, no live model execution and `level_2_eligible=false`.
- The public schema has no arbitrary prose, case, URL, family or recommendation
  fields. Its signature is verified before an aggregate is returned.
- Runtime Docker build context excludes `benchmarks/`, `test_lab/` and all but
  three required operational scripts. CI inspects the built image itself.
- Historical v1 benchmark rows remain append-only for audit integrity, but API,
  MCP and agent evidence paths expose only aggregate/generalized records.

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
