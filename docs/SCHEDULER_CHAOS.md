# Scheduler/control-plane chaos verification

Checkpoint: 2026-09-03. This is an isolated, accelerated verification suite, not
evidence of an always-on production deployment. It does not access the public lab
database, Google accounts, hosting accounts, a paid API or a live CMS.

## Authority and isolation

`tests/test_scheduler_chaos_v2.py` constructs disposable databases and local
providers with `autonomy_level=1`, `production_enabled=false`, `shadow_mode=true`,
zero daily production-action budget and zero paid-model cost/call allowance.
Site, mission and runtime settings are independently checked. Both default
httpx transports and the crawler's custom `PublicNetworkBackend.connect_tcp`
egress are blocked; only explicit `httpx.MockTransport` provider simulations can
return web responses. Configured PostgreSQL driver sockets remain available to
the gated database tests. No test invokes an OpenAI SDK model call.

Most tests use file-backed SQLite. Five cross-backend cases also run against
PostgreSQL **only** when `TEST_POSTGRES_URL` is already configured for a disposable
test database. Each uses a generated `chaos_v2_<uuid>` schema, separate sessions
and connections, and removes only that schema. Do not point this variable at a
production database. SQLite is not presented as PostgreSQL concurrency proof.

## Reproduced failures and minimal corrections

| Defect reproduced | Before hardening | Correction |
| --- | --- | --- |
| Failed duplicate ticks reset the effective provider retry allowance | Twelve duplicate ticks produced 36 HTTP attempts, both for timeouts and HTTP 429 | Maximum three durable observation attempts per site/job/scheduled period; count immutable attempt actions under the site lease |
| Stale ORM state bypassed the under-lease completion recheck | A failed job completed by another worker between optimistic read and acquisition was executed again | Refresh the JobRun from SQL with `populate_existing=True` after acquiring the lease |
| Interrupted daily cycle stayed apparently running forever | A replacement returned the abandoned `running` record indefinitely | Record one `control_loop_interrupted` failure and audited `reconciliation_required` state, preserve partial results, and do not replay an uncertain paid/executor-capable cycle |
| Independent review caught a recovery-hardening regression before integration | A cached `running` cycle could overwrite another session's committed completion/result with `reconciliation_required` | Refresh the daily-cycle JobRun from SQL before making any recovery decision; preserve the independently committed terminal result exactly |

The first two defects and the misleading daily-cycle state were demonstrated by
failing regression tests before their fixes. The daily-cycle lookup is also
scoped to `job_name="seo-control-loop"`, so an unrelated observation job cannot
answer a same-key daily request. CLI one-off runs now return a nonzero exit code
for `retry_exhausted` and `reconciliation_required` instead of reporting success.
The fourth row was introduced by the new recovery branch, identified by an
independent reviewer, reproduced as a failing two-session regression, and fixed
before integration. It is not represented as a defect discovered in a deployed
release.

The hard observation cap is `MAX_OBSERVATION_ATTEMPTS=3` in `scheduler/jobs.py`;
it is not an agent-selectable setting. New worker identities, process restarts
and duplicate ticks do not reset it. The scope is an actual scheduler period,
not a newly generated idempotency key. A later scheduled day/week is a new
observation period, not permission to retry or duplicate an external mutation.

## Coverage and recorded workload

