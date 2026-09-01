# Agent runtime integration

`AgentRuntime` is a code-governed manager. It invokes at most three relevant
specialists, then an independent blind diagnosis and a proposal verifier. Each
SDK agent receives no tools, MCP servers or handoffs. The deterministic executor
remains in `backend.app.services.execution`; a model cannot invoke it.

```python
from backend.app.agents import analyze_problem, draft_metadata, verify_proposal

result = await analyze_problem(
    {"kind": "technical", "page_url": "https://example.com/service"},
    canonical_evidence,
    mode="fixture",
    prior_failures=related_failure_cases,
    record_run=persist_agent_run,
)
```

Public functions are async and return JSON-serializable dictionaries.
`verify_proposal(problem, proposal, evidence, *, proposer_id, mode="fixture",
prior_failures=None, budget=None, record_run=None, revision_target=None,
model=None, api_key=None)` uses the same runtime.
`proposal` is a shared `FindingPacket` or its dictionary representation.
All public wrappers and `AgentRuntime` accept optional `model` and `api_key`
arguments so a service can pass its already-loaded settings without mutating the
process environment. An explicit blank value fails closed; it does not select a
fallback. API keys are passed only to the transport and never enter model inputs,
run packets, logs, traces or return values.

For a stored revision, pass `revision_target={"before": revision.before_json,
"after": revision.after_json, "revision_hash": revision.revision_hash}`. Both
snapshots must validate as `CMSPage`, and the hash must be a SHA256 hex string.
The caller must load this target from the canonical immutable revision; its
presence is not execution authority. The blind verifier receives the observed
before-state as `problem.baseline`, with `problem.current` removed. Only the final
verifier receives the exact `revision_target` alongside the proposal and blind
diagnosis. Without an explicit target, `baseline` and `current` remain ordinary
observations for compatibility with diagnostic callers. Do not put a proposed
after-state into the legacy diagnostic problem.

`draft_metadata(problem, evidence, *, mode="fixture", prior_failures=None,
record_run=None, budget=None, model=None, api_key=None)` performs at most one
tool-free SDK call. Supply the actual canonical `CMSPage` or its JSON in
`problem.before`; the runtime copies it and never mutates it. The result is:

```python
{
    "mode": "live",                 # or "fixture"
    "llm_executed": True,
    "status": "PROPOSAL",           # or "NEEDS_EVIDENCE"
    "proposal": {                   # null when unavailable, rejected or fixture
        "title": "Window cleaning | Clearview | Bristol",
        "reason": "Uses the existing service phrase and cited brand facts.",
        "evidence_ids": ["observation-1", "brand-1"],
        "confidence": 0.62,
        "uncertainty": ["Qualified conversion benefit is not established."],
    },
    "before_fingerprint": "...",    # CMSPage.fingerprint; null for invalid/oversized input
    "runs": [],                    # actual terminal run packets
}
```

The strict `MetadataDraftOutput` wire schema requires every field, explicit
uncertainty and bounded values; `title=null` is model abstention. Accepted
confidence and uncertainty are preserved without upgrading them to facts. The
runtime permits extractive title phrases from the before-state's title, visible
content and meta description, or cited operator brand facts. A canonical brand
record can have `source_type="brand_facts"`, `source_trust="trusted_operator"`
and `content={"brand_name": "Clearview", "services": ["Window cleaning"],
"service_areas": ["Bristol"]}`. Other supported explicit fields are
`business_name`, `legal_name`, `locations`, `products`, `verified_facts` and
`facts`. A nested `content.brand_facts` object is also accepted. Brand facts in
other problem fields, external text or measurement labels cannot introduce new
business facts. Every operator record used for wording must be cited.

The deterministic boundary rejects unknown/duplicate citations, fixture
citations in live proposals, novel words, novel numbers, ungrounded pricing or
credentials, title URLs/markup, unsupported explanatory URLs, unchanged titles
and combinations of unrelated numeric fragments. Titles use existing phrases
joined by separators; this intentionally rejects some reasonable paraphrases.
These checks constrain invention but do not establish semantic truth or benefit.
The trusted service must create the immutable revision, compare the returned
before fingerprint, and independently verify the exact proposed before/after
target through the execution service. A drafting result grants no authority.

