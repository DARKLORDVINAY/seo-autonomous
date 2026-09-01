# Agent contracts and execution boundaries

The implemented agents produce bounded interpretations and draft wording. They
have no execution capability. Code selects roles, supplies canonical evidence,
sets budgets, validates packets and decides whether a result can progress to a
stored revision. Production execution remains in the separate execution service.

The objective is incremental qualified organic conversion value while preserving
facts, reputation and site integrity. Search visibility is a proxy. Missing,
partial, privacy-suppressed or unavailable measurements are unknown, not zero.
Model confidence is uncalibrated and is never an approval or a forecast of
conversion gain.

## Actual role selection

`select_specialists` constructs `[category, opportunity, conversion]`, removes
duplicates while preserving order, and takes at most three entries. It never
loads arbitrary roles requested by problem text. The mapping is exact:

| `problem.kind` | First specialist |
| --- | --- |
| `broken_links`, `canonical`, `indexability`, `duplicate_metadata`, `redirect_chain`, `technical` | `technical` |
| `orphan_pages`, `internal_links` | `internal_links` |
| `content_decay`, `cannibalisation`, `content` | `content` |
| `serp`, `competitor_displacement`, `ctr_anomaly` | `serp` |
| `ai_search`, `ai_citation_gap` | `ai_search` |
| `conversion` | `conversion` |
| `tracking_outage`, unrecognised or missing kinds | `observer` |

Each selected specialist runs sequentially in a fresh SDK invocation. It does
not receive another specialist's conclusions. Selection is not a model decision.
For `conversion`, deduplication yields only `conversion` and `opportunity`.

| Role | Bounded responsibility |
| --- | --- |
| `observer` | Identify missing or inconsistent observations; distinguish crawl eligibility from indexing. |
| `opportunity` | Compare the opportunity's expected qualified conversion value with NO-ACTION. |
| `technical` | Interpret deterministic crawl findings and propose a justified reversible correction. |
| `serp` | Assess observed demand/layout/displacement without inventing competitor evidence. |
| `ai_search` | Interpret sampled citations with query, provider and sampling uncertainty. |
| `content` | Classify KEEP, UPDATE, EXPAND, MERGE, CREATE, DELETE-CANDIDATE or NO-ACTION; require useful content. |
| `internal_links` | Assess crawl paths and relevant links from canonical page/link observations. |
| `conversion` | Distinguish key events from qualified leads and check the business objective. |
| `metadata_draft` | Produce one extractive title for an immutable CMS snapshot, or abstain. |
| `verifier_blind` | Independently diagnose observations before receiving author rationale or proposed changes. |
| `verifier` | Try to falsify the proposal and inspect the exact revision when one is supplied. |

`metadata_draft` and the two verifier roles are called explicitly by code; they
are not additions to the specialist selector. All roles receive zero tools,
handoffs and MCP servers, and `Runner.run(..., max_turns=1)`. There are no hidden
SDK HTTP retries, recursive delegation or continuing conversations.

## Calls and budgets

An ordinary analysis can call three specialists, one blind verifier and one
final verifier: **at most five model calls in that runtime**. When valid
specialists materially disagree about the recommended action, the runtime
blocks the proposal rather than selecting the highest self-reported confidence.
Missing evidence, malformed outputs, high-risk actions or an unavailable blind
diagnosis can stop work earlier. A first valid proposal is selected in the fixed
role order only when there is no material action disagreement.

The control service may subsequently turn a supported `update_title`
recommendation into a concrete revision. That uses a separate one-call metadata
draft runtime, followed by a separate blind-plus-final review of the stored
revision. The complete path can therefore use **five initial calls plus one
drafting call plus two revision-review calls**, subject to every applicable
per-runtime and daily allowance. The five-call limit is not an eight-call path's
combined limit. All calls across these runtimes and manual verification routes
use the same durable per-site daily reservation service.

