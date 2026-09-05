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

The JUnit artifact independently records 1,070 collected outcomes: 1,060
passed, 10 skipped, and zero failures/errors. The single known-warning count is
from the captured pytest/CLI console summary, not a field attested by JUnit.

The ten complete-suite skips are explicit: one actual dump/restore test, two
live PostgreSQL migration gates (the general audit and the direct-Alembic
hostile-default gate), two runtime-role tests and five PostgreSQL
scheduler-chaos tests. Docker/Compose and PostgreSQL are unavailable
in this environment. Those gates remain required for this exact commit; a
historical CI result for another commit is not evidence for this tree.

## Recovery-integrity correction

The first local canonical follow-up action, identified in recovery history as
`94df1bd9-00ff-5cd9-b7dc-cc55553ce2a9`, used a legacy packet-hash convention
that excluded the top-level `production_write` and `external_mutation` flags.
The SQLite/database and archive digests still bound the stored bytes, and no
mutation was recorded, but that packet digest alone was not a sufficient safety
envelope. It is preserved as an append-only failure, not rewritten. The
superseding canonical receipt hashes every substantive payload field—including
both safety flags—and excludes only the digest itself.

The recovery audit also requires the ten skipped checks to be enumerated as
`1 + 1 + 1 + 1 + 1 + 5 = 10`: dump/restore, general live migration/audit,
direct-Alembic hostile-default, actual role split, runtime-role immutability,
and five scheduler-chaos cases. Machine-consumed recovery paths are
archive-root-relative and contain no parent traversal. The committed mission
document labels earlier remote CI by exact scope; the canonical database and
latest recovery receipt remain authoritative for post-commit state.

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

## External state preserved

- Public Test Lab: `https://seo-test-lab.pages.dev/` (not modified).
- Public Test Lab repository main remains at its last verified checkpoint
  `01608070c2ed22de636a703a673ed4da46a00a9c`; not re-fetched or changed here.
- Source repository main remains at its last verified checkpoint
  `47c4359f0898129a1739b67917841c64c06690f6`; not re-fetched or changed here.
- Existing source PR 6 remains open/unmerged at its last verified head
  `b6b91b20af690c6c972f7f8e223feadf43756331`; not re-fetched or changed here.
- No hosting, Google, analytics, paid-provider, public-site or account action was
  attempted. No production mutation or Level-2 activation occurred.

## Remaining uncertainty

- Actual PostgreSQL catalogue/ACL, migration-transaction, trigger, fencing and
  column-grant behavior for this exact commit needs the disposable live gate.
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

When account actions are allowed again, explicitly approve or decline disclosure
of local branch `hardening/blind-evaluation-isolation-v3-20260904` through commit
`7a612d006f938e79a485f9887539f96731e8390a` to
`DARKLORDVINAY/seo-autonomous` on a non-default branch. Only after approval may a
remote branch/PR and disposable PostgreSQL/container CI run be created. A later
reviewed code freeze and independent fresh holdout are separate gates. Merge,
durable hosting, Google API connection and any autonomy change remain separate
human decisions.
