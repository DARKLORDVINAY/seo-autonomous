# Controlled public SEO Test Lab

The first target is a 26-page educational demonstration, not a commercial website. The existing control plane remains canonical. Its Level 1 authority, disabled production writes, evidence requirements and earned-autonomy rules remain in force.

The site uses static HTML and Git instead of WordPress. It has no customers, reviews, address, professional credentials, purchase flow or commercial performance claims. Every page identifies the project as a demonstration. The two deliberately sparse notes are bounded test conditions, not a content-generation programme.

## Evidence and benchmark contract

`test_lab/pages.json` and `test_lab/assets/` build the public release. The generated `inventory.json` contains only page paths, exact HTML hashes, intended indexing and general purpose. Registration pins its SHA-256 from the owner's local release. The public collector checks the fetched manifest and each listed response against those bytes before claiming complete coverage. Unexpected pages, partial responses, rate limiting, missing inventory pages and truncated link graphs prevent a complete-graph conclusion.

`test_lab/ground_truth.json` is evaluator-only. It is neither published nor supplied to the crawler, detector or agent. Its initial SHA-256 is `924d52d55671481cd51ea37ead6ef36d46dc7bd99c113c77c156be83d1a1418d`. It defines 13 issue units, 13 clean controls and four explicit utility/NO-ACTION controls. Related symptoms from the same seed are not independent successes. Observation and decision evidence is committed before the evaluator reads the labels. Repeated runs cannot feed evaluator labels back through the agent failure-history input; the complete failure history remains available to the operator.

| Condition | Units | Expected interpretation |
| --- | ---: | --- |
| Orphans | 2 | No incoming HTML links in the verified inventory |
| Duplicate title and description | 2 | Two metadata defects on one pair, not proof of duplicate content |
| Weak internal links | 1 | One distinct incoming source; relevance review before adding a link |
| Broken internal link | 1 | An observed source link reaches a true 404 |
| Canonical mismatch | 1 | Wrong same-site preference under owner intent; high-risk review |
| Accidental noindex | 1 | Observed exclusion conflicts with attested indexing intent |
| Sparse informational notes | 2 | Investigate usefulness; no mandatory word quota |
| Topic overlap | 1 | A lexical hypothesis, not proven query cannibalisation |
| Sitemap omission and missing entry | 2 | Compare the declared release with directly observed sitemap responses |

Precision is TP/(TP+FP), recall is TP/(TP+FN), and zero detections never means perfect precision. Matching is one-to-one and checks available observed seed evidence, not merely category names. Every covered control receives an explicit NO-ACTION or INVESTIGATE decision; incomplete evidence yields NEEDS_EVIDENCE. The structural development gate requires a complete attested graph, zero FP/FN, correct controls, no website writes and all high/critical policy probes blocked.

This is a development benchmark whose categories informed implementation, not an independent generalisation or business-value estimate. Ground-truth isolation prevents answer leakage during execution but does not make the benchmark a blind holdout. New sites and prespecified independent outcome samples are required for broader claims.

## Run the artifact benchmark

Use a fresh output directory. Builds refuse to replace existing release bytes, preserving previous evidence:

```sh
.venv/bin/python test_lab/build.py --fixture --base-url https://example.test --output artifacts/lab-release
.venv/bin/python scripts/test_lab.py --mode artifact --base-url https://example.test --build-dir artifacts/lab-release --report artifacts/lab-report.json
```

The default dedicated canonical database is `artifacts/test-lab-artifact.sqlite3`. It does not replace the pre-existing foundation demo database. The same service accepts PostgreSQL through `LAB_DATABASE_URL`; keep credential-bearing URLs in host secrets, not shell arguments or chat. Artifact mode permits only the reserved `example.test` origin, uses no network provider, fabricates no search/analytics rows, and labels its crawl evidence as fixture data. It cannot qualify for live calibration.

The command executes the existing durable loop, freezes decisions and stores the benchmark, policy probes, explicit decisions and failure cases in canonical tables. Agents remain deterministic/unavailable until a live model is explicitly configured. A completed deterministic loop does not verify live SDK reasoning quality.

## GitHub → Cloudflare Pages