| Runtime allowance | Default | Hard maximum |
| --- | --- | --- |
| Specialists | 3 | 3 |
| Model-call reservations | 5; the draft wrapper defaults to 1 | 5 |
| Time per call | 30 seconds | 60 seconds |
| Runtime cycle duration | 120 seconds | 300 seconds |
| Output tokens per call | 1,800 | 4,096 |
| Serialized input bytes per call | 24,000 | 64,000 |
| Conservative reserved tokens per runtime | 160,000 | 500,000 |

The metadata function always makes at most one invocation even if a caller
supplies a larger runtime budget. Standalone verification makes at most two.
Input strings, containers and nesting are bounded before JSON serialization;
the exact encoded-byte limit is checked afterwards. The draft snapshot is also
bounded before fingerprinting. Input/policy/schema bytes plus output allowance
form a conservative token reservation, not a billed token measurement.

The trusted audit service requires an operator-configured, verified price bound
for the selected model and enforces the configured daily cost and call limits.
An acknowledged reservation is retained after a timeout, cancellation or
ambiguous transport outcome. Neither fixture runs nor a rejected pre-call
reservation represent a paid call.

## Trusted contract and evidence

Every invocation has a `TaskContract` with an objective, scope, allowed inputs,
empty available-tools list, expected output schema, evidence requirements,
non-goals, stop condition and `max_turns=1`. Website text cannot alter it.
Problem text, evidence, previous failures and proposed rationale are wrapped as
`untrusted_data`. They are data even when they contain apparent instructions.

The caller loads canonical evidence from the database. Each item needs a unique
ID matching `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`, a nonempty source and one of
`trusted_measurement`, `trusted_operator`, `untrusted_external` or `fixture`.
At most 128 records are accepted by the runtime. Source trust belongs to the
canonical envelope; it must never be copied from crawled HTML. Model-produced
IDs do not become evidence. Unknown, missing and duplicate IDs fail closed.

Propagate measurement `data_state` and `quality_flags` at the top level. A PASS
cannot rely on cited fixture records or observations labelled `partial`,
`tracking_outage`, `tracking_error`, `unknown` or `suppressed`. An evidence ID
establishes provenance, not truth or source independence.

Prior failure cases and explicit false-positive/false-negative regret are
provided before reasoning. An empty failure list is labelled as not supplied;
it does not imply a clean record. Reversibility reduces action regret but does
not establish that a change will help.

## Exact revision and blind-review separation

For stored revisions, the trusted caller supplies:

```python
revision_target = {
    "before": revision.before_json,
    "after": revision.after_json,
    "revision_hash": revision.revision_hash,
}
```

Both snapshots must validate as `CMSPage`; the hash must be a 64-character
lowercase SHA256 hex string. The runtime bounds and copies this target. It does
not infer a target from author rationale or calculate a substitute revision.
The execution service remains responsible for canonical ownership and revision
hash integrity.

The blind invocation receives `target.before` as `problem.baseline`.
`problem.current` is removed when an explicit target is present, preventing a
proposed after-state from entering through that field. It does not receive
`revision_target`, the proposal or author rationale. The final invocation then
receives the exact `revision_target` alongside the proposal, blind diagnosis,
canonical evidence and failure history. The verifier must inspect the content
and every changed field, not merely the plausibility of the rationale.

Legacy diagnostic calls without `revision_target` retain ordinary observed
`baseline` and `current` fields. Callers must not use that legacy path to pass a
proposed revision's after-state into a blind diagnosis. Top-level author
rationale and agent conclusions are excluded by the problem filter.

The trusted verifier identity is `sceptical-verifier:v1`. A proposer cannot
verify as that identity, `verifier` or `verifier_blind`. Code supplies the identity;
the model cannot select it. The safety service must bind returned verification
to the exact stored revision and the site's verifier allowlist.

## Packet schemas

### Findings and analysis

