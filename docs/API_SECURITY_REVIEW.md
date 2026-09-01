# Independent API integration security review

Review date: 2026-09-01. This second, bounded review covers the assembled FastAPI
routes and the semantic MCP-to-API boundary. It supplements the earlier service,
database, crawler, and OAuth review in `SECURITY_REVIEW.md`.

The reviewer owned only this report and `tests/test_api_security.py`.
Implementation changes were routed to the implementation owner. Tests use an
isolated in-memory SQLite database, the real canonical site/CMS ingestion
services, separate operator/reviewer/administrator tokens, FastAPI dependency
overrides, and a counting in-memory CMS. The verifier-input regression replaces
the model invocation method with a capturing test double. No external network,
real CMS mutation, or paid model call is needed.

## Finding

| ID | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| API-01 | High, decision integrity | The final independent verifier received a revision's rationale and action name without its actual proposed page snapshot or immutable revision binding | Fixed by implementation owners; independent API regression passes |

### API-01: review of an unseen change

The `POST /api/sites/{site_id}/revisions/{revision_id}/verify` route originally
constructed a `FindingPacket` from the revision's reason and action kind, then
invoked the trusted verifier. Canonical evidence contained the existing CMS
state, but the proposed `after_json` was absent at the final model boundary.
The resulting verification was nevertheless stored against the revision hash.
A verifier could therefore issue a trusted PASS without inspecting the exact
change it was purportedly verifying. Separate human approval and deterministic
action validation still apply; this finding does not demonstrate an operator
forging a reviewer token.

Reproduction: create a legitimate title revision through the API, select live
verification with a local test replacement for `AgentRuntime._invoke`, return a
valid evidence-backed blind finding, and capture the subsequent `verifier`
payload. The initial source sent the old snapshot through evidence but omitted
the proposed after snapshot. The regression requires the complete stored
before/after snapshots and revision hash at the final model boundary, while
requiring the blind diagnosis to remain free of the proposed after state and
proposer rationale.

The correction now passes the immutable `revision_target` from the API through
the runtime to the final verification call. The independent test observes the
exact stored `before`, `after`, and `revision_hash` together in that target at
`AgentRuntime._invoke("verifier", ...)`. It also confirms that the proposed after
state and proposer rationale are absent from the blind diagnosis. The runtime
uses the before snapshot for the blind baseline and validates the target before
review. The implementation owners made this change; the reviewer changed only
the regression and this report.

Regression:
`test_actual_immutable_revision_reaches_final_verifier_without_anchoring_blind_review`.

## Boundaries independently exercised

| Boundary | Evidence from HTTP tests |
| --- | --- |
| Authentication | Missing, incorrect, and query-string capabilities fail; no configured capabilities locks the control plane while harmless health remains available. |
| Administrator separation | Operator and reviewer credentials cannot register sites. Even an administrator cannot smuggle autonomy, production enablement, or earned categories into registration JSON. |
| Approval and verification | Operator credentials cannot call human review or approval, including with forged role/actor headers. Reviewer and administrator proposals cannot count their own review as independent. |
| Body authority | Seven parameterized cases reject actor, action-kind/risk, provenance, verifier identity, revision binding, approval identity/expiry, and execution-enable spoofing. |
| Trusted verifier ingress | A raw PASS packet sent to the model-verification route does not become a stored trusted PASS; the route runs its configured verifier instead. |
| Site binding | Fourteen read, comparison, proposal, evidence, experiment, crawl, verification, approval, execution, and rollback requests reject foreign-site IDs before mutation. |
| Content and instructions | Submitted script syntax is stored as escaped plain text. Instruction-like hypothesis text remains an unverified `HYPOTHESIS` with zero confidence and cannot change site policy. |
| Link scope | A foreign-site destination is rejected; an inventoried same-site destination can produce a local contextual-link revision. |
| Approval freshness | Approval of one revision does not authorize another; an expired exact-revision approval cannot dispatch. |
| Fixed semantic routes | Malformed identifiers/values and unsupported mutation routes fail. Client-selected risk cannot turn `delete_page` into a low-risk or authorized action. |
| Error confidentiality | A provider failure containing a synthetic secret is absent from the execution response, canonical action events, and HTTP logs; the ambiguous write retains reconciliation status. |
| MCP integration | A real semantic MCP call forwards through `ControlClient` into the actual API. Execution is blocked before review/approval, succeeds after the independent human path, and an idempotent replay performs no second CMS write. |

No additional operator privilege-escalation or foreign-record binding bypass was
found in these exercised paths. The tests verify server behavior, rather than
relying on tool descriptions or the absence of a UI control.

## Scope limits

- Bearer capabilities are operator-wide. Cross-site record binding is not a
  per-user tenant-entitlement model.
- The tests do not independently exercise production PostgreSQL concurrency,
  deployment ingress/TLS, a real OAuth issuer, or real WordPress behavior. The
  earlier review and deployment gates still apply.
- The MCP integration case uses the local semantic server and a transport bridge
  into TestClient. Remote OAuth scope rejection is covered by the earlier
  independent suite, not claimed as a second live end-to-end OAuth test here.
- The verifier capture establishes exactly which state reaches the model
  boundary. It cannot establish the factual quality of a real model's judgment.
- The synthetic provider-secret test covers the execution error path; it is not
  a claim that every possible exception, deployment logger, or third-party
  service has been exhaustively audited.

## Reproduction

```sh
.venv/bin/python -m pytest tests/test_api_security.py -q
.venv/bin/ruff check tests/test_api_security.py
```

Initial independent run: **21 passed, 1 failed in 2.45 seconds**; API-01 was the
failing case. After the correction, the complete independent API suite passed:
**22 passed in 2.69 seconds**. Ruff also passed. The existing
`CrawlResult.schema` Pydantic name-shadowing warning was the only warning.
No reported finding remains open in this bounded API review.

## Subsequent budget and assembled-path review

The independent reviewer subsequently authored `tests/test_budget_security.py` before the execution quota interrupted its turn. The implementation owner ran those retained adversarial tests and fixed invocation replay/state regression, request-binding changes, daily action limits across cycles, and successful-revision queue selection. Two test setup issues were corrected explicitly: the specialist name now uses the actual `technical` role, and deliberate JSON type corruption is forced to persist because Python otherwise treats `True == 1.0` as equal.

Those budget tests now pass, including assertions that no SDK client opens before a committed reservation and that error/log records do not contain a synthetic secret. Follow-up global level/cost enforcement and configuration/forecast/adjudication route checks are implementation-owner tests, not claimed as a separate independent audit. The final complete suite is recorded in `VERIFICATION.md`.
