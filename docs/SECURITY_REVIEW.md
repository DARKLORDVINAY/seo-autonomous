# Independent adversarial security review

Review date: 2026-09-01. Scope: the actual Python implementation of the control
plane, deterministic policy, execution and rollback, canonical audit records,
crawler transport, WordPress adapter, API bearer authentication, and MCP OAuth.
This is a bounded source review and local adversarial test run. It does not
certify a production deployment or a real WordPress installation.

The reviewer owned only this report and `tests/test_security_review.py`.
Implementation fixes were routed to the module owners. Test doubles made no
real CMS writes, network requests, or paid model calls. The simulated-live CMS
deliberately advertises `is_fixture=False` to exercise production authorization
checks without touching a real website.

## Findings and disposition

| ID | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| SEC-01 | High | Earned Level 2 authority overrode a later explicit human REJECT or REVOKE | Fixed by safety owner; independent regression passes |
| SEC-02 | High when remotely deployed with a weak token | Production configuration accepted one-character administrator/reviewer capabilities | Fixed; independent regression passes |
| SEC-03 | High as a decision-integrity weakness | Execution checked quality only at evidence root while ingestion stored quality in nested batch observations | Fixed by safety owner; independent regression passes |
| SEC-04 | Medium, SQLite only | `INSERT OR REPLACE` silently rewrote append-only evidence through SQLite implicit-delete behavior | Fixed; independent regression passes |
| INT-01 | Operational blocker, fail closed | Ingestion evidence owner and conversion-confirmation field differed from executor expectations | Corrected in source; full API acceptance remains root-owned |

### SEC-01: explicit human veto

Reproduction: prepare an exact title revision, record trusted independent
verification, approve it, select Level 2 with earned `update_title`, then append
a correctly bound `Approval` with `decision=REJECT` or `decision=REVOKE`.
Execution initially returned `succeeded` and made one simulated-live CMS write.
`_approval_valid` treated a veto as absence of approval, after which automatic
authority was sufficient to proceed.

Safe fix: check the latest correctly bound human decision as a separate veto
before every dispatch. REJECT and REVOKE remain blocking until superseded by a
later affirmative decision; expiration of a rejection does not grant authority.
The safety owner implemented this in `_human_veto_active` and the policy path.

Regression:
`test_explicit_human_veto_also_blocks_earned_level_two`, both decision variants.

### SEC-02: weak production capability tokens

Reproduction: instantiate production `Settings` with PostgreSQL, a 32+
character API token, and `admin_token="x"`. Configuration initially accepted it,
and `api/auth.py` treats that exact value as administrator authority. A
one-character approval token was also accepted while production writes were
globally disabled. Such a deployment exposes an easily guessed privileged
credential regardless of the strength of its operator token.

Safe fix: enforce the minimum strength requirement on every configured
production authority token, including administrator and reviewer credentials,
and retain the existing distinct-token rule. Use randomly generated credentials;
length validation alone cannot measure entropy. Production HTTPS and external
rate limiting remain deployment controls.

The implementation now validates every configured production administrator and
reviewer token at 32+ characters as well as preserving operator-token validation.

Regression: `test_production_rejects_weak_high_authority_tokens`.

### SEC-03: nested observation quality

Reproduction shape: an otherwise trusted canonical evidence record contains
`rows=[{"data_state":"partial"}]` or batch `quality_flags=["tracking_outage"]`.
The ingestion service actually stores observations in this shape. The initial
execution check inspected only `evidence.content.data_state`, so nested
incompleteness was not a deterministic blocker. A mistaken trusted verifier
PASS could consequently authorize an action using known-inadequate evidence.
This is a defense against mistaken model judgments, not a claim that an
untrusted caller can directly insert verification packets.

Safe fix: parse the supported structured observation shapes, preserve quality
flags through the canonical-to-agent boundary, and reject material partial,
missing, suppressed, or inconsistent observations in deterministic code. Do not
interpret prose or crawled instructions as configuration. Distinguish GSC's
documented incomplete query population from an incomplete time window.
`guardrails/evidence.py` now implements the structured checks.

Regression: `test_batch_quality_defects_cannot_authorise_live_write` covers
nested partial/unknown state, tracking outage, privacy thresholding, and nested
metadata outage, with a valid complete-evidence control case.

### SEC-04: SQLite implicit replacement

Reproduction: use `INSERT OR REPLACE INTO evidence ... SELECT ... FROM evidence`
with an existing evidence ID and a changed source. SQLite initially allowed it
despite the UPDATE/DELETE immutability triggers. The implicit deletion performed
by REPLACE does not invoke delete triggers unless recursive triggers are enabled.
The regression operates on an isolated in-memory database.