The existing repositories are now accessible: [`DARKLORDVINAY/seo-autonomous`](https://github.com/DARKLORDVINAY/seo-autonomous) contains the canonical control plane, site source, evaluator and CI; [`DARKLORDVINAY/seo-test-lab`](https://github.com/DARKLORDVINAY/seo-test-lab) contains generated public assets. The stable target is [seo-test-lab.pages.dev](https://seo-test-lab.pages.dev/). GitHub check receipts verify the existing Pages Git integration. The starter Search Console tag is preserved in `test_lab/public_target.json`; this public tag alone does not prove API access.

Build the reviewed release in the canonical source repository, then publish only its generated contents at the root of the site repository through a pull request. This preserves the working static-root deployment. The previous accidental HTML copy under `.github/workflows/` is unrelated to the generated release and is left intact. The ground truth never enters the deployed site directory. Since the canonical source repository is public, evaluator labels are publicly inspectable there: isolation is a runtime boundary, not secrecy or a blind holdout.

| Deployment setting | Current target |
| --- | --- |
| Site repository | `DARKLORDVINAY/seo-test-lab` |
| Production branch | `main`, with reviewed PRs |
| Public output | Generated static HTML/assets in repository root |
| Framework / functions | None |
| Public origin | `https://seo-test-lab.pages.dev` |
| Google verification | Preserve directly observed public token |
| GA4 | Unconfigured; no analytics requests |
| Control backend | Separate private container/PostgreSQL deployment required |

Cloudflare supports static HTML and Git-integrated Pages deployments. The exact account-side build settings are not available through the current connector; successful public readback, rather than a guessed configuration, is the release gate. [Static HTML deployment](https://developers.cloudflare.com/pages/framework-guides/deploy-anything/), [Git integration](https://developers.cloudflare.com/pages/configuration/git-integration/).

Use the Pages Free plan and do not enable paid products. The small static release is below the documented free file/build limits. No backend secret belongs in this public build. [Pages limits](https://developers.cloudflare.com/pages/platform/limits/).

Use the stable production Pages URL for the SEO benchmark. Branch/hashed preview deployments can carry a platform noindex header. Keep the top-level `404.html`: without it, Pages can treat unknown routes as an SPA and return the home page, destroying the broken-link ground truth. Verify actual public status codes and response headers before reporting a pass. [Serving Pages](https://developers.cloudflare.com/pages/configuration/serving-pages/).

Cloudflare hosts the public static target. The existing Python API, scheduler and PostgreSQL require a separate suitable container host; Pages is not their runtime. This task does not open a paid hosting account or invent credentials. The Docker image includes the lab registration/evaluation tooling; the existing nonroot services, restricted PostgreSQL role, persistent volume and migrations are reused.

```sh
python3 scripts/init_env.py
docker compose up -d --build --wait db api worker
```

See `DEPLOYMENT.md` for host TLS, backups, restricted role and secret mounting. The API remains local/private. The authoritative backend worker supplies repeat scheduling; no conversational context or ephemeral CI runner is the canonical database. CI now exercises the lab loop and restore drill against its disposable PostgreSQL service, alongside the existing real-role and container gates. Remote CI has to run successfully before those gates can be called verified.

## Public registration and repeated observation

After the actual stable Pages origin is known, build the exact release for that origin without `--fixture`. Register it using the pinned local inventory bytes:

```sh
.venv/bin/python test_lab/build.py --base-url "$LAB_BASE_URL" --output artifacts/lab-public-release
.venv/bin/python scripts/test_lab.py --mode public --base-url "$LAB_BASE_URL" --build-dir artifacts/lab-public-release --report artifacts/lab-public-report.json
```

Set `LAB_DATABASE_URL` to durable PostgreSQL on the container host. A public-mode SQLite run is a local diagnostic only. The CLI deliberately forces Level 1, zero write budget and deterministic agents. Provider bindings are explicit `--gsc-property` and `--ga4-property-id`; stale credentials for an unrelated business property cannot silently retarget the lab. A public fetch failure is recorded as unknown and cannot fall back to local artifacts. Once registered, the existing backend scheduler uses the lab ingestion path automatically.

If the release changes, re-register the new pinned manifest before evaluating it. Preserve old reports and immutable evidence. Do not loosen hashes simply to accommodate a deployment mismatch; first identify the deployed revision.

## Search Console and GA4 test conversions

For the assigned Pages subdomain, use a Search Console URL-prefix property and the HTML-tag verification method. Build the supplied public token into the home-page head using `GSC_VERIFICATION_TOKEN`, then complete ownership verification. The token is not a password. API read access still requires the correctly scoped Google account/service identity. [Search Console verification](https://support.google.com/webmasters/answer/9008080?hl=en).

Create or select a separate GA4 test property and web stream. Its public `G-...` measurement ID enables the optional UI, but no Google script or analytics request loads before an explicit per-page opt-in. The practice flow requires three checkboxes and one completion click; it queues at most one `lab_checklist_complete` event per page visit. This is a test interaction, never a purchase, qualified lead, revenue amount or genuine business success. There are no personal-data fields. Opted-in page views strip query strings and reduce referrers to their origin. These constraints, consent selection and browser/network failures limit acquisition coverage. No queued event is treated as verified delivery. [GA4 events](https://developers.google.com/analytics/devguides/collection/ga4/events).

Mark `lab_checklist_complete` as a key event in the **test** property, then verify delivery in Realtime/DebugView and subsequent Data API rows. Do not set the control plane's commercial conversion mapping to verified. Key-event counts and monetary value remain separate. Creating a GA4 property requires account authority beyond the read-only ingestion identity. [GA4 property creation](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties/create).

## Browser and rollback gates

`scripts/check_test_lab.cjs` checks all release pages, desktop/mobile layouts, the three-step practice interaction, no unexpected analytics calls and a real 404. It can use a local HTTP release on a suitable CI host, or `LAB_PUBLIC_URL` for the actual deployment. The current Work browser cannot open local-file URLs, and its runtime denied opening a local preview server. These are recorded limitations, not passing browser checks.

`scripts/lab_rollback_drill.py` creates an isolated Git copy, records a baseline, changes one title plus its inventory hash, commits the change, reverts it and compares the whole restored tree. It stores before/after bytes, commit/tree identifiers, an experiment and immutable action/rollback events. The original release directory is untouched. The command rejects live sites and requires a Level 1, write-disabled fixture site.

A public rollback must complete an observed drill: record successful deployment A, publish a bounded reviewed test revision B, verify B on the stable URL, restore A and verify all inventory/body hashes and status codes again. Record both deployment IDs, commits, timestamps, reviewer, experiment and any partial failures. Cloudflare's rollback feature targets successful production deployments, not previews. Restore the corresponding Git source so a later automatic build does not reintroduce B. [Pages rollbacks](https://developers.cloudflare.com/pages/configuration/rollbacks/).

Neither an artifact restore nor a perfect structural benchmark enables Level 2. Public deployment, actual PostgreSQL/container and rendered-browser gates, an observed public restore, relevant independent outcome evidence, calibration, and an explicit category-specific human graduation decision remain required under `AUTONOMY_POLICY.md`.

## Manual deployment audit

`scripts/lab_operator_audit.py` records the build operator’s bounded GitHub operations against the same canonical site. It cannot call GitHub, publish, run commands, or enable the executor. Every external mutation has intent, before/after state, actor, reason, experiment and rollback procedure, followed by an immutable tool receipt. Ref/merge operations are conservatively CRITICAL because they can trigger deployment. This authority comes from the user’s explicit sandbox build/deploy/rollback request; it is not Level 2 autonomy.

The `public-lab.yml` workflow observes the real public deployment with an isolated PostgreSQL service and browser. It can be manually dispatched or triggered by a reviewed `verify/public-lab-*` branch. This second route is available when the connected GitHub app can push branches but cannot dispatch workflows. It grants no website mutation capability.

The workflow checks out the exact owner-reviewed site commit pinned in `test_lab/public_target.json`, verifies its independently pinned manifest hash, and compares the live site against it. This supports attesting both a baseline and a temporary rollback-drill revision without reading expectations from the live site itself. Its downloadable state is a CI audit snapshot, not a durable service or authoritative scheduler. The existing backend worker remains the operational scheduler once a durable host is connected.
