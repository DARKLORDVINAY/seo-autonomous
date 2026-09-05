# Durable-boundary hardening checkpoint

Date: 2026-09-04

Code commit: `7a612d006f938e79a485f9887539f96731e8390a`

Code tree: `702e0d9afce0f502c7d94174c3c757c34b8432a3`

## Decision

Retain Level 1, `PRODUCTION_ENABLED=false`, zero autonomous production-write
budget and zero paid-API budget. This checkpoint hardens verification,
database, migration, recovery and execution boundaries; it does not establish
detector competence, business impact, durable deployment readiness or Level-2
eligibility.

The failed, disclosed and retired v2 holdout remains the latest independent
detector result. The perfect v3 development split remains non-blind training
feedback. No new holdout was authored, scored, replayed or relabelled during
this work.

## Verified locally

| Gate | Result |
| --- | --- |
| Complete pytest suite | 1,060 passed, 0 failed, 10 infrastructure-gated skips, 1 known warning, 39.20 s |
| Security and untrusted-input slice | 191 passed, 0 failed |
| Scheduler/control-plane/database slice | 115 passed, 0 failed, 7 PostgreSQL-gated skips |
| GSC/GA4/evidence-semantics slice | 216 passed, 0 failed |
| Durability/rollback slice | 138 passed, 0 failed, 3 infrastructure-gated skips |
| Final capability regression slice | 255 passed, 0 failed, 2 PostgreSQL-gated skips |
| Ruff | passed |
| Python byte compilation | passed |
| Installed dependency consistency | passed |
| Git diff/whitespace validation | passed |
| CLI import/help smoke tests | passed; one pre-existing Pydantic warning |

The machine-readable JUnit report records 1,070 collected outcomes: 1,060
passed, 10 skipped, zero failures/errors and 39.185 seconds. Those suite fields
are the complete scope attested by JUnit. The warning count, targeted-slice
totals, Ruff, compileall, dependency, diff and CLI results are local
mission-governor/console reports; JUnit does not attest them.

The ten complete-suite skips are explicit: one actual dump/restore test, two
live PostgreSQL migration gates (the general audit and the direct-Alembic
hostile-default gate), two runtime-role tests and five PostgreSQL
scheduler-chaos tests. Docker/Compose and PostgreSQL are unavailable
in this environment. Those gates remain required for the exact complete
reviewed branch HEAD/tree that would be recorded before any approved upload; a
historical CI result for another commit is not evidence for that state.

## Recovery-integrity correction

The first local canonical follow-up action, identified in recovery history as
`94df1bd9-00ff-5cd9-b7dc-cc55553ce2a9`, used a legacy packet-hash convention
that excluded the top-level `production_write` and `external_mutation` flags.
The SQLite/database and archive digests still bound the stored bytes, and no
mutation was recorded, but that packet digest alone was not a sufficient safety
envelope. It is preserved as an append-only failure, not rewritten. Corrective
action `ba550723-7040-510f-8f5d-e1d9832f631b` first repaired that envelope;
the later full-graph correction
`30802c8a-bcef-58b1-8736-464e2a972551` repaired evidence and Claim-link debt.
External-scope correction `e262c420-f38e-5f3d-bfde-d3f117de240f` is now the
latest canonical action.

The latest packet SHA-256 is
`cc07455bf44a71ae275aeb3f11b3120499bdb9a4bf7e841e320a0ada2276d883`.
It covers sorted compact UTF-8 JSON with JSON-native values, including both
safety flags, and excludes only `checkpoint_packet_sha256`. Its before/after
mission hashes cover the explicitly registered projection—`available_resources`,
`unknowns`, `blockers`, `critical_path`, `resource_budget`, `autonomy_level`,
`phase` and `stop_condition`—not an unspecified raw row. The registered prior
action-record hash covers `id`, UTC ISO-8601 `created_at`, `kind`, `risk`,
`actor`, `reason`, `experiment_id`, `idempotency_key` and `payload_json` under
the same canonicalization contract.

The recovery audit also requires the ten skipped checks to be enumerated as
`1 + 1 + 1 + 1 + 1 + 5 = 10`: dump/restore, general live migration/audit,
direct-Alembic hostile-default, actual role split, runtime-role immutability,
and five scheduler-chaos cases. Machine-consumed recovery paths are
archive-root-relative and contain no parent traversal. The committed mission
document labels earlier remote CI by exact scope; the canonical database and
latest recovery receipt are authoritative only within their stated scopes.

The graph-correction action binds documentation commit
`95621273b2d37fc39f195a1abda2f7db87d2e6f1` and tree
`5a057b264c61d205d98c88a851ad51ee31925db0`. A later documentation-only
packaging/provenance commit, including receipt-scope clarification, must be
recorded separately as the recovery repository head. The external-scope action
binds predecessor documentation commit
`2b4b9a779db177b6397045243099a08dce2c7a6d` and tree
`4c134a27c3be608b3eccd3476c1049ebe341e3de`. This document's later packaging
commit is recovery material, not a rewrite of either database action.