Evidence must be loaded by a trusted service from the canonical database. Each
record requires `id`, `source` and `source_trust`; supported trust values are
`trusted_measurement`, `trusted_operator`, `untrusted_external`, and `fixture`.
Propagate `data_state` and `quality_flags` at the top level when available.
The remaining JSON contains observations. Do not copy trust metadata from a
webpage into this envelope. Missing/duplicate/invalid IDs and unknown references
fail closed. Generated factual labels are downgraded to interpretations; this
module never writes claims or treats generated text as ground truth.

Use `result["findings"]` for proposed interpretation packets,
`result["verification"]` for a shared `VerificationPacket`, and `result["runs"]`
for latency, token usage, status, model, run/cycle IDs and redacted errors.
`record_run` accepts a dict and can be synchronous or asynchronous. A configured
live run sends `status="started"` **before any SDK spend**. The sink must durably
reserve its daily allowance and upsert the canonical `AgentRun` by packet `id`
before returning. The same `id`, `cycle_id` and `started_at` then identify its
single terminal packet. Each callback receives an independent snapshot; result
`runs` contains only terminal packets, so it cannot double-count a start/finish
pair. Start packets have `reserved_model_calls=1`, a conservative `reserved_tokens`
allowance, `llm_attempted=False`, `llm_executed=False`, unknown billed tokens/cost
and no `completed_at`. Terminal packets retain that reservation and add final
status, elapsed time, completion timestamp, attempt/execution flags, usage when
returned, and redacted error type/ID where applicable. Reserve once per stable
ID, never once per callback. A timeout, cancellation or ambiguous transport
failure does not release its conservative reservation.

A start sink failure raises `AuditSinkError` before a model call or local call
counter increment. It is not retried. A completion sink failure also aborts the
cycle before further calls or results can progress. Fixture calls emit only one
terminal `status="fixture"` packet with zero reservations, tokens and cost. Local
pre-call budget rejections also reserve zero; a cycle deadline reached after an
acknowledged reservation conservatively retains it without calling the model.
Persist findings and decisions in canonical state separately; they are not raw
facts.

The trusted verifier identity is `sceptical-verifier:v1`; the server must place
this identity in the site's verifier allowlist. The model cannot choose its
identity. The unique cycle and run IDs provide execution identity. Bind the
returned verification to the exact stored revision via the safety service.
A runtime PASS is a proposal review, never execution authority. Safety checks,
revision hashes, approvals, experiments and CMS concurrency remain mandatory.

Defaults: 3 specialists, 5 SDK calls, one turn per call, 30-second call timeout,
120-second cycle deadline, 1,800 output tokens/call, 24,000 input bytes/call, and
160,000 conservative reserved tokens/cycle. Reservations use serialized input,
policy/schema byte length and the output allowance; they are not billed tokens.
Actual token usage is recorded if returned. Dollar cost stays unknown unless a
separate accounting integration supplies it. SDK HTTP retries are disabled.
`budget.model_calls` counts acknowledged local reservations; `llm_attempted`
distinguishes actual SDK invocation. Input string/container size and nesting are
bounded before JSON serialization, then the exact serialized byte limit is
checked before reserving a call.

`mode="live"` requires explicit `OPENAI_MODEL` and `OPENAI_API_KEY`, or explicit
`model` and `api_key` arguments. No default model silently incurs spending. SDK tracing exports are disabled unless
`SEO_AGENT_TRACING=true`; sensitive payload capture is always disabled. Local
OpenTelemetry spans and the audit sink do not record prompt bodies or secrets.
Transport environments using SOCKS proxies require the optional `socksio`
dependency. Credentials and network access are still required for live work.

Fixture mode performs deterministic contract/audit-path simulation. It makes no
SDK calls, always returns NEEDS_EVIDENCE, and never claims independent model
verification or live-site validation. All runtime tests mock model and transport
boundaries; they establish software invariants, not agent reasoning quality.

Specialists do not share conclusions. The blind verifier receives observations
and failure history before the proposal is provided in a separate invocation.
Disagreement among specialists blocks action. Shared model training can still
produce correlated error: separate contexts are not independent evidence.
Empirical shadow evaluations and canonical calibration remain activation gates.

The implementation follows the official
[code orchestration pattern](https://openai.github.io/openai-agents-python/multi_agent/)
and [bounded Runner interface](https://openai.github.io/openai-agents-python/running_agents/).
The [tracing controls](https://openai.github.io/openai-agents-python/tracing/)
were verified against the installed SDK. Strict SDK output schemas reject
arbitrary-key dictionaries, so verifier checks use a typed list on the model
boundary and are converted into the canonical packet's checks dictionary.
