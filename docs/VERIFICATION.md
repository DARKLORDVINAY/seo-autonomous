# Release verification — 2026-09-01

September 2 continuation: the dedicated GA4 stream and its persisted privacy settings are verified. The reviewed opt-in static tag release is deployed; receipt is still unverified. See [GA4_ACTIVATION.md](GA4_ACTIVATION.md). The dated test and rollback figures below remain historical evidence, not a claim that Google ingestion is connected.

**633 passed, zero skipped, zero failed** on [actual PostgreSQL/container CI](https://github.com/DARKLORDVINAY/seo-autonomous/actions/runs/33568223519). The earlier local run passed 631 tests and explicitly skipped two PostgreSQL gates; both ran successfully in CI. Ruff, JavaScript syntax, migration drift and MCP protocol gates passed.

The [26-page public Test Lab](https://seo-test-lab.pages.dev/) passed its real HTTPS structural benchmark and rendered-browser checks. A temporary title revision was deployed, observed and restored through independently reviewed GitHub PRs. [Final public verification](https://github.com/DARKLORDVINAY/seo-autonomous/actions/runs/33571682933) reported 13 true positives, zero false positives/negatives, 13 correct NO-ACTION decisions, complete coverage and all high/critical probes intercepted. Details, limits, hashes and deployment identifiers are in [TEST_LAB_RESULTS.md](TEST_LAB_RESULTS.md).

## Exercised

- Persistent canonical tables, idempotent cycles, immutable evidence/actions and actual PostgreSQL migration/role/lease enforcement.
- Fixture CMS/GSC/GA4 ingestion, deterministic diagnosis, bounded specialist contracts, independent review and guarded exact revisions.
- Approved fixture change/readback/rollback; actual public Git A-to-B-to-A restoration with zero autonomous production writes.
- Actual MCP stdio initialisation and 37 semantic tools; no SQL, shell, human-approval or authority-escalation tool.
- Tenant isolation, stale approvals, vetoes, prompt injection, hostile HTML, SSRF/redirects, partial data, tracking outages, timeouts and ambiguous writes.
- Durable SDK reservations, immutable model-call binding, replay protection and conservative crash accounting.
- Independent outcome adjudication and calibration tests; fixtures cannot earn autonomy and poor calibration can remove authority.
- Nonroot Docker/Compose API, worker and database startup; restricted-role bootstrap repeated without duplicating state.
- Dashboard and lab rendering, desktop/mobile layout, all 26 public pages, local-only test interaction, no unexpected analytics requests and true HTTP 404.

## Remaining gates

| Gate | Current state |
| --- | --- |
| Durable PostgreSQL/container service | Package verified in disposable CI; no permanent owner host or backups connected |
| Recurring real ingestion | Backend scheduler implemented; durable host required |
| GSC / GA4 | Browser ownership and dedicated stream/privacy settings verified; opt-in tag deployed; backend read identity, test-key-event registration and event receipt pending |
| Live model / paid SERP | Credentials and explicit budget missing; no live model calls made |
| Remote Work MCP | Deployed HTTPS service and OAuth issuer/client registration required |
| Commercial WordPress mutation | No business site connected; atomic adapter and site-specific restore gate still required |
| SEO impact / Level 2 | No qualified business outcome or sufficient independent calibration; disabled |

One known non-failing warning remains: `CrawlResult.schema` shadows a Pydantic base method. The workflow runner also reports that older action versions are forced onto Node 24; the recorded runs passed. This checkpoint does not assert future compatibility without CI.

The local public-DNS failure, separate Workers preview failure and limited binary artifact transfer are recorded in the Test Lab results. They are not relabelled as passing gates. Full disposable CI database artifacts and local operator state are distinct evidence sources. No paid account was opened.
