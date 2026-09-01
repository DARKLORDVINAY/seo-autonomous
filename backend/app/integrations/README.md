# Provider contracts and activation limits

Verified against official documentation on 2026-09-01. These are synchronous
HTTP adapters; no paid service or customer site was called during implementation.
Each test injects `httpx.MockTransport` and has no network dependency.

| Adapter | Stable entry point | Result |
| --- | --- | --- |
| GSC | `GSCClient(property_url, token_provider=None, client=None).fetch(start,end)` | `ObservationBatch[GSCRow]` |
| GSC page totals | `fetch_page_totals(start,end)` | Query-independent rows, still not guaranteed exhaustive |
| GSC inspection | `inspect_url(url)` | Google's stored inspection result; explicitly not a live test |
| GA4 | `GA4Client(property_id,...).fetch(start,end,conversion_definition=None)` | Organic Search sessions/key events; optional verified qualified counts/value |
| WordPress | `WordPressClient(base_url,username,application_password,...)` | `CMSProvider`; page/post-scoped identifiers |
| SERP | `DataForSEOClient(login,password,enabled=False).search(keyword,location_code,language_code)` | Raw completed task results with cost and locale |
| AI Mode | `AISearchClient(serp_client).search(...)` | Documented DataForSEO AI Mode endpoint |
| GitHub | `GitHubClient('owner/repo',token='').get_repository()/get_commits()` | Read-only repository metadata/commits |
| Crawler | `Crawler(site_url).crawl_url(url)/crawl_site(max_pages=100)` | `CrawlResult` / `ObservationBatch[CrawlResult]` |
| Fixture | `FixtureCMS(pages=None)`, `fixture_observations()`, `fixture_crawler()` | Explicit fixture provenance, no real business claim |

`ObservationBatch` includes `rows`, `source`, `quality_flags`, `complete`,
`metadata`, `fetched_at`, and an `is_fixture` property. A completed API request
never implies a complete dataset. GSC omitted dates and anonymized queries are
unknown; query rows must not be summed as exhaustive page totals. GA4 qualified
conversions and value remain `None` without a verified business conversion
definition. GA4 report metadata preserves thresholding/sampling signals.

Google clients use ADC read-only scopes. Configure workload identity or a mounted
`GOOGLE_APPLICATION_CREDENTIALS` file outside source control, and grant the
identity access to the actual GSC and GA4 properties. Never put tokens in prompts.

WordPress inventory requires `context=edit` and raw fields. IDs are `pages:123`
or `posts:123`; naked numeric IDs are rejected. Allowed update fields are title,
content, and meta description. Meta description requires an explicitly registered,
REST-exposed key; Yoast metadata is not assumed writable. Schema/configuration,
slug, canonical, delete, and publish mutations are unsupported. The caller must
route any supported write through the audited execution service.

Core WordPress does not provide the atomic fingerprint precondition needed to
exclude a concurrent editor between read and write. Existing-page writes are
blocked by default. `allow_optimistic_writes=True` is an explicit operator decision
for a controlled deployment, and must never be described as atomic. Fixture
compare-and-swap uses a lock. Draft creation always sends `status=draft`.
Write timeouts, inconclusive HTTP responses, and malformed success responses
raise `AmbiguousWriteError`; reconciliation is required before a new attempt.

DataForSEO requires explicit enabling and operator budget. A live POST can cost
money even though it observes a SERP. This adapter performs one attempt to avoid
duplicate charges; later samples are the scheduler's responsibility. Both the
outer status and nested task status must indicate completion before ingestion.
AI Mode samples are locale-specific observations, never universal visibility.

The crawler uses `httpcore`'s network backend interface. DNS is checked at each
new connection and the checked numeric IP is passed to the socket backend, while
TLS still validates the original hostname. All returned DNS addresses must be
public. Local/private, IPv6 transition, and proxy routes are excluded. Only
HTTPS/443 on the configured origin is fetched. Redirects are revalidated and
checked against robots. Requests use an explicit agent, a minimum interval,
per-response and aggregate byte caps, page/URL/depth limits, and defused XML.
Unknown/error robots responses stop crawling. Gzip responses, cross-origin
sitemaps, JavaScript rendering, external broken-link checks, and live Google
indexability tests are outside this first adapter's capability. Google's stored
index status is available independently through `inspect_url`.

Fixture mode requires an injected `MockTransport` and only accepts `example.test`.
HTML, JSON-LD, competitor text, and API-provided text remain untrusted external data.
The provider package has no authority to change agent policy or invoke tools.