### Canonical graph debt

A full-graph recovery audit then found that four historical Evidence rows use
the earlier spaced-JSON hash serialization rather than the current compact
serialization. A fifth historical Evidence row binds only its nested
observation and leaves outer authority/activity fields outside its stored hash.
One Claim also declared an Evidence ID in JSON without the corresponding
relational `ClaimEvidence` edge.

These are retained failures, not silently normalized. The append-only
correction records the exact historical hash schemes and digests, appends
five fully bound replacement Evidence rows, and adds the missing relational
edge without rewriting the Claim. After the external-scope action adds one
more compact fully bound Evidence row, the full graph records 48 Evidence
rows: 43 current compact full-content hashes, four valid legacy
spaced-JSON full-content hashes and one retained historical partial-content
hash that is invalid for full-content integrity. The five replacements remain
preferred, 47 of 48 rows validate under their registered full-content schemes,
and all 38 Claims match all 56 relational links. Receipts must keep the one
historical partial-content failure explicit rather than reporting an unscoped
`evidence_hash_valid=true`.

### Rejected v10 candidate and canonical external-scope correction

Independent receipt-scope review rejected candidate
`ff7b733300b572964045eb61a30f5afc86df4d58020e62bb3a8597a426ed18bd`
(4,437,065 bytes) before promotion. ZIP, Git, database-integrity, evidence-graph,
JUnit, lineage and chronology checks passed, but the authoritative SQLite
MissionState still exposed current-looking GitHub, Cloudflare, CI, browser,
GSC, GA4, public-release and PR values. It also targeted disclosure and CI at
the hardened code commit alone even though later recovery/provenance commits
were part of the reviewed branch. Because the API and MCP expose MissionState
directly, a DB-only restore could reconstruct stale claims and the wrong next
action; accurate archive documentation was not a sufficient overlay.

The candidate remains rejected and unpromoted. Action
`e262c420-f38e-5f3d-bfde-d3f117de240f` appended one Evidence, Claim,
ClaimEvidence, FailureCase, DecisionLog and ActionEvent, then updated the sole
mutable MissionState row in the same transaction. The pre-correction database
is preserved byte-for-byte at SHA-256
`96f7e4aa32936cb76d1a1c6531272076fce4a1c9ef4583e8ce6c8d5d334e2da3`;
the corrected database is
`3447efd037a62b0dad3fd1d6715c6a2cc18b74fdb9815806f40f6f7de4838d51`.
Integrity, foreign keys, exact eight-change transaction shape, idempotent
replay, safety state, full Claim parity and all registered Evidence schemes
pass. No external state was fetched or changed.

### Recovery provenance, lineage and chronology

Canonical Evidence whose source names an independent v9 archive review contains
a mission-governor summary of that review. The raw independent audit report is
not a recovery-archive member, so the review's provenance is not independently
reconstructable from members alone. Likewise, any
`flawed_action_unchanged` comparison that used the separately retained v9 ZIP
must say so; the embedded metadata alone cannot reproduce that comparison.

Version lineage is deliberately narrow. The v9 outer archive SHA-256 belongs in
the v10 outer manifest and post-construction receipt. Only v9's README and
`SHA256SUMS.json` are embedded under `previous-bundle/`; that directory is not
the complete v9 archive. The stale-manifest defect occurred in v8 and must not
be attributed to v9. V8 metadata may also be preserved for failure history,
with the same metadata-only scope.

Receipt timestamps describe different events. `checkpoint_recorded_at` is the
canonical database action/event time and can precede recovery construction.
`member_receipt_prepared_at` is captured only after all referenced members have
been built and locally verified. The outer manifest's `generated_at` is the
actual later time at which the complete staging-tree manifest is generated,
not the canonical action time. Because the ZIP does not yet exist while its
members are written, its final SHA-256 and clean-room verification require a
separate post-construction receipt outside the ZIP.

## Material hardening

- Benchmark attestations now bind observations, predictions and truth
  commitment in addition to evaluator definition/source/challenge/runtime.
  Passing also requires all ambiguous cases to receive an explicitly appropriate
  uncertain outcome, zero unsubstantiated candidates and six material scope
  limitations. The local reference runner remains unable to claim independence.
- Production database construction accepts only an explicit PostgreSQL
  username/database/host, the supported psycopg dialect and the reviewed
  `sslmode`/`gssencmode` query keys. Remote transport requires hostname-verified
  TLS. Ambient libpq overrides, nested `conninfo`, psycopg wrapper parameters,
  SQLAlchemy plugins, custom pools/factories and caller engine kwargs fail before
  driver connection.
