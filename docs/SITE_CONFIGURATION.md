# Site-specific activation

Register the real site origin first. Every route below is site-scoped, accepts a strict schema, derives actor identity from the bearer capability, and records immutable evidence plus an action/event. No body can set its own actor, trust, fixture flag or production authority. Use the host's secret mechanism for credentials; do not send tokens in chat.

## Qualified business outcomes

Administrator `PUT /api/sites/{site_id}/conversion-definition` accepts this **illustrative** mapping; replace every business definition and value with a verified site-specific choice:

```json
{
  "verified": true,
  "tracking_verified": true,
  "qualification_verified": true,
  "deduplication_verified": true,
  "qualified_events": ["qualified_enquiry"],
  "qualification_definition": "An owner-confirmed eligible enquiry for an offered service",
  "deduplication_method": "One event per unique qualified enquiry in the CRM",
  "value_method": "fixed_per_qualified_conversion",
  "currency": "GBP",
  "value_per_conversion": 75.0
}
```

The amount is example data, not an estimate for this user's business. A fixed amount produces **modeled lead value**, not realised revenue. For `value_method: "event_value"`, omit the fixed amount and additionally attest `currency_verified: true` and `value_semantics_verified: true`. Reconcile samples against actual bookings/sales/qualified enquiries before attesting.

The GA4 adapter checks metric/dimension compatibility, collects organic sessions separately from event-filtered outcomes, and never counts sessions twice. Missing qualified rows remain unknown. Definition hashes must match measurement evidence. Timezone consistency and a configurable 12-day outcome processing holdback are conservative operational policy, not Google's guarantee of finality. Recent traffic can exist while outcome evidence is still unsettled; it cannot authorise a confident outcome conclusion.

## Model budget

Administrator `PUT /api/sites/{site_id}/model-price-bound` requires `model`, positive finite `usd_per_million_tokens`, `verified: true`, and `source` pointing to an official HTTPS OpenAI pricing page. Select `OPENAI_MODEL` first. Supply an upper bound covering **both input and output** for that exact model; no price is supplied automatically by this project.

The backend commits conservative token/cost reservations before the SDK call. Daily call count, per-task model allowance, dollar ceiling and suspension are checked under a site lock. A crash retains the reservation; terminal events cannot refund or rewrite it. These are admission estimates. Provider billing is authoritative. Stale or incorrect operator-attested prices require review at deployment and pricing changes.

## Brand facts

Administrator `POST /api/sites/{site_id}/brand-facts` accepts `brand_name`, lists `services` and `service_areas`, `source` and `reason`. These are manually attested facts, not scraped trust labels. They may ground a title but do not bypass sceptical or human review.

## Review and recovery capabilities

| Operation | Capability | Meaning |
| --- | --- | --- |
| `POST .../revisions/{id}/verify` | Operator | Invoke the bounded verifier on exact stored snapshots |
| `POST .../revisions/{id}/human-review` | Reviewer | Explicit factual/policy/conversion/source/alternative/tracking checks |
| `POST .../revisions/{id}/approve` | Reviewer | Approve exact hash, bounded expiry |
| `POST .../revisions/{id}/veto` | Reviewer | Append REJECT or REVOKE; a later explicit approval is required to supersede it |
| `POST .../revisions/{id}/execute` | Operator | Request execution; core gates decide authority |
| `POST .../actions/{id}/rollback` | Operator | Propose an exact inverse; fresh independent review and approval precede execution |
| `POST .../actions/{id}/reconcile` | Operator | Read remote state; no blind POST retry |
| `POST .../pause` | Administrator | Disable site production and suspend automatic work |

All paths in that table start `/api/sites/{site_id}`. OpenAPI schemas are exported in `docs/CONTROL_API.json` and at `/openapi.json`. Approval and configuration routes are not exposed as MCP tools. The dashboard displays approval history; use the privileged API for review.

Production authority remains disabled until a reviewed deployment and site-specific readiness assessment. A core WordPress REST credential alone is insufficient for atomic existing-page updates. The adapter contract must be demonstrated on the actual CMS before enabling those categories.
