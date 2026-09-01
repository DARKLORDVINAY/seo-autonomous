# Public SEO Test Lab

This subtree adds a 26-page educational target to the existing SEO control plane.
Every page identifies itself as a demonstration. It contains no invented people,
customers, reviews, credentials, addresses, or commercial outcomes. The content
covers small web-publishing decisions; it is not a bulk keyword-content generator.

The site's purpose is to test observation, cautious diagnosis, proposals, and
recovery with known structural conditions. The control plane stays at Level 1,
with production writes disabled. Passing this small structural benchmark does not
establish real conversion improvement or earn Level 2.

## Build a release

The builder uses Python 3.12 standard-library modules only. From the repository
root, select the **actual stable public hostname** before building:

```bash
python3 test_lab/build.py --base-url https://YOUR-PROJECT.pages.dev --output test_lab/dist
```

Use a new or empty output directory. The builder refuses to overwrite a nonempty
release, its source directory, or a source ancestor. Public builds require a DNS
HTTPS origin without credentials, a path, query parameters, or a fragment. Build
output belongs in an ignored directory and is not source code.

For isolated fixture checks only:

```bash
python3 test_lab/build.py --fixture --base-url https://example.test --output artifacts/test-lab-fixture
```

Fixture mode accepts only that exact reserved origin and rejects analytics and
verification identifiers. Do not deploy a fixture build. The Python entry point
is `build_site(base_url, output, *, measurement_id=None,
verification_token=None, fixture=False)` and returns the release inventory.

## Source, release, and evaluator boundaries

| File | Purpose | Published by the builder? |
| --- | --- | --- |
| `pages.json` | Authored structured content, page purposes, routes, and metadata | No |
| `assets/site.css`, `assets/site.js` | Responsive presentation and a small practice interaction | Yes |
| `ground_truth.json` | Frozen expected issue units and clean controls for independent scoring | **No; never read by the builder** |
| Generated `inventory.json` | Page paths, exact document hashes, intended indexing, and general purpose | Yes |
| Generated `sitemap.xml`, `robots.txt` | Ordinary discovery signals, including controlled inconsistencies | Yes |
| Generated `404.html`, `_headers` | Missing-page presentation and host response policy | Yes |

The public inventory schema is:

```json
{
  "schema_version": 1,
  "base_url": "https://YOUR-PROJECT.pages.dev",
  "pages": [
    {
      "path": "/guides/descriptive-titles/",
      "content_sha256": "SHA256_OF_EXACT_INDEX_HTML_BYTES",
      "desired_indexing": "index",
      "purpose": "guide"
    }
  ]
}
```

Purposes are `home`, `hub`, `guide`, `note`, `exercise`, `reference`, and `utility`.
There are no issue IDs, seed labels, expected detector outputs, or rationales in
the public inventory. A control-plane registration must independently trust the
release and its manifest hash before treating inventory completeness or indexing
intent as authoritative. A public JSON document alone does not establish either.

Evaluate ground truth only after freezing the detector results. The manifest
contains 13 issue units, 13 clean control pages, and explicit NO-ACTION controls.
The same canonical/noindex condition can have multiple observable facets; those
facets are documented to avoid double counting. The lexical exercise overlap is
only **potential** cannibalisation. There is no fabricated query, search ranking,
traffic, conversion, or revenue data.

## Hosting and missing pages

The output uses directory routes such as `/guides/sitemaps/index.html`, served as
`/guides/sitemaps/`. It includes a root `404.html` and no SPA catch-all redirect.
Cloudflare documents that the root 404 file selects missing-page handling instead
of its automatic SPA fallback. The actual public HTTP status must still be tested.
[Cloudflare Pages serving behaviour](https://developers.cloudflare.com/pages/configuration/serving-pages/)

Use the stable production `<project>.pages.dev` hostname for the public lab, even
though the lab itself is noncommercial. Cloudflare adds `X-Robots-Tag: noindex`
to preview deployments; a hashed or branch preview would invalidate the intended
single-noindex condition. Check the public headers and both missing destinations
before accepting a benchmark. The documentation above was checked on 2026-09-01.

Do not enable hosting-side analytics, injected scripts, HTML rewrites, or redirects
without explicitly recording their effect on the release and its fingerprints.
The builder does not connect a hosting account, upload files, or certify a public
deployment. GitHub/Cloudflare integration and container infrastructure are managed
by the parent project, outside this static subtree.

## Optional Google tags and test events

Both tags are absent by default. The CLI accepts these **public identifiers** from
build environment variables, never a Google password, refresh token, or API secret:

- `GSC_VERIFICATION_TOKEN`: URL-safe verification token, 16–200 characters. Merely
  publishing a tag does not verify ownership; the account must confirm it.
- `GA4_MEASUREMENT_ID`: `G-` followed by ten uppercase letters or digits. Use a
  separate test property with enhanced measurement disabled. The ID alone does
  not configure API ingestion or prove receipt of an event.

No remote analytics code loads until the reader explicitly chooses **Allow test
analytics** on that page visit. Consent queues one explicit `page_view`, with
query/fragment-free location and origin-only referrer. Advertising features are
disabled. Consent is not stored in local or session storage; reload returns to
analytics off. Google may set analytics cookies after opt-in and receives normal
network request information. The host also processes requests needed to serve files.

The `/exercises/` checklist has three labelled prerequisites. Only after all three
are checked can the reader record completion. With prior explicit consent, that
queues exactly one `lab_checklist_complete` event during that page visit, with only
`lab_mode=true` and `lab_exercise="page-review"` as custom parameters. No name,
email, monetary value, or written response is collected. Completion without consent
stays local and is not sent retroactively after consent.

This is a **test conversion**, not a lead, purchase, revenue measure, or qualified
business outcome. The page reports queued events, not confirmed delivery. Receipt,
event deduplication across visits, channel attribution, and API ingestion require
independent verification. Consent-based observations are an incomplete sample.
The control plane must not infer commercial qualified conversion value from them.

## Verification

```bash
.venv/bin/pytest -q tests/test_lab_site.py
.venv/bin/ruff check test_lab/build.py tests/test_lab_site.py
```

The 39 focused tests independently inspect generated page count, the actual link
graph, exact metadata/directive scope, sitemap differences, escape boundaries,
manifest hashes, output isolation, and explicit fixture handling. An isolated Node
VM executes the actual browser JavaScript to test consent, prerequisite gating,
deduplication, late consent, URL minimisation, and analytics-load failure. These VM
checks make no remote requests and do not replace rendered-browser verification.

Actual public deployment, true missing-page statuses, rendered desktop/mobile
behaviour, ownership verification, event receipt, live PostgreSQL, and public
rollback must each have separate observed evidence. Keep any unavailable gate
explicitly pending in canonical state.
