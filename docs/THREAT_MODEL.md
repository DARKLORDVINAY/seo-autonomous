# Threat model

Assets are site integrity, credentials, production authority, canonical evidence, experiment validity, budget and brand reputation. Untrusted input includes every external webpage, competitor claim, provider response, model output and operator-supplied free text. Trusted deployment administrators and database owners can change infrastructure; application controls cannot protect against a malicious schema owner.

| Threat | Control | Remaining limit |
| --- | --- | --- |
| Webpage prompt injection | Explicit data envelope, no agent tools/handoffs, strict schemas, immutable trusted policy, deterministic executor | A model may still misinterpret facts; independent and human review remain necessary |
| SSRF/DNS rebinding | Public-IP checks, pinned DNS transport, same-origin crawl, redirect revalidation, no credentials/nonstandard ports, conservative robots failures | Network/environment controls remain an additional boundary |
| Approval forgery | Distinct operator/reviewer/admin credentials, fixed server identities, exact revision hash, latest veto, strict input bodies | Token theft grants that capability; rotate and restrict ingress |
| Tampering with canonical provenance | Hashes, tenant foreign keys, append-only triggers and restricted runtime grants | Owner/superuser access is outside the guarantee |
| CMS concurrent edit | Immutable before hash, non-expiring page lease and atomic compare-and-swap requirement | Core WordPress REST updates remain blocked until an adapter provides the contract |
| Crash or network ambiguity | Commit intent/prediction before dispatch; no retry; read-only reconciliation retains lease on disagreement | External side effects can remain unknown until an operator reconciles |
| Budget/replay abuse | Site-locked durable reservations, immutable invocation binding, no refund after ambiguity, daily action limits | A configured price bound is operator-attested; provider billing remains authoritative |
| False success/calibration gaming | Qualified metrics, coverage/freshness flags, frozen forecast, immutable measurement binding, independent adjudication | Observational comparisons do not eliminate confounding or selection bias |
| Cross-site access | Every referenced record is checked against site ID | Deployment-wide tokens are not per-user tenant authorisation |
| UI injection/credential retention | Text-only DOM insertion, CSP, no persisted browser token, request/body limits | Real browser rendering is a separate verification gate |
| Model/provider secret leakage | No secrets in model payloads; error type/ID only; request metadata logging | Operators must also secure proxy logs and tracing/export infrastructure |

Remote MCP validates an external issuer's signed JWT, exact audience, allowed subject, expiry and required scopes. It does not implement an OAuth issuer or bypass Work's connector requirements. The MCP process receives no administrator/reviewer token or database login.

Security-sensitive paths have independently authored adversarial tests. See `SECURITY_REVIEW.md`, `API_SECURITY_REVIEW.md`, `tests/test_budget_security.py` and `tests/test_prediction_provenance.py`. None of these claim exhaustive formal verification or live penetration testing.
