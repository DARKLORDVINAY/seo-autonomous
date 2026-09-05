# Blind-isolation and durable-runtime hardening checkpoint

Date: 2026-09-04  
Code commit: `157096a38bc06c2c25caabe70925829f33533ef9`  
Code tree: `fc04da6cff03341b40fd48d43114184f7d192d42`

## Decision

Retain Level 1, `PRODUCTION_ENABLED=false`, zero production-write budget and
zero paid-API budget. No new holdout was created, scored or replayed. The failed,
disclosed and retired v2 result remains the latest independent detector evidence;
the perfect v3 development split remains non-blind training feedback only.

The in-repository Python reference runner is now structurally unable to emit a
passing independent benchmark attestation. A passing attestation requires a new
holdout and an independent immutable kernel-isolated runner. Benchmark success
still cannot grant Level 2.

## Verified locally

| Gate | Result |
| --- | --- |
| Complete pytest suite | 928 passed, 0 failed, 8 infrastructure-gated skips, 1 known Pydantic warning |
| Untrusted-input/security group | 268 passed, 0 failed |
| Scheduler/control-plane chaos group | 75 passed, 0 failed, 6 PostgreSQL-gated skips |
| GSC/GA4/evidence-semantics group | 206 passed, 0 failed |
| Ruff | passed |
| Python byte compilation | passed |
| Installed dependency consistency (`pip check`) | passed |
| Diff whitespace validation | passed |

The eight complete-suite skips are only the real PostgreSQL role, backup/restore
and cross-backend chaos gates. Docker, Podman and a PostgreSQL server are absent
from this execution environment. User/network namespaces could not be created,
so this checkpoint does not claim fresh container or kernel-sandbox execution.
The previous disposable CI PostgreSQL/container result remains historical evidence
for its earlier tested commit, not for this local commit.

## Material hardening

- Split legacy truth loading/scoring out of the runtime service and added built-
  image gates against evaluator files and symbols.
- Removed eager detector imports from the SEO package. Importing the evaluator
  protocol no longer loads the production detector or analysis module.
- Replaced enumerable private-truth digests with keyed HMAC commitments.
- Bound benchmark definitions to scorer module/callable source and froze staged
  predictor bytes from one source snapshot. Mutable scorer globals and installed
  artifacts remain an external-runner concern.
- Recursively rejects separator and camel-case variants of truth, label, policy,
  budget and authority keys in metadata, JSON-LD and issue objects.
- Added bounded child output files, resource limits, sanitized environment,
  Python startup isolation and capability probes. These are defense in depth,
  not a kernel security boundary.
- A local reference attestation is explicitly non-independent, carries isolation
  limitations and cannot claim a passing engineering gate.
- A passing aggregate requires minimum corpus composition and an independent
  immutable kernel-isolated profile.
- Administrator import now pins evaluator key, definition, source, evaluation,
  challenge, execution environment and freshness. It remains fixture evidence
  with no MCP or authority-graduation path.
- Redacted legacy truth hashes and private record hashes from API, strategy, MCP
  and agent-facing projections.
- Removed human/admin bearer capabilities and benchmark-import material from the
  worker; executable mode forces the service role.
- Worker readiness now checks heartbeat, database reachability, current migration
  head and production runtime privileges.
- Remote PostgreSQL configuration fails closed without `sslmode=verify-full`;
  local Compose database hosts remain supported.
- Added non-overwriting private backup receipt verification. Archive integrity is
  not a restore-success claim; the disposable restore gate remains separate.

## Failures found and retained

1. Runtime image could contain legacy evaluator logic.
2. General read surfaces could expose legacy truth-derived hashes.
3. Private truth commitments were enumerable bare hashes.
4. Nested camel-case keys could bypass the reserved-label filter.
5. Importing the protocol indirectly loaded production analysis.
6. Worker inherited reviewer/administrator bearer capabilities.
7. Attestation import did not bind one intended challenge/runtime or reject stale
   valid attestations.
8. The reference runner could be mistaken for a kernel-isolated reproducible
   evaluator.
9. GitHub source upload was blocked because the connector could not verify the
   disclosure destination. No tree, commit, ref, branch, PR or deployment changed.

The locally fixable failures above are remediated in the code commit. The external
kernel/dependency-artifact boundary and remote CI remain unresolved, explicitly
represented limitations.

## External state preserved

- Public Test Lab: `https://seo-test-lab.pages.dev/` (unchanged).
- Public Test Lab repository main: `01608070c2ed22de636a703a673ed4da46a00a9c`
  at the last verified checkpoint; not modified here.
- Source repository main: `47c4359f0898129a1739b67917841c64c06690f6`
  at the last verified checkpoint; not modified here.
- Existing source PR 6 remains open/unmerged at its last verified head
  `b6b91b20af690c6c972f7f8e223feadf43756331`. This local hardening commit is not
  on that PR and has no remote CI result.

## Exact next human-required critical action

When account actions are allowed again, explicitly approve or decline disclosure
of this source diff to `DARKLORDVINAY/seo-autonomous` on a non-default branch.
Only after that approval may a new branch/PR and disposable PostgreSQL/container
CI be created. After a reviewed code freeze, a genuinely independent evaluator
must author a fresh holdout and run the predictor in an immutable, network-denied,
kernel-enforced environment with no truth or signing material mounted. PR merge,
durable hosting, Google API connection and any autonomy change remain separate
human gates.
