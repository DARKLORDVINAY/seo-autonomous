# Release verification — 2026-09-01

**631 passed, 2 skipped, 0 failed.** Full pytest run: 18.64 seconds. Ruff and JavaScript syntax checks passed. SQLite migrations apply and Alembic reports no drift. Machine-readable details are in `VERIFICATION.json`.

## Exercised

- The assembled API ingests fixture CMS/GSC/GA4/crawl data, persists four inventoried URLs, produces five ranked opportunities, runs bounded fixture specialists and returns NO-ACTION with production disabled.
- Canonical data and the job result survive a new process/session. Replaying the same cycle ID does not duplicate the work or charge a model.
- A title draft is blocked before approval, independently reviewed, approved, applied to an atomic in-memory CMS and read back. A separately reviewed/approved exact inverse restores the original fingerprint. Before/after and action events persist.
- The actual MCP stdio protocol initialises, lists **37 tools**, calls health and reads canonical fixture state through a local HTTP API process. It does not expose SQL, shell or human approval capabilities.
- Separate operator/reviewer/administrator boundaries, foreign-site IDs, stale approvals, human vetoes, prompt injection, hostile HTML, SSRF/redirects, unknown metrics, outages, partial data, timeouts and ambiguous CMS writes are covered.
- Independent budget regressions verify durable admission before SDK spend, immutable call binding, denied replay, no terminal-state regression, conservative crash reservations, and daily action limits across cycles.
- Immutable forecast/measurement/adjudication tests reject retrospective confidence changes, counterfeit owner/hash bindings and self-adjudication. Fixture success cannot earn live calibration. Poor sufficiently sampled calibration removes earned categories.
- Backend integrity, evening measurement and weekly review jobs completed once against the persisted fixture database with canonical leases/job records. Their recurrence is configured in the backend worker; no durable external worker host is claimed.
- Dashboard assets return correctly from FastAPI; the assembled acceptance test caught and fixed an incorrect asset root. Complete dashboard rendering and responsive layout still require the browser gate below; the actual Pages preview home DOM for the Test Lab was inspected successfully.

## Gates not passed here

| Gate | Reason / next step |
| --- | --- |
| Actual PostgreSQL migration, locks and append-only triggers | No local PostgreSQL runtime; run the delivered CI service gate |
| Restricted PostgreSQL runtime login | Second explicitly skipped test; run with `TEST_POSTGRES_URL` |
| Compose image build and service startup | Docker unavailable here; delivered CI includes nonroot startup and repeatable bootstrap |
| Browser rendering | Chromium download failed through available network; Playwright script and CI screenshot gate supplied |
| Real OAuth/Work remote MCP | Needs a selected HTTPS host, issuer and account registration |
| Live GSC/GA4/LLM/SERP | Public static lab selected; provider read identities and live-model budget are still absent |
| Live WordPress existing-page execution | Needs an atomic site-side adapter and real restore drill; the ordinary adapter fails closed |
| SEO effectiveness and Level 2 graduation | Needs real shadow decisions and subsequent qualified outcomes; not established by fixtures |

One known, non-failing warning remains: `CrawlResult.schema` shadows the Pydantic base method. The public crawl contract is unchanged. It does not represent a skipped safety check.

The independent review reports document their original scope. Later implementation-owner integration fixes were verified against their retained tests; this is not represented as a new independent live audit. The autonomous executor has made no production change. Separately, user-authorised manual GitHub operations have created the Test Lab release PR and its Cloudflare preview deployment; those operations have immutable canonical action receipts. No paid account was opened.

## Controlled Lab checkpoint

The reviewed 26-page source produces the expected 13 structural issue units and 13 correct clean-page NO-ACTION decisions against frozen artifact observations. The independent evaluator review added nine regressions and fixed unsupported overlap scoring and secondary-pair NO-ACTION counting. Four operator-audit tests verify intent, no authority change, replay refusal and scope limits. The local artifact rollback restored the entire Git tree byte-for-byte. These results do not prove public rollback, live model quality or commercial value.

The two existing GitHub repositories and Pages Git integration are now accessible. Public release PR #1 has a successful Cloudflare Pages preview. An additional pre-existing Workers mirror build failed; its cause is not exposed by the GitHub receipt. Pages is the selected deployment target. Actual infrastructure/public gates are recorded only after execution; the dedicated public workflow uses disposable PostgreSQL and cannot substitute for a durable host.
