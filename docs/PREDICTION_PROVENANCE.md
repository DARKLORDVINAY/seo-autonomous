# Prediction and outcome provenance

Epistemic confidence in a diagnosis is not a probability that the action will increase qualified conversion value. This release never fills a missing success forecast with 0.5. Unknown forecasts permit an otherwise safe, approved reversible action, but cannot enter calibration.

The proposing principal may use `POST /api/sites/{site_id}/experiments/{experiment_id}/forecast` before dispatch. Required fields are `probability_of_success`, a prespecified `success_criterion` and a nonempty `uncertainty` list; `predicted_effect` is optional. The experiment must belong to that principal's exact revision. After dispatch this route rejects changes. Model-generated titles presently omit a success forecast unless one is separately supported; their title confidence is not repurposed.

At dispatch, the executor freezes an immutable `experiment_prediction` evidence packet with:

- Site, experiment, page, action, revision and hash bindings.
- The actual revision proposer and action category; mutable JSON cannot reassign an agent's results.
- Success probability, its explicit semantics, criterion, predicted effect and exclusion reasons—or UNKNOWN.
- Hypothesis, mechanism, primary/secondary outcomes, baseline, reference pages, checkpoints, measurement configuration and business definition hash.

This record is committed with the non-expiring execution lease and dispatch event before any CMS write. Readback/reconciliation links to the same prediction. Editing a later experiment summary cannot rewrite it.

The independent reviewer uses `POST /api/sites/{site_id}/experiments/{experiment_id}/adjudicate`, referencing `measurement_action_id` and `measurement_snapshot_hash`. The body includes `succeeded`, a reason, alternative explanations, causal confidence and whether a rollback is safe to **propose**. The endpoint accepts only a real, non-inconclusive primary checkpoint and trusted reviewer capability. The forecasting principal cannot adjudicate its own outcome. Already calibrated outcomes require explicit contradiction reconciliation, not overwriting history.

The measurement engine then rechecks immutable analysis, current data/definition, prediction source, hash, timestamps, dispatch bindings, primary checkpoint and independent outcome evidence. A recorded human assertion alone does not bypass these checks. Fixture outcomes are excluded from live calibration. Adequately attributed regressions can create a rollback proposal, never an automatic destructive rollback.

Calibration groups use unique adjudicated primary outcomes by agent and action category. Brier score, binned success fractions, sample sizes and selection-bias caveats are reported. Poor sufficiently sampled calibration removes affected earned categories and records the permission reduction; no outcome automatically increases authority.

Adversarial tests cover postoutcome edits, mutable checkpoint prose, fake owner labels, forged hashes, missing/ambiguous forecasts, crashes before persistence, timeout/reconciliation, self-adjudication and fixture exclusion. These test software provenance, not real causal identification.