| Test family | Workload / injected fault | Required result |
| --- | --- | --- |
| Accelerated observation soak | Seed `20260903`; 28 virtual days; 336 ticks across four logical worker identities | 61 logical jobs, 275 no-op duplicate replays, 61 starts/completions, zero failures, zero AgentRuns |
| Real worker contention | Eight simultaneous OS threads; shared SQL lease; real weekly review | One completed operation, seven `lease_busy` outcomes, one decision and one attempt action |
| Lease churn | Seed `20260904`; 200 lease owners/reclaims cycling through 12 names; 100 owners disappear without releasing | Fencing tokens increase from 1 through 200; sampled stale owners cannot renew, release or own a replacement lease |
| Committing stale worker | Expire and replace a lease through a second SQL session, then attempt the original worker's commit | `LeaseLost`, complete rollback of the stale change, replacement lease preserved |
| Real process crash | POSIX child calls `os._exit(23)` after its start record commits and an operation write flushes | Uncommitted write disappears; active lease blocks early retry; expiry permits a bounded repeatable observation retry on the same JobRun |
| Late result | An operation returns only after another worker acquired the expired lease | No stale completion/decision persists; interruption remains explicit before safe read/review retry |
| Cached daily recovery | One session retains `running` while another commits completion and its result | Recovery refreshes SQL state, replays the exact completed result, and creates no false interruption or overwritten outcome |
| Completion storage fault | Raise at the SQL statement inserting the completion event | Partial operation writes roll back; failure event persists; next attempt yields one completed decision |
| Failure-storage outage | Both completion and failure-event inserts fail | No false success; durable start remains unknown; recovery records interruption once |
| Heartbeat and cleanup faults | Renewal SQL fails; separately, release SQL fails after a successful commit | Heartbeat loss blocks the next commit; cleanup failure cannot erase a successful transaction or permit premature takeover |
| Provider timeout / rate limit | Real HTTP retry helper over local transport; 12 duplicate ticks per fault | Three job attempts, at most nine transport calls, six virtual backoffs, one durable exhaustion event |
| Zero budget | 32 new reservation IDs, each replayed three times | 32 denied records, not 96 reservations; zero paid-call/cost reservation and no automatic allowance reset |
| Perfect synthetic calibration | Thirty deliberately perfect fixture outcomes | Review may maintain existing authority, never promote it or earn a production category |
| Timezone repeat | Both occurrences of 01:30 during New York's DST fall-back | One daily job and one no-op replay, not two scheduled observations |

The soak's four worker identities are logical serial identities; the separate
eight-thread test is the actual concurrent execution check. Virtual clock
advancement replaces elapsed days and lease waiting. Thread/process barriers
have fixed deadlines of at most eight seconds; no test uses a sleep loop.

## Verification commands and results

Focused verification on 2026-09-03:

```bash
.venv/bin/pytest -q tests/test_scheduler_chaos_v2.py tests/test_operations.py tests/test_budget_security.py tests/test_database.py tests/test_acceptance.py
.venv/bin/ruff check backend/app/scheduler/jobs.py backend/app/scheduler/worker.py backend/app/services/control.py tests/test_scheduler_chaos_v2.py
git diff --check
```

Result: **121 passed, 7 skipped in 10.68 seconds**; lint and whitespace checks
passed. Five skips are the new actual-PostgreSQL cases, and two are pre-existing
PostgreSQL database/role gates. `TEST_POSTGRES_URL` was not present in this local
verification environment. A pre-existing Pydantic `schema` field-name warning
remained. The new suite contributes 21 local checks and five gated PostgreSQL
checks. Elapsed test time is diagnostic, not a production performance claim.

## Failure and restart semantics

Every admitted observation attempt has an immutable action plus start event
before the operation begins. An operation that cannot commit does not become a
successful observation. On a restart, an abandoned start becomes an explicit
`scheduler_interrupted` event and `scheduled_observation_interrupted` failure
before a repeatable read/review is retried. Failed attempts remain in history.

After three attempts, the same period becomes `retry_exhausted`; duplicate ticks
only replay that terminal state. Exhaustion appends a failure/event once and
requires diagnosis. The routine cannot erase the attempt history, change its
own budget or silently fabricate a fresh same-period key.

The daily control loop is intentionally different: its stages may have already
committed observations, task decisions, reservations, or separately approved
execution intents. An abandoned cycle is marked `reconciliation_required`,
never blindly restarted. Inspect canonical stage, reservation and executor
records before authorising a new run. Failure-record persistence itself is
impossible during a complete DB outage; the last committed start plus safe
process error is the available evidence until storage recovers.

## Limits and stopping decision

This suite proves the stated paths under bounded simulations and, where run,
real SQL transactions. It does **not** prove distributed PostgreSQL behavior
until the gated cases execute there; WAN partitions, database failover, clock
skew across independent hosts, container orchestration and long-duration memory
behavior remain untested. Leases still depend on appropriately synchronized
host clocks. The real process-crash case runs on POSIX SQLite, not a killed
PostgreSQL server. SQL fault injection exercises rollback without claiming an
actual PostgreSQL outage.

Provider-quality/attribution staleness is handled by the separate analytics
fixture verification; this suite's stale-result checks concern lease ownership
and uncertain job outcomes. No synthetic calibration record qualifies the live
site for Level 2, and benchmark success never changes authority.

Marginal-value stop: ownership races, bounded retries, crash ambiguity,
transaction rollback, disabled write/spend gates and timezone duplicate paths
are covered. More arbitrary ticks would add little risk reduction. Next useful
verification is the already-defined PostgreSQL gate in an approved test
environment, followed by provider-neutral operational recovery exercises when
durable infrastructure is available. No external hosting or account creation
is part of this test package.
