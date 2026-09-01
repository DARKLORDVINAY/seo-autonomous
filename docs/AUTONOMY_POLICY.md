# Autonomy and action authority

The release supports levels 0–2, defaults to Level 1, and starts with global/site production authority disabled and shadow mode enabled. Levels 3–5 remain roadmap concepts. Configuration validation rejects a global level above 2. No code path graduates a site automatically.

| Level | Allowed behaviour |
| --- | --- |
| 0 | Observe and report; no proposed or executed website changes. Canonical observation/audit persistence remains necessary. |
| 1 | Prepare evidence and drafts; every production revision needs a separate human approval. |
| 2 | Only explicitly earned, supported categories may omit per-revision approval; verifier, provenance, conversion, policy and concurrency gates still apply. A human veto always wins. |

An operator bearer token permits semantic observation/proposal/execution requests, never human approval or policy configuration. Reviewer and administrator tokens are distinct. Tokens are deployment-wide capabilities for a single-owner installation, not multi-customer SaaS entitlements. An administrator cannot count their own proposal review as independent.

The scheduler is not additional authority. Global production enablement, shadow mode, site enablement, site/global level, budget, suspension, exact verification and any current human veto are enforced by the core path. Caller-supplied labels such as `risk=LOW`, `approved=true` or `source_trust=trusted` cannot create authority.

## Risk and support

| Category | Examples | Release behaviour |
| --- | --- | --- |
| LOW | Local drafts, tasks, link proposals, experiment registration | Audited canonical changes; no arbitrary publishing |
| MEDIUM | Supported title/description/content/link/schema revisions | Exact draft plus independent review; Level 1 human approval; field-specific deterministic checks |
| HIGH | New publication, slug/canonical changes, redirects | Blocked even if an approval exists |
| CRITICAL | Deletion, robots-wide changes, templates, mass migration/deploy | No exposed execution capability |

The WordPress adapter supports a narrower set than the policy enum: unsupported fields fail closed. Existing-page updates additionally require a verified atomic CMS adapter; the default core WordPress client does not satisfy that gate. Creating a CMS draft is not publishing it.

## Earned autonomy

Graduation requires real, independent, prespecified outcomes, adequate samples, successful restore drills, acceptable error/incident history, category-specific calibration and a human decision. Fixture outcomes cannot earn autonomy. Generic "80% confidence" is not a forecast of conversion success.

Poor independently adjudicated calibration removes affected categories from `earned_categories`; it never adds authority. Default policy assesses at least 20 unique adjudicated primary outcomes and considers Brier score and binned calibration gap. These are conservative operational thresholds, not universal statistical standards. Missing outcomes and selection bias remain visible.

Use administrator `POST /api/sites/{site_id}/pause` with a reason to disable site production authority and suspend automation. Resume/graduation is a separate deployment-owner change following review; there is intentionally no model-accessible resume or promote tool. High/critical capabilities need a future reviewed implementation, not a configuration bypass.