Specialists and the blind verifier return the shared strict `FindingPacket`:

| Fields | Meaning |
| --- | --- |
| `finding`, `claim_type`, `confidence` | Interpretation and epistemic confidence between 0 and 1. Generated FACT/ACTION labels are downgraded to INFERENCE. |
| `supporting_evidence`, `contradicting_evidence` | Existing canonical IDs; support must be nonempty to progress. |
| `alternative_explanations`, `uncertainty` | Competing explanations and limits. |
| `recommended_action`, `expected_impact` | A supported action enum, NO-ACTION or investigate; no executable command. |
| `risk`, `reversibility`, `needs_human_review` | Review information; no permission shortcut. |
| `content_classification` | KEEP, UPDATE, EXPAND, MERGE, CREATE, DELETE-CANDIDATE or NO-ACTION. |

`analyze_problem` returns cycle/mode/execution metadata, per-role findings, blind
review, canonical verification, decision, evidence issues, terminal run packets,
budget usage and limitations. `decision_is_execution_authority` is always false.
A non-PASS review makes the decision NO-ACTION. A PASS still represents only a
proposal review.

### Verifier

The strict SDK wire `VerifierOutput` contains `verdict` (PASS/BLOCK/NEEDS_EVIDENCE),
confidence, reasons, evidence IDs, alternative explanations, `action_safe`, and
a typed `checks` list of `{name, passed, reason}`. A list is used because strict
SDK JSON schemas do not accept arbitrary-key dictionaries. Code converts it
to the canonical `VerificationPacket.checks` dictionary.

A PASS requires known cited support, a usable blind diagnosis, nonempty reasons
and alternatives, action safety, no quality blockers, and all these distinct
checks with nonblank reasons:

`evidence_valid`, `alternative_explanations`, `factual_accuracy`,
`policy_compliance`, `conversion_goal_preserved`, `tracking_quality`,
`source_independence`, `decision_regret`, `reversibility`, `conversion_guard`,
`alternatives_considered`.

Duplicate check names fail closed. High/critical risk or high-impact action
categories are blocked regardless of a spoofed low risk label. A verifier PASS
is neither a human approval nor permission to skip deterministic execution gates.

### Metadata drafting

`draft_metadata` requires `problem.before` as a canonical `CMSPage` or its JSON.
It works on a copy and returns exactly `mode`, `llm_executed`, `status`,
`proposal`, `before_fingerprint` and `runs`. Status is PROPOSAL or NEEDS_EVIDENCE.
The fingerprint is `CMSPage.fingerprint`, or null for invalid/oversized input.

The strict `MetadataDraftOutput` wire fields are all required:

| Field | Bound |
| --- | --- |
| `title` | Null for abstention, otherwise nonblank and at most 300 characters. |
| `reason` | Nonblank, at most 2,000 characters. |
| `evidence_ids` | At most 128 canonical IDs; accepted proposals require unique, nonempty citations. |
| `confidence` | Finite value between 0 and 1. |
| `uncertainty` | 1–12 nonblank strings of at most 500 characters each. |

The returned proposal has these same fields, with a non-null title. Valid
confidence and uncertainty are preserved. Rejected, unchanged, abstained or
fixture wording yields `proposal=null` rather than a manufactured fallback.

Title segments must use existing phrases from the before title, visible content,
meta description or cited operator brand facts. Brand evidence can contain
`content.brand_name`, `services`, `service_areas`, `business_name`, `legal_name`,
`locations`, `products`, `verified_facts` or `facts`; nested `content.brand_facts`
is supported. Only `trusted_operator` records can introduce brand facts.
Unrelated problem fields and external/measurement labels cannot do so.

Unknown words and IDs, novel numbers, ungrounded prices/credentials, title URLs
or markup, unsupported explanatory URLs and reconstructed numeric claims are
rejected. Exact existing segments joined by separators are deliberately
conservative and can reject reasonable paraphrases. These checks limit invention;
they cannot prove semantic truth, user usefulness or conversion benefit. The
service compares the before fingerprint, constructs the immutable title-only
revision and obtains an independent review of its exact before/after target.