GA4 qualified outcome ingestion:

`GA4Client.fetch(start, end, *, max_rows=50000, page_size=10000,
conversion_definition=None, outcome_holdback_days=12)` preserves the original
sessions/key-events report. A qualified definition enables a second report of
`eventCount` (and optionally `eventValue`) with a case-sensitive `eventName`
in-list filter, combined with the Organic Search filter. Counts join back by
date, landing page and channel; sessions are never summed across event names.
The client calls `checkCompatibility` with the exact qualified dimensions,
metrics and filter before reporting. The row budget applies to each report.

A conversion definition is a business attestation, not something inferred from
GA4 key events. For example:

```json
{
  "verified": true,
  "tracking_verified": true,
  "qualification_verified": true,
  "deduplication_verified": true,
  "qualified_events": ["qualified_form", "qualified_call"],
  "qualification_definition": "CRM-accepted leads for services we sell",
  "deduplication_method": "One event per CRM lead ID across mutually exclusive event names",
  "value_method": "fixed_per_qualified_conversion",
  "value_per_conversion": 40,
  "currency": "USD"
}
```

All four verification fields must be exact JSON `true`; qualification and
deduplication descriptions, distinct nonempty event names, and an uppercase
three-letter currency code are required. Deduplication must already occur in
the tracking/CRM pipeline, including across selected event names. This aggregate
adapter cannot deduplicate individual leads. `None` or `{}` keeps legacy
sessions-only behavior; an invalid nonempty definition fails before HTTP.

`fixed_per_qualified_conversion` requires a numeric finite nonnegative
`value_per_conversion` and is explicitly labeled `modeled_lead_value`, not
realized revenue. Alternatively, `value_method: "event_value"` requires
`currency_verified: true` and `value_semantics_verified: true`: the selected
events' numeric `value` parameters must represent qualified economic value in
the declared currency. A report currency setting alone does not verify those
units or business meaning. Generic key events, total revenue and arbitrary
event values are never promoted to qualified outcomes.

No matching qualified page/date row means `None`, never an invented zero.
Truncated, sampled, thresholded, restricted, or inconsistent reports cannot
certify outcomes; original session observations and privacy metadata remain
available. Metadata records the exact definition's `stable_hash`, verified
tracking/qualification/value semantics, and `scope: all_organic_landing_pages`
for the existing measurement service. A changed definition invalidates earlier
measurement evidence until it is collected under the new definition.

`complete_dates` also enforces an operator freshness policy. The default holds
back the property's current calendar date and the preceding 12 dates, using a
consistent IANA `timeZone` from every report page. The holdback accepts integers
from 0 to 365; zero still excludes the property's current date. Missing, invalid,
or conflicting report timezones prevent qualified coverage. Recent observation
rows carry `data_not_final`; older rows in the same batch do not inherit that
flag. `extraction_complete` / `extraction_complete_dates` separately describe
coverage before the holdback, and `held_back_dates` records the excluded dates.
This policy is not a guarantee of Google finality: late collection and revised
attribution can still change reports. The 12-day default reflects Google's
[documented data freshness limits](https://support.google.com/analytics/answer/11198161?hl=en).

References:

- [GSC Search Analytics](https://developers.google.com/webmaster-tools/v1/searchanalytics/query)
- [GSC URL Inspection](https://developers.google.com/webmaster-tools/v1/urlInspection.index/inspect)
- [GA4 runReport](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport)
- [GA4 schema](https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema)
- [GA4 compatibility](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/checkCompatibility)
- [GA4 filters](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/FilterExpression)
- [GA4 response metadata](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/ResponseMetaData)
- [Google ADC](https://google-auth.readthedocs.io/en/latest/reference/google.auth.html)
- [WordPress posts](https://developer.wordpress.org/rest-api/reference/posts/)
- [WordPress authentication](https://developer.wordpress.org/rest-api/using-the-rest-api/authentication/)
- [WordPress registered meta](https://developer.wordpress.org/rest-api/extending-the-rest-api/modifying-responses/)
- [DataForSEO organic](https://docs.dataforseo.com/v3/serp/google/organic/live/advanced/)
- [DataForSEO AI Mode](https://docs.dataforseo.com/v3/serp-google-ai_mode-live-advanced/)
- [GitHub repository API](https://docs.github.com/en/rest/repos/repos)
- [httpcore network backends](https://www.encode.io/httpcore/network-backends/)