Safe fix: enable `PRAGMA recursive_triggers=ON` for every SQLite connection or
add an equivalent collision-rejecting insert trigger. Continue prohibiting raw
SQL through control-plane tools. This finding requires database write access;
the review did not discover a remote arbitrary-SQL endpoint. PostgreSQL's
separate trigger implementation is not implicated by this reproduction.

The SQLite connection hook now enables recursive triggers. The same replacement
statement is rejected by the append-only trigger in the independent test.

Regression: `test_sqlite_replace_cannot_silently_rewrite_append_only_evidence`.

### INT-01: integration mismatches

At review time `control.record_evidence` used owner `data-observer`, absent from
the executor's default trusted owner set. Site registration and analysis used
`conversion_definition.verified`, whereas execution required `.confirmed`.
Both inconsistencies blocked activation rather than creating an authorization
bypass. Root was notified to establish one canonical contract and to run an
end-to-end acceptance test using the real ingestion service.
The site registration now explicitly trusts `data-observer` and execution uses
the same canonical `.verified` conversion-confirmation key as registration and
analysis; the reviewer checked these source corrections.

## Independently exercised boundaries

- Simulated-live execution succeeds only through the complete evidence,
  verifier, approval, experiment, production-enable, snapshot, and atomic-CAS
  path; replay does not perform a second write.
- Foreign-site evidence is rejected; SQL changes to immutable revisions are
  rejected; local metadata drafts never call the CMS or start an experiment.
- Later approval revocation blocks Level 1. Explicit human veto blocks Level 2
  after SEC-01's fix.
- An adapter lacking atomic compare-and-swap is blocked before any live
  existing-page write. WordPress core remains in this blocked category.
- Loopback, private and link-local URL targets, mapped IPv6 loopback, URL
  credentials, ambiguous backslashes, and nonstandard ports are rejected.
- Mixed public/private DNS answers never reach the socket connector. A crawler
  redirect toward link-local metadata does not issue the redirected request.
- OAuth signatures are checked against a pinned public key, expected issuer,
  audience, subject allowlist, expiry, issued-at time, and scope. Wrong claims,
  unsigned tokens, and HMAC algorithm substitution are rejected.
- A valid remote MCP read token cannot invoke proposal or execution tools. MCP
  semantic tools use validated UUID path segments, and expose no approval,
  arbitrary SQL, or shell capability.

The implementation team's separate tests also exercise stale CMS snapshots,
atomic last-window conflicts, ambiguous writes with retained leases, no blind
retries, exact rollback and stale rollback conflicts, unsupported dangerous
action kinds, prompt-injection examples, and database failure before dispatch.
Those tests support this review but do not replace the independent cases above.

## Limits and activation conditions

1. HTTP route assembly was still being integrated during the first review
   pass. Root must verify that every approval and site-configuration route uses
   the separate authority dependency and that no public route accepts arbitrary
   verifier packets. `record_verification` is a trusted internal ingress.
2. The independent suite uses SQLite and in-memory providers. Actual PostgreSQL
   concurrent execution, migration permissions, crash recovery and TLS/OAuth
   deployment require their separate integration gates. Local tests do not prove
   the behavior of a hosting platform or a third-party CMS plugin.
3. WordPress core does not offer the atomic fingerprint precondition required by
   this executor. Live existing-page writes stay blocked until a reviewed atomic
   adapter is available. Draft creation and read-only integrations are distinct
   capabilities. A new CMS draft cannot currently be reversed automatically by
   deleting it because deletion is an unsupported high-risk operation.
4. The pinned-key MCP verifier needs an operator-provided authorization server
   and public key. It is not itself an OAuth authorization server. Remote MCP
   is not configured, connected to GPT Work, or independently tested here against
   a real identity provider. Key rotation needs an operational procedure.
5. Bearer capabilities are operator-wide, not a per-user, per-site SaaS access
   model. The review verified cross-site record binding, not individual tenant
   entitlement mapping. A DB administrator can change schemas and permissions;
   audit triggers are not protection against a malicious DBA.
6. No frontend browser execution, external penetration test, compromised
   WordPress plugin test, or real prompt-injection model experiment occurred.
   Agents have no execution tools, and deterministic capability checks remain
   the final authorization boundary.

## Reproduction command

```sh
.venv/bin/python -m pytest tests/test_security_review.py -q
```

Final independent run: **35 passed in 1.72 seconds**. The only warning was the
existing Pydantic `CrawlResult.schema` name-shadowing warning. All four reported
security issues have a passing independent regression after their fixes. The
review made no live-site calls. Do not interpret this local suite as permission
to enable production writes or as proof of external deployment safety.