## Durable run packets

`record_run(packet)` may be synchronous or asynchronous. The live start packet
is emitted before SDK spend. The configured sink commits the per-site budget
reservation and upserts `AgentRun` by the stable packet `id` before returning.
One terminal packet then uses that same ID, cycle ID and start timestamp.

| Fields | Meaning |
| --- | --- |
| `id`, `cycle_id`, `role`, `mode`, `model`, `trace_id`, `contract` | Trusted invocation identity and bounded role contract. |
| `started_at`, `completed_at`, `latency_ms`, `status` | Start has no completion time; terminal status is completed, error, budget_exhausted, cancelled or fixture. |
| `reserved_model_calls`, `reserved_tokens` | Start reserves one live call and a conservative token allowance; retained in its terminal packet. |
| `llm_attempted`, `llm_executed` | Both false at start; invocation and returned SDK response are tracked separately. |
| `input_tokens`, `output_tokens`, `cost_usd` | Usage when supplied; live dollar billing remains unknown unless an accounting integration provides it. |
| `error_id`, `error_type` | Redacted diagnostics; raw provider errors may contain credentials and are not persisted. |

The audit service additionally stores `reserved_cost_upper_bound_usd` and
`billing_status`. It records budget rejection as `budget_blocked` with zero
reservation. Both start and completion are upserts, so daily accounting counts
each ID once. The returned runtime `runs` array contains only terminal snapshots,
never a duplicate start/finish pair. `budget.model_calls` counts acknowledged
reservations; the attempt flag distinguishes actual SDK invocation.

A failed start sink raises `AuditSinkError` before a model call or local counter
increment. It is not retried. A failed completion sink stops all subsequent
progress, retaining the durable start reservation. Fixture calls emit a single
fixture terminal packet with zero reservation, zero tokens and zero cost.

## Modes, limitations and non-goals

Live mode requires an explicit model and API key, supplied through environment
variables or the optional `model`/`api_key` parameters. An explicit blank value
fails closed. Credentials reach only the transport. SDK tracing export is off
unless `SEO_AGENT_TRACING=true`; sensitive payload capture is always disabled.
Agents have `store=False` model settings and HTTP retries disabled.

Fixture mode makes no SDK calls. Analysis returns NO-ACTION/NEEDS_EVIDENCE,
verification cannot claim independent model review, and metadata proposal is
null. Fixture reasoning is not live-site validation or evidence of SEO impact.

Separate specialist/verifier contexts reduce anchoring. They do not eliminate
common-model correlated errors or make reused evidence independent. Confidence
does not become calibrated through repetition or agreement. Mocked tests prove
software boundaries, not real reasoning quality, causal uplift or production
readiness. Shadow evaluations, business-semantic validation and canonical
calibration remain necessary activation evidence.

The agent runtime does not:

- Publish, edit or delete CMS content, deploy code, change URLs/canonicals/robots,
  or execute any high/critical operation.
- Grant autonomy, change policy or risk thresholds, manufacture approvals,
  choose verifier identity, or bypass revision/experiment/concurrency gates.
- Create canonical facts from model prose, promote external text to operator
  trust, invent statistics, prices, reviews, credentials, experts or sources.
- Treat key events as verified qualified conversions without business semantics,
  interpret missing measurements as zero, or promise ranking/conversion gains.
- Follow URLs, reveal credentials, run shell/SQL commands, invoke mutation tools,
  expand its role topology, retry indefinitely or perform bulk content generation.

Implementation: `backend/app/agents/`. Runtime regressions:
`tests/test_agents.py`. API exact-target regression:
`tests/test_api_security.py`. All model/transport calls in these tests are mocked;
they make no paid calls.