- API and scheduler use separate least-privilege PostgreSQL roles. Worker GSC/
  GA4 refreshes can update only non-key observation columns. Site authority,
  approvals and authoritative verifications remain outside worker write scope.
  Startup/readiness/ticks reject role drift, dangerous defaults, object
  ownership, direct system ACLs, cross-schema routes and cross-database access.
- Fresh Compose volumes establish an application-dedicated PostgreSQL cluster.
  Existing/shared clusters fail closed until an authorized owner isolates their
  database ACLs; provisioning does not mutate unknown cluster-wide permissions.
- Every production owner migration requires an immutable image selector plus
  independent database, PostgreSQL system-identifier and predecessor-head pins.
  The exact DDL connection pins `search_path=public`, rechecks identity, obtains
  a nonblocking advisory lock and applies migration plus API/worker grants in one
  transaction. Production offline migrations are rejected.
- Direct ASGI startup performs an uncached exact API-role admission check. A
  request latch also blocks production traffic when a server disables lifespan
  handling, so the guarded container entrypoint is defense in depth rather than
  the only admission control.
- One immutable revision can produce at most one successful external execution.
  Reusing an approved CMS-draft revision with a new idempotency key is audited
  and blocked instead of creating another remote draft.
- Scheduler verifier output is a preview only; the worker cannot create an
  authoritative verification. Calibration may record an idempotent reduction
  recommendation but cannot change site authority.
- Backup receipt schema v3 publishes a private two-member bundle atomically,
  binds pre/post source observations, target/TLS/release/runtime/checkpoint
  identities and timestamps, fails on tool warnings, and requires independent
  verification pins including the archive SHA-256. Listing still never claims a
  successful restore.
- Failed reverse actions now receive their own terminal audit event and failed
  rollback record; the original successful action is not rewritten or
  misattributed.

## Failures retained

The canonical failure log retains each discovered gap, including incomplete
attestation component/scope gates, ambiguous-case and unsupported-candidate
acceptance, transport/plugin/identity overrides, over-broad or incomplete worker
capabilities, system/cross-database ACL exposure, readiness and direct-ASGI
bypasses, unpinned image/target/search-path migrations, non-atomic backup
publication, weak recovery identity binding, warning acceptance, rollback
misattribution, revision reuse and current-release CI overclaiming. Locally
actionable paths are remediated; external proof gaps remain open rather than
being converted to passing evidence.

## External state scope

No public site or repository was re-fetched during this checkpoint. These are
last-verified identifiers only, not claims about current external state:

- Public Test Lab URL: `https://seo-test-lab.pages.dev/`; last-verified Test Lab
  main: `01608070c2ed22de636a703a673ed4da46a00a9c`.
- Last-verified source main:
  `47c4359f0898129a1739b67917841c64c06690f6`.
- PR 6 was last verified open/unmerged at head
  `b6b91b20af690c6c972f7f8e223feadf43756331` on
  `2026-09-04T00:37:59Z`; its current state is unknown.

The mission governor reports no attempted hosting, Google, analytics,
paid-provider, public-site or repository modification in this checkpoint. That
is a scoped execution report, not global absence proof. Canonical state does
prove Level 1, `PRODUCTION_ENABLED=false`, zero write/spend authority and no
Level-2 activation.

## Remaining uncertainty

- Actual PostgreSQL catalogue/ACL, migration-transaction, trigger, fencing and
  column-grant behavior for the exact complete reviewed/disclosed branch HEAD
  needs the disposable live gate.
- The merged Compose stack, init hook, nonroot startup and immutable-image order
  need a current container/CI receipt.
- Remote hostname/CA failure behavior needs a controlled TLS endpoint.
- Backup recovery needs a real PostgreSQL dump/restore and power-loss/off-host
  durability exercise. The unsigned receipt is not hostile-source provenance,
  and a same-address proxy's middle backend is not cryptographically proven.
- A trusted image digest still depends on an independent supply-chain record.
- Detector competence still requires a freshly authored holdout evaluated by an
  independent immutable, kernel-isolated, network-denied runner.

## Exact next human-required critical action

Explicitly approve or decline disclosure of the complete local non-default branch
`hardening/blind-evaluation-isolation-v3-20260904`. The review must identify
`7a612d006f938e79a485f9887539f96731e8390a` as the hardened code ancestor and
include every later recovery/documentation provenance commit through the
then-current branch HEAD. Record that exact final HEAD/tree before any upload to
`DARKLORDVINAY/seo-autonomous`. Only after approval may a remote branch/PR and
disposable PostgreSQL/container CI run for that exact disclosed HEAD be created.
A later reviewed code freeze and independent fresh holdout are separate gates.
Merge, durable hosting, Google API connection and any autonomy change remain
separate human decisions.

This source-disclosure decision can be given in the conversation; no Google,
Cloudflare or hosting-account action is needed at this gate. The connector's
automatic approval review previously rejected the upload because it could not
verify the disclosure destination. Any renewed upload must use the complete
reviewed source state and the repository named above, within explicit approval.
