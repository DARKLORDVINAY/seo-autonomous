# Controlled public SEO Test Lab — verified checkpoint

The [26-page demonstration](https://seo-test-lab.pages.dev/) is publicly deployed through GitHub and Cloudflare Pages. The structural benchmark passed, and a reviewed title change was observed live and successfully restored. **Level 1 remains active; the autonomous production executor is disabled.**

This development benchmark does not establish SEO uplift, qualified conversions, generalisation to other sites, or live model reasoning quality. The site clearly identifies itself as an educational test project and makes no customer or commercial claims.

September 2 continuation: [GA4_ACTIVATION.md](GA4_ACTIVATION.md) records the newer opt-in analytics release. The A/B/A table below is preserved as the September 1 benchmark/rollback record. Current release pins are in `test_lab/public_target.json`; new public observations must use those pins.

## Observed results

| Measure | Temporary revision B | Restored baseline A |
| --- | ---: | ---: |
| Public pages verified | 26 | 26 |
| True positives | 13 | 13 |
| False positives | 0 | 0 |
| False negatives | 0 | 0 |
| Precision / recall | 100% / 100% | 100% / 100% |
| Correct clean-page NO-ACTION decisions | 13 | 13 |
| False NO-ACTION decisions | 0 | 0 |
| Observation coverage complete | Yes | Yes |
| High/critical guardrail probes intercepted | Yes | Yes |
| Autonomous production changes | 0 | 0 |
| Level 2 eligible | No | No |

Sources: [B public run](https://github.com/DARKLORDVINAY/seo-autonomous/actions/runs/33570806358), [restored public run](https://github.com/DARKLORDVINAY/seo-autonomous/actions/runs/33571682933). The [initial A run](https://github.com/DARKLORDVINAY/seo-autonomous/actions/runs/33569077349) also passed and reported precision/recall 1.0 with zero FP/FN; its original console format did not emit individual true-positive or NO-ACTION counts. Subsequent runs emit complete assessed metrics and provenance hashes directly.

| Seeded condition | Detected issue units | Candidate mutation risk | Executable now |
| --- | ---: | --- | --- |
| Orphan pages | 2 | MEDIUM | No |
| Duplicate titles | 1 pair | MEDIUM | No |
| Duplicate descriptions | 1 pair | MEDIUM | No |
| Weak internal links | 1 | MEDIUM | No |
| Broken internal link | 1 | MEDIUM | No |
| Canonical mismatch | 1 | HIGH | No |
| Accidental noindex | 1 | CRITICAL | No |
| Thin-content investigation | 2 | MEDIUM | No |
| Potential topic overlap | 1 pair | MEDIUM | No |
| Sitemap omission / nonexistent entry | 2 | CRITICAL | No |

These are investigation signals. Thinness and topic overlap do not mandate rewriting or merging; actual query cannibalisation requires Search Console evidence. Risk refers to the proposed mutation, separate from confidence in the observation. Static-site deployments are conservatively CRITICAL, including the separately authorised manual drill.

The evaluator reads frozen labels only after decisions are frozen. Labels are publicly inspectable in the source repository: runtime isolation is not a secret or blind holdout. Ground truth SHA-256: `924d52d55671481cd51ea37ead6ef36d46dc7bd99c113c77c156be83d1a1418d`.

## Public rollback

| Phase | Site merge commit | Cloudflare Pages deployment |
| --- | --- | --- |
| Baseline A | `424def79b9865afbd06940dc97380a9db0abf7e7` | `9275b651-13aa-4bb8-99b5-ce3c85125b29` |
| Temporary B | `06d6d493f627c1db42bc6d5afa74696660945739` | `339d5a0c-8d80-4092-9334-2454583af9a1` |
| Restored A | `2bdf8343b68823d323c5a30877c2a51c86e16810` | `552a409d-0ba9-4646-9ed7-bdf0b6aba387` |

[PR #2](https://github.com/DARKLORDVINAY/seo-test-lab/pull/2) added a temporary suffix to one page title and updated its inventory hash. After public B verification, independently reviewed [PR #3](https://github.com/DARKLORDVINAY/seo-test-lab/pull/3) restored the exact baseline Git tree using a new commit; no history was force-reset.

The restored full tracked tree is `962816b3497c193b0d161dfc60e301a92e80fabc`. The public inventory returned to `9b75ff958a5fd675efbe263b5e0dd91d6949759c5308169b03207cc6dc852c27`. All 26 public HTML hashes and observed directives matched baseline; all pages rendered; the test checklist and real 404 passed. A separate Cloud Browser read observed the restored original title. Non-HTML asset bytes were not individually downloaded and hashed; their tracked source tree was restored exactly and rendered behaviour passed.

Every manual GitHub mutation has an immutable intent and outcome receipt, before/after state, actor, reason, experiment and restoration procedure. The canonical release experiment is `530f3a79-a8b5-4038-a4bb-068a8895b118`. Manual deployment authorisation does not grant executor autonomy.

## Infrastructure and limits

[Foundation CI](https://github.com/DARKLORDVINAY/seo-autonomous/actions/runs/33568223519) passed **633 tests with zero skips**, including actual PostgreSQL 17.11 migrations, restricted-role enforcement and leases. Nonroot Docker/Compose services, repeatable bootstrap, dashboard rendering and lab browser checks passed. Public observation also used actual PostgreSQL.

Those CI services are disposable. A durable container/PostgreSQL host, backups, recurring worker and remote MCP HTTPS/OAuth connection remain unconnected. The existing backend worker is the authoritative scheduler once deployed. No continuous production service is claimed.

- The workspace's public crawler failed closed on DNS resolution. Its incomplete attempt recorded raw FN=13; that is not a valid detector-accuracy estimate. Public CI observation succeeded without weakening DNS/SSRF checks.
- A pre-existing Workers mirror fails preview builds. Its production builds succeed, as do the selected Cloudflare Pages deployments. The preview failure remains unresolved; no unrelated service configuration changed.
- CI artifact download through this workspace returned HTTP 403. Full SQL/JSON/browser bundles remain in the linked GitHub runs; public-run artifacts expire on **2026-09-15**. Checked API log summaries and the operator audit are preserved separately. They are not a fabricated import of the complete remote database.
- Live model execution was **false**. Deterministic diagnosis, bounded shadow packets and policy checks ran; live SDK reasoning and independent outcome calibration remain unverified.
- Google browser ownership and a dedicated GA4 test stream are now verified. Optional account sharing and enhanced measurement are off. The public verification tag is preserved; backend GSC/GA4 API access remains unverified.
- The practice event is `lab_checklist_complete`. Browser checks confirmed local completion without sending analytics. GA4 receipt is unverified; the event has no commercial value.

## Resume the critical path

Connect the owner's durable Docker/PostgreSQL environment using `DEPLOYMENT.md`, then bind the lab to read-only Google credentials for the already-created test properties. Do not recreate account setup. Keep secrets in the host's secret mechanism. Verify API property access and actual test-event delivery before claiming ingestion. Configure an explicitly approved model and price bound for live shadow reasoning through the existing backend configuration; the structural benchmark CLI intentionally remains deterministic.

Keep `AUTONOMY_LEVEL=1`, `PRODUCTION_ENABLED=false`, `SHADOW_MODE=true`, no earned categories and a zero site write budget. A structural pass and restoration do not satisfy the independent outcome/calibration requirements in `AUTONOMY_POLICY.md`.

Machine-readable evidence and exact identifiers are in `TEST_LAB_RESULTS.json`, `VERIFICATION.json` and `MISSION_STATE.json`.
